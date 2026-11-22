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
    static br_state_t br;                    // breathing on node 0's ACKs (BREATH lines)
    br_init(&br);
    md_take_cal_event(&det);                 // the boot event is implied
    while (fgets(line, sizeof line, stdin)) {
        long long ts;
        int src, kind, ch, rssi, sig, cwb, len, off = 0;
        if (sscanf(line, "CHANNEL,%lld,%d", &ts, &ch) == 2) {
            md_reset(&det, ts);
            continue;
        }
        if (sscanf(line, "CSI,%lld,%d,%d,%d,%d,%d,%d,%d,%n", &ts, &src, &kind, &ch,
                   &rssi, &sig, &cwb, &len, &off) != 8 || off == 0)
            continue;
        int8_t buf[CSI_MAX_LEN];
        len = parse_csi_data(line + off, len, buf);      // either line format
        if (len < 128) continue;
        if (kind < 0 || kind > 1) continue;  // the Python parser rejects these too
        if (src == 0 && kind == 1) {         // learned features on node 0's ACKs (FEAT lines)
            static ml_state_t ml;
            static bool ml_ready;
            if (!ml_ready) { ml_init(&ml); ml_ready = true; }
            float af[N_SC];
            static ml_model_t mdl;
            static ml_quiet_t qt;
            static ml_vote_t vt;
            if (!mdl.np) ml_model_default(&mdl);
            if (lltf_amplitudes(buf, len, af) && ml_feed(&ml, ts, af, rssi)) {
                printf("FEAT %lld %.5f %.5f %.5f %.5f %.5f %.5f %.5f %.5f\n", (long long)(ml.next_us - 1000000), ml.f[0], ml.f[1],
                       ml.f[2], ml.f[3], ml.f[4], ml.f[5], ml.f[6], ml.f[7]);
                ml_quiet_add(&qt, ml.f);
                float pr[3];
                float pm = ml_forward(&mdl, ml.f, &qt, pr);
                int ch = ml_vote(&vt, pm > 0.5f);
                printf("ML %lld %.5f %d %d\n", (long long)(ml.next_us - 1000000), pm, vt.motion, ch);
            }
        }
        if (src == 0 && kind == 1) {
            float am[N_SC];
            if (lltf_amplitudes(buf, len, am) && br_feed(&br, ts, am))
                printf("BREATH %lld %d %.2f %.4f %.4f\n", ts, br.level, br.rate, br.prom, br.agree);
        }
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
