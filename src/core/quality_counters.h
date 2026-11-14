/*
    ceracoder - live video encoder with dynamic bitrate control
    Copyright (C) 2026 CERALIVE

    This program is free software: you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.

    You should have received a copy of the GNU General Public License
    along with this program.  If not, see <https://www.gnu.org/licenses/>.
*/

#ifndef CERACODER_QUALITY_COUNTERS_H
#define CERACODER_QUALITY_COUNTERS_H

#include <stdatomic.h>
#include <stddef.h>
#include <stdint.h>

/*
 * Quality counters: cheap numbers the encoder already has, published so the
 * box can say "the camera delivered 25 of 30 frames per second" or "the stream
 * spent 40 s at the minimum bitrate" without decoding anything.
 *
 * This translation unit is pure (no GStreamer, no clock): time is injected, so
 * the accounting is unit-tested deterministically. src/gst/quality_probe.c
 * gathers the pipeline-side values and ceracoder.c writes the snapshot to the
 * file named by CERACODER_STATS_FILE once per second.
 */

#define QUALITY_MAX_VIDEORATE 4
#define QUALITY_CAPS_LEN 256
#define QUALITY_NAME_LEN 32

typedef struct {
    /* Time at the minimum bitrate (main loop only). */
    uint64_t at_min_ms;
    uint64_t last_bitrate_sample_ms;
    int last_bitrate_bps;
    int last_min_bitrate_bps;

    /* Frames entering the encoder (streaming thread writes, main loop reads). */
    _Atomic uint64_t encoder_frames;

    /* Sender pipeline latency: running time now minus the buffer's PTS when
     * a muxed buffer reaches appsink (streaming thread writes). Window stats
     * are reset by quality_latency_take(). Values in microseconds. */
    _Atomic int64_t latency_last_us;
    _Atomic int64_t latency_sum_us;
    _Atomic uint64_t latency_count;
    _Atomic int64_t latency_max_us;
} QualityCounters;

typedef struct {
    char name[QUALITY_NAME_LEN];
    uint64_t in, out, duplicate, drop;
} QualityVideorate;

typedef struct {
    uint64_t time_ms;          /* wall clock, Unix ms */
    uint64_t uptime_ms;
    uint64_t pattern_epoch_ms; /* 0 when the pipeline has no pattern clock */

    uint64_t encoder_frames;
    int nominal_fps_n, nominal_fps_d; /* 0/0 when unknown */
    double fps_measured;              /* < 0 when unknown */

    int n_videorate;
    QualityVideorate videorate[QUALITY_MAX_VIDEORATE];

    char input_caps[QUALITY_CAPS_LEN]; /* empty when unknown */

    int bitrate_bps, min_bitrate_bps, max_bitrate_bps;
    uint64_t at_min_ms;

    int latency_valid;
    double latency_last_ms, latency_avg_ms, latency_max_ms;

    int srt_valid;
    double srt_rtt_ms;
    int srt_send_buffer_ms;
    int srt_latency_ms;
    int64_t srt_pkt_snd_drop_total, srt_pkt_snd_loss_total, srt_pkt_retrans_total;
} QualitySnapshot;

void quality_counters_init(QualityCounters *qc, uint64_t now_ms);

/* Account time spent at (or below) the minimum bitrate. Call on every bitrate
 * update; the interval since the previous call is attributed to the bitrate
 * that was in force during it. */
void quality_bitrate_sample(QualityCounters *qc, uint64_t now_ms,
                            int bitrate_bps, int min_bitrate_bps);

void quality_encoder_frame(QualityCounters *qc);
uint64_t quality_encoder_frames(QualityCounters *qc);

void quality_latency_record(QualityCounters *qc, int64_t latency_us);
/* Read and reset the latency window. Returns 0 when no sample was recorded. */
int quality_latency_take(QualityCounters *qc, double *last_ms, double *avg_ms,
                         double *max_ms);

/* Bit k of the pattern clock (Unix ms of frame 0). */
int quality_epoch_bit(uint64_t epoch_ms, int k);

/* Write the snapshot as one JSON object. Returns the length written, or -1
 * when the buffer is too small. */
int quality_snapshot_json(const QualitySnapshot *s, char *buf, size_t len);

#endif
