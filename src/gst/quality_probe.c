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
#include "quality_probe.h"

#include <stdio.h>
#include <string.h>

#define QP_WHITE 0xffffffffu
#define QP_BLACK 0xff000000u
#define QP_EPOCH_MAX_BITS 64

uint64_t quality_pattern_clock_setup(GstPipeline *pipeline) {
    GstElement *first = gst_bin_get_by_name(GST_BIN(pipeline), "qp_epoch_0_a");
    if (first == NULL) return 0;
    gst_object_unref(first);

    GstClock *clock = g_object_new(GST_TYPE_SYSTEM_CLOCK, "clock-type",
                                   GST_CLOCK_TYPE_REALTIME, NULL);
    gst_pipeline_use_clock(pipeline, clock);

    GstClockTime now = gst_clock_get_time(clock);
    /* Frame 0 is "now". A base time in the future would give the live
     * sources negative running times at start, so their first buffers would
     * never be shown; the few ms until PLAYING only delay the first frames. */
    uint64_t epoch_ms = (now + GST_MSECOND - 1) / GST_MSECOND;
    gst_element_set_start_time(GST_ELEMENT(pipeline), GST_CLOCK_TIME_NONE);
    gst_element_set_base_time(GST_ELEMENT(pipeline), epoch_ms * GST_MSECOND);
    gst_object_unref(clock);

    int bits = 0;
    for (int k = 0; k < QP_EPOCH_MAX_BITS; k++) {
        char name[32];
        int bit = quality_epoch_bit(epoch_ms, k);
        snprintf(name, sizeof(name), "qp_epoch_%d_a", k);
        GstElement *a = gst_bin_get_by_name(GST_BIN(pipeline), name);
        snprintf(name, sizeof(name), "qp_epoch_%d_b", k);
        GstElement *b = gst_bin_get_by_name(GST_BIN(pipeline), name);
        if (a) {
            g_object_set(a, "foreground-color", bit ? QP_WHITE : QP_BLACK, NULL);
            gst_object_unref(a);
        }
        if (b) {
            g_object_set(b, "foreground-color", bit ? QP_BLACK : QP_WHITE, NULL);
            gst_object_unref(b);
        }
        if (!a && !b) break;
        bits++;
    }
    if (bits < 64 && (epoch_ms >> bits) != 0) {
        fprintf(stderr, "Test pattern clock: only %d bits, the clock does not fit\n", bits);
    }
    fprintf(stderr, "Test pattern clock: frame 0 at %llu ms (Unix time, wall clock)\n",
            (unsigned long long)epoch_ms);
    return epoch_ms;
}

static GstPadProbeReturn encoder_probe(GstPad *pad, GstPadProbeInfo *info, gpointer data) {
    (void)pad;
    (void)info;
    quality_encoder_frame((QualityCounters *)data);
    return GST_PAD_PROBE_OK;
}

static GstElement *find_encoder(GstPipeline *pipeline) {
    GstElement *enc = gst_bin_get_by_name(GST_BIN(pipeline), "venc_bps");
    if (enc == NULL) enc = gst_bin_get_by_name(GST_BIN(pipeline), "venc_kbps");
    return enc;
}

void quality_install_encoder_probe(GstPipeline *pipeline, QualityCounters *qc) {
    GstElement *enc = find_encoder(pipeline);
    if (enc == NULL) return;
    GstPad *pad = gst_element_get_static_pad(enc, "sink");
    if (pad) {
        gst_pad_add_probe(pad, GST_PAD_PROBE_TYPE_BUFFER, encoder_probe, qc, NULL);
        gst_object_unref(pad);
    }
    gst_object_unref(enc);
}

void quality_record_buffer_latency(GstElement *sink, GstBuffer *buffer, QualityCounters *qc) {
    if (!GST_BUFFER_PTS_IS_VALID(buffer)) return;
    GstClock *clock = gst_element_get_clock(sink);
    if (clock == NULL) return;
    GstClockTime now = gst_clock_get_time(clock);
    GstClockTime base = gst_element_get_base_time(sink);
    gst_object_unref(clock);
    if (now < base) return;
    int64_t running_now = (int64_t)(now - base);
    int64_t pts = (int64_t)GST_BUFFER_PTS(buffer);
    quality_latency_record(qc, (running_now - pts) / 1000);
}

static void caps_to_field(GstCaps *caps, char *dst, size_t len) {
    gchar *s = gst_caps_to_string(caps);
    if (s) {
        g_strlcpy(dst, s, len);
        g_free(s);
    }
}

void quality_collect(GstPipeline *pipeline, QualitySnapshot *snap) {
    snap->n_videorate = 0;
    snap->input_caps[0] = '\0';
    snap->nominal_fps_n = snap->nominal_fps_d = 0;

    GstIterator *it = gst_bin_iterate_recurse(GST_BIN(pipeline));
    GValue item = G_VALUE_INIT;
    while (gst_iterator_next(it, &item) == GST_ITERATOR_OK) {
        GstElement *el = GST_ELEMENT(g_value_get_object(&item));
        GstElementFactory *f = gst_element_get_factory(el);
        if (f && strcmp(GST_OBJECT_NAME(f), "videorate") == 0 &&
            snap->n_videorate < QUALITY_MAX_VIDEORATE) {
            QualityVideorate *v = &snap->videorate[snap->n_videorate++];
            guint64 in = 0, out = 0, dup = 0, drop = 0;
            g_object_get(el, "in", &in, "out", &out, "duplicate", &dup, "drop", &drop, NULL);
            g_strlcpy(v->name, GST_OBJECT_NAME(el), sizeof(v->name));
            v->in = in; v->out = out; v->duplicate = dup; v->drop = drop;
        }
        g_value_reset(&item);
    }
    g_value_unset(&item);
    gst_iterator_free(it);

    /* What the capture actually delivers: the caps entering ptsfixup, which
     * every HDMI/USB capture pipeline places right after its source. */
    GstElement *fix = gst_bin_get_by_name(GST_BIN(pipeline), "ptsfixup");
    if (fix) {
        GstPad *pad = gst_element_get_static_pad(fix, "sink");
        if (pad) {
            GstCaps *caps = gst_pad_get_current_caps(pad);
            if (caps) {
                caps_to_field(caps, snap->input_caps, sizeof(snap->input_caps));
                gst_caps_unref(caps);
            }
            gst_object_unref(pad);
        }
        gst_object_unref(fix);
    }

    GstElement *enc = find_encoder(pipeline);
    if (enc) {
        GstPad *pad = gst_element_get_static_pad(enc, "sink");
        if (pad) {
            GstCaps *caps = gst_pad_get_current_caps(pad);
            if (caps) {
                const GstStructure *st = gst_caps_get_structure(caps, 0);
                gint n, d;
                if (gst_structure_get_fraction(st, "framerate", &n, &d)) {
                    snap->nominal_fps_n = n;
                    snap->nominal_fps_d = d;
                }
                gst_caps_unref(caps);
            }
            gst_object_unref(pad);
        }
        gst_object_unref(enc);
    }
}
