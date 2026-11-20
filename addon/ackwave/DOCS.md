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
