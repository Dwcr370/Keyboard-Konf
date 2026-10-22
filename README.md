# Keyboard Konf

Backlight control for the 4-zone RGB keyboard of the Lenovo Legion 5 16IRX9
(ITE 8295, USB `048d:c994`) on Linux. A white that is actually white, mixed
colours that match the screen, and one short command instead of a long one.

## Why

Lenovo Vantage does not exist on Linux, and every tool that does exist sends
the colour you pick straight to the controller. That skips two things:

- the sRGB curve: screen values are not linear light, PWM duty is, so orange
  comes out yellow, purple comes out pink and pastels wash out;
- the white balance of the LEDs: on the tested keyboard a plain
  `255,255,255` is a distinct yellow, because the blue channel is weaker
  than a neutral white needs.

keyboard-konf decodes sRGB to linear light, applies a per-channel white
balance that you calibrate once by eye against your own screen, and only then
talks to the hardware. `docs/hardware-research.md` has the full story: how the
backlight is built, what the protocol does, what was measured.

## Install

Get the code; every command below runs from this folder:

```sh
git clone https://github.com/Dwcr370/Keyboard-Konf.git
cd Keyboard-Konf
```

Access without sudo, one time. The rule opens only the lighting interface of
the controller to the logged-in user; the file must sort before
`73-seat-late.rules`, which is where the `uaccess` tag becomes an ACL.

```sh
sudo cp udev/60-keyboard-konf.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=hidraw
getfacl /dev/hidraw*        # one node must list user:<you>:rw-
```

If `getfacl` shows no such line, log out and back in: logind sets the ACL for
the active session.

The command and the launcher entry (no root needed):

```sh
ln -s "$PWD/kkonf.py" ~/.local/bin/kkonf
cp kkonf.desktop ~/.local/share/applications/
```

`kkonf` is a link into this folder, so keep the folder where it is. After
moving or renaming it, run `ln -sf "$PWD/kkonf.py" ~/.local/bin/kkonf` from
the new place and, if the daemon runs, `systemctl --user restart kkonf`.

Requirements: Python 3.12+, and for the window `python-gobject`, `gtk4`,
`libadwaita`. The CLI needs nothing beyond the standard library.

## Use

```sh
kkonf white                 # 6500 K, the white of the screen
kkonf white 4000            # warmer; kkonf white 8000 is colder
kkonf '#ff8000'             # one colour on all four zones
kkonf '#ff0000,#00ff00,#0000ff,#ffffff'   # per zone, zone 1 = Esc side
kkonf off | on | toggle
kkonf effect wave --color '#00c0ff' --color2 '#001030' --speed 0.6
kkonf effects               # every effect and its parameters
kkonf next                  # the next effect in the cycle (bind it to a key)
kkonf preset Ocean          # a saved preset; kkonf presets lists them
kkonf preset Mine --save    # save what the daemon shows now (--delete removes)
kkonf brightness 60         # software brightness, 0-100%
kkonf max on                # the brightest level that keeps the hue
kkonf status                # what the daemon shows right now
kkonf gui                   # the window; changes apply as you move things
kkonf calibrate             # the white-balance page
```

Effects, presets, `on`, `toggle`, `next`, `brightness`, `max` and `status`
need the daemon (next section). White, a colour and `off` go through the daemon when
it runs and straight to the device when it does not.

Options for direct writes: `--brightness 1|2` (the two hardware levels),
`--boost 1..4` (multiply all channels, clip at 255: brighter, white drifts
warm), `--raw` (send the bytes exactly as given, no gamma and no white
balance; for hardware tests), `--dry-run` (print the report instead of
sending it), `--direct` (skip the daemon even if it runs). `--max` works both
ways: it pushes the colour to the brightest level that does not clip, with
hue, saturation and zone balance kept and white unchanged.

## Daemon

Everything beyond a still colour is `kkonf daemon`. It owns the device,
remembers what it shows in `~/.config/keyboard-konf/state.json`, renders the
moving effects (25 frames per second by default, `--fps` to change), fades
every change over 300 ms in linear light, puts the light back when you log in
and after a resume from sleep, and reloads the white balance whenever you
recalibrate. Presets and your own colours live next to it in `presets.json`
(four presets and one colour to start with; all of it is yours to change).
Clients reach it over `$XDG_RUNTIME_DIR/keyboard-konf.sock`.

Run it in a terminal to try it, or as a user service so it starts with your
session:

```sh
mkdir -p ~/.config/systemd/user
cp systemd/kkonf.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now kkonf
journalctl --user -u kkonf -f       # its log
```

Effects: `white`, `static`, `gradient`, `breathe`, `breathe2`, `heartbeat`,
`candle`, `wave`, `rainbow`, `hue`, `scanner`, `comet`, `ripple`, `sparkle`,
`aurora`, `blink`, and four that react to the machine: `system` (the colour
of the caelestia Material scheme, follows the wallpaper), `locks` (zone 1 in
its own colour while Caps Lock is on, zone 4 while Num Lock is on), `battery`
(charge as a bar from zone 1, red to green, breathing while it charges, red
breathing below 15%), `daylight` (white that cools towards noon and warms
towards night). Parameters go as options (`--color`, `--color2`, `--colors`,
`--palette`, `--speed` 0-1, `--direction ltr|rtl`) or as `--param
name=value`; `kkonf effects` lists which effect takes which. A parameter you
do not mention keeps its last value, so switching effects keeps the colours
you chose. The controller's own breath/wave/hue effects are not used: they
skip the gamma and the white balance.

