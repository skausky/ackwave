// csi_core.h: pure logic shared by firmware and host tests (no ESP-IDF deps)
#pragma once
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <inttypes.h>
#include <math.h>

#define CSI_MAX_LEN 384
#define MAX_APS     8                     // APs learned from beacons: nodes 0..7
#define MAX_DEVS    4                     // devices you picked to poke: nodes 8..11
#define MAX_NODES   (MAX_APS + MAX_DEVS)
#define DEV_NODE(k) (MAX_APS + (k))

// ---------- Beacon parsing ----------
typedef struct {
    bool    ok;          // frame is a well-formed beacon header
    bool    ssid_match;
    uint8_t bssid[6];
    int     ds_ch;       // channel from DS Parameter Set IE, 0 if absent
    int     csa_ch;      // new channel from CSA IE, 0 if absent
    int     csa_cnt;     // beacons until switch
} beacon_info_t;

// f = 802.11 frame WITHOUT FCS, len = its length in bytes
static inline void parse_beacon(const uint8_t *f, int len, const char *ssid,
                                beacon_info_t *out) {
    memset(out, 0, sizeof(*out));
    if (!f || len < 36 || f[0] != 0x80) return;   // 24 hdr + 12 fixed fields
    out->ok = true;
    memcpy(out->bssid, f + 16, 6);                 // addr3

    size_t want = strlen(ssid);
    const uint8_t *ie = f + 36, *end = f + len;
    while (end - ie >= 2 && (end - ie) - 2 >= ie[1]) {
        uint8_t id = ie[0], l = ie[1];
        const uint8_t *v = ie + 2;
        if (id == 0)
            out->ssid_match = want > 0 && l == want && memcmp(v, ssid, l) == 0;
        else if (id == 3 && l >= 1)
            out->ds_ch = v[0];
        else if (id == 37 && l >= 3) {             // mode, new_ch, count
            out->csa_ch = v[1];
            out->csa_cnt = v[2];
        }
        ie += 2 + l;
    }
}

static inline bool csa_should_switch(int new_ch, int cur_ch, int max_ch) {
    return new_ch >= 1 && new_ch <= max_ch && new_ch != cur_ch;
}

// ---------- Wrap-safe 32-bit millisecond timing ----------
static inline uint32_t elapsed_ms(uint32_t now, uint32_t then) {
    return now - then;
}
static inline bool deadline_passed(uint32_t now, uint32_t at) {
    return (int32_t)(now - at) >= 0;
}
// True if more than `limit` has passed since `then`. A `then` slightly in the
// future (another task stamped it after we read `now`) counts as fresh, not as
// ~49 days ago.
static inline bool older_than(uint32_t now, uint32_t then, uint32_t limit) {
    int32_t d = (int32_t)(now - then);
    return d > 0 && (uint32_t)d > limit;
}

// ---------- Node table (single writer, many readers) ----------
// APs fill slots 0..n-1 (Wi-Fi task is the only writer). Device slots
// MAX_APS.. are set and cleared by the control task; dev_mask says which
// are live, and a slot's MAC is only rewritten while its bit is clear.
typedef struct {
    uint8_t mac[MAX_NODES][6];
    int n;
    int dev_mask;
} node_table_t;

static inline int node_count(const node_table_t *t) {
    return __atomic_load_n(&t->n, __ATOMIC_ACQUIRE);
}
static inline bool node_active(const node_table_t *t, int i) {
    if (i < 0 || i >= MAX_NODES) return false;
    if (i < MAX_APS) return i < node_count(t);
    return (__atomic_load_n(&t->dev_mask, __ATOMIC_ACQUIRE) >> (i - MAX_APS)) & 1;
}
static inline int node_find(const node_table_t *t, const uint8_t *mac) {
    if (!mac) return -1;
    for (int i = 0; i < MAX_NODES; i++)
        if (node_active(t, i) && memcmp(t->mac[i], mac, 6) == 0) return i;
    return -1;
}
static inline bool node_is_ap(int i) { return i >= 0 && i < MAX_APS; }
static inline int node_add(node_table_t *t, const uint8_t *mac) {
    int i = node_find(t, mac);
    if (i >= 0) return i;
    if (t->n >= MAX_APS) return -1;
    memcpy(t->mac[t->n], mac, 6);
    __atomic_store_n(&t->n, t->n + 1, __ATOMIC_RELEASE);  // publish after copy
    return t->n - 1;
}
// Device slot k: put `mac` in (slot must be clear) / take it out
static inline void dev_set(node_table_t *t, int k, const uint8_t *mac) {
    if (k < 0 || k >= MAX_DEVS) return;
    memcpy(t->mac[DEV_NODE(k)], mac, 6);
    __atomic_or_fetch(&t->dev_mask, 1 << k, __ATOMIC_RELEASE);
}
static inline void dev_clear(node_table_t *t, int k) {
    if (k >= 0 && k < MAX_DEVS) __atomic_and_fetch(&t->dev_mask, ~(1 << k), __ATOMIC_RELEASE);
}
// Slot holding `mac`, else a free slot (*is_new = true), else -1
static inline int dev_slot_for(const node_table_t *t, const uint8_t *mac, bool *is_new) {
    int free_k = -1;
    for (int k = 0; k < MAX_DEVS; k++) {
        if (node_active(t, DEV_NODE(k))) {
            if (memcmp(t->mac[DEV_NODE(k)], mac, 6) == 0) { *is_new = false; return k; }
        } else if (free_k < 0) {
            free_k = k;
        }
    }
    *is_new = true;
    return free_k;
}

// ---------- Devices on our network (listen only) ----------
// Every data frame whose BSSID is one of our APs names a client device. We
// note it with the signal strength of frames it sent itself (the AP's
// frames to it say nothing about where it is). Neighbors' networks never
// match a BSSID of ours, so only our own devices are listed.
#define MAX_SEEN 32

