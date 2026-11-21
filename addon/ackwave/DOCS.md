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
