# Control panel redesign and next steps (plan, not built yet)

The dashboard (`tools/web.py`, `tools/web/`) is thorough but heavy: eight
tabs, every chart at once, and a lot of data to the browser. This is the plan
for a simpler control panel with an advanced mode, plus guarding features,
easy downloads and lighter storage.

Updated 2026-11-19 after studying ESPectre (another ESP32 Wi-Fi sensing
project) and testing our detector on its labelled recordings: see
[What ESPectre taught us](#what-espectre-taught-us), then the new parts on
[detection](#better-motion-detection), [activities](#activities-v2),
[routers and devices](#say-which-router-each-device-is-on),
[satellites](#satellites-and-a-pi-hub), and the updated
[build order](#build-order). Those parts do change the firmware.

The mockups are in [`mockups/`](mockups/) (inline-styled HTML of the design
canvas: read them for layout, colours and copy; they need the canvas runtime
to render). The live canvas: <https://claude.ai/artifact/WJm4ovXspD7hbyD83BH9JL>

| Mockup | What it shows |
|---|---|
| `Main.dc.html` | Phone, Watch screen, simple mode (the Arm button toggles) |
| `Alert.dc.html` | Phone, a Guard alert while away |
| `Export.dc.html` | Phone, Data: downloads and storage |
| `Desktop.dc.html` | Desktop, Watch, simple mode |
| `Advanced.dc.html` | Desktop, Advanced mode (Signals page) |

## Look

Dark command centre. No gradients, no emoji.

| Token | Value | Use |
|---|---|---|
| ground | `#090C10` | page |
| panel | `#10151B` | cards |
| well | `#0C1015` | maps, inner tiles |
| line | `#1E2630` | borders |
| text | `#E6EDF3` | |
| muted | `#8B98A5` | labels (passes 4.5:1 on panel) |
| present | `#3DD6C8` (cyan) | someone here, good link, accent |
| moving / alert | `#FF8A3D` (orange) | movement, guard armed, alerts |
| threshold | `#FF5A4F` | threshold lines only |
| long window | `#A78BFA` | the 4 s score in Advanced |
| empty | `#4A5664` | empty spans |

Fonts: Inter (text), JetBrains Mono (labels, numbers; small caps-style
labels at 11 px, letter-spacing .12em). Cards 14–16 px radius. Touch targets
44 px or more.

## Structure

**Simple mode (default): 4 tabs.** Phone: bottom tab bar. Desktop: sidebar.

- **Watch**: guard bar (state + Arm/Disarm), "Now" card (state in words,
  e.g. "Present · still", activity and where: "Sitting, near the desk link",
  how long, three tiles: breathing, activity ×, link health), the sensing map
  (links as lines from the board; a link that sees movement lights up, with a
  dot for the person), the timeline (arrived, left, walking 1 min 40 s, sat
  down, got out of bed, alerts), and a 24 h strip (present / moving / empty).
- **Activity**: today's Activity page (calibration by example), restyled.
- **History**: the Stats page plus past timelines; each day downloadable.
- **Settings**: network, sensitivity, buzzer, LED, light, guard, away,
  storage, and the **Advanced mode** switch.

**Advanced mode** (switch in the header, remembered per browser) adds:

- **Signals**: score × quiet level with thresholds, Doppler, breathing.
- **Links & map**: today's Links page plus map editing (dragging dots).
- **System**: Debug plus a live health panel: server CPU, data rate to this
  page, storage write rate, USB lines lost, queue drops.

Today's 8 tabs map: Home → Watch; Activity → Activity; Stats → History;
Graphs → Signals; Links + Map → Links & map; Debug → System; Settings →
Settings.

## New features

### Guard mode
Armed (button, schedule, or Away mode): any movement, or presence while away,
is an alert:
- the board: siren (alarm mode) and red LED, even without the PC (`EXT,1`
  already exists; a guard flag on the board is better so it survives the PC
  going away),
- a phone push: a webhook URL, e.g. ntfy.sh, Home Assistant, Discord (same
  secret handling as the light URLs: stored 0600, never shown back),
- the light (the existing Light on motion),
- an **alert clip**: raw data from 30 s before to 30 s after, kept even in
  Events-only storage.
Alert screen actions: Sound siren, Lights on, It's me (disarm), Dismiss.

### Away mode
The board already hears devices on the network (`DEV` lines). Pick your phone
(phones keep one private MAC per network, so it is stable). Not heard for
10 min → away → guard arms; heard again → home → disarms. Timeline shows
"Left" / "Arrived · your phone joined".

### Download
- **Download everything (.zip)** for the current session: events log,
  raw data, activity clips and labels, alert clips, `summary.csv`.
- Or just: timeline (`events.csv`), last hour raw, alert clips; older days
  from History.
- Downloads are links, so the server cannot require the `X-Ackwave` header
  on them: POST `/api/export` (header checked) returns a one-time token, then
  GET `/api/export?token=…` streams the zip.

## Measured: CPU and data (2026-11-19)

Replaying the real `test/data/move.log.gz` capture at its real rate (about
100 ACK/s, router link only):

| What | Measured |
|---|---|
| `web.py`, no page open | 6 % of one core |
| one page open | 8 % |
| three pages, 10-min window | 11 % |
| per-link analysis on, one page | 9 % |
| browser (headless Chrome), Home / Graphs | 45 % / 46 % of one core |
| browser, Activity page (no charts) | 26 % |
| data to each page | 220–280 KB/s (fast payload 44–57 KB × 4/s, slow 25–45 KB/s, debug every 2 s even off the Debug page) |

So the server is light and the **browser is the load**: whole chart windows
are re-sent and re-parsed four times a second, and charts redraw at ~30 fps.
Over an SSH tunnel to a phone that is about 1 GB an hour.

**Fix:**
- Send only new points (appends), and only what the page on screen draws;
  debug payload only on the System page.
- Simple mode: about 1 update a second, a few KB/s.
- Charts at 10 fps (Advanced) and none in simple mode except the map.
- Keep the existing `document.hidden` pause.

## Measured: storage

One hour at ~100 ACK/s (362,415 packets), router link only:

| Format | MB / hour |
|---|---|
| today: text lines (base64), gzip level 1 | **35.0** |
| text, gzip level 6 | 32.6 |
| binary records, gzip level 1 | 26.3 |
| binary, delta to previous packet | 28.0 (noise: no gain) |
| amplitude only (52 bytes/packet) | ~13 (estimate, not measured) |

The README's 55 MB/h includes device links.

**Plan:**
- Storage modes (Settings → Storage): **Compact** (default; amplitude only:
  everything the current tools use), **Full** (with phase, for research),
  **Events only** (timeline plus a minute around each motion and alert).
- Retention: delete the oldest raw data past a cap (default 8 GB) or age
  (14 days). Timelines are always kept.
- Crash safety: today the gzip stream is only closed at exit, so a crash or
  power cut can lose what was buffered. Flush it (`Z_SYNC_FLUSH`) every
  60 s.
- On a Pi's SD card, Compact or Events only.

## What ESPectre taught us

[ESPectre](https://github.com/francescopace/espectre) (GPLv3) is a mature
ESP32 motion sensor: firmware for six chips, Home Assistant / Matter, a
published dataset and a small neural network. We take ideas from its docs,
never its code (ackwave is MIT), and use its dataset only on this PC for
testing, never in the repo or in shipped weights.

**Where ackwave is already ahead.** ESPectre must join the Wi-Fi network
and make traffic (pings, or its own null frames to the router); one board
watches one link. Ackwave joins nothing, works on any nearby network, and one
board watches up to 8 routers plus 4 device links. Per link it is the same
exchange as their cheapest mode (a null frame and its ACK, about 1.3 % airtime
at 100 a second by their own estimate).
ESPectre's own docs say its "two-device" link needs both boards connected to
the same router; ours needs neither.

**Our detector on their labelled data (first real ground truth).** 270
minutes of recordings (empty room / someone still / normal movement) from 5
chip types in 4 homes, played through our firmware detector
(`build-host/score_lines`) as one session per board: empty first (so it
calibrates as on a real boot), then still, then moving.

| Share of time called motion | empty | someone still | moving |
|---|---:|---:|---:|
| ackwave, default (SENS 1.4), all chips | 4.2 % | 8.5 % | **67 %** |
| ackwave, default, S3 only | 1.0 % | 12.9 % | **56 %** |
| ackwave, SENS 1.2, all chips | 23 % | 27 % | 87 % |
| ESPectre's own model (their report) | under 1 % | under 1 % | about 99 % |

Their CSI comes from data frames, not ACKs, and their score is measured on
their own homes, so it flatters them. Still, the lesson is clear: one
threshold on one energy number cannot be both quiet and sensitive (1.2 catches
87 % but calls an empty room motion 23 % of the time). Several numbers that
describe *how* the signal moves can.

**Ideas worth taking:**
1. **Gain-proof numbers.** Every feature is a ratio (spread ÷ mean,
   correlation, crossing rate), so the radio's automatic gain changes cancel.
   Their best two: how much the per-packet spread changes from one packet to
   the next (lag-1 autocorrelation) and its robust range (IQR ÷ mean) over 1 s.
2. **A tiny network on the board.** 8 numbers → 24 → 12 → 1 (529 weights);
   on an S3 it costs about 1.7 % more CPU than their plain detector. That fits
   our satellites.
3. **Fixed time slots.** Packets are placed on a 10 ms grid (one per slot),
   so features with time lags mean the same at any packet rate; a window
   needs 70 % of its slots filled to count.
4. **Outlier filter** (Hampel: replace a value more than 5 MADs from the
   median of its 7 neighbours) before scoring.
5. **Calibration that survives a passer-by.** If the worst 5 s of the 10 s
   calibration holds a burst, drop it and extend in 5 s steps (up to 30 s),
   instead of starting over; after 60 s of proven quiet the threshold may only
   go *down*.
6. **Honest testing.** Score by whole recording, room and board (never mix
   windows of one recording into training and testing), report the *worst*
   recording, and set an alarm budget for long empty recordings.
7. **Presence is its own question**, not a lower motion threshold: a still
   person shows up in breathing-band power and slow drift. It is on their
   roadmap, not shipped. We already have a breathing tile and the activity
   page's still-body shape, so we can get there first.
8. **Identifying people is weak on one ESP32 link.** Their literature notes:
   about 95 % for 2 people and 74 % for 3 in controlled tests, and a 2026 paper
   finds gait separation stuck at 45–56 %. For 4 housemates expect worse than
   74 %. Parked; if ever tried: walking only, opt-in, everyone told.

## Better motion detection

1. **Benchmark first** (`tools/bench_external.py`): replay any labelled
   recordings (ESPectre's, kept outside the repo, and our own activity clips)
   through the firmware detector; report empty / still / moving shares, the
   worst recording, and false alarms per hour. Every detector change must not
   make the worst case worse.
2. **More numbers, PC side:** add the gain-proof features above next to our
   score, fit a small logistic model (then a tiny network) on our own data
   plus the benchmark, and show it in Advanced beside the board's verdict.
3. **On the board** once it beats the current detector on the benchmark
   without more false alarms: same features in `csi_core.h` (with host tests
   against the Python), weights stored in NVS so the PC can update them.
   The current detector stays as the fallback and for weak links.
4. Burst-tolerant calibration and the down-only settle rule (ideas 4–5).

## Activities v2

The user's feedback on the Activity page: it works well; keep the learning
when the board or router moves; and posture and movement happen together
("sitting and moving", "lying and still").

- **Two answers, not one:** *posture* (standing, sitting, lying, away) and
  *movement* (still, moving, walking). A clip can set one or both. The page
  shows "Sitting · moving". The moments (sitting down, getting up) stay.
- **Survive a move:** the movement numbers mostly carry over; the still-body
  shape belongs to one placement. Clips record a *placement* (which router,
  board spot). After a move: movement keeps working, posture asks for one
  short still clip per posture ("re-check sitting here"). Clips from several
  placements are kept and all used, so it gets sturdier.
- The gain-proof features above help both.

## Say which router each device is on

Today the pages say "router", "AP 1" and list devices without their router.
The board already reads the router's address in every frame it uses to list
a device (`seen_note` in `csi_core.h`) but does not keep it.

- Firmware: keep the router (AP slot) per device and add it to `DEV` (old
  dashboards ignore the extra field).
- Dashboard: name routers everywhere ("Router 2 · <network> · ch 6 · −52 dBm",
  with a nickname you can set), group devices under their router, and show
  each link as "board ↔ router 2" or "board ↔ Phone (on router 1)".
- Map and Links use the same names.

## Satellites and a Pi hub

More ESP32-S3 boards plus one on USB as the hub (on a PC or, later, a
Raspberry Pi); no board joins any network. Built so far: boards find and
poke each other, and send their own verdict sealed with AES-CCM under a key
per board (no ESP-NOW). The plan for several boards in the dashboard,
including that design, is in [multi-board.md](multi-board.md).

## Build order

1. Light data stream (appends, per-page payloads) and the simple Watch
   screen.
2. Routers named and devices grouped under them (small firmware change).
3. Tabs collapsed to 4 + the Advanced switch.
4. Detection benchmark, then the gain-proof features PC side.
5. Activities v2 (posture + movement, placements).
6. Guard mode, then Away mode.
7. Download (zip + token).
8. Storage modes, retention, periodic flush.
9. Detection on the board, then satellites and the Pi hub (needs the
   boards).

Each step keeps the tests green (`make test`); `tools/test_web.py` checks the
page list (`ROUTES`) and that nothing from the network is put on the page as
HTML.
