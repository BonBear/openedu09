# Installation

Requirements: Python 3.12 or newer, an EDU09 connected over USB. The `hidapi` wheel bundles the
HID library, so no system HID library is needed.

```bash
python3 -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e '.[gui]'                            # GUI + driver + MCP server
pip install -e .                                   # headless: driver + MCP server only
pip install -e '.[gui,dev]'                        # development (tests, ruff, mypy)
```

With uv: `uv sync --all-extras` (or `uv pip install -e '.[gui]'`).

This installs `edu09-gui`, `edu09-mcp`, `edu09-probe` and `edu09-gain`. Try without hardware first:
`edu09-gui --simulate "sine:1khz:2vpp"`.

## Linux

### udev rule (required)

The device appears as `/dev/hidrawN`, root-only by default. Install the rule shipped in the repo:

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
"Could not load the Qt platform plugin xcb".

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

The EDU09 is a standard HID device, so **no driver is needed**; Windows binds `HidUsb`
automatically. Install Python 3.12 from python.org, then in PowerShell:

```powershell
py -3.12 -m venv .venv
.venv\Scripts\activate
pip install -e ".[gui]"
edu09-gui
```

**Use Python 3.12 or 3.13, not 3.14+/3.15.** `hidapi` (and some other dependencies) has no
pre-built wheels on PyPI for the newest Python yet, so pip tries to compile it from source, which
needs the MSVC build tools and typically fails with `LNK1104` (cannot open file) because the build
path exceeds Windows' 260-character limit. If you have several Pythons installed, create the venv
explicitly with `py -3.12 -m venv .venv` as above. (If you must build from source: enable long
paths — `HKLM\SYSTEM\CurrentControlSet\Control\FileSystem\LongPathsEnabled = 1` — and clone into a
short path such as `C:\src\openedu09`.)

Notes: simulator mode has been run on Windows; hardware on Windows is being tested. Do not run the vendor EDU09.exe at the
same time (only one program can open the device). The owner lock lives under the user's local
app data; if the device is reported busy, check for a stale `python.exe`. Please report problems
with the output of `edu09-probe list`.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `DeviceNotFound: no EDU09 found` | Not plugged in, a charge-only cable, or a USB hub problem. `lsusb -d 10cf:ed90` (Linux) should list it. |
| `DeviceAccessError` (permission denied on `/dev/hidrawN`) | udev rule missing or not applied: install it, reload, replug. Check group membership with `id`. In a container pass the hidraw device through. |
| `DeviceBusy: EDU09 is in use by gui (pid ...)` | Another edu09 process (GUI or MCP server) owns the scope. Close it, or talk to it: the GUI serves MCP at the URL shown in the message (`edu09-gui --mcp-http`). A crashed owner releases the lock automatically (it is an OS file lock). |
| Device busy but nothing running | Another program has the hidraw node open (e.g. the vendor software under Wine). Close it. |
| GUI: "Could not load the Qt platform plugin xcb" | Install `libxcb-cursor0` (table above). |
| GUI: `ImportError: PySide6` | Installed without the extra: `pip install -e '.[gui]'`. |
| Frames stop after unplug | The driver reconnects by itself when the device returns; the GUI shows the state. |
| Voltage readings 5-10 % high/low | Gain is not calibrated (see README). Offsets are: run Tools > Calibrate with the input open. |
| Zero reads off | Run the calibration (input open); without a calibration file typical values are used. |

Calibration data: `~/.config/edu09/calibration.json` (Linux; `platformdirs` user config dir
elsewhere). Owner lock: `$XDG_RUNTIME_DIR/edu09/`.
