#include <stdio.h>
#include <string.h>
#include <inttypes.h>
#include <math.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_timer.h"
#include "nvs_flash.h"
#include "driver/gpio.h"
#include "driver/ledc.h"
#include "driver/rmt_tx.h"
#include "hal/usb_serial_jtag_ll.h"
#include "csi_core.h"

// The network to watch is chosen at run time (`./csi network`: SCAN, then
// USE,<name>) and saved on the board. main/my_network.h (git-ignored) may set
// MY_SSID as the default for a board that has none saved yet, plus local
// hardware settings. The board never joins the network: no password is needed.
#if __has_include("my_network.h")
#include "my_network.h"
#endif
#ifndef MY_SSID
#define MY_SSID ""                // none: wait for USE,<name> from the PC
#endif

// ---------------- EDIT THESE ----------------
#ifndef MAX_CH
#define MAX_CH         13        // use 11 in the US
#endif
#ifndef POKE_HZ
#define POKE_HZ        100       // Null frames/s sent to the AP for ACK CSI; 0 = off
#endif
#ifndef CSI_PROBE
#define CSI_PROBE      0         // 1 = log every CSI/ACK callback (hardware bring-up)
#endif
#ifndef BUZZER_GPIO
#define BUZZER_GPIO    -1        // piezo pin, -1 = no buzzer
#endif
#ifndef BUZZER_ACTIVE
#define BUZZER_ACTIVE  0         // 0 = passive piezo (PWM tone), 1 = active buzzer
#endif
#ifndef CAL_BUTTON_GPIO
#define CAL_BUTTON_GPIO 0        // press to recalibrate (BOOT button); -1 = none
#endif
#ifndef CAL_DELAY_S
#define CAL_DELAY_S    5         // after a button press: time to step away
#endif
#ifndef LED_GPIO
#define LED_GPIO       48        // on-board RGB LED (WS2812): 48 on most S3 boards,
#endif                           // 38 on DevKitC-1 v1.1; -1 = no LED
#ifndef LED_BRIGHT
#define LED_BRIGHT     40        // motion colour brightness, 1-255
#endif
// Detector thresholds, multiples of the measured quiet level. Lower = more
// sensitive. Defaults (md_cfg_default in csi_core.h): short 2.8/2.0, long 1.4/1.2
// #define MD_ON_SHORT 2.8f
// #define MD_ON_LONG  1.5f
// --------------------------------------------

#if CAL_BUTTON_GPIO >= 0 && CAL_BUTTON_GPIO == BUZZER_GPIO
#error "CAL_BUTTON_GPIO and BUZZER_GPIO must differ"
#endif
#if LED_GPIO >= 0 && (LED_GPIO == BUZZER_GPIO || LED_GPIO == CAL_BUTTON_GPIO)
#error "LED_GPIO must differ from BUZZER_GPIO and CAL_BUTTON_GPIO"
#endif

#define LOST_MS        2000      // rescan after 2 s without beacons/CSI
#define DWELL_MS       150       // > one beacon interval (~102 ms)
#define POLL_MS        50
#define ACK_WIN_US     10000     // sanity bound only: ACKs are matched by dmac
#define CSI_OUT_LEN    128       // bytes sent per packet: LLTF only (all we use)
#define STATS_MS       5000
#define CALIB_EVERY_US 5000000   // CALIB line period (also sent on every change)
#ifndef BUZZER_HZ
#define BUZZER_HZ      2700      // tone for a passive piezo (try a sweep: BEEP,700,<hz>)
#endif
#define BEEP_MS        150
#ifndef BUZZER_VOL
#define BUZZER_VOL     50        // loudness, % (50 = full; a piezo is much quieter at 5-10)
#endif
#ifndef MOTION_ALARM
#define MOTION_ALARM   0         // 1 = loop an air-raid whoop while motion lasts (instead of one beep)
#endif
#ifndef BEEP_GAP_MS
#define BEEP_GAP_MS    30000     // at most one motion beep per this long
#endif
#ifndef LINK_MIN_ACKS
#define LINK_MIN_ACKS  50        // router ACKs/s below this = weak link: no motion beeps
#endif
#ifndef LINK_MIN_RSSI
#define LINK_MIN_RSSI  (-85)     // ...or a signal below this (dBm)
#endif
#ifndef BUZZER_SOFT_HZ
#define BUZZER_SOFT_HZ 2000      // pitch of the quieter status sounds
#endif
#ifndef BEEP_AFTER_MS
#define BEEP_AFTER_MS  2000      // beep once motion has lasted this long; 0 = at once
#endif
#define WIFI_CORE      0         // default Wi-Fi task core

static node_table_t nodes;
static int node_ch[MAX_NODES];   // channel each node was last heard on
static poke_gate_t gate;          // which APs / devices are worth poking
static seen_list_t seen;          // devices heard on our network
static portMUX_TYPE seen_mux = portMUX_INITIALIZER_UNLOCKED;
static QueueHandle_t q;
static uint8_t my_mac[6];
static volatile uint32_t cal_request;   // set by the button, consumed by print_task
static bool btn_ok;                      // button pin configured

// Shared with the Wi-Fi task. All 32-bit, and every task that touches them is
// pinned to the Wi-Fi core, so reads/writes are atomic and ordered.
static volatile int      cur_ch = 1;
static volatile uint32_t ch_epoch = 0;       // bumps on every channel change
static volatile uint32_t last_seen_ms = 0;
static volatile int      found_ch = 0;
static volatile int      csa_ch = 0;
static volatile uint32_t csa_at_ms = 0;
static volatile bool     scanning = false;
static poke_state_t      pk = {-1, 0};

// The network we watch. Written only at boot (before Wi-Fi starts), read by the
// beacon callback; a new choice is saved and the board restarts.
static char my_ssid[33];
static volatile bool     net_collect = false;  // SCAN running: note every beacon
static net_list_t        nets;                 // written by the Wi-Fi task while collecting
static volatile uint32_t scan_request;         // set by run_cmd, done by control_task

