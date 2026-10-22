# Legion 5 16IRX9 keyboard backlight: research and calibration

Notes on the 4-zone RGB keyboard backlight of the Lenovo Legion 5 16IRX9: what
Linux sees, the protocol, how the light is produced, and how keyboard-konf
calibrates it.

Tested with BIOS NMCN31WW.

Tags: **[verified]** observed on the tested unit (a Legion 5 16IRX9 with the RGB
keyboard) or read in a primary source; **[source]** from the documentation or
code of other projects; **[assumption]** a conclusion not confirmed on the
hardware; **[not found]** searched for, not publicly available. Figures given
"on the tested unit" come from one laptop and may differ on others.

In short: the yellowish white and the wrong mixed colours have two separate
causes, no white balance and no gamma. keyboard-konf fixes both in software; no
existing utility, Lenovo Vantage included, does this. A third cause of
unevenness, the spread of the LEDs between keys, is in the hardware and cannot
be fixed.

---

## 1. Device

| | |
|---|---|
| Laptop | Lenovo Legion 5 16IRX9, product name `83DG` (2024) |
| Backlight controller | ITE 8295, USB `048d:c994` |
| PID classification | `c994` = "Regular/Slim Legion 2024" |
| Second ITE controller | ITE 8176, USB `048d:c992`: not the backlight (section 1.4) |

The backlight can be controlled only through USB HID; the kernel has no driver
for it (section 1.6).

The 8295 appears as two hidraw nodes, one per USB interface, both with VID:PID
`048d:c994`. The lighting node is the only one whose report descriptor declares
report `0xCC`. hidraw numbers are not guaranteed to stay the same across
reboots: find the device by VID:PID, then pick the node by the report it
declares. Never hardcode a `/dev/hidrawN` path.

### 1.1 Two ITE controllers on USB

| | `048d:c994` "ITE Device(8295)" | `048d:c992` "ITE Device(8176)" |
|---|---|---|
| Speed | Full Speed, 12 Mbit/s | Full Speed |
| Power (configuration) | **Self Powered**, Remote Wakeup, MaxPower 100 mA | Self Powered, 100 mA |
| Interfaces | 2 | 1 |

**[verified]** "Self Powered" means the chip is fed from a motherboard rail, not
from the USB bus; USB is only the data channel here.

### 1.2 The 8295, interface 0: the backlight

Class 3/1/1 (Boot Keyboard), one IN endpoint 0x81 of 64 bytes, 1 ms interval.
The report descriptor (179 bytes) declares:

| Collection | Report ID | Size | What it is |
|---|---|---|---|
| Vendor page 0xFF89, usage 0x10 | 0x5A | 16 bytes, feature | unknown; also present on the 8176 and on the per-key ITE 8258 |
| Vendor page 0xFF89, usage 0x07 | 0x07 | 191 bytes, feature | unknown; the same size as in the Legion 7 per-key protocol |
| Vendor page 0xFF89, usage 0xCC | **0xCC** | **32 bytes, feature** | **the backlight; the report keyboard-konf writes** |
| Generic Desktop / Keyboard | 0x01 | input | NKRO keyboard: 8 modifier bits, 12 key codes + a 224-bit map, 5 LED outputs |
| Consumer Control | 0x05 | 16-bit input | vendor usages 0xFF00–0xFF10; **[assumption]** Fn hotkeys for Lenovo Vantage |

The lighting report in the descriptor bytes: `85 CC ... 75 08 95 20 B1 00`
(Report ID 0xCC, Report Size 8, Report Count 32, Feature).

### 1.3 The 8295, interface 1: an unknown channel

Vendor page 0xFFC2, usage 4: a 64-byte feature report and a 64-byte output
report, **without a report ID**; endpoints IN 0x82 and OUT 0x03, 64 bytes each.
A separate two-way "raw" channel. Its purpose is unknown, **[assumption]**
service or firmware. On the sibling LOQ 2024, reading from it gives "broken
pipe". **Do not touch it** (section 9). The udev rule deliberately leaves this
interface closed to users.

