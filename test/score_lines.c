// Runs the firmware's motion detector (csi_core.h) over firmware-format lines
// on stdin and prints every score, state change and calibration change, so
// tools/test_pipeline.py can check the C and Python detectors agree.
// Build: make build-host/score_lines
// Thresholds can be overridden from the environment (see main).
#include <stdlib.h>
#include "csi_core.h"

int main(void) {
    static motion_det_t det;
    static char line[8192];
    md_cfg_t cfg;
    md_cfg_default(&cfg);
    // Try other thresholds on a recording: MD_ON_LONG=1.6 MD_OFF_LONG=1.3 ...
    const char *e;
    if ((e = getenv("MD_ON_LONG"))) cfg.on_long = strtof(e, NULL);
    if ((e = getenv("MD_OFF_LONG"))) cfg.off_long = strtof(e, NULL);
    if ((e = getenv("MD_ON_SHORT"))) cfg.on_short = strtof(e, NULL);
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
