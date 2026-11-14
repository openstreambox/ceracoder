#!/usr/bin/env python3
"""Synthetic fault runs for the quality check, all on one machine.

Each case runs the real ceracoder with the quality pattern (x264 variant)
inside its own unprivileged network namespace (unshare -rn), sends it over
SRT on the loopback interface, optionally through netem, an SRT relay or an
SRTLA bond, records it with `analyse.py capture`, and then checks that the
report names the fault. A clean run is the control.

Usage:
  run_faults.py --out DIR [--only NAME,NAME] [--ceracoder PATH] [--srtla-rec PATH]

Needs: ceracoder (built), srt-live-transmit, ffmpeg, tc/ip (iproute2),
unshare (util-linux), and for the SRTLA cases srtla_send and srtla_rec.
"""
import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
QC = HERE.parent
REPO = QC.parents[1]
sys.path.insert(0, str(QC))
import gen_pipelines as G  # noqa: E402

ANALYSE = QC / "analyse.py"
X264 = "x264enc speed-preset=superfast key-int-max=60 bitrate=4000 threads=1 name=venc_kbps"
AAC = "avenc_aac bitrate=131072"


def base_pipeline(**kw):
    opts = dict(fmt="I420", venc=X264, parse="h264parse", aenc=AAC)
    opts.update(kw)
    return G.render(**opts)


def after_compositor(text, insert):
    """Insert elements right after the compositor's output caps."""
    key = "format=I420 !\n"
    assert key in text
    return text.replace(key, key + insert + "\n", 1)


def after_mixer(text, insert):
    key = "identity name=a_delay signal-handoffs=TRUE ! audioconvert !"
    assert key in text
    return text.replace(key, "identity name=a_delay signal-handoffs=TRUE ! audioconvert ! " + insert, 1)


def extra_layer(text, src, pad_props):
    """Add one more compositor input (for injected frames)."""
    n = text.count("! comp.sink_")
    text = text.replace("compositor name=comp background=black ",
                        f"compositor name=comp background=black sink_{n}::{pad_props.replace(' ', f' sink_{n}::')} ", 1)
    return f"{src} ! comp.sink_{n}\n" + text


def audio_rate_relabel(text, real_rate):
    """Make the sound at real_rate but label it 48 kHz after the mixer, and let
    the encoder time it by sample count: an audio clock that runs fast or slow
    against the picture (or a wrong sample rate that nothing resamples)."""
    text = text.replace("rate=48000", f"rate={real_rate}")
    key = "audiomixer name=amix ! audio/x-raw,channels=2,rate=" + str(real_rate) + " ! "
    assert key in text
    text = text.replace(key, key + "capssetter caps=\"audio/x-raw,rate=(int)48000\" ! ", 1)
    return text.replace(AAC, AAC + " perfect-timestamp=true")


MIX = lambda m: f"audioconvert mix-matrix=\"{m}\" ! audio/x-raw,channels=2,channel-mask=(bitmask)0x3 ! audioconvert !"


def has(sub):
    return lambda r: any(sub.lower() in p.lower() for p in r["problems"])


def fact(key, pred):
    return lambda r: key in r["facts"] and r["facts"][key] is not None and pred(r["facts"][key])


def all_of(*fs):
    return lambda r: all(f(r) for f in fs)


def any_of(*fs):
    return lambda r: any(f(r) for f in fs)


def none_of_problems(r):
    return not r["problems"]


# ---------------------------------------------------------------- cases ---
# Each case: id (taxonomy row), cause, viewer (what viewers see), expect
# (check on the report), and the run settings.