// A choice made from the PC is saved together with the build default it
// replaced, so flashing a build with a different default (a new
// my_network.h) makes that default win again.
static void ssid_load(void) {
    nvs_handle_t h;
    char saved[33] = "", base[33] = "";
    size_t n1 = sizeof saved, n2 = sizeof base;
    if (nvs_open("ackwave", NVS_READONLY, &h) == ESP_OK) {
        if (nvs_get_str(h, "ssid", saved, &n1) != ESP_OK) saved[0] = '\0';
        if (nvs_get_str(h, "dflt", base, &n2) != ESP_OK) base[0] = '\0';
        nvs_close(h);
    }
    const char *use = saved[0] && strcmp(base, MY_SSID) == 0 ? saved : MY_SSID;
    strncpy(my_ssid, use, sizeof my_ssid - 1);
    my_ssid[sizeof my_ssid - 1] = '\0';
}

// name = NULL: forget the choice (back to the build default)
static bool ssid_save(const char *name) {
    nvs_handle_t h;
    if (nvs_open("ackwave", NVS_READWRITE, &h) != ESP_OK) return false;
    bool ok = name ? nvs_set_str(h, "ssid", name) == ESP_OK && nvs_set_str(h, "dflt", MY_SSID) == ESP_OK
                   : nvs_erase_all(h) == ESP_OK;
    ok = ok && nvs_commit(h) == ESP_OK;
    nvs_close(h);
    return ok;
}

static void print_network(void) {
    const char *how = !my_ssid[0] ? "none" : strcmp(my_ssid, MY_SSID) == 0 ? "default" : "chosen";
    printf("NETWORK,%s,%s\n", how, my_ssid);
}

static volatile uint32_t n_poke, n_tx_err, n_ack, n_ap, n_drop, n_qfull, n_reply;

static inline uint32_t now_ms(void) { return (uint32_t)(esp_timer_get_time() / 1000); }
static inline uint32_t now_us(void) { return (uint32_t)esp_timer_get_time(); }

static void set_ch(int ch) {
    esp_wifi_set_channel(ch, WIFI_SECOND_CHAN_NONE);
    cur_ch = ch;
    ch_epoch++;
}

// ---------------- Optional buzzer ----------------
// A piezo can't be detected electrically, so it's opt-in via BUZZER_GPIO.
// Any setup failure just disables it; capture keeps running.
// Sounds are short tunes played by their own task, so a beep never holds up
// sensing; a new tune cuts off the one playing.
static bool buzzer_ok;
static QueueHandle_t tune_q;

typedef struct { uint16_t hz, ms, hz2; } note_t;  // hz 0 = rest; hz2 set = glide hz -> hz2
typedef struct { note_t n[4]; uint8_t vol; } tune_t;  // ends at the first ms == 0; vol % (0 = BUZZER_VOL)
static int buzz_vol = BUZZER_VOL;            // volume of the tune playing (buzzer task only)

// Rising = something started, falling = it ended, low = a problem.
// Motion uses BUZZER_HZ and a minor third above it.
static const tune_t TUNE_HELLO  = {{{BUZZER_SOFT_HZ, 70, 0}, {3000, 70, 0}, {BUZZER_HZ, 120, 0}}, 0};  // rising chirp
static const tune_t TUNE_TICK   = {{{3000, 30, 0}}, 0};                                          // got the command
static const tune_t TUNE_READY  = {{{BUZZER_SOFT_HZ, 50, 0}, {0, 80, 0}, {BUZZER_SOFT_HZ, 50, 0}}, 0}; // two soft blips
static const tune_t TUNE_MOTION = {{{BUZZER_HZ, 70, 0}, {0, 40, 0}, {BUZZER_HZ * 6 / 5, 110, 0}}, 0}; // bi-ding: rising = started
static const tune_t TUNE_STILL  = {{{BUZZER_HZ * 6 / 5, 70, 0}, {0, 40, 0}, {BUZZER_HZ, 110, 0}}, 0}; // ding-bi: falling = ended
static const tune_t TUNE_LOST   = {{{2000, 120, 0}, {1500, 120, 0}, {1000, 300, 0}}, 0};               // doo-doo-dooo
// Alarm mode: an air-raid whoop, looped while motion lasts, always at full volume
static const tune_t TUNE_WHOOP  = {{{800, 900, 6000}, {0, 250, 0}}, 50};
#define WHOOP_EVERY_US 1150000
static const tune_t TUNE_FOUND  = {{{1500, 80, 0}, {0, 30, 0}, {2500, 120, 0}}, 0};                    // doo-dee

static void buzzer_set(bool on) {
#if BUZZER_ACTIVE
    gpio_set_level(BUZZER_GPIO, on);
#else
    int v = buzz_vol < 1 ? 1 : buzz_vol > 50 ? 50 : buzz_vol;      // past 50 % gets quieter again
    ledc_set_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_0, on ? (uint32_t)v * 1024 / 100 : 0);
    ledc_update_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_0);
#endif
}

// Pitch of the next note (passive piezo only; an active buzzer has its own)
static void buzzer_pitch(int hz) {
#if !BUZZER_ACTIVE
    ledc_set_freq(LEDC_LOW_SPEED_MODE, LEDC_TIMER_0, hz);
#else
    (void)hz;
#endif
}

static void buzzer_task(void *arg) {
    tune_t t;
    xQueueReceive(tune_q, &t, portMAX_DELAY);
    for (;;) {
        bool cut = false;
        buzz_vol = t.vol ? t.vol : BUZZER_VOL;
        for (int i = 0; i < 4 && t.n[i].ms && !cut; i++) {
            note_t n = t.n[i];
            if (n.hz) buzzer_pitch(n.hz);
            buzzer_set(n.hz != 0);
            if (n.hz && n.hz2 && n.hz2 != n.hz) {    // glide: even steps in pitch, every 2 ms
                int64_t t0 = esp_timer_get_time(), len = (int64_t)n.ms * 1000;
                for (;;) {
                    int64_t el = esp_timer_get_time() - t0;
                    if (el >= len) break;
                    buzzer_pitch((int)(n.hz * powf((float)n.hz2 / n.hz, (float)el / len)));
                    if (xQueueReceive(tune_q, &t, pdMS_TO_TICKS(2)) == pdTRUE) { cut = true; break; }
                }
            } else {
                cut = xQueueReceive(tune_q, &t, pdMS_TO_TICKS(n.ms)) == pdTRUE;
            }
        }
        buzzer_set(false);
        if (!cut) xQueueReceive(tune_q, &t, portMAX_DELAY);
    }
}

