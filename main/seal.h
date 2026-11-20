// AES-CCM (RFC 3610) for the boards' reports: 13-byte nonce, 8-byte tag.
// Pure logic over a block-encrypt function, so the host tests run the same
// code as the board (which uses its AES hardware through mbedtls_aes).
#pragma once
#include <stdbool.h>
#include <stdint.h>
#include <string.h>

#define CCM_NONCE 13
#define CCM_TAG   8

// Encrypts one 16-byte block with the key the caller set up
typedef void (*aes_block_fn)(void *ctx, const uint8_t in[16], uint8_t out[16]);

static inline void ccm_xor(uint8_t *a, const uint8_t *b, int n) {
    for (int i = 0; i < n; i++) a[i] ^= b[i];
}

// CBC-MAC over B0, the associated data and the message, then the tag.
// aad_len < 0xFF00, len < 0x10000 (L = 2).
static inline void ccm_mac(aes_block_fn enc, void *ctx, const uint8_t nonce[CCM_NONCE],
                           const uint8_t *aad, int aad_len, const uint8_t *msg, int len,
                           uint8_t x[16]) {
    uint8_t b[16];
    b[0] = (uint8_t)((aad_len ? 0x40 : 0) | (((CCM_TAG - 2) / 2) << 3) | (2 - 1));
    memcpy(b + 1, nonce, CCM_NONCE);
    b[14] = (uint8_t)(len >> 8);
    b[15] = (uint8_t)len;
    enc(ctx, b, x);
    if (aad_len) {
        memset(b, 0, 16);
        b[0] = (uint8_t)(aad_len >> 8);
        b[1] = (uint8_t)aad_len;
        int k = 2;                                 // the length, then the data, in 16-byte blocks
        for (int i = 0; i < aad_len; i++) {
            b[k++] = aad[i];
            if (k == 16) { ccm_xor(x, b, 16); enc(ctx, x, x); memset(b, 0, 16); k = 0; }
        }
        if (k) { ccm_xor(x, b, 16); enc(ctx, x, x); }
    }
    for (int i = 0; i < len; i += 16) {
        int n = len - i < 16 ? len - i : 16;
        memset(b, 0, 16);
        memcpy(b, msg + i, n);
        ccm_xor(x, b, 16);
        enc(ctx, x, x);
    }
}

// Counter block i encrypted: the keystream
static inline void ccm_ctr(aes_block_fn enc, void *ctx, const uint8_t nonce[CCM_NONCE], int i,
                           uint8_t s[16]) {
    uint8_t a[16];
    a[0] = 2 - 1;
    memcpy(a + 1, nonce, CCM_NONCE);
    a[14] = (uint8_t)(i >> 8);
    a[15] = (uint8_t)i;
    enc(ctx, a, s);
}

static inline void ccm_crypt(aes_block_fn enc, void *ctx, const uint8_t nonce[CCM_NONCE],
                             const uint8_t *in, int len, uint8_t *out) {
    uint8_t s[16];
    for (int i = 0; i < len; i += 16) {
        int n = len - i < 16 ? len - i : 16;
        ccm_ctr(enc, ctx, nonce, i / 16 + 1, s);
        for (int j = 0; j < n; j++) out[i + j] = in[i + j] ^ s[j];
    }
}

// out = ciphertext (len bytes), then the tag (CCM_TAG bytes). out may equal msg.
static inline void ccm_seal(aes_block_fn enc, void *ctx, const uint8_t nonce[CCM_NONCE],
                            const uint8_t *aad, int aad_len, const uint8_t *msg, int len,
                            uint8_t *out) {
    uint8_t x[16], s0[16];
    ccm_mac(enc, ctx, nonce, aad, aad_len, msg, len, x);
    ccm_crypt(enc, ctx, nonce, msg, len, out);
    ccm_ctr(enc, ctx, nonce, 0, s0);
    for (int j = 0; j < CCM_TAG; j++) out[len + j] = x[j] ^ s0[j];
}

// ct = ciphertext then tag. True (and out = the message) only if the tag is
// right; on a wrong tag out is wiped. out may equal ct.
static inline bool ccm_open(aes_block_fn enc, void *ctx, const uint8_t nonce[CCM_NONCE],
                            const uint8_t *aad, int aad_len, const uint8_t *ct, int len,
                            uint8_t *out) {
    uint8_t tag[CCM_TAG], x[16], s0[16];
    memcpy(tag, ct + len, CCM_TAG);
    ccm_crypt(enc, ctx, nonce, ct, len, out);
    ccm_mac(enc, ctx, nonce, aad, aad_len, out, len, x);
    ccm_ctr(enc, ctx, nonce, 0, s0);
    uint8_t diff = 0;                              // constant time
    for (int j = 0; j < CCM_TAG; j++) diff |= (uint8_t)(tag[j] ^ x[j] ^ s0[j]);
    if (diff) memset(out, 0, (size_t)len);
    return diff == 0;
}
