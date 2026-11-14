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

#include "quality_counters.h"

#include <stdarg.h>
#include <stdio.h>
#include <string.h>

void quality_counters_init(QualityCounters *qc, uint64_t now_ms) {
    qc->at_min_ms = 0;
    qc->last_bitrate_sample_ms = now_ms;
    qc->last_bitrate_bps = -1;
    qc->last_min_bitrate_bps = 0;
    atomic_store_explicit(&qc->encoder_frames, 0, memory_order_relaxed);
    atomic_store_explicit(&qc->latency_last_us, 0, memory_order_relaxed);
    atomic_store_explicit(&qc->latency_sum_us, 0, memory_order_relaxed);
    atomic_store_explicit(&qc->latency_count, 0, memory_order_relaxed);
    atomic_store_explicit(&qc->latency_max_us, INT64_MIN, memory_order_relaxed);
}

void quality_bitrate_sample(QualityCounters *qc, uint64_t now_ms,
                            int bitrate_bps, int min_bitrate_bps) {
    if (qc->last_bitrate_bps >= 0 && now_ms > qc->last_bitrate_sample_ms &&
        qc->last_bitrate_bps <= qc->last_min_bitrate_bps) {
        qc->at_min_ms += now_ms - qc->last_bitrate_sample_ms;
    }
    qc->last_bitrate_sample_ms = now_ms;
    qc->last_bitrate_bps = bitrate_bps;
    qc->last_min_bitrate_bps = min_bitrate_bps;
}

void quality_encoder_frame(QualityCounters *qc) {
    atomic_fetch_add_explicit(&qc->encoder_frames, 1, memory_order_relaxed);
}

uint64_t quality_encoder_frames(QualityCounters *qc) {
    return atomic_load_explicit(&qc->encoder_frames, memory_order_relaxed);
}

void quality_latency_record(QualityCounters *qc, int64_t latency_us) {
    atomic_store_explicit(&qc->latency_last_us, latency_us, memory_order_relaxed);
    atomic_fetch_add_explicit(&qc->latency_sum_us, latency_us, memory_order_relaxed);
    atomic_fetch_add_explicit(&qc->latency_count, 1, memory_order_relaxed);
    int64_t cur = atomic_load_explicit(&qc->latency_max_us, memory_order_relaxed);
    while (latency_us > cur &&
           !atomic_compare_exchange_weak_explicit(&qc->latency_max_us, &cur, latency_us,
                                                  memory_order_relaxed, memory_order_relaxed)) {
    }
}

int quality_latency_take(QualityCounters *qc, double *last_ms, double *avg_ms,
                         double *max_ms) {
    uint64_t n = atomic_exchange_explicit(&qc->latency_count, 0, memory_order_relaxed);
    int64_t sum = atomic_exchange_explicit(&qc->latency_sum_us, 0, memory_order_relaxed);
    int64_t max = atomic_exchange_explicit(&qc->latency_max_us, INT64_MIN, memory_order_relaxed);
    if (n == 0) return 0;
    *last_ms = atomic_load_explicit(&qc->latency_last_us, memory_order_relaxed) / 1000.0;
    *avg_ms = (double)sum / (double)n / 1000.0;
    *max_ms = max / 1000.0;
    return 1;
}

int quality_epoch_bit(uint64_t epoch_ms, int k) {
    if (k < 0 || k > 63) return 0;
    return (int)((epoch_ms >> k) & 1u);
}

/* Small bounded appender: tracks overflow instead of truncating silently. */
typedef struct { char *buf; size_t len, pos; int overflow; } Out;

static void out(Out *o, const char *fmt, ...) {
    if (o->overflow) return;
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(o->buf + o->pos, o->len - o->pos, fmt, ap);
    va_end(ap);
    if (n < 0 || (size_t)n >= o->len - o->pos) { o->overflow = 1; return; }
    o->pos += (size_t)n;
}

static void out_json_string(Out *o, const char *s) {
    out(o, "\"");
    for (; *s; s++) {
        unsigned char c = (unsigned char)*s;
        if (c == '"' || c == '\\') out(o, "\\%c", c);
        else if (c < 0x20) out(o, "\\u%04x", c);
        else out(o, "%c", c);
    }
    out(o, "\"");
}

int quality_snapshot_json(const QualitySnapshot *s, char *buf, size_t len) {
    if (len == 0) return -1;
    Out o = { buf, len, 0, 0 };
    out(&o, "{\"version\":1,\"time_ms\":%llu,\"uptime_ms\":%llu,",
        (unsigned long long)s->time_ms, (unsigned long long)s->uptime_ms);
    if (s->pattern_epoch_ms) out(&o, "\"pattern_epoch_ms\":%llu,", (unsigned long long)s->pattern_epoch_ms);
    else out(&o, "\"pattern_epoch_ms\":null,");

    out(&o, "\"encoder\":{\"frames\":%llu,", (unsigned long long)s->encoder_frames);
    if (s->nominal_fps_d > 0) out(&o, "\"nominal_fps\":\"%d/%d\",", s->nominal_fps_n, s->nominal_fps_d);
    else out(&o, "\"nominal_fps\":null,");
    if (s->fps_measured >= 0) out(&o, "\"fps\":%.2f},", s->fps_measured);
    else out(&o, "\"fps\":null},");

    out(&o, "\"videorate\":[");
    for (int i = 0; i < s->n_videorate; i++) {
        const QualityVideorate *v = &s->videorate[i];
        out(&o, "%s{\"name\":", i ? "," : "");
        out_json_string(&o, v->name);
        out(&o, ",\"in\":%llu,\"out\":%llu,\"duplicate\":%llu,\"drop\":%llu}",
            (unsigned long long)v->in, (unsigned long long)v->out,
            (unsigned long long)v->duplicate, (unsigned long long)v->drop);
    }
    out(&o, "],");

    out(&o, "\"input_caps\":");
    if (s->input_caps[0]) out_json_string(&o, s->input_caps);
    else out(&o, "null");

    out(&o, ",\"bitrate\":{\"current_kbps\":%d,\"min_kbps\":%d,\"max_kbps\":%d,\"at_min_ms\":%llu},",
        s->bitrate_bps / 1000, s->min_bitrate_bps / 1000, s->max_bitrate_bps / 1000,
        (unsigned long long)s->at_min_ms);

    if (s->latency_valid)
        out(&o, "\"sender_latency_ms\":{\"last\":%.1f,\"avg\":%.1f,\"max\":%.1f},",
            s->latency_last_ms, s->latency_avg_ms, s->latency_max_ms);
    else out(&o, "\"sender_latency_ms\":null,");

    if (s->srt_valid)
        out(&o, "\"srt\":{\"rtt_ms\":%.1f,\"send_buffer_ms\":%d,\"latency_ms\":%d,"
                "\"pkt_snd_drop_total\":%lld,\"pkt_snd_loss_total\":%lld,\"pkt_retrans_total\":%lld}",
            s->srt_rtt_ms, s->srt_send_buffer_ms, s->srt_latency_ms,
            (long long)s->srt_pkt_snd_drop_total, (long long)s->srt_pkt_snd_loss_total,
            (long long)s->srt_pkt_retrans_total);
    else out(&o, "\"srt\":null");
    out(&o, "}\n");
    return o.overflow ? -1 : (int)o.pos;
}