static void buzzer_play(const tune_t *t) {
    if (buzzer_ok) xQueueOverwrite(tune_q, t);
}

static void buzzer_init(void) {
    buzzer_ok = false;
    if (BUZZER_GPIO < 0) { printf("BUZZER,off\n"); return; }
    esp_err_t err;
#if BUZZER_ACTIVE
    gpio_config_t io = { .pin_bit_mask = 1ULL << BUZZER_GPIO, .mode = GPIO_MODE_OUTPUT };
    err = gpio_config(&io);
    if (err == ESP_OK) err = gpio_set_drive_capability(BUZZER_GPIO, GPIO_DRIVE_CAP_3);  // ~40 mA: louder
#else
    ledc_timer_config_t t = {
        .speed_mode = LEDC_LOW_SPEED_MODE, .duty_resolution = LEDC_TIMER_10_BIT,
        .timer_num = LEDC_TIMER_0, .freq_hz = BUZZER_HZ, .clk_cfg = LEDC_AUTO_CLK,
    };
    err = ledc_timer_config(&t);
    if (err == ESP_OK) {
        ledc_channel_config_t c = {
            .gpio_num = BUZZER_GPIO, .speed_mode = LEDC_LOW_SPEED_MODE,
            .channel = LEDC_CHANNEL_0, .timer_sel = LEDC_TIMER_0, .duty = 0,
        };
        err = ledc_channel_config(&c);
    }
    if (err == ESP_OK) err = gpio_set_drive_capability(BUZZER_GPIO, GPIO_DRIVE_CAP_3);  // ~40 mA: louder
#endif
    if (err == ESP_OK) {
        tune_q = xQueueCreate(1, sizeof(tune_t));
        if (!tune_q || xTaskCreate(buzzer_task, "buzzer", 2048, NULL, 2, NULL) != pdPASS)
            err = ESP_ERR_NO_MEM;
    }
    if (err != ESP_OK) {
        printf("BUZZER,error,%s\n", esp_err_to_name(err));   // keep going without it
        return;
    }
    buzzer_ok = true;
    buzzer_set(false);
    printf("BUZZER,gpio%d\n", BUZZER_GPIO);
}


// ---------------- Status LED (on-board WS2812) ----------------
// red = motion (brighter = stronger), amber fading over 30 s = motion just
// ended, quick dim purple = a blip too faint to count, slow blue pulse =
// calibrating, dim yellow = looking for the router, off = still.
// Only control_task writes it (led_update); print_task sets the inputs.
// A missing LED can't be detected; on a pin with nothing on it this is harmless.
static bool led_ok;
static volatile bool led_motion, led_cal;    // set by print_task
static volatile uint8_t led_strength;        // motion score / threshold x10 (10 = at threshold)
static volatile uint32_t led_blip_ms;        // when a too-faint blip was last seen, 0 = never
static volatile uint32_t led_ended_ms;       // when motion last ended, 0 = never
#define LED_FADE_MS  30000                   // "recent motion" amber fade
#define LED_BLIP_MS  150
static rmt_channel_handle_t led_chan;
static rmt_encoder_handle_t led_enc;

static void led_init(void) {
    led_ok = false;
    if (LED_GPIO < 0) { printf("LED,off\n"); return; }
    rmt_tx_channel_config_t tc = {
        .gpio_num = LED_GPIO, .clk_src = RMT_CLK_SRC_DEFAULT,
        .resolution_hz = 10000000, .mem_block_symbols = 48, .trans_queue_depth = 2,
    };
    esp_err_t err = rmt_new_tx_channel(&tc, &led_chan);
    if (err == ESP_OK) {
        rmt_bytes_encoder_config_t bc = {           // WS2812 bit timing, 0.1 us ticks
            .bit0 = { .level0 = 1, .duration0 = 3, .level1 = 0, .duration1 = 9 },
            .bit1 = { .level0 = 1, .duration0 = 9, .level1 = 0, .duration1 = 3 },
            .flags.msb_first = 1,
        };
        err = rmt_new_bytes_encoder(&bc, &led_enc);
    }
    if (err == ESP_OK) err = rmt_enable(led_chan);
    if (err != ESP_OK) {
        printf("LED,error,%s\n", esp_err_to_name(err));   // keep going without it
        return;
    }
    led_ok = true;
    printf("LED,gpio%d\n", LED_GPIO);
}

static bool led_rgb(uint8_t r, uint8_t g, uint8_t b) {
    static uint8_t grb[3];                   // must outlive the transfer
    if (!led_ok || rmt_tx_wait_all_done(led_chan, 10) != ESP_OK) return false;
    grb[0] = g; grb[1] = r; grb[2] = b;
    rmt_transmit_config_t t = { .loop_count = 0 };
    return rmt_transmit(led_chan, led_enc, grb, sizeof grb, &t) == ESP_OK;
}

static void led_hello(void) {                // white for 0.5 s at power-on
    if (!led_rgb(LED_BRIGHT, LED_BRIGHT, LED_BRIGHT)) return;
    vTaskDelay(pdMS_TO_TICKS(500));
    led_rgb(0, 0, 0);
}

static uint32_t clock_ms(void) { return (uint32_t)(esp_timer_get_time() / 1000); }

