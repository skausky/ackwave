# ackwave

The ackwave dashboard in Home Assistant's sidebar. Plug the hub board (the
one the other boards report to) into this machine's USB; other boards can be
anywhere on any power. The page is only reachable through Home Assistant
(its login), never directly on your network.

Options:
- `port`: the hub's serial port, e.g. `/dev/ttyACM0`. Empty: every board on USB.
- `keep_raw`: also keep every board line (several hundred MB a day). Off by default.

Data, settings and the secret the boards' keys come from are in
`/share/ackwave` (owner-only). Flash and pair boards from a PC with the
ackwave repository; copy that PC's `sessions/dashboard.json` here once so the
boards keep their keys.

Firmware for the hub: the PC builds it and copies `flash_args` with the
files it names into `/share/ackwave/firmware`, plus an empty `FLASH_ME`.
The next start of the add-on flashes the hub (on the Pi's USB) once, then
renames the marker. The hub never has to come back to the PC.

If the hub's USB drops (a glitch, a replug, the board restarting after a
network change), the dashboard reconnects by itself. If the port stays gone
a minute, the add-on exits; with **Watchdog** on (the add-on's Info tab)
Home Assistant starts it again, which also picks up a re-created USB device.

## Using it

- **Home** shows one line (who's moving and where) over a live map. The
  technical charts are folded under *More details*.
- **Map**: place the hub and the boards (square, floor, the way each faces:
  drag the small arrow). Routers are optional: leave them on *automatic*, or
  give a rough spot (a circle) or an exact one. *Satellite picture* fetches a
  picture of your property once, from your browser (Esri World Imagery),
  given the hub's latitude and longitude; nothing else is sent. *Accuracy*
  colours each spot by how well the boards can pin someone down there;
  *Suggest a spot* tries every place for one board and shows the best move.
- **Zones** (Map page): draw room-sized areas. The add-on tracks people all
  the time (no page needs to be open) and says when someone walks into one;
  with *notify me*, your phone too.
- **Phone notifications** (Settings): Pushover. Paste your application's API
  token and your user key; one message per stretch of motion, a daily cap,
  optional night hours. The keys stay in `/share/ackwave/dashboard.json`.
- **Several boards** (Settings): which boards count for the light and alerts,
  and each board's *Recalibrate* and *Beep*, sent through the hub over the
  air. Choosing another network in Settings moves every board to it: the hub
  tells the others first, then switches.
- **Learned (experimental)** in the timeline is a small learned motion
  detector running beside the board's own verdict, for comparison only.