### 1.4 The 8176: not the backlight

The same vendor feature report 0x5A (16 bytes); a keyboard (report 1, 6 key
codes + a 224-bit map); Consumer Control (report 2, usages 0x200–0x23C);
Generic Desktop "Wireless Radio Controls" (report 3, usage 0xC6, the
airplane-mode key). The kernel creates a "Keyboard" and a "Wireless Radio
Control" input device for it. There is no report 0xCC.

### 1.5 Where the keyboard really is

**[verified]** The main key matrix is on PS/2:
`i8042 → atkbd → "AT Translated Set 2 keyboard"`. Typing goes through the EC
over PS/2 (a repair forum names the EC as an IT5570E; not verified).
**[verified]** During 15 seconds of typing not a single input report arrived on
the lighting node: ordinary keys do not pass through it, and access to it does
not allow reading keystrokes.

### 1.6 What else there is, and what the kernel lacks

- `/sys/class/leds`: only `platform::fnlock` and the Caps/Num/Scroll
  indicators. The kernel knows nothing about the RGB backlight.
- WMI: GUID `8C5B9127-ECD4-4657-980F-851019F99CA5` has no driver
  (LenovoLegionLinux calls it `LENOVO_LIGHTING_METHOD`: on/off and brightness
  levels only). The backlight-profile-change event (Fn+Space) has its own GUID,
  `D320289E-8FEA-41E0-86F9-811D83151B5F`, also without a driver: a candidate
  for learning about state changes made outside the software.

---

## 2. Protocol

Lenovo publishes no documentation for this protocol; it comes from open
implementations (section 10). The Status column below shows what was verified
on the hardware.

### 2.1 Transport

The reference implementations send a HID Set_Report as a USB control transfer
of 32 bytes in total, with `0xCC` as the first byte:

```
bmRequestType=0x21, bRequest=0x09, wValue=0x03CC, wIndex=0x00
```

The report descriptor implies 33 bytes: the report ID plus 32 data bytes
(section 1.2).

**[verified]** hidraw with `ioctl(HIDIOCSFEATURE)` on the lighting node works;
libusb (pyusb) is not needed. The buffer is **33 bytes**: `[0xCC]` followed by
a 32-byte payload with `payload[0] = 0x16`. Written that way it changes the
colour; the 32-byte form was not needed.

### 2.2 Report layout

Bytes are numbered from the report ID, so the 32-byte form is bytes 0–31 and
the 33-byte hidraw buffer adds byte 32, which stays `0x00`.

| Byte | Field | Values | Status |
|---|---|---|---|
| 0–1 | Header | `0xCC`, `0x16` | [verified] |
| 2 | Effect | 1 static, 3 breath, 4 wave, 6 hue | 1 [verified]; 3, 4, 6 [source] |
| 3 | Speed | 1–4 | [source] |
| 4 | Brightness | 1–2 | [verified] (section 4) |
| 5–7 | Zone 1 RGB | 0x00–0xFF | [verified] |
| 8–10 | Zone 2 RGB | 0x00–0xFF | [verified] |
| 11–13 | Zone 3 RGB | 0x00–0xFF | [verified] |
| 14–16 | Zone 4 RGB | 0x00–0xFF | [verified] |
| 17 | Unused (padding) | `0x00` | |
| 18 | Wave right-to-left | 0/1 | [source], disputed (section 4) |
| 19 | Wave left-to-right | 0/1 | [source], disputed (section 4) |
| 20–31 | Unused | `0x00` | |

Zone 1 is the leftmost zone (section 5.1). So far only the static effect has
been verified on the hardware: breath, wave and hue, like any field not marked
[verified], should be checked with a small probe before anything is built on
them.

### 2.3 Open implementations