static void led_update(bool searching) {
    static uint32_t shown = UINT32_MAX;      // last colour sent, 0x00RRGGBB
    if (!led_ok) return;
    const int B = LED_BRIGHT, d = B / 5 ? B / 5 : 1;
    uint32_t t = clock_ms(), ended = led_ended_ms, blip = led_blip_ms;
    int r = 0, g = 0, b = 0;
    if (searching) {
        r = g = d;
    } else if (led_cal) {                    // 2 s triangle pulse, dim
        uint32_t ph = t % 2000, tri = ph < 1000 ? ph : 2000 - ph;
        b = 1 + (int)((2 * d - 1) * tri / 1000);
    } else if (led_motion) {                 // B/3 at the threshold .. B at 3x
        int k = led_strength < 10 ? 10 : led_strength > 30 ? 30 : led_strength;
        r = B / 3 + (B - B / 3) * (k - 10) / 20;
    } else if (ended && t - ended < LED_FADE_MS) {
        int a = (B / 3) * (int)(LED_FADE_MS - (t - ended)) / LED_FADE_MS;
        if (a >= 2) { r = a; g = a / 2; }    // amber; below 2 it would turn red
    }
    if (!r && !g && !b && !searching && !led_cal && !led_motion &&
        blip && t - blip < LED_BLIP_MS) {
        r = b = d;
    }
    uint32_t c = (uint32_t)r << 16 | (uint32_t)g << 8 | (uint32_t)b;
    if (c != shown && led_rgb(r, g, b)) shown = c;   // else retry next poll
}

// ---------------- Probe mode (hardware bring-up) ----------------
#if CSI_PROBE
typedef struct {
    char     what;              // 'c' = CSI callback, 'a' = ACK frame seen by sniffer
    int8_t   verdict, rssi;
    uint8_t  sig_mode, rate;
    uint16_t len;
    int16_t  pending;
    uint32_t t_us, dt_us, rx_ts;
    uint8_t  mac[6], dmac[6];
} probe_rec_t;
static QueueHandle_t probe_q;

static void probe_task(void *arg) {
    probe_rec_t r;
    for (;;) {
        if (!xQueueReceive(probe_q, &r, portMAX_DELAY)) continue;
        const uint8_t *m = r.mac, *d = r.dmac;
        printf("PROBE,%c,%" PRIu32 ",%02x:%02x:%02x:%02x:%02x:%02x,"
               "%02x:%02x:%02x:%02x:%02x:%02x,%u,%u,%u,%d,%" PRIu32 ",%d,%d,%" PRIu32 "\n",
               r.what, r.t_us, m[0], m[1], m[2], m[3], m[4], m[5],
               d[0], d[1], d[2], d[3], d[4], d[5], r.sig_mode, r.rate, r.len,
               r.rssi, r.dt_us, r.pending, r.verdict, r.rx_ts);
    }
}
#endif

// ---------------- Wi-Fi callbacks (run in the Wi-Fi task) ----------------
// Beacon sniffer: learns BSSIDs by SSID, tracks real channel, catches CSA
static void promisc_cb(void *buf, wifi_promiscuous_pkt_type_t type) {
    const wifi_promiscuous_pkt_t *pkt = buf;
#if CSI_PROBE
    if (type == WIFI_PKT_CTRL) {
        const uint8_t *f = pkt->payload;
        int flen = (int)pkt->rx_ctrl.sig_len - 4;
        if (f && flen >= 10 && f[0] == 0xD4) {            // any ACK, any receiver
            probe_rec_t r = { .what = 'a', .t_us = now_us(), .rssi = pkt->rx_ctrl.rssi,
                              .sig_mode = pkt->rx_ctrl.sig_mode, .rate = pkt->rx_ctrl.rate,
                              .dt_us = now_us() - pk.sent_us, .pending = pk.node,
                              .rx_ts = pkt->rx_ctrl.timestamp };
            memcpy(r.dmac, f + 4, 6);
            xQueueSend(probe_q, &r, 0);
        }
        return;
    }
#endif
    if (type == WIFI_PKT_DATA) {
        const uint8_t *f = pkt->payload;
        int flen = (int)pkt->rx_ctrl.sig_len - 4;
        int rssi = pkt->rx_ctrl.rssi;
        taskENTER_CRITICAL(&seen_mux);
        seen_note(&seen, &nodes, f, flen, rssi, now_ms());
        taskEXIT_CRITICAL(&seen_mux);
        if (flen >= 24 && (f[1] & 0x03) == 1) {        // sent by a device: if we poke it,
            int dn = node_find(&nodes, f + 10);        // its signal and channel matter
            if (dn >= MAX_APS) {
                gate_rssi(&gate, dn, rssi);
                node_ch[dn] = pkt->rx_ctrl.channel;
            }
        }
        return;
    }
    if (type != WIFI_PKT_MGMT) return;
    beacon_info_t b;
    parse_beacon(pkt->payload, (int)pkt->rx_ctrl.sig_len - 4, my_ssid, &b);
    if (b.ok && net_collect)
        net_note(&nets, b.ssid, b.ssid_len, b.ds_ch ? b.ds_ch : pkt->rx_ctrl.channel,
                 pkt->rx_ctrl.rssi);
    if (!b.ok || !b.ssid_match) return;

    int idx = node_add(&nodes, b.bssid);
    last_seen_ms = now_ms();
    found_ch = b.ds_ch ? b.ds_ch : pkt->rx_ctrl.channel;
    if (idx >= 0) {
        node_ch[idx] = found_ch;
        gate_rssi(&gate, idx, pkt->rx_ctrl.rssi);
    }

    if (!scanning && !csa_ch && csa_should_switch(b.csa_ch, cur_ch, MAX_CH)) {
        csa_at_ms = now_ms() + (uint32_t)b.csa_cnt * 103;   // 1 TU ≈ 1.024 ms
        csa_ch = b.csa_ch;                                  // set last
    }
}

static csi_pkt_t s_pkt;   // CSI callbacks run serially in the Wi-Fi task