CASES = [
    dict(name="control", ids="—", cause="Clean run, nothing injected",
         viewer="A clean picture and sound", expect=none_of_problems),
    dict(name="repeat_25in30", ids="E8+C16",
         cause="Camera at 25 fps into a 30 fps pipeline (videorate fills the gaps)",
         viewer="Stutter: one frame in six is a copy",
         pipeline=dict(fps="25/1", out_fps="30/1"), fps=25,
         expect=all_of(has("Repeated frames"), has("filled"))),
    dict(name="repeat_20in30", ids="E8+C16",
         cause="Camera at 20 fps into a 30 fps pipeline",
         viewer="Stutter: every third frame is a copy",
         pipeline=dict(fps="20/1", out_fps="30/1"), fps=20,
         expect=all_of(has("Repeated frames"), has("filled"))),
    dict(name="repeat_2997in30", ids="E8+C16",
         cause="Camera at 29.97 fps into a 30 fps pipeline (one copy every ~33 s)",
         viewer="A hitch roughly every 33 s",
         pipeline=dict(fps="30000/1001", out_fps="30/1"), fps="30000/1001", seconds=50,
         expect=any_of(has("Repeated frames"), has("filled"))),
    dict(name="loss_random_lat100", ids="T3",
         cause="4% random loss, 60 ms round trip, SRT latency 100 ms (too short; ceracoder's minimum)",
         viewer="Grey or smeared blocks, frozen moments, crackle",
         netem="delay 30ms loss 4%", latency=100,
         expect=all_of(has("SRT gave up"), any_of(has("Broken picture"), has("Frames lost")))),
    dict(name="loss_random_lat300", ids="T3",
         cause="Same 4% loss and 60 ms round trip, SRT latency 300 ms",
         viewer="Mostly clean; fewer breakups than at 100 ms",
         netem="delay 30ms loss 4%", latency=300,
         expect=lambda r: True),  # comparison row: report the numbers
    dict(name="loss_burst_lat120", ids="T4",
         cause="Bursty loss (Gilbert-Elliott: 1% into bad, 20% back), 40 ms round trip, latency 120 ms",
         viewer="Short breakups in bursts",
         netem="delay 20ms loss gemodel 1% 20%", latency=120,
         expect=has("SRT gave up")),
    dict(name="long_gop_loss", ids="E1",
         cause="Keyframe every 10 s, plus 2% loss at too-short latency",
         viewer="After each loss the picture stays broken for seconds",
         pipeline=dict(venc=X264.replace("key-int-max=60", "key-int-max=300")),
         netem="delay 30ms loss 2%", latency=100,
         expect=all_of(has("Keyframes only every"), fact("broken_longest_s", lambda x: x > 2))),
    dict(name="vbr_no_vbv", ids="E5",
         cause="Constant-quality encoding with no bitrate cap (x264 pass=quant)",
         viewer="Fine at home; overflowing cellular links at keyframes",
         pipeline=dict(venc=X264.replace("bitrate=4000", "pass=quant quantizer=18")),
         expect=has("Bitrate peaks")),
    dict(name="av_offset_100", ids="C25",
         cause="Sound delayed by 100 ms at the sender (ceracoder -d 100)",
         viewer="Voice after the lips",
         ceracoder_args=["-d", "100"],
         expect=fact("av_offset_ms", lambda x: 130 < x < 170)),
    dict(name="av_drift", ids="C24",
         cause="Audio clock 0.2% slow against the video clock (48,096 samples per second labelled 48,000)",
         viewer="Lips and voice slowly come apart (about 120 ms per minute)",
         pipeline_edit=lambda t: audio_rate_relabel(t, 48096), seconds=40,
         expect=has("drifts away")),
    dict(name="dropouts_20ms", ids="C22",
         cause="Sound buffers lost before the encoder (20 ms each, 2% of them)",
         viewer="Clicks; the encoder closes each gap, so the sound also moves 20 ms earlier per drop",
         pipeline_edit=lambda t: after_mixer(t, "audiobuffersplit output-buffer-duration=1/50 ! "
                                                "identity drop-probability=0.02 ! audioconvert !"),
         expect=any_of(has("Sound gaps"), has("clicks"))),
    dict(name="dropouts_300ms", ids="C22",
         cause="Sound buffers lost before the encoder (300 ms each, 8% of them)",
         viewer="Sound cuts out for a third of a second",
         pipeline_edit=lambda t: after_mixer(t, "audiobuffersplit output-buffer-duration=3/10 ! "
                                                "identity drop-probability=0.08 ! audioconvert !"),
         expect=has("Sound gaps")),
    dict(name="clipping", ids="E17+Q1",
         cause="Sound gain +18 dB (volume 8x) before the encoder",
         viewer="Harsh distortion on loud moments",
         pipeline_edit=lambda t: after_mixer(t, "volume volume=8.0 ! audioconvert !"),
         expect=has("Sound clips")),
    dict(name="right_silent", ids="C8",
         cause="Right channel silent (mono mic on one side)",
         viewer="Sound in one ear only; thin on a phone",
         pipeline_edit=lambda t: after_mixer(t, MIX("<<(float)1.0, (float)0.0>, <(float)0.0, (float)0.0>>")),
         expect=has("Only one channel")),
    dict(name="right_inverted", ids="C8",
         cause="Right channel polarity inverted",
         viewer="Sound cancels in a mono mix",
         pipeline_edit=lambda t: after_mixer(t, MIX("<<(float)1.0, (float)0.0>, <(float)0.0, (float)-1.0>>")),
         expect=has("inverted")),
    dict(name="swapped", ids="C8",
         cause="Left and right swapped",
         viewer="Sound on the wrong side",
         pipeline_edit=lambda t: after_mixer(t, MIX("<<(float)0.0, (float)1.0>, <(float)1.0, (float)0.0>>")),
         expect=has("swapped")),
    dict(name="wrong_rate", ids="C7",
         cause="44.1 kHz sound labelled 48 kHz without resampling",
         viewer="Voices 9% too high, sound runs away from the picture",
         pipeline_edit=lambda t: audio_rate_relabel(t, 44100),
         expect=has("pitch is")),
    dict(name="outage_reconnect", ids="T7",
         cause="7 s outage (100% loss); sender and receiver reconnect",
         viewer="Frozen picture, then a jump; broken until the next keyframe",
         reconnect=True, seconds=40, actions=[(14, "loss 100%"), (21, None)],
         expect=any_of(has("stood still"), has("Frames lost"))),
    dict(name="outage_stuck_reader", ids="T7+R1",
         cause="7 s outage; the receiver does not pick the stream up again",
         viewer="Picture frozen for good while the box says it is live",
         reconnect=False, seconds=40, actions=[(14, "loss 100%"), (21, None)],
         expect=has("stuck reader")),
    dict(name="late_join_no_sps", ids="E3",
         cause="Stream description (SPS/PPS) sent only once; receiver joins 6 s late via a relay",
         viewer="No picture at all for the late viewer",
         pipeline=dict(parse="h264parse config-interval=0"),
         x264_headers_once=True, relay_late=6,
         expect=has("No picture could be decoded")),
    dict(name="late_join_control", ids="E3",
         cause="Same late join, with SPS/PPS repeated at every keyframe (default)",
         viewer="Picture starts at the next keyframe",
         relay_late=6, expect=lambda r: r["facts"].get("arrived", 0) > 0),
    dict(name="starved", ids="E6",
         cause="Video bitrate 300 kbit/s for 1080p30 with noise",
         viewer="Blocky, smeared picture",
         pipeline=dict(venc=X264.replace("bitrate=4000", "bitrate=300")),
         expect=has("Blocky")),
    dict(name="range_mismatch", ids="C5+E12",
         cause="Limited-range picture flagged as full range",
         viewer="Grey blacks, washed out",
         pipeline_edit=lambda t: after_compositor(
             t, "capssetter caps=\"video/x-raw,colorimetry=(string)1:4:0:0\" !"),
         expect=has("Colour range")),
    dict(name="green_frame", ids="C3",
         cause="One all-zero frame (an unfilled capture buffer) at 10 s",
         viewer="A flash of flat green",
         pipeline_edit=lambda t: extra_layer(
             t, "filesrc location=/dev/zero blocksize=3110400 num-buffers=1 ! "
                "rawvideoparse width=1920 height=1080 format=i420 framerate=30/1",
             "xpos=0 ypos=0 zorder=9 offset=10000000000"),
         expect=has("Green frames")),
    dict(name="black_frames", ids="C3",
         cause="Half a second of black at 10 s",
         viewer="A black flash",
         pipeline_edit=lambda t: extra_layer(
             t, "videotestsrc pattern=black num-buffers=15 ! video/x-raw,width=1920,height=1080,framerate=30/1",
             "xpos=0 ypos=0 zorder=9 offset=10000000000"),
         expect=has("Black picture")),
    dict(name="interlaced", ids="C6",
         cause="Interlaced video (two moments woven into one frame)",
         viewer="Comb-like edges on movement",
         pipeline_edit=lambda t: after_compositor(t, "interlace field-pattern=1:1 top-field-first=true !"),
         expect=has("Interlaced")),
    dict(name="jitter_lat500", ids="T9",
         cause="Jitter 40±20 ms (packets reordered), latency 500 ms",
         viewer="Nothing: SRT repairs it",
         netem="delay 40ms 20ms distribution normal", latency=500,
         expect=lambda r: not any("SRT gave up" in p for p in r["problems"])),
    dict(name="jitter_lat120", ids="T9",
         cause="Same jitter, latency 120 ms",
         viewer="Breakups when packets arrive too late",
         netem="delay 40ms 20ms distribution normal", latency=120,
         expect=has("SRT gave up")),
    dict(name="mtu_1300", ids="T8",
         cause="Path MTU 1300 (SRT packets are 1,360 bytes on the wire)",
         viewer="Depends on the network: here IP fragments them",
         mtu=1300, expect=lambda r: True),
    dict(name="delay_control", ids="Delay",
         cause="Delay baseline: latency 1000 ms, no added delay",
         viewer="—", expect=fact("delay_ms", lambda x: x > 0)),
    dict(name="delay_srt_latency", ids="Delay",
         cause="SRT latency raised from 1,000 to 2,000 ms",
         viewer="1 s more delay", latency=2000,
         expect=fact("delay_parts_ms", lambda d: any("TSBPD 2000" in k for k in d))),
    dict(name="delay_network", ids="Delay",
         cause="150 ms added each way on the network (netem)",
         viewer="150 ms more delay",
         netem="delay 150ms",
         expect=fact("delay_parts_ms", lambda d: any("network" in k and 130 < v < 170 for k, v in d.items()))),
    dict(name="srtla_idle", ids="T11 / FAQ",
         cause="SRTLA bond of two links; link 1 registered but its packets never arrive",
         viewer="Nothing yet, but no backup: the second connection shows idle",
         srtla=True, link_netem={1: "loss 100%"}, link_netem_at=3,
         expect=has("carried nothing")),
    dict(name="srtla_lossy", ids="T11",
         cause="SRTLA bond; link 1 loses 25% of packets, link 0 clean",
         viewer="Usually nothing (the good link carries it); breakups if the good link drops",
         srtla=True, link_netem={1: "loss 25%"},
         expect=has("lost")),
]

