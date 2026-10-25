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
- Calibration and unknown opcodes are gated behind explicit flags and confirmations.

## Reporting a vulnerability

Please do not open a public issue for security problems. Use GitHub's private vulnerability
reporting ("Security" tab, "Report a vulnerability") on this repository. Include affected
version, steps to reproduce and impact. This is a hobby project; expect a best-effort response.