static void csi_cb(void *ctx, wifi_csi_info_t *info) {
    if (!info || !info->buf) return;
    uint32_t t = now_us();
#if CSI_PROBE
    int16_t was_pending = (int16_t)pk.node;
#endif
    int node = -1;
    int kind = classify_csi(&nodes, &pk, info->mac, info->dmac, my_mac,
                            info->rx_ctrl.sig_mode, info->len, t, ACK_WIN_US, &node);
#if CSI_PROBE
    probe_rec_t r = { .what = 'c', .t_us = t, .rssi = info->rx_ctrl.rssi,
                      .sig_mode = info->rx_ctrl.sig_mode, .rate = info->rx_ctrl.rate,
                      .len = info->len, .dt_us = t - pk.sent_us, .pending = was_pending,
                      .verdict = (int8_t)kind, .rx_ts = info->rx_ctrl.timestamp };
    memcpy(r.mac, info->mac, 6);
    memcpy(r.dmac, info->dmac, 6);
    xQueueSend(probe_q, &r, 0);
#endif
    if (kind == CSI_DROP) { n_drop++; return; }
    if (kind == CSI_REPLY) { n_reply++; return; }
    if (kind == CSI_KIND_ACK) { n_ack++; gate_ack(&gate, node); } else n_ap++;
    last_seen_ms = now_ms();

    s_pkt.ts = esp_timer_get_time();
    s_pkt.src = (uint8_t)node;
    s_pkt.kind = (uint8_t)kind;
    s_pkt.ch = info->rx_ctrl.channel;
    s_pkt.rssi = info->rx_ctrl.rssi;
    s_pkt.sig_mode = info->rx_ctrl.sig_mode;
    s_pkt.cwb = info->rx_ctrl.cwb;
    s_pkt.len = info->len > CSI_OUT_LEN ? CSI_OUT_LEN : info->len;
    memcpy(s_pkt.buf, info->buf, s_pkt.len);
    if (info->first_word_invalid && s_pkt.len >= 4) memset(s_pkt.buf, 0, 4);
    if (xQueueSend(q, &s_pkt, 0) != pdTRUE) n_qfull++;   // never block the Wi-Fi task
}

// ---------------- Tasks ----------------
#if POKE_HZ > 0
// Polite Wi-Fi: Null frame to the AP at 6 Mbps; its OFDM ACK gives us CSI.
// Two interleaved slots: APs get POKE_HZ/s whatever happens, and devices
// share another POKE_HZ/s (a slot with no device to poke stays silent).
static void poke_task(void *arg) {
    uint8_t frame[NULL_FRAME_LEN];
    int rr[2] = {0, 0}, slot = 0;
    TickType_t wake = xTaskGetTickCount();
    TickType_t period = pdMS_TO_TICKS(500 / POKE_HZ);
    if (period == 0) period = 1;
    for (;;) {
        vTaskDelayUntil(&wake, period);
        int devs = slot;
        slot ^= 1;
        if (scanning) continue;
        int target = next_poke_target(&nodes, node_ch, cur_ch, &rr[devs], &gate, devs);
        if (target < 0) continue;
        uint8_t ta[6];
        poke_ta(my_mac, target, ta);
        build_null_frame(frame, nodes.mac[target], ta);
        pk.node = -1;                        // no ACK matched while we update
        pk.sent_us = now_us();
        pk.node = target;
        if (esp_wifi_80211_tx(WIFI_IF_STA, frame, sizeof frame, true) == ESP_OK) {
            n_poke++;
            gate_sent(&gate, target);
        } else {
            pk.node = -1;
            n_tx_err++;
        }
    }
}
#endif

static int sweep(void) {
    int prev = cur_ch, got = 0;
    scanning = true;
    csa_ch = 0;
    for (int ch = 1; ch <= MAX_CH && !got; ch++) {
        found_ch = 0;
        set_ch(ch);
        vTaskDelay(pdMS_TO_TICKS(DWELL_MS));
        int f = found_ch;
        if (f >= 1 && f <= MAX_CH) got = f;
    }
    set_ch(got ? got : prev);
    scanning = false;
    last_seen_ms = now_ms();
    return got;
}

// True if `mac` was heard on our network (copied out under the lock)
static bool seen_has(const uint8_t *mac) {
    bool found = false;
    taskENTER_CRITICAL(&seen_mux);
    for (int i = 0; i < MAX_SEEN && !found; i++)
        found = seen.d[i].used && memcmp(seen.d[i].mac, mac, 6) == 0;
    taskEXIT_CRITICAL(&seen_mux);
    return found;
}

#define MACF "%02x:%02x:%02x:%02x:%02x:%02x"
#define MACA(m) (m)[0], (m)[1], (m)[2], (m)[3], (m)[4], (m)[5]

static __attribute__((unused)) void print_poking(void) {   // unused when POKE_HZ = 0
    for (int k = 0; k < MAX_DEVS; k++)
        if (node_active(&nodes, DEV_NODE(k)))
            printf("POKING,%d," MACF "\n", DEV_NODE(k), MACA(nodes.mac[DEV_NODE(k)]));
}