Reviewed: imShara/l5p-kbl, 4JX/L5P-Keyboard-RGB, OpenRGB, LenovoLegionToolkit
(LLT, which reimplements Vantage's traffic), LegionAura, Lenovo-Legion-Control,
loq-rgb (a hardware report for the LOQ 15IRX9, 2024), Aurora-Legion, maniac103,
LenovoLegionLinux.

- All of them send the same report, `CC 16 …`: 32 bytes over libusb, 33 over
  hidraw/hidapi.
- **None of them applies gamma or white balance.** A Lenovo forum thread
  (Legion 5 Pro, Vantage) confirms the result directly: white looks yellow and
  purple turns light pink.

---

## 3. Reading the state

**[verified]** The backlight state cannot be read back. `ioctl(HIDIOCGFEATURE)`
on report 0xCC succeeds, but returns the device identification, not the current
colours:

```
CC 03 FF 0A 00 8D 04 94 C9 00 03 00 ...
               ^^^^^ ^^^^^
               048D  C994   (VID:PID, little-endian)
```

Byte 1 is the EC profile slot (1..3, 4 = off); bytes 5–8 are the VID and PID.

- Software therefore has to keep the current state itself. After a reboot or a
  change made elsewhere (for example from Windows), its idea of the state is
  wrong until it writes its own again.
- Fn+Space changes the state in the firmware, bypassing any software, and
  raises a WMI event (section 1.6).
- There is no command to save a profile, and none to read the firmware version,
  for the 4-zone keyboards. Any new report, Fn+Space or a reboot replaces what
  was sent last.

---

## 4. Effects and brightness

- The controller has four built-in effects (byte 2): static, breath, wave and
  hue. Any other animation is a process that sends a new static frame for
  every frame.
- Software effects in other projects refresh at 20–30 fps; there are no reports
  of hangs caused by the rate. Several programs writing at once cause flicker.
- **[verified]** The PWM is linear: red at 255/16/8/2 gives the steps "bright,
  dim, faint, at the threshold", as a linear duty cycle predicts; with a curve
  in the firmware, zones 2–4 would be dark. So sRGB gamma in software is
  mandatory.
- **[verified]** There is practically no PWM floor: a value of 2 is still
  visible.
- **[verified]** The brightness byte works: 1 is slightly dimmer than 2, and the
  difference is small (on the LOQ 2024 it is reported as ignored; on the tested
  unit it is not).
- **[verified]** The controller accepts 3 and 4 in the brightness byte without
  an error, but lights exactly as at 2. Level 2 is the ceiling; there is no
  hardware headroom above it.
- **[verified]** "Off" as Vantage and LLT send it (`CC 16` followed by zeros:
  effect 0, brightness 0, black zones) turns the light off completely; the next
  report turns it back on.
- **Wave direction:** the table in section 2.2 labels byte 18 right-to-left, as
  imShara/l5p-kbl does. In LLT, OpenRGB and loq-rgb (checked on hardware) byte
  18 means "rightwards". The two contradict each other; verify with a probe
  before using the controller's wave.

---

## 5. Zones and light

### 5.1 Zone order

**[verified]** The zones run **left to right** as seen by the person sitting at
the laptop. **Zone 1 is the left part**, where Esc, Tab and ~ are; zone 4 is
the rightmost. The reference is given by keys rather than by "left" on purpose,
so that the sides are not confused when describing effects.

### 5.2 Controller and power

- **[source]** There is no public datasheet for the IT8295; ITE gives
  documentation only to OEMs, under NDA. The closest documented relative, the
  IT8296, is described as an embedded controller for RGB lighting with PWM, a
  keyboard scanner and addressable LED strips, built on a 32-bit MCU. So the
  8295 takes commands over USB HID and generates PWM for the LED channels.
- **[source]** The 8176 is the IT8176, described as an embedded controller with
  system functions for mobile systems. It is present on every 16IRX9 variant,
  the 8295 only on the RGB one. According to a Lenovo employee on a forum, the
  motherboards of the two variants differ. So the backlight controller and its
  power stage sit on the motherboard, and the keyboard module (FRU "Upper case
  with keyboard") holds only the LEDs, the light guide and the keys.
- **[verified]** The LEDs sit **under the keys** (visible as dots near the top
  edge of the keys), not at the edge of a light guide. In the bottom row
  (arrows, space bar, Ctrl) individual LEDs can be seen directly.
- **[not found]** The supply rail (3.3 V or 5 V), the current limiting, the PWM
  resolution, the number of LEDs per zone. No schematics or boardview of this
  motherboard are publicly available, and Lenovo's Hardware Maintenance Manual
  says nothing about the backlight.

### 5.3 The LEDs: three dies in one package

| Die | Material | Wavelength | Vf | Eye sensitivity V(λ) |
|---|---|---|---|---|
| Red | AlGaInP | 620–630 nm | 1.9–2.6 V | 0.265 |
| Green | InGaN | 520–530 nm | 2.7–3.4 V | **0.793** |
| Blue | InGaN | 465–475 nm | 2.6–3.3 V | **0.074** |

- A D65 white needs a **luminance** ratio of R:G:B ≈ 1 : 2.4 : 0.2. At equal
  PWM each die runs at the current its circuit sets, so the mix is decided by
  the chosen currents and the bins of the dies. **On the tested unit, the
  yellowish white means a shortage of blue.**
- **[verified]** The shortage is large: on the tested unit a neutral white
  needs red and green at 0.12–0.27 of blue, and a full-power neutral white is
  roughly a quarter of the maximum brightness of the yellowish "255,255,255".
- Heat: AlGaInP loses up to ~40% of its light at 60 °C, InGaN ~10%, so with
  heat the mix drifts towards cyan, not yellow. Heat does not explain the
  yellow tint.

### 5.4 The optical stack

PMMA passes ~92%, evenly across the spectrum; over a long path, polycarbonate
loses ~5% more blue than red and looks slightly yellow from the edge. The
optics add a few percent of warmth and are not the main cause. The
transmission of the legend paint: **[not found]**.

### 5.5 Zones blend at the seams

**[verified]** Four different colours on the four zones give about six hues to
the eye: light scatters into the neighbouring zones, and intermediate colours
form at the borders. There are no sharp borders between zones; this is not a
bug and cannot be fixed in software. Sharp transitions are therefore
impossible, but smooth effects benefit: four anchor colours are enough, and the
hardware blends the gradient between them.

### 5.6 Spread between LEDs and zones

- **[source]** Within one part number the flux bins spread the light output by
  up to 3×, and the chromaticity of a "white" LED ranges over x 0.244–0.311,
  y 0.298–0.388.
- **[verified]** The spread between keys shows on cool whites: some keys look
  greener and others bluer, while on warm colours they look the same. While
  blue played almost no part (the yellowish white), its spread went unnoticed;
  with blue at full and red and green at about a quarter, the bin of the blue
  die became the main tint of each key. It cannot be corrected per key (control
  is per zone only); a warmer white softens it.
- **[verified]** The zones differ too: on the tested unit, with the same bytes,
  zone 1 (left) looks noticeably bluer and dimmer than zone 4. That is why the
  white balance is stored **per zone**.

### 5.7 Perception

The eye adapts mostly to the monitor (large, bright, in the upper field of
view), so a warm keyboard light under a cold screen looks even more yellow. The
target white for the keyboard is the actual white of the screen, not an
abstract 5000 K. Dimming does not make white yellower, but the eye reads a dim
yellow as olive: that is why, in a white-balance test, a row of whites with
ever smaller R and G looked "warmer" until the share of blue became large
enough.

---

## 6. Hardware limitations

These are not temporary inconveniences; every feature has to be designed within
them.

1. **Four zones, not per-key.** The keyboard is split into four vertical
   strips. A wave across individual keys is physically impossible; a reaction
   to a key press can at most light one of the four zones.
2. **Two hardware brightness levels.** Smooth dimming has to scale the RGB
   values themselves, which leaves noticeably fewer shades on dark colours.
3. **Four built-in effects** (static, breath, wave, hue). Any other animation
   is a process that sends a new static frame for every frame: a daemon, not
   "set and forget".
4. **Zones blend at the seams** (section 5.5). Sharp transitions between zones
   are impossible, so do not try to make them; for smooth effects four anchor
   colours are enough, and the hardware fills in the gradient.
5. **Individual LEDs vary.** On white, some keys can show a noticeably greenish
   tint. This is LED binning, not a software fault. Software can compensate
   only per zone (there is no per-key control), so the choice of white can
   soften it but not remove it.
6. **The PWM is linear.** Bytes 0–255 map linearly onto the duty cycle, with
   practically no floor (2 is still visible). sRGB gamma in software is
   therefore mandatory; without it orange turns yellow and purple turns pink.
7. **The blue channel is weak.** On the tested unit a neutral white needs red
   and green at 0.12–0.27 of blue, and a full white is about a quarter of the
   brightness of the yellowish "255,255,255". Blue cannot go above 255, so a
   neutral white is dimmer.
8. **Zones differ.** On the tested unit, with the same bytes, zone 1 is
   noticeably bluer and dimmer than zone 4. The white balance is stored per
   zone (`calibration.json`).
9. **Keys differ.** The spread between keys shows on cool whites (some keys
   look greener, others bluer) and not on warm colours. Only a warmer white
   helps.
10. **The LEDs sit under the keys** (visible as dots), not at the edge of a
    light guide.
11. **Brightness byte, "off", wave direction.** The brightness byte works (1 is
    slightly dimmer than 2; 3 and 4 are accepted but light as 2). "Off", a
    report of zeros after `CC 16` as Vantage sends it, turns the light off
    completely. The wave direction (bytes 18/19) is not verified: other
    implementations label the two bytes the other way round from the table in
    section 2.2.

---

## 7. Colour calibration

### 7.1 Why colours come out wrong

1. **No white balance.** (255,255,255) is three dies at their factory maximum
   currents; blue is weak, hence the yellow. Pure R, G and B are not affected.
2. **No gamma.** sRGB is non-linear and the PWM is linear: 128 on screen is
   21.6% of the light, 128 on the keyboard is 50%. Orange (255,128,0) lights up
   as (255,188,0), purple (128,0,128) as light pink, and pastels wash out.
3. **Zones differ, keys differ.** The zone spread is corrected by a per-zone
   balance; the key spread can only be softened with a warmer white.

### 7.2 Colour pipeline

In `kkonf_core.py`: sRGB → linear light (the exact sRGB curve) → per-zone
balance multipliers (r, g, b) → bytes 0–255. White by colour temperature: the
CIE daylight locus from 4000 K up (6500 K = D65 = exactly 255,255,255), the
Planckian locus below 4000 K.

### 7.3 Calibration data and the calibration page

- The calibration is stored in `~/.config/keyboard-konf/calibration.json`, per
  zone: `temp` and `tint` (two sliders, as in a photo editor), `level` (dims
  the whole zone) and the resulting `gains`. The older single-zone format is
  still read.
- The calibration page in the GUI: a black screen with a grey strip just above
  the keys, a zone choice ("All" moves every zone together and keeps their
  differences), three sliders, swatches for checking, and save.

### 7.4 Calibration method

1. **By eye.** Rows of test whites with blue at 255 and red and green stepped
   down show where neutral lies. That gives the starting multipliers, which are
   then fine-tuned by eye on the calibration page.
2. **With a camera, between zones only.** Within one frame the camera's white
   balance is shared by all zones, so the differences between zones can be
   measured, even from a JPEG. The pixels are linearised with the sRGB curve,
   clipped and dark pixels are dropped, the key area is split into four
   quarters, and the central 60% of each quarter is used. This refines the
   by-eye estimate of how the zones differ; a second photo, taken after the
   correction, shows how much difference is left.
3. **Zone brightness by eye.** A camera cannot measure it: the average
   brightness in the frame depends on the area of the key legends
   (Home/End/Num Lock versus F6/F7). Zone levels are set by eye.
4. **A common cast.** A tint shared by all zones (on the tested unit, a magenta
   one) is removed with the shared "All" adjustment, which keeps the
   differences between zones.

### 7.5 Brightness on top of the calibration

The balanced white keeps blue at full drive, so every colour is limited by the
level of the blue channel (about a quarter of what the LEDs can do).
`calibration.json` has two settings for this:

- `boost` (1.0–4.0): all channels are multiplied, and whatever goes past 255 is
  clipped. White gradually turns warmer. The default is 1.0 (100%).
- `max_brightness`: the chosen colour is multiplied by one factor for all
  zones, so that the strongest channel just reaches 255 somewhere. Hue,
  saturation and the balance between zones are kept; only the light grows.
  White is not changed. The colour is first applied as calibrated; this switch
  is a separate step on top.

### 7.6 Colour checks

Hex colours sent through the pipeline and compared by eye with the same colours
on screen:

| Colour | Result |
|---|---|
| `#FF8000` orange | a clean orange (without the pipeline: yellow-orange) |
| `#800080` purple | matches; dim because the colour itself is dark; slightly towards red |
| `#A000FF`, `#FF00FF` | match |
| `#FF69B4` pink | matches, slightly warmer than the reference; `max_brightness` does not change the hue |
| `#00C8FF` cyan | the hue matches; darker than on screen (blue is weak, `max_brightness` does not help) |
| `#C8DCFF` pastel | matches: white with a barely visible cool tint |
| `#FFFF00` yellow, `#80FF00` lime | match; lime looks slightly darker to the eye |

---

## 8. Access without root

The rule is `udev/60-keyboard-konf.rules`; installation steps are in the README.

- The `uaccess` tag is processed by `73-seat-late.rules`, so the rule file must
  sort before it: `60-keyboard-konf.rules`. A file named `99-…` would not work.
- The rule has two lines: the first marks the hidraw nodes of USB interface 0,
  the second gives `uaccess` only to the one of them that belongs to
  `048d:c994`. **[verified]** The ACL `user:<you>:rw-` appears on the lighting
  node; the node of the 8295's interface 1 and the 8176's node stay root-only.
- **[verified, hidraw.c]** The `HIDIOCSFEATURE`, `HIDIOCGFEATURE` and
  `HIDIOCGINPUT` ioctls do not check the mode the file was opened in, so a
  write-only open is no safeguard. None is needed, though: keystrokes do not
  pass through this node (section 1.5).

---

## 9. Do not touch

- **The second ITE controller, `048d:c992` (ITE 8176).** It is not the
  backlight (section 1.4).
- **The 8295's other HID channel**, interface 1, with its 64-byte reports
  without a report ID (section 1.3). Purpose unknown, possibly service or
  firmware.
- **Feature reports `0x5A` and `0x07`** on the lighting interface (section
  1.2). Purpose unknown.

Interface 1 and reports 0x5A/0x07 are the only realistic places where something
could be broken. keyboard-konf never touches them, and the udev rule keeps
interface 1 root-only.

It is not known whether a malformed report can leave the backlight in an odd
state, or what would bring it out of one. Keep probes small, one change at a
time.

Bad states others have seen: the RGB "stuck" until an EC reset (4JX #52); the
controller showing up as `048d:c101` "ITE Device(8910)" without RGB control
(#263/#285). Nobody found the cause, and software writes were not blamed. The
16IRX9 has no Novo button (its menu opens with F9). An EC reset means cutting
all power: shut down, unplug the charger and USB devices, and hold the power
button for a minute.

---

## 10. Credits

The protocol comes from these open implementations:

- [imShara/l5p-kbl](https://github.com/imShara/l5p-kbl)
- [4JX/L5P-Keyboard-RGB](https://github.com/4JX/L5P-Keyboard-RGB)

The other projects reviewed are listed in section 2.3.