typedef struct {
    uint8_t  mac[6];
    int      rssi_x16;          // average of frames it sent, x16; 0 = none yet
    uint32_t n_tx, n_rx;        // frames it sent / frames sent to it
    uint32_t last_ms;
    bool     used;
} seen_dev_t;

typedef struct { seen_dev_t d[MAX_SEEN]; } seen_list_t;

// f = 802.11 frame without FCS. Returns the entry touched, or -1.
static inline int seen_note(seen_list_t *l, const node_table_t *t, const uint8_t *f,
                            int len, int rssi, uint32_t now_ms) {
    if (!f || len < 24 || (f[0] & 0x0C) != 0x08) return -1;     // data frames only
    int ds = f[1] & 0x03;                                        // ToDS | FromDS<<1
    const uint8_t *bssid, *dev;
    bool sent_by_dev;
    if (ds == 1)      { bssid = f + 4;  dev = f + 10; sent_by_dev = true;  }  // to AP
    else if (ds == 2) { bssid = f + 10; dev = f + 4;  sent_by_dev = false; }  // from AP
    else return -1;                                     // ad-hoc / mesh backhaul
    if (dev[0] & 0x01) return -1;                       // group address
    if (!node_is_ap(node_find(t, bssid))) return -1;    // not our network
    if (node_is_ap(node_find(t, dev))) return -1;       // an AP of ours, not a device
    int k = -1;
    for (int i = 0; i < MAX_SEEN && k < 0; i++)         // already listed?
        if (l->d[i].used && memcmp(l->d[i].mac, dev, 6) == 0) k = i;
    for (int i = 0; i < MAX_SEEN && k < 0; i++)         // free entry?
        if (!l->d[i].used) k = i;
    if (k < 0) {                                        // full: evict the stalest
        k = 0;
        for (int i = 1; i < MAX_SEEN; i++)
            if ((int32_t)(l->d[k].last_ms - l->d[i].last_ms) > 0) k = i;
    }
    seen_dev_t *e = &l->d[k];
    if (!e->used || memcmp(e->mac, dev, 6) != 0) {
        memset(e, 0, sizeof *e);
        memcpy(e->mac, dev, 6);
        e->used = true;
    }
    if (sent_by_dev) {
        e->n_tx++;
        int x = e->rssi_x16;
        x = x ? x + (rssi * 16 - x) / 8 : rssi * 16;
        e->rssi_x16 = x ? x : -1;
    } else {
        e->n_rx++;
    }
    e->last_ms = now_ms;
    return k;
}

// Commands from the PC, one per line:
//   POKE,<mac>     start poking a device of ours (mac as 6 hex pairs with ':')
//   UNPOKE,<mac>   stop
//   UNPOKE,all
//   CAL            recalibrate, like the button
//   BEEP[,<ms>[,<hz>[,<vol>]]]  test the buzzer (parse_beep)
//   GLIDE,<ms>,<hz>,<hz2>[,<vol>]  a pitch glide (parse_glide)
enum { CMD_NONE = 0, CMD_POKE, CMD_UNPOKE, CMD_UNPOKE_ALL, CMD_CAL };

static inline bool parse_mac(const char *s, uint8_t out[6]) {
    for (int i = 0; i < 6; i++) {
        int v = 0;
        for (int j = 0; j < 2; j++) {
            char c = *s++;
            int h = c >= '0' && c <= '9' ? c - '0' : c >= 'a' && c <= 'f' ? c - 'a' + 10
                  : c >= 'A' && c <= 'F' ? c - 'A' + 10 : -1;
            if (h < 0) return false;
            v = v * 16 + h;
        }
        out[i] = (uint8_t)v;
        if (i < 5 && *s++ != ':') return false;
    }
    return *s == '\0' || *s == '\r' || *s == '\n';
}

static inline int parse_cmd(const char *line, uint8_t mac[6]) {
    if (!line) return CMD_NONE;
    if (strncmp(line, "CAL", 3) == 0 && (line[3] == '\0' || line[3] == '\r' || line[3] == '\n'))
        return CMD_CAL;
    if (strncmp(line, "UNPOKE,all", 10) == 0 &&
        (line[10] == '\0' || line[10] == '\r' || line[10] == '\n')) return CMD_UNPOKE_ALL;
    if (strncmp(line, "UNPOKE,", 7) == 0) return parse_mac(line + 7, mac) ? CMD_UNPOKE : CMD_NONE;
    if (strncmp(line, "POKE,", 5) == 0) {
        if (!parse_mac(line + 5, mac) || (mac[0] & 0x01)) return CMD_NONE;  // no group MACs
        return CMD_POKE;
    }
    return CMD_NONE;
}

// BEEP, BEEP,<ms>, BEEP,<ms>,<hz> or BEEP,<ms>,<hz>,<vol>: returns the beep
// length in ms (1-3000, default 300) and sets *hz (100-10000) and *vol
// (1-100 %), 0 = the default, or returns -1 if the line isn't a valid beep command
static inline int parse_beep(const char *line, int *hz, int *vol) {
    *hz = *vol = 0;
    if (!line || strncmp(line, "BEEP", 4) != 0) return -1;
    const char *s = line + 4;
    if (*s == '\0' || *s == '\r' || *s == '\n') return 300;
    int v[3] = {0, 0, 0}, k = 0;
    while (*s == ',' && k < 3) {
        s++;
        int digits = 0;
        while (*s >= '0' && *s <= '9' && digits < 6) { v[k] = v[k] * 10 + (*s++ - '0'); digits++; }
        if (!digits) return -1;
        k++;
    }
    if (!k || !(*s == '\0' || *s == '\r' || *s == '\n')) return -1;
    if (v[0] < 1 || v[0] > 3000) return -1;
    if (k >= 2 && (v[1] < 100 || v[1] > 10000)) return -1;
    if (k == 3 && (v[2] < 1 || v[2] > 100)) return -1;
    *hz = k >= 2 ? v[1] : 0;
    *vol = k == 3 ? v[2] : 0;
    return v[0];
}