# Cases made from the control recording by a receiver-side change.
POST_CASES = [
    dict(name="catchup_robotic", ids="C24 / FAQ",
         cause="Receiver speeds sound up 5% for 4 s to catch up (OBS-style buffer catch-up)",
         viewer="Robotic or warbling sound for a few seconds",
         filter="[0:a]asplit=3[a0][a1][a2];[a0]atrim=0:8,asetpts=PTS-STARTPTS[p0];"
                "[a1]atrim=8:12,asetpts=PTS-STARTPTS,asetrate=50400,aresample=48000[p1];"
                "[a2]atrim=12,asetpts=PTS-STARTPTS[p2];[p0][p1][p2]concat=n=3:v=0:a=1[aout]",
         expect=any_of(has("sped up"), has("Distorted sound"))),
    dict(name="atempo_drift", ids="C24",
         cause="Receiver plays sound 0.3% slow (atempo) against the picture",
         viewer="Lips and voice slowly come apart",
         filter="[0:a]atempo=0.997[aout]",
         expect=has("drifts away")),
]


def netns_script(case, run, args, latency, port):
    """Shell script run inside `unshare -rn`."""
    q = shlex.quote
    seconds = case.get("seconds", args.seconds)
    rx_port = port
    lines = ["set -u", "ip link set lo up"]
    if case.get("mtu"):
        lines.append(f"ip link set lo mtu {case['mtu']}")
    if case.get("srtla"):
        # per-link netem: traffic from/to 127.0.0.3 is link 1
        lines += [
            "tc qdisc add dev lo root handle 1: prio bands 4 priomap 3 3 3 3 3 3 3 3 3 3 3 3 3 3 3 3",
            "tc qdisc add dev lo parent 1:1 handle 10: netem delay 0ms",
            "tc qdisc add dev lo parent 1:4 handle 40: pfifo",
            "tc filter add dev lo parent 1: protocol ip prio 1 u32 match ip src 127.0.0.3/32 flowid 1:1",
            "tc filter add dev lo parent 1: protocol ip prio 1 u32 match ip dst 127.0.0.3/32 flowid 1:1",
        ]
    elif case.get("netem") or case.get("actions"):
        lines.append(f"tc qdisc add dev lo root netem {case.get('netem') or 'delay 0ms'}")
    cap = [sys.executable, str(ANALYSE), "capture", "--no-report", "--out", str(run / "cap"),
           "--seconds", str(seconds + 2), "--wait", "40", "--fps", str(case.get("fps", 30))]
    if case.get("reconnect") is False:
        cap.append("--no-reconnect")
    relay = case.get("relay_late")
    if relay:
        cap += ["--srt", f"udp://127.0.0.1:{rx_port + 2}"]
    else:
        cap += ["--srt", f"srt://:{rx_port}?mode=listener&latency={latency}"]
    cap_cmd = " ".join(q(c) for c in cap) + f" > {q(str(run / 'capture.log'))} 2>&1"
    send_port = rx_port
    if relay:
        lines.append(f"srt-live-transmit -q 'srt://:{rx_port}?mode=listener&latency={latency}' "
                     f"'udp://127.0.0.1:{rx_port + 2}' > {q(str(run / 'relay.log'))} 2>&1 &")
        lines.append("RELAY=$!")
    else:
        lines.append(cap_cmd + " &")
        lines.append("CAP=$!")
    if case.get("srtla"):
        ips = run / "ips"
        ips.write_text("127.0.0.2\n127.0.0.3\n")
        lines.append(f"{q(args.srtla_rec)} {rx_port + 1} 127.0.0.1 {rx_port} > {q(str(run / 'srtla_rec.log'))} 2>&1 &")
        lines.append("REC=$!")
        lines.append("sleep 0.5")
        lines.append(f"srtla_send --stats-file {q(str(run / 'srtla.json'))} {rx_port + 3} 127.0.0.1 {rx_port + 1} "
                     f"{q(str(ips))} > {q(str(run / 'srtla_send.log'))} 2>&1 &")
        lines.append("SEND=$!")
        send_port = rx_port + 3
    lines.append("sleep 1")
    env = f"CERACODER_STATS_FILE={q(str(run / 'stats.json'))} CERACODER_RECONNECT_BASE_MS=500"
    cc = [args.ceracoder, str(run / "pipeline"), "127.0.0.1", str(send_port),
          "-c", str(run / "ceracoder.conf"), "-l", str(latency)] + case.get("ceracoder_args", [])
    lines.append(f"{env} {' '.join(q(c) for c in cc)} > {q(str(run / 'ceracoder.log'))} 2>&1 &")
    lines.append("CC=$!")
    if relay:
        lines.append(f"sleep {relay}")
        lines.append(cap_cmd + " &")
        lines.append("CAP=$!")
    # once a second: keep the sender (and SRTLA) stats; apply timed actions
    actions = {int(t): a for t, a in case.get("actions", [])}
    if case.get("link_netem"):
        for link, spec in case["link_netem"].items():
            actions[int(case.get("link_netem_at", 0))] = ("LINK", spec)
    lines.append(f"for i in $(seq 1 {seconds}); do")
    lines.append("  sleep 1")
    lines.append(f"  cat {q(str(run / 'stats.json'))} >> {q(str(run / 'stats.jsonl'))} 2>/dev/null; echo >> /dev/null")
    if case.get("srtla"):
        lines.append(f"  (cat {q(str(run / 'srtla.json'))}; echo) >> {q(str(run / 'srtla.jsonl'))} 2>/dev/null")
    for t, a in sorted(actions.items()):
        if isinstance(a, tuple):
            cmd = f"tc qdisc change dev lo parent 1:1 handle 10: netem {a[1]}"
        elif a is None:
            cmd = f"tc qdisc change dev lo root netem {case.get('netem') or 'delay 0ms'}"
        else:
            cmd = f"tc qdisc change dev lo root netem {a}"
        lines.append(f"  if [ $i -eq {t} ]; then {cmd}; echo \"$(date +%s.%N) {cmd}\" >> {q(str(run / 'actions.log'))}; fi")
    lines.append("done")
    lines.append("kill -TERM $CC 2>/dev/null; wait $CC 2>/dev/null")
    lines.append("wait $CAP 2>/dev/null")
    lines.append("kill $(jobs -p) 2>/dev/null; wait 2>/dev/null; true")
    return "\n".join(lines) + "\n"


