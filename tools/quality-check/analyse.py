#!/usr/bin/env python3
"""Quality check for the test pattern: counts frames, finds picture and sound
faults, measures A/V offset and end-to-end delay, and prints one plain report.

Two ways to use it:

  analyse.py capture --srt 'srt://:4000?mode=listener' --seconds 60 --out DIR
      Receive the stream (srt-live-transmit), record it, stamp each decoded
      frame with the receiver's wall clock, keep the receiver's SRT stats, then
      print the report.

  analyse.py report REC.ts [--arrivals DIR/arrivals] [--srt-stats FILE]
                           [--sender-stats FILE.jsonl] [--srtla-stats FILE.jsonl]
      Report on a recording made elsewhere. Without arrival stamps the report
      has no end-to-end delay.

Needs ffmpeg/ffprobe on PATH (and srt-live-transmit for capture). Python 3.9+,
standard library only.
"""
import argparse
import json
import math
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from array import array
from operator import mul
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pattern_layout as L  # noqa: E402

SR = 48000


# ---------------------------------------------------------------- helpers ---

def run(cmd, **kw):
    return subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kw)


def ffprobe_json(path, *args):
    out = run(["ffprobe", "-v", "error", "-of", "json", *args, str(path)]).stdout
    return json.loads(out or b"{}")


def fmt_n(n):
    return f"{n:,}"


def fmt_t(sec):
    """Position in the recording as m:ss.s"""
    if sec is None:
        return "?"
    m, s = divmod(max(sec, 0.0), 60)
    return f"{int(m)}:{s:04.1f}"


def pct(a, b):
    return 100.0 * a / b if b else 0.0


def read_json_objects(path):
    """srt-live-transmit writes one JSON object per report; tolerate junk."""
    text = Path(path).read_text(errors="replace")
    dec = json.JSONDecoder()
    i, objs = 0, []
    while True:
        j = text.find("{", i)
        if j < 0:
            break
        try:
            o, i = dec.raw_decode(text, j)
            objs.append(o)
        except json.JSONDecodeError:
            i = j + 1
    return objs


def read_jsonl(path):
    objs = []
    for line in Path(path).read_text(errors="replace").splitlines():
        line = line.strip()
        if line:
            try:
                objs.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return objs


# ------------------------------------------------------- pattern read-out ---

ROWS = [  # (name, pairs, half width, y, height)
    ("frame", L.FRAME_BITS, L.FRAME_HALF, L.FRAME_Y, L.FRAME_H),
    ("epoch0", L.EPOCH_PER_ROW, L.EPOCH_HALF, L.EPOCH_ROWS_Y[0], L.EPOCH_H),
    ("epoch1", L.EPOCH_BITS - L.EPOCH_PER_ROW, L.EPOCH_HALF, L.EPOCH_ROWS_Y[1], L.EPOCH_H),
]
SAMPLES_PER_PAIR = 8
READOUT_W = sum(r[1] * SAMPLES_PER_PAIR for r in ROWS) + 1  # + marker
STATIC_TW, STATIC_TH = 64, 16
MIN_MARGIN = 40  # grey levels between a block and its inverse


def readout_filters(w, h, src):
    """Filter chain that turns each frame into READOUT_W x 1 grey samples."""
    sx, sy = w / L.REF_W, h / L.REF_H
    parts, labels = [], []
    n = len(ROWS) + 1
    parts.append(f"[{src}]split={n}" + "".join(f"[ro{i}]" for i in range(n)))
    for i, (_, pairs, half, y, hh) in enumerate(ROWS):
        cw = int(round(pairs * 2 * half * sx))
        ch = max(2, int(round(hh * sy / 2)))
        cy = int(round((y + hh / 4) * sy))
        parts.append(f"[ro{i}]crop={cw}:{ch}:0:{cy},extractplanes=y,"
                     f"scale={pairs * SAMPLES_PER_PAIR}:1:flags=area[rc{i}]")
        labels.append(f"[rc{i}]")
    mx = int(round((L.MARK_X + L.MARK_W / 4) * sx))
    my = int(round((L.MARK_Y + L.MARK_H / 4) * sy))
    mw, mh = int(round(L.MARK_W / 2 * sx)), int(round(L.MARK_H / 2 * sy))
    parts.append(f"[ro{n - 1}]crop={mw}:{mh}:{mx}:{my},extractplanes=y,scale=1:1:flags=area[rcm]")
    labels.append("[rcm]")
    parts.append("".join(labels) + f"hstack=inputs={len(labels)}")
    return ";".join(parts)


def field_readout(w, h):
    """Frame-number row only, for one field (half height)."""
    sx, sy = w / L.REF_W, (h // 2) / L.REF_H
    _, pairs, half, y, hh = ROWS[0]
    cw = int(round(pairs * 2 * half * sx))
    ch = max(2, int(round(hh * sy / 2)))
    cy = int(round((y + hh / 4) * sy))
    return f"crop={cw}:{ch}:0:{cy},extractplanes=y,scale={pairs * SAMPLES_PER_PAIR}:1:flags=area"


def decode_pairs(vals, off, pairs):
    """Return (value, min margin) for `pairs` bits starting at sample `off`."""
    v, margin = 0, 255
    for k in range(pairs):
        b = off + k * SAMPLES_PER_PAIR
        a = (vals[b + 1] + vals[b + 2]) / 2
        z = (vals[b + 5] + vals[b + 6]) / 2
        if a > z:
            v |= 1 << k
        margin = min(margin, abs(a - z))
    return v, margin


def decode_readout(vals):
    off = 0
    out = {}
    for name, pairs, *_ in ROWS:
        out[name] = decode_pairs(vals, off, pairs)
        off += pairs * SAMPLES_PER_PAIR
    frame, fm = out["frame"]
    e0, m0 = out["epoch0"]
    e1, m1 = out["epoch1"]
    epoch = e0 | (e1 << L.EPOCH_PER_ROW)
    darks, lights = [], []
    for k in range(L.FRAME_BITS):
        b = k * SAMPLES_PER_PAIR
        a, z = (vals[b + 1] + vals[b + 2]) / 2, (vals[b + 5] + vals[b + 6]) / 2
        darks.append(min(a, z))
        lights.append(max(a, z))
    return {
        "black": statistics.median(darks), "white": statistics.median(lights),
        "code": frame if fm >= MIN_MARGIN else None,
        "epoch": epoch if min(m0, m1) >= MIN_MARGIN else None,
        "marker": vals[-1],
    }


def read_raw_frames(path, size):
    data = Path(path).read_bytes()
    return [data[i:i + size] for i in range(0, len(data) - size + 1, size)]


def parse_meta(path):
    """Parse ffmpeg metadata=print output into a list of dicts (one per frame)."""
    frames = []
    cur = None
    for line in Path(path).read_text(errors="replace").splitlines():
        if line.startswith("frame:"):
            cur = {}
            for tok in line.split():
                if ":" in tok:
                    k, v = tok.split(":", 1)
                    cur[k] = v
            frames.append(cur)
        elif cur is not None and "=" in line:
            k, v = line.split("=", 1)
            cur[k.strip()] = v.strip()
    return frames


# --------------------------------------------------------------- capture ---

def capture(args):
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    rec = out / "rec.ts"
    stats = out / "srt-stats.json"
    arr = out / "arrivals"
    arr.mkdir(exist_ok=True)
    slt_cmd = ["srt-live-transmit", "-q", "-s", "1000", "-pf", "json", "-statsout", str(stats)]
    slt_cmd += ["-a", "no" if args.no_reconnect else "yes"]
    slt = subprocess.Popen(
        slt_cmd + [args.srt, "file://con"],
        stdout=subprocess.PIPE, stderr=open(out / "srt-live-transmit.log", "wb"))
    # Live decode: stamp every decoded frame with the wall clock (RTCTIME, in
    # microseconds) and keep the pattern read-out for it.
    w, h = args.width, args.height
    graph = ("[0:v]split=2[lv][lm];"
             + readout_filters(w, h, "lv") + "[code];"
             + "[lm]setpts=RTCTIME/(TB*1000000),metadata=mode=add:key=arrival:value=1,"
             + "metadata=print:file=" + str(arr / "meta.txt") + ",nullsink")
    dec = subprocess.Popen(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-threads", "1",
         "-fflags", "nobuffer", "-probesize", "2000000", "-analyzeduration", "3000000",
         "-f", "mpegts", "-i", "pipe:0", "-filter_complex", graph,
         "-map", "[code]", "-fps_mode", "passthrough", "-f", "rawvideo", str(arr / "code.raw")],
        stdin=subprocess.PIPE, stderr=open(arr / "ffmpeg.log", "wb"))
    deadline = time.monotonic() + args.seconds
    started = None
    with open(rec, "wb") as f:
        os.set_blocking(slt.stdout.fileno(), False)
        while time.monotonic() < deadline or started is None and time.monotonic() < deadline + args.wait:
            try:
                chunk = slt.stdout.read(65536)
            except BlockingIOError:
                chunk = None
            if chunk:
                if started is None:
                    started = time.monotonic()
                    deadline = started + args.seconds
                f.write(chunk)
                try:
                    dec.stdin.write(chunk)
                except BrokenPipeError:
                    pass
            else:
                if slt.poll() is not None:
                    break
                time.sleep(0.005)
    slt.terminate()
    try:
        dec.stdin.close()
    except BrokenPipeError:
        pass
    dec.wait(timeout=30)
    slt.wait(timeout=10)
    print(f"recorded {rec} ({rec.stat().st_size:,} bytes)", file=sys.stderr)
    args.recording = str(rec)
    args.arrivals = str(arr)
    args.srt_stats = str(stats)
    if args.no_report:
        return 0
    return report(args)