// GLIDE,<ms>,<hz>,<hz2>[,<vol>]: returns ms (1-3000) and sets the start and
// end pitch (100-10000 Hz) and *vol (1-100 %, 0 = default), or returns -1
static inline int parse_glide(const char *line, int *hz, int *hz2, int *vol) {
    *hz = *hz2 = *vol = 0;
    if (!line || strncmp(line, "GLIDE,", 6) != 0) return -1;
    const char *s = line + 5;
    int v[4] = {0, 0, 0, 0}, k = 0;
    while (*s == ',' && k < 4) {
        s++;
        int digits = 0;
        while (*s >= '0' && *s <= '9' && digits < 6) { v[k] = v[k] * 10 + (*s++ - '0'); digits++; }
        if (!digits) return -1;
        k++;
    }
    if (k < 3 || !(*s == '\0' || *s == '\r' || *s == '\n')) return -1;
    if (v[0] < 1 || v[0] > 3000) return -1;
    if (v[1] < 100 || v[1] > 10000 || v[2] < 100 || v[2] > 10000) return -1;
    if (k == 4 && (v[3] < 1 || v[3] > 100)) return -1;
    *hz = v[1]; *hz2 = v[2]; *vol = k == 4 ? v[3] : 0;
    return v[0];
}

// True if the MAC is locally administered: phones' private / random addresses
static inline bool mac_is_random(const uint8_t *m) { return (m[0] & 0x02) != 0; }

// ---------- "Polite Wi-Fi" poking ----------
// Every 802.11 device ACKs a unicast frame addressed to it before checking
// whether the sender is associated. We send a 24-byte Null data frame to the
// AP; its ACK gives us an extra CSI sample on demand.
//
// The Null frame's sender (TA) is a made-up address, not our own MAC: our
// radio consumes ACKs addressed to itself and never reports CSI for them, but
// an ACK to an address it doesn't own is reported like any passing frame. The
// ACK has no sender address, only its receiver (dmac), so a unique made-up TA
// per AP identifies both "this is our ACK" and "from which AP" exactly.

#define NULL_FRAME_LEN 24

// Null function data frame, ToDS=1: addr1 = AP (RA), addr2 = us (TA), addr3 = BSSID
static inline void build_null_frame(uint8_t out[NULL_FRAME_LEN],
                                    const uint8_t bssid[6], const uint8_t me[6]) {
    memset(out, 0, NULL_FRAME_LEN);
    out[0] = 0x48;                      // type data (2), subtype null (4)
    out[1] = 0x01;                      // ToDS
    memcpy(out + 4, bssid, 6);
    memcpy(out + 10, me, 6);
    memcpy(out + 16, bssid, 6);
}

// Made-up poke TA for node n, derived from our MAC so nearby boards differ:
// locally-administered bit flipped (so it never equals `me`), unicast,
// node index added to the last byte.
static inline void poke_ta(const uint8_t me[6], int node, uint8_t out[6]) {
    memcpy(out, me, 6);
    out[0] = (uint8_t)((me[0] ^ 0x02) & 0xFE);
    out[5] = (uint8_t)(me[5] + node);
}
// Node index whose poke TA is `dmac`, or -1
static inline int poke_ta_node(const uint8_t me[6], const uint8_t *dmac) {
    uint8_t base[6];
    poke_ta(me, 0, base);
    if (!dmac || memcmp(dmac, base, 5) != 0) return -1;
    int n = (uint8_t)(dmac[5] - base[5]);
    return n < MAX_NODES ? n : -1;
}

// True if f (no FCS) is an ACK control frame addressed to `me`
static inline bool is_ack_to(const uint8_t *f, int len, const uint8_t me[6]) {
    return f && len >= 10 && f[0] == 0xD4 && memcmp(f + 4, me, 6) == 0;
}

enum { CSI_REPLY = -2, CSI_DROP = -1, CSI_KIND_AP = 0, CSI_KIND_ACK = 1 };

typedef struct {
    int      node;          // node index the ACK is expected from, -1 = none
    uint32_t sent_us;       // when the poke was handed to the driver
} poke_state_t;

// Decide what a CSI callback is. mac/dmac = sender/receiver the driver
// reports, me = our MAC, now_us = callback time. An ACK must be addressed to
// the poke TA of the node we're waiting on, non-HT, and inside the window
// (a sanity check only; dmac already identifies it). Consumes the poke.
static inline int classify_csi(const node_table_t *t, poke_state_t *pk,
                               const uint8_t *mac, const uint8_t *dmac,
                               const uint8_t me[6], int sig_mode, int len,
                               uint32_t now_us, uint32_t win_us, int *node_out) {
    int idx = node_find(t, mac);
    if (idx >= 0) {                                 // frame sent by a known node
        *node_out = idx;
        // ...but addressed to one of our made-up poke addresses: the node
        // answering a poke (some routers send a deauth, retried ~6 times
        // since nobody ACKs it). Same path and moment as the ACK, so it
        // adds nothing; count it, don't stream it.
        if (poke_ta_node(me, dmac) >= 0) return CSI_REPLY;
        return CSI_KIND_AP;
    }
    int want = pk->node;
    int32_t dt = (int32_t)(now_us - pk->sent_us);   // wrap-safe
    if (want >= 0 && poke_ta_node(me, dmac) == want && sig_mode == 0 && len >= 128 &&
        dt >= 0 && (uint32_t)dt <= win_us) {
        pk->node = -1;                              // at most one ACK per poke
        *node_out = want;
        return CSI_KIND_ACK;
    }
    return CSI_DROP;
}