// POKE / UNPOKE from the PC. Only devices already heard on our network.
static void run_cmd(const char *line) {
    uint8_t mac[6];
    int cmd = parse_cmd(line, mac);
    char name[33];
    int hz, hz2, vol, beep_ms = parse_beep(line, &hz, &vol);
    int glide_ms = parse_glide(line, &hz, &hz2, &vol);
    if (glide_ms > 0) {
        tune_t t = {{{(uint16_t)hz, (uint16_t)glide_ms, (uint16_t)hz2}}, (uint8_t)vol};
        buzzer_play(&t);
        printf("GLIDE,%d,%d,%d,%d\n", glide_ms, hz, hz2, vol ? vol : BUZZER_VOL);
    } else if (beep_ms > 0) {
        parse_beep(line, &hz, &vol);
        tune_t t = {{{(uint16_t)(hz ? hz : BUZZER_HZ), (uint16_t)beep_ms, 0}}, (uint8_t)vol};
        buzzer_play(&t);
        printf(buzzer_ok ? "BEEP,%d,%d,%d\n" : "BEEP,%d,%d,%d,no-buzzer\n",
               beep_ms, hz ? hz : BUZZER_HZ, vol ? vol : BUZZER_VOL);
    } else if (cmd == CMD_SCAN) {
        scan_request = 1;
    } else if (parse_use(line, name) > 0 || strncmp(line, "USE_DEFAULT", 11) == 0) {
        bool dflt = line[3] == '_';
        if (ssid_save(dflt ? NULL : name)) {
            printf("NETWORK,saved,%s\n", dflt ? MY_SSID : name);
            fflush(stdout);
            buzzer_play(&TUNE_TICK);
            vTaskDelay(pdMS_TO_TICKS(300));
            esp_restart();                 // start clean on the new network
        }
        printf("NETWORK,error,not-saved\n");
    } else if (cmd == CMD_CAL) {
        cal_request = 1;
        buzzer_play(&TUNE_TICK);
        printf("BUTTON,recalibrate,pc\n");
    } else if (cmd == CMD_UNPOKE_ALL) {
        for (int k = 0; k < MAX_DEVS; k++) dev_clear(&nodes, k);
        printf("UNPOKED,all\n");
    } else if (cmd == CMD_UNPOKE) {
        int n = node_find(&nodes, mac);
        if (n >= MAX_APS) dev_clear(&nodes, n - MAX_APS);
        printf("UNPOKED," MACF "\n", MACA(mac));
    } else if (cmd == CMD_POKE) {
        bool is_new;
        int k = dev_slot_for(&nodes, mac, &is_new);
        if (node_is_ap(node_find(&nodes, mac))) {
            printf("REFUSED," MACF ",is-an-ap\n", MACA(mac));
        } else if (!seen_has(mac)) {
            printf("REFUSED," MACF ",not-seen-on-network\n", MACA(mac));
        } else if (k < 0) {
            printf("REFUSED," MACF ",full\n", MACA(mac));
        } else {
            if (is_new) {                  // slot is idle: nothing else writes it now
                gate_reset(&gate, DEV_NODE(k));
                node_ch[DEV_NODE(k)] = cur_ch;
                dev_set(&nodes, k, mac);
            }
            printf("POKING,%d," MACF "\n", DEV_NODE(k), MACA(mac));
        }
    } else {
        printf("REFUSED,bad-command\n");
    }
}

// Commands arrive on the USB port, which this build uses for output only,
// so read its RX FIFO directly (never blocks, works with no PC attached).
static void poll_cmds(void) {
    static char buf[48];
    static int n;
    uint8_t c;
    while (usb_serial_jtag_ll_rxfifo_data_available() &&
           usb_serial_jtag_ll_read_rxfifo(&c, 1) == 1) {
        if (c == '\n' || c == '\r') {
            if (n) { buf[n] = '\0'; run_cmd(buf); }
            n = 0;
        } else if (n < (int)sizeof buf - 1) {
            buf[n++] = (char)c;
        } else {
            n = 0;                         // overlong line: drop it
        }
    }
}

static void print_devices(void) {
    seen_list_t copy;
    taskENTER_CRITICAL(&seen_mux);
    copy = seen;
    taskEXIT_CRITICAL(&seen_mux);
    uint32_t now = now_ms();
    for (int i = 0; i < MAX_SEEN; i++) {
        const seen_dev_t *e = &copy.d[i];
        if (!e->used) continue;
        uint32_t age = now - e->last_ms;
        if ((int32_t)age < 0) age = 0;
        printf("DEV," MACF ",%d,%" PRIu32 ",%" PRIu32 ",%" PRIu32 "\n", MACA(e->mac),
               e->rssi_x16 / 16, e->n_tx, e->n_rx, age);
    }
}

// SCAN: visit every channel, note every network's beacons, list them.
// Pokes pause meanwhile; the channel change makes the detector start over.
static void net_scan(void) {
    int prev = cur_ch;
    scanning = true;
    csa_ch = 0;
    memset(&nets, 0, sizeof nets);
    net_collect = true;
    for (int ch = 1; ch <= MAX_CH; ch++) {
        set_ch(ch);
        vTaskDelay(pdMS_TO_TICKS(2 * DWELL_MS));   // two beacon intervals
    }
    net_collect = false;
    vTaskDelay(pdMS_TO_TICKS(5));                // a callback in flight finishes
    set_ch(prev);
    scanning = false;
    last_seen_ms = now_ms();
    for (int i = 0; i < nets.count; i++) {
        char name[33];
        for (int j = 0; j < 33; j++) {           // one line per network, always
            char c = nets.n[i].ssid[j];
            name[j] = c && (unsigned char)c < 0x20 ? '?' : c;
        }
        printf("NET,%d,%d,%s\n", nets.n[i].ch, nets.n[i].rssi, name);
    }
    printf("NETS,done,%d\n", nets.count);
}

// The only task that changes channels
static void control_task(void *arg) {
    int printed = 0;
    bool need_scan = true;                 // find the AP immediately at boot
    uint32_t next_stats = now_ms() + STATS_MS;
    printf("ME,%02x:%02x:%02x:%02x:%02x:%02x\n", my_mac[0], my_mac[1], my_mac[2],
           my_mac[3], my_mac[4], my_mac[5]);
    print_network();
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(POLL_MS));
        poll_cmds();
        led_update(!my_ssid[0]);           // no network chosen yet: yellow

        for (int n = node_count(&nodes); printed < n; printed++) {
            const uint8_t *m = nodes.mac[printed];
            printf("LEARNED,%d,%02x:%02x:%02x:%02x:%02x:%02x\n", printed,
                   m[0], m[1], m[2], m[3], m[4], m[5]);
        }

