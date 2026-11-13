// Runs the firmware's motion detector (csi_core.h) over firmware-format lines
// on stdin and prints every score, state change and calibration change, so
// tools/test_pipeline.py can check the C and Python detectors agree.
// Build: make build-host/score_lines
// Thresholds can be overridden from the environment (see main).
#include <stdlib.h>
#include "csi_core.h"

// A threshold from the environment: false if unset; exits on a bad value
static bool env_threshold(const char *name, float *out) {
    const char *s = getenv(name);
    if (!s) return false;
    char *end;
    float v = strtof(s, &end);
    if (end == s || *end != '\0' || !(v > 0.0f && v < 100.0f)) {
        fprintf(stderr, "score_lines: bad %s=\"%s\" (want a number like 1.6)\n", name, s);
        exit(2);
    }
    *out = v;
    return true;
}

int main(void) {
    static motion_det_t det;
    static char line[8192];
    md_cfg_t cfg;
    md_cfg_default(&cfg);
    // Try other thresholds on a recording: MD_ON_LONG=1.6 MD_ON_SHORT=3.0.
    // Each end threshold keeps the default on/off ratio, as the firmware does
    // for SENS and MD_ON_LONG, unless MD_OFF_LONG / MD_OFF_SHORT is given.
    float v;
    if (env_threshold("MD_ON_LONG", &v)) {
        cfg.off_long *= v / cfg.on_long;
        cfg.on_long = v;
    }
    if (env_threshold("MD_ON_SHORT", &v)) {
        cfg.off_short *= v / cfg.on_short;
        cfg.on_short = v;
    }
    if (env_threshold("MD_OFF_LONG", &v)) cfg.off_long = v;
    if (env_threshold("MD_OFF_SHORT", &v)) cfg.off_short = v;
    if (cfg.off_long >= cfg.on_long || cfg.off_short >= cfg.on_short) {
        fprintf(stderr, "score_lines: an end threshold must be below its start threshold\n");
        return 2;
    }
    md_init(&det, &cfg, 0);
    md_take_cal_event(&det);                 // the boot event is implied
    while (fgets(line, sizeof line, stdin)) {
        long long ts;
        int src, kind, ch, rssi, sig, cwb, len, off = 0;
        if (sscanf(line, "CHANNEL,%lld,%d", &ts, &ch) == 2) {
            md_reset(&det, ts);
            continue;
        }
        if (sscanf(line, "CSI,%lld,%d,%d,%d,%d,%d,%d,%d,%n", &ts, &src, &kind, &ch,
                   &rssi, &sig, &cwb, &len, &off) != 8 || len < 128 || len > CSI_MAX_LEN)
            continue;
        int8_t buf[CSI_MAX_LEN];
        char *p = line + off;
        int got = 0;
        for (; got < len; got++) {
            char *e;
            long v = strtol(p, &e, 10);
            if (e == p || v < -128 || v > 127) break;
            buf[got] = (int8_t)v;
            p = e;
        }
        if (got != len) continue;
        if (kind < 0 || kind > 1) continue;  // the Python parser rejects these too
        float s = md_update(&det, src, kind, ts, rssi, buf, len);
        if (s >= 0)
            printf("SCORE %lld %d %d %.6f %.6f\n", ts, src, kind, s,
                   det.s[src * 2 + kind].score_l);
        int change = md_eval(&det, ts);
        if (md_take_cal_event(&det))
            printf("CALIB %lld %s %s\n", ts, md_phase_name(det.phase), md_why_name(det.why));
        if (change) printf("STATE %lld %s\n", ts, change > 0 ? "motion" : "still");
        static float last_floor = 0;
        if (det.floor != last_floor) {       // only changes, to keep output small
            printf("FLOOR %lld %.6f\n", ts, det.floor);
            last_floor = det.floor;
        }
    }
    return 0;
}
