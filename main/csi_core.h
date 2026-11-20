// csi_core.h: pure logic shared by firmware and host tests (no ESP-IDF deps)
#pragma once
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <inttypes.h>
#include <math.h>
#include "seal.h"

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
    uint8_t ssid[32];    // network name as sent (not NUL-terminated)
    int     ssid_len;    // 0 = hidden network
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
        if (id == 0) {
            out->ssid_match = want > 0 && l == want && memcmp(v, ssid, l) == 0;
            out->ssid_len = l <= 32 ? l : 0;
            if (out->ssid_len) memcpy(out->ssid, v, (size_t)out->ssid_len);
        }
        else if (id == 3 && l >= 1)
            out->ds_ch = v[0];
        else if (id == 37 && l >= 3) {             // mode, new_ch, count
            out->csa_ch = v[1];
            out->csa_cnt = v[2];
        }
        ie += 2 + l;
    }
}

// ---------- Nearby networks (the SCAN command) ----------
#define MAX_NETS 32
#define MAX_NET_APS 6            // access points kept per name (mesh nodes, extenders)
typedef struct {
    char    ssid[33];            // NUL-terminated
    uint8_t ch;
    int8_t  rssi;                // strongest beacon heard
    uint16_t n;                  // beacons heard
    uint8_t n_ap;                // access points with this name
    uint8_t ap[MAX_NET_APS][6];  // their BSSIDs,
    uint8_t ap_ch[MAX_NET_APS];  // channels
    int8_t  ap_rssi[MAX_NET_APS]; // and strongest beacons
} net_t;
typedef struct { net_t n[MAX_NETS]; int count; } net_list_t;

// Note a beacon from `bssid`. Hidden networks (empty or all-zero names) are
// skipped; one entry per name, keeping its strongest AP and a list of its APs.
// Returns the entry or -1.
static inline int net_note(net_list_t *l, const uint8_t *ssid, int len, const uint8_t *bssid,
                           int ch, int rssi) {
    if (len <= 0 || len > 32 || !ssid) return -1;
    bool blank = true;
    for (int i = 0; i < len; i++) if (ssid[i]) blank = false;
    if (blank) return -1;
    int k = -1;
    for (int i = 0; i < l->count && k < 0; i++)
        if ((int)strlen(l->n[i].ssid) == len && memcmp(l->n[i].ssid, ssid, (size_t)len) == 0) k = i;
    if (k < 0) {
        if (l->count >= MAX_NETS) return -1;
        k = l->count++;
        memset(&l->n[k], 0, sizeof l->n[k]);
        memcpy(l->n[k].ssid, ssid, (size_t)len);
        l->n[k].rssi = -127;
    }
    net_t *e = &l->n[k];
    if (rssi > e->rssi) { e->rssi = (int8_t)rssi; e->ch = (uint8_t)ch; }
    if (e->n < 0xFFFF) e->n++;
    if (bssid) {
        int a = -1;
        for (int i = 0; i < e->n_ap && a < 0; i++)
            if (memcmp(e->ap[i], bssid, 6) == 0) a = i;
        if (a < 0 && e->n_ap < MAX_NET_APS) {
            a = e->n_ap++;
            memcpy(e->ap[a], bssid, 6);
            e->ap_rssi[a] = -127;
        }
        if (a >= 0 && rssi > e->ap_rssi[a]) { e->ap_rssi[a] = (int8_t)rssi; e->ap_ch[a] = (uint8_t)ch; }
    }
    return k;
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
    int8_t   ap;                // the AP (node 0..MAX_APS-1) of its latest frame
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
    int ap = node_find(t, bssid);
    if (!node_is_ap(ap)) return -1;                     // not our network
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
    e->ap = (int8_t)ap;                                 // a device can move to another AP
    return k;
}

// Commands from the PC, one per line:
//   POKE,<mac>     start poking a device of ours (mac as 6 hex pairs with ':')
//   UNPOKE,<mac>   stop
//   UNPOKE,all
//   CAL            recalibrate, like the button
//   BEEP[,<ms>[,<hz>[,<vol>]]]  test the buzzer (parse_beep)
//   GLIDE,<ms>,<hz>,<hz2>[,<vol>]  a pitch glide (parse_glide)
//   SCAN           list nearby networks (NET lines, then NETS,done)
//   USE,<name>     watch this network from now on (saved; parse_use)
//   USE_DEFAULT    forget that choice: back to the build's MY_SSID
//   SOUND          report the buzzer settings (parse_sound)
//   SOUND,<off|beep|alarm>[,<vol>[,<hz>]]  change them (saved on the board)
//   SOUND,default  back to the build's buzzer settings
//   SOUND,test     play the current mode's sound (siren in alarm mode)
//   SENS           report the long-window threshold (parse_sens)
//   SENS,<x100>    set it (saved on the board): 110-300 = x1.1-x3.0
//   SENS,default   back to the build's
//   APS,<mac>[,<mac>...]  watch these access points (1-8, any network names,
//                  saved; parse_aps) instead of every one named like the network
//   APS,clear      back to watching every access point of the network by name
//   APS            report the list (APS,<n>[,<mac>...])
//   EXT,<1|0>      a device link the PC watches sees motion (1) or not (0): the LED
//                  and buzzer follow it too; 1 lapses after EXT_HOLD_MS unless repeated.
//                  The board's own verdict (STATE) is never changed by it
enum { CMD_NONE = 0, CMD_POKE, CMD_UNPOKE, CMD_UNPOKE_ALL, CMD_CAL, CMD_SCAN };

// ---------- Command lines from the PC ----------
// Assembles bytes into lines. The longest command is APS with MAX_APS
// addresses (4 + 8 x 18 = 147 chars), so lines get 192 bytes. A longer line is
// dropped whole: everything up to its newline is skipped, so its tail is never
// run as a command of its own.
#define CMD_LINE_MAX 192
typedef struct {
    char buf[CMD_LINE_MAX];
    int  n;
    bool overflow;       // the current line is too long: skipping to its end
} cmd_rx_t;

