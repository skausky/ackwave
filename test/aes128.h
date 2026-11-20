// AES-128 encryption of one block, for the host tests only (the board uses
// its AES hardware). Straightforward FIPS-197; checked against its vector.
#pragma once
#include <stdint.h>
#include <string.h>

typedef struct { uint8_t rk[176]; } aes128_t;

static uint8_t aes_sbox(uint8_t x) {
    // multiplicative inverse in GF(2^8), then the affine map
    uint8_t inv = 0;
    if (x) {
        uint8_t a = x, r = 1;
        for (int e = 254; e; e >>= 1) {          // x^254 = x^-1
            if (e & 1) {
                uint8_t p = 0, b = a, c = r;
                for (int i = 0; i < 8; i++) { if (c & 1) p ^= b; b = (uint8_t)((b << 1) ^ (b & 0x80 ? 0x1b : 0)); c >>= 1; }
                r = p;
            }
            uint8_t p = 0, b = a, c = a;
            for (int i = 0; i < 8; i++) { if (c & 1) p ^= b; b = (uint8_t)((b << 1) ^ (b & 0x80 ? 0x1b : 0)); c >>= 1; }
            a = p;
        }
        inv = r;
    }
    uint8_t s = inv;
    for (int i = 1; i < 5; i++) s ^= (uint8_t)((inv << i) | (inv >> (8 - i)));
    return s ^ 0x63;
}

static uint8_t SBOX[256];

static void aes128_init(aes128_t *a, const uint8_t key[16]) {
    if (!SBOX[0]) for (int i = 0; i < 256; i++) SBOX[i] = aes_sbox((uint8_t)i);
    memcpy(a->rk, key, 16);
    uint8_t rcon = 1;
    for (int i = 16; i < 176; i += 4) {
        uint8_t t[4];
        memcpy(t, a->rk + i - 4, 4);
        if (i % 16 == 0) {
            uint8_t u = t[0];
            t[0] = SBOX[t[1]] ^ rcon; t[1] = SBOX[t[2]]; t[2] = SBOX[t[3]]; t[3] = SBOX[u];
            rcon = (uint8_t)((rcon << 1) ^ (rcon & 0x80 ? 0x1b : 0));
        }
        for (int j = 0; j < 4; j++) a->rk[i + j] = a->rk[i - 16 + j] ^ t[j];
    }
}

static uint8_t xt(uint8_t b) { return (uint8_t)((b << 1) ^ (b & 0x80 ? 0x1b : 0)); }

static void aes128_block(void *ctx, const uint8_t in[16], uint8_t out[16]) {
    const aes128_t *a = ctx;
    uint8_t s[16];
    for (int i = 0; i < 16; i++) s[i] = in[i] ^ a->rk[i];
    for (int r = 1; r <= 10; r++) {
        uint8_t t[16];
        for (int i = 0; i < 16; i++) t[i] = SBOX[s[i]];
        for (int c = 0; c < 4; c++)                 // shift rows: column c takes row j from c + j
            for (int j = 0; j < 4; j++) s[4 * c + j] = t[4 * ((c + j) % 4) + j];
        if (r < 10)
            for (int c = 0; c < 4; c++) {           // mix columns
                uint8_t *m = s + 4 * c, x0 = m[0], x1 = m[1], x2 = m[2], x3 = m[3];
                m[0] = xt(x0) ^ xt(x1) ^ x1 ^ x2 ^ x3;
                m[1] = x0 ^ xt(x1) ^ xt(x2) ^ x2 ^ x3;
                m[2] = x0 ^ x1 ^ xt(x2) ^ xt(x3) ^ x3;
                m[3] = xt(x0) ^ x0 ^ x1 ^ x2 ^ xt(x3);
            }
        for (int i = 0; i < 16; i++) s[i] ^= a->rk[16 * r + i];
    }
    memcpy(out, s, 16);
}
