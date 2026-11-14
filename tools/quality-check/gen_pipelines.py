#!/usr/bin/env python3
"""Generate the quality test-pattern pipelines.

The pattern uses only stock GStreamer elements (plugins-base, -good, -bad),
so it runs wherever the encoder runs. See tools/quality-check/README.md for
the layout and how the far end reads it.

Usage: python3 tools/quality-check/gen_pipelines.py   (rewrites the files in place)
"""
from pathlib import Path

from pattern_layout import *  # noqa: F401,F403  (shared constants)

ROOT = Path(__file__).resolve().parents[2] / "pipeline"
WHITE = "0xffffffff"
BLACK = "0xff000000"


def pad(n, x, y, w, h, z):
    return (f"sink_{n}::xpos={x} sink_{n}::ypos={y} sink_{n}::width={w} "
            f"sink_{n}::height={h} sink_{n}::zorder={z}")


def video_chain(fmt, fps=None, out_fps=None):
    """fps: rate of the pattern itself (a frame-rate fraction string); out_fps:
    pipeline rate after videorate, to emulate a camera at another rate."""
    fps = fps or f"{FPS}/1"
    pads, srcs = [], []

    def add(src, x, y, w, h, z):
        n = len(srcs)
        srcs.append(f"{src} ! comp.sink_{n}")
        pads.append(pad(n, x, y, w, h, z))

    live = f"framerate={fps}"
    num, den = (int(x) for x in fps.split("/"))
    f = f"format={fmt}"
    # Every source is made at its final size and format, so the compositor
    # only copies blocks (no scaling or conversion). The colour bars are drawn
    # once a second; the compositor repeats the last buffer of slow inputs.
    add(f"videotestsrc pattern=smpte100 ! video/x-raw,{f},width={REF_W},height={REF_H},framerate=1/1",
        0, 0, REF_W, REF_H, 0)
    add(f"videotestsrc is-live=true pattern=ball motion=sweep ! video/x-raw,{f},width={REF_W},height={BALL_H},{live}",
        0, BALL_Y, REF_W, BALL_H, 1)
    add(f"videotestsrc is-live=true pattern=snow ! video/x-raw,{f},width={NOISE_W},height={NOISE_H},{live}",
        NOISE_X, NOISE_Y, NOISE_W, NOISE_H, 1)
    add(f"videotestsrc pattern=blink background-color={WHITE} foreground-color={BLACK} ! "
        f"video/x-raw,{f},width={MARK_W},height={MARK_H},framerate=2/1", MARK_X, MARK_Y, MARK_W, MARK_H, 2)
    # Frame counter: bit k is a square wave with a period of 2^(k+1) frames,
    # made by a blink source running at fps / 2^k.
    for k in range(FRAME_BITS):
        for inv in (0, 1):
            bg, fg = (BLACK, WHITE) if not inv else (WHITE, BLACK)
            add(f"videotestsrc pattern=blink background-color={bg} foreground-color={fg} ! "
                f"video/x-raw,{f},width={FRAME_HALF},height={FRAME_H},framerate={num}/{den * 2 ** k}",
                (2 * k + inv) * FRAME_HALF, FRAME_Y, FRAME_HALF, FRAME_H, 2)
    # Pattern clock: static blocks, coloured by the encoder before it starts.
    for k in range(EPOCH_BITS):
        row, col = divmod(k, EPOCH_PER_ROW)
        for inv, name in ((0, "a"), (1, "b")):
            fg = BLACK if not inv else WHITE
            add(f"videotestsrc name=qp_epoch_{k}_{name} pattern=solid-color foreground-color={fg} ! "
                f"video/x-raw,{f},width={EPOCH_HALF},height={EPOCH_H},framerate=1/1",
                (2 * col + inv) * EPOCH_HALF, EPOCH_ROWS_Y[row], EPOCH_HALF, EPOCH_H, 2)

    tail = [
        "compositor name=comp background=black " + " ".join(pads) + " !",
        f"video/x-raw,width={REF_W},height={REF_H},{live},format={fmt} !",
    ] + ([f"videorate ! video/x-raw,framerate={out_fps} !"] if out_fps else []) + [
        "identity name=v_delay signal-handoffs=TRUE !",
        "timecodestamper ! timeoverlay time-mode=time-code font-desc=\"Sans Bold 16\" "
        "halignment=left valignment=top shaded-background=true !",
        "clockoverlay time-format=\"%Y-%m-%d %H:%M:%S\" font-desc=\"Sans 12\" "
        "halignment=left valignment=top ypad=72 shaded-background=true !",
        "textoverlay text='' valignment=top halignment=right font-desc=\"Monospace, 5\" name=overlay ! queue !",
    ]
    return srcs + tail


def audio_chain(enc):
    def mono(src, matrix):
        return (f"{src} ! audio/x-raw,format=F32LE,channels=1,rate=48000 ! "
                f"audioconvert mix-matrix=\"{matrix}\" ! "
                "audio/x-raw,format=F32LE,channels=2,channel-mask=(bitmask)0x3,rate=48000 ! amix.")
    left = "<<(float)1.0>, <(float)0.0>>"
    right = "<<(float)0.0>, <(float)1.0>>"
    both = "<<(float)1.0>, <(float)1.0>>"
    periods = BEEP_HZ * BEEP_MS // 1000
    return [
        mono(f"audiotestsrc is-live=true wave=sine freq={LEFT_HZ} volume={TONE_AMP}", left),
        mono(f"audiotestsrc is-live=true wave=sine freq={RIGHT_HZ} volume={TONE_AMP}", right),
        mono(f"audiotestsrc is-live=true wave=ticks freq={BEEP_HZ} volume={BEEP_AMP} "
             f"sine-periods-per-tick={periods} tick-interval=1000000000 apply-tick-ramp=true", both),
        "audiomixer name=amix ! audio/x-raw,channels=2,rate=48000 ! "
        "identity name=a_delay signal-handoffs=TRUE ! audioconvert !",
        f"{enc} ! aacparse ! queue max-size-time=10000000000 max-size-buffers=1000 ! mux.",
    ]


def render(fmt, venc, parse, aenc, fps=None, out_fps=None):
    lines = video_chain(fmt, fps, out_fps) + [
        venc + " !",
        f"{parse} config-interval=-1 ! queue max-size-time=10000000000 max-size-buffers=1000 "
        "max-size-bytes=41943040 ! mux.",
    ] + audio_chain(aenc) + ["mpegtsmux name=mux !", "appsink name=appsink"]
    return "\n".join(lines) + "\n"


TARGETS = {
    "rk3588/h265_quality_pattern": dict(
        fmt="NV12",
        venc="mpph265enc zero-copy-pkt=0 qp-max=51 gop=60 name=venc_bps",
        parse="h265parse",
        aenc="voaacenc bitrate=128000"),
    "generic/x264_superfast_quality_pattern": dict(
        fmt="I420",
        venc="x264enc speed-preset=superfast key-int-max=60 name=venc_kbps",
        parse="h264parse",
        aenc="avenc_aac bitrate=131072"),
}


def main():
    for rel, opts in TARGETS.items():
        (ROOT / rel).write_text(render(**opts))
        print(f"wrote pipeline/{rel}")


if __name__ == "__main__":
    main()