// Feed one byte. Returns the line (NUL-terminated, without CR/LF) once a
// complete, non-empty line has arrived, else NULL. The line stays valid until
// the next call.
static inline const char *cmd_rx_feed(cmd_rx_t *r, char c) {
    if (c == '\n' || c == '\r') {
        bool ok = !r->overflow && r->n > 0;
        int n = r->n;
        r->n = 0;
        r->overflow = false;
        if (!ok) return NULL;
        r->buf[n] = '\0';
        return r->buf;
    }
    if (r->overflow) return NULL;
    if (r->n < CMD_LINE_MAX - 1) {
        r->buf[r->n++] = c;
    } else {
        r->n = 0;
        r->overflow = true;
    }
    return NULL;
}

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
    if (strncmp(line, "SCAN", 4) == 0 && (line[4] == '\0' || line[4] == '\r' || line[4] == '\n'))
        return CMD_SCAN;
    if (strncmp(line, "UNPOKE,all", 10) == 0 &&
        (line[10] == '\0' || line[10] == '\r' || line[10] == '\n')) return CMD_UNPOKE_ALL;
    if (strncmp(line, "UNPOKE,", 7) == 0) return parse_mac(line + 7, mac) ? CMD_UNPOKE : CMD_NONE;
    if (strncmp(line, "POKE,", 5) == 0) {
        if (!parse_mac(line + 5, mac) || (mac[0] & 0x01)) return CMD_NONE;  // no group MACs
        return CMD_POKE;
    }
    return CMD_NONE;
}

// ---------- Chosen access points (APS) ----------
// An explicit list of BSSIDs to watch instead of every access point with the
// network's name: they may belong to different networks, but the board listens
// on one channel, so only those on the channel it picks are poked.
typedef struct { uint8_t mac[MAX_APS][6]; int n; } ap_list_t;

static inline int ap_list_index(const ap_list_t *l, const uint8_t *mac) {
    for (int i = 0; l && mac && i < l->n; i++)
        if (memcmp(l->mac[i], mac, 6) == 0) return i;
    return -1;
}

enum { APS_BAD = -1, APS_CLEAR = 0, APS_QUERY = -2 };
// APS,<mac>[,<mac>...]: 1-MAX_APS different unicast MACs into *out, returns
// how many; APS,clear -> APS_CLEAR; APS -> APS_QUERY; anything else APS_BAD
// (and *out is left alone)
static inline int parse_aps(const char *line, ap_list_t *out) {
    if (!line || strncmp(line, "APS", 3) != 0) return APS_BAD;
    const char *s = line + 3;
    if (*s == '\0' || *s == '\r' || *s == '\n') return APS_QUERY;
    if (*s++ != ',') return APS_BAD;
    if (strncmp(s, "clear", 5) == 0 && (s[5] == '\0' || s[5] == '\r' || s[5] == '\n'))
        return APS_CLEAR;
    ap_list_t l;
    l.n = 0;
    for (;;) {
        char tok[18];
        size_t n = strcspn(s, ",\r\n");
        if (n != 17 || l.n >= MAX_APS) return APS_BAD;
        memcpy(tok, s, 17);
        tok[17] = '\0';
        if (!parse_mac(tok, l.mac[l.n]) || (l.mac[l.n][0] & 0x01)) return APS_BAD;  // no group MACs
        if (ap_list_index(&l, l.mac[l.n]) >= 0) return APS_BAD;                       // no repeats
        l.n++;
        s += 17;
        if (*s != ',') break;
        s++;
    }
    if (*s != '\0' && *s != '\r' && *s != '\n') return APS_BAD;
    *out = l;
    return l.n;
}

// The channel to listen on for a list of access points: the one where most of
// them were heard (hits[ch]: bit i = list entry i heard there), the strongest
// on a tie (best[ch]: its strongest RSSI). 0 if none heard.
static inline int aps_best_channel(const int *hits, const int *best, int max_ch) {
    int ch = 0, n_best = 0;
    for (int c = 1; c <= max_ch; c++) {
        int n = __builtin_popcount((unsigned)hits[c]);
        if (n && (n > n_best || (n == n_best && best[c] > best[ch]))) { ch = c; n_best = n; }
    }
    return ch;
}

