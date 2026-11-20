# Several boards: plan

Approved 2026-11-20. Goal: more coverage (each link is its own sensing zone),
fewer false blips (a second link confirming a brief one) and telling router
glitches from people. Not measured yet: whether a second board helps. Step A
exists to measure it.

Fixed requirements: one board keeps working exactly as now, including its
`sessions/` files; the later hub stream must fit without a rewrite; the
security model stays (localhost only, the command header, nothing from a
board put into the page as HTML); `make test` stays green;
`tools/fake_csi.py` can play several boards.

## What assumes one board today

| Where | Assumption |
|---|---|
| `web.py main()` | one source, one reader thread, one `send`, one `SessionLog` |
| `Dashboard` | one `gui.State` (`self.s`): detector, calibration, spans, the board-clock mapping (`wall_off`), network, sound/sens/LED, `Devices` |
| Link identity | `ap:N` and nodes 8-11 are per board, so timeline keys `board` / `ap:N` are ambiguous across boards |
| `gui.State.feed` | a clock going back > 1 s means a restart: interleaved boards would reset each other |
| Commands | `outbox` goes to the one board |
| `dashboard.json` | mixes PC-wide settings with per-board ones (sound, sens are re-applied to "the board") |
| `devices.json`, `activities.json` | one device mode and pick list; clips from one board's router link |
| `map.json`, `map.js` | positions in metres from "the board"; `ap:0` is the board's verdict |
| `record.py` | one Recorder; events log and Stats follow one board |
| Front end | one hero, one score chart, one breathing/Doppler |
| `fake_csi.py` | one board |

## Data model

- **Board**: id = its Wi-Fi MAC (`ME`, the hello); your name; how it is
  reached (`usb:<port>` or `hub:<hub>`); what it sends: full (CSI) or
  summary only (a hub can't relay CSI: ~17 KB/s per board on the sensing
  channel).
- **Link** = (board, target: router BSSID, device MAC or another board).
  A zone belongs to a link, not a board.
- **Event** = (board, link, t0, t1, peak, source: board verdict or PC per-link).
  House events are combined from these (below).
- Internally every line is `(board_id, line)`. USB readers tag lines once
  `ME` arrives; the hub stream will be demultiplexed into the same pairs.
  In the raw log the primary board's lines stay unprefixed (one board =
  byte-identical to today); others get `@<id>,`.
- `Dashboard.boards = {id: BoardCtx}` wraps today's `gui.State` + `Devices`
  unchanged; `self.s` is the primary board until each page is converted.
- **The primary board is chosen by saved MAC** (`hub` in `dashboard.json`,
  the first board ever connected), never by port order: ttyACM numbers swap
  on replug.
- Settings: PC-wide in `dashboard.json` as today; per-board in a
  `boards: {mac: {...}}` section, the primary still reading today's keys.
  Clips get a `board` field. Timeline keys `board@<id>`, `ap:<n>@<id>`,
  `<mac>@<id>`; keys without `@` are the primary board (old files read as
  before). Other boards' own verdicts already use `sat:<mac>`.

## Pages

