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
/*
 * Unit tests for the quality counters: time at the minimum bitrate, the
 * sender latency window, the pattern clock bits and the JSON snapshot.
 */

#include <stdarg.h>
#include <stddef.h>
#include <setjmp.h>
#include <string.h>
#include <cmocka.h>

#include "quality_counters.h"

static void test_at_min_accumulates_only_at_min(void **state) {
    (void)state;
    QualityCounters qc;
    quality_counters_init(&qc, 1000);
    quality_bitrate_sample(&qc, 1000, 500000, 500000);  /* at min from 1000 */
    quality_bitrate_sample(&qc, 1500, 2000000, 500000); /* 500 ms at min */
    quality_bitrate_sample(&qc, 2500, 500000, 500000);  /* 1000 ms above */
    quality_bitrate_sample(&qc, 2520, 500000, 500000);  /* 20 ms at min */
    assert_int_equal(qc.at_min_ms, 520);
}

static void test_at_min_first_sample_counts_nothing(void **state) {
    (void)state;
    QualityCounters qc;
    quality_counters_init(&qc, 0);
    quality_bitrate_sample(&qc, 5000, 300000, 500000);
    assert_int_equal(qc.at_min_ms, 0);
    quality_bitrate_sample(&qc, 5100, 300000, 500000); /* below min counts too */
    assert_int_equal(qc.at_min_ms, 100);
}

static void test_latency_window(void **state) {
    (void)state;
    QualityCounters qc;
    double last, avg, max;
    quality_counters_init(&qc, 0);
    assert_int_equal(quality_latency_take(&qc, &last, &avg, &max), 0);
    quality_latency_record(&qc, 10000);
    quality_latency_record(&qc, 30000);
    quality_latency_record(&qc, 20000);
    assert_int_equal(quality_latency_take(&qc, &last, &avg, &max), 1);
    assert_true(last > 19.9 && last < 20.1);
    assert_true(avg > 19.9 && avg < 20.1);
    assert_true(max > 29.9 && max < 30.1);
    /* window resets */
    assert_int_equal(quality_latency_take(&qc, &last, &avg, &max), 0);
}

static void test_encoder_frames(void **state) {
    (void)state;
    QualityCounters qc;
    quality_counters_init(&qc, 0);
    for (int i = 0; i < 42; i++) quality_encoder_frame(&qc);
    assert_int_equal(quality_encoder_frames(&qc), 42);
}

static void test_epoch_bits_round_trip(void **state) {
    (void)state;
    uint64_t epoch = 1794700000123ULL; /* fits in 42 bits */
    uint64_t back = 0;
    for (int k = 0; k < 42; k++) back |= (uint64_t)quality_epoch_bit(epoch, k) << k;
    assert_true(back == epoch);
    assert_int_equal(quality_epoch_bit(epoch, 64), 0);
    assert_int_equal(quality_epoch_bit(epoch, -1), 0);
}

static void test_snapshot_json(void **state) {
    (void)state;
    QualitySnapshot s;
    memset(&s, 0, sizeof(s));
    s.time_ms = 1794700000123ULL;
    s.uptime_ms = 5000;
    s.encoder_frames = 150;
    s.nominal_fps_n = 30;
    s.nominal_fps_d = 1;
    s.fps_measured = 25.0;
    s.n_videorate = 1;
    strcpy(s.videorate[0].name, "videorate0");
    s.videorate[0].in = 125;
    s.videorate[0].out = 150;
    s.videorate[0].duplicate = 25;
    strcpy(s.input_caps, "video/x-raw, format=(string)\"NV12\"");
    s.bitrate_bps = 500000;
    s.min_bitrate_bps = 500000;
    s.max_bitrate_bps = 6000000;
    s.at_min_ms = 1200;
    char buf[2048];
    int n = quality_snapshot_json(&s, buf, sizeof(buf));
    assert_true(n > 0);
    assert_non_null(strstr(buf, "\"pattern_epoch_ms\":null"));
    assert_non_null(strstr(buf, "\"nominal_fps\":\"30/1\""));
    assert_non_null(strstr(buf, "\"fps\":25.00"));
    assert_non_null(strstr(buf, "\"duplicate\":25"));
    assert_non_null(strstr(buf, "format=(string)\\\"NV12\\\""));
    assert_non_null(strstr(buf, "\"at_min_ms\":1200"));
    assert_non_null(strstr(buf, "\"sender_latency_ms\":null"));
    assert_non_null(strstr(buf, "\"srt\":null}"));
    /* too small a buffer is reported, not truncated */
    assert_int_equal(quality_snapshot_json(&s, buf, 40), -1);
}

int main(void) {
    const struct CMUnitTest tests[] = {
        cmocka_unit_test(test_at_min_accumulates_only_at_min),
        cmocka_unit_test(test_at_min_first_sample_counts_nothing),
        cmocka_unit_test(test_latency_window),
        cmocka_unit_test(test_encoder_frames),
        cmocka_unit_test(test_epoch_bits_round_trip),
        cmocka_unit_test(test_snapshot_json),
    };
    return cmocka_run_group_tests(tests, NULL, NULL);
}
