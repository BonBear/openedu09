# Velleman EDU09 host <-> device protocol (reverse engineered)

Source: static analysis of `EDU09/EDU09.exe` v1.06 (Delphi, 32-bit PE,
SHA-256 `f2d4c6589692025b8acfe3cbf1aa7647a5ed04de8c8c6eeac72904583508b92f`), `EDU09/DSOLink.dll`,
and the help files `EDU09/Info/*.chm`. On 2026-10-24 the orchestrator also observed the USB descriptors of a
real device plugged into the Linux host. Hardware captures (H1, 2026-10-24) are summarised in
**section 11 (Hardware-verified)**, which overrides the static findings where they differ.

Confidence tags:
- **Observed**: read directly from code, data, DFM resources, help text or the real USB descriptors. The
  evidence is a virtual address (VA) in `EDU09.exe` unless another file is named.
- **Inferred**: a reasoned interpretation of observed facts that has not been confirmed on hardware.
- **Unknown**: not determinable statically.

Function names are Delphi published method names recovered from the class method tables (for example
`Set500`, `asetus_1`, `Reset_500`). Several are Finnish: `asetus` means "setting", `aloitus` "start", `lue` "read".

---

## 0. Headline findings

| # | Finding | Status |
|---|---------|--------|
| 1 | The scope is a **USB HID device** (VID 0x10CF, PID 0xED90) reached through SetupAPI and `CreateFileA`/`ReadFile`/`WriteFile` on the HID interface path. It is **not** a serial/COM device. | Observed (0x5a4d60, 0x5a82b0, 0x5a82f0; hardware descriptors) |
| 2 | `TComPort ComPort1` (9600 8N1, COM1) belongs to the **"Wave Sequence" form `TForm900`**, a function-generator sequencing utility that reads commands from a serial port. It has nothing to do with the scope link. | Observed (DFM of `TForm900`; `ComPort1AfterOpen` 0x57a780 sets the caption "Wave Sequence - Input Port: ") |
| 3 | Every transfer is one 64-byte HID report. Commands are 1 byte, except configuration, which is 10 bytes. | Observed |
| 4 | A waveform frame is **1024 bytes = 16 consecutive IN reports**, 8-bit unsigned, **one channel only** (CH1). In hardware mode the CH2 trace is filled with zeros. | Observed (0x5db8d1-0x5db912; 0x56ea74-0x56ea9c) |
| 5 | The protocol is a strict poll state machine: `0x09` reset, `0x0B` arm, poll for status `64 C8 mm`, `0x0A` read-out, then 16 data reports. | Observed (FastTimer1Timer 0x5db5e0) |

---

## 1. Device discovery

### 1.1 Windows (original software)

`USBOpen` -> `sub_5a4d60`. **Observed**:

1. `SetupDiGetClassDevsA(&GUID, NULL, NULL, 0x12)`. The flags are `DIGCF_PRESENT|DIGCF_DEVICEINTERFACE`.
   The GUID at VA 0x5eb394 is **`{4D1E55B2-F16F-11CF-88CB-001111000030}`**, the standard Windows HID
   interface class GUID, the same value `HidD_GetHidGuid` returns. EDU09.exe does not import hid.dll.
   - Note: the orchestrator's message gave `...-88A5-00AA003BB30A`. The bytes in the binary decode to
     `88CB-001111000030`, which is the correct HID GUID.
2. The code enumerates every interface with `SetupDiEnumDeviceInterfaces(member index 0..n)` and fetches its
   path with `SetupDiGetDeviceInterfaceDetailA`. That is two calls: one for the size, then one with
   `cbSize = 5`.
3. It matches when `Pos('vid_10cf&pid_ed90', DevicePath) > 0`. The search string is at VA 0x5a4ef4. The match
   is case-sensitive and lowercase, which works because Windows HID paths are lowercase.
4. On a match it calls `CreateFileA(path, GENERIC_WRITE, FILE_SHARE_READ|FILE_SHARE_WRITE, NULL,
   OPEN_EXISTING, 0, NULL)` and stores the result as the write handle (global 0x5eb3a4). A second
   `CreateFileA(path, GENERIC_READ, ...)` gives the read handle (global 0x5eb3a8). Both handles are
   **synchronous**: there is no `FILE_FLAG_OVERLAPPED`.
5. The loop does **not** break on a match, so the last matching interface wins. Nothing filters on the
   product string or serial number.
6. `OpenDeviceW` (0x5c2540) sets the global `deviceOpen` (0xefb474) to 1 on success. If the device is not
   found the application offers **Demo mode** (global 0xefb451 = 1). `test0`/`test1` are at 0x5c2398 and
   0x5c23c0.

There is no COM port selection logic for the scope.

### 1.2 USB identity (Observed on real hardware, reported by the orchestrator)

| Item | Value |
|---|---|
| VID / PID | 0x10CF / 0xED90 |
| bcdDevice | 0.02 |
| Manufacturer / Product | "Velleman Instruments" / "EDU09 Oscilloscope Experiment Board" |
| Speed / power | Full-speed, 100 mA |
| Report descriptor | `06 00 ff 09 01 a1 01 19 01 29 40 15 00 25 ff 75 08 95 40 81 00 19 01 29 40 91 00 c0`: vendor page 0xFF00, **no report IDs**, Input 64 x u8, Output 64 x u8 |
| Endpoints | 0x81 interrupt IN 64 B, bInterval 1; 0x01 interrupt OUT 64 B, bInterval 1 |
| Linux binding | `hid-generic` -> `/dev/hidrawN` |

### 1.3 Cross-platform mapping (Inferred, standard HID semantics)

| Operation | Windows (as EDU09.exe does it) | Linux hidraw | hidapi (both OSes) |
|---|---|---|---|
| Write a command | `WriteFile(hW, buf65, 65)` where `buf65[0]=0x00` (report ID) and `buf65[1..64]` is the payload | `write(fd, buf65, 65)` with `buf65[0]=0x00` | `hid_write(dev, buf65, 65)` with `buf65[0]=0x00` |
| Read a report | `ReadFile(hR, buf65, 65)`. The payload is `buf65[1..64]` (`USBRead` drops byte 0). | `read(fd, buf64, 64)`. There is no report-ID byte because the descriptor has no report IDs. | `hid_read(dev, buf64, 64)` or `hid_read_timeout` |
| Find the device | SetupAPI with the HID GUID and `vid_10cf&pid_ed90` | Scan `/sys/class/hidraw/*/device/uevent` for `HID_ID=0003:000010CF:0000ED90` | `hid_open(0x10CF, 0xED90, NULL)` |

