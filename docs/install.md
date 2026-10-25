# Installation

Requirements: Python 3.12 or newer (on Windows 3.12 or 3.13, see [Windows](#windows)) and a
Whadda WSEDU09 / Velleman EDU09 connected over USB. The `hidapi` wheel bundles the HID library, so
no system HID library is needed. Tested platforms: Linux and Windows with real hardware; macOS is
untested.

```bash
git clone https://github.com/BonBear/openedu09.git && cd openedu09
python3 -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e '.[gui]'                            # GUI + driver + MCP server
pip install -e .                                   # headless: driver + MCP server only
pip install -e '.[gui,dev]'                        # development (tests, ruff, mypy)
```

With uv: `uv sync --all-extras` (or `uv pip install -e '.[gui]'`).

This installs `edu09-gui`, `edu09-mcp`, `edu09-calibrate`, `edu09-gain` and `edu09-probe` (see
[usage](usage.md)). Try without hardware first: `edu09-gui --simulate "sine:1khz:2vpp"`.

## Linux

### udev rule (required)

The device appears as `/dev/hidrawN`, root-only by default. Install the rule shipped in the repo
with the helper script (recommended; it uses `sudo` to copy the rule to
`/etc/udev/rules.d/99-velleman-edu09.rules` and reloads udev):

```bash
sh packaging/linux/install-udev.sh
```

or do the same by hand:

```bash
sudo cp packaging/linux/99-velleman-edu09.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
```

Unplug and replug the scope. The rule gives access to the logged-in desktop user (`uaccess`) and
to the `plugdev` group. On systems without a desktop seat (SSH sessions, servers, containers), add
yourself to that group and log in again:

```bash
sudo usermod -aG plugdev "$USER"      # Fedora/Arch: the group may not exist; create it first
                                      # with: sudo groupadd -r plugdev
```

Check: `ls -l /dev/hidraw*` shows the scope as `root:plugdev` mode `0660` (or an ACL for you), and
`edu09-probe list` finds it.

The driver uses the `hidraw` backend, so the kernel driver stays bound; the `SUBSYSTEM=="usb"`
line in the rule is only needed if you force the libusb backend.

### Qt on X11 needs libxcb-cursor0

Qt 6.5+ requires `libxcb-cursor` for the `xcb` platform plugin. Without it the GUI aborts with
"Could not load the Qt platform plugin xcb"; `edu09-gui` checks for it first and prints the
install command instead (set `EDU09_SKIP_QT_CHECK=1` to skip the check).

| Distribution | Package |
|---|---|
| Debian / Ubuntu / Mint | `sudo apt install libxcb-cursor0` |
| Fedora / RHEL | `sudo dnf install xcb-util-cursor` |
| Arch / Manjaro | `sudo pacman -S xcb-util-cursor` |
| openSUSE | `sudo zypper install libxcb-cursor0` |

On Wayland sessions Qt uses the `wayland` plugin when available (`libqt6-wayland` /
`qt6-wayland` may be needed on some distros) and otherwise falls back to XWayland through xcb,
which again needs `libxcb-cursor0`. Force a backend with `QT_QPA_PLATFORM=xcb` or
`QT_QPA_PLATFORM=wayland`. `QT_DEBUG_PLUGINS=1` shows why a plugin fails to load.

## Windows

The scope is a standard HID device, so **no driver is needed**; Windows binds `HidUsb`
automatically. Install Python 3.12 from python.org, then in PowerShell:

```powershell
py -3.12 -m venv .venv
.venv\Scripts\activate
pip install -e ".[gui]"
edu09-gui
```

**Use Python 3.12 or 3.13.** `hidapi` (and some other dependencies) has no pre-built wheels on
PyPI for the newest Pythons yet (none for 3.15 at the time of testing; 3.14 is untested here), so
pip tries to compile it from source, which needs the MSVC build tools and typically fails with
`LNK1104` (cannot open file) because the build path exceeds Windows' 260-character limit. If you
have several Pythons installed, create the venv explicitly with `py -3.12 -m venv .venv` as
above. (If you must build from source: enable long paths,
`HKLM\SYSTEM\CurrentControlSet\Control\FileSystem\LongPathsEnabled = 1`, and clone into a short
path such as `C:\src\openedu09`.)

Notes:

- Verified on Windows with hardware (Python 3.12): all hardware tests, the GUI, the in-process
  MCP server, the `edu09-mcp` stdio bridge and the owner lock (the lock is released when the
  owner is killed or closes normally).
- Windows buffers up to 64 input reports, so several status reports may precede a read-out; the
  driver handles this.
- Do not run the vendor EDU09.exe at the same time (only one program can open the device).
- If the device is reported busy, check for a stale `python.exe`.
- MCP client config: use the full path of `.venv\Scripts\edu09-mcp.exe`.
- Please report problems with the output of `edu09-probe list`.

## Files and locations

Paths come from `platformdirs` (on Linux they follow `XDG_*`):

| What | Linux | Windows |
|---|---|---|
| `calibration.json`, backups `calibration.<stamp>.bak.json`, `calibration_sources.json` | `~/.config/edu09/` | `%LOCALAPPDATA%\edu09\` |
| MCP CSV exports (`exports/`, see `--export-dir`), wizard session logs (`calibration-sessions/`) | `~/.local/share/edu09/` | `%LOCALAPPDATA%\edu09\` |
| Owner lock (`device.lock`, `owner.json`) | `$XDG_RUNTIME_DIR/edu09/` | `%LOCALAPPDATA%\edu09\Cache\` |

Set `EDU09_RUNTIME_DIR` to move the lock directory. Without a usable runtime directory (containers,
cron) a private `edu09-<uid>` directory under the temp dir is used. Probe captures
(`edu09-probe`) go to `captures/` in the current directory.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `DeviceNotFound: no EDU09 device found` | Not plugged in, a charge-only cable, or a USB hub problem. `lsusb -d 10cf:ed90` (Linux) should list it. |
| `DeviceAccessError` (permission denied on `/dev/hidrawN`) | udev rule missing or not applied: install it, reload, replug. Check group membership with `id`. In a container pass the hidraw device through. |
| `DeviceBusy: EDU09 is in use by gui (pid ...)` | Another edu09 process (GUI or MCP server) owns the scope. Close it, or talk to it: the GUI serves MCP at the URL shown in the message (`edu09-gui --mcp-http`). A crashed owner releases the lock automatically (it is an OS file lock). |
| Device busy but nothing running | Another program has the device open (e.g. the vendor software, under Wine on Linux), or on Windows a stale `python.exe`. Close it. |
| GUI: "Could not load the Qt platform plugin xcb" | Install `libxcb-cursor0` (table above). |
| GUI: `ImportError: PySide6` | Installed without the extra: `pip install -e '.[gui]'`. |
| pip fails on Windows with `LNK1104` | a Python without `hidapi` wheels (e.g. 3.15): use Python 3.12 or 3.13 (see [Windows](#windows)). |
| Frames stop after unplug | The driver reconnects by itself when the device returns; the GUI shows the state. |
| Readings a few percent off (uncalibrated units read 0.4-2.6 % high) | The gain differs per unit and range. Run Tools > Calibrate (or `edu09-calibrate`), see [usage](usage.md#calibration). |
| 0 V reads off | Run the calibration with the input disconnected; without a calibration file typical offsets (no gain correction) are used and the status bar shows "Uncalibrated". |
| A readout timeout shows up at GUI startup | Seen once, not reproduced. Start with `edu09-gui --record FILE` and attach the file to a bug report. |
