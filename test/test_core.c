// Host-side tests for csi_core.h. From the repo root: make test-c
// (or: gcc -std=c11 -Wall -Wextra -g -fsanitize=address,undefined
//      -Imain test/test_core.c -o test_core && ./test_core)
#include <stdlib.h>
#include "csi_core.h"

static int passes = 0, fails = 0;
#define CHECK(c) do { if (c) passes++; else { fails++; \
    printf("FAIL line %d: %s\n", __LINE__, #c); } } while (0)

static const uint8_t AP1[6] = {0xaa,0xbb,0xcc,0xdd,0xee,0xf0};
static const uint8_t AP2[6] = {0xaa,0xbb,0xcc,0xdd,0xee,0xf1};

typedef struct { uint8_t b[512]; int n; } frame_t;

static void frame_start(frame_t *f, uint8_t fc0, const uint8_t bssid[6]) {
    memset(f, 0, sizeof *f);
    f->b[0] = fc0;
    memset(f->b + 4, 0xff, 6);     // addr1 broadcast
    memcpy(f->b + 10, bssid, 6);   // addr2
    memcpy(f->b + 16, bssid, 6);   // addr3 = BSSID
    f->b[32] = 0x64;               // beacon interval 100 TU
    f->b[34] = 0x01;               // capability: ESS
    f->n = 36;
}
static void add_ie(frame_t *f, uint8_t id, uint8_t len, const void *v, int actual) {
    f->b[f->n++] = id;
    f->b[f->n++] = len;
    memcpy(f->b + f->n, v, actual);
    f->n += actual;
}
#define IE(f, id, s) add_ie(f, id, sizeof(s) - 1, s, sizeof(s) - 1)

// Copy to an exact-size heap buffer so ASan catches any over-read
static beacon_info_t parse(const frame_t *f) {
    uint8_t *p = malloc(f->n ? f->n : 1);
    memcpy(p, f->b, f->n);
    beacon_info_t b;
    parse_beacon(p, f->n, "MyNetwork", &b);
    free(p);
    return b;
}

int main(void) {
    frame_t f; beacon_info_t b;

    // Normal beacon
    frame_start(&f, 0x80, AP1);
    IE(&f, 0, "MyNetwork"); IE(&f, 1, "\x82\x84\x8b\x96"); IE(&f, 3, "\x06");
    b = parse(&f);
    CHECK(b.ok && b.ssid_match); CHECK(b.ds_ch == 6);
    CHECK(b.csa_ch == 0); CHECK(memcmp(b.bssid, AP1, 6) == 0);
    CHECK(b.ssid_len == 9 && memcmp(b.ssid, "MyNetwork", 9) == 0);   // name kept for SCAN

    // SSID mismatches: different, longer, shorter, hidden
    frame_start(&f, 0x80, AP1); IE(&f, 0, "Neighbor");   b = parse(&f); CHECK(b.ok && !b.ssid_match);
    frame_start(&f, 0x80, AP1); IE(&f, 0, "MyNetwork2"); b = parse(&f); CHECK(!b.ssid_match);
    frame_start(&f, 0x80, AP1); IE(&f, 0, "MyNetwor");   b = parse(&f); CHECK(!b.ssid_match);
    frame_start(&f, 0x80, AP1); IE(&f, 0, ""); IE(&f, 3, "\x06");
    b = parse(&f); CHECK(!b.ssid_match && b.ds_ch == 6 && b.ssid_len == 0);

    // CSA: switch to ch 11 in 5 beacons
    frame_start(&f, 0x80, AP2);
    IE(&f, 0, "MyNetwork"); IE(&f, 3, "\x06"); IE(&f, 37, "\x01\x0b\x05");
    b = parse(&f);
    CHECK(b.ssid_match && b.csa_ch == 11 && b.csa_cnt == 5);
    CHECK(memcmp(b.bssid, AP2, 6) == 0);
    CHECK(csa_should_switch(11, 6, 13));
    CHECK(!csa_should_switch(6, 6, 13));    // same channel
    CHECK(!csa_should_switch(36, 6, 13));   // 5 GHz
    CHECK(!csa_should_switch(12, 6, 11));   // outside US range
    CHECK(!csa_should_switch(0, 6, 13));

    // Malformed CSA (too short) is ignored
    frame_start(&f, 0x80, AP1); IE(&f, 0, "MyNetwork"); IE(&f, 37, "\x01\x0b");
    b = parse(&f); CHECK(b.ssid_match && b.csa_ch == 0);

    // IE order doesn't matter
    frame_start(&f, 0x80, AP1); IE(&f, 3, "\x0b"); IE(&f, 0, "MyNetwork");
    b = parse(&f); CHECK(b.ssid_match && b.ds_ch == 11);

    // Truncated IEs: no over-read, partial IE ignored
    frame_start(&f, 0x80, AP1); IE(&f, 0, "MyNetwork"); add_ie(&f, 3, 10, "\x06", 1);
    b = parse(&f); CHECK(b.ssid_match && b.ds_ch == 0);
    frame_start(&f, 0x80, AP1); add_ie(&f, 0, 9, "My", 2);
    b = parse(&f); CHECK(b.ok && !b.ssid_match);
    frame_start(&f, 0x80, AP1); IE(&f, 0, "MyNetwork"); f.b[f.n++] = 3;   // lone ID byte
    b = parse(&f); CHECK(b.ssid_match && b.ds_ch == 0);

    // Not a beacon / too short / NULL / no IEs
    frame_start(&f, 0x40, AP1); IE(&f, 0, "MyNetwork"); b = parse(&f); CHECK(!b.ok);
    frame_start(&f, 0x50, AP1); IE(&f, 0, "MyNetwork"); b = parse(&f); CHECK(!b.ok);
    frame_start(&f, 0x80, AP1); f.n = 30; b = parse(&f); CHECK(!b.ok);
    parse_beacon(NULL, 100, "MyNetwork", &b); CHECK(!b.ok);
    frame_start(&f, 0x80, AP1); b = parse(&f); CHECK(b.ok && !b.ssid_match);

    // Node table
    node_table_t t = {0};
    CHECK(node_add(&t, AP1) == 0);
    CHECK(node_add(&t, AP1) == 0);           // dedupe
    CHECK(node_add(&t, AP2) == 1);
    CHECK(node_count(&t) == 2 && node_find(&t, AP2) == 1);
    uint8_t m[6] = {2, 0, 0, 0, 0, 0};
    CHECK(node_find(&t, m) == -1);
    for (int i = 0; i < MAX_APS; i++) { m[5] = (uint8_t)i; node_add(&t, m); }
    CHECK(node_count(&t) == MAX_APS);
    m[5] = 0xfe; CHECK(node_add(&t, m) == -1);   // full

    // Timer wraparound
    CHECK(elapsed_ms(5, 0xFFFFFFFBu) == 10);
    CHECK(deadline_passed(5, 0xFFFFFFFEu));
    CHECK(!deadline_passed(0xFFFFFFFEu, 5));
    CHECK(older_than(3000, 0, 2000));
    CHECK(!older_than(2000, 0, 2000));                     // exactly the limit
    CHECK(!older_than(100, 150, 2000));                    // stamped after `now`
    CHECK(older_than(2005, 0xFFFFFFFBu, 2000));            // across the wrap
    CHECK(!older_than(5, 0xFFFFFFFBu, 2000));

    // CSI line formatting
    static csi_pkt_t p;
    p.ts = 1234567890123LL; p.src = 1; p.kind = CSI_KIND_ACK; p.ch = 6; p.rssi = -42;
    p.sig_mode = 1; p.cwb = 0; p.len = 4;
    p.buf[0] = -128; p.buf[1] = 127; p.buf[2] = 0; p.buf[3] = -1;
    char line[2048];
    const char *want = "CSI,1234567890123,1,1,6,-42,1,0,4,-128 127 0 -1\n";
    int n = format_csi_line(line, sizeof line, &p);
    CHECK(n == (int)strlen(want) && strcmp(line, want) == 0);
    p.len = CSI_MAX_LEN; p.kind = 255; p.src = 255; p.ch = 255;  // worst case
    p.ts = INT64_MIN; p.rssi = -128; p.sig_mode = 255; p.cwb = 255;
    memset(p.buf, -128, sizeof p.buf);
    CHECK(format_csi_line(line, sizeof line, &p) > 0);
    CHECK(format_csi_line(line, 20, &p) == -1);                   // too small
    p.len = 0; CHECK(format_csi_line(line, sizeof line, &p) > 0);

    // ---------- Polite Wi-Fi poking ----------
    static const uint8_t ME[6] = {0x24,0x0a,0xc4,0x11,0x22,0x33};
    uint8_t nf[NULL_FRAME_LEN];
    build_null_frame(nf, AP1, ME);
    CHECK(nf[0] == 0x48 && nf[1] == 0x01);                 // null data, ToDS
    CHECK(nf[2] == 0 && nf[3] == 0);                       // duration (driver)
    CHECK(memcmp(nf + 4, AP1, 6) == 0);                    // RA = AP
    CHECK(memcmp(nf + 10, ME, 6) == 0);                    // TA = us
    CHECK(memcmp(nf + 16, AP1, 6) == 0);                   // BSSID
    CHECK(nf[22] == 0 && nf[23] == 0);                     // seq (driver)
    // Must not be mistaken for a beacon by our own sniffer
    parse_beacon(nf, NULL_FRAME_LEN, "MyNetwork", &b); CHECK(!b.ok);

    uint8_t ack[10] = {0xD4, 0, 0, 0};
    memcpy(ack + 4, ME, 6);
    CHECK(is_ack_to(ack, 10, ME));
    CHECK(!is_ack_to(ack, 9, ME));                         // truncated
    CHECK(!is_ack_to(ack, 10, AP1));                       // someone else's
    ack[0] = 0xC4; CHECK(!is_ack_to(ack, 10, ME));         // CTS, not ACK
    CHECK(!is_ack_to(NULL, 10, ME));

    // Made-up poke TA: never our MAC, unicast, one per node, reversible
    uint8_t ta0[6], ta1[6], ta7[6];
    poke_ta(ME, 0, ta0); poke_ta(ME, 1, ta1); poke_ta(ME, MAX_NODES - 1, ta7);
    CHECK(memcmp(ta0, ME, 6) != 0 && memcmp(ta0 + 1, ME + 1, 5) == 0);
    CHECK((ta0[0] & 0x01) == 0);                           // unicast
    CHECK((ta0[0] & 0x02) != (ME[0] & 0x02));              // local bit flipped
    CHECK(memcmp(ta0, ta1, 6) != 0);
    static const uint8_t TA1_VEC[6] = {0x26,0x0a,0xc4,0x11,0x22,0x34};  // also in test_probe_report.py
    CHECK(memcmp(ta1, TA1_VEC, 6) == 0);
    CHECK(poke_ta_node(ME, ta0) == 0 && poke_ta_node(ME, ta1) == 1);
    CHECK(poke_ta_node(ME, ta7) == MAX_NODES - 1);
    CHECK(poke_ta_node(ME, ME) == -1);                     // our own MAC is not a TA
    CHECK(poke_ta_node(ME, AP1) == -1);
    CHECK(poke_ta_node(ME, NULL) == -1);
    uint8_t tax[6]; memcpy(tax, ta0, 6); tax[5] = (uint8_t)(ta0[5] + MAX_NODES);
    CHECK(poke_ta_node(ME, tax) == -1);                    // beyond the node table
    static const uint8_t MEW[6] = {0x24,0x0a,0xc4,0x11,0x22,0xfe};
    uint8_t taw[6]; poke_ta(MEW, 3, taw);
    CHECK(taw[5] == 0x01 && poke_ta_node(MEW, taw) == 3);  // last-byte wrap
    static const uint8_t MEL[6] = {0x0a,0x0a,0xc4,0x11,0x22,0x33};  // already local
    uint8_t tal[6]; poke_ta(MEL, 0, tal);
    CHECK(memcmp(tal, MEL, 6) != 0 && (tal[0] & 0x01) == 0);
    // Poke frame carries the made-up TA
    build_null_frame(nf, AP1, ta0);
    CHECK(memcmp(nf + 10, ta0, 6) == 0);

    // Classifier: table holds AP1 (0) and AP2 (1). ACKs report mac 00:..:00.
    static const uint8_t Z[6] = {0};
    static const uint8_t BC[6] = {0xff,0xff,0xff,0xff,0xff,0xff};
    node_table_t ct = {0};
    node_add(&ct, AP1); node_add(&ct, AP2);
    poke_state_t pk = {-1, 0};
    int node = -9;
    const uint32_t WIN = 10000;
    CHECK(classify_csi(&ct, &pk, AP2, BC, ME, 1, 384, 100, WIN, &node) == CSI_KIND_AP && node == 1);
    CHECK(classify_csi(&ct, &pk, Z, ta1, ME, 0, 128, 100, WIN, &node) == CSI_DROP);  // no poke out

    pk.node = 1; pk.sent_us = 1000;
    node = -9;
    CHECK(classify_csi(&ct, &pk, Z, ta1, ME, 1, 384, 1200, WIN, &node) == CSI_DROP); // HT, not an ACK
    CHECK(classify_csi(&ct, &pk, Z, ta0, ME, 0, 128, 1200, WIN, &node) == CSI_DROP); // ACK for node 0
    CHECK(classify_csi(&ct, &pk, Z, ME, ME, 0, 128, 1200, WIN, &node) == CSI_DROP);  // to our real MAC
    static const uint8_t OTHER[6] = {0x20,0x4e,0xf6,0x8f,0xb1,0x17};
    CHECK(classify_csi(&ct, &pk, OTHER, OTHER, ME, 0, 128, 1200, WIN, &node) == CSI_DROP); // foreign
    static const uint8_t FOREIGN_RA[6] = {0xc4,0x38,0x75,0x85,0x70,0x8d};
    CHECK(classify_csi(&ct, &pk, Z, FOREIGN_RA, ME, 0, 128, 1200, WIN, &node) == CSI_DROP); // other's ACK
    CHECK(pk.node == 1 && node == -9);                                                     // still pending
    CHECK(classify_csi(&ct, &pk, Z, ta1, ME, 0, 128, 1200, WIN, &node) == CSI_KIND_ACK && node == 1);
    CHECK(pk.node == -1);                                                                  // consumed
    CHECK(classify_csi(&ct, &pk, Z, ta1, ME, 0, 128, 1300, WIN, &node) == CSI_DROP);     // 2nd ACK
    // Known AP frames still pass while a poke is pending, and don't consume it
    pk.node = 0; pk.sent_us = 2000;
    CHECK(classify_csi(&ct, &pk, AP1, BC, ME, 0, 128, 2100, WIN, &node) == CSI_KIND_AP && pk.node == 0);
    // A known node's frame to one of our poke addresses is its reply to a poke
    // (e.g. a deauth): counted apart, never streamed, and doesn't consume the poke
    CHECK(classify_csi(&ct, &pk, AP1, ta0, ME, 0, 128, 2100, WIN, &node) == CSI_REPLY && pk.node == 0);
    CHECK(classify_csi(&ct, &pk, AP2, ta1, ME, 0, 128, 2100, WIN, &node) == CSI_REPLY);
    CHECK(classify_csi(&ct, &pk, AP1, ME, ME, 0, 128, 2100, WIN, &node) == CSI_KIND_AP); // real MAC
    // Late ACK is dropped; on-time one passes
    CHECK(classify_csi(&ct, &pk, Z, ta0, ME, 0, 128, 2000 + WIN + 1, WIN, &node) == CSI_DROP);
    CHECK(classify_csi(&ct, &pk, Z, ta0, ME, 0, 128, 2000 + WIN, WIN, &node) == CSI_KIND_ACK);
    // CSI stamped before the poke (poke state updated mid-callback) is not an ACK
    pk.node = 0; pk.sent_us = 5000;
    CHECK(classify_csi(&ct, &pk, Z, ta0, ME, 0, 128, 4990, WIN, &node) == CSI_DROP);
    // Window across the 32-bit microsecond wrap (~71 min)
    pk.node = 0; pk.sent_us = 0xFFFFFF00u;
    CHECK(classify_csi(&ct, &pk, Z, ta0, ME, 0, 128, 0x100u, WIN, &node) == CSI_KIND_ACK);
    // Too-short CSI is not trusted
    pk.node = 0; pk.sent_us = 10;
    CHECK(classify_csi(&ct, &pk, Z, ta0, ME, 0, 64, 20, WIN, &node) == CSI_DROP);
    // NULL dmac never matches
    CHECK(classify_csi(&ct, &pk, Z, NULL, ME, 0, 128, 20, WIN, &node) == CSI_DROP);

    // Round-robin poke target: only nodes on the current channel
    int nch[MAX_NODES] = {6, 11};
    int rr = 0;
    poke_gate_t pg; gate_init(&pg);
    CHECK(next_poke_target(&ct, nch, 6, &rr, &pg, 0) == 0);
    CHECK(next_poke_target(&ct, nch, 6, &rr, &pg, 0) == 0);        // only one on ch 6
    nch[1] = 6;
    CHECK(next_poke_target(&ct, nch, 6, &rr, &pg, 0) == 1);
    CHECK(next_poke_target(&ct, nch, 6, &rr, &pg, 0) == 0);        // alternates
    CHECK(next_poke_target(&ct, nch, 1, &rr, &pg, 0) == -1);       // none on ch 1
    node_table_t empty = {0};
    rr = 0; CHECK(next_poke_target(&empty, nch, 6, &rr, &pg, 0) == -1);
    // A far AP (more than POKE_FAR_DB under the strongest) doesn't split the router's pokes
    gate_rssi(&pg, 0, -50); gate_rssi(&pg, 1, -79);
    rr = 0;
    { int g0 = 0; for (int j = 0; j < 20; j++) g0 += next_poke_target(&ct, nch, 6, &rr, &pg, 0) == 0;
      CHECK(g0 == 20); }
    CHECK(ap_is_far(&pg, 1, ap_best_rssi_x16(&ct, nch, 6, &pg)) && !ap_is_far(&pg, 0, ap_best_rssi_x16(&ct, nch, 6, &pg)));
    gate_init(&pg); gate_rssi(&pg, 0, -50); gate_rssi(&pg, 1, -60);   // close enough: shared
    { int g1 = 0; for (int j = 0; j < 20; j++) g1 += next_poke_target(&ct, nch, 6, &rr, &pg, 0) == 1;
      CHECK(g1 == 10); }
    pg.yield[0] = 0;                                         // strongest goes silent: the other isn't "far" any more
    { int g1 = 0; for (int j = 0; j < 20; j++) g1 += next_poke_target(&ct, nch, 6, &rr, &pg, 0) == 1;
      CHECK(g1 >= 19); }
    gate_init(&pg);

    // Device slots: nodes MAX_APS.., found by MAC, poked like APs
    {
        static const uint8_t TV[6] = {0x3c,0x22,0xfb,0x01,0x02,0x03};
        static const uint8_t PLUG[6] = {0x50,0x02,0x91,0x0a,0x0b,0x0c};
        node_table_t dt = {0};
        node_add(&dt, AP1);
        bool is_new = false;
        int k = dev_slot_for(&dt, TV, &is_new);
        CHECK(k == 0 && is_new);
        CHECK(node_find(&dt, TV) == -1);                       // not live yet
        dev_set(&dt, k, TV);
        CHECK(node_find(&dt, TV) == DEV_NODE(0) && !node_is_ap(DEV_NODE(0)));
        CHECK(dev_slot_for(&dt, TV, &is_new) == 0 && !is_new); // same device, same slot
        CHECK(dev_slot_for(&dt, PLUG, &is_new) == 1 && is_new);
        for (int j = 1; j < MAX_DEVS; j++) { uint8_t x[6] = {2,0,0,0,0,(uint8_t)j}; dev_set(&dt, j, x); }
        CHECK(dev_slot_for(&dt, PLUG, &is_new) == -1);         // all slots taken
        dev_clear(&dt, 2);
        CHECK(dev_slot_for(&dt, PLUG, &is_new) == 2);
        CHECK(node_count(&dt) == 1);                           // APs unaffected
        // An ACK from the TV's made-up poke address is classified as the TV
        uint8_t tat[6]; poke_ta(ME, DEV_NODE(0), tat);
        CHECK(poke_ta_node(ME, tat) == DEV_NODE(0));
        poke_state_t dpk = {DEV_NODE(0), 100};
        int dn = -9;
        CHECK(classify_csi(&dt, &dpk, Z, tat, ME, 0, 128, 150, WIN, &dn) == CSI_KIND_ACK
              && dn == DEV_NODE(0));
        CHECK(classify_csi(&dt, &dpk, TV, BC, ME, 1, 384, 200, WIN, &dn) == CSI_KIND_AP
              && dn == DEV_NODE(0));                           // TV's own frames
        // Poke rotation covers the AP and the TV on the same channel
        int dch[MAX_NODES] = {0};
        dch[0] = 6; dch[DEV_NODE(0)] = 6;
        dt.dev_mask = 1;
        poke_gate_t dg; gate_init(&dg);
        // APs and devices are picked from separate slots: the TV never takes
        // the router's pokes, and the router never fills the TV's
        int drr[2] = {0, 0}, got[MAX_NODES] = {0};
        for (int j = 0; j < 100; j++) got[next_poke_target(&dt, dch, 6, &drr[0], &dg, 0)]++;
        CHECK(got[0] == 100);
        memset(got, 0, sizeof got);
        for (int j = 0; j < 100; j++) got[next_poke_target(&dt, dch, 6, &drr[1], &dg, 1)]++;
        CHECK(got[DEV_NODE(0)] == 100);
        dev_set(&dt, 1, PLUG); dch[DEV_NODE(1)] = 6;
        memset(got, 0, sizeof got);
        for (int j = 0; j < 100; j++) got[next_poke_target(&dt, dch, 6, &drr[1], &dg, 1)]++;
        CHECK(got[DEV_NODE(0)] == 50 && got[DEV_NODE(1)] == 50);   // devices share their slot
        dev_clear(&dt, 1);
        // A silent device gets trial pokes only; no strongest-fallback for devices
        dg.yield[DEV_NODE(0)] = 0;
        int n_none = 0, n_tv = 0;
        for (int j = 0; j < 10 * POKE_TRIAL_EVERY; j++) {
            int x = next_poke_target(&dt, dch, 6, &drr[1], &dg, 1);
            n_none += x < 0; n_tv += x == DEV_NODE(0);
        }
        CHECK(n_tv == 10 && n_none == 10 * (POKE_TRIAL_EVERY - 1));
        dg.yield[DEV_NODE(0)] = -1;
        dev_clear(&dt, 0);
        CHECK(next_poke_target(&dt, dch, 6, &drr[1], &dg, 1) == -1);  // removed: no more pokes
        CHECK(next_poke_target(&dt, dch, 6, &drr[0], &dg, 0) == 0);
        // gate_reset clears a reused slot's verdicts
        dg.yield[DEV_NODE(1)] = 3; gate_rssi(&dg, DEV_NODE(1), -90);
        gate_reset(&dg, DEV_NODE(1));
        CHECK(gate_state(&dg, DEV_NODE(1)) == GATE_OK && dg.rssi_x16[DEV_NODE(1)] == 0);

        // Device list from data frames on our network
        seen_list_t sl; memset(&sl, 0, sizeof sl);
        uint8_t df[24] = {0};
        df[0] = 0x88; df[1] = 0x01;                            // QoS data, ToDS
        memcpy(df + 4, AP1, 6); memcpy(df + 10, PLUG, 6); memcpy(df + 16, AP1, 6);
        int e = seen_note(&sl, &dt, df, 24, -55, 1000);
        CHECK(e >= 0 && memcmp(sl.d[e].mac, PLUG, 6) == 0 && sl.d[e].n_tx == 1
              && sl.d[e].rssi_x16 / 16 == -55);
        df[1] = 0x02;                                          // FromDS: AP -> device
        memcpy(df + 4, PLUG, 6); memcpy(df + 10, AP1, 6);
        CHECK(seen_note(&sl, &dt, df, 24, -40, 1100) == e && sl.d[e].n_rx == 1
              && sl.d[e].rssi_x16 / 16 == -55 && sl.d[e].last_ms == 1100); // AP's RSSI ignored
        memcpy(df + 4, BC, 6);
        CHECK(seen_note(&sl, &dt, df, 24, -40, 1100) == -1);   // broadcast
        df[1] = 0x01; memcpy(df + 4, AP2, 6); memcpy(df + 10, PLUG, 6);
        CHECK(seen_note(&sl, &dt, df, 24, -40, 1100) == -1);   // AP2 isn't ours here
        df[1] = 0x03; memcpy(df + 4, AP1, 6);
        CHECK(seen_note(&sl, &dt, df, 24, -40, 1100) == -1);   // mesh backhaul
        df[1] = 0x01; df[0] = 0x80;
        CHECK(seen_note(&sl, &dt, df, 24, -40, 1100) == -1);   // beacon, not data
        df[0] = 0x88;
        CHECK(seen_note(&sl, &dt, df, 23, -40, 1100) == -1);   // truncated
        CHECK(seen_note(&sl, &dt, NULL, 24, -40, 1100) == -1);
        for (int j = 0; j < MAX_SEEN + 5; j++) {               // fills, then evicts stalest
            uint8_t x[6] = {0x02,0,0,0,1,(uint8_t)j};
            memcpy(df + 10, x, 6);
            CHECK(seen_note(&sl, &dt, df, 24, -60, 2000 + j) >= 0);
        }
        int listed = 0, plug_listed = 0;
        for (int j = 0; j < MAX_SEEN; j++) {
            listed += sl.d[j].used;
            plug_listed |= sl.d[j].used && memcmp(sl.d[j].mac, PLUG, 6) == 0;
        }
        CHECK(listed == MAX_SEEN && !plug_listed);             // PLUG was the stalest
        uint8_t rnd[6] = {0xda,0xa1,0x19,0,0,1};
        CHECK(mac_is_random(rnd) && !mac_is_random(PLUG));

        // Commands
        uint8_t cm[6];
        char cb_[64], up_[32];
        #define CMD(fmt_tail, pre, m) (snprintf(cb_, sizeof cb_, "%s%02x:%02x:%02x:%02x:%02x:%02x" fmt_tail, \
            pre, (m)[0], (m)[1], (m)[2], (m)[3], (m)[4], (m)[5]), cb_)
        CHECK(parse_cmd(CMD("", "POKE,", PLUG), cm) == CMD_POKE && memcmp(cm, PLUG, 6) == 0);
        for (char *q = strcpy(up_, CMD("\r\n", "POKE,", PLUG)); *q; q++)
            if (*q >= 'a' && *q <= 'f') *q -= 32;              // upper-case hex
        CHECK(parse_cmd(up_, cm) == CMD_POKE && memcmp(cm, PLUG, 6) == 0);
        CHECK(parse_cmd(CMD("", "UNPOKE,", PLUG), cm) == CMD_UNPOKE);
        CHECK(parse_cmd("UNPOKE,all", cm) == CMD_UNPOKE_ALL);
        CHECK(parse_cmd("UNPOKE,allx", cm) == CMD_NONE);
        CHECK(parse_cmd("CAL", cm) == CMD_CAL && parse_cmd("CAL\r\n", cm) == CMD_CAL);
        CHECK(parse_cmd("CALX", cm) == CMD_NONE);
        int hz, vol;
        CHECK(parse_beep("BEEP", &hz, &vol) == 300 && hz == 0 && vol == 0);
        CHECK(parse_beep("BEEP,150\r\n", &hz, &vol) == 150 && hz == 0);
        CHECK(parse_beep("BEEP,500,3200", &hz, &vol) == 500 && hz == 3200 && vol == 0);
        CHECK(parse_beep("BEEP,500,3200,20", &hz, &vol) == 500 && hz == 3200 && vol == 20);
        CHECK(parse_beep("BEEP,500,3200,0", &hz, &vol) == -1 && parse_beep("BEEP,500,3200,101", &hz, &vol) == -1);
        CHECK(parse_beep("BEEP,0", &hz, &vol) == -1 && parse_beep("BEEP,3001", &hz, &vol) == -1);
        CHECK(parse_beep("BEEP,", &hz, &vol) == -1 && parse_beep("BEEP,12x", &hz, &vol) == -1);
        CHECK(parse_beep("BEEP,500,50", &hz, &vol) == -1 && parse_beep("BEEP,500,", &hz, &vol) == -1);
        CHECK(parse_beep("BEEP,1,200,3,4", &hz, &vol) == -1 && parse_beep("BEEPX", &hz, &vol) == -1);
        CHECK(parse_beep("CAL", &hz, &vol) == -1);
        char nm[33];
        CHECK(parse_cmd("SCAN", cm) == CMD_SCAN && parse_cmd("SCAN\r\n", cm) == CMD_SCAN && parse_cmd("SCANX", cm) == CMD_NONE);
        CHECK(parse_use("USE,My Net, 2.4G\r\n", nm) == 12 && strcmp(nm, "My Net, 2.4G") == 0);
        CHECK(parse_use("USE,", nm) == -1 && parse_use("USE,123456789012345678901234567890123", nm) == -1);
        CHECK(parse_use("USE,12345678901234567890123456789012", nm) == 32 && parse_cmd("USE,x", cm) == CMD_NONE);
        {   // nearby networks: one entry per name, strongest kept, its APs listed, hidden skipped
            net_list_t nl; memset(&nl, 0, sizeof nl);
            const uint8_t a1[] = "Alpha", b1[] = "Beta", z[4] = {0};
            uint8_t ap1[6] = {2, 0, 0, 0, 0, 1}, ap2[6] = {2, 0, 0, 0, 0, 2};
            CHECK(net_note(&nl, a1, 5, ap1, 6, -70) == 0 && net_note(&nl, a1, 5, ap1, 11, -50) == 0);
            CHECK(nl.n[0].rssi == -50 && nl.n[0].ch == 11 && nl.n[0].n == 2 && nl.n[0].n_ap == 1);
            CHECK(net_note(&nl, a1, 5, ap2, 1, -90) == 0 && nl.n[0].ch == 11 && nl.n[0].n_ap == 2);
            CHECK(nl.n[0].ap_rssi[1] == -90 && nl.n[0].ap_ch[1] == 1 && memcmp(nl.n[0].ap[1], ap2, 6) == 0);
            for (int j = 0; j < 10; j++) { uint8_t x[6] = {2, 1, 0, 0, 0, (uint8_t)j}; net_note(&nl, a1, 5, x, 6, -60); }
            CHECK(nl.n[0].n_ap == MAX_NET_APS && nl.count == 1);        // AP list bounded
            CHECK(net_note(&nl, b1, 4, NULL, 1, -80) == 1 && strcmp(nl.n[1].ssid, "Beta") == 0 && nl.n[1].n_ap == 0);
            CHECK(net_note(&nl, z, 4, ap1, 1, -40) == -1 && net_note(&nl, a1, 0, ap1, 1, -40) == -1 && nl.count == 2);
            for (int j = 0; j < 40; j++) { uint8_t x[3] = {'n', (uint8_t)('a' + j / 26), (uint8_t)('a' + j % 26)}; net_note(&nl, x, 3, ap1, 1, -60); }
            CHECK(nl.count == MAX_NETS);
        }
        int hz2;
        CHECK(parse_glide("GLIDE,600,1500,4500", &hz, &hz2, &vol) == 600 && hz == 1500 && hz2 == 4500 && vol == 0);
        CHECK(parse_glide("GLIDE,600,1500,4500,50\r\n", &hz, &hz2, &vol) == 600 && vol == 50);
        CHECK(parse_glide("GLIDE,600,1500", &hz, &hz2, &vol) == -1 && parse_glide("GLIDE,600,1500,99", &hz, &hz2, &vol) == -1);
        CHECK(parse_glide("GLIDE,0,1500,4500", &hz, &hz2, &vol) == -1 && parse_glide("GLIDE,600,1500,4500,0", &hz, &hz2, &vol) == -1);
        CHECK(parse_glide("BEEP,600,1500,4500", &hz, &hz2, &vol) == -1 && parse_beep("GLIDE,600,1500,4500", &hz, &vol) == -1);
        CHECK(parse_cmd("BEEP,150", cm) == CMD_NONE);
        CHECK(parse_cmd(CMD("", "POKE,", BC), cm) == CMD_NONE);   // broadcast refused
        CHECK(parse_cmd(CMD("z", "POKE,", PLUG), cm) == CMD_NONE); // trailing junk
        cb_[strlen(cb_) - 4] = '\0';                              // 5 bytes only
        CHECK(parse_cmd(cb_, cm) == CMD_NONE);
        CMD("", "POKE,", PLUG); cb_[6] = 'g';
        CHECK(parse_cmd(cb_, cm) == CMD_NONE);                   // not hex
        CMD("", "POKE,", PLUG); for (char *q = cb_; *q; q++) if (*q == ':') *q = '-';
        CHECK(parse_cmd(cb_, cm) == CMD_NONE);                   // wrong separator
        #undef CMD
        CHECK(parse_cmd("HELLO", cm) == CMD_NONE && parse_cmd(NULL, cm) == CMD_NONE);
    }

    // Poke gating. Beacon RSSI average with hysteresis:
    gate_init(&pg);
    CHECK(gate_state(&pg, 0) == GATE_OK && pg.yield[0] == -1);   // unknown = OK
    gate_rssi(&pg, 0, -50);
    CHECK(pg.rssi_x16[0] / 16 == -50 && gate_state(&pg, 0) == GATE_OK);
    gate_rssi(&pg, 1, -82);
    CHECK(gate_state(&pg, 1) == GATE_WEAK);                     // the far 2nd AP
    for (int i = 0; i < 40; i++) gate_rssi(&pg, 1, -79);
    CHECK(gate_state(&pg, 1) == GATE_WEAK);                     // inside hysteresis
    for (int i = 0; i < 40; i++) gate_rssi(&pg, 1, -70);
    CHECK(gate_state(&pg, 1) == GATE_OK);                       // climbed back
    gate_rssi(&pg, 1, -95);
    CHECK(gate_state(&pg, 1) == GATE_OK);                       // one bad beacon isn't enough
    gate_rssi(&pg, 9, -50); gate_rssi(&pg, -1, -50);             // out of range: ignored
    gate_ack(&pg, 9); gate_sent(&pg, -1);
    gate_rssi(&pg, 2, 0);
    CHECK(pg.rssi_x16[2] != 0);                                 // 0 dBm isn't "none"

    // Weak AP leaves the rotation: every poke goes to the router
    gate_init(&pg); rr = 0;
    gate_rssi(&pg, 0, -55); gate_rssi(&pg, 1, -82);
    int hits[2] = {0, 0};
    for (int i = 0; i < 200; i++) hits[next_poke_target(&ct, nch, 6, &rr, &pg, 0)]++;
    CHECK(hits[0] == 200 && hits[1] == 0);

    // Yield: a window of POKE_EVAL_N pokes is judged when the next one goes,
    // so the last poke's ACK counts (send, then ACK, as on the air)
    gate_init(&pg);
    for (int i = 0; i < POKE_EVAL_N; i++) gate_sent(&pg, 1);
    CHECK(pg.yield[1] == -1);
    for (int i = 0; i < 5; i++) gate_ack(&pg, 1);
    gate_sent(&pg, 1);                                          // judges, opens next
    CHECK(pg.yield[1] == 10 && gate_state(&pg, 1) == GATE_SILENT);
    gate_ack(&pg, 1);
    for (int i = 0; i < POKE_EVAL_N - 1; i++) { gate_sent(&pg, 1); gate_ack(&pg, 1); }
    CHECK(pg.yield[1] == 10);                                   // not judged yet
    gate_sent(&pg, 1);
    CHECK(pg.yield[1] == 100 && gate_state(&pg, 1) == GATE_OK); // every poke answered
    for (int i = 0; i < POKE_EVAL_N; i++) gate_ack(&pg, 1);     // stray extra ACKs
    for (int i = 0; i < POKE_EVAL_N; i++) gate_sent(&pg, 1);
    CHECK(pg.yield[1] == 100);                                  // capped

    // Silent AP gets only trial pokes, and can come back
    gate_init(&pg); rr = 0;
    pg.yield[1] = 0;
    hits[0] = hits[1] = 0;
    for (int i = 0; i < 100 * POKE_TRIAL_EVERY; i++)
        hits[next_poke_target(&ct, nch, 6, &rr, &pg, 0)]++;
    CHECK(hits[1] == 100 && hits[0] == 100 * (POKE_TRIAL_EVERY - 1));
    pg.yield[1] = 90;
    hits[0] = hits[1] = 0;
    for (int i = 0; i < 100; i++) hits[next_poke_target(&ct, nch, 6, &rr, &pg, 0)]++;
    CHECK(hits[0] == 50 && hits[1] == 50);                     // back to even split

    // Everything out: the strongest AP on the channel is still poked
    gate_init(&pg); rr = 0;
    gate_rssi(&pg, 0, -88); gate_rssi(&pg, 1, -84);
    CHECK(next_poke_target(&ct, nch, 6, &rr, &pg, 0) == 1);
    CHECK(next_poke_target(&ct, nch, 6, &rr, &pg, 0) == 1);
    nch[1] = 11;
    CHECK(next_poke_target(&ct, nch, 6, &rr, &pg, 0) == 0);        // only one here
    gate_init(&pg); pg.yield[0] = 0; nch[1] = 6; pg.yield[1] = 0;
    CHECK(next_poke_target(&ct, nch, 6, &rr, &pg, 0) >= 0);        // no RSSI yet: still pokes
    CHECK(strcmp(gate_name(GATE_WEAK), "weak") == 0 && strcmp(gate_name(GATE_SILENT), "silent") == 0
          && strcmp(gate_name(GATE_OK), "ok") == 0);

    // Verdict streams: a router's own frames only count while its ACKs don't
    {
        static motion_det_t vd;
        md_cfg_t vc; md_cfg_default(&vc);
        md_init(&vd, &vc, 0);
        const int64_t T = 100000000;
        md_stream_t *fr = &vd.s[0 * 2 + CSI_KIND_AP], *ack = &vd.s[0 * 2 + CSI_KIND_ACK];
        md_stream_t *dev = &vd.s[DEV_NODE(0) * 2 + CSI_KIND_ACK];
        fr->n = 50;  fr->t_last = T;  fr->score_s = 5;  fr->score_l = 4;
        ack->n = 50; ack->t_last = T; ack->score_s = 1; ack->score_l = 1;
        dev->n = 50; dev->t_last = T; dev->score_s = 9; dev->score_l = 9;
        float vs, vl;
        md_max_scores(&vd, T, &vs, &vl);
        CHECK(vs == 1 && vl == 1);                 // ACK live: frames and device ignored
        ack->t_last = T - (int64_t)(vc.stale_s * 1e6f) - 1;
        md_max_scores(&vd, T, &vs, &vl);
        CHECK(vs == 5 && vl == 4);                 // no ACKs (e.g. POKE_HZ=0): frames decide
        ack->n = 0; ack->t_last = T;
        md_max_scores(&vd, T, &vs, &vl);
        CHECK(vs == 5);                            // never had ACKs: frames decide
        ack->n = 50; ack->score_s = -1;            // ACKs live but still warming up
        md_max_scores(&vd, T, &vs, &vl);
        CHECK(vs == -1);                           // frames still don't decide
    }

    // ---------- Motion detector ----------
    int8_t cb[128] = {0};
    float amp[N_SC];
    cb[2 * 1] = 3;  cb[2 * 1 + 1] = 4;                    // subcarrier 1: |3j+4| = 5
    cb[2 * 63 + 1] = -7;                                  // subcarrier 63 -> last slot
    cb[2 * 30 + 1] = 100;                                 // guard band, must be skipped
    CHECK(lltf_amplitudes(cb, 128, amp));
    CHECK(amp[0] == 5.0f && amp[N_SC - 1] == 7.0f);
    float total = 0; for (int i = 0; i < N_SC; i++) total += amp[i];
    CHECK(total == 12.0f);                                // DC/guards ignored
    CHECK(!lltf_amplitudes(cb, 127, amp));
    CHECK(!lltf_amplitudes(NULL, 128, amp));

    // Top-K mean: the 8 largest of 52, regardless of order
    float e[N_SC];
    for (int i = 0; i < N_SC; i++) e[i] = (float)((i * 37) % N_SC);   // a permutation of 0..51
    CHECK(fabsf(md_top_mean(e) - (51 + 50 + 49 + 48 + 47 + 46 + 45 + 44) / 8.0f) < 1e-5f);
    for (int i = 0; i < N_SC; i++) e[i] = 1.0f;
    CHECK(md_top_mean(e) == 1.0f);

    static motion_det_t md;
    md_cfg_t mc;
    md_cfg_default(&mc);
    md_init(&md, &mc, 0);
    CHECK(md.phase == MD_CAL_WAIT && md.why == MD_WHY_BOOT && md_take_cal_event(&md));
    CHECK(!md_take_cal_event(&md));                          // consumed
    CHECK(strcmp(md_phase_name(MD_CAL_TRACK), "track") == 0 && strcmp(md_why_name(9), "?") == 0);
    for (int i = 0; i < 128; i += 2) { cb[i] = 0; cb[i + 1] = 40; }   // flat channel
    CHECK(md_update(&md, -1, 0, 0, -50, cb, 128) < 0);       // bad node
    CHECK(md_update(&md, MAX_NODES, 0, 0, -50, cb, 128) < 0);
    CHECK(md_update(&md, 0, 2, 0, -50, cb, 128) < 0);        // bad kind
    int8_t zero[128] = {0};
    CHECK(md_update(&md, 0, 0, 0, -50, zero, 128) < 0);      // all-zero CSI
    // Steady channel: no score during warm-up, then ~0; no verdict before a floor
    float sc = -1;
    int first_score = -1, changes = 0;
    for (int i = 0; i < 300; i++) {
        sc = md_update(&md, 0, 1, i * 10000LL, -50, cb, 128);
        if (sc >= 0 && first_score < 0) first_score = i;
        changes += md_eval(&md, i * 10000LL) != 0;
    }
    CHECK(changes == 0);
    CHECK(first_score == 200);                               // warm_s = 2 s at 100 Hz
    CHECK(sc >= 0 && sc < 1e-6f && !md.motion);
    CHECK(md.phase == MD_CAL_COLLECT && md.floor == 0);
    // A wildly changing channel during collect: still no verdict (no floor yet)
    for (int i = 300; i < 400; i++) {
        for (int j = 0; j < 128; j += 2) cb[j + 1] = (int8_t)(((i * 7 + j * 13) % 60) + 10);
        md_update(&md, 0, 1, i * 10000LL, -50, cb, 128);
        changes += md_eval(&md, i * 10000LL) != 0 || md.motion;
    }
    CHECK(changes == 0 && md.s[1].score_s > 0.05f);          // changing a lot, but no floor

    // ---- Floor (auto-calibration), driven with synthetic short scores ----
    md_init(&md, &mc, 0);
    int64_t tt = 0;
    #define FEED(score, secs) for (int k_ = 0; k_ < (secs) * 10; k_++, tt += 100000) \
                                  md_floor_step(&md, tt, (score))
    FEED(0.04f, 9.85);                                       // 99 samples: not enough
    CHECK(md.phase == MD_CAL_COLLECT && md.floor == 0);
    FEED(0.04f, 1);
    CHECK(md.phase == MD_CAL_FAST && fabsf(md.floor - 0.04f) < 1e-7f);
    // Mostly movement with brief pauses: floor stays at the quiet level
    for (int k = 0; k < 400; k++, tt += 100000) md_floor_step(&md, tt, k % 5 ? 0.20f : 0.04f);
    CHECK(fabsf(md.floor - 0.04f) < 1e-7f && md.phase == MD_CAL_FAST);
    for (int k = 0; md.ring_n < MD_RING - 1; k++, tt += 100000)
        md_floor_step(&md, tt, k % 5 ? 0.20f : 0.04f);
    CHECK(md.phase == MD_CAL_FAST && fabsf(md.floor - 0.04f) < 1e-7f);
    md_floor_step(&md, tt, 0.04f); tt += 100000;
    FEED(0.04f, 1);
    CHECK(md.phase == MD_CAL_TRACK && fabsf(md.floor - 0.04f) < 1e-7f);   // full: 120 s
    // Tracking: rises at most 1 %/s ...
    FEED(0.08f, 10);                                         // 20 % of the ring still quiet
    CHECK(fabsf(md.floor - 0.04f) < 1e-7f);                  // p10 unchanged: floor holds
    for (int i = 0; i < MD_RING; i++) md.ring[i] = 0.08f;    // now the whole room got noisier
    for (int k = 0; k < 10; k++) { tt += 1000000; md_floor_step(&md, tt, 0.08f); }
    CHECK(fabsf(md.floor - 0.04f * powf(1.01f, 10)) < 1e-5f); // ... rises only 1 %/s
    for (int k = 0; k < 80; k++) { tt += 1000000; md_floor_step(&md, tt, 0.08f); }
    CHECK(fabsf(md.floor - 0.08f) < 1e-7f);                  // and gets there (ln 2 / 1 % = 70 s)
    // ... but falls at once
    FEED(0.01f, 120);
    CHECK(fabsf(md.floor - 0.01f) < 1e-7f);
    // Clamped both ways
    FEED(0.0001f, 120);
    CHECK(md.floor == mc.floor_lo);
    md_init(&md, &mc, tt);
    FEED(5.0f, 11);
    CHECK(md.floor == mc.floor_hi);
    // Negative scores (no live stream) are not sampled
    md_init(&md, &mc, tt);
    FEED(-1.0f, 20);
    CHECK(md.ring_n == 0 && md.floor == 0);
    // Recalibration with a delay: nothing happens before it, then collect
    md_recalibrate(&md, tt, 5000000, MD_WHY_BUTTON);
    CHECK(md.phase == MD_CAL_WAIT && md.why == MD_WHY_BUTTON && md_take_cal_event(&md));
    FEED(0.04f, 4.9);
    CHECK(md.phase == MD_CAL_WAIT && md.ring_n == 0 && !md_take_cal_event(&md));
    FEED(0.04f, 0.2);
    CHECK(md.phase == MD_CAL_COLLECT && md.ring_n > 0 && md_take_cal_event(&md));
    #undef FEED

    // ---- Verdict hysteresis, scores set directly ----
    md_init(&md, &mc, 0);
    md.floor = 0.04f; md.phase = MD_CAL_TRACK;
    md.collect_from = INT64_MAX;                             // freeze the floor
    md_stream_t *st = &md.s[1];
    st->n = 10; st->t_last = 1000000;
    #define SET(sv, lv) (st->score_s = (sv) * 0.04f, st->score_l = (lv) * 0.04f)
    SET(1.0f, 1.0f); CHECK(md_eval(&md, 1000000) == 0 && !md.motion);
    SET(2.9f, 1.0f); CHECK(md_eval(&md, 1000000) == 0 && !md.motion);      // a blip: long flat
    SET(2.9f, 1.25f); CHECK(md_eval(&md, 1000000) == 1 && md.m_short && !md.m_long);
    SET(2.1f, 1.0f); CHECK(md_eval(&md, 1000000) == 0 && md.motion);       // above off, gate no longer matters
    SET(1.9f, 1.0f); CHECK(md_eval(&md, 1000000) == -1 && !md.motion);
    SET(1.0f, 1.45f); CHECK(md_eval(&md, 1000000) == 1 && md.m_long && !md.m_short);
    SET(1.0f, 1.25f); CHECK(md_eval(&md, 1000000) == 0 && md.motion);      // above off
    SET(1.0f, 1.15f); CHECK(md_eval(&md, 1000000) == -1);
    SET(2.9f, 1.6f); CHECK(md_eval(&md, 1000000) == 1);                    // both
    SET(1.0f, 1.6f); CHECK(md_eval(&md, 1000000) == 0 && md.motion);       // long holds it
    SET(5.0f, 5.0f);
    CHECK(md_eval(&md, 1000000 + 2100000) == -1);                          // stale stream
    float ms, ml;
    md_max_scores(&md, 1000000 + 2100000, &ms, &ml);
    CHECK(ms < 0 && ml < 0);
    md.floor = 0;                                            // floor lost -> still
    st->t_last = 5000000; SET(9.0f, 9.0f);
    CHECK(md_eval(&md, 5000000) == 0 && !md.motion);
    #undef SET

    // Long-window input is capped at long_cap x floor; the short window isn't
    md_init(&md, &mc, 0);
    md.floor = 0.04f;
    for (int i = 0; i < 1000; i++) {
        for (int j = 0; j < 128; j += 2) cb[j + 1] = (int8_t)(((i * 7 + j * 13) % 60) + 10);
        md_update(&md, 0, 1, i * 10000LL, -50, cb, 128);
    }
    CHECK(md.s[1].score_s > mc.long_cap * 0.04f);
    CHECK(md.s[1].score_l > 0 && md.s[1].score_l <= mc.long_cap * 0.04f * 1.0001f);

    // ---- RSSI step = board moved -> recalibrate ----
    md_init(&md, &mc, 0);
    md_take_cal_event(&md);
    for (int i = 0; i < 128; i += 2) { cb[i] = 0; cb[i + 1] = 40; }
    int recal_at = -1;
    for (int i = 0; i < 3000; i++) {                         // 30 s at 100 Hz
        int r = -60;
        if (i >= 1500 && i < 1600) r = -70;                  // 1 s dip: someone walks past
        md_update(&md, 0, 1, i * 10000LL, r, cb, 128);
        if (md.why == MD_WHY_RSSI && recal_at < 0) recal_at = i;
    }
    CHECK(recal_at < 0);                                     // brief dip ignored
    for (int i = 3000; i < 4500; i++) {                      // board moved: -60 -> -52 dBm
        md_update(&md, 0, 1, i * 10000LL, -52, cb, 128);
        if (md.why == MD_WHY_RSSI && recal_at < 0) recal_at = i;
    }
    CHECK(recal_at > 3000 && recal_at < 3800);               // within ~8 s
    CHECK(md.phase == MD_CAL_WAIT && md.floor == 0);
    int recals = 0;                                          // and only once
    for (int i = 4500; i < 6000; i++) {
        int before = md.cal_event; md.cal_event = false;
        md_update(&md, 0, 1, i * 10000LL, -52, cb, 128);
        recals += md.cal_event; md.cal_event = before;
    }
    CHECK(recals == 0);
    // A channel change before any floor (start-up scan) is still the boot calibration
    md_init(&md, &mc, 0);
    md_reset(&md, 1000000);
    CHECK(md.why == MD_WHY_BOOT && md.phase == MD_CAL_WAIT);
    md.floor = 0.04f;
    // Channel change: streams cleared, settle, recalibrate
    md_reset(&md, 70000000);
    CHECK(md.why == MD_WHY_CHANNEL && md.phase == MD_CAL_WAIT && md.s[1].n == 0);
    CHECK(md_update(&md, 0, 1, 70500000, -50, cb, 128) < 0 && md.s[1].n == 0);   // settling

    // Glitch filter: a brief, very different shape is ignored; a lasting one is accepted
    md_init(&md, &mc, 0);
    int8_t flat[128], odd[128];
    for (int i = 0; i < 128; i += 2) {
        flat[i] = odd[i] = 0;
        flat[i + 1] = 40;
        odd[i + 1] = (i / 2) % 2 ? 70 : 10;                   // shape distance ~0.75
    }
    tt = 0;
    for (int i = 0; i < 300; i++, tt += 10000) md_update(&md, 0, 1, tt, -50, flat, 128);
    uint32_t n0 = md.s[1].n;
    int ignored = 0;
    for (int i = 0; i < 30; i++, tt += 10000)                 // 0.3 s router glitch
        ignored += md_update(&md, 0, 1, tt, -50, odd, 128) < 0;
    CHECK(ignored == 30 && md.s[1].n_glitch == 30 && md.s[1].n == n0);
    sc = md_update(&md, 0, 1, tt, -50, flat, 128);           // snaps back: unaffected
    tt += 10000;
    CHECK(sc >= 0 && sc < 1e-6f && md.s[1].n == n0 + 1);
    // Small changes pass straight through
    int8_t near_flat[128];
    memcpy(near_flat, flat, sizeof flat);
    for (int i = 1; i < 128; i += 4) near_flat[i] = 50;
    CHECK(md_update(&md, 0, 1, tt, -50, near_flat, 128) >= 0);
    tt += 10000;
    CHECK(md.s[1].n_glitch == 30);
    // A new shape that lasts (board moved, furniture shifted) is accepted after glitch_hold
    int64_t t_odd = tt;
    while (md_update(&md, 0, 1, tt, -50, odd, 128) < 0 && tt - t_odd < 10000000) tt += 10000;
    CHECK(tt - t_odd >= mc.glitch_hold_us - 10000 && tt - t_odd <= mc.glitch_hold_us);   // since the last good packet
    uint32_t g = md.s[1].n_glitch;
    tt += 10000;
    CHECK(md_update(&md, 0, 1, tt, -50, odd, 128) >= 0 && md.s[1].n_glitch == g);

    // Router background scan: the ACKs stop for a moment and come back with a
    // milder switch (~0.3). Without a gap that passes (people can reach it in
    // single packets); right after an unusual gap the stricter limit applies.
    md_init(&md, &mc, 0);
    int8_t mild[128];
    for (int i = 0; i < 128; i += 2) { mild[i] = 0; mild[i + 1] = (i / 2) % 2 ? 52 : 28; }  // ~0.3
    tt = 0;
    for (int i = 0; i < 300; i++, tt += 10000) md_update(&md, 0, 1, tt, -50, flat, 128);
    CHECK(md_update(&md, 0, 1, tt, -50, mild, 128) >= 0 && md.s[1].n_glitch == 0);
    for (int i = 0; i < 100; i++) { tt += 10000; md_update(&md, 0, 1, tt, -50, flat, 128); }
    CHECK(md.s[1].scan_until == 0 && fabsf(md.s[1].iv_us - 10000) < 1);
    tt += 185000;                                            // router off channel
    CHECK(md_update(&md, 0, 1, tt, -50, mild, 128) < 0 && md.s[1].n_glitch == 1);
    CHECK(md.s[1].scan_until == tt + mc.scan_hold_us);
    for (int i = 0; i < 250; i++) {                          // the switch lasts 2.5 s > scan_hold
        tt += 10000;
        md_update(&md, 0, 1, tt, -50, mild, 128);
    }
    CHECK(md.s[1].n_glitch == 251);                          // strict for as long as it lasts
    CHECK(md.s[1].scan_until <= md.s[1].scan_gap_at + mc.glitch_hold_us);   // capped
    {   // the router's own frames never arm scan mode (they're bursty)
        static motion_det_t fd;
        md_init(&fd, &mc, 0);
        int64_t ft = 0;
        for (int i = 0; i < 50; i++, ft += 10000) md_update(&fd, 0, 0, ft, -50, flat, 128);
        ft += 500000;
        md_update(&fd, 0, 0, ft, -50, flat, 128);
        CHECK(fd.s[0].scan_until == 0);
    }
    int64_t stl = (int64_t)(mc.stale_s * 1e6f);
    CHECK(!md_live(&md.s[1], tt, stl) && md_scanning(&md.s[1], tt, stl));   // mid-scan:
    md.s[0].n = 50; md.s[0].t_last = tt; md.s[0].score_s = md.s[0].score_l = 9;
    md_max_scores(&md, tt, &ms, &ml);                        // the router's own frames
    CHECK(ms < 0 && ml < 0);                                 // don't take over
    md.s[0].n = 0;
    // Back to the old shape: accepted, and normal limits again after scan_hold
    tt += 10000;
    CHECK(md_update(&md, 0, 1, tt, -50, flat, 128) >= 0);
    for (int i = 0; i < 250; i++) { tt += 10000; md_update(&md, 0, 1, tt, -50, flat, 128); }
    CHECK(tt >= md.s[1].scan_until);
    tt += 10000;
    CHECK(md_update(&md, 0, 1, tt, -50, mild, 128) >= 0 && md.s[1].n_glitch == 251);

    // Fuzz: random frames must never crash or over-read (ASan watches)
    srand(1);
    int fuzzed = 0;
    static seen_list_t fz_seen;
    node_table_t fz_nodes = {0};
    node_add(&fz_nodes, AP1);
    for (int it = 0; it < 200000; it++) {
        int len = rand() % 300;
        uint8_t *buf = malloc(len ? len : 1);
        for (int i = 0; i < len; i++) buf[i] = (uint8_t)rand();
        if (len) buf[0] = 0x80;
        if (len > 40 && (it & 1)) { buf[36] = 0; buf[37] = 9; }
        parse_beacon(buf, len, "MyNetwork", &b);
        if (len) buf[0] = 0x88;                               // same bytes as a data frame
        if (len >= 16 && (it & 2)) memcpy(buf + ((it & 4) ? 4 : 10), AP1, 6);  // "our" BSSID
        seen_note(&fz_seen, &fz_nodes, buf, len, -50, (uint32_t)it);
        free(buf);
        fuzzed++;
    }
    printf("fuzz: %d random frames parsed without crashing\n", fuzzed);

    printf("%d passed, %d failed\n", passes, fails);
    if (fails == 0) printf("ALL C TESTS PASSED\n");
    return fails ? 1 : 0;
}