`ReadFile` in the original program is **blocking with no timeout**. A new driver should use a read timeout
(for example 200 ms) and treat a timeout as "no report yet". Linux needs a udev rule to give the user access
to the hidraw node, for example:
`SUBSYSTEM=="hidraw", ATTRS{idVendor}=="10cf", ATTRS{idProduct}=="ed90", MODE="0666"`.

---

## 2. Serial settings

Not applicable to the scope, which uses HID. For completeness, **Observed** (DFM of `TForm900`), the
unrelated Wave Sequence `ComPort1` is configured as: 9600 baud, COM1, 8 data bits, no parity, 1 stop bit,
`dtrDisable`, `rtsDisable`, no XON/XOFF, and `TriggersOnRxChar`. `ComPort1RxChar` (0x57a3fc) parses
text-file style sequence lines for the function generator. It can be ignored for the scope driver.

---

## 3. Output (host -> device) reports

Low-level writer `USBWrite(Self, buf, len)` at 0x5a82f0, **Observed**:
- It zero-fills a 65-byte stack buffer, copies `buf[0..len-1]` into `report[1..len]`, leaves `report[0]=0`
  (the report ID), and writes all 65 bytes.
- Every command is therefore a 64-byte payload that is **zero-padded**. The tables below use payload byte
  indices; on hidraw/hidapi, add the leading 0x00 report-ID byte.

### 3.1 Command summary

| Cmd byte | Length | Name (inferred meaning) | Built at | Status |
|---|---|---|---|---|
| `0x08` | 1 | STOP / abort acquisition | `Reset_500(dl=4, cl=0)` 0x5c6c47; sent by the Run button when stopping (0x5bac50). Also sent first by the legacy `SendFPGAdata` 0x5c25a3. | Observed (meaning Inferred) |
| `0x09` | 1 | RESET acquisition state | `Reset_500(0,0)` 0x5c6c56 | Observed (meaning Inferred) |
| `0x0A` | 1 | READ-OUT: start sending the captured buffer | `Reset_500(1,0)` 0x5c6c66 | Observed |
| `0x0B` | 1 | ARM / start capture, wait for trigger | `Reset_500(1,1)` 0x5c6c77 | Observed (meaning Inferred) |
| `0x0C` | 1 | Unknown. `Reset_500(2,0)` exists, but no caller passing (2,0) was found. | 0x5c6c87 | Observed (unused) |
| `0x0D` | 1 | Intended as LIVE ADC block (64 live samples). **HW (H2, section 11): firmware 1.01 sends no samples; while armed it starts a ~1 ms status stream until `0x08`; idle: no reply.** Never sent by our driver | `Reset_500(3,0)` 0x5c6c97; used by calibration in `ReadBytes`/`ReadBytes2` 0x56f780/0x56f830 | Observed; meaning **refuted on HW** |
| `0x0E` | 10 | SET CONFIGURATION (see 3.2) | `asetus_1` 0x5c6cb0, payload built by `Set500` 0x5c6894 | Observed |
| `0x0F` | 1 | GET FIRMWARE VERSION. The reply holds 4 ASCII chars in bytes 0..3. | `Timer3Timer` 0x5c1e52 | Observed |
| `0x14` | 1 | Sent on application close (inferred: release / power-down) | `FormClose` 0x5a8438 | Observed (meaning Inferred) |

Quirk: if `Reset_500` is called with an unlisted (dl, cl) pair it re-sends whatever byte is already in the
TX buffer at 0x187d838. A new driver should never do this.

`Reset_500` and `asetus_1` only transmit when `deviceOpen && !demoMode` (0x5c6c2d, 0x5c6cb2). Observed.

### 3.2 `0x0E` SET CONFIGURATION, 10 bytes plus zero padding

Built in `Set500` (0x5c6894-0x5c6c13) into the global array at 0x187d7e0. `asetus_1` then prefixes the
3-byte header and sends 10 bytes (0x5c6cc3-0x5c6cfa). Observed.

| Payload byte | Value | Meaning | Status |
|---|---|---|---|
| 0 | `0x0E` | command | Observed |
| 1 | `0x80` | constant (inferred: sub-command / address) | Observed |
| 2 | `0x07` | constant: number of config bytes that follow | Observed (meaning Inferred) |
| 3 | `CH1CFG` | CH1 gain code (Table A) OR `0x04` if CH1 coupling = AC | Observed (0x5c6b0d-0x5c6bb5) |
| 4 | `CH2CFG` | CH2 gain code (Table A) OR `0x01` if CH2 coupling = DC, OR `0x10` if CH2 = GND | Observed (0x5c6a86-0x5c6ba9) |
| 5 | `CH1OFS` | CH1 vertical offset DAC (section 5.3) | Observed (`efb298`) |
| 6 | `CH2OFS` | CH2 vertical offset DAC | Observed (`efb299`) |
| 7 | `TRIG` | `255 - TrackBar3.Position`. The trackbar runs 0..255 with default 128, and top is 0. | Observed (0x5c6bcc, 0x5c82b0) |
| 8 | `TB` | Hardware timebase code (Table B) | Observed (`efb295`/`efb296`) |
| 9 | `FLAGS` | Bit field (Table C) | Observed (0x5c6bdf-0x5c6c13) |
| 10..63 | `0x00` | padding | Observed |

The packet is resent from `asetus` (0x5a8f70) whenever any control changes. This covers V/div, coupling,
timebase, trigger, Y-position, Run and Single.

#### Table A: volts/div code -> gain bits (Observed: jump tables at 0x5c6aa4/0x5c6ac3 for CH2, 0x5c6b2b/0x5c6b4a for CH1)

The internal V/div code (`efb27c` for CH1, `efb27d` for CH2) is set by the V/div buttons, for example
`SpeedButton015v1Click` 0x5be98c.

| Internal code | Label, x1 probe (CH1) | Full scale, mV (DSOLink export) | CH1 byte 3 gain bits | CH2 byte 4 gain bits |
|---|---|---|---|---|
| 0x1E | 0.1 V/div | 800 | 0x08 | 0x22 |
| 0x0D | 0.2 V/div | 1600 | 0x00 | 0x02 |
| 0x1D | 0.5 V/div | 4000 | 0x01 | 0x24 |
| 0x0B | 1 V/div | 8000 | 0x0A | 0x04 |
| 0x1B | 2 V/div | 16000 | 0x02 | 0x28 |
| 0x01 | 5 V/div | 40000 | 0x03 | 0x08 |