#if CAL_BUTTON_GPIO >= 0
        static int held;                   // debounce: 2 polls = 100 ms
        if (btn_ok && gpio_get_level(CAL_BUTTON_GPIO) == 0) {
            if (++held == 2) {
                cal_request = 1;
                buzzer_play(&TUNE_TICK);
                printf("BUTTON,recalibrate\n");
            }
        } else {
            held = 0;
        }
#endif

        uint32_t now = now_ms();
        if (deadline_passed(now, next_stats)) {
            next_stats = now + STATS_MS;
            // Re-sent so a capture started after boot still knows who's who
            printf("ME,%02x:%02x:%02x:%02x:%02x:%02x\n", my_mac[0], my_mac[1], my_mac[2],
                   my_mac[3], my_mac[4], my_mac[5]);
            for (int i = 0; i < printed; i++) {
                const uint8_t *m = nodes.mac[i];
                printf("LEARNED,%d,%02x:%02x:%02x:%02x:%02x:%02x\n", i,
                       m[0], m[1], m[2], m[3], m[4], m[5]);
            }
#if POKE_HZ > 0
            int best_ap = ap_best_rssi_x16(&nodes, node_ch, cur_ch, &gate);
            for (int i = 0; i < MAX_NODES; i++)
                if (node_active(&nodes, i)) {
                    int st = gate_state(&gate, i);
                    printf("GATE,%d,%d,%d,%d,%s\n", i, gate.rssi_x16[i] / 16, gate.yield[i],
                           gate.sent[i], st == GATE_OK && ap_is_far(&gate, i, best_ap)
                           ? "far" : gate_name(st));
                }
            print_poking();
#endif
            print_devices();
            print_network();
            printf("STATS,%" PRId64 ",%" PRIu32 ",%" PRIu32 ",%" PRIu32 ",%" PRIu32
                   ",%" PRIu32 ",%" PRIu32 ",%" PRIu32 "\n", esp_timer_get_time(), n_poke,
                   n_tx_err, n_ack, n_ap, n_drop, n_qfull, n_reply);
        }

        int pending = csa_ch;
        if (pending && deadline_passed(now, csa_at_ms)) {        // announced hop
            csa_ch = 0;
            set_ch(pending);
            last_seen_ms = now_ms();
            printf("CHANNEL,%" PRId64 ",%d,csa\n", esp_timer_get_time(), pending);
            continue;
        }

        if (scan_request) {
            scan_request = 0;
            net_scan();
            continue;
        }
        if (!my_ssid[0]) continue;         // nothing to look for until USE,<name>

        now = now_ms();                    // the prints above can block
        if (need_scan || older_than(now, last_seen_ms, LOST_MS)) {  // silent hop
            need_scan = false;
            led_update(true);
            int ch = sweep();
            printf("CHANNEL,%" PRId64 ",%d,%s\n", esp_timer_get_time(),
                   ch, ch ? "scan" : "lost");
            static bool lost;                  // sound once per loss, not every sweep
            if (!ch && !lost) buzzer_play(&TUNE_LOST);
            if (ch && lost) buzzer_play(&TUNE_FOUND);
            lost = !ch;
        }
    }
}

static void print_calib(const motion_det_t *d, int64_t ts) {
    float f = d->floor;
    printf("CALIB,%" PRId64 ",%s,%s,%d,%d,%d\n", ts, md_phase_name(d->phase),
           md_why_name(d->why), (int)(f * 1e6f), (int)(d->c.on_short * f * 1e6f),
           (int)(d->c.on_long * f * 1e6f));
}