For other clients: one JSON object per line on the socket, `{"cmd": "get"}`,
`{"cmd": "set", "state": {...}}`, `{"cmd": "toggle"}`, `{"cmd": "next"}`,
`{"cmd": "effects"}`, `{"cmd": "presets"}`, `{"cmd": "preset", "name": ...}`
(`save_preset`, `delete_preset` likewise), `{"cmd": "add_colour", "color":
...}` and `remove_colour`. The daemon answers each command and sends
`{"event": "state", ...}` or `{"event": "presets", ...}` to every connected
client after a change, so a window and the command line never disagree.
`kkonf_ipc.py` is the reference.

Example Hyprland bindings for the next effect and for on/off (add them to
your Hyprland config):

```
bind = SUPER, F9, exec, kkonf next
bind = SUPER SHIFT, F9, exec, kkonf toggle
```

## Settings in caelestia

`caelestia/` holds a "Keyboard" section for the Appearance pane of the
caelestia shell settings (right under Background): backlight on/off,
brightness, maximum, the effect and its parameters, swatches (the shell's
own scheme colours, twelve basic hues in three shades, your own colours), a
colour wheel with value, red/green/blue and hex, and the presets. Everything
applies live through the daemon. The shell has no plugin mechanism, so the
section is three QML files copied into the installed shell plus six lines in
`AppearancePane.qml`; a pacman hook puts them back after every shell upgrade.
The hook runs a root-owned copy in `/usr/local/share/keyboard-konf/`, never
anything from a home directory.

Tested with caelestia-shell 1.5.2 and Quickshell 0.3.2; the patch fits the
settings pane of every 1.x release up to 1.6.2. Caelestia 2.0 replaced the
settings window, so on 2.x the installer stops and the section is not
installed; the daemon, the command line and the GTK window do not depend on
the shell.

```sh
sudo caelestia/install.sh apply             # copies the files there, patches the shell
caelestia shell -k; caelestia shell -d      # the shell does not watch its files
```

If a shell update changes the pane's structure the hook prints a message,
the section stays out until the patch is adjusted, and the daemon and the
command line are unaffected.

Why brightness needs a control at all: the balanced white keeps blue at full
drive and holds red and green down to roughly a quarter of it, so every
colour is reproduced at the blue channel's level. That is accurate (a dark
screen colour comes out dark) but not always what you want on a backlight.

## Calibrate once

Without a calibration file the program starts from a white balance set by eye
on the tested laptop (the average of its four zones), so white is already
close to neutral on the same model. LEDs differ between laptops and between
zones, though: if white still looks tinted, or one zone does not match the
others, calibrate, zone by zone where needed. "Reset" on the calibration page
returns to that starting point.

`kkonf calibrate` turns the screen black with a grey strip right above the
keys. With "All" selected, "warm / cold" and "green / magenta" move all four
zones together; pick a zone number to correct that zone alone (zones can
differ: on the tested keyboard the left one is bluer), and lower its "level"
if it is brighter than its neighbours. Click through the swatches to check mixed
colours, then save. The result lives in `~/.config/keyboard-konf/calibration.json`
and is used by the command line and the window alike. The strip is the only
reference that matters for the overall white: the keyboard and the monitor
have different spectra, so a camera and an eye disagree about white, and the
eye is the one that counts. A camera photo is good for one thing only, the
differences between zones within one frame; `docs/hardware-research.md`
describes the method.

## Uninstall / roll back

Each layer comes off on its own:

```sh
sudo /usr/local/share/keyboard-konf/install.sh remove     # the shell section + hook
systemctl --user disable --now kkonf && rm ~/.config/systemd/user/kkonf.service
sudo rm /etc/udev/rules.d/60-keyboard-konf.rules
sudo udevadm control --reload-rules && sudo udevadm trigger --subsystem-match=hidraw
rm ~/.local/bin/kkonf ~/.local/share/applications/kkonf.desktop
rm -r ~/.config/keyboard-konf
```

Nothing is written to the controller's flash: there is no "save" command in
this protocol. Any new report, Fn+Space or a reboot replaces whatever was sent
last.

## Files

- `kkonf.py` — command line and entry point; a client of the daemon.
- `kkonfd.py` — the daemon: state, presets, socket, frame loop, crossfade,
  resume, the sensors the reactive effects read.
- `kkonf_effects.py` — the effects, each a pure function of time and sensors.
- `kkonf_ipc.py` — the socket protocol.
- `kkonf_gui.py` — GTK4/libadwaita window and the calibration page.
- `kkonf_core.py` — device discovery, report layout, colour pipeline.
- `systemd/kkonf.service` — user unit for the daemon.
- `caelestia/` — the Keyboard section for the shell settings, its installer
  (`install.sh`, `patch-appearance.py`) and the pacman hook.
- `probe.py` — raw probe for hardware experiments (`--write` to send).
- `udev/60-keyboard-konf.rules` — access rule.
- `docs/hardware-research.md` — research notes.