- Home: the house verdict, "seen by Kitchen + Hall", a tile per board.
- Where and when: rows grouped by board (Step A's comparison view).
- Graphs, Links, Debug, Activity: a board picker; CSI-only views say
  "summary only" for hub boards.
- Map: boards placed in house coordinates (origin = the primary board, so
  old maps keep working); lines between any two ends.
- Settings: PC-wide first, then one section per board.
- Stats: house totals; per-board filter later.

Router glitches: every router link shares the router's end, so a person by
the router moves all of them at once, like a glitch. Evidence of a glitch
is ACK gaps or RSSI steps on every board's router link at once while the
board-to-board links stay quiet. Those links are the independent witness.

## Clock

Each board counts µs since its boot; crystals drift up to ~±20 ppm (about
70 ms an hour), so a mapping fixed at start goes wrong. Per board: the
offset is the **minimum of (PC receive time − board time) over a sliding
window of about 10 min**, or a fitted line (offset + drift) when the window
shows drift. A plain running minimum would stick if a board's clock runs
slow. USB boards: a few ms. Hub boards (later): the hub stamps each `SAT`
line, about ±100 ms. Restart detection runs per board.

## Combining verdicts (a setting)

1. **Any board.** Most coverage, most blips.
2. **Confirm brief blips (default).** Motion over 2 s from any one board
   counts alone; a blip under 2 s counts only if another link (another board,
   or the same board's long window) agrees within ±1.5 s. This targets what
   the 11-13 analysis found: about two thirds of blips false, sustained
   periods real.
3. **Two boards agree.** For alarms (Guard / Away later).

## Build order (first plan; status 2026-11-20)

Done and on main: A, C, D (map with every board and link), E (the Pi, as a
Home Assistant add-on), the airtime check (no drop with 3 boards: 92-94 ACK/s
each). B is half done (server side; the Settings picker is not). The roadmap
at the end of this file replaces this list from here on.


- **A** (USB boards only): `./csi web <port> <port> ...`; the primary board
  (saved MAC) behaves exactly as today; other boards get their own state,
  verdicts on the timeline, rows in Where and when; `fake_csi.py --boards N`
  (own MACs, clock offsets and drift, scripted "only A", "both", "router
  glitch"); `tools/compare.py` counts periods seen by A only, B only and
  both (±2 s) and blips confirmed by the other board.
- **Airtime check, before B**: router ACK/s and answer rate per board with
  1, 2 and 3 boards poking. If it drops, say so before going further.
- **B**: board picker; `/cmd` gets a `board` field (checked against known
  boards); per-board settings.
- **C**: combining rule and house verdict; the light follows it.
- **D**: map with several boards and board-to-board links.
- **E**: the hub stream (`SAT`/`FWD` with the hub's timestamp), the Pi.
- **F**: polish; README throughout.

## Reports between boards (built 2026-11-20)

Boards find each other with a hello broadcast twice a second (a vendor
action frame from the board's real MAC: magic `ACKW`, version, channel,
router BSSID). Any board can poke another like a device (board-to-board
links answered 78-92 % at -40 dBm). Version 3 adds the board's own verdict,
**sealed**:

- AES-CCM (RFC 3610, 8-byte tag, `main/seal.h`) over flags, scores, ACK
  rate, signal, links and uptime; the sender MAC, magic, channel, BSSID and
  counters are checked by the tag. Readable: who is there and on which
  router. Not readable or changeable: anything about motion.
- **A key per board**, derived on the PC: HMAC-SHA256(secret, "ackwave-board"
  + MAC)[:16]. One secret in `sessions/dashboard.json` (owner-only, never in
  git). `KEY,<hex>` gives a board its own key; `PEERKEY,<mac>,<hex>` gives
  the hub its boards' keys. A board that isn't the hub holds no one else's,
  so one stolen satellite reveals only its own reports. Boards print
  fingerprints (`KEY,<fp>`, `PEERKEY,<mac>,<fp>`), never keys; the Debug
  page shows these commands as `<hidden>`.
- **Limit: 8 boards per hub** (`MAX_PEERS`). ESP-NOW is not used, so its
  encrypted-peer limit (7 by default, 17 at most) does not apply.
- **Replays**: nonce = MAC + boot counter (saved, +1 per start) + 24-bit seq.
  The hub accepts only a (boot, seq) newer than the last it accepted from
  that board, and saves each board's boot, so a recording from an earlier
  boot stays refused after the hub restarts. (Within the current boot, a
  hub restart forgets the seq until the next real report, 0.5 s later.)
  ESP-NOW's own encryption documents no replay check, which is one reason
  not to rely on it.
- **Timing**: hellos go out at a fixed 2/s, always the same length, motion
  or not.
- Pairing: plug each board into the PC once while the dashboard runs.

## Measure on hardware

1. Airtime with 1, 2, 3 boards poking (above).
2. CPU load from overheard pokes: `STATS` drop / queue-full / lost counters.
3. Board-to-board links through real walls.
4. Clock drift between two USB boards over an hour.
5. How long boards disagree on channel after the router changes channel.

## Open questions and risks

- Activities with several boards: a model per board, or the nearest board's.
- Home Assistant later: the PC or Pi publishes the house verdict (MQTT); no
  board joins Wi-Fi.
- Biggest risk: a second board mostly double-counts the same rooms. Step A
  measures before any combining is built.

## What four boards make possible (2026-11-20)

Each board already measures far more than its verdict: how strongly it hears
every other board (hellos), every router (beacons) and every device on the
network (DEV), and how each of its links moves. Sent to the hub, sealed, this
gives:

1. **A map that lays itself out.** Signal strength between every pair
   (board↔board both ways, board→router, board→device) gives rough
   distances (path loss, its exponent fitted to the house). Weighted
   multidimensional scaling places every dot so those distances fit best,
   held by the two or three you placed by hand. Pairs that lose more than
   the fit expects show where walls are ("8 dB more than open air between
   Kitchen and Hall"). Dots get an uncertainty ring; placing one more
   tightens the rest.
2. **Where a person is, not only that someone moved.** Each link has a
   sensitivity map (the glow); when links report their motion scores, the
   likelihood of a person at each spot is the product over links of how
   well that spot explains each score. With 4 boards × (router, devices,
   each other) about 20-30 links cross the house: a blob on the floor plan,
   "someone in the hall by the kitchen".
3. **Paths and rooms over time.** The blob over time: paths on the floor
   plan, and time per room in Where and when.
4. **Rooms learned, not modelled.** A walk test: stand in each room and
   press its name; the pattern of which links respond becomes that room's
   fingerprint (kNN, like activities), corrected with ✓/✗ as you use it.
   Moving a board means a new walk test, minutes not days.
5. **Devices shared smartly.** Boards choose which devices to poke so links
   cross different rooms (coverage), and not every board pokes the same
   device (airtime). A device near one board and far from another is
   poked by both: one more link through the house.
6. **Your own devices' rough location.** The signal of a fixed MAC
   (not phones' private ones) seen by every board places it like a dot.

Needs first: the report carries per-link scores and the activity
features (item 3), and a survey every ~10 s: who the board hears and how
strongly. One reflash of the satellites, then each of 1-6 is PC side.


## Roadmap (revised 2026-11-20)

Ordered by what makes the rest possible and what you will notice first. Each
phase says when it is done; a phase goes to main only after it was tried on
the boards and you said so.

### Phase 1: measure and harden (first, small)

The extra boards are only worth what they can be shown to add, and a house
system must survive weeks without anyone watching.

- **Ground truth from a phone.** A "label" button on the dashboard (works on
  a phone through HA): tap the room you walk into, or "nobody home". Kept
  next to the timeline. Every later claim (a board helps, the location is
  right, a room is recognised) is scored against these labels.
- **Daily report.** compare.py run by the add-on every night: per board,
  periods seen, seen by it alone, blips confirmed, and against the labels
  (hit rate, false alarms per day). On the Stats page.
- **Board health.** Each board's answered-report rate, last heard, channel,
  key state, restarts; Home warns "Kitchen board not heard for 5 min" and the
  log says so. A board's motion never counts while it is stale.
- **Keep the Pi tidy.** Retention for /share/ackwave (events and timeline
  for a year, raw only while switched on, oldest deleted first), and the
  disk space on the Health card.
- **Pairing housekeeping.** Remove a board (its key taken back from the hub);
  rotate every key (new secret, boards re-keyed as each is plugged in).

Done when: a week of labels, a daily report on the Pi, and a board pulled
out of the wall shows up as such within 5 min.

### Phase 2: geometry (the map lays itself out)

- **Survey to distances.** Every report's survey (who a board hears, how
  strongly; both directions between boards) → distance per pair with a path
  loss fitted to the house (and to the PC side's own signal readings).
- **Placement.** Weighted multidimensional scaling (stress majorisation),
  held by the dots you placed by hand and the floor plan's scale; each guess
  gets an uncertainty ring, smaller as more pairs agree.
- **Facing.** Each board gets a direction (drag the arrow on its square). A
  rough pattern for the S3's PCB antenna (strongest ahead, a few dB down to
  the sides, weakest behind the USB end) corrects its readings and tilts its
  glow. Optionally learned: the board pair readings that fit worst hint which
  way a board faces.
- **Walls.** Drawn on the floor plan (two taps, a material each: drywall,
  brick, concrete, glass, metal, door), or suggested where pairs lose much
  more than open air (shown as "a wall here?" to confirm). Paths through
  walls lose their material's dB; the glow bends toward doors and thin walls.

Done when: with 3 dots placed by hand, the rest land within ~1.5 m of where
they are, and the walls suggested match real ones.

### Phase 3: where people are

- **A simulator first.** fake_csi gets a house: the floor plan, boards,
  routers, devices, and people walking paths; each link responds by the
  sensitivity model. It tests everything below without walking around, and
  scores it (location error, rooms right).
- **Location.** Every link's motion score against its sensitivity map gives
  how likely a person is at each spot (the product over links, with each
  link's own noise); a blob on the map, "someone in the hall by the kitchen".
  Two people: two blobs when the links allow it; the map says when it can't.
- **Paths and rooms over time.** The blob over minutes: a faint path on the
  map; time per room in Where and when (rooms are areas you draw, or the
  rooms the walk test learned).

Done when: in the simulator, median error under 1.5 m with 4 boards; in the
house, the right room for 8 of 10 labelled walks.

### Phase 4: learning that adapts

- **Rooms by walking.** Stand in each room, tap its name: the pattern of
  which links respond (and how) is that room's fingerprint (kNN, as for
  activities); ✓/✗ while using it add examples.
- **Activities from every board.** Each satellite computes the activity
  features itself (energy over 3 s and 20 s, burst, power by band, breathing)
  and sends them in its report; the dashboard trains and recognises per
  board, from clips recorded on any board.
- **Adapting, not retraining.** Movement features don't depend on where the
  board is, so they carry over; what does depend (the channel's shape, a
  link's quiet level) is kept per placement and re-learned on its own:
  a board moved or a router changed is noticed (signal step, new channel
  shape), its placement-bound parts start over, the rest stays. Corrections
  you make weigh more than old clips; very old clips fade.
- **Breathing per board.** A cheap on-board rhythm estimate per link, sent
  in the report: "someone still in the bedroom" without USB.

Done when: after moving a board, movement is recognised at once and rooms
again after one short walk test.

### Phase 5: coverage and the radio

- **Shared devices, chosen together.** The dashboard picks which devices each
  board pokes (sent to each board as POKE over the air, sealed, or over USB
  when plugged): links that cross rooms the others don't cover, no device
  poked by every board.
- **A time beacon.** The hub stamps its own clock into an acknowledged
  message; each board keeps the offset, so reports carry hub time
  (milliseconds across the house).
- **Taking turns.** With hub time, each board pokes in its own slots, so
  boards stop overhearing each other's pokes as noise (the drop counter) and
  more boards fit the same airtime.
- **Router glitch filter.** With hub time: every board's router link jumping
  within ~100 ms while board-to-board links stay quiet is the router, not a
  person: dropped from every verdict.

Done when: 4 boards, each with 2-3 devices, still over 90 % answered; a
router glitch makes no motion period anywhere.

### Phase 6: Home Assistant

- **Entities over MQTT** (the broker is already there): house motion, each
  board / room's motion, the person's room, the activity, each board's
  health; with discovery, so they appear by themselves.
- **Its automations instead of URL hooks:** lights per room, "nobody home"
  modes, notifications for Guard / Away (motion while nobody should be
  there).
- Done when: a light follows the room's motion through an HA automation.

### Phase 7: living with it

- **New board wizard:** plug it in → it's found, flashed, paired, named,
  placed on the map; one screen.
- **Satellite updates over the air, through the hub.** Firmware in sealed,
  numbered chunks, the image signed (Ed25519, the private key only on the PC,
  the board checks before it switches), the old image kept to fall back to.
  Then no board ever needs a cable again. Large and security-critical: after
  everything above works.
- **The 5 GHz boards (C5, Christmas):** the same protocol, a link between
  bands' boards through the hub.

### Order and why

1 → 2 → 3 is the backbone: measure, then place, then locate. Phase 4's
"rooms by walking" can start right after 1 (it needs no geometry), and
Phase 6's entities are small, so both can slot in early if you want HA
automations sooner. Phase 5 matters once more boards poke more devices.
Phase 7 last.

### Risks

- RSSI distances indoors are rough (±30-50 %): the auto-layout needs the
  hand-placed anchors and will say how sure it is.
- The sensitivity model is an estimate until walk tests correct it; the
  location is only as good as the links crossing a room (a room with none
  can't be told apart).
- Two people close together look like one.
- Every report's size and every board's airtime grow with links and
  sections: the airtime check is repeated at each phase.
- Over-the-air updates can brick a board if done wrong: signed, verified,
  with a fallback image, and last.
