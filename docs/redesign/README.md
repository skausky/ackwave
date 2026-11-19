# Control panel redesign (plan, not built yet)

The dashboard (`tools/web.py`, `tools/web/`) is thorough but heavy: eight
tabs, every chart at once, and a lot of data to the browser. This is the plan
for a simpler control panel with an advanced mode, plus guarding features,
easy downloads and lighter storage. Nothing here changes the board's firmware
or its motion verdict.

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

## Build order

1. Light data stream (appends, per-page payloads) and the simple Watch
   screen.
2. Tabs collapsed to 4 + the Advanced switch.
3. Guard mode, then Away mode.
4. Download (zip + token).
5. Storage modes, retention, periodic flush.

Each step keeps the tests green (`make test`); `tools/test_web.py` checks the
page list (`ROUTES`) and that nothing from the network is put on the page as
HTML.