# --------------------------------------------------------------- analysis ---

def analyse_video(rec, workdir, fps):
    info = ffprobe_json(rec, "-select_streams", "v:0", "-show_entries",
                        "stream=width,height,r_frame_rate,codec_name,start_time,color_range")
    st = (info.get("streams") or [{}])[0]
    w, h = int(st.get("width") or 0), int(st.get("height") or 0)
    if not w or not h:
        return {"width": 0, "height": 0, "codec": st.get("codec_name"), "declared_fps": None,
                "frames": [], "decode_errors": 0, "no_params": True, "idet": {},
                "mpdecimate_kept": None, "blocks": [], "color_range": None}
    code_raw = workdir / "code.raw"
    static_raw = workdir / "static.raw"
    meta = workdir / "meta.txt"
    meta_b = workdir / "meta_bottom.txt"
    sx, sy = w / L.REF_W, h / L.REF_H
    stc = (f"crop={int(L.STATIC_W * sx)}:{int(L.STATIC_H * sy)}:{int(L.STATIC_X * sx)}:"
           f"{int(L.STATIC_Y * sy)},extractplanes=y,scale={STATIC_TW}:{STATIC_TH}:flags=area")
    meta_dec = workdir / "meta_dec.txt"
    meta_blk = workdir / "meta_blk.txt"
    by = int(L.BALL_Y * sy)
    bh = int(L.BALL_H * sy) // 2 * 2
    graph = (
        "[0:v]split=8[va][vs][vq][vb][vm][vk][vi][vf];"
        + readout_filters(w, h, "va") + "[code];"
        + f"[vs]{stc}[st];"
        + f"[vq]scale=480:270,signalstats,freezedetect=n=0.001:d=0.3,blackdetect=d=0.1:pix_th=0.10,"
          f"metadata=print:file={meta}[q];"
        + f"[vb]crop=iw:ih/2:0:ih/2,scale=480:135,signalstats,metadata=print:file={meta_b}[qb];"
        # passive checks that also work on a real stream:
        + f"[vm]crop={int(L.NOISE_W * sx)}:{int(L.NOISE_H * sy)}:{int(L.NOISE_X * sx)}:{int(L.NOISE_Y * sy)},"
          f"mpdecimate,metadata=mode=add:key=kept:value=1,metadata=print:file={meta_dec}[qm];"
        # blockiness on the noise patch, sampled once a second (cheap)
        + f"[vk]select='not(mod(n\\,{int(round(fps))}))',crop={int(L.NOISE_W * sx)}:{int(L.NOISE_H * sy)}:"
          f"{int(L.NOISE_X * sx)}:{int(L.NOISE_Y * sy)},blockdetect,metadata=print:file={meta_blk}[qk];"
        + f"[vi]crop=iw:{bh}:0:{by},idet[qi];"
        # each field on its own: fields from different moments mean interlaced video
        + f"[vf]split=2[vf1][vf2];[vf1]field=top,{field_readout(w, h)}[ft];[vf2]field=bottom,{field_readout(w, h)}[fb]"
    )
    p = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "info", "-threads", "2", "-copyts", "-i", str(rec),
         "-filter_complex", graph,
         "-map", "[code]", "-fps_mode", "passthrough", "-f", "rawvideo", "-y", str(code_raw),
         "-map", "[st]", "-fps_mode", "passthrough", "-f", "rawvideo", "-y", str(static_raw),
         "-map", "[q]", "-fps_mode", "passthrough", "-f", "null", "-",
         "-map", "[qb]", "-fps_mode", "passthrough", "-f", "null", "-",
         "-map", "[qm]", "-fps_mode", "passthrough", "-f", "null", "-",
         "-map", "[qk]", "-fps_mode", "passthrough", "-f", "null", "-",
         "-map", "[qi]", "-fps_mode", "passthrough", "-f", "null", "-",
         "-map", "[ft]", "-fps_mode", "passthrough", "-f", "rawvideo", "-y", str(workdir / "ftop.raw"),
         "-map", "[fb]", "-fps_mode", "passthrough", "-f", "rawvideo", "-y", str(workdir / "fbot.raw")],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    log = p.stderr.decode(errors="replace")
    decode_errors = sum(1 for ln in log.splitlines()
                        if ln.startswith("[h264") or ln.startswith("[hevc") or "error while decoding" in ln)
    no_params = any("non-existing PPS" in ln or "no frame!" in ln or "sps_id" in ln.lower()
                    for ln in log.splitlines())
    idet = {}
    for ln in log.splitlines():
        if "Multi frame detection:" in ln:
            for k in ("TFF", "BFF", "Progressive", "Undetermined"):
                try:
                    idet[k] = int(ln.split(k + ":")[1].split()[0])
                except (IndexError, ValueError):
                    pass
    kept = sum(1 for f in parse_meta(meta_dec)) if meta_dec.exists() else None
    fw = L.FRAME_BITS * SAMPLES_PER_PAIR
    ftop = read_raw_frames(workdir / "ftop.raw", fw)
    fbot = read_raw_frames(workdir / "fbot.raw", fw)
    field_mismatch = sum(1 for a_, b_ in zip(ftop, fbot)
                         if decode_pairs(a_, 0, L.FRAME_BITS) [0] != decode_pairs(b_, 0, L.FRAME_BITS)[0]
                         and decode_pairs(a_, 0, L.FRAME_BITS)[1] >= MIN_MARGIN
                         and decode_pairs(b_, 0, L.FRAME_BITS)[1] >= MIN_MARGIN)
    blocks = [(float(m.get("pts_time", "nan")), float(m["lavfi.block"]))
              for m in parse_meta(meta_blk) if "lavfi.block" in m] if meta_blk.exists() else []
    metas = parse_meta(meta)
    metas_b = parse_meta(meta_b)
    reads = read_raw_frames(code_raw, READOUT_W)
    statics = read_raw_frames(static_raw, STATIC_TW * STATIC_TH)
    n = min(len(metas), len(reads), len(statics))
    frames = []
    for i in range(n):
        m = metas[i]
        d = decode_readout(reads[i])
        mb = metas_b[i] if i < len(metas_b) else {}
        frames.append({
            "t": float(m.get("pts_time", "nan")),
            **d,
            "y": float(m.get("lavfi.signalstats.YAVG", "nan")),
            "u": float(m.get("lavfi.signalstats.UAVG", "nan")),
            "v": float(m.get("lavfi.signalstats.VAVG", "nan")),
            "yb": float(mb.get("lavfi.signalstats.YAVG", "nan")),
            "ub": float(mb.get("lavfi.signalstats.UAVG", "nan")),
            "vb": float(mb.get("lavfi.signalstats.VAVG", "nan")),
            "freeze_start": "lavfi.freezedetect.freeze_start" in m,
            "freeze_end": "lavfi.freezedetect.freeze_end" in m,
            "black_start": "lavfi.black_start" in m,
            "static": statics[i],
        })
    return {"width": w, "height": h, "codec": st.get("codec_name"),
            "declared_fps": st.get("r_frame_rate"), "frames": frames,
            "decode_errors": decode_errors, "no_params": no_params, "idet": idet,
            "mpdecimate_kept": kept, "blocks": blocks,
            "field_mismatch": field_mismatch, "field_frames": min(len(ftop), len(fbot)),
            "color_range": st.get("color_range")}