// ---------- Poke gating ----------
// Pokes are split evenly over the APs in the rotation, so a far AP that
// rarely answers costs the good ones ACKs (seen: learning a -82 dBm second AP
// dropped the router from 97 to 43 ACK/s). An AP leaves the rotation when its
// beacons are weak or it answers too few pokes. A silent AP still gets one
// trial poke in POKE_TRIAL_EVERY so it can earn its way back; a weak one
// rejoins when its beacons get stronger. If every AP on the channel is out,
// the strongest is poked anyway, so a single far router behaves as before.
#define POKE_MIN_RSSI    (-80)   // dBm, beacon average; below = weak
#define POKE_RSSI_HYST   3       // weak until it climbs back to MIN + HYST
#define POKE_MIN_YIELD   20      // % of pokes answered; below = silent
#define POKE_EVAL_N      50      // pokes per yield verdict
#define POKE_TRIAL_EVERY 50      // 1 in N pokes goes to a silent AP
#define POKE_FAR_DB      15      // an AP this much weaker than the strongest is skipped

enum { GATE_OK = 0, GATE_WEAK = 1, GATE_SILENT = 2 };

// Every field has one writer (noted), all ints, all on the Wi-Fi core.
typedef struct {
    int  rssi_x16[MAX_NODES];   // beacon RSSI average x16, 0 = none yet (beacon cb)
    bool weak[MAX_NODES];       // RSSI verdict with hysteresis (beacon cb)
    int  acked[MAX_NODES];      // ACKs matched, running total (CSI cb)
    int  sent[MAX_NODES];       // pokes sent, running total (poke task)
    int  base_sent[MAX_NODES], base_acked[MAX_NODES];   // window start (poke task)
    int  yield[MAX_NODES];      // last verdict in %, -1 = not judged yet (poke task)
    int  since_trial[2];        // pokes since the last trial, APs / devices (poke task)
    int  trial_rr[2];           // (poke task)
} poke_gate_t;

static inline void gate_init(poke_gate_t *g) {
    memset(g, 0, sizeof *g);
    for (int i = 0; i < MAX_NODES; i++) g->yield[i] = -1;
}

// Forget everything about slot i (only while nothing else touches it)
static inline void gate_reset(poke_gate_t *g, int i) {
    if (i < 0 || i >= MAX_NODES) return;
    g->rssi_x16[i] = 0; g->weak[i] = false;
    g->base_sent[i] = g->sent[i]; g->base_acked[i] = g->acked[i];
    g->yield[i] = -1;
}

// Beacon (or, for a device, any frame it sent) from node i heard at `rssi` dBm
static inline void gate_rssi(poke_gate_t *g, int i, int rssi) {
    if (i < 0 || i >= MAX_NODES) return;
    int x = g->rssi_x16[i];
    x = x ? x + (rssi * 16 - x) / 8 : rssi * 16;      // ~8-beacon average
    if (x == 0) x = -1;                               // keep 0 meaning "none"
    g->rssi_x16[i] = x;
    if (x < POKE_MIN_RSSI * 16) g->weak[i] = true;
    else if (x >= (POKE_MIN_RSSI + POKE_RSSI_HYST) * 16) g->weak[i] = false;
}

static inline void gate_ack(poke_gate_t *g, int i) {
    if (i >= 0 && i < MAX_NODES) g->acked[i]++;
}

// Poke to node i went out. A full window of POKE_EVAL_N pokes is judged when
// the next one goes, so the last poke's ACK has had time to arrive.
static inline void gate_sent(poke_gate_t *g, int i) {
    if (i < 0 || i >= MAX_NODES) return;
    int s = g->sent[i] - g->base_sent[i];
    if (s >= POKE_EVAL_N) {
        int a = g->acked[i] - g->base_acked[i];
        g->yield[i] = a >= s ? 100 : a * 100 / s;
        g->base_sent[i] = g->sent[i];
        g->base_acked[i] = g->acked[i];
    }
    g->sent[i]++;
}

static inline int gate_state(const poke_gate_t *g, int i) {
    if (g->weak[i]) return GATE_WEAK;
    if (g->yield[i] >= 0 && g->yield[i] < POKE_MIN_YIELD) return GATE_SILENT;
    return GATE_OK;
}
static inline const char *gate_name(int s) {
    return s == GATE_WEAK ? "weak" : s == GATE_SILENT ? "silent" : "ok";
}

// Strongest beacon RSSI (x16) among the usable APs on `ch`, 0 if none known.
// An AP far weaker than that one is "far": it splits the router's pokes but
// its link covers little, so it is skipped (it rejoins if it gets closer).
static inline int ap_best_rssi_x16(const node_table_t *t, const int *node_ch, int ch,
                                   const poke_gate_t *g) {
    int best = 0;
    for (int i = 0; i < MAX_APS; i++)
        if (node_active(t, i) && node_ch[i] == ch && gate_state(g, i) == GATE_OK &&
            g->rssi_x16[i] && (!best || g->rssi_x16[i] > best))
            best = g->rssi_x16[i];
    return best;
}
static inline bool ap_is_far(const poke_gate_t *g, int i, int best_x16) {
    return i < MAX_APS && best_x16 && g->rssi_x16[i] &&
           g->rssi_x16[i] < best_x16 - POKE_FAR_DB * 16;
}