- Full scale = 8 divisions (Observed, from the DSOLink export table at 0x5d9f9f/0x5d9fbe: 0.1 V/div -> 800 mV).
- CH2 labels in the x1 mode read 10 mV, 30 mV, 0.1 V, 0.3 V, 1 V, 3 V. The labels with a trailing space are
  0.1/0.3/1/3/10/30 V; they are selected by flags 0xefb46f/0xefb46e, which are probably probe x10 or legacy
  PCSU200 settings. These CH2 labels look like legacy PCSU200 code. The EDU09 is single-channel (section 4.3).
  Status: Observed (strings), meaning Inferred.
- Other codes leave the register unchanged, which uses the previous value. Observed.

#### Table B: timebase (Observed)

Button handler -> `efb27e` (internal code) -> `Set500` switch (0x5c68a0-0x5c6a07) -> `efb295` (hardware
code, byte 8). The sample rate comes from the DSOLink export switch in `SaveShareData` (0x5da096-0x5da1d1).
`efb2a0` is time/div in units of 10 ns. `efb216` is the expected "mode" byte in the completion status
(section 4.1).

| Time/div | Internal code | **HW code (byte 8)** | Exported sample rate (Hz) | Status mode `mm` | Notes |
|---|---|---|---|---|---|
| 500 ms | 0x05 | 0x01 | 62 | 1 | |
| 200 ms | 0x02 | 0x02 | 156 | 1 | |
| 100 ms | 0x5D | 0x03 | 312 | 1 | |
| 50 ms | 0x40 | 0x04 | 625 | 1 | |
| 20 ms | 0x41 | 0x05 | 1 562 | 1 | |
| 10 ms | 0x42 | 0x06 | 3 125 | 1 | |
| 5 ms | 0x50 | 0x07 | 6 250 | 1 | |
| 2 ms | 0x51 | 0x08 | 15 625 | 1 | Default at start (`SpeedButton2msClick`) |
| 1 ms | 0x52 | 0x09 | 31 250 | 1 | |
| 0.5 ms | 0x58 | 0x0A | 62 500 | 1 | |
| 0.2 ms | 0x59 | 0x0B | 156 250 | 1 | |
| 0.1 ms | 0x5A | 0x0B | 156 250 | 1 | Same HW setting as 0.2 ms, 2x software zoom |
| 50 us | 0x5C | 0x0C | 625 000 (effective) | **2** | ETS, 8 interleaved sub-records |
| 20 us | 0x55 | 0x0D | 1 562 500 (effective) | **3** | ETS, 16 interleaved sub-records |
| 10 us | 0x5E | 0x10 | 1 562 500 (effective) | **4** | ETS, 16 sub-records, 2x software zoom |
| <=5 us (hidden) | 0x20 | 0x0B | 5 000 000 (export) | 1 | Legacy PCSU200 buttons, probably hidden in EDU09 (Inferred) |
| (unused) | 0x98 / 0x99 | 0x0F / 0x0E | - | - | No setter found |

Rate check (Inferred): every rate is 31.25 samples/div / (time/div). 1024 samples therefore cover about
32.8 divisions; the screen shows a window of them, positioned with the X scrollbar. The help says ETS is
used on 50/20/10 us/div with trigger forced on and no pre-trigger (`controls.htm`).

#### Table C: FLAGS byte (payload byte 9)

| Bit | Source | Meaning | Status |
|---|---|---|---|
| 0 | `efb287` = 1 when trigger source = CH2 (`efb281`=0), 0 when CH1 (`efb281`=0x10). For Ext (`efb281`=1) the previous value is kept. | Trigger source | Observed; the Ch1/Ch2/Ext buttons are `Visible=False` in the EDU09 DFM, so in practice it is always CH1 (0) |
| 1 | `efb288` = 1 when Trigger ON (`efb285`=0), 0 when OFF (`efb285`=0x40) | Trigger enable (off = free run) | Observed |
| 2 | `efb289` = 0 when `efb282`=0x20 (SpeedButton5, default), 1 when `efb282`=0 (SpeedButton6) | Trigger edge. Inferred: 0 = rising "Up", 1 = falling "Dn". | Observed bit / Inferred polarity |
| 3 | `efb473` | "Digital level grabber" mode (GrSpeedButton15, caption `10110010`) | Observed |
| 4-5, 6-7 | Uninitialised Delphi locals in `Set500` (`[esp]`, `[esp+1]` from `push ecx`) | Garbage. **Send 0.** | Observed |

#### Coupling (Observed)

| Control | Effect on the wire |
|---|---|
| CH1 AC (`SpeedButton_ac1Click` 0x5c6d48) | byte 3 \|= 0x04 |
| CH1 DC | byte 3 bit 2 = 0 |
| CH1 GND | **No hardware bit.** `efb28f`=0, and `LueData_500` replaces every CH1 sample with the frame mean (0x56eac9-0x56eb18). |
| CH2 AC / DC / GND | byte 4: DC sets 0x01; GND sets 0x10 and also gets the software mean fill (0x56eb1a) |

---

## 4. Input (device -> host) reports

`USBRead(Self, dst)` at 0x5a82b0 reads one 65-byte report and copies the 64 payload bytes to `dst`.
Observed. All IN reports are 64 bytes. There is no checksum or terminator anywhere: framing comes only from
the command state machine. Observed (no validation code exists).

### 4.1 Status report (while armed)

> **HW-verified (section 11)**: the device streams these by itself after `0x0B`. Waiting = 64 zero
> bytes every ~33 ms (no `0x64` marker); complete = `64 C8 mm` + 61 zero bytes.

| Byte | Value | Meaning | Status |
|---|---|---|---|
| 0 | `0x64` | Status marker (decimal 100) | Observed (0x5db877) |
| 1 | `0xC8` = capture complete. Any other value means "still waiting" (Inferred). | Completion flag | Observed |
| 2 | `mm` | Acquisition mode. Must equal the expected mode `efb216`: 1 = real-time, 2 = ETS x8 (50 us), 3 = ETS x16 (20 us), 4 = ETS x16 (10 us). | Observed (0x5a98fa-0x5a991e, 0x5db88e) |
| 3..63 | ? | The recorder reads byte 15 of reports received after `0x0B` as a live sample (`255 - b[15]`, 0x57e8cd). **HW (H1/H2): always zero** on firmware 1.01 (also in the `0x0D` stream), so no live data. | Observed usage; HW: zero |

The single-shot loop in `FastTimer2Timer` (0x5db1e9) checks only bytes 0 and 1.

### 4.2 Data frame (after `0x0A`)

Observed at 0x5db8d1-0x5db912 (Run) and at 0x5ba5ec / 0x5db22e (Single):

```
repeat r = read_report() until r[0] != 0x64      # discard leftover status reports
frame[0:64] = r                                  # first data report
for i in 1..15: frame[64*i : 64*i+64] = read_report()
# frame = 1024 bytes, unsigned 8-bit ADC samples, oldest first (Inferred)
```

