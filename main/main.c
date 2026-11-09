#include <stdio.h>
#include <string.h>
#include <inttypes.h>
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
#include "hal/usb_serial_jtag_ll.h"
#include "csi_core.h"

// Your network name lives in main/my_network.h (git-ignored, so it is never
// committed). Create it from main/my_network.h.example, or let `./csi flash`
// ask for it. The board never joins the network: no password is needed.
#if __has_include("my_network.h")
#include "my_network.h"
#endif
#ifndef MY_SSID
#error "Network name not set: copy main/my_network.h.example to main/my_network.h (or run ./csi flash)"
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
// Detector thresholds, multiples of the measured quiet level. Lower = more
// sensitive. Defaults (md_cfg_default in csi_core.h): short 2.8/2.0, long 1.4/1.2
// #define MD_ON_SHORT 2.8f
// #define MD_ON_LONG  1.5f
// --------------------------------------------

#if CAL_BUTTON_GPIO >= 0 && CAL_BUTTON_GPIO == BUZZER_GPIO
#error "CAL_BUTTON_GPIO and BUZZER_GPIO must differ"
#endif

#define LOST_MS        2000      // rescan after 2 s without beacons/CSI
#define DWELL_MS       150       // > one beacon interval (~102 ms)
#define POLL_MS        50
#define ACK_WIN_US     10000     // sanity bound only: ACKs are matched by dmac
#define CSI_OUT_LEN    128       // bytes sent per packet: LLTF only (all we use)
#define STATS_MS       5000
#define CALIB_EVERY_US 5000000   // CALIB line period (also sent on every change)
#define BUZZER_HZ      2700
#define BEEP_MS        150
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

static volatile uint32_t n_poke, n_tx_err, n_ack, n_ap, n_drop, n_qfull;

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
static bool buzzer_ok;
static esp_timer_handle_t buzz_off_timer;

static void buzzer_set(bool on) {
#if BUZZER_ACTIVE
    gpio_set_level(BUZZER_GPIO, on);
#else
    ledc_set_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_0, on ? 512 : 0);   // 50 % of 10 bit
    ledc_update_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_0);
#endif
}

static void buzz_off_cb(void *arg) { buzzer_set(false); }

static void buzzer_init(void) {
    buzzer_ok = false;
    if (BUZZER_GPIO < 0) { printf("BUZZER,off\n"); return; }
    esp_err_t err;
#if BUZZER_ACTIVE
    gpio_config_t io = { .pin_bit_mask = 1ULL << BUZZER_GPIO, .mode = GPIO_MODE_OUTPUT };
    err = gpio_config(&io);
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
#endif
    if (err == ESP_OK) {
        esp_timer_create_args_t a = { .callback = buzz_off_cb, .name = "buzz_off" };
        err = esp_timer_create(&a, &buzz_off_timer);
    }
    if (err != ESP_OK) {
        printf("BUZZER,error,%s\n", esp_err_to_name(err));   // keep going without it
        return;
    }
    buzzer_ok = true;
    buzzer_set(false);
    printf("BUZZER,gpio%d\n", BUZZER_GPIO);
}

static void buzzer_beep(int ms) {
    if (!buzzer_ok) return;
    esp_timer_stop(buzz_off_timer);          // fine if it wasn't running
    buzzer_set(true);
    if (esp_timer_start_once(buzz_off_timer, (uint64_t)ms * 1000) != ESP_OK)
        buzzer_set(false);
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
    parse_beacon(pkt->payload, (int)pkt->rx_ctrl.sig_len - 4, MY_SSID, &b);
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
static void poke_task(void *arg) {
    uint8_t frame[NULL_FRAME_LEN];
    int rr = 0;
    TickType_t wake = xTaskGetTickCount();
    TickType_t period = pdMS_TO_TICKS(1000 / POKE_HZ);
    if (period == 0) period = 1;
    for (;;) {
        vTaskDelayUntil(&wake, period);
        if (scanning) continue;
        int target = next_poke_target(&nodes, node_ch, cur_ch, &rr, &gate);
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
    if (cmd == CMD_CAL) {
        cal_request = 1;
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

// The only task that changes channels
static void control_task(void *arg) {
    int printed = 0;
    bool need_scan = true;                 // find the AP immediately at boot
    uint32_t next_stats = now_ms() + STATS_MS;
    printf("ME,%02x:%02x:%02x:%02x:%02x:%02x\n", my_mac[0], my_mac[1], my_mac[2],
           my_mac[3], my_mac[4], my_mac[5]);
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(POLL_MS));
        poll_cmds();

        for (int n = node_count(&nodes); printed < n; printed++) {
            const uint8_t *m = nodes.mac[printed];
            printf("LEARNED,%d,%02x:%02x:%02x:%02x:%02x:%02x\n", printed,
                   m[0], m[1], m[2], m[3], m[4], m[5]);
        }

#if CAL_BUTTON_GPIO >= 0
        static int held;                   // debounce: 2 polls = 100 ms
        if (btn_ok && gpio_get_level(CAL_BUTTON_GPIO) == 0) {
            if (++held == 2) { cal_request = 1; printf("BUTTON,recalibrate\n"); }
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
            for (int i = 0; i < MAX_NODES; i++)
                if (node_active(&nodes, i))
                    printf("GATE,%d,%d,%d,%d,%s\n", i, gate.rssi_x16[i] / 16, gate.yield[i],
                           gate.sent[i], gate_name(gate_state(&gate, i)));
            print_poking();
#endif
            print_devices();
            printf("STATS,%" PRId64 ",%" PRIu32 ",%" PRIu32 ",%" PRIu32 ",%" PRIu32
                   ",%" PRIu32 ",%" PRIu32 "\n", esp_timer_get_time(), n_poke, n_tx_err,
                   n_ack, n_ap, n_drop, n_qfull);
        }

        int pending = csa_ch;
        if (pending && deadline_passed(now, csa_at_ms)) {        // announced hop
            csa_ch = 0;
            set_ch(pending);
            last_seen_ms = now_ms();
            printf("CHANNEL,%" PRId64 ",%d,csa\n", esp_timer_get_time(), pending);
            continue;
        }

        now = now_ms();                    // the prints above can block
        if (need_scan || older_than(now, last_seen_ms, LOST_MS)) {  // silent hop
            need_scan = false;
            int ch = sweep();
            printf("CHANNEL,%" PRId64 ",%d,%s\n", esp_timer_get_time(),
                   ch, ch ? "scan" : "lost");
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

        md_update(&det, p.src, p.kind, p.ts, p.rssi, p.buf, p.len);
        int change = md_eval(&det, p.ts);
        if (md_take_cal_event(&det) || p.ts >= next_calib) {
            next_calib = p.ts + CALIB_EVERY_US;
            print_calib(&det, p.ts);
        }
        if (change) {
            float ms, ml;
            md_max_scores(&det, p.ts, &ms, &ml);
            printf("STATE,%" PRId64 ",%s,%d,%d\n", p.ts, change > 0 ? "motion" : "still",
                   (int)(ms * 1000), (int)(ml * 1000));
            if (change > 0) buzzer_beep(BEEP_MS);
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

    gate_init(&gate);
    buzzer_init();                           // never fatal
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
