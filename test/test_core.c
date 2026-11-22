// Host-side tests for csi_core.h. From the repo root: make test-c
// (or: gcc -std=c11 -Wall -Wextra -g -fsanitize=address,undefined
//      -Imain test/test_core.c -o test_core && ./test_core)
#include <stdlib.h>
#include "csi_core.h"
#include "aes128.h"

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
    const char *want = "CSI,1234567890123,1,1,6,-42,1,0,4,gH8A/w==\n";   // base64 of the 4 bytes
    int n = format_csi_line(line, sizeof line, &p);
    CHECK(n == (int)strlen(want) && strcmp(line, want) == 0);
    {   // base64 both ways, every padding; bad input refused
        uint8_t in[7] = {0, 255, 16, 131, 1, 2, 250}, back[8];
        char b[16];
        static const char *enc[] = {"", "AA==", "AP8=", "AP8Q", "AP8Qgw==", "AP8QgwE=", "AP8QgwEC", "AP8QgwEC+g=="};
        for (int k = 0; k <= 7; k++) {
            CHECK(b64_encode(in, k, b, sizeof b) == (int)strlen(enc[k]) && strcmp(b, enc[k]) == 0);
            CHECK(b64_decode(b, back, sizeof back) == k && memcmp(back, in, (size_t)k) == 0);
        }
        CHECK(b64_encode(in, 7, b, 12) == -1);                    // no room for the NUL
        CHECK(b64_decode("AP8QgwEC\n", back, sizeof back) == 6);  // up to the end of the line
        CHECK(b64_decode("AP8QgwEC", back, 5) == -1);             // doesn't fit
        CHECK(b64_decode("AP8Q gwEC", back, sizeof back) == -1);
        CHECK(b64_decode("AP8", back, sizeof back) == -1 && b64_decode("A", back, sizeof back) == -1);
        CHECK(b64_decode("AP8Q====", back, sizeof back) == -1);
    }
    {   // A 128-byte LLTF goes out without its 12 unused subcarriers (len 104)
        // and reads back the same; other lengths, or a value where none
        // belongs, go out whole. The first format (decimal text) still reads.
        static csi_pkt_t q;
        q.ts = 5; q.src = 9; q.kind = CSI_KIND_ACK; q.ch = 11; q.rssi = -70; q.len = 128;
        for (int k = 0; k < 64; k++) {
            q.buf[2 * k] = lltf_used(k) ? (int8_t)(k - 90) : 0;
            q.buf[2 * k + 1] = lltf_used(k) ? (int8_t)(127 - k) : 0;
        }
        int8_t back[CSI_MAX_LEN];
        int len = 0, off = 0;
        int m = format_csi_line(line, sizeof line, &q);
        CHECK(m > 0 && m < 185);
        CHECK(sscanf(line, "CSI,5,9,1,11,-70,0,0,%d,%n", &len, &off) == 1 && len == 104);
        CHECK(parse_csi_data(line + off, len, back) == 128 && memcmp(back, q.buf, 128) == 0);
        q.buf[2 * 30] = 3;                                        // a guard subcarrier with a value
        CHECK(format_csi_line(line, sizeof line, &q) > 0);
        CHECK(sscanf(line, "CSI,5,9,1,11,-70,0,0,%d,%n", &len, &off) == 1 && len == 128);
        CHECK(parse_csi_data(line + off, len, back) == 128 && memcmp(back, q.buf, 128) == 0);
        q.len = 256;
        CHECK(format_csi_line(line, sizeof line, &q) > 0);
        CHECK(sscanf(line, "CSI,5,9,1,11,-70,0,0,%d,%n", &len, &off) == 1 && len == 256);
        CHECK(parse_csi_data(line + off, len, back) == 256 && memcmp(back, q.buf, 256) == 0);
        CHECK(parse_csi_data(line + off, 255, back) == -1);       // len must match the data
        CHECK(parse_csi_data("-128 127 0 -1\n", 4, back) == 4 && back[0] == -128 && back[3] == -1);
        CHECK(parse_csi_data("-128 127 0\n", 4, back) == -1 && parse_csi_data("1 2 300 4\n", 4, back) == -1);
        CHECK(parse_csi_data("AAAA", CSI_MAX_LEN + 1, back) == -1);
    }
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
    CHECK(memcmp(ta0, ME, 6) != 0 && ta0[1] == ME[1]);
    CHECK((ta0[0] & 0x01) == 0);                           // unicast
    CHECK((ta0[0] & 0x02) != (ME[0] & 0x02));              // local bit flipped
    CHECK(memcmp(ta0, ta1, 6) != 0);
    static const uint8_t TA1_VEC[6] = {0x26,0x0a,0x26,0xb4,0x32,0x66};  // also in test_probe_report.py
    CHECK(memcmp(ta1, TA1_VEC, 6) == 0);
    CHECK(poke_ta_node(ME, ta0) == 0 && poke_ta_node(ME, ta1) == 1);
    CHECK(poke_ta_node(ME, ta7) == MAX_NODES - 1);
    CHECK(poke_ta_node(ME, ME) == -1);                     // our own MAC is not a TA
    CHECK(poke_ta_node(ME, AP1) == -1);
    CHECK(poke_ta_node(ME, NULL) == -1);
    uint8_t tax[6]; poke_ta(ME, REPORT_NODE, tax);
    CHECK(poke_ta_node(ME, tax) == -1);                    // the report's address is no node
    {   // Boards from one batch, a few MACs apart: no address of one is the
        // other's (it used to be MAC + n: 4 apart shared 5 addresses), and no
        // board's own addresses repeat
        int clash = 0, self = 0;
        for (int b = 0; b < 64; b++) {
            uint8_t ma[6] = {0x24,0x0a,0xc4,0x11,0x22,(uint8_t)(0x30 + b)}, mb[6];
            memcpy(mb, ma, 6); mb[5] = (uint8_t)(ma[5] + 4);
            for (int i = 0; i <= MAX_NODES; i++) {
                uint8_t x[6], y[6];
                poke_ta(ma, i, x);
                for (int j = 0; j <= MAX_NODES; j++) {
                    poke_ta(mb, j, y);
                    clash += memcmp(x, y, 6) == 0;
                    if (j != i) { poke_ta(ma, j, y); self += memcmp(x, y, 6) == 0; }
                }
                if (i < MAX_NODES) CHECK(poke_ta_node(ma, x) == i && poke_ta_node(mb, x) == -1);
            }
        }
        CHECK(clash == 0 && self == 0);
    }
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
    pg.keep_far = true;                                       // chosen one by one (APS): poked anyway
    { int g1 = 0; for (int j = 0; j < 20; j++) g1 += next_poke_target(&ct, nch, 6, &rr, &pg, 0) == 1;
      CHECK(g1 == 10 && !ap_is_far(&pg, 1, ap_best_rssi_x16(&ct, nch, 6, &pg))); }
    pg.keep_far = false;
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
        CHECK(sl.d[e].ap == node_find(&dt, AP1) && node_is_ap(sl.d[e].ap));  // its AP is kept
        {   // a device that moves to another AP of ours: DEV names the new one
            node_table_t t2 = dt;
            int a2 = node_add(&t2, AP2);
            seen_list_t s2 = sl;
            CHECK(node_is_ap(a2) && seen_note(&s2, &t2, df, 24, -40, 1150) == e && s2.d[e].ap == a2);
        }
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

        // APS: chosen access points (MAC strings built here, never written out)
        {
            static const uint8_t A1[6] = {0x50,0x02,0x91,0x1a,0x2b,0x3c}, A2[6] = {0x52,0x02,0x91,0x1a,0x2b,0x3d};
            char al[256], m1[18], m2[18];
            snprintf(m1, sizeof m1, "%02x:%02x:%02x:%02x:%02x:%02x", A1[0], A1[1], A1[2], A1[3], A1[4], A1[5]);
            snprintf(m2, sizeof m2, "%02x:%02x:%02x:%02x:%02x:%02x", A2[0], A2[1], A2[2], A2[3], A2[4], A2[5]);
            ap_list_t L = {0}, keep;
            snprintf(al, sizeof al, "APS,%s,%s\r\n", m1, m2);
            CHECK(parse_aps(al, &L) == 2 && memcmp(L.mac[0], A1, 6) == 0 && memcmp(L.mac[1], A2, 6) == 0);
            CHECK(ap_list_index(&L, A2) == 1 && ap_list_index(&L, PLUG) == -1);
            keep = L;
            CHECK(parse_aps("APS,clear", &L) == APS_CLEAR && parse_aps("APS", &L) == APS_QUERY);
            CHECK(parse_aps("APS\r\n", &L) == APS_QUERY && parse_aps("APSX", &L) == APS_BAD);
            snprintf(al, sizeof al, "APS,%s,%s", m1, m1);
            CHECK(parse_aps(al, &L) == APS_BAD);                               // a repeat
            snprintf(al, sizeof al, "APS,%s,", m1);
            CHECK(parse_aps(al, &L) == APS_BAD);                               // empty entry
            snprintf(al, sizeof al, "APS,%s x", m1);
            CHECK(parse_aps(al, &L) == APS_BAD);                               // trailing junk
            snprintf(al, sizeof al, "APS,51%s", m1 + 2);
            CHECK(parse_aps(al, &L) == APS_BAD);                               // group address
            CHECK(parse_aps("APS,", &L) == APS_BAD && parse_aps("APS,clearx", &L) == APS_BAD);
            CHECK(memcmp(&L, &keep, sizeof L) == 0);                           // a bad line changes nothing
            int n = snprintf(al, sizeof al, "APS");
            for (int k = 0; k < MAX_APS + 1; k++)                              // 9 entries: too many
                n += snprintf(al + n, sizeof al - (size_t)n, ",50:02:91:00:00:%02x", k);
            CHECK(parse_aps(al, &L) == APS_BAD);
            al[strlen(al) - 18] = '\0';                                       // 8: fine
            CHECK(parse_aps(al, &L) == MAX_APS);
            {   // The longest command (APS with 8 addresses) gets through the line
                // reader whole; a line over the limit is dropped whole, its tail
                // never run as a command of its own
                cmd_rx_t rx;
                memset(&rx, 0, sizeof rx);
                const char *got = NULL;
                for (const char *p = al; *p; p++) CHECK(cmd_rx_feed(&rx, *p) == NULL);
                got = cmd_rx_feed(&rx, '\n');
                CHECK(got && strcmp(got, al) == 0 && parse_aps(got, &L) == MAX_APS);
                char big[CMD_LINE_MAX + 40];
                memset(big, 'A', sizeof big - 1);
                big[sizeof big - 1] = '\0';
                memcpy(big + sizeof big - 5, "CAL", 3);          // a tail that would parse
                int lines = 0;
                for (const char *p = big; *p; p++) lines += cmd_rx_feed(&rx, *p) != NULL;
                CHECK(lines == 0 && cmd_rx_feed(&rx, '\n') == NULL);
                got = NULL;                                      // the next line is fine again
                for (const char *p = "CAL\r\n"; *p; p++) { const char *x = cmd_rx_feed(&rx, *p); if (x) got = x; }
                CHECK(got && strcmp(got, "CAL") == 0);
                CHECK(cmd_rx_feed(&rx, '\n') == NULL);           // empty line: nothing
                char edge[CMD_LINE_MAX];                         // exactly the limit - 1: kept
                memset(edge, 'B', sizeof edge - 1);
                edge[sizeof edge - 1] = '\0';
                for (const char *p = edge; *p; p++) cmd_rx_feed(&rx, *p);
                got = cmd_rx_feed(&rx, '\n');
                CHECK(got && strlen(got) == CMD_LINE_MAX - 1);
            }
            // The channel with the most chosen APs wins; on a tie, the strongest
            int hits[14] = {0}, best[14] = {0};
            CHECK(aps_best_channel(hits, best, 13) == 0);
            hits[3] = 1; best[3] = -40; hits[11] = 2 | 4; best[11] = -60;
            CHECK(aps_best_channel(hits, best, 13) == 11);
            hits[6] = 1 | 8; best[6] = -55;
            CHECK(aps_best_channel(hits, best, 13) == 6);
        }
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
        {   // SENS: long-window threshold at run time
            int x;
            CHECK(parse_sens("SENS", &x) == SENS_QUERY && parse_sens("SENS\r\n", &x) == SENS_QUERY);
            CHECK(parse_sens("SENS,default", &x) == SENS_DEFAULT);
            CHECK(parse_sens("SENS,160", &x) == SENS_SET && x == 160);
            CHECK(parse_sens("SENS,110\n", &x) == SENS_SET && x == 110 && parse_sens("SENS,300", &x) == SENS_SET);
            CHECK(parse_sens("SENS,109", &x) == SENS_BAD && parse_sens("SENS,301", &x) == SENS_BAD);
            CHECK(parse_sens("SENS,", &x) == SENS_BAD && parse_sens("SENS,1.6", &x) == SENS_BAD);
            CHECK(parse_sens("SENS,16000", &x) == SENS_BAD && parse_sens("SENSE", &x) == SENS_BAD);
            CHECK(parse_sens("SOUND", &x) == SENS_BAD && parse_cmd("SENS,160", cm) == CMD_NONE);
        }
        {   // OUT: the light output
            int m, sec;
            CHECK(parse_out("OUT", &m, &sec) == OUT_QUERY && parse_out("OUT\r\n", &m, &sec) == OUT_QUERY);
            CHECK(parse_out("OUT,default", &m, &sec) == OUT_DEFAULT && parse_out("OUT,default\n", &m, &sec) == OUT_DEFAULT);
            CHECK(parse_out("OUT,motion,90", &m, &sec) == OUT_SET && m == OUT_MODE_MOTION && sec == 90);
            CHECK(parse_out("OUT,off,0", &m, &sec) == OUT_SET && m == OUT_MODE_OFF && sec == 0);
            CHECK(parse_out("OUT,on,86400\r\n", &m, &sec) == OUT_SET && m == OUT_MODE_ON && sec == 86400);
            CHECK(parse_out("OUT,motion", &m, &sec) == OUT_SET && m == OUT_MODE_MOTION && sec == -1);
            CHECK(parse_out("OUT,on,86401", &m, &sec) == OUT_BAD && m == -1 && sec == -1);
            CHECK(parse_out("OUT,motion,", &m, &sec) == OUT_BAD && parse_out("OUT,motion,1.5", &m, &sec) == OUT_BAD);
            CHECK(parse_out("OUT,motion,-5", &m, &sec) == OUT_BAD && parse_out("OUT,motion,30,1", &m, &sec) == OUT_BAD);
            CHECK(parse_out("OUT,motions", &m, &sec) == OUT_BAD && parse_out("OUT,", &m, &sec) == OUT_BAD);
            CHECK(parse_out("OUT,ON", &m, &sec) == OUT_BAD && parse_out("OUTS", &m, &sec) == OUT_BAD);
            CHECK(parse_out("OUT,defaults", &m, &sec) == OUT_BAD && parse_out("OUT,motion,9999999", &m, &sec) == OUT_BAD);
            CHECK(parse_out("SOUND,on", &m, &sec) == OUT_BAD && parse_out(NULL, &m, &sec) == OUT_BAD);
            CHECK(parse_cmd("OUT,on,5", cm) == CMD_NONE && parse_sens("OUT", &m) == SENS_BAD);
            int sm, sv, sh;
            CHECK(parse_sound("OUT,on", &sm, &sv, &sh) == SOUND_BAD && parse_ext("OUT,1") == -1);

            out_state_t os = {0};
            // motion: on at once, held off_after_s past the last motion
            CHECK(!out_step(&os, OUT_MODE_MOTION, 10, false, 1000));
            CHECK(out_step(&os, OUT_MODE_MOTION, 10, true, 2000));
            CHECK(out_step(&os, OUT_MODE_MOTION, 10, false, 2001));
            CHECK(out_step(&os, OUT_MODE_MOTION, 10, true, 5000));          // motion again: from here
            CHECK(out_step(&os, OUT_MODE_MOTION, 10, false, 14999));
            CHECK(!out_step(&os, OUT_MODE_MOTION, 10, false, 15000));
            CHECK(!out_step(&os, OUT_MODE_MOTION, 10, false, 99999));
            // 0 s: off at the first still poll
            CHECK(out_step(&os, OUT_MODE_MOTION, 0, true, 100000) && !out_step(&os, OUT_MODE_MOTION, 0, false, 100050));
            // the clock wrapping in the middle of the hold
            CHECK(out_step(&os, OUT_MODE_MOTION, 5, true, 0xFFFFF000u));
            CHECK(out_step(&os, OUT_MODE_MOTION, 5, false, 0x00000100u));    // 4352 ms later
            CHECK(!out_step(&os, OUT_MODE_MOTION, 5, false, 0x00000400u));   // 5120 ms later
            // off / on ignore motion
            CHECK(!out_step(&os, OUT_MODE_OFF, 10, true, 200000));
            CHECK(out_step(&os, OUT_MODE_ON, 10, false, 300000));
            // on -> motion: stays on for off_after_s, then off
            CHECK(out_step(&os, OUT_MODE_MOTION, 30, false, 310000));
            CHECK(out_step(&os, OUT_MODE_MOTION, 30, false, 329999));
            CHECK(!out_step(&os, OUT_MODE_MOTION, 30, false, 330000));
            // on with motion, then off: off at once
            CHECK(out_step(&os, OUT_MODE_MOTION, 30, true, 400000) && !out_step(&os, OUT_MODE_OFF, 30, true, 400050));
            // a long hold doesn't overflow
            CHECK(out_step(&os, OUT_MODE_MOTION, OUT_OFF_MAX, true, 0) &&
                  out_step(&os, OUT_MODE_MOTION, OUT_OFF_MAX, false, OUT_OFF_MAX * 1000u - 1) &&
                  !out_step(&os, OUT_MODE_MOTION, OUT_OFF_MAX, false, OUT_OFF_MAX * 1000u));
        }
        {   // LEDSET: what the status LED shows
            led_cfg_t lc = led_cfg_default(40), keep;
            CHECK(lc.mode == LED_MODE_STATUS && lc.pattern == LED_PAT_STRENGTH && lc.bright == 40 && lc.rgb == 0xff0000);
            CHECK(led_cfg_default(0).bright == 1 && led_cfg_default(999).bright == 255);
            CHECK(parse_led("LEDSET", &lc) == LED_QUERY && parse_led("LEDSET\r\n", &lc) == LED_QUERY);
            CHECK(parse_led("LEDSET,default", &lc) == LED_DEFAULT);
            CHECK(parse_led("LEDSET,motion,200,pulse,00FF80\r\n", &lc) == LED_SET && lc.mode == LED_MODE_MOTION &&
                  lc.bright == 200 && lc.pattern == LED_PAT_PULSE && lc.rgb == 0x00ff80);
            keep = lc;
            const char *bad[] = {"LEDSET,", "LEDSET,on,40,steady,ff0000", "LEDSET,off,0,steady,ff0000",
                                 "LEDSET,off,256,steady,ff0000", "LEDSET,off,40,wobble,ff0000",
                                 "LEDSET,off,40,steady,ff00", "LEDSET,off,40,steady,ff00zz",
                                 "LEDSET,off,40,steady,ff0000x", "LEDSET,off,40,steady", "LEDSETX", "LED,off"};
            for (size_t i = 0; i < sizeof bad / sizeof *bad; i++) CHECK(parse_led(bad[i], &lc) == LED_BAD);
            CHECK(memcmp(&lc, &keep, sizeof lc) == 0);                 // a bad line changes nothing
            // The defaults show exactly what the LED always did
            led_cfg_t d0 = led_cfg_default(40);
            for (int k = 0; k <= 40; k += 5) {                         // motion: red, B/3 .. B with strength
                int kk = k < 10 ? 10 : k > 30 ? 30 : k;
                CHECK(led_pick(&d0, false, false, true, false, k, 0, 0, 1000) == (uint32_t)(40 / 3 + (40 - 40 / 3) * (kk - 10) / 20) << 16);
            }
            CHECK(led_pick(&d0, true, false, true, false, 20, 0, 0, 1000) == (uint32_t)(8 << 16 | 8 << 8));   // looking: yellow wins
            CHECK((led_pick(&d0, false, true, false, false, 0, 0, 0, 1000) & 0xffff00) == 0 &&
                  (led_pick(&d0, false, true, false, false, 0, 0, 0, 1000) & 0xff) > 0);                    // calibrating: blue
            CHECK(led_pick(&d0, false, false, false, false, 0, 990, 0, 1000) == (uint32_t)(12 << 16 | 6 << 8)); // just ended: amber
            CHECK(led_pick(&d0, false, false, false, false, 0, 0, 950, 1000) == (uint32_t)(8 << 16 | 8));       // blip: purple
            CHECK(led_pick(&d0, false, false, false, false, 0, 0, 0, 1000) == 0);                                // still: off
            // Motion only: nothing else lights it
            led_cfg_t mo = d0; mo.mode = LED_MODE_MOTION;
            CHECK(led_pick(&mo, true, false, false, false, 0, 0, 0, 1000) == 0 && led_pick(&mo, false, true, false, false, 0, 0, 0, 1000) == 0);
            CHECK(led_pick(&mo, false, false, false, false, 0, 990, 950, 1000) == 0 && led_pick(&mo, false, false, true, false, 30, 0, 0, 1000) == 40u << 16);
            // Off: never lit
            led_cfg_t of = d0; of.mode = LED_MODE_OFF;
            CHECK(led_pick(&of, true, true, true, false, 30, 990, 950, 1000) == 0);
            // Colour and brightness: steady full, a dim colour stays visible
            led_cfg_t st = d0; st.pattern = LED_PAT_STEADY; st.rgb = 0x00ff80; st.bright = 255;
            CHECK(led_pick(&st, false, false, true, false, 10, 0, 0, 1000) == 0x00ff80);
            st.bright = 1;
            CHECK(led_pick(&st, false, false, true, false, 10, 0, 0, 1000) == 0x000100);
            st.rgb = 0x404040;                                         // would round to black: still lit
            CHECK(led_pick(&st, false, false, true, false, 10, 0, 0, 1000) == 0x010101);
            // Blink: on, then off; pulse: dim and bright, never dark
            led_cfg_t bl = d0; bl.pattern = LED_PAT_BLINK;
            CHECK(led_pick(&bl, false, false, true, false, 10, 0, 0, 100) == 40u << 16 && led_pick(&bl, false, false, true, false, 10, 0, 0, 500) == 0);
            led_cfg_t pu = d0; pu.pattern = LED_PAT_PULSE;
            uint32_t lo = led_pick(&pu, false, false, true, false, 10, 0, 0, 0), hi = led_pick(&pu, false, false, true, false, 10, 0, 0, 800);
            CHECK(lo > 0 && hi == 40u << 16 && lo < hi);
            // A device link sees motion (EXT): purple, in status and motion-only modes, not when off;
            // the board's own motion wins; looking for the router still wins over it
            CHECK(led_pick(&d0, false, false, false, true, 0, 0, 0, 1000) == (uint32_t)(0x8a * 40 / 255 << 16 | 0x3c * 40 / 255 << 8 | 0xff * 40 / 255));
            CHECK(led_pick(&mo, false, false, false, true, 0, 0, 0, 1000) == led_pick(&d0, false, false, false, true, 0, 0, 0, 1000));
            CHECK(led_pick(&of, false, false, false, true, 0, 0, 0, 1000) == 0);
            CHECK(led_pick(&d0, false, false, true, true, 30, 0, 0, 1000) == 40u << 16);
            CHECK(led_pick(&d0, true, false, false, true, 0, 0, 0, 1000) == (uint32_t)(8 << 16 | 8 << 8));
            CHECK(led_pick(&d0, false, false, false, true, 0, 0, 950, 1000) != (uint32_t)(8 << 16 | 8));   // no blip flash over it
            CHECK(led_pick(&bl, false, false, false, true, 0, 0, 0, 500) == 0);                            // blinks too
            // The BOOT button: short press on release; a 5 s hold resets settings once
            btn_t bt = {0};
            CHECK(btn_step(&bt, true, 50) == BTN_NONE && btn_step(&bt, false, 50) == BTN_NONE);   // a bounce
            for (int i = 0; i < 4; i++) CHECK(btn_step(&bt, true, 50) == BTN_NONE);
            CHECK(btn_step(&bt, false, 50) == BTN_SHORT);
            int longs = 0, shorts = 0;
            for (int i = 0; i < 160; i++) { int r = btn_step(&bt, true, 50); longs += r == BTN_LONG; shorts += r == BTN_SHORT; }
            CHECK(longs == 1 && shorts == 0);                                         // once, at 5 s
            CHECK(btn_step(&bt, false, 50) == BTN_NONE && bt.held == 0);             // no recalibration after it
            for (int i = 0; i < 99; i++) CHECK(btn_step(&bt, true, 50) == BTN_NONE); // 4.95 s: not yet long
            CHECK(btn_step(&bt, false, 50) == BTN_SHORT);
            // EXT commands
            CHECK(parse_ext("EXT,1") == 1 && parse_ext("EXT,0\r\n") == 0 && parse_ext("EXT,2") == -1);
            CHECK(parse_ext("EXT,10") == -1 && parse_ext("EXT,") == -1 && parse_ext("EXT") == -1 && parse_ext("EXTX,1") == -1);
        }
        {   // SOUND: buzzer settings at run time
            int sm, sv, sh;
            CHECK(parse_sound("SOUND", &sm, &sv, &sh) == SOUND_QUERY && sm == -1);
            CHECK(parse_sound("SOUND\r\n", &sm, &sv, &sh) == SOUND_QUERY);
            CHECK(parse_sound("SOUND,off", &sm, &sv, &sh) == SOUND_SET && sm == SND_OFF && sv == -1 && sh == -1);
            CHECK(parse_sound("SOUND,beep,15", &sm, &sv, &sh) == SOUND_SET && sm == SND_BEEP && sv == 15 && sh == -1);
            CHECK(parse_sound("SOUND,alarm,50,3500\n", &sm, &sv, &sh) == SOUND_SET && sm == SND_ALARM && sv == 50 && sh == 3500);
            CHECK(parse_sound("SOUND,default", &sm, &sv, &sh) == SOUND_DEFAULT);
            CHECK(parse_sound("SOUND,test\r", &sm, &sv, &sh) == SOUND_TEST);
            CHECK(parse_sound("SOUND,beep,0", &sm, &sv, &sh) == SOUND_BAD && parse_sound("SOUND,beep,51", &sm, &sv, &sh) == SOUND_BAD);
            CHECK(parse_sound("SOUND,beep,15,499", &sm, &sv, &sh) == SOUND_BAD && parse_sound("SOUND,beep,15,6001", &sm, &sv, &sh) == SOUND_BAD);
            CHECK(parse_sound("SOUND,beeps", &sm, &sv, &sh) == SOUND_BAD && parse_sound("SOUND,loud", &sm, &sv, &sh) == SOUND_BAD);
            CHECK(parse_sound("SOUND,beep,", &sm, &sv, &sh) == SOUND_BAD && parse_sound("SOUND,beep,15,3500,1", &sm, &sv, &sh) == SOUND_BAD);
            CHECK(parse_sound("SOUNDS", &sm, &sv, &sh) == SOUND_BAD && parse_sound("BEEP", &sm, &sv, &sh) == SOUND_BAD);
            CHECK(parse_sound("SOUND,offx", &sm, &sv, &sh) == SOUND_BAD && parse_sound("SOUND,testy", &sm, &sv, &sh) == SOUND_BAD);
            CHECK(parse_cmd("SOUND,off", cm) == CMD_NONE && parse_beep("SOUND,off", &hz, &vol) == -1);
        }
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

    {   // Floor while the ring fills: someone moving right after calibrating
        // can't lift it above fast_rise_max x the first floor; it may still fall
        static motion_det_t fd;
        md_cfg_t fc;
        md_cfg_default(&fc);
        md_init(&fd, &fc, 0);
        int64_t t = 0;
        for (; t < 10000000; t += 100000) md_floor_step(&fd, t, 0.02f);     // 10 s quiet
        CHECK(fd.phase == MD_CAL_FAST && fabsf(fd.floor - 0.02f) < 1e-6f && fabsf(fd.floor0 - 0.02f) < 1e-6f);
        for (; t < 70000000; t += 100000) md_floor_step(&fd, t, 0.2f);      // 60 s of motion
        CHECK(fd.phase == MD_CAL_FAST && fd.floor <= 0.02f * fc.fast_rise_max + 1e-6f);
        for (; t < 90000000; t += 100000) md_floor_step(&fd, t, 0.012f);    // quieter than at first
        CHECK(fd.floor < 0.02f);                                            // falls freely
        md_recalibrate(&fd, t, 0, MD_WHY_BUTTON);                           // a new first floor
        for (int64_t stop = t + 10000000; t < stop; t += 100000) md_floor_step(&fd, t, 0.05f);
        CHECK(fabsf(fd.floor0 - 0.05f) < 1e-6f);
    }

    {   // The board's verdict comes from AP links only: a device link changing
        // wildly (someone near the TV, a PC sending POKE) never triggers it, so
        // the board behaves the same with or without a PC attached
        static motion_det_t ad;
        md_cfg_t ac;
        md_cfg_default(&ac);
        md_init(&ad, &ac, 0);
        int8_t still[128], wild[128];
        for (int i = 0; i < 128; i += 2) { still[i] = 0; still[i + 1] = 40; }
        uint32_t r = 12345;
        int64_t t = 0;
        for (; t < 30000000; t += 10000) {                       // 30 s: router steady, gets a floor
            for (int i = 0; i < 128; i++) still[i] = (int8_t)((i & 1) ? 40 + (int)((r = r * 1103515245u + 12345u) >> 30) : 0);
            md_update(&ad, 0, 1, t, -50, still, 128);
            md_eval(&ad, t);
        }
        CHECK(ad.floor > 0 && !ad.motion);
        bool any = false;
        for (int64_t end = t + 20000000; t < end; t += 5000) {    // 20 s: the device link goes wild
            for (int i = 0; i < 128; i++) still[i] = (int8_t)((i & 1) ? 40 + (int)((r = r * 1103515245u + 12345u) >> 30) : 0);
            for (int i = 0; i < 128; i++) wild[i] = (int8_t)((i & 1) ? 10 + (int)((r = r * 1103515245u + 12345u) >> 26) : 0);
            if ((t / 5000) & 1) md_update(&ad, MAX_APS, 1, t, -40, wild, 128);
            else md_update(&ad, 0, 1, t, -50, still, 128);
            md_eval(&ad, t);
            any |= ad.motion;
        }
        CHECK(!any);
        for (int64_t end = t + 10000000; t < end && !any; t += 10000) {   // control: the same on the router
            for (int i = 0; i < 128; i++) wild[i] = (int8_t)((i & 1) ? 10 + (int)((r = r * 1103515245u + 12345u) >> 26) : 0);
            md_update(&ad, 0, 1, t, -50, wild, 128);
            md_eval(&ad, t);
            any |= ad.motion;
        }
        CHECK(any);
    }

    {   // A device slot's signal level never recalibrates the board: the slot
        // can be given to another device (or the device carried around), and
        // that says nothing about the board having moved
        static motion_det_t rd;
        md_cfg_t rc;
        md_cfg_default(&rc);
        md_init(&rd, &rc, 0);
        int8_t still[128];
        uint32_t r = 777;
        int64_t t = 0;
        bool recal = false;
        for (; t < 60000000; t += 10000) {                       // device: -70 dBm, then -50 from 30 s
            for (int i = 0; i < 128; i++) still[i] = (int8_t)((i & 1) ? 40 + (int)((r = r * 1103515245u + 12345u) >> 30) : 0);
            md_update(&rd, 0, 1, t, -50, still, 128);
            md_update(&rd, MAX_APS, 1, t + 3000, t < 30000000 ? -70 : -50, still, 128);
            md_eval(&rd, t);
            recal |= rd.why == MD_WHY_RSSI;
        }
        CHECK(!recal && rd.floor > 0);
        for (int64_t end = t + 30000000; t < end; t += 10000) {   // control: the router's level steps
            for (int i = 0; i < 128; i++) still[i] = (int8_t)((i & 1) ? 40 + (int)((r = r * 1103515245u + 12345u) >> 30) : 0);
            md_update(&rd, 0, 1, t, -70, still, 128);
            md_eval(&rd, t);
            recal |= rd.why == MD_WHY_RSSI;
        }
        CHECK(recal);
    }

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
    {   // Glitch lockout (no scan): the ACKs keep arriving but are ignored for up
        // to glitch_hold, so the stream isn't live after stale_s; the router's
        // own frames must still stay out (they caused most false starts)
        static motion_det_t gd;
        md_init(&gd, &mc, 0);
        int64_t gt = 0;
        for (int i = 0; i < 300; i++, gt += 10000) md_update(&gd, 0, 1, gt, -50, flat, 128);
        int8_t odd[128];
        for (int i = 0; i < 128; i += 2) { odd[i] = 0; odd[i + 1] = (int8_t)((i / 2) % 2 ? 70 : 10); }
        for (int i = 0; i < 400; i++, gt += 10000)            // 4 s of a very different shape
            CHECK(md_update(&gd, 0, 1, gt, -50, odd, 128) < 0);
        CHECK(gd.s[1].scan_until == 0 && !md_live(&gd.s[1], gt, stl) && md_heard(&gd.s[1], gt, stl));
        gd.s[0].n = 50; gd.s[0].t_last = gt; gd.s[0].score_s = gd.s[0].score_l = 9;
        float gs, gl;
        md_max_scores(&gd, gt, &gs, &gl);
        CHECK(gs < 0 && gl < 0);
    }
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
    static const uint8_t FZK[16] = {7};
    aes128_t fz_key; aes128_init(&fz_key, FZK);
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
        if (len) buf[0] = 0xD0;                               // ...and as a peer's hello
        if (len >= 32 && (it & 8)) memcpy(buf + 24, HELLO_MAGIC, sizeof HELLO_MAGIC);
        { hello_t fh; sat_report_t fr;
          rep_extra_t fx;
          if (parse_hello(buf, len, &fh) && fh.sealed) hello_open(buf, len, aes128_block, &fz_key, &fr, &fx);
          order_t fo;                                         // ...and as an order
          if (len >= 33 && (it & 16)) buf[32] = ORDER_VERSION;
          if (len >= 10 && is_order_to(buf, len, buf + 4)) order_open(buf, len, AP1, aes128_block, &fz_key, &fo); }
        free(buf);
        fuzzed++;
    }
    printf("fuzz: %d random frames parsed without crashing\n", fuzzed);

    {   // Other ackwave boards: hello frames, the peer list, their poke addresses
        const uint8_t B1[6] = {0x24,0x0a,0xc4,0x11,0x22,0x30}, B2[6] = {0x24,0x0a,0xc4,0x11,0x22,0x40};
        const uint8_t PLUG[6] = {0x50,0x02,0x91,0x0a,0x0b,0x0c};
        // AES and CCM against independent references (FIPS-197; RFC 3610 #1;
        // Python's cryptography AESCCM for a hello-shaped message)
        static const uint8_t FK[16] = {0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15};
        aes128_t ak; aes128_init(&ak, FK);
        uint8_t blk[16], fips_in[16];
        for (int i = 0; i < 16; i++) fips_in[i] = (uint8_t)(i * 0x11);
        aes128_block(&ak, fips_in, blk);
        static const uint8_t FIPS[16] = {0x69,0xc4,0xe0,0xd8,0x6a,0x7b,0x04,0x30,0xd8,0xcd,0xb7,0x80,0x70,0xb4,0xc5,0x5a};
        CHECK(memcmp(blk, FIPS, 16) == 0);
        {
            uint8_t k[16], n[13] = {0,0,0,3,2,1,0,0xa0,0xa1,0xa2,0xa3,0xa4,0xa5}, ad[8], m[23], out[31], back[23];
            for (int i = 0; i < 16; i++) k[i] = (uint8_t)(0xc0 + i);
            for (int i = 0; i < 8; i++) ad[i] = (uint8_t)i;
            for (int i = 0; i < 23; i++) m[i] = (uint8_t)(8 + i);
            static const uint8_t RFC1[31] = {0x58,0x8c,0x97,0x9a,0x61,0xc6,0x63,0xd2,0xf0,0x66,0xd0,0xc2,0xc0,0xf9,
                0x89,0x80,0x6d,0x5f,0x6b,0x61,0xda,0xc3,0x84,0x17,0xe8,0xd1,0x2c,0xfd,0xf9,0x26,0xe0};
            aes128_t rk; aes128_init(&rk, k);
            ccm_seal(aes128_block, &rk, n, ad, 8, m, 23, out);
            CHECK(memcmp(out, RFC1, 31) == 0);
            CHECK(ccm_open(aes128_block, &rk, n, ad, 8, out, 23, back) && memcmp(back, m, 23) == 0);
            out[30] ^= 1; CHECK(!ccm_open(aes128_block, &rk, n, ad, 8, out, 23, back)); out[30] ^= 1;
            out[3] ^= 1;  CHECK(!ccm_open(aes128_block, &rk, n, ad, 8, out, 23, back) && back[0] == 0); out[3] ^= 1;
            ad[2] ^= 1;   CHECK(!ccm_open(aes128_block, &rk, n, ad, 8, out, 23, back)); ad[2] ^= 1;
            uint8_t n2[13], ad2[29], m2[10], o2[18];
            for (int i = 0; i < 13; i++) n2[i] = (uint8_t)(0x10 + i);
            for (int i = 0; i < 29; i++) ad2[i] = (uint8_t)(0x40 + i);
            for (int i = 0; i < 10; i++) m2[i] = (uint8_t)(0x60 + i);
            static const uint8_t PY[18] = {0x1c,0x80,0x12,0x22,0xdc,0x39,0x88,0xb3,0xd3,0x30,0x7c,0x4c,0x26,0x0d,
                0xba,0xf0,0xde,0x4d};
            ccm_seal(aes128_block, &ak, n2, ad2, 29, m2, 10, o2);
            CHECK(memcmp(o2, PY, 18) == 0);
            uint8_t m3[40], o3[48];
            for (int i = 0; i < 40; i++) m3[i] = (uint8_t)i;
            static const uint8_t PY3[48] = {0x7c,0xe0,0x72,0x42,0xbc,0x59,0xe8,0xd3,0xb3,0x50,0x42,0x9a,0x23,0x0a,
                0x62,0x8e,0x3a,0xc2,0x47,0x6a,0x6b,0x82,0x88,0x3d,0x25,0x54,0x00,0xc8,0x92,0xa5,0x9b,0x5c,0x25,
                0x5b,0xbd,0xfd,0x05,0xb5,0x82,0xbd,0xd4,0x82,0x49,0x25,0x49,0xb2,0xfb,0xc1};
            ccm_seal(aes128_block, &ak, n2, NULL, 0, m3, 40, o3);
            CHECK(memcmp(o3, PY3, 48) == 0);                  // no associated data, 3 blocks
        }

        uint8_t h[REPORT_MAX_LEN], HUBM[6] = {0x98,0xa3,0x16,0x01,0x02,0x03};
        hello_t hl;
        sat_report_t rp = { .seq = 0x123456, .flags = REP_MOTION | REP_CALIBRATED, .k_short = 77,
                            .k_long = 255, .acks = 96, .rssi = -57, .n_dev = 3, .up_s = 0x01020304 }, got;
        // the report: to the hub, from a made-up sender, the real one inside
        CHECK(build_report(h, B1, HUBM, AP1, 6, &rp, NULL, 42, aes128_block, &ak) == HELLO_LEN);
        uint8_t rta[6]; poke_ta(B1, REPORT_NODE, rta);
        CHECK(memcmp(h + 4, HUBM, 6) == 0 && memcmp(h + 10, rta, 6) == 0 && memcmp(h + 10, B1, 6) != 0);
        CHECK(poke_ta_node(B1, rta) < 0);                    // never taken for a poke's ACK
        uint8_t *hp = malloc(HELLO_LEN);                     // exact size: ASan catches over-reads
        memcpy(hp, h, HELLO_LEN);
        CHECK(parse_hello(hp, HELLO_LEN, &hl) && memcmp(hl.mac, B1, 6) == 0 &&
              memcmp(hl.bssid, AP1, 6) == 0 && hl.ch == 6 && hl.sealed && hl.boot == 42 && hl.seq == 0x123456);
        CHECK(hello_open(hp, HELLO_LEN, aes128_block, &ak, &got, NULL));
        CHECK(got.seq == 0x123456 && got.flags == (REP_MOTION | REP_CALIBRATED) &&
              got.k_short == 77 && got.k_long == 255 && got.acks == 96 &&
              got.rssi == -57 && got.n_dev == 3 && got.up_s == 0x01020304);
        CHECK(!parse_hello(hp, 39, &hl));                    // short
        CHECK(parse_hello(hp, HELLO_LEN - 1, &hl) && !hl.sealed);   // cut: not a report
        free(hp);
        {   // nothing readable, nothing changeable
            static const uint8_t OTHER[16] = {9,9,9,9,9,9,9,9,9,9,9,9,9,9,9,9};
            aes128_t bk; aes128_init(&bk, OTHER);
            CHECK(!hello_open(h, HELLO_LEN, aes128_block, &bk, &got, NULL));   // another key
            bool leak = false;                                           // the plain bytes aren't on air
            for (int i = 53; i < 63; i++) leak |= h[i] == 77 && h[i + 1] == 255;
            CHECK(!leak);
            const int at[] = {24, 31, 32, 33, 34, 40, 45, 46, 50, 52, 53, 62, 63, 70};  // magic, version, channel,
            for (unsigned i = 0; i < sizeof at / sizeof at[0]; i++) {           // router, sender, boot, seq,
                h[at[i]] ^= 0x04;                                               // report, tag
                CHECK(!hello_open(h, HELLO_LEN, aes128_block, &ak, &got, NULL));
                h[at[i]] ^= 0x04;
            }
            CHECK(hello_open(h, HELLO_LEN, aes128_block, &ak, &got, NULL));
            h[40] |= 1; CHECK(parse_hello(h, HELLO_LEN, &hl) && !hl.sealed); h[40] &= 0xFE;  // group sender inside
        }
        {   // version 5: per-link scores and the survey, inside the seal
            rep_extra_t x = {0}, y;
            x.n_links = 3;
            for (int i = 0; i < 3; i++) {
                memcpy(x.link[i].mac, PLUG, 6); x.link[i].mac[5] = (uint8_t)i;
                x.link[i].flags = (uint8_t)(i ? RL_DEVICE : RL_AP | RL_ACK);
                x.link[i].ks = (uint8_t)(20 + i); x.link[i].kl = (uint8_t)(200 + i);
            }
            x.n_seen = REP_SEEN_MAX + 5;                     // too many: only REP_SEEN_MAX go out
            for (int i = 0; i < REP_SEEN_MAX + 5 && i < REP_SEEN_MAX; i++) {
                memcpy(x.seen[i].mac, AP1, 6); x.seen[i].mac[5] = (uint8_t)i;
                x.seen[i].kind = (uint8_t)(i % 3); x.seen[i].rssi = (int8_t)(-40 - i);
            }
            int n = build_report(h, B1, HUBM, AP1, 6, &rp, &x, 43, aes128_block, &ak);
            CHECK(n == 53 + REPORT_LEN + 2 + 27 + 2 + 8 * REP_SEEN_MAX + CCM_TAG && n <= REPORT_MAX_LEN);
            CHECK(parse_hello(h, n, &hl) && hl.sealed && hl.version == 5);
            CHECK(hello_open(h, n, aes128_block, &ak, &got, &y) && got.k_short == 77);
            CHECK(y.n_links == 3 && y.link[2].mac[5] == 2 && y.link[2].flags == RL_DEVICE &&
                  y.link[2].ks == 22 && y.link[2].kl == 202 && y.link[0].flags == (RL_AP | RL_ACK));
            CHECK(y.n_seen == REP_SEEN_MAX && y.seen[29].rssi == -69 && y.seen[29].kind == 29 % 3 &&
                  y.seen[29].mac[5] == 29);
            CHECK(hello_open(h, n, aes128_block, &ak, &got, NULL));      // sections not wanted
            CHECK(!hello_open(h, n - 1, aes128_block, &ak, &got, &y));   // cut frame: the tag fails
            CHECK(!hello_open(h, REPORT_MAX_LEN + 1, aes128_block, &ak, &got, &y));
            h[53 + 15] ^= 1;                                  // inside a section: refused too
            CHECK(!hello_open(h, n, aes128_block, &ak, &got, &y));
            h[53 + 15] ^= 1;
            // a section whose length runs past the end is skipped, never read past
            uint8_t m[64] = {0};
            m[REPORT_LEN] = TLV_LINKS; m[REPORT_LEN + 1] = 200;
            uint8_t fr[128] = {0}, nn[CCM_NONCE], aa[HELLO_AAD_LEN];
            memcpy(fr, h, 53);
            hello_nonce(fr, nn); hello_aad(fr, aa);
            ccm_seal(aes128_block, &ak, nn, aa, HELLO_AAD_LEN, m, REPORT_LEN + 2, fr + 53);
            CHECK(hello_open(fr, 53 + REPORT_LEN + 2 + CCM_TAG, aes128_block, &ak, &got, &y) && y.n_links == 0);
            CHECK(rep_k20(1.0f, 1.0f) == 20 && rep_k20(20.0f, 1.0f) == 255 && rep_k20(1.0f, 0.0f) == 0);
        }
        {   // breathing in the report, and the fullest report still fits
            rep_extra_t x = {0}, y;
            x.n_links = REP_LINKS_MAX;
            for (int i = 0; i < REP_LINKS_MAX; i++) { memcpy(x.link[i].mac, PLUG, 6); x.link[i].mac[5] = (uint8_t)i; }
            x.n_seen = REP_SEEN_MAX;
            for (int i = 0; i < REP_SEEN_MAX; i++) { memcpy(x.seen[i].mac, AP1, 6); x.seen[i].mac[5] = (uint8_t)i; }
            x.has_order = true; x.ord_boot = 3; x.ord_n = 9;
            x.has_breath = true; x.br_level = BR_CLEAR; x.br_rate2 = 28; x.br_prom = 200; x.br_agree = 75;
            x.has_ml = true; x.ml_motion = 1; x.ml_p[0] = 10; x.ml_p[1] = 20; x.ml_p[2] = 225;
            for (int i = 0; i < 8; i++) x.ml_f[i] = (int16_t)(i * 1000 - 3500);
            int n = build_report(h, B1, HUBM, AP1, 6, &rp, &x, 46, aes128_block, &ak);
            CHECK(n == 460 && n <= REPORT_MAX_LEN);
            CHECK(hello_open(h, n, aes128_block, &ak, &got, &y) && y.has_breath && y.br_level == BR_CLEAR &&
                  y.br_rate2 == 28 && y.br_prom == 200 && y.br_agree == 75 && y.has_order && y.n_seen == REP_SEEN_MAX);
            CHECK(y.has_ml && y.ml_motion == 1 && y.ml_p[2] == 225 && y.ml_f[0] == -3500 && y.ml_f[7] == 3500);
        }
        {   // the learned model: weights over the air (MLW), forward pass, verdict
            int a, b, nv; float vals[MLW_VALS_MAX];
            CHECK(parse_mlw("MLW", &a, &b, vals, &nv) == MLW_QUERY && parse_mlw("MLW,default", &a, &b, vals, &nv) == MLW_DEFAULT);
            CHECK(parse_mlw("MLW,begin,12,3", &a, &b, vals, &nv) == MLW_BEGIN && a == 12 && b == 3);
            CHECK(parse_mlw("MLW,begin,17,3", &a, &b, vals, &nv) == MLW_BAD && parse_mlw("MLW,begin,4,2", &a, &b, vals, &nv) == MLW_BAD);
            CHECK(parse_mlw("MLW,40,1.5,-2e-3,7", &a, &b, vals, &nv) == MLW_VALUES && a == 40 && nv == 3 && vals[1] == -2e-3f);
            CHECK(parse_mlw("MLW,commit,27", &a, &b, vals, &nv) == MLW_COMMIT && a == 27);
            CHECK(parse_mlw("MLW,1,x", &a, &b, vals, &nv) == MLW_BAD && parse_mlw("MLW,1,", &a, &b, vals, &nv) == MLW_BAD &&
                  parse_mlw("MLW,1,nan", &a, &b, vals, &nv) == MLW_BAD && parse_mlw("MLWX", &a, &b, vals, &nv) == MLW_BAD);
            CHECK(order_allowed("MLW,0,1,2") && order_allowed("MLW,commit,27"));
            static ml_model_t mm;
            ml_model_default(&mm);
            CHECK(ml_model_ok(&mm) && mm.np == ml_param_count(0, 1));
            float pr[3], quiet[ML_F] = {-3.0f, -3.0f, 1.0f, 0.1f, 0.05f, 0.5f, 0.04f, 0.3f};
            float loud[ML_F] = {-1.5f, -1.6f, 1.2f, 0.7f, 0.2f, 0.2f, 0.15f, 1.5f};
            CHECK(ml_forward(&mm, quiet, NULL, pr) < 0.5f && ml_forward(&mm, loud, NULL, pr) > 0.5f);
            CHECK(fabsf(pr[0] + pr[2] - 1.0f) < 1e-5f && pr[1] == 0.0f);
            // a 3-output model with a hidden layer: probabilities sum to 1
            mm.nh = 4; mm.no = 3; mm.np = (uint16_t)ml_param_count(4, 3);
            for (int i = 0; i < mm.np; i++) mm.p[i] = (i % 7 - 3) * 0.1f;
            for (int i = 0; i < ML_F; i++) mm.p[ML_F + i] = 1.0f;                     // sd
            CHECK(ml_model_ok(&mm));
            ml_forward(&mm, loud, NULL, pr);
            CHECK(fabsf(pr[0] + pr[1] + pr[2] - 1.0f) < 1e-5f && pr[0] > 0 && pr[1] > 0 && pr[2] > 0);
            mm.np--; CHECK(!ml_model_ok(&mm));
            ml_vote_t vt = {0};
            CHECK(ml_vote(&vt, true) == 0 && ml_vote(&vt, true) == 1 && vt.motion);   // 2 of 3: moving
            CHECK(ml_vote(&vt, false) == 0 && ml_vote(&vt, false) == 0 && ml_vote(&vt, false) == -1 && !vt.motion);
        }
        {   // breathing on the board: a 12 /min ripple is found, plain noise isn't
            static br_state_t br;
            float am[N_SC];
            uint32_t rs = 12345;
            for (int pass = 0; pass < 2; pass++) {
                br_init(&br);
                bool got = false;
                for (int i = 0; i < 50 * 60; i++) {            // 60 s at 50 packets/s
                    double t = i / 50.0;
                    for (int k = 0; k < N_SC; k++) {
                        rs = rs * 1103515245u + 12345u;
                        float noise = ((float)((rs >> 8) & 0xFFFF) / 65535.0f - 0.5f) * 0.8f;
                        float ripple = pass == 0 ? 0.01f * (float)sin(2 * M_PI * 0.2 * t) * (k < 26 ? 1.0f : -0.5f) : 0.0f;
                        am[k] = (20.0f + 6.0f * (float)sin(k / 10.0)) * (1.0f + ripple) + noise;
                    }
                    got |= br_feed(&br, (int64_t)(t * 1e6), am);
                }
                CHECK(got && br.have);
                if (pass == 0) CHECK(br.level == BR_CLEAR && fabsf(br.rate - 12.0f) <= 1.0f);
                else CHECK(br.level == BR_NONE);
            }
            br_init(&br);
            for (int k = 0; k < N_SC; k++) am[k] = 20.0f;
            bool early = false;
            for (int i = 0; i < 50 * 20; i++) early |= br_feed(&br, (int64_t)i * 20000, am);
            CHECK(!early);                                    // under 40 s: no reading
        }
        {   // the hub's orders: one command line, sealed for one board, to its reports' sender
            static uint8_t of[ORDER_MAX_LEN];
            order_t o;
            int n = build_order(of, HUBM, B1, 6, 7, 0x010203, "USE,Other net", aes128_block, &ak);
            CHECK(n == 53 + 6 + 13 + CCM_TAG);
            uint8_t rta2[6]; poke_ta(B1, REPORT_NODE, rta2);
            CHECK(is_order_to(of, n, rta2) && memcmp(of + 10, HUBM, 6) == 0);
            CHECK(!parse_hello(of, n, &hl));                 // never taken for a hello or a report
            CHECK(order_open(of, n, B1, aes128_block, &ak, &o) && strcmp(o.line, "USE,Other net") == 0 &&
                  o.boot == 7 && o.n == 0x010203 && memcmp(o.hub, HUBM, 6) == 0);
            static const uint8_t OTHER2[16] = {8,8,8,8,8,8,8,8,8,8,8,8,8,8,8,8};
            aes128_t ck; aes128_init(&ck, OTHER2);
            CHECK(!order_open(of, n, B1, aes128_block, &ck, &o));      // another key
            CHECK(!order_open(of, n, PLUG, aes128_block, &ak, &o));    // sealed for another board
            uint8_t rta3[6]; poke_ta(PLUG, REPORT_NODE, rta3);
            CHECK(!is_order_to(of, n, rta3));
            const int at[] = {33, 40, 46, 50, 52, 53, 60, n - 1};      // channel, hub, boot, number, body, tag
            for (unsigned i = 0; i < sizeof at / sizeof at[0]; i++) {
                of[at[i]] ^= 0x04;
                CHECK(!order_open(of, n, B1, aes128_block, &ak, &o));
                of[at[i]] ^= 0x04;
            }
            of[32] ^= 0x04; CHECK(!order_open(of, n, B1, aes128_block, &ak, &o) && !is_order_to(of, n, rta2)); of[32] ^= 0x04;
            CHECK(!order_open(of, n - 1, B1, aes128_block, &ak, &o));  // cut
            bool leak = false;                                // the command isn't readable on air
            for (int i = 53; i + 3 <= n; i++) leak |= memcmp(of + i, "USE", 3) == 0;
            CHECK(!leak);
            char longl[ORDER_LINE_MAX + 2];
            memset(longl, 'A', sizeof longl); longl[sizeof longl - 1] = '\0';
            CHECK(build_order(of, HUBM, B1, 6, 7, 1, longl, aes128_block, &ak) == 0);
            longl[ORDER_LINE_MAX] = '\0';
            CHECK(build_order(of, HUBM, B1, 6, 7, 1, longl, aes128_block, &ak) == ORDER_MAX_LEN);
            CHECK(order_open(of, ORDER_MAX_LEN, B1, aes128_block, &ak, &o) && strlen(o.line) == ORDER_LINE_MAX);
            CHECK(build_order(of, HUBM, B1, 6, 7, 1, "CAL\nKEY,x", aes128_block, &ak) == 0);
            CHECK(build_order(of, HUBM, B1, 6, 7, 1, "", aes128_block, &ak) == 0);
            // what a board takes from the air
            CHECK(order_allowed("USE,Net") && order_allowed("USE_DEFAULT") && order_allowed("APS,clear") &&
                  order_allowed("CAL") && order_allowed("SENS,150") && order_allowed("SENS") &&
                  order_allowed("SOUND,beep,80,2700") && order_allowed("LEDSET,default") && order_allowed("BEEP,200"));
            CHECK(order_allowed("OUT") && order_allowed("OUT,motion,60") && order_allowed("OUT,default") &&
                  !order_allowed("OUTX") && !order_allowed("OUTPUT,on"));
            CHECK(!order_allowed("KEY,00") && !order_allowed("PEERKEY,clear") && !order_allowed("HUB,clear") &&
                  !order_allowed("POKE,aa") && !order_allowed("CALX") && !order_allowed("SCAN") &&
                  !order_allowed("USEX") && !order_allowed("") && !order_allowed("SENSE"));
            // its reports say which order it did last; the hub stops then
            rep_extra_t x = {0}, y;
            x.has_order = true; x.ord_boot = 7; x.ord_n = 0x010203;
            int rn = build_report(h, B1, HUBM, AP1, 6, &rp, &x, 44, aes128_block, &ak);
            CHECK(rn == 53 + REPORT_LEN + 2 + 7 + CCM_TAG);
            CHECK(hello_open(h, rn, aes128_block, &ak, &got, &y) && y.has_order && y.ord_boot == 7 && y.ord_n == 0x010203);
            x.has_order = false;
            rn = build_report(h, B1, HUBM, AP1, 6, &rp, &x, 45, aes128_block, &ak);
            CHECK(hello_open(h, rn, aes128_block, &ak, &got, &y) && !y.has_order);
        }
        // the hello: broadcast, from the real MAC, nothing about motion
        CHECK(build_hello(h, B1, AP1, 6) == HELLO_V1_LEN);
        CHECK(h[4] == 0xFF && memcmp(h + 10, B1, 6) == 0);
        CHECK(parse_hello(h, HELLO_V1_LEN, &hl) && !hl.sealed && hl.version == 1 && hl.ch == 6 &&
              memcmp(hl.mac, B1, 6) == 0);
        h[32] = 3;                                           // version 3 (broadcast report): not used
        CHECK(parse_hello(h, HELLO_LEN, &hl) && !hl.sealed);
        h[32] = 1;
        CHECK(!parse_hello(NULL, HELLO_LEN, &hl));
        h[28] ^= 1; CHECK(!parse_hello(h, HELLO_LEN, &hl)); h[28] ^= 1;   // not ours
        h[0] = 0x80; CHECK(!parse_hello(h, HELLO_LEN, &hl)); h[0] = 0xD0; // a beacon
        h[10] |= 1; CHECK(!parse_hello(h, HELLO_LEN, &hl)); h[10] &= 0xFE; // group sender
        build_hello(h, B1, NULL, 3);                         // no router found yet: zeros
        CHECK(parse_hello(h, HELLO_V1_LEN, &hl) && hl.ch == 3 && hl.bssid[0] == 0 && hl.bssid[5] == 0);
        beacon_info_t hb; parse_beacon(h, HELLO_V1_LEN, "MyNetwork", &hb);
        CHECK(!hb.ssid_match);                               // never mistaken for our router
        CHECK(rep_k(0.5f, 1.0f) == 25 && rep_k(1.0f, 1.0f) == 50 && rep_k(9.0f, 1.0f) == 255);
        CHECK(rep_k(1.0f, 0.0f) == 0 && rep_k(-1.0f, 1.0f) == 0 && rep_k(0.0f / 0.0f, 1.0f) == 0);

        // Finding the hub: our own channel first; after 3 misses, one channel per report
        hub_link_t hk = {0};
        CHECK(hub_next_ch(&hk, 6, 13) == 6);
        CHECK(!hub_result(&hk, true, 13) && hk.ch == 6);              // answered: stay
        for (int i = 1; i < HUB_MISS_MAX; i++) CHECK(!hub_result(&hk, false, 13) && hk.ch == 6);  // a few: stay
        CHECK(!hub_result(&hk, false, 13) && hk.search && hk.ch == 7);  // then: search
        CHECK(!hub_result(&hk, false, 13) && hk.ch == 8);
        for (int i = 0; i < 4; i++) hub_result(&hk, false, 13);
        CHECK(hk.ch == 12 && !hub_result(&hk, false, 13) && hk.ch == 13);
        CHECK(!hub_result(&hk, false, 13) && hk.ch == 1);             // wraps
        CHECK(hub_result(&hk, true, 13) && hk.ch == 1 && !hk.search);  // found: save channel 1
        CHECK(!hub_result(&hk, true, 13));                            // already known: nothing to save
        hub_link_t bad = { .ch = 40 };
        CHECK(hub_next_ch(&bad, 11, 11) == 11 && hub_next_ch(&bad, 11, 11) == 11);  // a saved 5 GHz / bad one
        hub_link_t none = {0};
        CHECK(hub_next_ch(&none, 0, 13) == 1);                        // no channel of our own yet
        uint8_t hm[6];
        CHECK(parse_hub("HUB", hm) == HUB_QUERY && parse_hub("HUB,clear\r\n", hm) == HUB_CLEAR);
        {
            char line[32];
            snprintf(line, sizeof line, "HUB,%02x:%02x:%02x:%02x:%02x:%02x", HUBM[0], HUBM[1], HUBM[2],
                     HUBM[3], HUBM[4], HUBM[5]);
            CHECK(parse_hub(line, hm) == HUB_SET && memcmp(hm, HUBM, 6) == 0);
            line[4] = '9'; line[5] = '9';                             // 99: a group address
            CHECK(parse_hub(line, hm) == HUB_BAD);
            strcat(line, "x");
            CHECK(parse_hub(line, hm) == HUB_BAD);
        }
        CHECK(parse_hub("HUBX", hm) == HUB_BAD && parse_hub("HUB,", hm) == HUB_BAD);

        // Replays: only newer (boot, seq) passes; a new boot is always newer
        CHECK(replay_newer(5, 1, 4, 999) && replay_newer(5, 11, 5, 10));
        // Saved counters: rounded down to a mark; saved on a restart or a new mark
        CHECK(replay_mark(5000) == 4096 && replay_mark(1023) == 0 && replay_mark(1024) == 1024);
        CHECK(replay_save_due(3, 1023, 4, 1));                   // the board restarted
        CHECK(replay_save_due(3, 1023, 3, 1024));                // passed a mark
        CHECK(!replay_save_due(3, 1024, 3, 2000));               // same mark: no write
        CHECK(!replay_newer(3, 4000, 3, replay_mark(4100)) && replay_newer(3, 4200, 3, replay_mark(4100)));
        CHECK(!replay_newer(5, 10, 5, 10) && !replay_newer(5, 9, 5, 10) && !replay_newer(4, 999, 5, 1));
        CHECK(replay_newer(1, 1, 0, 0));                     // the first report from a new key

        // KEY and PEERKEY from the PC
        uint8_t kk[16], km[6];
        CHECK(parse_key("KEY", kk) == KEY_QUERY && parse_key("KEY\r\n", kk) == KEY_QUERY);
        CHECK(parse_key("KEY,clear", kk) == KEY_CLEAR);
        CHECK(parse_key("KEY,000102030405060708090a0b0c0d0E0F", kk) == KEY_SET && kk[0] == 0 && kk[15] == 15);
        CHECK(parse_key("KEY,00000000000000000000000000000000", kk) == KEY_BAD);   // all zero
        CHECK(parse_key("KEY,000102030405060708090a0b0c0d0e", kk) == KEY_BAD);     // short
        CHECK(parse_key("KEY,000102030405060708090a0b0c0d0e0f00", kk) == KEY_BAD); // long
        CHECK(parse_key("KEY,0g0102030405060708090a0b0c0d0e0f", kk) == KEY_BAD);
        CHECK(parse_key("KEYS", kk) == KEY_BAD && parse_key("KEY,", kk) == KEY_BAD);
        CHECK(parse_peerkey("PEERKEY", km, kk) == PK_QUERY && parse_peerkey("PEERKEY,clear", km, kk) == PK_CLEAR_ALL);
        char pl_[96], b1s[18], gs[18];                         // addresses built here: no MAC literals in git
        snprintf(b1s, sizeof b1s, "%02x:%02x:%02x:%02x:%02x:%02x", B1[0], B1[1], B1[2], B1[3], B1[4], B1[5]);
        snprintf(gs, sizeof gs, "%02x%s", B1[0] | 1, b1s + 2);           // a group address
#define PK(fmt, ...) (snprintf(pl_, sizeof pl_, fmt, __VA_ARGS__), parse_peerkey(pl_, km, kk))
        CHECK(PK("PEERKEY,%s,%s", b1s, "000102030405060708090a0b0c0d0e0f") == PK_SET &&
              memcmp(km, B1, 6) == 0 && kk[1] == 1);
        CHECK(PK("PEERKEY,%s,clear", b1s) == PK_CLEAR_ONE);
        CHECK(PK("PEERKEY,%s,%s", gs, "000102030405060708090a0b0c0d0e0f") == PK_BAD);        // group
        CHECK(PK("PEERKEY,%.14s,%s", b1s, "000102030405060708090a0b0c0d0e0f") == PK_BAD);    // cut
        CHECK(PK("PEERKEY,%s", b1s) == PK_BAD);
        CHECK(PK("PEERKEY,%s,00", b1s) == PK_BAD);
#undef PK
        CHECK(parse_peerkey("PEERKEYX", km, kk) == PK_BAD && parse_key("PEERKEY", kk) == KEY_BAD);
        {
            peer_keys_t pkt; memset(&pkt, 0, sizeof pkt);
            uint8_t key1[16] = {1}, key2[16] = {2};
            int i1 = peer_key_set(&pkt, B1, key1);
            pkt.k[i1].boot = 7; pkt.k[i1].seq = 99;
            CHECK(i1 >= 0 && peer_key_find(&pkt, B1) == i1);
            CHECK(peer_key_set(&pkt, B1, key1) == i1 && pkt.k[i1].seq == 99);   // same key: counters kept
            CHECK(peer_key_set(&pkt, B1, key2) == i1 && pkt.k[i1].seq == 0 && pkt.k[i1].key[0] == 2);
            for (int j = 1; j < MAX_PEERS; j++) {
                uint8_t m[6]; memcpy(m, B2, 6); m[5] = (uint8_t)j;
                CHECK(peer_key_set(&pkt, m, key1) >= 0);
            }
            uint8_t m9[6]; memcpy(m9, B2, 6); m9[5] = 0x99;
            CHECK(peer_key_set(&pkt, m9, key1) == -1 && peer_key_find(&pkt, m9) == -1);   // 8 at most
        }

        peer_list_t pl; memset(&pl, 0, sizeof pl);
        int k = peer_note(&pl, B1, AP1, 6, -50, 1000);
        CHECK(k >= 0 && peer_find(&pl, B1, 1000) == k && pl.p[k].n == 1 && pl.p[k].rssi_x16 == -800);
        CHECK(peer_note(&pl, B1, AP1, 6, -60, 2000) == k && pl.p[k].n == 2 && pl.p[k].rssi_x16 / 16 == -52);
        CHECK(peer_find(&pl, B2, 2000) < 0);
        CHECK(peer_find(&pl, B1, 2000 + PEER_GONE_MS + 1) < 0);          // gone after 10 s
        for (int j = 0; j < MAX_PEERS - 1; j++) {
            uint8_t m[6]; memcpy(m, B2, 6); m[5] = (uint8_t)(0x40 + j);
            CHECK(peer_note(&pl, m, AP1, 6, -70, 2000) >= 0);
        }
        uint8_t extra[6] = {0x24,0x0a,0xc4,0x11,0x22,0x99};
        CHECK(peer_note(&pl, extra, AP1, 6, -70, 2500) == -1);           // full of live peers
        CHECK(peer_note(&pl, extra, AP1, 6, -70, 2000 + PEER_GONE_MS + 1) >= 0);  // reuses a gone one
        CHECK(peer_find(&pl, extra, 2000 + PEER_GONE_MS + 1) >= 0);

        // A peer's pokes to our router come from its made-up addresses: not devices
        peer_list_t one; memset(&one, 0, sizeof one);
        peer_note(&one, B1, AP1, 6, -50, 1000);
        uint8_t ta[6];
        poke_ta(B1, 0, ta);  CHECK(peer_poke_addr(&one, ta));
        poke_ta(B1, DEV_NODE(2), ta); CHECK(peer_poke_addr(&one, ta));
        CHECK(!peer_poke_addr(&one, B1) && !peer_poke_addr(&one, PLUG));
        poke_ta(B2, 0, ta);  CHECK(!peer_poke_addr(&one, ta));
        node_table_t pt; memset(&pt, 0, sizeof pt); node_add(&pt, AP1);
        seen_list_t ps; memset(&ps, 0, sizeof ps);
        uint8_t df2[24] = {0x48, 0x01};
        memcpy(df2 + 4, AP1, 6); poke_ta(B1, 0, df2 + 10); memcpy(df2 + 16, AP1, 6);
        int pe = seen_note(&ps, &pt, df2, 24, -50, 100);     // heard before the hello
        memcpy(df2 + 10, PLUG, 6);
        int de = seen_note(&ps, &pt, df2, 24, -50, 100);
        CHECK(pe >= 0 && de >= 0 && pe != de);
        seen_forget_peer(&ps, B1);
        CHECK(!ps.d[pe].used && ps.d[de].used);              // the real device stays
    }


    printf("%d passed, %d failed\n", passes, fails);
    if (fails == 0) printf("ALL C TESTS PASSED\n");
    return fails ? 1 : 0;
}