def damage_flags(frames):
    """Frames whose static colour-bar area differs from the usual picture."""
    if not frames:
        return []
    size = len(frames[0]["static"])
    sample = frames[:: max(1, len(frames) // 300)]
    ref = bytes(int(statistics.median(f["static"][j] for f in sample)) for j in range(size))
    out = []
    for f in frames:
        diff = sum(abs(a - b) for a, b in zip(f["static"], ref)) / size
        out.append(diff)
    return out


def is_green(y, u, v):
    # All-zero YUV decodes as flat dark green: U and V far below neutral (128).
    return u < 60 and v < 60 and y < 100


def frame_accounting(frames, fps):
    """Count frames per run of the pattern. A new pattern clock means the
    sender restarted. A frame number that doesn't fit the time since the last
    good frame is a damaged read (garbled), not a jump."""
    res = {"sent": 0, "arrived": 0, "repeated": 0, "missing": 0, "reordered": 0,
           "garbled": 0, "restarts": 0, "gaps": [], "repeat_runs": [], "restart_times": []}
    readable = [i for i, f in enumerate(frames) if f["code"] is not None]
    res["garbled_idx"] = []

    def persistent_epoch(pos, ep):
        nxt = [frames[j]["epoch"] for j in readable[pos:pos + 10]]
        return ep is not None and nxt.count(ep) >= 5

    seg = None  # dict(epoch, lo, hi, seen, last_c, last_t)
    segs = []
    run_start = None
    for pos, i in enumerate(readable):
        f = frames[i]
        c, t, ep = f["code"], f["t"], f["epoch"]
        if seg is not None and ep is not None and seg["epoch"] is not None and ep != seg["epoch"] \
                and persistent_epoch(pos, ep):
            res["restarts"] += 1
            res["restart_times"].append(t)
            seg = None
        if seg is None:
            seg = {"epoch": ep, "lo": c, "hi": c, "seen": {c}, "last_c": c, "last_t": t}
            segs.append(seg)
            continue
        if seg["epoch"] is None and ep is not None and persistent_epoch(pos, ep):
            seg["epoch"] = ep
        dt = t - seg["last_t"]
        expected = seg["last_c"] + dt * fps
        if not (seg["last_c"] - 3 <= c <= expected + 3 + 0.2 * abs(dt) * fps):
            # Timestamps may jump (reconnects, relays). Take the new number as
            # the anchor if the next frames continue from it.
            nxt = [frames[j] for j in readable[pos + 1:pos + 6]]
            agree = sum(1 for g in nxt if abs(g["code"] - (c + (g["t"] - t) * fps)) <= 3)
            if c > seg["last_c"] and len(nxt) >= 3 and agree >= len(nxt) - 1:
                res["time_jumps"] = res.get("time_jumps", 0) + 1
            else:
                res["garbled"] += 1
                res["garbled_idx"].append(i)
                continue
        d = c - seg["last_c"]
        if d == 0:
            res["repeated"] += 1
            if run_start is None:
                run_start = t
        else:
            if run_start is not None:
                res["repeat_runs"].append(run_start)
                run_start = None
            if d > 1:
                res["gaps"].append((t, d - 1))
            elif d < 0:
                res["reordered"] += 1
        seg["seen"].add(c)
        seg["hi"] = max(seg["hi"], c)
        seg["last_c"], seg["last_t"] = c, t
    for sg in segs:
        res["sent"] += sg["hi"] - sg["lo"] + 1
        res["arrived"] += len(sg["seen"])
        res["missing"] += (sg["hi"] - sg["lo"] + 1) - len(sg["seen"])
    return res


def longest_still(frames, fps):
    """Longest time the picture showed no new frame number, in seconds."""
    best, best_at = 0.0, None
    last_new_t, last_code = None, None
    for f in frames:
        c = f["code"]
        if c is None:
            continue
        if c != last_code:
            if last_new_t is not None and f["t"] - last_new_t > best:
                best, best_at = f["t"] - last_new_t, last_new_t
            last_new_t, last_code = f["t"], c
    return best, best_at


def video_packets(rec):
    """Keyframe times and video bytes per whole second (by DTS order)."""
    data = ffprobe_json(rec, "-select_streams", "v:0", "-show_entries", "packet=pts_time,dts_time,flags,size")
    kts, per_sec = [], {}
    for p in data.get("packets", []):
        t = p.get("dts_time") if p.get("dts_time") not in (None, "N/A") else p.get("pts_time")
        if t in (None, "N/A"):
            continue
        t = float(t)
        if "K" in p.get("flags", ""):
            kts.append(t)
        per_sec[int(t)] = per_sec.get(int(t), 0) + int(p.get("size", 0))
    return kts, per_sec


def audio_packets(rec):
    data = ffprobe_json(rec, "-select_streams", "a:0", "-show_entries",
                        "packet=pts_time,duration_time:stream=sample_rate,channels,start_time")
    st = (data.get("streams") or [{}])[0]
    pk = [(float(p["pts_time"]), float(p.get("duration_time") or 0))
          for p in data.get("packets", []) if p.get("pts_time") not in (None, "N/A")]
    gaps = []
    for (t0, d0), (t1, _) in zip(pk, pk[1:]):
        if d0 and t1 - (t0 + d0) > d0 * 0.5:
            gaps.append((t0 + d0, t1 - (t0 + d0)))
    return {"sample_rate": int(st.get("sample_rate", 0) or 0), "channels": int(st.get("channels", 0) or 0),
            "start": float(st.get("start_time", "nan")), "ts_gaps": gaps}


def ls_tone(x, f, rate=SR):
    """Least-squares fit of a sine at f over x: returns (amplitude, residual rms)."""
    n = len(x)
    w = 2 * math.pi * f / rate
    c = [math.cos(w * i) for i in range(n)]
    s = [math.sin(w * i) for i in range(n)]
    cc = sum(map(mul, c, c))
    ss = sum(map(mul, s, s))
    cs = sum(map(mul, c, s))
    xc = sum(map(mul, x, c))
    xs = sum(map(mul, x, s))
    xx = sum(map(mul, x, x))
    det = cc * ss - cs * cs
    if det == 0:
        return 0.0, math.sqrt(xx / n) if n else 0.0
    a = (xc * ss - xs * cs) / det
    b = (xs * cc - xc * cs) / det
    proj = a * xc + b * xs
    return math.hypot(a, b), math.sqrt(max(xx - proj, 0.0) / n)


class ToneFitter:
    """Precomputed least-squares fit at one frequency for one window length."""

    def __init__(self, f, n, rate=SR):
        w = 2 * math.pi * f / rate
        self.c = [math.cos(w * i) for i in range(n)]
        self.s = [math.sin(w * i) for i in range(n)]
        cc = sum(map(mul, self.c, self.c))
        ss = sum(map(mul, self.s, self.s))
        cs = sum(map(mul, self.c, self.s))
        self.det = cc * ss - cs * cs
        self.cc, self.ss, self.cs, self.n = cc, ss, cs, n

    def fit_ab(self, x):
        xc = sum(map(mul, x, self.c))
        xs = sum(map(mul, x, self.s))
        a = (xc * self.ss - xs * self.cs) / self.det
        b = (xs * self.cc - xc * self.cs) / self.det
        return a, b

    def fit(self, x):
        xc = sum(map(mul, x, self.c))
        xs = sum(map(mul, x, self.s))
        xx = sum(map(mul, x, x))
        a = (xc * self.ss - xs * self.cs) / self.det
        b = (xs * self.cc - xc * self.cs) / self.det
        proj = a * xc + b * xs
        return math.hypot(a, b), math.sqrt(max(xx - proj, 0.0) / self.n)


def zero_cross_freq(x, rate=SR):
    idx = [i for i in range(1, len(x)) if (x[i - 1] < 0) != (x[i] < 0)]
    if len(idx) < 20:
        return None
    # linear interpolation of the first and last crossing
    def at(i):
        a, b = x[i - 1], x[i]
        return i - 1 + (a / (a - b) if a != b else 0.5)
    span = at(idx[-1]) - at(idx[0])
    return (len(idx) - 1) / 2 / span * rate if span > 0 else None


def analyse_audio(rec, workdir, info):
    raw = workdir / "audio.f32"
    # aresample=async keeps sample index and timestamp in step across gaps.
    run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(rec), "-map", "0:a:0",
         "-af", "aresample=async=1:first_pts=0", "-ac", "2", "-ar", str(SR), "-f", "f32le", "-y", str(raw)])
    buf = array("f")
    buf.frombytes(raw.read_bytes())
    left, right = buf[0::2], buf[1::2]
    n = len(left)
    t0 = info["start"]

    # per-channel stats and silence via ffmpeg (astats, silencedetect)
    p = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-copyts", "-i", str(rec), "-map", "0:a:0", "-af",
         "astats=measure_overall=none,silencedetect=n=-50dB:d=0.02:mono=1,ebur128=peak=true",
         "-f", "null", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    log = p.stderr.decode(errors="replace")
    astats, ch = {}, None
    silences = []
    open_sil = {}
    for ln in log.splitlines():
        if "Channel:" in ln:
            ch = int(ln.split("Channel:")[1].strip())
            astats[ch] = {}
        elif ch is not None and "astats" in ln and ":" in ln:
            k, _, v = ln.split("]", 1)[1].strip().partition(":")
            astats[ch][k.strip()] = v.strip()
        if "silence_start" in ln:
            c = int(ln.split("channel:")[1].split("|")[0].strip()) if "channel:" in ln else 0
            open_sil[c] = float(ln.split("silence_start:")[1].split()[0])
        elif "silence_end" in ln:
            c = int(ln.split("channel:")[1].split("|")[0].strip()) if "channel:" in ln else 0
            end = float(ln.split("silence_end:")[1].split()[0])
            dur = float(ln.split("silence_duration:")[1].split()[0])
            silences.append((c, end - dur, dur))
            open_sil.pop(c, None)
    loud = {}
    tail = log[log.rfind("Summary:"):] if "Summary:" in log else ""
    for key, label in (("I:", "integrated_lufs"), ("LRA:", "range_lu"), ("Peak:", "true_peak_dbfs")):
        if key in tail:
            try:
                loud[label] = float(tail.split(key)[1].split()[0])
            except (IndexError, ValueError):
                pass
    dur_total = n / SR
    for c, s in open_sil.items():
        silences.append((c, s, dur_total - s))

    # beep onsets: 2 kHz fit on L+R, 4 ms windows, then 1 ms refinement
    W4 = 192
    f2k = ToneFitter(L.BEEP_HZ, W4)

    def beep_amp(j):
        # 2 kHz amplitude per channel, added: an inverted channel can't cancel it
        return f2k.fit(left[j:j + W4])[0] + f2k.fit(right[j:j + W4])[0]
    thr = 0.6 * L.BEEP_AMP  # summed beep is 2x BEEP_AMP; still found with one channel muted
    onsets = []
    i = 0
    last = -SR
    while i + W4 <= n:
        a = beep_amp(i)
        if a > thr and i - last > SR // 2:
            j0 = max(0, i - W4)
            j = j0
            while j + W4 <= n and j <= i:
                a2 = beep_amp(j)
                if a2 > thr:
                    break
                j += SR // 1000
            # A window first exceeds the threshold when about half of it
            # overlaps the beep: beep start ~ j + W4/2. Find the end the same
            # way and keep the centre (GStreamer centres each tick on the
            # second, and the centre is immune to ramps and level changes).
            k = j + W4
            while k + W4 <= n:
                a3 = beep_amp(k)
                if a3 < thr:
                    break
                k += SR // 1000
            onsets.append(((j + k) / 2 + W4 / 2) / SR)
            last = i
            i += SR // 2
            continue
        i += W4
    # per-channel tone fit per 10 ms window, skipping beeps
    W = 480
    fitL, fitR = ToneFitter(L.LEFT_HZ, W), ToneFitter(L.RIGHT_HZ, W)
    fitLx, fitRx = ToneFitter(L.RIGHT_HZ, W), ToneFitter(L.LEFT_HZ, W)
    beep_windows = set()
    for o in onsets:
        for k in range(int((o - 0.05) * SR) // W, int((o + 0.05) * SR) // W + 1):
            beep_windows.add(k)
    win = []
    for k in range(n // W):
        if k in beep_windows:
            continue
        xl, xr = left[k * W:(k + 1) * W], right[k * W:(k + 1) * W]
        al, rl = fitL.fit(xl)
        ar, rr = fitR.fit(xr)
        win.append((k * W / SR, al, rl, ar, rr))
    # swapped channels check on a few windows
    swaps = 0
    for k in range(0, n // W, max(1, (n // W) // 50)):
        if k in beep_windows:
            continue
        xl, xr = left[k * W:(k + 1) * W], right[k * W:(k + 1) * W]
        if fitLx.fit(xl)[0] > 2 * fitL.fit(xl)[0] and fitRx.fit(xr)[0] > 2 * fitR.fit(xr)[0]:
            swaps += 1
    # frequency per second for each channel (0.15 .. 0.95 s after each beep)
    freqs = {"L": [], "R": []}
    for o in onsets:
        a, b = int((o + 0.1) * SR), int((o + 0.9) * SR)
        if b > n:
            break
        for name, x in (("L", left), ("R", right)):
            subs = [zero_cross_freq(x[j:j + SR // 10]) for j in range(a, b - SR // 10 + 1, SR // 10)]
            subs = [v for v in subs if v]
            freqs[name].append((o, statistics.median(subs) if len(subs) >= 4 else None))
    # correlation (mono / inverted) on a middle second
    mid = n // 2
    seg_l, seg_r = left[mid:mid + SR // 2], right[mid:mid + SR // 2]
    num = sum(map(mul, seg_l, seg_r))
    den = math.sqrt(sum(map(mul, seg_l, seg_l)) * sum(map(mul, seg_r, seg_r))) or 1.0
    corr = num / den
    # polarity: the beep is the same on both channels, so its phase in L and R
    # should match; ~180 degrees apart means one channel is inverted.
    fb = ToneFitter(L.BEEP_HZ, 480)
    phases = []
    for o in onsets:
        a = int((o - 0.005) * SR)
        if a < 0 or a + 480 > n:
            continue
        al, bl = fb.fit_ab(left[a:a + 480])
        ar, br = fb.fit_ab(right[a:a + 480])
        if math.hypot(al, bl) > 0.05 and math.hypot(ar, br) > 0.05:
            d = math.degrees(math.atan2(br, ar) - math.atan2(bl, al))
            phases.append(abs((d + 180) % 360 - 180))
    return {"t0": t0, "n": n, "onsets": onsets, "windows": win, "freqs": freqs,
            "astats": astats, "silences": silences, "corr": corr, "swaps": swaps,
            "loudness": loud, "beep_phase_deg": statistics.median(phases) if phases else None}


def ts_pts_pcr(rec, limit_packets=400000):
    """Median (video PTS - PCR) at the start of each video PES, in ms."""
    pmt_pid, vid_pid, pcr_pid = None, None, None
    last_pcr = None
    leads = []
    with open(rec, "rb") as f:
        data = f.read(188 * limit_packets)
    for off in range(0, len(data) - 187, 188):
        p = data[off:off + 188]
        if p[0] != 0x47:
            continue
        pid = ((p[1] & 0x1F) << 8) | p[2]
        pusi = p[1] & 0x40
        afc = (p[3] >> 4) & 3
        pos = 4
        if afc in (2, 3):
            alen = p[4]
            if alen and pid == pcr_pid and (p[5] & 0x10) and alen >= 7:
                b = p[6:12]
                base = (b[0] << 25) | (b[1] << 17) | (b[2] << 9) | (b[3] << 1) | (b[4] >> 7)
                ext = ((b[4] & 1) << 8) | b[5]
                last_pcr = base * 300 + ext
            pos = 5 + alen
        if afc not in (1, 3) or pos >= 188:
            continue
        pl = p[pos:]
        if pid == 0 and pusi:
            sec = pl[1 + pl[0]:]
            for i in range(8, 8 + ((((sec[1] & 0x0F) << 8) | sec[2]) - 9), 4):
                prog = (sec[i] << 8) | sec[i + 1]
                if prog:
                    pmt_pid = ((sec[i + 2] & 0x1F) << 8) | sec[i + 3]
        elif pid == pmt_pid and pusi and vid_pid is None:
            sec = pl[1 + pl[0]:]
            slen = ((sec[1] & 0x0F) << 8) | sec[2]
            pcr_pid = ((sec[8] & 0x1F) << 8) | sec[9]
            pil = ((sec[10] & 0x0F) << 8) | sec[11]
            i = 12 + pil
            while i < 3 + slen - 4:
                st, epid = sec[i], ((sec[i + 1] & 0x1F) << 8) | sec[i + 2]
                eil = ((sec[i + 3] & 0x0F) << 8) | sec[i + 4]
                if st in (0x1B, 0x24) and vid_pid is None:
                    vid_pid = epid
                i += 5 + eil
        elif pid == vid_pid and pusi and pl[:3] == b"\x00\x00\x01" and last_pcr is not None:
            if pl[7] & 0x80:
                h = pl[9:14]
                pts = (((h[0] >> 1) & 7) << 30) | (h[1] << 22) | ((h[2] >> 1) << 15) | (h[3] << 7) | (h[4] >> 1)
                lead = pts / 90.0 - last_pcr / 27000.0
                if -10000 < lead < 10000:
                    leads.append(lead)
    return statistics.median(leads) if leads else None


# ----------------------------------------------------------------- report ---

def pattern_epoch(frames):
    vals = [f["epoch"] for f in frames if f["epoch"]]
    if not vals:
        return None
    best = max(set(vals), key=vals.count)
    return best if vals.count(best) >= max(5, len(vals) // 4) else None


def arrival_delays(arr_dir, fps):
    arr = Path(arr_dir)
    metas = parse_meta(arr / "meta.txt")
    reads = read_raw_frames(arr / "code.raw", READOUT_W)
    n = min(len(metas), len(reads))
    decoded = [decode_readout(reads[i]) for i in range(n)]
    ep = pattern_epoch(decoded)
    if ep is None:
        return None
    out = []
    seen = set()
    for i in range(n):
        d = decoded[i]
        if d["code"] is None or d["epoch"] != ep or d["code"] in seen:
            continue
        seen.add(d["code"])
        wall = float(metas[i]["pts_time"]) * 1000.0
        made = ep + d["code"] * 1000.0 / fps
        out.append((d["code"] / fps, wall - made))
    return {"epoch": ep, "delays": out}


def srt_receiver_summary(path, skip=0):
    objs = [o for o in read_json_objects(path) if "recv" in o][int(math.ceil(skip)):]
    if not objs:
        return None
    s = lambda k: sum(o["recv"].get(k, 0) for o in objs)
    rtts = [o["link"]["rtt"] for o in objs if o.get("link", {}).get("rtt")]
    return {
        "packets": s("packets"), "lost": s("packetsLost"), "dropped": s("packetsDropped"),
        "belated": s("packetsBelated"), "retransmitted": s("packetsRetransmitted"),
        "rtt_ms": statistics.median(rtts) if rtts else None,
        "tsbpd_ms": objs[-1]["recv"].get("msTsbPdDelay"),
        "buffer_ms": statistics.median([o["recv"].get("msBuf", 0) for o in objs[1:]] or [0]),
        "drop_times": [i for i, o in enumerate(objs) if o["recv"].get("packetsDropped")],
        "last_rx_ms": max((iso_ms(o.get("timepoint")) for o in objs if o["recv"].get("packets")),
                          default=None),
    }


def iso_ms(text):
    from datetime import datetime
    try:
        return datetime.fromisoformat(text).timestamp() * 1000.0
    except (TypeError, ValueError):
        return None


def srtla_summary(path, skip=0):
    """Per-link view from once-a-second snapshots of srtla_send's --stats-file:
    bytes carried, idle stretches while other links carried data, NAKs."""
    snaps = [s for s in read_jsonl(path) if s.get("connections")]
    snaps = snaps[int(math.ceil(skip)):]
    if len(snaps) < 2:
        return None
    ids = sorted({str(c.get("conn_id")) for s in snaps for c in s["connections"]})
    series = {i: [] for i in ids}
    for s in snaps:
        cur = {str(c.get("conn_id")): c for c in s["connections"]}
        for i in ids:
            series[i].append(cur.get(i))
    deltas = {i: [] for i in ids}
    naks = {i: 0 for i in ids}
    for i in ids:
        rows = series[i]
        for prev, now in zip(rows, rows[1:]):
            if prev is None or now is None:
                deltas[i].append(None)
                continue
            deltas[i].append(max(0, now.get("bytes_sent_total", 0) - prev.get("bytes_sent_total", 0)))
            dn = now.get("nak_count", 0) - prev.get("nak_count", 0)
            naks[i] += dn if dn > 0 else 0
    out = []
    for i in ids:
        idle, run, best, best_at = 0, 0, 0, None
        for k, d in enumerate(deltas[i]):
            others = sum((deltas[j][k] or 0) for j in ids if j != i)
            if d == 0 and others > 0:
                idle += 1
                run += 1
                if run > best:
                    best, best_at = run, k + 2 - run
            else:
                run = 0
        sent = sum(d or 0 for d in deltas[i])
        rtts = [r.get("rtt_ms") for r in series[i] if r and r.get("rtt_ms") is not None]
        out.append({"link": i, "bytes": sent, "packets": max(1, sent // 1316), "nak": naks[i],
                    "rtt_ms": statistics.median(rtts) if rtts else None,
                    "idle_s": idle, "idle_longest_s": best, "idle_from_s": best_at,
                    "seconds": len(deltas[i])})
    return out

def runs_of(times, gap):
    """Group sorted times into [start, end] runs separated by more than gap."""
    out = []
    for t in times:
        if out and t - out[-1][1] <= gap:
            out[-1][1] = t
        else:
            out.append([t, t])
    return out


def sender_view(path, epoch, last_code, fps):
    """What the box reported: frames, videorate repeats, time at minimum
    bitrate, and how long it kept sending after the last frame that arrived."""
    snaps = [s for s in read_jsonl(path) if s.get("version") == 1]
    if not snaps:
        return None
    first, last = snaps[0], snaps[-1]
    vr_dup = sum(v.get("duplicate", 0) for v in last.get("videorate", []))
    vr_drop = sum(v.get("drop", 0) for v in last.get("videorate", []))
    vr_in = sum(v.get("in", 0) for v in last.get("videorate", []))
    lat = [s["sender_latency_ms"]["avg"] for s in snaps if s.get("sender_latency_ms")]
    fps_vals = [s["encoder"]["fps"] for s in snaps[2:] if s.get("encoder", {}).get("fps") is not None]
    out = {
        "frames": last.get("encoder", {}).get("frames"),
        "nominal_fps": last.get("encoder", {}).get("nominal_fps"),
        "fps": statistics.median(fps_vals) if fps_vals else None,
        "videorate_duplicate": vr_dup, "videorate_drop": vr_drop, "videorate_in": vr_in,
        "at_min_ms": last.get("bitrate", {}).get("at_min_ms"),
        "input_caps": last.get("input_caps"),
        "sender_latency_ms": statistics.median(lat[2:] or lat) if lat else None,
        "pkt_snd_drop": (last.get("srt") or {}).get("pkt_snd_drop_total"),
        "kept_sending_s": None,
        "last_time_ms": last.get("time_ms"),
    }
    if epoch and last_code is not None and last.get("pattern_epoch_ms") == epoch:
        made_last = epoch + last_code * 1000.0 / fps
        out["kept_sending_s"] = (last["time_ms"] - made_last) / 1000.0
    return out


def report(args):
    rec = Path(args.recording)
    fps = args.fps
    work = Path(tempfile.mkdtemp(prefix="qcheck-"))
    try:
        v = analyse_video(rec, work, fps)
        a_info = audio_packets(rec)
        a = analyse_audio(rec, work, a_info) if a_info["sample_rate"] else None
        kts, per_sec = video_packets(rec)
        lead = ts_pts_pcr(rec)
    finally:
        if not args.keep:
            shutil.rmtree(work, ignore_errors=True)
        else:
            print(f"intermediate files kept in {work}", file=sys.stderr)

    all_frames = v["frames"]
    t_first = all_frames[0]["t"] if all_frames else (a["t0"] if a else 0.0)
    t_last = all_frames[-1]["t"] if all_frames else t_first
    cut, end = t_first + args.warmup, t_last - args.tail
    frames = [f for f in all_frames if cut <= f["t"] <= end]
    rel = lambda t: t - t_first
    kgaps = [b - a_ for a_, b in zip(kts, kts[1:]) if a_ >= cut]
    if a is not None:
        r0, r1 = cut - a["t0"], end - a["t0"]
        a["windows"] = [w for w in a["windows"] if r0 <= w[0] <= r1]
        a["onsets"] = [o for o in a["onsets"] if r0 <= o <= r1]
        a["freqs"] = {k: [(o, f) for o, f in fl if r0 <= o <= r1] for k, fl in a["freqs"].items()}
        a["silences"] = [(c, max(s0, cut), min(s0 + d, end) - max(s0, cut)) for c, s0, d in a["silences"]
                         if s0 + d > cut and s0 < end]

    problems, fine, facts, lines = [], [], {}, []
    epoch = pattern_epoch(all_frames)
    facts["pattern_epoch_ms"] = epoch

    # ---------------------------------------------------------- picture --
    if not all_frames:
        msg = "No picture could be decoded"
        if v["no_params"]:
            msg += (": the decoder never got the stream description (SPS/PPS). The sender sends it only "
                    "once at the start, so a receiver that joins later can't start the picture")
        problems.append(msg + ".")
        facts.update(sent=0, arrived=0)
        lines.append("No picture decoded.")
    else:
        acc = frame_accounting(frames, fps)
        still, still_at = longest_still(frames, fps)
        dmg = damage_flags(frames)
        damaged = sorted(set(i for i, d in enumerate(dmg) if d > 12)
                         | set(acc["garbled_idx"]) | {i for i, f in enumerate(frames) if f["code"] is None})
        unreadable = sum(1 for f in frames if f["code"] is None)
        green = [f["t"] for f in frames if is_green(f["y"], f["u"], f["v"])]
        green_part = [f["t"] for f in frames
                      if not is_green(f["y"], f["u"], f["v"]) and is_green(f["yb"], f["ub"], f["vb"])]
        green_t = set(green)
        black = [f["t"] for f in frames if f["black_start"] and f["t"] not in green_t]
        facts.update(sent=acc["sent"], arrived=acc["arrived"], repeated=acc["repeated"],
                     missing=acc["missing"], restarts=acc["restarts"], unreadable=unreadable,
                     damaged=len(damaged), green=len(green), green_part=len(green_part),
                     black=len(black), longest_still_s=round(still, 3), decode_errors=v["decode_errors"])
        head = (f"{fmt_n(acc['sent'])} frames sent, {fmt_n(acc['arrived'])} arrived, "
                f"{fmt_n(acc['repeated'])} repeated ({pct(acc['repeated'], max(1, len(frames))):.1f}%)")
        if acc["missing"]:
            head += f", {fmt_n(acc['missing'])} missing ({pct(acc['missing'], acc['sent']):.1f}%)"
        lines.append(head + ".")
        if acc["missing"]:
            where = ", ".join(f"{n} at {fmt_t(rel(t))}" for t, n in acc["gaps"][:4])
            more = f" and {len(acc['gaps']) - 4} more places" if len(acc["gaps"]) > 4 else ""
            problems.append(f"Frames lost: {fmt_n(acc['missing'])} never arrived ({where}{more}). "
                            "Viewers see jumps in movement.")
        if acc["repeated"]:
            every = len(frames) / acc["repeated"]
            problems.append(f"Repeated frames: {fmt_n(acc['repeated'])} frames showed the same picture as the one "
                            f"before (about one in {every:.0f}). Movement stutters: something delivered fewer "
                            "frames than the stream rate and the gaps were filled with copies.")
        if acc["restarts"]:
            problems.append("The stream started over at " + ", ".join(fmt_t(rel(t)) for t in acc["restart_times"])
                            + " (frame numbers began again), so the sender restarted. Players that keep the "
                            "old timestamps can show a frozen picture after this.")
        if still >= 0.5:
            problems.append(f"The picture stood still for {still:.1f} s from {fmt_t(rel(still_at))}.")
        if green:
            problems.append(f"Green frames: {len(green)} frames flat green (first at {fmt_t(rel(green[0]))}).")
        elif green_part:
            problems.append(f"Green frames: the lower half was flat green in {len(green_part)} frames "
                            f"(first at {fmt_t(rel(green_part[0]))}).")
        else:
            fine.append("no green frames")
        if black:
            problems.append(f"Black picture: {len(black)} times, first at {fmt_t(rel(black[0]))}.")
        if damaged:
            rr = runs_of(damaged, 2)
            longest = max(frames[e]["t"] - frames[s]["t"] + 1 / fps for s, e in rr)
            facts["broken_longest_s"] = round(longest, 2)
            problems.append(f"Broken picture: {len(damaged)} frames with grey or smeared blocks or a damaged "
                            f"frame number, in {len(rr)} "
                            f"places (first at {fmt_t(rel(frames[rr[0][0]]['t']))}, longest {longest:.1f} s). "
                            "Usually lost data; it clears at the next keyframe.")
        else:
            fine.append("no broken pictures")
        facts["garbled"] = acc["garbled"]
        # passive estimates that also work without the pattern
        if v["mpdecimate_kept"] is not None and all_frames:
            facts["mpdecimate_dropped"] = len(all_frames) - v["mpdecimate_kept"]
        bl = [b for t, b in v["blocks"] if cut <= t <= end]
        blk = statistics.median(bl) if bl else None
        if blk is not None:
            facts["blockiness"] = round(blk, 2)
        vsecs = [per_sec[k] for k in per_sec if cut <= k <= end]
        bpp = (statistics.median(vsecs) * 8 / (v["width"] * v["height"] * fps)) if vsecs and v["width"] else None
        if bpp is not None:
            facts["bits_per_pixel"] = round(bpp, 4)
        if (blk is not None and blk > args.block_limit) or (bpp is not None and bpp < args.min_bpp):
            problems.append("Blocky picture: not enough bitrate for the picture ("
                            + (f"blockdetect {blk:.1f}, the clean pattern stays near 1; " if blk is not None else "")
                            + (f"{statistics.median(vsecs) * 8 / 1000:,.0f} kbit/s for {v['width']}x{v['height']} "
                               f"at {fps:g} fps is {bpp:.3f} bits per pixel" if bpp is not None else "")
                            + ").")
        if v["idet"]:
            facts["idet"] = v["idet"]  # passive estimate; the pattern's sharp lines confuse it
        facts["field_mismatch"] = v["field_mismatch"]
        if v["field_frames"] and v["field_mismatch"] > 0.2 * v["field_frames"]:
            problems.append("Interlaced picture: the two fields of a frame show different moments "
                            f"({pct(v['field_mismatch'], v['field_frames']):.0f}% of frames), seen as comb-like "
                            "edges on movement. The source probably sends 1080i.")
        else:
            fine.append("progressive picture")
        # colour range from the black and white blocks of the frame strip
        good = [f for f in frames if f["code"] is not None] or frames
        bk = statistics.median(f["black"] for f in good)
        wt = statistics.median(f["white"] for f in good)
        facts.update(black_level=round(bk), white_level=round(wt), color_range=v["color_range"])
        if v["color_range"] == "pc" and bk > 10:
            problems.append(f"Colour range mismatch: the stream says full range, but black sits at {bk:.0f} and "
                            f"white at {wt:.0f} (limited range). Blacks look grey and washed out.")
        elif v["color_range"] in (None, "tv", "unknown") and (bk < 8 or wt > 245):
            problems.append(f"Colour range mismatch: limited range declared, but black at {bk:.0f} and white at "
                            f"{wt:.0f}. Shadows and highlights get clipped.")
        else:
            fine.append("colour range")
        vt = [f["t"] for f in frames]
        gaps = [b - a_ for a_, b in zip(vt, vt[1:])]
        big = [g for g in gaps if g > 1.5 / fps]
        back = [g for g in gaps if g < 0]
        if big:
            problems.append(f"Picture timestamps skip {len(big)} times (largest {max(big) * 1000:.0f} ms).")
        if back:
            problems.append(f"Picture timestamps jump backwards {len(back)} times.")
        if not big and not back:
            fine.append("picture timestamps even")

    # -------------------------------------------------------- keyframes --
    if kgaps:
        ki = statistics.median(kgaps)
        facts.update(keyframe_interval_s=round(ki, 2), keyframe_gap_max_s=round(max(kgaps), 2),
                     keyframe_gap_min_s=round(min(kgaps), 2))
        kf = f"Keyframes every {ki:.1f} s."
        if ki > 4.0:
            problems.append(f"Keyframes only every {ki:.1f} s: after any loss the picture stays broken or frozen "
                            "for up to that long, and new viewers wait that long for a picture. "
                            "Platforms want 2 s (never over 4 s).")
        elif max(kgaps) > 4.0:
            problems.append(f"Keyframes mostly every {ki:.1f} s, but one gap was {max(kgaps):.1f} s.")
        elif min(kgaps) < 0.6 * ki:
            problems.append(f"Extra keyframes: gaps between {min(kgaps):.1f} and {max(kgaps):.1f} s "
                            "(scene-cut keyframes). Harmless for viewers, but platforms that want a fixed "
                            "interval may warn.")
    else:
        kf = "No keyframe interval measured."
    # bitrate peaks
    secs = sorted(k for k in per_sec if cut <= k <= end)
    if len(secs) >= 4:
        kb = [per_sec[k] * 8 / 1000 for k in secs]
        med_kb, peak = statistics.median(kb), max(kb)
        facts.update(video_kbps_median=round(med_kb), video_kbps_peak=round(peak))
        if peak > args.peak_ratio * med_kb:
            problems.append(f"Bitrate peaks: up to {peak:,.0f} kbit/s in one second against a typical "
                            f"{med_kb:,.0f} kbit/s ({peak / med_kb:.1f}x). Peaks like this overflow cellular "
                            "links; the encoder needs a VBV limit.")
        else:
            fine.append(f"bitrate steady (peak {peak / max(med_kb, 1):.1f}x typical)")

    # ------------------------------------------------------------ sound --
    sound_bits = []
    if a is None:
        problems.append("No sound track.")
    else:
        exp = {"L": L.LEFT_HZ, "R": L.RIGHT_HZ}
        chname = {"L": "left", "R": "right"}
        win = a["windows"]
        nwin = max(1, len(win))
        silent = {"L": [w[0] for w in win if w[1] < L.TONE_AMP * 0.05],
                  "R": [w[0] for w in win if w[3] < L.TONE_AMP * 0.05]}
        mute = [ch for ch in ("L", "R") if len(silent[ch]) > 0.8 * nwin]
        noisy = [w[0] for w in win if (w[1] >= L.TONE_AMP * 0.05 and w[2] > L.TONE_AMP * args.residual_limit)
                 or (w[3] >= L.TONE_AMP * 0.05 and w[4] > L.TONE_AMP * args.residual_limit)]
        fmed = {}
        for ch in ("L", "R"):
            vals = [f for _, f in a["freqs"][ch] if f]
            fmed[ch] = round(statistics.median(vals), 2) if vals else None
        facts.update(tone_hz=fmed, sample_rate=a_info["sample_rate"], channels=a_info["channels"],
                     loudness=a["loudness"], beep_phase_deg=a["beep_phase_deg"])
        if a["swaps"] >= 3:
            problems.append("Sound channels swapped: the left tone comes out on the right and the other way round.")
        elif len(mute) == 2:
            problems.append("No sound: both channels silent.")
        elif mute:
            other = "right" if mute[0] == "L" else "left"
            problems.append(f"Only one channel: the {chname[mute[0]]} channel is silent, the {other} one is fine. "
                            "On a phone speaker or a mono mix the sound gets quieter or thin.")
        else:
            sound_bits.append("both channels")
            if a["beep_phase_deg"] is not None and a["beep_phase_deg"] > 150:
                problems.append("One channel is inverted (polarity): in a mono mix, such as a phone speaker, "
                                "the sound cancels out and gets thin or vanishes.")
            elif a["beep_phase_deg"] is not None:
                fine.append("channel polarity")
        if a_info["sample_rate"] != 48000:
            problems.append(f"Sound declared at {a_info['sample_rate']:,} Hz instead of 48,000 Hz.")
        for ch in ("L", "R"):
            if ch in mute:
                continue
            off = [(o, f) for o, f in a["freqs"][ch] if f and abs(f / exp[ch] - 1) > 0.01]
            if not off:
                continue
            r = statistics.median(f / exp[ch] for _, f in off)
            span = (rel(a["t0"] + off[0][0]), rel(a["t0"] + off[-1][0]))
            if len(off) > 0.8 * max(1, len(a["freqs"][ch])):
                problems.append(f"Sound pitch is {abs(r - 1) * 100:.1f}% {'high' if r > 1 else 'low'} "
                                f"({exp[ch] * r:.0f} Hz instead of {exp[ch]} Hz on the {chname[ch]} channel): a "
                                "sample-rate mismatch. Voices sound off and drift away from the picture.")
            else:
                problems.append(f"Sound sped up or slowed down for a while ({abs(r - 1) * 100:.1f}% "
                                f"{'faster' if r > 1 else 'slower'}, {fmt_t(span[0])} to {fmt_t(span[1])}): "
                                "heard as warbling or robotic sound, typical of a receiver catching up.")
            break
        tsg = [(t, d) for t, d in a_info["ts_gaps"] if cut <= t <= end]
        if tsg:
            facts.update(sound_ts_gaps=len(tsg), sound_ts_gap_ms_max=round(max(d for _, d in tsg) * 1000))
            problems.append(f"Sound data missing: the sound timestamps have {len(tsg)} gaps of up to "
                            f"{max(d for _, d in tsg) * 1000:.0f} ms (first at {fmt_t(rel(tsg[0][0]))}). "
                            "Heard as dropouts or clicks.")
        gaps = [(c, s0, d) for c, s0, d in a["silences"] if d >= 0.015 and d < 0.8 * (end - cut)]
        if gaps:
            starts = sorted({round(s0, 2) for _, s0, _ in gaps})
            ev = runs_of(starts, 0.05)
            durs = [d for _, _, d in gaps]
            facts.update(sound_gaps=len(ev), sound_gap_ms_max=round(max(durs) * 1000))
            problems.append(f"Sound gaps: {len(ev)} dropouts of {min(durs) * 1000:.0f}–{max(durs) * 1000:.0f} ms "
                            f"(first at {fmt_t(rel(starts[0]))}).")
            sound_bits.append(f"{len(ev)} gaps")
        elif not mute:
            sound_bits.insert(0, "no gaps")
        if len(noisy) > 2:
            rr = runs_of(noisy, 0.03)
            longest = max(e - s0 + 0.01 for s0, e in rr)
            kind = "warbling or robotic sound" if longest >= 0.3 else "clicks or crackle"
            facts["distorted_bursts"] = len(rr)
            problems.append(f"Distorted sound: {len(rr)} bursts of {kind} (first at "
                            f"{fmt_t(rel(rr[0][0] + a['t0']))}, longest {longest * 1000:.0f} ms).")
        elif not mute:
            fine.append("clean sound (no clicks or distortion)")
        peaks = []
        for st in a["astats"].values():
            try:
                peaks.append(float(st.get("Peak level dB", "nan")))
            except ValueError:
                pass
        flat = []
        for st in a["astats"].values():
            try:
                flat.append(float(st.get("Peak count", "0")))
            except ValueError:
                pass
        tp = a["loudness"].get("true_peak_dbfs")
        if (peaks and max(peaks) > -1.0) or (tp is not None and tp > -1.0):
            problems.append(f"Sound clips: peaks reach {max(peaks + ([tp] if tp is not None else [])):.1f} dBFS. "
                            "The pattern peaks near -6 dBFS, so something adds about "
                            f"{max(peaks) + 6:.0f} dB of gain; loud moments will distort.")
        elif "integrated_lufs" in a["loudness"]:
            fine.append(f"levels (loudness {a['loudness']['integrated_lufs']:.0f} LUFS, no clipping)")
        # A/V offset and drift from the second marker and the beep
        # For each whole second k, the frame numbered round(k * fps); its
        # picture time is corrected by the sub-frame difference (k - c/fps),
        # which matters for fractional rates such as 29.97.
        marks = {}
        for f in frames:
            c = f["code"]
            if c is None:
                continue
            k = round(c / fps)
            if c == round(k * fps) and k not in marks:
                marks[k] = f["t"] + (k - c / fps)
        offs = []
        for k, tv in sorted(marks.items()):
            near = [o + a["t0"] for o in a["onsets"] if abs(o + a["t0"] - tv) < 0.5]
            if near:
                ta = min(near, key=lambda x: abs(x - tv))
                offs.append((tv, (ta - tv) * 1000.0 - args.beep_bias_ms))
        if len(offs) >= 3:
            vals = [o for _, o in offs]
            med = statistics.median(vals)
            xs = [t for t, _ in offs]
            mx, my = statistics.mean(xs), statistics.mean(vals)
            sxx = sum((x - mx) ** 2 for x in xs)
            sxy = sum((x - mx) * (o - my) for x, o in offs)
            syy = sum((o - my) ** 2 for o in vals)
            slope = sxy / sxx * 60 if sxx else 0.0
            r2 = (sxy * sxy) / (sxx * syy) if sxx and syy else 0.0
            spread = max(vals) - min(vals)
            facts.update(av_offset_ms=round(med, 1), av_drift_ms_per_min=round(slope, 1), av_marks=len(offs),
                         av_spread_ms=round(spread), av_drift_r2=round(r2, 2))
            side = "ahead of" if med < 0 else "behind"
            if spread >= 40 and r2 < 0.8:
                problems.append(f"Sound sync is unstable: the offset jumps between {min(vals):+.0f} and "
                                f"{max(vals):+.0f} ms (+ means sound later). Usually sound data lost and the "
                                "gap closed up, then corrected in a jump.")
                sound_bits.append(f"sync jumping between {min(vals):+.0f} and {max(vals):+.0f} ms")
            elif abs(slope) > args.drift_limit and len(offs) >= 10:
                problems.append(f"Sound drifts away from the picture: {offs[0][1]:+.0f} ms at the start, "
                                f"{offs[-1][1]:+.0f} ms at the end ({slope:+.0f} ms per minute; + means sound "
                                "later). Lips and voice slowly come apart: two clocks that don't agree.")
                sound_bits.append(f"drifting {slope:+.0f} ms per minute")
            else:
                if med < -45 or med > 125:
                    problems.append(f"Sound {abs(med):.0f} ms {side} the picture. Viewers notice from about "
                                    "45 ms early or 125 ms late.")
                sound_bits.append(f"{abs(med):.0f} ms {side} the picture, "
                                  + ("steady" if spread < 40 else f"varying by {spread:.0f} ms"))
        else:
            sound_bits.append("A/V offset not measurable")

    # ------------------------------------------------------------ delay --
    delay_lines = []
    srt = srt_receiver_summary(args.srt_stats, args.warmup) if getattr(args, "srt_stats", None) else None
    sv = None
    if getattr(args, "sender_stats", None):
        last_code = max((f["code"] for f in all_frames if f["code"] is not None and f["epoch"] == epoch),
                        default=None)
        sv = sender_view(args.sender_stats, epoch, last_code, fps)
    if getattr(args, "arrivals", None):
        ad = arrival_delays(args.arrivals, fps)
        if ad and ad["delays"]:
            t_ad0 = ad["delays"][0][0]
            dl = [(t, d) for t, d in ad["delays"] if t >= t_ad0 + args.warmup] or ad["delays"]
            ds = sorted(d for _, d in dl)
            med = statistics.median(ds)
            p95 = ds[int(0.95 * (len(ds) - 1))]
            n5 = int(fps * 5)
            early = statistics.median([d for _, d in dl[:n5]])
            late = statistics.median([d for _, d in dl[-n5:]])
            facts.update(delay_ms=round(med), delay_p95_ms=round(p95))
            delay_lines.append(f"End-to-end delay: {med / 1000:.2f} s from the pattern clock to the decoded "
                               f"picture (95% of frames within {p95 / 1000:.2f} s)."
                               + (f" It grew by {late - early:.0f} ms during the run." if late - early > 150 else ""))
            parts = []
            if sv and sv["sender_latency_ms"] is not None:
                parts.append(("encoder and sender pipeline", sv["sender_latency_ms"]))
            if srt and srt["tsbpd_ms"] is not None:
                parts.append(("network one way (half the SRT round trip)", (srt["rtt_ms"] or 0) / 2))
                parts.append((f"SRT latency buffer (TSBPD {srt['tsbpd_ms']} ms)", srt["tsbpd_ms"]))
            if parts:
                parts.append(("receiver demux and decode (the rest)", med - sum(p for _, p in parts)))
                delay_lines.append("Where it goes: " + "; ".join(f"{n} {p:.0f} ms" for n, p in parts) + ".")
                facts["delay_parts_ms"] = {n: round(p) for n, p in parts}
        else:
            delay_lines.append("End-to-end delay: not measurable (pattern clock unreadable).")
    if lead is not None:
        facts["ts_pts_minus_pcr_ms"] = round(lead)
        delay_lines.append(f"Stream clock: pictures are sent {lead:.0f} ms ahead of their presentation time "
                           "(PTS - PCR); a player that follows the stream clock buffers about that much.")

    # -------------------------------------------------------- transport --
    if srt:
        facts["srt"] = {k: srt[k] for k in ("packets", "lost", "dropped", "belated", "retransmitted",
                                            "rtt_ms", "tsbpd_ms")}
        if srt["dropped"]:
            problems.append(f"SRT gave up on {fmt_n(srt['dropped'])} packets at the receiver (pktRcvDrop): they "
                            f"weren't repaired within the {srt['tsbpd_ms']} ms latency (round trip "
                            f"{srt['rtt_ms']:.0f} ms, {fmt_n(srt['lost'])} lost on the way). Raise the latency "
                            "to at least 4x the round trip, or fix the link.")
        elif srt["lost"]:
            fine.append(f"SRT repaired all {fmt_n(srt['lost'])} lost packets in time"
                        + (f" ({fmt_n(srt['belated'])} late repairs discarded)" if srt["belated"] else ""))
        else:
            fine.append("no SRT packet loss")
    if sv:
        facts["sender"] = sv
        if sv["videorate_duplicate"]:
            problems.append(f"The box filled {fmt_n(sv['videorate_duplicate'])} missing camera frames with copies "
                            f"(videorate, {sv['videorate_in']:,} frames in): the camera delivers fewer frames "
                            "than the pipeline's rate.")
        if sv["videorate_drop"]:
            problems.append(f"The box dropped {fmt_n(sv['videorate_drop'])} extra camera frames (videorate): "
                            "the camera sends faster than the pipeline's rate.")
        if sv["at_min_ms"]:
            problems.append(f"The box spent {sv['at_min_ms'] / 1000:.0f} s at its minimum bitrate.")
        if srt and srt.get("last_rx_ms") and sv.get("last_time_ms"):
            sv["kept_sending_s"] = (sv["last_time_ms"] - srt["last_rx_ms"]) / 1000.0
        if sv["kept_sending_s"] is not None and sv["kept_sending_s"] > 5:
            problems.append(f"The feed stopped arriving {sv['kept_sending_s']:.0f} s before the box stopped "
                            "sending: the receiver lost the stream and didn't pick it up again (stuck reader).")
    links = srtla_summary(args.srtla_stats, args.warmup) if getattr(args, "srtla_stats", None) else None
    if links:
        total = sum(l["bytes"] for l in links) or 1
        facts["srtla_links"] = links
        for l in links:
            share = pct(l["bytes"], total)
            if l["idle_longest_s"] >= 5:
                problems.append(f"Link {l['link']} is connected but carried nothing for {l['idle_longest_s']} s "
                                f"(from about {l['idle_from_s'] + args.warmup:.0f} s; {share:.0f}% of the data "
                                "over the whole run) while the other links did: it shows as idle. Check that "
                                "it can reach the receiver.")
            if l["nak"] / l["packets"] > 0.03:
                problems.append(f"Link {l['link']} lost about {pct(l['nak'], l['packets']):.0f}% of its packets "
                                f"(receiver NAKs) while carrying {share:.0f}% of the data; they had to be sent "
                                "again over the bond.")
        if all(l["idle_longest_s"] < 5 for l in links):
            fine.append(f"all {len(links)} links carried data")

    out = lines[:]
    green_ok = facts.get("green", 0) == 0 and facts.get("green_part", 0) == 0 and all_frames
    out.append(" ".join(x for x in ["No green frames." if green_ok else "",
                                    ("Sound: " + ", ".join(sound_bits) + ".") if sound_bits else "",
                                    kf] if x))
    out += delay_lines
    if problems:
        out += ["", "Problems found:"] + [f"- {p}" for p in problems]
    if fine:
        out += ["", "Checked and fine: " + ", ".join(fine) + "."]
    out.append(f"Not judged: the first {args.warmup:g} s and the last {args.tail:g} s.")
    text = "\n".join(out)
    print(text)
    if args.json:
        Path(args.json).write_text(json.dumps({"facts": facts, "problems": problems, "fine": fine,
                                               "report": text}, indent=2, default=str))
    return 0


def parse_fps(text):
    if "/" in text:
        n, d = text.split("/", 1)
        return float(n) / float(d)
    return float(text)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--fps", type=parse_fps, default=float(L.FPS),
                       help="frame rate of the pattern, e.g. 30, 25 or 30000/1001")
        p.add_argument("--sender-stats", help="JSONL of ceracoder stats-file snapshots")
        p.add_argument("--srtla-stats", help="JSONL of srtla_send stats-file snapshots")
        p.add_argument("--json", help="also write the findings as JSON here")
        p.add_argument("--keep", action="store_true", help="keep intermediate files")
        p.add_argument("--warmup", type=float, default=3.0,
                       help="seconds at the start that are not judged (default 3)")
        p.add_argument("--tail", type=float, default=1.0,
                       help="seconds at the end that are not judged (default 1)")
        p.add_argument("--block-limit", type=float, default=1.8,
                       help="blockdetect score on the noise patch that counts as blocky (clean runs: about 1.0-1.4)")
        p.add_argument("--min-bpp", type=float, default=0.02,
                       help="video bits per pixel below which the picture counts as starved")
        p.add_argument("--peak-ratio", type=float, default=2.5, help="per-second bitrate peak vs median")
        p.add_argument("--drift-limit", type=float, default=15.0, help="A/V drift in ms per minute")
        p.add_argument("--residual-limit", type=float, default=0.03,
                       help="tone residual (fraction of the tone) that counts as distortion")
        p.add_argument("--beep-bias-ms", type=float, default=0.0,
                       help="subtract this from measured A/V offsets (detector calibration)")

    c = sub.add_parser("capture", help="receive over SRT, record and report")
    c.add_argument("--srt", required=True, help="SRT URI for srt-live-transmit, e.g. 'srt://:4000?mode=listener'")
    c.add_argument("--seconds", type=float, default=60)
    c.add_argument("--wait", type=float, default=30, help="how long to wait for the first data")
    c.add_argument("--out", required=True)
    c.add_argument("--width", type=int, default=1920)
    c.add_argument("--height", type=int, default=1080)
    c.add_argument("--no-reconnect", action="store_true",
                   help="stop receiving when the sender disconnects (emulates a reader that gets stuck)")
    c.add_argument("--no-report", action="store_true", help="only record; run 'report' later")
    common(c)
    r = sub.add_parser("report", help="report on a recording")
    r.add_argument("recording")
    r.add_argument("--arrivals", help="arrivals directory written by capture")
    r.add_argument("--srt-stats", help="srt-live-transmit -statsout JSON of the receiver")
    common(r)
    args = ap.parse_args()
    if args.cmd == "capture":
        return capture(args)
    args.arrivals = getattr(args, "arrivals", None)
    return report(args)


if __name__ == "__main__":
    sys.exit(main())
