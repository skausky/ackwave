# ackwave: ESP32 Wi-Fi CSI Motion Sensing

![tests](https://github.com/YOUR_USERNAME/ackwave/actions/workflows/ci.yml/badge.svg)

Detects human motion in a room using **Wi-Fi Channel State Information (CSI)**, with no camera and no wearable. An ESP32-S3 measures how the radio channel between itself and your existing router changes over time. A person moving changes the multipath reflections, so the per-subcarrier amplitudes start to fluctuate. The detector runs **on the board itself**, so the sensor works unplugged from a PC (with an optional piezo buzzer), and a live GUI shows what is happening when it is plugged in.

![Live GUI on synthetic data: calibrating, motion, channel hop with recalibration, motion again](docs/gui.png)

## Quick start

Everything runs from one script in the project folder. Plug the board in, then:

```bash
cd ackwave
./csi              # menu: pick what you want
```

| Command | What it does |
|---|---|
| `./csi gui` | **Live view.** It also logs everything in the background, just like `record`. Close the window to stop; the summary is printed and saved. |
| `./csi record` | **Log motion with clock times, no window** (e.g. overnight or while you're out). The PC stays awake; **Ctrl+C** stops it and prints a summary. |
| `./csi history` | **Look back** at the last session: a zoomable timeline and a list of motion periods. `./csi history --list` shows all sessions; `./csi history 11-08` opens the one from 8 Nov. |
| `./csi demo` | The GUI on fake data, no board needed |
| `./csi flash` | Build and flash the firmware (uses ESP-IDF v5.3.2 at `~/esp/esp-idf-v5.3.2`) |
| `./csi test` | Run all tests |
| `./csi shortcut` | Add **CSI Motion** to the desktop app menu (opens the live view) |

Only one program can read the board at a time: close the GUI before recording, and stop a recording before opening the GUI. The script says so if the board is busy or unplugged.

Sessions from both `gui` and `record` are saved to `sessions/` in the project folder: `events-<start>.log` is readable text with clock times, and `raw-<start>.log.gz` holds all the data (about 55 MB per hour).

**In the GUI:** the badge shows `CALIBRATING`, `STILL` or `MOTION`.
- **Top plot:** both scores as multiples of the room's quiet level, with the thresholds as lines. The **30 s / 2 min / 10 min** buttons (or keys 1/2/3) choose how much it shows.
- **Session timeline:** everything since the GUI opened, on a clock. It shows the peak score each second, motion periods in red, brief blips as orange ticks and calibration in grey.
- **Pause** (or the space bar) freezes the view so the toolbar's zoom and pan tools work. Logging carries on, and **Live** goes back. Calibration takes about 10 s after power-up; keep still for it, or press the board's **BOOT** button to recalibrate (it waits 5 s so you can step away).

**First-time setup** (already done on this PC): `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`.

## How it gets a steady CSI stream: "Polite Wi-Fi"

Routers send beacons only about 10 times a second, and on 2.4 GHz they often send them at 1 Mbps (802.11b). That format has no OFDM training field, so it produces no CSI. To get a dense, steady stream without joining your network, the board uses a quirk found by [researchers at the University of Waterloo](https://uwaterloo.ca/computer-science/news/is-wifi-too-polite): **every Wi-Fi device ACKs a unicast frame addressed to it before checking who sent it.**

The board sends the router a 24-byte Null data frame (default 100/s) at **6 Mbps OFDM**. The router answers with an OFDM ACK, and that ACK carries a full 64-subcarrier channel estimate. No password or association is needed, and the airtime cost is tiny.

The Null frame's sender address is **made up**, not the board's own MAC. The ESP32's radio consumes ACKs addressed to itself and never reports CSI for them, but an ACK to an address it doesn't own is reported like any passing frame. Each AP gets its own made-up address (the board's MAC with the locally-administered bit flipped, plus the AP's index in the last byte), so the ACK's receiver address (`dmac`) says exactly that it answers our poke and which AP sent it. Frames from other devices can't be mistaken for ACKs. On a real ESP32-S3 this gives about 97 ACK CSI per second from 100 pokes. A **probe mode** logs every callback so this can be verified on your hardware (see [Bring-up](#bring-up-probe-mode)).

Pokes are split evenly over the APs being poked, so a far AP that rarely answers costs the router ACKs: learning a second AP at -82 dBm once dropped the router from 97 to 43 ACK/s. So an AP leaves the rotation when its beacons average below -80 dBm (it rejoins above -77) or when it answers under 20 % of a 50-poke window. A silent AP still gets 1 poke in 50 so it can come back, and if every AP on the channel is out, the strongest is poked anyway, so a single far router works as before. Each AP's state is printed in the `GATE` line every 5 s.

> Only point this at your own router. The firmware pokes only access points whose SSID matches `MY_SSID` (set in `main/my_network.h`).

## Architecture

```
 router ──beacons──────────────► ESP32-S3 (station mode, never associates)
        ◄──Null frame (6 Mbps)── poke_task   100/s, round-robin over good APs on this channel
        ──ACK (OFDM)───────────►
                                 promisc_cb  beacons: learn BSSIDs by SSID, follow CSA hops
                                 csi_cb      classify: AP frame / ACK / drop → queue
                                 control     the only task that changes channel
                                 print_task  serial line + on-board motion detector → buzzer
                                     │
                         USB serial (921600)
                                     ▼
                tools/gui.py      live plots     tools/analyze.py   text output
```

### Motion detector (`csi_core.h`, mirrored in `analyze.py`)

Per stream (node × beacon/ACK), for every packet:

1. Take the LLTF amplitudes and drop the DC and guard subcarriers, leaving **52 values**, then divide by their mean, which **cancels AGC gain jumps**.
2. **Band-pass** each subcarrier to about 0.3–5 Hz, the range of human movement. This drops the static multipath and most of the measurement noise: on a real board, about 60 % of the raw variation in a still room is noise above 10 Hz.
3. Track the energy of that signal over a **short window (0.5 s)** and a **long window (4 s)**.
4. Score = root of the mean energy of the **8 most active subcarriers**, so a movement that only touches a few subcarriers isn't averaged away.

The **short window** reacts within a fraction of a second to clear movement. The **long window** catches weak but sustained movement, which noise can't imitate for 4 s. Motion is reported if either window crosses its threshold, with hysteresis. What one packet can add to the long window is capped, so a big burst can't hold it on for long. Everything is time-based, so a 10 Hz beacon stream and a 100 Hz ACK stream score alike.

**Router glitches are ignored.** Some routers briefly switch to a very different channel shape and then snap back. One ISP-supplied gateway did this every 20 min 2 s, around the clock, for 0.1–1 s each time. Each packet's normalised shape is compared to a 1 s average of the recent ones. A packet that differs by more than 0.4 is skipped: the router glitches measured 0.53–0.62, while people moving, even right next to the board, stayed at 0.39 or below. A new shape that lasts 5 s is accepted as real, for example when the board is moved.

### Auto-calibration

Thresholds are **multiples of the room's quiet level** (short ×2.8, long ×1.4), and the board measures that level itself:

- **The quiet level** is the 10th percentile of the short-window score over the last 2 minutes, sampled 10 times a second. The short score drops back to the quiet level in every brief pause, so this works **even while someone is moving around**.
- **At boot** it measures for about 10 s (`CALIBRATING`, no verdict), then follows the measurement directly while the 2-minute history fills.
- **Afterwards it keeps tracking**, motion or not. It can fall immediately but rise at most 1 % per second, so constant activity isn't quickly learned as background, and a board moved somewhere noisier can't get stuck in "motion".
- **It recalibrates by itself** after a channel change, or when the signal strength steps by more than 4 dB and stays there. Moving the board does that; a person walking past only causes a brief dip.
- **The BOOT button** (`CAL_BUTTON_GPIO`, GPIO 0) recalibrates on demand, starting 5 s after the press so you can step away.

On a real ESP32-S3 the still room peaked at 2.0× (short) and 1.1× (long) the quiet level. A person moving scored 5–7× (short), and lighter movement 1.6–1.9× (long). In a quiet hour the long score stayed under 1.1× 99 % of the time, while small movements of a person lying down, confirmed by a mmWave sensor next to the board, reached 1.2–1.5×. That is why the long threshold is 1.4×.

The C and Python versions are tested against each other on synthetic and real captures: scores agree to within 10⁻⁶, and verdicts and calibration events match exactly.

## Testing without hardware

All protocol and decision logic lives in [`main/csi_core.h`](main/csi_core.h), which has no ESP-IDF dependencies, so it runs on a PC.

| Suite | What it covers |
|---|---|
| `test/test_core.c` | 175 checks: beacon/IE parsing (truncated, reordered, hidden SSID), CSA validation, Null-frame builder, made-up poke address, ACK classifier (dmac match, foreign frames, window, one-ACK-per-poke, 32-bit wrap), wrap-safe timeouts, poke round-robin and gating (RSSI hysteresis, yield windows, trial pokes, strongest fallback), detector (band-pass scores, top-K, hysteresis of both windows, long-window cap, quiet-level percentile, tracking limits and clamps, recalibration delay, RSSI-step detection), serial format, plus a 200,000-frame fuzzer, all under AddressSanitizer + UBSan |
| `tools/test_pipeline.py` | End to end on synthetic multipath CSI (beacons at 10 Hz + ACKs at 40 Hz, plus malformed lines) in five scenarios: calibration → motion → channel hop → motion; **motion during calibration**; **board moved** (RSSI step → one recalibration); **board moved somewhere noisier** without an RSSI change (must not get stuck in motion, and the quiet level must converge to the new spot's); and **two real captures** from an ESP32-S3 (`test/data`: a still room, which must give no motion, and a person moving). Checks detection within 1 s, release within 10 s, no false motion, and **C vs Python agreement** on every run |
| `tools/test_probe_report.py` | The bring-up report on a synthetic probe log |
| `tools/test_gui.py` | GUI state tracking, reboot handling and rendering (headless) |
| `tools/test_record.py` | The session recorder's event log and summary, with a fake clock |

```bash
pip install -r requirements.txt
make test        # all of the above
make demo        # text analyzer on fake data
make gui-demo    # live GUI on fake data, paced in real time
make gui         # live GUI on the board
make record      # log motion with clock times until Ctrl+C
```

The firmware builds with **ESP-IDF v5.3.2 for ESP32-S3 with zero warnings**. CI builds three configurations (default, probe + passive buzzer, beacons-only + active buzzer).

## Running on hardware

Requires ESP-IDF v5.x and an ESP32-S3.

1. Set your network name: `cp main/my_network.h.example main/my_network.h` and edit it (or just run `./csi flash`, which asks). That file is git-ignored, so your network name is never committed. No password is needed: the board never joins the network.

   Other settings are at the top of `main/main.c`:

   | Setting | Default | |
   |---|---|---|
   | `MAX_CH` | 13 | use 11 in the US |
   | `POKE_HZ` | 100 | ACK pokes per second; 0 = beacons only |
   | `CSI_PROBE` | 0 | 1 = log every callback (bring-up) |
   | `BUZZER_GPIO` | -1 | piezo pin; -1 = no buzzer |
   | `BUZZER_ACTIVE` | 0 | 0 = passive piezo (PWM tone), 1 = active buzzer |
   | `CAL_BUTTON_GPIO` | 0 | button that recalibrates (BOOT on most boards); -1 = none |
   | `CAL_DELAY_S` | 5 | after a press: time to step away before measuring |
   | `MD_ON_SHORT` / `MD_ON_LONG` | 2.8 / 1.4 | thresholds as multiples of the quiet level; lower = more sensitive |

   Any of these can also be set at build time without editing, for example `CSI_DEFS="CSI_PROBE=1" idf.py build`.
2. Build and flash: `idf.py set-target esp32s3 build flash`
3. Watch it live (close `idf.py monitor` first, since only one program can hold the port):
   ```bash
   python tools/gui.py /dev/ttyUSB0        # or COM5 on Windows
   ```
4. After boot, keep the room still for about 15 s while it shows `CALIBRATING`. It's fine if someone moves now and then; it settles within a minute or two either way. Press **BOOT** to recalibrate at any time, for example after rearranging the room. If you get false alarms, raise `MD_ON_SHORT`/`MD_ON_LONG`; to catch smaller movements, lower them. The GUI shows the scores as multiples of the quiet level, so you can see the margins directly.

**Placement matters more than any setting.** The board is most sensitive along the line between it and the router, and close to either device. Place it so the area you want to watch lies between the two.

### Recording (any length, e.g. overnight)

```bash
make record          # or: python tools/record.py [port] [--out DIR] [--no-raw]
```

It finds the board, keeps the PC from sleeping while it runs, and reconnects if the board is unplugged. It writes a readable log with clock times to `sessions/events-<start>.log` in the project folder (`03:12:45  MOTION started (short x4.2, long x1.9)` … `still again after 17 s`), and the full data to `raw-<start>.log.gz` (about 55 MB per hour) for later analysis. Press **Ctrl+C** to stop: it prints a summary of every motion period. Blips shorter than 2 s are listed separately; in testing they came about 5 times an hour and mostly matched no other sensor, so treat them as noise unless they cluster. `make gui` opens the live GUI on the board; close it first, since only one program can use the port.

### Running unplugged

The detector runs on the board, so it works from any USB charger or power bank. With a buzzer it beeps when motion starts. Without one it still runs; serial output just goes nowhere.

### Buzzer (optional)

Wire a **passive piezo** between a free GPIO (GPIO 4 is a good choice; avoid strapping pins 0, 3, 45, 46 and USB pins 19, 20) and GND, then set `BUZZER_GPIO 4`. For an **active** buzzer (one that beeps on plain DC), also set `BUZZER_ACTIVE 1`.

A piezo can't be detected electrically, so the buzzer is opt-in. If the pin can't be set up, the board prints `BUZZER,error,...` and carries on sensing.

### Bring-up: probe mode

The ACK path depends on chip and driver behaviour that can only be confirmed on real hardware. Build with `CSI_PROBE=1`, capture a minute of output, and run the report:

```bash
python tools/probe_report.py probe.log
```

It cross-references ACKs seen by the sniffer with CSI callbacks by radio timestamp. It reports whether ACKs produce CSI, what MAC/dmac the driver reports for them, the delay distribution with a suggested `ACK_WIN_US`, and how accurate the classifier is. It also counts ACKs sent to other devices, which shows whether control frames reach the sniffer at all.

## Serial protocol

```
ME,<board mac>
LEARNED,<idx>,<bssid>
CHANNEL,<ts_us>,<ch>,<csa|scan|lost>
CSI,<ts_us>,<node>,<kind 0=AP 1=ACK>,<ch>,<rssi>,<sig_mode>,<cwb>,<len>,<b0 b1 ...>   int8 [imag, real] pairs, LLTF
STATE,<ts_us>,<motion|still>,<short×1000>,<long×1000>     on-board verdict changes
CALIB,<ts_us>,<wait|collect|fast|track>,<boot|button|rssi|channel>,<quiet×1e6>,<short_thr×1e6>,<long_thr×1e6>
                                                 on every calibration change and every 5 s
STATS,<ts_us>,<pokes>,<tx_err>,<ack_csi>,<ap_csi>,<dropped>,<queue_full>   every 5 s
                                                 (ME, LEARNED and GATE are repeated just before it)
GATE,<idx>,<beacon_rssi>,<yield_pct|-1>,<pokes>,<ok|weak|silent>   per AP, every 5 s (POKE_HZ > 0)
BUZZER,<off|gpioN|error,...>
BUTTON,<gpioN|error,gpioN|recalibrate>
PROBE,...                                        only with CSI_PROBE=1
```

## Project layout

```
├── main/
│   ├── csi_core.h          pure logic: parsing, ACK classifier, motion detector
│   └── main.c              ESP-IDF firmware
├── test/
│   ├── test_core.c         host-side C tests + fuzzer
│   ├── score_lines.c       runs the C detector on a log (C/Python cross-check)
│   └── data/               real captures (still room, person moving) used by the tests
├── tools/
│   ├── gui.py              live GUI
│   ├── analyze.py          text analyzer + Python detector
│   ├── record.py           session recorder: readable motion log + summary
│   ├── probe_report.py     hardware bring-up report
│   ├── fake_csi.py         synthetic data generator
│   └── test_*.py           Python tests
└── docs/gui.png
```

## Limitations

- Detects motion *presence* only, with no localisation or activity classification.
- The quiet level is measured automatically, but the threshold multipliers come from one room and position. If you get false alarms or miss movement, adjust `MD_ON_SHORT`/`MD_ON_LONG`.
- Right after a (re)calibration during constant movement, sensitivity is reduced for up to about 30 s. Moved somewhere noisier without a change in signal strength, the board may report motion for up to about 1.5 minutes while the quiet level catches up (BOOT skips that).
- Anything that moves continuously for minutes without pauses (a fan, curtains) slowly becomes part of the background. You are still detected on top of it.
- Breathing-scale movement is at the edge of what this can see; heartbeat is out of reach with ESP32 CSI.
- The ACK path is confirmed on one ESP32-S3 and router (100 % of sniffed ACKs produced CSI, 100 % classifier precision). Other chips or routers may differ, so run probe mode once on new hardware.
- Some routers answer unassociated stations with deauthentication frames. These are harmless here, but they add a little traffic.

## License

MIT, see [LICENSE](LICENSE).
