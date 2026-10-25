# Security policy

## Scope

openedu09 is a local driver, GUI and MCP server for a USB device.

- The MCP HTTP server binds to **127.0.0.1 only** by default and enables DNS-rebinding protection.
- `--allow-remote` (needed for a non-loopback `--host`) exposes the server on the network
  **without authentication or TLS**. Anyone who can reach it can control the scope. With it,
  add the Host names clients use via `--allowed-host NAME[:PORT]` (repeatable); other Host
  headers are rejected. Use it only on a trusted network or behind your own authenticating proxy.
- The stdio-to-HTTP bridge (`edu09-mcp` forwarding to the GUI) only talks to loopback owner URLs
  (`127.0.0.0/8`, `::1`, `localhost`), because `owner.json` is user-writable; anything else is
  refused. If the owner's `owner.json` disappears (owner exited), the bridge reports `OwnerGone`
  instead of reusing the old URL (another program may listen on that port now). Forwarded tool
  calls are limited to 330 s and upstream JSON responses to 8 MiB. `--no-bridge` disables
  forwarding.
- The stdio MCP server inherits the privileges of the launching client.
- Calibration and unknown opcodes are gated behind explicit flags and confirmations: the MCP
  `calibrate` and `calibration_*` tools exist only with `--allow-calibration`, saving needs
  `confirm=true`, the zero step needs `input_disconnected=true`, and reference readings must be typed
  by the human (the tools forbid agents to invent them). `edu09-probe` sends nothing beyond read-only
  commands without `--allow-acquire`, and unknown opcodes need `--allow-unknown` plus a typed
  confirmation.
- `export_csv` only writes plain `*.csv` file names inside the server's export directory (no paths,
  no overwriting unless asked).
- Only one process can own the scope (file lock), so another local program cannot interleave
  commands with a running GUI or MCP server. The Linux udev rule gives the device to the logged-in
  desktop user and the `plugdev` group only (mode `0660`).
- Debug recordings (`--record`) contain the USB reports exchanged with the scope (no credentials);
  check a file before attaching it to a public issue.

## Reporting a vulnerability

Please do not open a public issue for security problems. Use GitHub's private vulnerability
reporting ("Security" tab, "Report a vulnerability") on this repository. Include affected
version, steps to reproduce and impact. This is a hobby project; expect a best-effort response.
