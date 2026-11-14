import { describe, it, expect } from "bun:test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { parseQualityCounters, readQualityCounters, summarizeQualityCounters } from "./quality.js";

// Shape as written by ceracoder (src/core/quality_counters.c).
const SAMPLE = {
	version: 1,
	time_ms: 1794700000000,
	uptime_ms: 9005,
	pattern_epoch_ms: null,
	encoder: { frames: 269, nominal_fps: "30/1", fps: 25.0 },
	videorate: [{ name: "videorate0", in: 225, out: 270, duplicate: 45, drop: 0 }],
	input_caps: "video/x-raw, width=(int)1920, height=(int)1080, framerate=(fraction)25/1",
	bitrate: { current_kbps: 500, min_kbps: 500, max_kbps: 6000, at_min_ms: 4200 },
	sender_latency_ms: { last: 86.8, avg: 86.8, max: 88.2 },
	srt: {
		rtt_ms: 61.2,
		send_buffer_ms: 23,
		latency_ms: 2000,
		pkt_snd_drop_total: 0,
		pkt_snd_loss_total: 3,
		pkt_retrans_total: 3,
	},
};

describe("quality counters", () => {
	it("parses a snapshot and summarizes it", () => {
		const c = parseQualityCounters(JSON.stringify(SAMPLE));
		expect(c).toBeDefined();
		const s = summarizeQualityCounters(c!);
		expect(s.nominalFps).toBe(30);
		expect(s.actualFps).toBe(25);
		expect(s.repeatedFrames).toBe(45);
		expect(s.secondsAtMinBitrate).toBeCloseTo(4.2);
	});

	it("rejects malformed or foreign JSON", () => {
		expect(parseQualityCounters("{")).toBeUndefined();
		expect(parseQualityCounters(JSON.stringify({ ...SAMPLE, version: 2 }))).toBeUndefined();
	});

	it("treats a stale or missing file as no data", () => {
		const p = path.join(os.tmpdir(), `ceracoder_quality_${Date.now()}_${Math.random()}.json`);
		expect(readQualityCounters(p)).toBeUndefined();
		fs.writeFileSync(p, JSON.stringify(SAMPLE));
		expect(readQualityCounters(p, { now: SAMPLE.time_ms + 1000 })).toBeDefined();
		expect(readQualityCounters(p, { now: SAMPLE.time_ms + 60000 })).toBeUndefined();
		fs.unlinkSync(p);
	});
});
