/**
 * Quality counters published by ceracoder.
 *
 * When the environment variable CERACODER_STATS_FILE names a path, ceracoder
 * writes one JSON snapshot there each second (temp file, then rename). This
 * module describes that snapshot and reads it, so a host application can show
 * the counters next to the stream status, for example "camera delivers 25 of
 * 30 frames per second" or "40 s at the minimum bitrate".
 *
 * Stub: the schema and reader are complete; deciding how a host application
 * polls and presents the values is left to that application.
 */

import fs from "node:fs";
import { z } from "zod";

export const QUALITY_STATS_ENV = "CERACODER_STATS_FILE";

export const qualityVideorateSchema = z.object({
	name: z.string(),
	in: z.number().int().nonnegative(),
	out: z.number().int().nonnegative(),
	duplicate: z.number().int().nonnegative(),
	drop: z.number().int().nonnegative(),
});

export const qualityCountersSchema = z.object({
	version: z.literal(1),
	/** Wall clock of the snapshot, Unix ms */
	time_ms: z.number(),
	uptime_ms: z.number(),
	/** Test pattern only: wall-clock time of frame 0, Unix ms */
	pattern_epoch_ms: z.number().nullable(),
	encoder: z.object({
		/** Frames that entered the video encoder since start */
		frames: z.number().int().nonnegative(),
		/** Pipeline frame rate, e.g. "30/1" */
		nominal_fps: z.string().nullable(),
		/** Frames per second into the encoder over the last interval */
		fps: z.number().nullable(),
	}),
	/** Counters of every videorate element: duplicate = frames the source did not deliver */
	videorate: z.array(qualityVideorateSchema),
	/** Caps the capture actually delivers (entering ptsfixup), when known */
	input_caps: z.string().nullable(),
	bitrate: z.object({
		current_kbps: z.number(),
		min_kbps: z.number(),
		max_kbps: z.number(),
		/** Time spent at (or below) the minimum bitrate since start */
		at_min_ms: z.number().nonnegative(),
	}),
	/** Running time minus PTS for muxed buffers reaching the SRT sender */
	sender_latency_ms: z
		.object({ last: z.number(), avg: z.number(), max: z.number() })
		.nullable(),
	srt: z
		.object({
			rtt_ms: z.number(),
			send_buffer_ms: z.number(),
			latency_ms: z.number(),
			pkt_snd_drop_total: z.number(),
			pkt_snd_loss_total: z.number(),
			pkt_retrans_total: z.number(),
		})
		.nullable(),
});
export type QualityCounters = z.infer<typeof qualityCountersSchema>;

/** Parse one snapshot; returns undefined for anything that does not match. */
export function parseQualityCounters(text: string): QualityCounters | undefined {
	try {
		const parsed = qualityCountersSchema.safeParse(JSON.parse(text));
		return parsed.success ? parsed.data : undefined;
	} catch {
		return undefined;
	}
}

/** Read the snapshot file; undefined when missing, stale or malformed. */
export function readQualityCounters(
	filePath: string,
	opts: { maxAgeMs?: number; now?: number } = {},
): QualityCounters | undefined {
	let text: string;
	try {
		text = fs.readFileSync(filePath, "utf8");
	} catch {
		return undefined;
	}
	const counters = parseQualityCounters(text);
	if (!counters) return undefined;
	const maxAge = opts.maxAgeMs ?? 5000;
	const now = opts.now ?? Date.now();
	return now - counters.time_ms > maxAge ? undefined : counters;
}

/** Plain summary of what the counters say, for a status line. */
export interface QualitySummary {
	/** Frames per second actually reaching the encoder */
	actualFps: number | null;
	nominalFps: number | null;
	/** Frames the source did not deliver and videorate filled with copies */
	repeatedFrames: number;
	/** Frames videorate discarded because the source was faster */
	droppedFrames: number;
	secondsAtMinBitrate: number;
}

export function summarizeQualityCounters(c: QualityCounters): QualitySummary {
	let nominal: number | null = null;
	if (c.encoder.nominal_fps) {
		const [n, d] = c.encoder.nominal_fps.split("/").map(Number);
		if (n !== undefined && d) nominal = n / d;
	}
	return {
		actualFps: c.encoder.fps,
		nominalFps: nominal,
		repeatedFrames: c.videorate.reduce((s, v) => s + v.duplicate, 0),
		droppedFrames: c.videorate.reduce((s, v) => s + v.drop, 0),
		secondsAtMinBitrate: c.bitrate.at_min_ms / 1000,
	};
}