- The frame is 16 reports x 64 bytes = **1024 samples**.
- Quirk: if the first data byte happens to be 0x64 (ADC value 100), the original program discards it as a
  status report and the frame shifts. A robust driver should instead count reports after `0x0A`.
- After the frame the host immediately sends `0x09` and then `0x0B` to re-arm.

### 4.3 Sample layout and post-processing (`LueData_500`, 0x56e738)

1. **ETS de-interleave** (Observed):
   - Mode 2 (50 us): `out[8*i + k] = in[i + 128*k]` for i in 0..127, k in 0..7.
   - Modes 3/4 (20/10 us): `out[16*i + k] = in[i + 64*k]` for i in 0..63, k in 0..15.
   - Mode 1: no reordering.
2. **Upsample x4 and smooth** (Observed, used for display only; skipped in spectrum mode `efb42b`):
   - Each sample is repeated 4 times, giving 4096 points.
   - A `[1,2,1]/4` FIR is applied and the result clamped to 0..255.
3. **Channels**: the 4096 points go to the even bytes (CH1). The odd bytes (CH2) are set to **0**. The EDU09
   firmware path is **single-channel**; the CH2 code is a PCSU200 leftover. Observed (0x56ea74-0x56ea9c).
4. The trigger point is at sample index **258** of the 1024 (help `dataacquisitiontootherappl.htm` for
   DSOLink). Observed in the docs; not yet located in code.

A new driver should expose the 1024 raw (de-interleaved) samples and leave interpolation to the UI.

### 4.4 Firmware version reply

After `0x0F`, read 1 report. The version string is `chr(b[0]) + chr(b[1]) + chr(b[2]) + chr(b[3])`, shown as
"Firmware version XXXX" (0x5c1e7f-0x5c1ee5). Observed.

### 4.5 Live-level reply (calibration)

`0x09, 0x0B, 0x0D`, then read 1 report. `ReadBytes` returns `255 - mean(b[0..63])` and `ReadBytes2` returns
`sum(b[0..63])`. Observed (static).

**Hardware (H2, section 11 findings 20-21): on firmware 1.01 this reply is not live data.** `0x0D` while armed
switches the device to a fast status stream (one all-zero waiting report or `64 C8 mm` per ~1 ms, bytes 3..63
always zero) until `0x08`; `0x0D` while idle gets no reply. The original's `255 - mean(report)` would
therefore read status reports. Our driver never sends `0x0D`: `Scope.live_level()` /
`DeviceIO.live_samples()` capture free-run frames instead (trigger off, current V/div and offset, 200 us/div
unless a realtime timebase is given) and average the raw frame samples.

---

## 5. Converting samples to volts and time

### 5.1 Time

- `t[n] = (n - 258) / Fs` seconds relative to the trigger, with `Fs` from Table B. Fs is Observed; the
  formula is Inferred.
- ETS rows give the effective, post-de-interleave rate.

### 5.2 Voltage

DSOLink exports `[full_scale_mV, gnd_counts, samples]` (section 7). The natural conversion (**Inferred**; it
matches 8 div x 32 counts/div) is:

```
V = (sample - gnd_counts) * full_scale_mV / 256 / 1000        # volts
counts per division = 32
```

`full_scale_mV` = 8 x V/div (Table A). Polarity (whether a larger raw count means a more positive voltage) is
**Unknown**. The GUI works in an inverted "screen" domain (`255 - raw`), and the exported GND is
`255 - efb228` (0x5da1e8). Verify on hardware.

Hardware (section 11.3): the real gain is a few per cent higher than 32 counts/div and differs per range
and per side of ground; the driver corrects it per range (calibration v2, `units.deflection_to_volts`). With
`dev` = counts relative to ground in the voltage sense (`(raw - gnd) * s`, `s = -1` for an inverted ADC):

```
dev >= 0:  V = dev * V/div * g+ / 32                       # g+ = range_gain (positive side)
dev <  0:  r = -dev * g- / 32                              # g- = range_gain_neg (falls back to g+)
           V = -(r - E(r)) * V/div                         # E: negative nonlinearity knots (11.3)
```

`E` is piecewise linear through the per-range knots `(r_k, e_k)` (`neg_nonlinearity`), 0 for `r <= r_0` and
extrapolated with the last slope; the inverse (volts -> counts, trigger byte) is exact. The position ->
offset DAC mapping keeps the positive effective `32 / g+` counts per division.

### 5.3 Offset DAC and ground level

- **Offset DAC byte** (Observed, `Set_DACs` 0x5c6d04, `TrackBar1Change` 0x5c8300):
  `CH1OFS = (base_ofs + range_corr + tb_corr + ypos) & 0xFF`
  - `base_ofs` = `efb368`. Default 0x80; set by calibration and persisted in `EDU09.ini`.
  - `range_corr` = `efb370`. Per V/div correction: 0, or the calibrated `efb378` for 0.1 V and 1 V/div.
  - `tb_corr` = `efb390`. 0 for all observed timebases.
  - `ypos` = `TrackBar1.Position`, -128..127. It is clamped so the sum stays within 0..255.
- **Ground level in counts** (Observed formula, `y1_viiva` 0x5c8de4). Calibration stores 3 points
  (DAC_i, AD_i), i = 0..2, at INI keys `Ch1_pos0..2` / `Ch1_ad0..2` (globals efb2fc/efb2f8, efb304/efb300,
  efb30c/efb308):
  ```
  k = ((AD2-AD0)/(P2-P0) + (AD2-AD1)/(P2-P1) + (AD1-AD0)/(P1-P0)) / 3
  b = ((AD0 - k*P0) + (AD1 - k*P1) + (AD2 - k*P2)) / 3
  gnd_screen = round(k * (base_ofs + ypos) + b)          # efb228
  gnd_counts (raw) = 255 - gnd_screen                    # as exported to DSOLink
  ```
  The AD values are measured with `ReadBytes`, which returns `255 - mean`. In demo mode
  `gnd_screen = 2*ypos + 128` (slope 2 ADC counts per DAC step). **That is the original's demo mode only:
  real hardware measures k ~1.03, b ~-9.8, base_ofs 0x86** (HW-verified 2026-10-24, section 11 finding 24), so
  one division of position (32 counts) is ~31 DAC steps, not 16. The driver's `Calibration()` keeps the demo
  model (simulator/test fixtures); hardware without a calibration file uses `Calibration.typical()`.
- **Settling** (HW-verified, section 11 finding 25): after a config that changed the offset byte the front end
  needs ~10-20 ms to settle; the driver waits `tables.OFFSET_DAC_SETTLE_S` = 20 ms before arming.

### 5.4 Calibration procedure (`calibrate` 0x56f96c, Inferred outline from observed calls)