// Next node to poke among those heard on `ch`, or -1. devs = 0 picks among
// APs, 1 among devices: the poke task alternates, so devices never take
// pokes from the router. Round-robin over the OK ones, with the trial rule
// above; if every AP is out the strongest is poked anyway (devices: none).
static inline int next_poke_target(const node_table_t *t, const int *node_ch,
                                   int ch, int *rr, poke_gate_t *g, int devs) {
    const int n = MAX_NODES;
    devs = devs ? 1 : 0;
#define NPT_MINE(i) (node_active(t, (i)) && node_is_ap(i) != devs && node_ch[(i)] == ch)
    if (++g->since_trial[devs] >= POKE_TRIAL_EVERY) {
        g->since_trial[devs] = 0;
        for (int k = 0; k < n; k++) {
            int i = (g->trial_rr[devs] + k) % n;
            if (NPT_MINE(i) && gate_state(g, i) == GATE_SILENT) {
                g->trial_rr[devs] = (i + 1) % n;
                return i;
            }
        }
    }
    int best_ap = devs ? 0 : ap_best_rssi_x16(t, node_ch, ch, g);
    for (int k = 0; k < n; k++) {
        int i = (*rr + k) % n;
        if (NPT_MINE(i) && gate_state(g, i) == GATE_OK && !ap_is_far(g, i, best_ap)) {
            *rr = (i + 1) % n;
            return i;
        }
    }
    int best = -1, bx = 0;                            // all APs out: strongest one
    for (int i = 0; i < n && !devs; i++) {
        if (!NPT_MINE(i)) continue;
        int x = g->rssi_x16[i] ? g->rssi_x16[i] : -1000 * 16;
        if (best < 0 || x > bx) { best = i; bx = x; }
    }
#undef NPT_MINE
    return best;
}

// ---------- CSI packet + serial line format ----------
typedef struct {
    int64_t  ts;
    uint8_t  src, kind, ch, sig_mode, cwb;
    int8_t   rssi;
    uint16_t len;
    int8_t   buf[CSI_MAX_LEN];
} csi_pkt_t;

// "CSI,ts,src,kind,ch,rssi,sig_mode,cwb,len,b0 b1 ...\n"   kind: 0 = AP frame, 1 = ACK
// Returns chars written (excluding NUL), or -1 if it doesn't fit.
static inline int format_csi_line(char *out, size_t cap, const csi_pkt_t *p) {
    int n = snprintf(out, cap, "CSI,%" PRId64 ",%u,%u,%u,%d,%u,%u,%u,",
                     p->ts, (unsigned)p->src, (unsigned)p->kind, (unsigned)p->ch,
                     (int)p->rssi, (unsigned)p->sig_mode, (unsigned)p->cwb,
                     (unsigned)p->len);
    if (n < 0 || (size_t)n >= cap) return -1;
    for (int i = 0; i < p->len; i++) {
        int k = snprintf(out + n, cap - n, i ? " %d" : "%d", (int)p->buf[i]);
        if (k < 0 || (size_t)k >= cap - n) return -1;
        n += k;
    }
    if ((size_t)n + 2 > cap) return -1;
    out[n++] = '\n';
    out[n] = '\0';
    return n;
}

// ---------- On-device motion detector ----------
// Mirrors MotionDetector in tools/analyze.py (a test checks they agree).
// Per stream (node x kind), per packet:
//  1. LLTF amplitudes minus DC/guard subcarriers -> 52 values, divided by
//     their mean (cancels AGC gain jumps)
//  2. band-pass each subcarrier to ~0.3-5 Hz (difference of a fast and a
//     slow time-based average): keeps human motion, drops static multipath
//     and most of the fast measurement noise
//  3. energy of that signal over a short (0.5 s) and a long (4 s) window
//  4. score = sqrt(mean energy of the 8 most active subcarriers), so a
//     movement that only touches a few subcarriers isn't averaged away
// Short window = quick clear movement, long window = weak sustained movement.
// Thresholds are multiples of the room's quiet level ("floor"), which the
// detector measures itself and keeps re-measuring (see md_eval).
// Everything is time-based, so 10 Hz beacons and 100 Hz ACKs score alike.

#define N_SC        52
#define MAX_STREAMS (MAX_NODES * 2)
#define MD_TOP_K    8
#define MD_RING     1200            // floor samples kept: 120 s at 10 Hz

// buf = CSI bytes as [imag, real] pairs; uses the first 64 subcarriers (LLTF)
static inline bool lltf_amplitudes(const int8_t *buf, int len, float out[N_SC]) {
    if (!buf || len < 128) return false;
    int j = 0;
    for (int k = 1; k < 64; k++) {
        if (k >= 27 && k <= 37) continue;
        float im = buf[2 * k], re = buf[2 * k + 1];
        out[j++] = sqrtf(re * re + im * im);
    }
    return true;
}


typedef struct {
    float   tau_lp, tau_hp;         // band-pass time constants (s)
    float   tau_short, tau_long;    // energy windows (s)
    float   warm_s;                 // stream age before it may score (s)
    int     min_pkts;               // ...and packet count
    float   stale_s;                // a stream silent this long is ignored (s)
    int64_t settle_us;              // ignore data this long after a channel change
    float   on_short, off_short;    // thresholds, multiples of the floor
    float   on_long, off_long;
    float   short_gate;             // short window starts motion only if long > this x floor (0 = off)
    float   long_cap;               // long window input cap, multiple of the floor
    int64_t sample_us;              // floor sample period
    int64_t floor_every_us;         // floor update period
    int     floor_min_n;            // samples before the first floor
    int     floor_pct;              // floor = this percentile of short scores
    float   floor_up_per_s;         // max relative rise per second once tracking
    float   floor_lo, floor_hi;     // floor clamps
    float   rssi_tau_fast, rssi_tau_slow;   // s
    float   rssi_step_db;           // lasting RSSI change that means "board moved"
    int64_t rssi_min_age_us;        // stream must be tracked this long first
    float   glitch_shape;           // a packet whose shape differs from the recent
                                    // ones by more than this is ignored...
    int64_t glitch_hold_us;         // ...unless the new shape lasts this long
    float   tau_ref;                // shape reference average (s)
    // Router background scans: the stream goes quiet in gaps (the router is on
    // another channel) and comes back with a milder shape switch. Right after
    // such a gap, and while that switch lasts, the stricter limit applies.
    float   scan_shape;             // glitch limit while a scan is suspected
    int64_t scan_gap_us;            // a gap at least this long...
    float   scan_gap_ratio;         // ...and this many typical intervals means a scan
    int64_t scan_hold_us;           // stricter limit for this long after it
} md_cfg_t;