// Prints every packet and runs the on-device motion detector
static void print_task(void *arg) {
    static char line[1024];
    static motion_det_t det;                 // ~24 KB: keep it off the stack
    md_cfg_t cfg;
    md_cfg_default(&cfg);
#ifdef MD_ON_SHORT
    cfg.on_short = MD_ON_SHORT;
    cfg.off_short = MD_ON_SHORT * (2.0f / 2.8f);
#endif
#ifdef MD_ON_LONG
    cfg.on_long = MD_ON_LONG;
    cfg.off_long = MD_ON_LONG * (1.2f / 1.4f);
#endif
    md_init(&det, &cfg, esp_timer_get_time());
    uint32_t epoch = ch_epoch;
    int64_t next_calib = 0;
    int64_t beep_at = -1;                    // when to beep for the current motion, -1 = no
    bool beeped = false;                     // this motion got its beep: sound its end too
    int64_t last_beep = INT64_MIN / 2;       // when the last motion beep played
    int64_t next_whoop = -1;                 // alarm mode: next whoop, -1 = alarm off
    // Link watch: a weak link is noisy and looks like motion, so no beeps then
    int64_t link_from = 0;
    int link_acks = 0, link_rssi_sum = 0;
    bool link_weak = false;
    csi_pkt_t p;
    for (;;) {
        if (!xQueueReceive(q, &p, portMAX_DELAY)) continue;
        if (epoch != ch_epoch) {                 // channel changed: new baselines
            epoch = ch_epoch;
            md_reset(&det, esp_timer_get_time());
        }
        if (cal_request) {
            cal_request = 0;
            md_recalibrate(&det, p.ts, (int64_t)CAL_DELAY_S * 1000000, MD_WHY_BUTTON);
        }
        int n = format_csi_line(line, sizeof line, &p);
        if (n > 0) fwrite(line, 1, n, stdout);

        if (p.kind == 1 && p.src < MAX_APS) { link_acks++; link_rssi_sum += p.rssi; }
        if (p.ts - link_from >= 5000000) {   // every 5 s
            int rate = (int)((int64_t)link_acks * 1000000 / (p.ts - link_from));
            int rssi = link_acks ? link_rssi_sum / link_acks : -127;
            bool weak = link_from > 0 && (rate < LINK_MIN_ACKS || rssi < LINK_MIN_RSSI);
            if (weak != link_weak)
                printf("LINK,%" PRId64 ",%s,%d,%d\n", p.ts, weak ? "weak" : "ok", rate, rssi);
            link_weak = weak;
            link_from = p.ts;
            link_acks = link_rssi_sum = 0;
        }
        md_update(&det, p.src, p.kind, p.ts, p.rssi, p.buf, p.len);
        int change = md_eval(&det, p.ts);
        {                                    // LED inputs: strength, faint blips
            float ms, ml, f = det.floor;
            md_max_scores(&det, p.ts, &ms, &ml);
            if (f > 0) {
                float k = ms / (det.c.on_short * f), kl = ml / (det.c.on_long * f);
                k = (kl > k ? kl : k) * 10.0f;
                led_strength = k > 255 ? 255 : k < 0 ? 0 : (uint8_t)k;
                if (!det.motion && ms > det.c.on_short * f) led_blip_ms = clock_ms() | 1;
            }
        }
        bool cal = det.phase <= MD_CAL_COLLECT;  // no quiet level yet
        if (led_cal && !cal) buzzer_play(&TUNE_READY);   // calibrated: watching now
        led_cal = cal;
        led_motion = det.motion;
        if (md_take_cal_event(&det) || p.ts >= next_calib) {
            next_calib = p.ts + CALIB_EVERY_US;
            print_calib(&det, p.ts);
        }
        if (change) {
            float ms, ml;
            md_max_scores(&det, p.ts, &ms, &ml);
            printf("STATE,%" PRId64 ",%s,%d,%d\n", p.ts, change > 0 ? "motion" : "still",
                   (int)(ms * 1000), (int)(ml * 1000));
            beep_at = change > 0 ? p.ts + (int64_t)BEEP_AFTER_MS * 1000 : -1;
            if (change < 0 && beeped) buzzer_play(&TUNE_STILL);
            if (change < 0) led_ended_ms = clock_ms() | 1;
            beeped = false;
        }
        if (beep_at >= 0 && p.ts >= beep_at) {  // lasted long enough: not a brief blip
            beep_at = -1;
            if (MOTION_ALARM && det.motion && !link_weak) {
                next_whoop = p.ts;           // sounds until motion ends
                beeped = true;
            } else if (det.motion && !link_weak && p.ts - last_beep >= (int64_t)BEEP_GAP_MS * 1000) {
                buzzer_play(&TUNE_MOTION);
                beeped = true;
                last_beep = p.ts;
            }
        }
        if (next_whoop >= 0) {
            if (!det.motion || link_weak) {
                next_whoop = -1;             // motion over (or link went bad): stop
                if (link_weak) { tune_t quiet = {{{0, 1, 0}}, 0}; buzzer_play(&quiet); }
            } else if (p.ts >= next_whoop) {
                buzzer_play(&TUNE_WHOOP);
                next_whoop = p.ts + WHOOP_EVERY_US;
            }
        }
    }
}

void app_main(void) {
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);
    ssid_load();

    gate_init(&gate);
    buzzer_init();                           // never fatal
    buzzer_play(&TUNE_HELLO);                // power-on chirp: shows the wiring works
    led_init();                              // never fatal
    led_hello();
#if CAL_BUTTON_GPIO >= 0
    gpio_config_t btn = { .pin_bit_mask = 1ULL << CAL_BUTTON_GPIO, .mode = GPIO_MODE_INPUT,
                          .pull_up_en = GPIO_PULLUP_ENABLE };
    btn_ok = gpio_config(&btn) == ESP_OK;
    printf(btn_ok ? "BUTTON,gpio%d\n" : "BUTTON,error,gpio%d\n", CAL_BUTTON_GPIO);
#endif

    // Queues + printers must exist before CSI is enabled
    q = xQueueCreate(32, sizeof(csi_pkt_t));
    configASSERT(q);
    xTaskCreate(print_task, "csi_print", 4096, NULL, 5, NULL);
#if CSI_PROBE
    probe_q = xQueueCreate(64, sizeof(probe_rec_t));
    configASSERT(probe_q);
    xTaskCreate(probe_task, "csi_probe", 3072, NULL, 4, NULL);
#endif

    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));
    // Station mode (never connects): needed to transmit the poke frames
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
    ESP_ERROR_CHECK(esp_wifi_get_mac(WIFI_IF_STA, my_mac));
    // 6 Mbps OFDM, so the AP's ACK is OFDM too (1 Mbps 11b ACKs have no CSI)
    ESP_ERROR_CHECK(esp_wifi_config_80211_tx_rate(WIFI_IF_STA, WIFI_PHY_RATE_6M));

    wifi_promiscuous_filter_t filt = {
        .filter_mask = WIFI_PROMIS_FILTER_MASK_MGMT | WIFI_PROMIS_FILTER_MASK_DATA |
                       WIFI_PROMIS_FILTER_MASK_CTRL
    };
    wifi_promiscuous_filter_t ctrl = { .filter_mask = WIFI_PROMIS_CTRL_FILTER_MASK_ACK };
    ESP_ERROR_CHECK(esp_wifi_set_promiscuous_filter(&filt));
    ESP_ERROR_CHECK(esp_wifi_set_promiscuous_ctrl_filter(&ctrl));
    ESP_ERROR_CHECK(esp_wifi_set_promiscuous_rx_cb(promisc_cb));
    ESP_ERROR_CHECK(esp_wifi_set_promiscuous(true));
    set_ch(1);

    wifi_csi_config_t csi = {
        .lltf_en = true, .htltf_en = true, .stbc_htltf2_en = true,
        .ltf_merge_en = true, .channel_filter_en = false,
        .manu_scale = false, .shift = 0,
    };
    ESP_ERROR_CHECK(esp_wifi_set_csi_config(&csi));
    ESP_ERROR_CHECK(esp_wifi_set_csi_rx_cb(csi_cb, NULL));
    ESP_ERROR_CHECK(esp_wifi_set_csi(true));

    xTaskCreatePinnedToCore(control_task, "ch_ctrl", 4096, NULL, 6, NULL, WIFI_CORE);
#if POKE_HZ > 0
    xTaskCreatePinnedToCore(poke_task, "poke", 3072, NULL, 5, NULL, WIFI_CORE);
#endif
}