The input must be disconnected, and the application asks for this. The procedure:
1. Selects 2 V/div, trigger off, DC.
2. Sends `0x09, 0x0B, 0x0D` and averages 100 `ReadBytes` results. (On firmware 1.01 these are status
   reports, see 4.5; our calibration (T9) uses free-run frame means from `Scope.live_level()` instead.)
3. Steps `base_ofs` by +/-1 until the average equals 128 (at most 50 rounds). The result must lie in
   0x5A < ofs < 0xA6, otherwise the message is "CALIBRATION FAILED".
4. Repeats at 1 V/div (range correction) and at three Y positions (`CH1_DAC0/1/2` -> `Ch1_pos*`/`Ch1_ad*`).
5. Logs to `Calibration.log` and saves to `%APPDATA%...\EDU09.ini`, section `[Calibration]`.

---

## 6. State machine, sequencing and timers

### 6.1 Start-up (Observed: `Timer3Timer` 0x5c0fd8, 200 ms one-shot style)

1. Open the device (section 1). On failure, offer demo mode.
2. If the INI marks the device as uncalibrated, run calibration.
3. Send `0x0F` and read 1 report to get the firmware version.
4. Select 2 V/div on CH1/CH2 and 2 ms/div. Each selection triggers `asetus`, which sends a `0x0E` config.

### 6.2 Continuous run (Run button, `FastTimer1` TTimer, Interval = 10 ms; Observed in DFM and 0x5db5e0)

```
on Run:     send 0x09 ; send 0x0B                      # (state 0x187d828 != 2/3)
every 10ms tick:
  if waitingForTrigger:
      r = read_report()                                # blocking
      if r[0]==0x64 and r[1]==0xC8 and r[2]==expected_mode:
          send 0x0A ; waitingForTrigger = false
  if not waitingForTrigger:
      read frame (4.2) ; process (4.3) ; publish to DSOLink (7)
      send 0x09 ; send 0x0B ; waitingForTrigger = true
on Stop:    send 0x08                                  # (Reset_500(4,0) at 0x5bac50)
on any setting change: send 0x0E config (and asetus re-arms with 0x09,0x0B when Run/Single is down, 0x5a9943)
on exit:    send 0x14
```

### 6.3 Single shot

`SpeedButtonSingleClick` and `SingleClick` (0x5ba37c) send the same `0x09`, `0x0B`, then poll for the status
(0x5ba595) and read out after `0x0A`, once. `FastTimer2Timer` (10 ms) does the polling and checks only
`64 C8`.

### 6.4 Timing notes

- Timer resolution is Windows `TTimer` (about 10-16 ms). There is no explicit timeout or retry. A missing
  report blocks the GUI thread. Observed (synchronous `ReadFile`).
- With no trigger, the host keeps polling forever. The "Waiting for trigger" text appears after more than
  2 ticks (0x5db7f3). Observed.

---

## 7. DSOLink.dll shared memory (brief)

Observed: EDU09.exe 0x5e1060; DSOLink.dll exports at RVA 0x772c, 0x779c and 0x77d4.

- The mapping is created by EDU09.exe with `CreateFileMappingA(INVALID_HANDLE_VALUE, NULL, PAGE_READWRITE,
  0, 40000, "ShareDSOData")` and is 40 000 bytes (10 000 x int32). It exists only in **Expert settings**
  (`ExpertSettings1Click`).
- Layout, as int32 indices:

| Index | Content |
|---|---|
| 0 | CH2 sample rate, Hz |
| 1 | CH2 full scale, mV |
| 2 | CH2 GND, counts (`255 - efb22c`) |
| 3..1026 | CH2 data: 1024 values, every 4th point of the interpolated array |
| 1027..4098 | 0 |
| 4099 | CH1 sample rate, Hz |
| 4100 | CH1 full scale, mV |
| 4101 | CH1 GND, counts (`255 - efb228`) |
| 4102..5125 | CH1 data, 1024 values |
| 5126..8197 | 0 |
| 8198 | DataReady flag (1 = new data) |

- `ReadCh1(buf)` copies int32 [4099..8197] (0x1003 dwords) and clears the flag. `ReadCh2` copies [0..4098].
  `DataReady()` returns `flag == 1`.

This can be reproduced in the new app for backward compatibility. It is not part of the device protocol.

---

## 8. Specs from the help files (Observed: `EDU09DSO.chm`)

- Time/div from 10 us to 500 ms (code). ETS on 50/20/10 us/div; ETS needs repetitive signals and has no
  pre-trigger.
- Trigger: On/Off (free run), Level, Edge Up/Dn, and "Find" (re-centre the X reference).
- Run / Single. History of up to 1000 acquisitions (software only).
- Coupling: AC / DC (GND in software).
- Autoset range is 100 mV-30 V, above 50 Hz, with duty cycle above 10%.
- Noise Reject trigger option applies to real-time sampling only (software).

---

## 9. Open questions (need hardware or a capture)

1. **Answered (section 11)**: nothing while idle; after `0x0B` the device streams a 64-zero-byte waiting
   report every ~33 ms and `64 C8 mm` on completion. (Was: does the device stream IN reports
   continuously while idle or armed, or only once per command?)
2. **Answered (section 11, H1/H2)**: byte 1 is only ever `C8`; bytes 3..63 are always zero; no live samples
   anywhere (`0x0D` is a fast status stream). (Was: what do status bytes 1 and 3..63 contain? The recorder
   uses byte 15.)
3. **Answered (section 11)**: higher raw = more positive input; trigger byte is not inverted. GND count at
   `CH1OFS = 0x80`: ~132 (1 V/div open input, 2 V/div square low level), model says 127; per-unit
   calibration. (Was: ADC polarity and GND count?)
4. **Answered (section 11)**: not 258; per mode 255 / 248 / 253 / 253 (+-1). (Was: is the trigger
   position index 258 for every timebase, and how does ETS change it?)
5. **Partly answered (section 11)**: bit 2 set = falling edge. Bit 3 (digital mode) still unknown.
6. Do `0x08` (stop) and `0x14` (exit) produce a reply? What does `0x0C` do?
7. What is the device's behaviour if `0x0A` is sent before `64 C8`? Is it safe to re-send `0x0E` while armed?
8. **Answered (section 11)**: `"1.01"` + CR, then garbage. (Was: are bytes 0..3 ASCII digits?)
9. Effect of the CH2 bytes (4, 6) on single-channel EDU09 hardware: ignored, or used?
10. **Answered for 200 us, 50 us, 20 us, 10 us/div (section 11)**: Table B rates are right (10 us/div =
    1.5625 MHz, like 20 us). Other timebases follow the same 31.25 samples/div rule but are untested.

