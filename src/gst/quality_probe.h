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
#ifndef CERACODER_QUALITY_PROBE_H
#define CERACODER_QUALITY_PROBE_H

#include <gst/gst.h>
#include "quality_counters.h"

/*
 * Pipeline-side glue for the quality counters and the test-pattern clock.
 */

/* If the pipeline carries the quality pattern's clock blocks (qp_epoch_*),
 * switch it to the wall clock, take "now" as the base time (frame 0), write
 * it into the blocks and return it in Unix ms. Call right before setting the
 * pipeline to PLAYING. Returns 0 (and changes nothing) for every
 * other pipeline. */
uint64_t quality_pattern_clock_setup(GstPipeline *pipeline);

/* Count frames entering the video encoder (venc_bps / venc_kbps). */
void quality_install_encoder_probe(GstPipeline *pipeline, QualityCounters *qc);

/* Sender latency for one muxed buffer: running time now minus its PTS. */
void quality_record_buffer_latency(GstElement *sink, GstBuffer *buffer, QualityCounters *qc);

/* Fill the pipeline fields of the snapshot: videorate counters, input caps,
 * nominal frame rate. */
void quality_collect(GstPipeline *pipeline, QualitySnapshot *snap);

#endif