static inline void md_cfg_default(md_cfg_t *c) {
    c->tau_lp = 0.032f;  c->tau_hp = 0.5f;
    c->tau_short = 0.5f; c->tau_long = 4.0f;
    c->warm_s = 2.0f;    c->min_pkts = 8;  c->stale_s = 2.0f;
    c->settle_us = 1000000;
    c->on_short = 2.8f;  c->off_short = 2.0f;
    c->on_long = 1.4f;   c->off_long = 1.2f;
    c->short_gate = 1.2f;
    c->long_cap = 3.0f;
    c->sample_us = 100000;  c->floor_every_us = 1000000;
    c->floor_min_n = 100;   c->floor_pct = 10;
    c->floor_up_per_s = 0.01f;
    c->floor_lo = 0.005f;   c->floor_hi = 0.2f;
    c->rssi_tau_fast = 5.0f; c->rssi_tau_slow = 60.0f;
    c->rssi_step_db = 4.0f;  c->rssi_min_age_us = 10000000;
    c->glitch_shape = 0.4f;  c->glitch_hold_us = 5000000;  c->tau_ref = 1.0f;
    c->scan_shape = 0.2f;    c->scan_gap_us = 100000;  c->scan_gap_ratio = 4.0f;
    c->scan_hold_us = 2000000;
}

typedef struct {
    uint32_t n;
    uint32_t n_glitch;                      // packets ignored as glitches
    int64_t  t_start, t_last, rssi_t0;      // microseconds; rssi_t0 = RSSI baseline start
    int64_t  t_heard;                       // last packet, including ignored glitches
    int64_t  scan_until;                    // stricter glitch limit until then
    int64_t  scan_gap_at;                   // when the last scan-like gap ended
    float    iv_us;                         // typical packet interval
    float    score_s, score_l;              // last scores, < 0 = not ready
    float    rssi_f, rssi_s;                // fast / slow RSSI averages
    float    lp[N_SC], hp[N_SC], es[N_SC], el[N_SC];
    float    ref[N_SC];                     // recent normalised shape (glitch check)
} md_stream_t;

enum { MD_CAL_WAIT = 0, MD_CAL_COLLECT, MD_CAL_FAST, MD_CAL_TRACK };
enum { MD_WHY_BOOT = 0, MD_WHY_BUTTON, MD_WHY_RSSI, MD_WHY_CHANNEL };
static inline const char *md_phase_name(int p) {
    static const char *const n[] = { "wait", "collect", "fast", "track" };
    return p >= 0 && p <= MD_CAL_TRACK ? n[p] : "?";
}
static inline const char *md_why_name(int w) {
    static const char *const n[] = { "boot", "button", "rssi", "channel" };
    return w >= 0 && w <= MD_WHY_CHANNEL ? n[w] : "?";
}

typedef struct {
    md_cfg_t c;
    int64_t  settle_until;
    bool     motion, m_short, m_long;
    md_stream_t s[MAX_STREAMS];
    // floor (auto-calibration)
    int      phase, why;
    bool     cal_event;                     // phase/floor source changed
    int64_t  collect_from, next_sample, next_floor;
    int      ring_n, ring_i;
    float    floor;                         // 0 = not known yet
    float    ring[MD_RING], scratch[MD_RING];
} motion_det_t;

// Forget the floor and measure it again, starting delay_us from now
static inline void md_recalibrate(motion_det_t *d, int64_t now_us, int64_t delay_us, int why) {
    d->phase = MD_CAL_WAIT;
    d->why = why;
    d->cal_event = true;
    d->collect_from = now_us + delay_us;
    d->next_sample = d->collect_from;
    d->next_floor = d->collect_from;
    d->ring_n = d->ring_i = 0;
    d->floor = 0;
    for (int i = 0; i < MAX_STREAMS; i++) {         // RSSI: current level is the new normal
        d->s[i].rssi_s = d->s[i].rssi_f;
        d->s[i].rssi_t0 = now_us;
    }
}

static inline void md_init(motion_det_t *d, const md_cfg_t *c, int64_t now_us) {
    memset(d, 0, sizeof *d);
    d->c = *c;
    md_recalibrate(d, now_us, 0, MD_WHY_BOOT);
}

// Channel changed: every baseline and the floor are invalid
static inline void md_reset(motion_det_t *d, int64_t now_us) {
    for (int i = 0; i < MAX_STREAMS; i++) d->s[i].n = 0;
    d->settle_until = now_us + d->c.settle_us;
    bool booting = d->why == MD_WHY_BOOT && d->floor <= 0;   // e.g. the start-up scan
    md_recalibrate(d, now_us, 0, booting ? MD_WHY_BOOT : MD_WHY_CHANNEL);
}

static inline float md_alpha(float dt_s, float tau_s) { return 1.0f - expf(-dt_s / tau_s); }

// Mean of the K largest values (order of summation: largest first)
static inline float md_top_mean(const float *e) {
    float top[MD_TOP_K];
    int n = 0;
    for (int i = 0; i < N_SC; i++) {
        float v = e[i];
        int j = n < MD_TOP_K ? n++ : MD_TOP_K;
        if (j == MD_TOP_K) { if (v <= top[MD_TOP_K - 1]) continue; j = MD_TOP_K - 1; }
        while (j > 0 && top[j - 1] < v) { top[j] = top[j - 1]; j--; }
        top[j] = v;
    }
    float sum = 0;
    for (int i = 0; i < MD_TOP_K; i++) sum += top[i];
    return sum / MD_TOP_K;
}