// USE,<name>: copies the network name (1-32 bytes, the rest of the line
// without a trailing CR/LF) to out (33 bytes), returns its length or -1
static inline int parse_use(const char *line, char out[33]) {
    if (!line || strncmp(line, "USE,", 4) != 0) return -1;
    const char *s = line + 4;
    int n = (int)strcspn(s, "\r\n");
    if (n < 1 || n > 32) return -1;
    memcpy(out, s, (size_t)n);
    out[n] = '\0';
    return n;
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

// Buzzer modes: no automatic sounds / a chime when motion starts and ends /
// an air-raid whoop for as long as motion lasts
enum { SND_OFF = 0, SND_BEEP, SND_ALARM };
enum { SOUND_BAD = -1, SOUND_QUERY = 0, SOUND_SET, SOUND_DEFAULT, SOUND_TEST };
#define SOUND_VOL_MAX 50                 // % PWM duty: past 50 a piezo gets quieter again
#define SOUND_HZ_MIN  500
#define SOUND_HZ_MAX  6000
static const char *const SND_NAMES[] = {"off", "beep", "alarm"};

// SOUND, SOUND,default, SOUND,test or SOUND,<mode>[,<vol>[,<hz>]]: returns
// one of SOUND_*; for SOUND_SET sets *mode (SND_*), *vol (1-50 %) and *hz
// (500-6000), each -1 when not given
static inline int parse_sound(const char *line, int *mode, int *vol, int *hz) {
    *mode = *vol = *hz = -1;
    if (!line || strncmp(line, "SOUND", 5) != 0) return SOUND_BAD;
    const char *s = line + 5;
#define SND_END(c) ((c) == '\0' || (c) == '\r' || (c) == '\n')
    if (SND_END(*s)) return SOUND_QUERY;
    if (*s++ != ',') return SOUND_BAD;
    if (strncmp(s, "default", 7) == 0 && SND_END(s[7])) return SOUND_DEFAULT;
    if (strncmp(s, "test", 4) == 0 && SND_END(s[4])) return SOUND_TEST;
    int m = -1;
    for (int i = 0; i < 3 && m < 0; i++) {
        size_t n = strlen(SND_NAMES[i]);
        if (strncmp(s, SND_NAMES[i], n) == 0 && (s[n] == ',' || SND_END(s[n]))) { m = i; s += n; }
    }
    if (m < 0) return SOUND_BAD;
    int v[2] = {-1, -1};
    for (int k = 0; k < 2 && *s == ','; k++) {
        s++;
        int x = 0, digits = 0;
        while (*s >= '0' && *s <= '9' && digits < 6) { x = x * 10 + (*s++ - '0'); digits++; }
        if (!digits) return SOUND_BAD;
        v[k] = x;
    }
    if (!SND_END(*s)) return SOUND_BAD;
#undef SND_END
    if (v[0] >= 0 && (v[0] < 1 || v[0] > SOUND_VOL_MAX)) return SOUND_BAD;
    if (v[1] >= 0 && (v[1] < SOUND_HZ_MIN || v[1] > SOUND_HZ_MAX)) return SOUND_BAD;
    *mode = m; *vol = v[0]; *hz = v[1];
    return SOUND_SET;
}

// SENS, SENS,default or SENS,<on_long x 100>: returns SENS_QUERY, SENS_DEFAULT
// or SENS_SET (with *x100 = 110-300), or SENS_BAD
// KEY: report whether the board has its key (KEY,none or KEY,<fingerprint>)
// KEY,<32 hex>: set it (saved); KEY,clear: forget it (the board then sends no
// reports). Parsed with PEERKEY below.
enum { KEY_BAD = -1, KEY_QUERY = 0, KEY_SET, KEY_CLEAR };
static inline int hexval(char c) {
    return c >= '0' && c <= '9' ? c - '0' : c >= 'a' && c <= 'f' ? c - 'a' + 10
         : c >= 'A' && c <= 'F' ? c - 'A' + 10 : -1;
}
enum { SENS_BAD = -1, SENS_QUERY = 0, SENS_SET, SENS_DEFAULT };
#define SENS_MIN 110
#define SENS_MAX 300
static inline int parse_sens(const char *line, int *x100) {
    *x100 = 0;
    if (!line || strncmp(line, "SENS", 4) != 0) return SENS_BAD;
    const char *s = line + 4;
#define SENS_END(c) ((c) == '\0' || (c) == '\r' || (c) == '\n')
    if (SENS_END(*s)) return SENS_QUERY;
    if (*s++ != ',') return SENS_BAD;
    if (strncmp(s, "default", 7) == 0 && SENS_END(s[7])) return SENS_DEFAULT;
    int v = 0, digits = 0;
    while (*s >= '0' && *s <= '9' && digits < 4) { v = v * 10 + (*s++ - '0'); digits++; }
    if (!digits || !SENS_END(*s) || v < SENS_MIN || v > SENS_MAX) return SENS_BAD;
#undef SENS_END
    *x100 = v;
    return SENS_SET;
}

// ---------- The status LED (LEDSET) ----------
// What the on-board LED shows. Modes: status = everything (motion, the fade
// after it, faint blips, calibrating, looking for the router); motion = only
// motion; off = never lit. The motion pattern: strength = brighter the
// stronger, steady, pulse (a slow breath) or blink. bright scales every colour.
enum { LED_MODE_STATUS = 0, LED_MODE_MOTION, LED_MODE_OFF };
enum { LED_PAT_STRENGTH = 0, LED_PAT_STEADY, LED_PAT_PULSE, LED_PAT_BLINK };
static const char *const LED_MODE_NAMES[] = {"status", "motion", "off"};
static const char *const LED_PAT_NAMES[] = {"strength", "steady", "pulse", "blink"};
typedef struct {
    uint8_t mode, pattern, bright;   // bright 1-255
    uint32_t rgb;                    // motion colour 0xRRGGBB
} led_cfg_t;

static inline led_cfg_t led_cfg_default(int bright) {
    led_cfg_t c = { LED_MODE_STATUS, LED_PAT_STRENGTH, (uint8_t)(bright < 1 ? 1 : bright > 255 ? 255 : bright),
                    0xff0000 };
    return c;
}

static inline int led_name_index(const char *s, size_t n, const char *const *names, int count) {
    for (int i = 0; i < count; i++)
        if (strlen(names[i]) == n && strncmp(s, names[i], n) == 0) return i;
    return -1;
}

// LEDSET, LEDSET,default or LEDSET,<mode>,<bright>,<pattern>,<rrggbb>:
// returns LED_QUERY, LED_DEFAULT or LED_SET (filling *out), else LED_BAD
enum { LED_BAD = -1, LED_QUERY = 0, LED_SET, LED_DEFAULT };
static inline int parse_led(const char *line, led_cfg_t *out) {
    if (!line || strncmp(line, "LEDSET", 6) != 0) return LED_BAD;
    const char *s = line + 6;
#define LED_END(c) ((c) == '\0' || (c) == '\r' || (c) == '\n')
    if (LED_END(*s)) return LED_QUERY;
    if (*s++ != ',') return LED_BAD;
    if (strncmp(s, "default", 7) == 0 && LED_END(s[7])) return LED_DEFAULT;
    led_cfg_t c;
    size_t n = strcspn(s, ",\r\n");
    int m = led_name_index(s, n, LED_MODE_NAMES, 3);
    if (m < 0 || s[n] != ',') return LED_BAD;
    s += n + 1;
    int v = 0, digits = 0;
    while (*s >= '0' && *s <= '9' && digits < 4) { v = v * 10 + (*s++ - '0'); digits++; }
    if (!digits || v < 1 || v > 255 || *s++ != ',') return LED_BAD;
    n = strcspn(s, ",\r\n");
    int p = led_name_index(s, n, LED_PAT_NAMES, 4);
    if (p < 0 || s[n] != ',') return LED_BAD;
    s += n + 1;
    uint32_t rgb = 0;
    for (int i = 0; i < 6; i++) {
        char ch = *s++;
        int h = ch >= '0' && ch <= '9' ? ch - '0' : ch >= 'a' && ch <= 'f' ? ch - 'a' + 10
              : ch >= 'A' && ch <= 'F' ? ch - 'A' + 10 : -1;
        if (h < 0) return LED_BAD;
        rgb = rgb << 4 | (uint32_t)h;
    }
    if (!LED_END(*s)) return LED_BAD;
#undef LED_END
    c.mode = (uint8_t)m; c.bright = (uint8_t)v; c.pattern = (uint8_t)p; c.rgb = rgb;
    *out = c;
    return LED_SET;
}

// ---------- The BOOT button ----------
// A short press recalibrates (on release); held BTN_LONG_MS it resets the board's
// saved settings instead (once, while still held). Call every poll with the level.
#define BTN_LONG_MS 5000
enum { BTN_NONE = 0, BTN_SHORT, BTN_LONG };
typedef struct { int held; bool long_done; } btn_t;
static inline int btn_step(btn_t *b, bool pressed, int poll_ms) {
    if (pressed) {
        b->held++;
        if (!b->long_done && b->held * poll_ms >= BTN_LONG_MS) { b->long_done = true; return BTN_LONG; }
        return BTN_NONE;
    }
    int r = b->held >= 2 && !b->long_done ? BTN_SHORT : BTN_NONE;   // 2 polls: not a bounce
    b->held = 0;
    b->long_done = false;
    return r;
}

// EXT,1 / EXT,0: returns 1 or 0, or -1 if the line isn't one
#define EXT_HOLD_MS 5000                     // EXT,1 lasts this long unless repeated
static inline int parse_ext(const char *line) {
    if (!line || strncmp(line, "EXT,", 4) != 0 || (line[4] != '0' && line[4] != '1')) return -1;
    char e = line[5];
    if (e != '\0' && e != '\r' && e != '\n') return -1;
    return line[4] - '0';
}

#define LED_EXT_RGB 0x8a3cff                 // motion only a device link sees: purple

// The LED's colour now (0xRRGGBB). searching: looking for the router;
// strength: motion score / threshold x10 (10 = at it); ended_ms / blip_ms: when
// motion last ended / a too-faint blip was seen (0 = never); t_ms: now.
#define LED_FADE_MS  30000                   // "recent motion" amber fade
#define LED_BLIP_MS  150
// ext: a device link the PC watches sees motion while the board's own verdict is still
// (shown in LED_EXT_RGB with the motion pattern; "stronger = brighter" shows it steady).
static inline uint32_t led_pick(const led_cfg_t *c, bool searching, bool cal, bool motion, bool ext,
                                int strength, uint32_t ended_ms, uint32_t blip_ms, uint32_t t_ms) {
    const int B = c->bright, d = B / 5 ? B / 5 : 1;
    int r = 0, g = 0, b = 0;
    if (c->mode == LED_MODE_OFF) return 0;
    bool status = c->mode == LED_MODE_STATUS;
    if (status && searching) {
        r = g = d;
    } else if (status && cal) {               // 2 s triangle pulse, dim
        uint32_t ph = t_ms % 2000, tri = ph < 1000 ? ph : 2000 - ph;
        b = 1 + (int)((2 * d - 1) * tri / 1000);
    } else if (motion || ext) {
        uint32_t rgb = motion ? c->rgb : LED_EXT_RGB;
        int lvl = B;                          // how bright the motion colour is, 0-255
        if (c->pattern == LED_PAT_STRENGTH && motion) {   // B/3 at the threshold .. B at 3x
            int k = strength < 10 ? 10 : strength > 30 ? 30 : strength;
            lvl = B / 3 + (B - B / 3) * (k - 10) / 20;
        } else if (c->pattern == LED_PAT_PULSE) {        // a 1.6 s breath, never fully dark
            uint32_t ph = t_ms % 1600, tri = ph < 800 ? ph : 1600 - ph;
            lvl = B / 6 + (int)((B - B / 6) * tri / 800);
        } else if (c->pattern == LED_PAT_BLINK) {        // 0.4 s on, 0.4 s off
            lvl = (t_ms % 800) < 400 ? B : 0;
        }
        r = (int)((rgb >> 16) & 0xff) * lvl / 255;
        g = (int)((rgb >> 8) & 0xff) * lvl / 255;
        b = (int)(rgb & 0xff) * lvl / 255;
        if (lvl && !r && !g && !b) {          // a very dim colour still shows
            r = (rgb >> 16) & 0xff ? 1 : 0; g = (rgb >> 8) & 0xff ? 1 : 0; b = rgb & 0xff ? 1 : 0;
        }
    } else if (!status) {
        return 0;                             // motion only: dark otherwise
    } else if (ended_ms && t_ms - ended_ms < LED_FADE_MS) {
        int a = (B / 3) * (int)(LED_FADE_MS - (t_ms - ended_ms)) / LED_FADE_MS;
        if (a >= 2) { r = a; g = a / 2; }     // amber; below 2 it would turn red
    }
    if (status && !r && !g && !b && !searching && !cal && !motion && !ext && blip_ms && t_ms - blip_ms < LED_BLIP_MS)
        r = b = d;                            // a blip too faint to count: quick purple
    return (uint32_t)r << 16 | (uint32_t)g << 8 | (uint32_t)b;
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

// ---------- Other ackwave boards ("peers") ----------
// Each board broadcasts a small hello once a second: a vendor-specific action
// frame from its real MAC, saying which router (BSSID) it watches and on
// which channel, and its own verdict (SAT lines on the board that hears it).
// A board that hears it lists the peer (PEER lines) and can poke it like a
// device: an ESP32 ACKs frames to its own MAC whether or not it joined
// anything, so two boards give a link across the rooms between them.
// The hello carries no network name, but it is not encrypted: anyone nearby
// with a sniffer could read whether a board sees motion.
#define HELLO_LEN    71         // a report (version 4): sealed, sent to the hub
#define HELLO_V1_LEN 40         // a hello: who and where (broadcast, every board)
#define HELLO_EVERY_MS 500      // hellos and reports each at a fixed rate: timing says nothing
#define MAX_PEERS    8          // boards one board lists, and keys a hub holds
#define PEER_GONE_MS 10000      // not heard this long: forgotten
static const uint8_t HELLO_MAGIC[8] = { 127, 0x02, 0xAC, 0x57, 'A', 'C', 'K', 'W' };  // vendor action, made-up local OUI
#define HELLO_VERSION 4
#define HELLO_SEQ_MAX 0xFFFFFFu // 24 bits; then the boot counter goes up, so a nonce is never reused
#define REPORT_LEN   10         // sealed: flags, k_short, k_long, acks, rssi, n_dev, up_s
#define HELLO_AAD_LEN 29        // bytes 24..52 (magic .. seq, with the sender's MAC), checked by the tag
#define REPORT_NODE  MAX_NODES  // made-up sender for reports: poke_ta(me, REPORT_NODE)

// Two frames, both vendor action frames:
//  - the hello (40 bytes), broadcast on the board's own channel from its real
//    MAC: who it is, its channel, its router. Boards find and poke each other
//    with it. Nothing about motion.
//  - the report (71 bytes), sent to the hub (addr1 = the hub's MAC) on the
//    hub's channel, whatever network each one watches: the board's own
//    verdict, sealed with AES-CCM (seal.h) under the board's own key, which
//    the PC derives (keys.py); the hub holds its boards' keys (PEERKEY). Its
//    sender address is made up (REPORT_NODE), so the hub's ACK, addressed to
//    it, reaches us: that is how a board knows the hub got it, and finds the
//    hub's channel again when it moved (hub_link_t). The real sender MAC is
//    inside, checked by the tag. A forged, altered or replayed report is
//    refused. Scores are relative to the board's own thresholds: 50 = at the
//    threshold, capped at 255.
enum { REP_MOTION = 1, REP_CALIBRATED = 2, REP_WEAK = 4 };
typedef struct {
    uint32_t seq;               // 24 bits, +1 per report; with the boot counter it is the nonce
    uint8_t  flags;             // REP_*
    uint8_t  k_short, k_long;   // score / threshold x50
    uint8_t  acks;              // router ACKs/s (capped)
    int8_t   rssi;              // the router link's signal, dBm
    uint8_t  n_dev;             // device (and board) links it pokes
    uint32_t up_s;              // seconds since it started
} sat_report_t;

typedef struct {
    uint8_t mac[6], bssid[6];   // the sender (its real MAC), its router
    int ch;                     // the sender's own channel
    int version;
    bool sealed;                // a report: hello_open() reads it
    uint32_t boot, seq;
} hello_t;

static inline uint8_t rep_k(float score, float threshold) {
    if (!(threshold > 0) || !(score > 0)) return 0;
    float k = score / threshold * 50.0f;
    return k >= 255.0f ? 255 : (uint8_t)k;
}

// Nonce = sender MAC, boot counter, seq (13 bytes): unique per report for one key
static inline void hello_nonce(const uint8_t *f, uint8_t n[CCM_NONCE]) { memcpy(n, f + 40, CCM_NONCE); }
static inline void hello_aad(const uint8_t *f, uint8_t a[HELLO_AAD_LEN]) { memcpy(a, f + 24, HELLO_AAD_LEN); }

static inline void hello_head(uint8_t *out, const uint8_t *to, const uint8_t ta[6], const uint8_t *bssid, int ch) {
    out[0] = 0xD0;                                       // management, action
    if (to) memcpy(out + 4, to, 6); else memset(out + 4, 0xFF, 6);
    memcpy(out + 10, ta, 6);
    if (to) memcpy(out + 16, to, 6); else memset(out + 16, 0xFF, 6);
    memcpy(out + 24, HELLO_MAGIC, sizeof HELLO_MAGIC);
    out[33] = (uint8_t)ch;
    if (bssid) memcpy(out + 34, bssid, 6);
}

// The hello: broadcast, from our real MAC. Returns its length.
static inline int build_hello(uint8_t out[HELLO_V1_LEN], const uint8_t me[6], const uint8_t *bssid, int ch) {
    memset(out, 0, HELLO_V1_LEN);
    hello_head(out, NULL, me, bssid, ch);
    out[32] = 1;
    return HELLO_V1_LEN;
}

// The report to `hub`, sealed with our key. Returns its length.
static inline int build_report(uint8_t out[HELLO_LEN], const uint8_t me[6], const uint8_t hub[6],
                               const uint8_t *bssid, int ch, const sat_report_t *r, uint32_t boot,
                               aes_block_fn enc, void *key) {
    uint8_t ta[6];
    memset(out, 0, HELLO_LEN);
    poke_ta(me, REPORT_NODE, ta);
    hello_head(out, hub, ta, bssid, ch);
    out[32] = HELLO_VERSION;
    memcpy(out + 40, me, 6);
    for (int i = 0; i < 4; i++) out[46 + i] = (uint8_t)(boot >> (8 * i));
    for (int i = 0; i < 3; i++) out[50 + i] = (uint8_t)(r->seq >> (8 * i));
    uint8_t m[REPORT_LEN] = { r->flags, r->k_short, r->k_long, r->acks, (uint8_t)r->rssi, r->n_dev,
                              (uint8_t)r->up_s, (uint8_t)(r->up_s >> 8), (uint8_t)(r->up_s >> 16),
                              (uint8_t)(r->up_s >> 24) };
    uint8_t n[CCM_NONCE], a[HELLO_AAD_LEN];
    hello_nonce(out, n);
    hello_aad(out, a);
    ccm_seal(enc, key, n, a, HELLO_AAD_LEN, m, REPORT_LEN, out + 53);
    return HELLO_LEN;
}

// f = frame without FCS. True if it is a hello or a report (any version).
// Reports of older versions (2 sent them in the clear, 3 broadcast them) are not used.
static inline bool parse_hello(const uint8_t *f, int len, hello_t *h) {
    if (!f || len < HELLO_V1_LEN || f[0] != 0xD0 ||
        memcmp(f + 24, HELLO_MAGIC, sizeof HELLO_MAGIC) != 0 || f[32] < 1 || (f[10] & 0x01))
        return false;
    memset(h, 0, sizeof *h);
    memcpy(h->mac, f + 10, 6);
    memcpy(h->bssid, f + 34, 6);
    h->ch = f[33];
    h->version = f[32];
    if (f[32] >= 4 && len >= HELLO_LEN && !(f[40] & 0x01)) {
        h->sealed = true;
        memcpy(h->mac, f + 40, 6);
        h->boot = (uint32_t)f[46] | (uint32_t)f[47] << 8 | (uint32_t)f[48] << 16 | (uint32_t)f[49] << 24;
        h->seq = (uint32_t)f[50] | (uint32_t)f[51] << 8 | (uint32_t)f[52] << 16;
    }
    return true;
}

// The sealed part of a report (f, at least HELLO_LEN bytes).
// False if the key differs or anything was changed.
static inline bool hello_open(const uint8_t *f, aes_block_fn enc, void *key, sat_report_t *r) {
    uint8_t n[CCM_NONCE], a[HELLO_AAD_LEN], m[REPORT_LEN];
    hello_nonce(f, n);
    hello_aad(f, a);
    if (!ccm_open(enc, key, n, a, HELLO_AAD_LEN, f + 53, REPORT_LEN, m)) return false;
    r->seq = (uint32_t)f[50] | (uint32_t)f[51] << 8 | (uint32_t)f[52] << 16;
    r->flags = m[0]; r->k_short = m[1]; r->k_long = m[2]; r->acks = m[3];
    r->rssi = (int8_t)m[4]; r->n_dev = m[5];
    r->up_s = (uint32_t)m[6] | (uint32_t)m[7] << 8 | (uint32_t)m[8] << 16 | (uint32_t)m[9] << 24;
    return true;
}

// ---------- Finding the hub's channel ----------
// Reports go out on the channel the hub was last found on (saved), first our
// own. The hub's radio ACKs each one; after HUB_MISS_MAX (5) reports in a row
// without an ACK, each further report tries the next channel until one is
// answered (13 channels: ~7 s at 2 reports/s). An ACK is sometimes missed
// (about 1 in 4 seen) while the report arrived, so one miss means little.
#define HUB_MISS_MAX 5
typedef struct { int ch; int miss; bool search; } hub_link_t;

static inline int hub_next_ch(hub_link_t *h, int own_ch, int max_ch) {
    if (h->ch < 1 || h->ch > max_ch) h->ch = own_ch >= 1 && own_ch <= max_ch ? own_ch : 1;
    return h->ch;
}

// After a report on h->ch: true when a search just found the hub (save h->ch)
static inline bool hub_result(hub_link_t *h, bool acked, int max_ch) {
    if (acked) {
        bool found = h->search;
        h->miss = 0;
        h->search = false;
        return found;
    }
    if (++h->miss >= HUB_MISS_MAX) {
        h->search = true;
        h->miss = HUB_MISS_MAX - 1;                // every further miss moves on one channel
        h->ch = h->ch % max_ch + 1;
    }
    return false;
}

// Replays: a recorded hello sent again must not count. The boot counter is
// saved on each board and goes up at every start, so (boot, seq) only ever
// grows: per board, only a report newer than the last one accepted passes.
// The hub saves each board's boot (replay_save), so after the hub restarts a
// recording from an earlier boot is still refused. Called only for reports
// whose seal opened (the counters are checked by the tag).
static inline bool replay_newer(uint32_t boot, uint32_t seq, uint32_t last_boot, uint32_t last_seq) {
    return boot > last_boot || (boot == last_boot && seq > last_seq);
}

// ---------- Keys (from the PC, over USB) ----------
//   KEY,<32 hex>           this board's own key (seals its reports)
//   PEERKEY,<mac>,<32 hex> another board's key (the hub opens its reports); 8 at most
//   PEERKEY,<mac>,clear    forget one;  PEERKEY,clear  forget all
typedef struct {
    uint8_t  mac[6];
    uint8_t  key[16];
    uint32_t boot, seq;         // the newest report accepted from it
    bool     used;
} peer_key_t;
typedef struct { peer_key_t k[MAX_PEERS]; } peer_keys_t;

static inline int peer_key_find(const peer_keys_t *t, const uint8_t mac[6]) {
    for (int i = 0; i < MAX_PEERS; i++)
        if (t->k[i].used && memcmp(t->k[i].mac, mac, 6) == 0) return i;
    return -1;
}

// Adds or replaces a board's key. A new key starts its replay counters over.
// Returns the slot, or -1 when 8 other boards already have keys.
static inline int peer_key_set(peer_keys_t *t, const uint8_t mac[6], const uint8_t key[16]) {
    int i = peer_key_find(t, mac);
    if (i >= 0 && memcmp(t->k[i].key, key, 16) == 0) return i;
    for (int j = 0; i < 0 && j < MAX_PEERS; j++)
        if (!t->k[j].used) i = j;
    if (i < 0) return -1;
    memset(&t->k[i], 0, sizeof t->k[i]);
    memcpy(t->k[i].mac, mac, 6);
    memcpy(t->k[i].key, key, 16);
    t->k[i].used = true;
    return i;
}

// A MAC at p, anything may follow it
static inline bool parse_mac_at(const char *p, uint8_t mac[6]) {
    for (int i = 0; i < 6; i++) {
        int hi = hexval(p[3 * i]), lo = hi < 0 ? -1 : hexval(p[3 * i + 1]);
        if (hi < 0 || lo < 0 || (i < 5 && p[3 * i + 2] != ':')) return false;
        mac[i] = (uint8_t)(hi << 4 | lo);
    }
    return !(mac[0] & 1);
}

static inline bool parse_hex16(const char *p, uint8_t key[16]) {
    uint8_t any = 0;
    for (int i = 0; i < 16; i++) {
        int hi = hexval(p[2 * i]), lo = hi < 0 ? -1 : hexval(p[2 * i + 1]);
        if (hi < 0 || lo < 0) return false;
        key[i] = (uint8_t)(hi << 4 | lo);
        any |= key[i];
    }
    return any != 0 && (!p[32] || p[32] == '\r' || p[32] == '\n');   // all zero: never a real key
}

enum { PK_BAD = -1, PK_QUERY = 0, PK_SET, PK_CLEAR_ONE, PK_CLEAR_ALL };
static inline int parse_peerkey(const char *line, uint8_t mac[6], uint8_t key[16]) {
    if (strncmp(line, "PEERKEY", 7) != 0) return PK_BAD;
    const char *p = line + 7;
    if (!*p || *p == '\r' || *p == '\n') return PK_QUERY;
    if (*p++ != ',') return PK_BAD;
    if (strncmp(p, "clear", 5) == 0 && (!p[5] || p[5] == '\r' || p[5] == '\n')) return PK_CLEAR_ALL;
    if (!parse_mac_at(p, mac) || p[17] != ',') return PK_BAD;
    p += 18;
    if (strncmp(p, "clear", 5) == 0 && (!p[5] || p[5] == '\r' || p[5] == '\n')) return PK_CLEAR_ONE;
    return parse_hex16(p, key) ? PK_SET : PK_BAD;
}

// HUB,<mac>: send reports to this board (saved); HUB,clear: send none; HUB: report it
enum { HUB_BAD = -1, HUB_QUERY = 0, HUB_SET, HUB_CLEAR };
static inline int parse_hub(const char *line, uint8_t mac[6]) {
    if (strncmp(line, "HUB", 3) != 0) return HUB_BAD;
    const char *p = line + 3;
    if (!*p || *p == '\r' || *p == '\n') return HUB_QUERY;
    if (*p++ != ',') return HUB_BAD;
    if (strncmp(p, "clear", 5) == 0 && (!p[5] || p[5] == '\r' || p[5] == '\n')) return HUB_CLEAR;
    if (!parse_mac_at(p, mac) || (p[17] && p[17] != '\r' && p[17] != '\n')) return HUB_BAD;
    return HUB_SET;
}

static inline int parse_key(const char *line, uint8_t key[16]) {
    if (strncmp(line, "KEY", 3) != 0) return KEY_BAD;
    const char *p = line + 3;
    if (!*p || *p == '\r' || *p == '\n') return KEY_QUERY;
    if (*p++ != ',') return KEY_BAD;
    if (strncmp(p, "clear", 5) == 0 && (!p[5] || p[5] == '\r' || p[5] == '\n')) return KEY_CLEAR;
    return parse_hex16(p, key) ? KEY_SET : KEY_BAD;
}

typedef struct {
    uint8_t  mac[6];
    uint8_t  bssid[6];          // the router it watches
    int      rssi_x16;          // average, x16
    uint32_t n;                 // hellos heard
    uint32_t last_ms;
    int      ch;
    bool     used;
} peer_t;

typedef struct { peer_t p[MAX_PEERS]; } peer_list_t;

// A hello was heard. Returns the entry, or -1 (the list is full of live peers).
static inline int peer_note(peer_list_t *l, const uint8_t mac[6], const uint8_t bssid[6],
                            int ch, int rssi, uint32_t now_ms) {
    int k = -1;
    for (int i = 0; i < MAX_PEERS && k < 0; i++)
        if (l->p[i].used && memcmp(l->p[i].mac, mac, 6) == 0) k = i;
    for (int i = 0; i < MAX_PEERS && k < 0; i++)
        if (!l->p[i].used || (int32_t)(now_ms - l->p[i].last_ms) > PEER_GONE_MS) {
            k = i;
            memset(&l->p[i], 0, sizeof l->p[i]);
        }
    if (k < 0) return -1;
    peer_t *e = &l->p[k];
    if (!e->used) { memcpy(e->mac, mac, 6); e->used = true; }
    memcpy(e->bssid, bssid, 6);
    e->ch = ch;
    e->rssi_x16 = e->n ? e->rssi_x16 + (rssi * 16 - e->rssi_x16) / 4 : rssi * 16;
    e->n++;
    e->last_ms = now_ms;
    return k;
}

// The live peer with this MAC, or -1
static inline int peer_find(const peer_list_t *l, const uint8_t *mac, uint32_t now_ms) {
    for (int i = 0; i < MAX_PEERS; i++)
        if (l->p[i].used && (int32_t)(now_ms - l->p[i].last_ms) <= PEER_GONE_MS &&
            memcmp(l->p[i].mac, mac, 6) == 0)
            return i;
    return -1;
}

// True if `addr` is one of a known peer's made-up poke addresses. Its pokes
// to our router look like a device talking to it; they must not be listed
// as devices (they'd never answer a poke).
static inline bool peer_poke_addr(const peer_list_t *l, const uint8_t *addr) {
    for (int i = 0; i < MAX_PEERS; i++)
        if (l->p[i].used && poke_ta_node(l->p[i].mac, addr) >= 0) return true;
    return false;
}

// Drops the devices that are really a peer's poke addresses (heard before its hello)
static inline void seen_forget_peer(seen_list_t *s, const uint8_t peer[6]) {
    for (int i = 0; i < MAX_SEEN; i++)
        if (s->d[i].used && poke_ta_node(peer, s->d[i].mac) >= 0) s->d[i].used = false;
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
    bool keep_far;              // APs chosen one by one: never skipped as "far" (set at boot)
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
    return !g->keep_far && i < MAX_APS && best_x16 && g->rssi_x16[i] &&
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

// Base64 (standard alphabet, '=' padding). Returns chars written (excluding
// NUL), or -1 if it doesn't fit.
static inline int b64_encode(const uint8_t *in, int n, char *out, size_t cap) {
    static const char T[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    if (n < 0 || (size_t)(n + 2) / 3 * 4 + 1 > cap) return -1;
    int o = 0;
    for (int i = 0; i < n; i += 3) {
        uint32_t v = (uint32_t)in[i] << 16;
        if (i + 1 < n) v |= (uint32_t)in[i + 1] << 8;
        if (i + 2 < n) v |= in[i + 2];
        out[o++] = T[v >> 18];
        out[o++] = T[(v >> 12) & 63];
        out[o++] = i + 1 < n ? T[(v >> 6) & 63] : '=';
        out[o++] = i + 2 < n ? T[v & 63] : '=';
    }
    out[o] = '\0';
    return o;
}

static inline int b64_val(int c) {
    if (c >= 'A' && c <= 'Z') return c - 'A';
    if (c >= 'a' && c <= 'z') return c - 'a' + 26;
    if (c >= '0' && c <= '9') return c - '0' + 52;
    return c == '+' ? 62 : c == '/' ? 63 : -1;
}

// Decodes up to the end of the line. Returns the bytes written, or -1 if it
// isn't base64 or doesn't fit.
static inline int b64_decode(const char *in, uint8_t *out, int cap) {
    int n = 0, bits = 0, chars = 0, pad = 0;
    uint32_t acc = 0;
    for (; b64_val((unsigned char)*in) >= 0; in++, chars++) {
        acc = acc << 6 | (uint32_t)b64_val((unsigned char)*in);
        bits += 6;
        if (bits >= 8) {
            bits -= 8;
            if (n >= cap) return -1;
            out[n++] = (uint8_t)(acc >> bits);
        }
    }
    for (; *in == '='; in++) pad++;
    if (pad > 2 || (chars + pad) % 4 || chars % 4 == 1) return -1;
    if (*in != '\0' && *in != '\n' && *in != '\r') return -1;
    return n;
}

// The LLTF's subcarriers that carry signal: 1..26 and 38..63 (0 is DC, 27..37
// are guards; the radio reports those as zeros)
#define CSI_LLTF_LEN   128                  // 64 subcarriers x [imag, real]
#define CSI_PACKED_LEN 104                  // the 52 used ones
static inline bool lltf_used(int k) { return k >= 1 && k < 64 && !(k >= 27 && k <= 37); }

// The used subcarriers of a 128-byte LLTF. False (nothing to gain or lossy)
// unless len is 128 and the unused subcarriers are all zero.
static inline bool lltf_pack(const int8_t *buf, int len, int8_t out[CSI_PACKED_LEN]) {
    if (len != CSI_LLTF_LEN) return false;
    int j = 0;
    for (int k = 0; k < 64; k++) {
        if (lltf_used(k)) { out[j++] = buf[2 * k]; out[j++] = buf[2 * k + 1]; }
        else if (buf[2 * k] || buf[2 * k + 1]) return false;
    }
    return true;
}

static inline void lltf_unpack(const int8_t in[CSI_PACKED_LEN], int8_t out[CSI_LLTF_LEN]) {
    int j = 0;
    for (int k = 0; k < 64; k++) {
        bool u = lltf_used(k);
        out[2 * k] = u ? in[j++] : 0;
        out[2 * k + 1] = u ? in[j++] : 0;
    }
}

// "CSI,ts,src,kind,ch,rssi,sig_mode,cwb,len,<base64>\n"   kind: 0 = AP frame, 1 = ACK
// The data is len int8 values, [imag, real] per subcarrier, in base64. len 104
// = a 128-byte LLTF without its 12 unused subcarriers (lltf_pack); any other
// len is the bytes as received. About 175 bytes a line; the USB port carries
// about 70 kB/s, and the first format (decimal text, ~380 bytes, still read by
// the PC tools) filled it at 200 lines a second.
// Returns chars written (excluding NUL), or -1 if it doesn't fit.
static inline int format_csi_line(char *out, size_t cap, const csi_pkt_t *p) {
    int8_t packed[CSI_PACKED_LEN];
    const int8_t *data = p->buf;
    int len = p->len;
    if (lltf_pack(p->buf, p->len, packed)) { data = packed; len = CSI_PACKED_LEN; }
    int n = snprintf(out, cap, "CSI,%" PRId64 ",%u,%u,%u,%d,%u,%u,%d,",
                     p->ts, (unsigned)p->src, (unsigned)p->kind, (unsigned)p->ch,
                     (int)p->rssi, (unsigned)p->sig_mode, (unsigned)p->cwb, len);
    if (n < 0 || (size_t)n >= cap) return -1;
    int k = b64_encode((const uint8_t *)data, len, out + n, cap - (size_t)n);
    if (k < 0 || (size_t)(n + k) + 2 > cap) return -1;
    n += k;
    out[n++] = '\n';
    out[n] = '\0';
    return n;
}

// The data field of a CSI line (after "len,"), in either format: base64, or
// the first format's decimal numbers. Fills buf (cap CSI_MAX_LEN) and returns
// the length in bytes, a packed LLTF already unpacked to 128; -1 if malformed.
static inline int parse_csi_data(const char *p, int len, int8_t *buf) {
    if (len < 0 || len > CSI_MAX_LEN) return -1;
    if (!strchr(p, ' ')) {
        int8_t raw[CSI_MAX_LEN];
        if (b64_decode(p, (uint8_t *)raw, CSI_MAX_LEN) != len) return -1;
        if (len == CSI_PACKED_LEN) { lltf_unpack(raw, buf); return CSI_LLTF_LEN; }
        memcpy(buf, raw, (size_t)len);
        return len;
    }
    for (int got = 0; got < len; got++) {
        char *e;
        long v = strtol(p, &e, 10);
        if (e == p || v < -128 || v > 127) return -1;
        buf[got] = (int8_t)v;
        p = e;
    }
    return len;
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
    float   fast_rise_max;          // while the ring fills: at most this x the first floor
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
    c->fast_rise_max = 1.5f;
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
    float    floor0;                        // first floor after (re)calibrating
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

    // A lasting RSSI step means the board (or the AP) was moved: recalibrate.
    // AP links only: a device slot's level also steps when the slot is given
    // to another device, or the device itself is carried around
    if (node < MAX_APS && ts - s->rssi_t0 >= c->rssi_min_age_us &&
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
// ("fast") the floor follows directly but stays under fast_rise_max x the
// first one (someone moving around right after calibrating can't lift it to
// their own level); afterwards ("track") it may fall immediately but rise at
// most floor_up_per_s per second.
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
        if (d->phase == MD_CAL_COLLECT) d->floor0 = md_clampf(q, c->floor_lo, c->floor_hi);
        else if (q > d->floor0 * c->fast_rise_max) q = d->floor0 * c->fast_rise_max;
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