def run_case(case, args, port):
    run = Path(args.out) / case["name"]
    if run.exists():
        shutil.rmtree(run)
    run.mkdir(parents=True)
    latency = case.get("latency", 1000)
    kw = case.get("pipeline", {})
    text = base_pipeline(**kw)
    if case.get("x264_headers_once"):
        # x264 repeats SPS/PPS at every IDR by default; turn that off
        text = text.replace("name=venc_kbps", "option-string=\"repeat-headers=0\" name=venc_kbps", 1)
    if case.get("pipeline_edit"):
        text = case["pipeline_edit"](text)
    (run / "pipeline").write_text(text)
    (run / "ceracoder.conf").write_text(
        f"[general]\nmin_bitrate = 500\nmax_bitrate = 4000\nbalancer = fixed\n[srt]\nlatency = {latency}\n")
    script = netns_script(case, run, args, latency, port)
    (run / "run.sh").write_text(script)
    t0 = time.time()
    subprocess.run(["unshare", "-rn", "bash", str(run / "run.sh")], cwd=run,
                   stdout=open(run / "run.log", "wb"), stderr=subprocess.STDOUT,
                   timeout=case.get("seconds", args.seconds) + 120)
    return report_case(case, run, t0)


def report_case(case, run, t0, rec=None, extra=()):
    rec = rec or run / "cap" / "rec.ts"
    cmd = [sys.executable, str(ANALYSE), "report", str(rec), "--json", str(run / "report.json"),
           "--fps", str(case.get("fps", 30))]
    if (run / "cap" / "arrivals" / "meta.txt").exists() and not extra:
        cmd += ["--arrivals", str(run / "cap" / "arrivals")]
    if (run / "cap" / "srt-stats.json").exists() and not extra:
        cmd += ["--srt-stats", str(run / "cap" / "srt-stats.json")]
    if (run / "stats.jsonl").exists() and not extra:
        cmd += ["--sender-stats", str(run / "stats.jsonl")]
    if (run / "srtla.jsonl").exists():
        cmd += ["--srtla-stats", str(run / "srtla.jsonl")]
    cmd += list(extra)
    cc_log = run / "ceracoder.log"
    if cc_log.exists() and "Syntax: ceracoder" in cc_log.read_text(errors="replace"):
        res = {"facts": {}, "problems": ["(setup failed: ceracoder refused its arguments)"], "fine": [],
               "report": ""}
    elif not rec.exists():
        res = {"facts": {}, "problems": ["(no recording)"], "fine": [], "report": "(no recording)"}
    else:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        (run / "report.txt").write_bytes(p.stdout + (b"\n--- stderr ---\n" + p.stderr if p.returncode else b""))
        try:
            res = json.loads((run / "report.json").read_text())
        except (OSError, json.JSONDecodeError):
            res = {"facts": {}, "problems": [f"(analyser failed: {p.stderr.decode()[-300:]})"], "fine": [],
                   "report": ""}
    try:
        detected = bool(case["expect"](res))
    except Exception as e:  # noqa: BLE001
        detected = False
        res.setdefault("problems", []).append(f"(check failed: {e})")
    out = {"name": case["name"], "ids": case["ids"], "cause": case["cause"], "viewer": case["viewer"],
           "detected": detected, "problems": res.get("problems", []), "facts": res.get("facts", {}),
           "seconds": round(time.time() - t0, 1)}
    (run / "result.json").write_text(json.dumps(out, indent=2, default=str))
    return out