// Feed one packet. Returns the stream's short-window score, or -1 if it isn't
// ready yet / the packet is unusable.
static inline float md_update(motion_det_t *d, int node, int kind, int64_t ts, int rssi,
                              const int8_t *buf, int len) {
    if (node < 0 || node >= MAX_NODES || kind < 0 || kind > 1) return -1;
    if (ts < d->settle_until) return -1;
    float a[N_SC];
    if (!lltf_amplitudes(buf, len, a)) return -1;
    float mean = 0;
    for (int i = 0; i < N_SC; i++) mean += a[i];
    mean /= N_SC;
    if (mean <= 0) return -1;

    md_stream_t *s = &d->s[node * 2 + kind];
    const md_cfg_t *c = &d->c;
    if (s->n > 0) {
        // Scan check, ACK streams only (steady 100/s; a router's own frames are
        // bursty): a gap far longer than usual means the router left the
        // channel for a background scan (people never stop the ACKs). Until
        // scan_hold after it, the stricter scan_shape limit applies below.
        int64_t gap = ts - s->t_heard;
        if (kind == CSI_KIND_ACK && gap > c->scan_gap_us && s->iv_us > 0 &&
            (float)gap > c->scan_gap_ratio * s->iv_us) {
            s->scan_until = ts + c->scan_hold_us;
            s->scan_gap_at = ts;
        } else if (gap >= 0) {               // typical interval: falls fast, rises slowly
            float g = (float)gap, w = g < s->iv_us ? 0.5f : 0.05f;
            s->iv_us = s->iv_us > 0 ? s->iv_us + w * (g - s->iv_us) : g;
        }
        s->t_heard = ts;
        // Glitch check: some routers briefly switch to a very different channel
        // shape and snap back (seen every ~20 min). People never change the
        // shape that much that fast, so ignore such packets; a new shape that
        // lasts glitch_hold is real (e.g. the board was moved) and is accepted.
        // Around a scan the switch is milder (0.26-0.34), hence scan_shape;
        // people moving reach that only in single packets and never stop the
        // ACKs, so the normal limit stays loose and can't lock out movement.
        float dist = 0;
        for (int i = 0; i < N_SC; i++) dist += fabsf(a[i] / mean - s->ref[i]);
        dist /= N_SC;
        int64_t since = ts - s->t_last;
        bool scan = ts < s->scan_until;
        if (dist > (scan ? c->scan_shape : c->glitch_shape) && since < c->glitch_hold_us) {
            s->n_glitch++;
            if (scan) {                      // the switch lasts: stay strict, but never
                int64_t u = ts + c->scan_hold_us, cap = s->scan_gap_at + c->glitch_hold_us;
                s->scan_until = u < cap ? u : cap;   // past glitch_hold after the gap
            }
            return -1;
        }
        float a_r = since >= c->glitch_hold_us ? 1.0f
                  : md_alpha((float)(since > 0 ? since : 0) * 1e-6f, c->tau_ref);
        for (int i = 0; i < N_SC; i++) s->ref[i] += a_r * (a[i] / mean - s->ref[i]);
    }
    if (s->n == 0) {
        for (int i = 0; i < N_SC; i++) {
            s->lp[i] = s->hp[i] = s->ref[i] = a[i] / mean;
            s->es[i] = s->el[i] = 0;
        }
        s->t_heard = ts;
        s->scan_until = s->scan_gap_at = 0;
        s->iv_us = 0;
        s->t_start = ts;
        s->rssi_f = s->rssi_s = (float)rssi;
        s->rssi_t0 = ts;
    } else {
        float dt = (float)(ts - s->t_last) * 1e-6f;
        if (dt < 0) dt = 0;
        float a_lp = md_alpha(dt, c->tau_lp), a_hp = md_alpha(dt, c->tau_hp);
        float a_s = md_alpha(dt, c->tau_short), a_l = md_alpha(dt, c->tau_long);
        // The long window is for weak movement: cap what one packet adds, so a
        // big burst can't hold it on for long and lone noise spikes count less
        float lcap = c->long_cap * d->floor;
        lcap = lcap > 0 ? lcap * lcap : INFINITY;
        for (int i = 0; i < N_SC; i++) {
            float x = a[i] / mean;
            s->lp[i] += a_lp * (x - s->lp[i]);
            s->hp[i] += a_hp * (x - s->hp[i]);
            float y = s->lp[i] - s->hp[i], y2 = y * y;
            s->es[i] += a_s * (y2 - s->es[i]);
            s->el[i] += a_l * ((y2 < lcap ? y2 : lcap) - s->el[i]);
            if (s->el[i] > lcap) s->el[i] = lcap;   // e.g. built up before a floor existed
        }
        // Running mean at first (no start-up bias); for rssi_min_age after a
        // start/recalibration the slow average follows the fast one, so it
        // settles at the current level before steps are judged
        float inv_n = 1.0f / (float)(s->n + 1);
        float a_f = md_alpha(dt, c->rssi_tau_fast);
        if (a_f < inv_n) a_f = inv_n;
        float a_sl = md_alpha(dt, c->rssi_tau_slow);
        if (a_sl < inv_n) a_sl = inv_n;
        if (ts - s->rssi_t0 < c->rssi_min_age_us) a_sl = a_f;
        s->rssi_f += a_f * ((float)rssi - s->rssi_f);
        s->rssi_s += a_sl * ((float)rssi - s->rssi_s);
    }
    s->n++;
    s->t_last = ts;
    s->score_s = s->score_l = -1;

    // A lasting RSSI step means the board (or the AP) was moved: recalibrate
    if (ts - s->rssi_t0 >= c->rssi_min_age_us &&
        fabsf(s->rssi_f - s->rssi_s) > c->rssi_step_db)
        md_recalibrate(d, ts, 0, MD_WHY_RSSI);

    if ((int)s->n < c->min_pkts || (float)(ts - s->t_start) * 1e-6f < c->warm_s)
        return -1;
    s->score_s = sqrtf(md_top_mean(s->es));
    s->score_l = sqrtf(md_top_mean(s->el));
    return s->score_s;
}