## 10. Suggested capture plan

1. **Windows + Wireshark/USBPcap** (best). Install USBPcap, capture the root hub the EDU09 is on, and run
   EDU09.exe. Filter with `usb.idVendor == 0x10cf || usb.device_address == N`, then look at `usb.capdata` on
   the interrupt endpoints 0x01 and 0x81. Script these sessions:
   a) start-up (version `0x0F`, configs); b) Run at 2 ms/div with the input open; c) the same with a known
   1 kHz square wave (the scope's own test signal, if the kit has one); d) step every V/div and timebase;
   e) Trigger on/off, edge up/down, level min/mid/max; f) AC/DC/GND; g) Single; h) Stop; i) the
   Options -> Calibrate run; j) close the program (`0x14`).
2. **Linux + usbmon/Wireshark** to check the new driver: `sudo modprobe usbmon`, then capture `usbmonN` for
   the device's bus. The same filters apply. Alternatively run EDU09.exe in a Windows VM with USB
   pass-through and capture on the Linux host's usbmon.
3. **Direct probing on Linux** (low risk): open `/dev/hidrawN` with hidapi and do the following.
   - Send `00 0F` and read with a timeout to get the version.
   - Send `00 0E 80 07 0A 04 80 80 80 08 02` and zero-pad to 65 bytes. This sets CH1 1 V/div DC,
     offset 0x80, trigger level mid (0x80; EDU09.exe's own default for 0 V is 0x7F = 255 - 128), 2 ms/div, trigger on and rising.
   - Send `00 09`, then `00 0B`, and read status reports until `64 C8 01`.
   - Send `00 0A` and read 16 reports.
   - Log every raw report with a timestamp to answer questions 1-2.
4. Feed captures into the REA `inspect_pcap_capture` / `inspect_packet_capture` tools for structured diffing
   against this spec.

---

## 11. Hardware-verified (H1, 2026-10-24)

Probed on a real EDU09 (firmware `1.01`, Linux hidraw) with `edu09-probe`. The raw captures are committed as
test fixtures in `tests/fixtures/h1/` (device bytes only) and replayed by `tests/protocol/test_h1_fixtures.py`.
Where this section disagrees with sections 0-10, this section wins.

| Fixture | Session |
|---|---|
| `listen_idle.jsonl` | `listen` for 5 s, nothing sent |
| `version.jsonl` | `0x0F` |
| `config_noreply.jsonl` | `0x0E` `80 07 0A 04 80 80 80 08 02` |
| `freerun_2ms.jsonl` | `acquire`, 1 V/div DC, 2 ms/div, free run (flags `00`), input open |
| `freerun_500us.jsonl` | same at 500 us/div |
| `trigger_timeout_2ms.jsonl` | 2 ms/div, trigger on (flags `02`), level `0x80`, input open -> never triggers; `0x08` after 3 s |
| `stop_idle.jsonl` | `0x08` while idle |

Findings (part 1: protocol behaviour, input open):

1. **Idle**: the device sends nothing unsolicited (5 s listens before and after the other tests).
2. **Version** (`0x0F`): reply starts `31 2E 30 31 0D` = `"1.01"` + CR; bytes 5..63 are garbage. `parse_version`
   takes the printable run up to the CR (fallback when unterminated: bytes 0..3). Answers Q8.
3. **Config** (`0x0E`): no reply.
4. **Status streaming (answers Q1)**: after `0x0B` the device streams status reports **by itself**; the host
   never prompts (`tables.STATUS_POLL = "passive"`).
   - While waiting for a trigger it sends a **waiting report = 64 zero bytes** (not `64 00 mm`) every
     **~33 ms** (~30 Hz; first one 33 ms after `0x0B`; mean spacing 33.0 ms over 3 s) -
     `tables.WAITING_REPORT`, `tables.STATUS_INTERVAL_S`, `reports.is_waiting_report`.
   - **Complete** = `64 C8 mm` followed by 61 zero bytes (mode 1 at 2 ms and 500 us/div). Sent as soon as the
     capture is done. Bytes 3..63 are zero, so they do not carry live samples (Q2: status bytes 3..63 = 0;
     byte 1 is only ever `C8`, the waiting report has no marker at all).
   - `parse_status` still returns `None` for the all-zero report (byte 0 is not `0x64`); the waiting report is
     recognised only by `is_waiting_report` and only before `0x0A`, because a data report of 64 raw zeros
     looks identical (R1: `FrameAssembler` never skips it).
5. **Free run** (flags `00`) completes about one frame time after the arm: 67.5 ms at 2 ms/div (frame
   1024 / 15625 Hz = 65.5 ms), 18.1 ms at 500 us/div (16.4 ms). No waiting reports were sent during the 2 ms/div
   free-run capture (the 33 ms tick did not produce one), so waiting reports are presumably tied to the
   trigger wait (Inferred; the simulator models it so).
6. **Read-out** (`0x0A`): `64 C8 01` leading report(s), then 16 frame reports ~1 ms apart (plus one
   trailing report, see finding 18). Leading
   count: 2 at 2 ms/div, 1 at 500 us/div (one `64 C8 01` was already read before `0x0A` in both). The count
   probably depends on the time between the completion report and `0x0A` (Inferred), so hosts must skip any
   number of exact `64 C8 mm` reports (`FrameAssembler` does, up to 64). Simulator default:
   `tables.READOUT_LEADING_STATUS = 2`.
7. **Stop** (`0x08`): idle -> no reply. Armed/waiting -> one more all-zero report (22 ms after the `0x08`,
   which is on the 33 ms tick grid: it may be the last streamed report rather than a reply), then silence.
   Partly answers Q6.
8. **Open input, 1 V/div DC, offset DAC `0x80`**: raw samples 131..135, mean 132.6 at both timebases. The
   uncalibrated model (`Calibration` defaults, `GndFit(k=2, b=-128)`) predicts 127. Recorded as observed only;
   the default calibration is unchanged (a per-unit calibration absorbs this offset).

Findings (part 2: signal-verified). Setup: the user's 555 square wave, ~30 kHz, 0..4.16 V, ~63 % high, on
CH1, DC, 2 V/div, offset `0x80`; trigger byte 165 (~2.1 V), rising unless noted. Replayed and asserted by
`tests/protocol/test_h1_signal_fixtures.py`.

| Fixture | Setting | Acquisitions |
|---|---|---|
| `square_freerun_200us_2v.jsonl` | 200 us/div, free run | 1 |
| `square_trig_200us_rise.jsonl` / `square_trig_200us_fall.jsonl` | 200 us/div, trigger rising / falling (flags `02` / `06`) | 3 / 3 |
| `square_trig_50us_rise.jsonl` | 50 us/div (ETS mode 2) | 2 |
| `square_trig_20us_rise.jsonl` | 20 us/div (ETS mode 3) | 2 |
| `square_trig_10us_rise.jsonl` | 10 us/div (ETS mode 4) | 2 |

9. **Sample rates (Q10)**: 200 us/div (156.25 kHz) measures 29.97-30.05 kHz. ETS: `protocol.ets.deinterleave`
   gives clean waveforms for mode 2 (50 us, 625 kHz), 3 (20 us) and 4 (10 us); all ~29.9 kHz. The 10 us/div
   sample spacing equals 20 us/div (1.5625 MHz): Table B is right; the 10 us "zoom" is display-only.
10. **Levels / ADC polarity (Q3)**: raw ~132 at 0 V and ~197 at 4.16 V = 65 counts (66.6 predicted by 32
    counts/div at 2 V/div). Higher voltage -> higher raw: `ADC_INVERTED = False` is correct.
11. **Trigger level byte (Q3/Q5)**: byte 165 (~2.1 V) triggers mid-edge and frames are phase-stable, so the byte
    is in the raw-sample domain (`TRIGGER_BYTE_INVERTED = False`).
12. **Edge (Q5)**: `FLAG_EDGE_FALLING` (`0x04`) selects the falling edge. Bit 3 (digital) is still untested.
13. **Status mode byte**: `64 C8 mm` carries the Table B mode for modes 1, 2, 3, 4.
14. **Trigger position (answers Q4)**: index of the first sample past the level on the triggering edge, in the
    time-ordered (de-interleaved) frame, is **not 258**: realtime 255 (200 us/div; only ~5 samples per period,
    so +-1), mode 2: 248 (247..248), mode 3: 253, mode 4: 253 (orchestrator's reading 252). ->
    `tables.TRIGGER_INDEX_BY_MODE`, `units.trigger_index(tb)`; `tables.TRIGGER_INDEX` is now the realtime 255.
15. **Completion time**: triggered captures complete 7-9 ms after `0x0B` at 200 us/div, 16-19 ms in ETS
    (one 50 us/div capture took 44 ms); the 200 us/div free run 8 ms (frame 6.6 ms).
16. **ETS waiting cadence**: while an ETS capture is in progress the device sends the all-zero waiting report
    every ~1-4 ms (mean ~2 ms, first ~1-2 ms after `0x0B`), not every 33 ms (`tables.STATUS_INTERVAL_ETS_S`).
    At 200 us/div no waiting report arrived before completion (8 ms < 33 ms).
17. **Leading status count** after `0x0A` was 1 in most acquisitions, 2 and 3 once each (the 3 came 0.5 ms
    apart after a 1.5 ms delay before `0x0A`): it depends on host timing. Skip any number of exact `64 C8 mm`.
18. **Trailing report**: every read-out sends **one extra data-like report after the 16 frame reports**
    (~1 ms after the 16th). The probe stopped reading after 16, so it showed up as the first report after the
    *next* `0x0B` in every multi-acquisition capture. It is not part of the frame (the frames' trigger
    positions and signal are consistent with the first 16 reports), and in all three ETS captures it was
    byte-identical (stale memory). Hosts must drain it after the frame (or flush before the next arm) and must
    not treat it as a status (`tables.READOUT_TRAILING_REPORTS = 1`). Because of it, a lost frame report
    cannot be detected by counting reports alone.
19. **ETS frames contain a few out-of-place samples** (isolated spikes of 1-3 samples,
    several per frame) which upset edge-based measurements; a 5-point median filter removes them (the tests do
    this for ETS only). Cause unknown (interleave jitter?).

### 11.1 H2 (2026-10-24): Scope smoke test and `0x0D`

Driver-level smoke test of `Scope` on the real device (firmware 1.01), same 555 square-wave setup as part 2.
Captures: `captures/h2-scope.jsonl` (the whole session, not committed as a fixture) and the `0x0D` probe
session, committed as `tests/fixtures/h1/h2_live_0d.jsonl` and asserted by `tests/protocol/test_h2_fixtures.py`.

20. **`0x0D` returns no live samples.** After `0x09, 0x0B, 0x0D` the device switches to a **fast status
    stream**: one report per ~1 ms (`tables.FAST_STATUS_INTERVAL_S`), the all-zero waiting report while the
    capture is in progress and `64 C8 01` (repeated every ms) once it is complete, until `0x08`, which yields
    one final report. A second `0x0D` changes nothing. `0x0D` while idle (after `0x09` only): no reply. Bytes
    3..63 are always zero: never ADC data. (The session's first `0x0D` was preceded by three stale `64 C8 01`.)
21. **The old `Scope.live_level()` hung**: the worker sent `0x0D` and its reply loop skipped every streamed
    status report without checking its deadline, so it never timed out (the reports kept coming); the caller
    got `TimeoutError` after 20 s and `close()` logged "acquisition worker did not stop within 5.0 s". Fixed:
    the driver never sends `0x0D` (live levels come from free-run frames, section 4.5) and every worker wait
    loop is bounded by a deadline on the worker clock **and** independently of the clock (summed read
    timeouts, maximum report count); regression tests stream status reports forever
    (`tests/device/test_h2_streaming.py`).
22. **Post-stop report**: after `0x08` while armed the one final report (all-zero, or `64 C8 01` if the capture
    had completed) arrives **0.8-8.2 ms** after the stop (`tables.POST_STOP_REPORT_DELAY_S`). The worker's
    post-stop wait (one status tick + 10 ms, ~43 ms) covers it with margin. This refines finding 7.
23. **Everything else in the Scope smoke test worked**: NORMAL trigger rising/falling at 200 us/div; ETS
    50/20/10 us/div; AUTO fallback free-run (no zero blocks in the frames); continuous run at ~28 frames/s with
    no zero blocks; stop -> acquire clean; per-mode trigger index (`TRIGGER_INDEX_BY_MODE`) confirmed. ETS
    frames still show the isolated outlier samples of finding 19: `measure` frequency reads up to ~31.4 kHz
    instead of 29.9 kHz at 10 us/div. `analysis.despike()` (opt-in, median-of-3 on isolated one-sample
    outliers) restores ~29.9 kHz on the H1 ETS fixtures but corrupts undersampled realtime frames (200 us/div,
    ~5 samples per period), so it is not applied by default.

### 11.2 Calibration and offset settling (2026-10-24)

Driver calibration (`Scope.calibrate`, free-run frame means at 5 ms/div) on the real unit, input open.

24. **Calibration result** (3 consistent dry runs): `base_ofs = 0x86` (134), `range_corr = {1 V/div: -1,
    0.1 V/div: -1}`, ground fit **k ~1.03 ad counts per DAC step, b ~-9.8**. Afterwards 0 V reads 0 +-0.05 div
    at 2 ms/div on all ranges and positions. The DAC slope is about half the demo-mode 2 of section 5.3, so the
    uncalibrated model `GndFit(2, -128)` scales positions ~2x wrong on hardware. These values are recorded as
    `tables.TYPICAL_*` and used by `Calibration.typical()` (source `"typical"`), the fallback for hardware
    without a calibration file. (This refines finding 8: at offset 0x80 the open input reads ~132.)
25. **Offset DAC settling**: after a `0x0E` with a changed offset byte the first frame is wrong at its start by
    up to ~8 counts (1 V/div, 200 us/div: first 100 samples mean 118.7 vs settled 127; at position -3: 40.4 vs
    31). From the second frame (>= ~30 ms later) it is settled within noise; at 2 ms/div (65 ms frames) the
    effect is small but visible at the frame start. Settling time ~10-20 ms. The worker waits
    `tables.OFFSET_DAC_SETTLE_S` = 20 ms after such a config before `0x0B` (only when the offset or CH1
    gain/coupling byte changed, so continuous running keeps its frame rate). The simulator can model it
    (`SimulatedDevice.offset_settle_tau_s`, exponential, tau ~5 ms; off by default).
26. **Calibration noise check**: the very first reading right after `Scope.open()` once failed the noise check
    (6.0 counts rms vs ~0.7 normally), consistent with settling from the previous DAC state. Besides the
    settle wait, the calibration now retries a too-noisy first reading once after an extra 100 ms settle.

### 11.3 Vertical gain and negative nonlinearity (2026-10-25)

DC reference voltages on CH1, measured at the same time with a multimeter; 5 ms/div, trigger off, frame means
averaged; the offset/ground calibration of 11.2 checked accurate within ~1 count at all positions first.

27. **Per-range gain error**: the scope reads **too high by 0.4-2.6 %**, by a factor that depends on the range
    (positive levels 2.4-6 div above ground). Correction factor `g = V_true / V_read` on the development unit:

    | V/div | 0.1 | 0.2 | 0.5 | 1 | 2 | 5 |
    |---|---|---|---|---|---|---|
    | g | 0.987 | 0.975 | 0.974 | 0.996 | 0.981 | 0.979 |

    Equivalently the true sensitivity is `32 / g` counts/div (e.g. 0.5 V/div ~32.85). The driver stores these
    per unit in `Calibration.range_gain` (calibration.json key `range_gain`, missing = 1.0) and applies them in
    every counts <-> volts path, the trigger byte and the position -> offset DAC mapping. The table is
    `tables.TYPICAL_RANGE_GAIN`, used by `Calibration.typical()` (measured on one unit only). Measure your
    own with `edu09-gain` / `edu09.device.calibration.gain_from_reference` (docs/usage.md).
28. **Negative side** (sweep data: `docs/data/edu09_gain_sweep_unit1.json`): below ground the sensitivity is
    **0.5-1 % higher** than above ground (unit 1, 0.5 V/div: 33.0 counts/div vs 32.85), independent of the
    position. At **0.2 and 0.5 V/div** it **expands beyond ~4 div below ground** (0.5 V/div secant 33.0 up to
    4 div, 33.55 at 5 div, 34.73 at 6 div); at 0.1 and 1 V/div no expansion was seen in the measured span
    (to 5 resp. 3 div). The effect depends on the *deflection*, not on the ADC code or position.
    Calibration v2 models this per range:

    | Field (calibration.json) | Meaning |
    |---|---|
    | `range_gain` | `g+`, positive side (as before) |
    | `range_gain_neg` | `g-`, negative-side linear gain; missing = `g+` |
    | `neg_nonlinearity` | `{"0.5": [[r, e], ...]}`: knots of the excess `E` in the linear-corrected read domain `r = c * g- / 32` (`c` counts below ground); true deflection `d = r - E(r)` |
    | `meta` | provenance (tool, date, mode, sources); not part of `calibration_id` |

    Knot rules (`tables.NL_*`): 2-8 knots, `e_0 = 0`, `1 <= r_0`, `r` strictly increasing and `<= 10`,
    `|e| <= 2` div and every slope `de/dr` within +-0.5 (so the mapping is strictly monotonic). Unit 1
    (`transport.sim.UNIT1`, the simulator's `unit1` front end):

    | V/div | g+ | g- | knots (r, e) |
    |---|---|---|---|
    | 0.1 | 0.9870 | 0.9846 | - |
    | 0.2 | 0.9750 | 0.9668 | (4.0, 0) (5.069, 0.069) (6.26, 0.26)* |
    | 0.5 | 0.9741 | 0.9697 | (4.0, 0) (5.083, 0.083) (6.315, 0.315) |
    | 1 | 0.9960 | 0.9892 | - |
    | 2 | 0.9810 | 0.9786 | - |
    | 5 | 0.9789 | 0.9726* | - |

    (* extrapolated or assumed.) Files with any of the v2-only keys are written as `"version": 2`; offset-
    and `range_gain`-only files stay version 1 (readable by older builds). The calibration wizard
    (`edu09-calibrate`, docs/specs/calibration_wizard.md) measures and fits all of this per unit.

### 11.4 ETS trigger position varies between sessions (2026-10-25)

29. **ETS trigger index is not fixed**: with a 555 square wave (3.87 V supply, ~29.4 kHz, RV1 trimmed) the
    triggering edge in the de-interleaved ETS frame was consistently at the table index **+4** in all ETS
    modes (mode 2: 252 vs 248; modes 3/4: 257 vs 253), for both edges, at trigger levels 1.0/1.935/3.0 V, at
    1 and 2 V/div, with both the worker and the raw probe path. The H1 recordings of 2026-10-24
    (`tests/fixtures/h1/square_trig_*`, 4.4 V supply, 30 kHz) show 248/253. So the ETS position varies by a
    few samples between sessions/signals (cause unknown). Realtime timebases could not be checked (~5
    samples per period at 200 us/div).
    -> For **triggered ETS frames** the worker (`device.worker.detect_trigger_index`) searches the raw frame
    within `tables.TRIGGER_INDEX_BY_MODE[mode]` +- `ETS_TRIGGER_SEARCH` (8) samples for the first sample past
    the trigger level (the count of `units.trigger_byte`, edge direction flipped for an inverted ADC) on the
    configured edge, picks the crossing closest to the table index and sets `Waveform.trigger_index` to it;
    with no crossing (or GND coupling) the table value is kept. Realtime frames always carry the table
    value. The simulator models the shift with `SimulatedDevice(ets_trigger_offset=...)`.