def run_post(case, args, control_dir):
    run = Path(args.out) / case["name"]
    if run.exists():
        shutil.rmtree(run)
    run.mkdir(parents=True)
    src = control_dir / "cap" / "rec.ts"
    rec = run / "rec.ts"
    t0 = time.time()
    subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-copyts", "-i", str(src),
                    "-filter_complex", case["filter"], "-map", "0:v", "-map", "[aout]", "-c:v", "copy",
                    "-c:a", "aac", "-b:a", "128k", "-muxdelay", "0", "-f", "mpegts", "-y", str(rec)], check=True)
    return report_case(case, run, t0, rec=rec, extra=["--warmup", "3"])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True)
    ap.add_argument("--only", help="comma-separated case names")
    ap.add_argument("--seconds", type=int, default=25)
    ap.add_argument("--ceracoder", default=str(REPO / "ceracoder"))
    ap.add_argument("--srtla-rec", default=shutil.which("srtla_rec") or "srtla_rec")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()
    if args.list:
        for c in CASES + POST_CASES:
            print(f"{c['name']:22} {c['ids']:10} {c['cause']}")
        return 0
    only = set(args.only.split(",")) if args.only else None
    Path(args.out).mkdir(parents=True, exist_ok=True)
    results = []
    port = 4100
    for case in CASES:
        if only and case["name"] not in only:
            continue
        print(f"== {case['name']}: {case['cause']}", flush=True)
        r = run_case(case, args, port)
        print(f"   detected={r['detected']} ({r['seconds']} s)", flush=True)
        for p in r["problems"]:
            print(f"   - {p}", flush=True)
        results.append(r)
    control = Path(args.out) / "control"
    for case in POST_CASES:
        if only and case["name"] not in only:
            continue
        if not (control / "cap" / "rec.ts").exists():
            print(f"== {case['name']}: skipped (needs the control recording)")
            continue
        print(f"== {case['name']}: {case['cause']}", flush=True)
        r = run_post(case, args, control)
        print(f"   detected={r['detected']}", flush=True)
        for p in r["problems"]:
            print(f"   - {p}", flush=True)
        results.append(r)
    old = []
    summary = Path(args.out) / "results.json"
    if summary.exists() and only:
        old = [r for r in json.loads(summary.read_text()) if r["name"] not in {x["name"] for x in results}]
    summary.write_text(json.dumps(old + results, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