// Highest short / long score among the deciding streams heard within
// stale_s, or -1. Deciding: AP streams only (device links are scored and
// streamed but don't decide yet), and an AP's own-frames stream only while
// it has no live ACK stream. The ACKs are one known frame type at a steady
// 100/s; a router's own frames are sparse mixed traffic and alone caused
// most false starts on a router that sends them (8 of 14 in one session).
static inline bool md_live(const md_stream_t *s, int64_t now_us, int64_t stale) {
    return s->n && now_us - s->t_last <= stale;
}
// An ACK stream mid router-scan: it may score nothing for a few seconds (its
// packets are ignored), but the router's own frames must not take over then
static inline bool md_scanning(const md_stream_t *s, int64_t now_us, int64_t stale) {
    return s->n && now_us - s->t_heard <= stale && now_us < s->scan_until;
}
static inline void md_max_scores(const motion_det_t *d, int64_t now_us,
                                 float *short_out, float *long_out) {
    float bs = -1, bl = -1;
    int64_t stale = (int64_t)(d->c.stale_s * 1e6f);
    for (int i = 0; i < MAX_APS * 2; i++) {
        const md_stream_t *s = &d->s[i];
        if (!md_live(s, now_us, stale) || s->score_s < 0) continue;
        if (i % 2 == CSI_KIND_AP && (md_live(&d->s[i + 1], now_us, stale) ||
                                     md_scanning(&d->s[i + 1], now_us, stale))) continue;
        if (s->score_s > bs) bs = s->score_s;
        if (s->score_l > bl) bl = s->score_l;
    }
    *short_out = bs;
    *long_out = bl;
}

static inline int md_cmp_float(const void *a, const void *b) {
    float x = *(const float *)a, y = *(const float *)b;
    return (x > y) - (x < y);
}

static inline float md_clampf(float v, float lo, float hi) {
    return v < lo ? lo : v > hi ? hi : v;
}

// Floor bookkeeping: sample the short score at 10 Hz into a 120 s ring and,
// once a second, set the floor to its 10th percentile. The short score drops
// back to the quiet level in every brief pause, so this works even while
// someone moves around, and it runs all the time, motion or not, so a board
// moved somewhere noisier can't get stuck in "motion". While the ring fills
// ("fast") the floor follows directly; afterwards ("track") it may fall
// immediately but rise at most floor_up_per_s per second.
static inline void md_floor_step(motion_det_t *d, int64_t ts, float score_short) {
    const md_cfg_t *c = &d->c;
    if (ts < d->collect_from) return;
    if (d->phase == MD_CAL_WAIT) { d->phase = MD_CAL_COLLECT; d->cal_event = true; }
    if (score_short >= 0 && ts >= d->next_sample) {
        d->ring[d->ring_i] = score_short;
        d->ring_i = (d->ring_i + 1) % MD_RING;
        if (d->ring_n < MD_RING) d->ring_n++;
        d->next_sample = ts + c->sample_us;
    }
    if (ts < d->next_floor || d->ring_n < c->floor_min_n) return;
    d->next_floor = ts + c->floor_every_us;
    memcpy(d->scratch, d->ring, (size_t)d->ring_n * sizeof(float));
    qsort(d->scratch, (size_t)d->ring_n, sizeof(float), md_cmp_float);
    float q = d->scratch[d->ring_n * c->floor_pct / 100];
    if (d->phase == MD_CAL_TRACK) {
        float cap = d->floor * (1.0f + c->floor_up_per_s * (float)c->floor_every_us * 1e-6f);
        if (q > cap) q = cap;
    } else {
        int next = d->ring_n == MD_RING ? MD_CAL_TRACK : MD_CAL_FAST;
        if (d->phase == MD_CAL_COLLECT)          // first floor: the long window's
            for (int i = 0; i < MAX_STREAMS; i++) { // memory predates the scale, restart it
                memset(d->s[i].el, 0, sizeof d->s[i].el);
                if (d->s[i].score_l > 0) d->s[i].score_l = 0;
            }
        if (next != d->phase) { d->phase = next; d->cal_event = true; }
    }
    d->floor = md_clampf(q, c->floor_lo, c->floor_hi);
}

// Run after md_update(). Hysteresis per window, motion if either window says
// so; no verdict (still) until the floor is known.
// Returns +1 on motion start, -1 on motion end, 0 otherwise.
static inline int md_eval(motion_det_t *d, int64_t now_us) {
    float ms, ml;
    md_max_scores(d, now_us, &ms, &ml);
    md_floor_step(d, now_us, ms);
    md_max_scores(d, now_us, &ms, &ml);      // the floor step may restart the long window
    const md_cfg_t *c = &d->c;
    float f = d->floor;
    if (f <= 0) {
        d->m_short = d->m_long = false;
    } else {
        if (!d->m_short && ms > c->on_short * f && ml > c->short_gate * f) d->m_short = true;
        else if (d->m_short && ms < c->off_short * f) d->m_short = false;
        if (!d->m_long && ml > c->on_long * f) d->m_long = true;
        else if (d->m_long && ml < c->off_long * f) d->m_long = false;
    }
    bool m = d->m_short || d->m_long;
    int change = m == d->motion ? 0 : (m ? +1 : -1);
    d->motion = m;
    return change;
}

// True once after the calibration phase or its reason changed
static inline bool md_take_cal_event(motion_det_t *d) {
    bool e = d->cal_event;
    d->cal_event = false;
    return e;
}
