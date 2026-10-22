"""Shared protocol and colour logic for keyboard-konf.

This module holds everything the CLI and the GUI need: how to find the
lighting controller, how to lay out its report, and how to turn a colour
picked on screen into the bytes the controller should actually receive.

Protocol facts below were verified on the target hardware, not copied from
documentation (there is none). See docs/hardware-research.md for the full
write-up.

Colour pipeline (see docs/hardware-research.md for the reasoning):

    screen colour (sRGB, 0-255)
      -> linear light (the sRGB transfer curve, roughly gamma 2.2)
      -> x per-channel white-balance gain from ~/.config/keyboard-konf/
      -> 0-255 duty byte for the controller

Without the first step every mixed colour drifts towards "equal channels"
(orange turns yellow, purple turns pink); without the second step white is
whatever tint the LEDs happen to have, on the tested unit a distinct yellow.
"""

from __future__ import annotations

import fcntl
import glob
import json
import math
import os

# ----------------------------------------------------------------- protocol

VENDOR_ID = "048D"
PRODUCT_ID = "C994"

REPORT_ID = 0xCC
HEADER = 0x16
PAYLOAD_LEN = 32
BUFFER_LEN = 1 + PAYLOAD_LEN  # report ID + payload; 33 confirmed on hardware

ZONE_COUNT = 4
BRIGHTNESS_LEVELS = (1, 2)  # the controller offers exactly two
BRIGHTNESS_DEFAULT = 2

EFFECT_STATIC = 1
SPEED_DEFAULT = 1

Color = tuple[int, int, int]
Gains = tuple[float, float, float]

# ------------------------------------------------------------------- colour

# The controller is assumed to map 0-255 linearly onto PWM duty (measured on
# the hardware). Should the firmware turn out to apply a curve of its own, put
# that curve's exponent here and encode() will pre-compensate. 1.0 means linear.
FIRMWARE_GAMMA = 1.0

NO_GAINS: Gains = (1.0, 1.0, 1.0)

DEFAULT_KELVIN = 6500  # the screen's own white (D65)
KELVIN_MIN = 2000
KELVIN_MAX = 12000

# How far the two calibration sliders reach, in powers of two. At +-1 the
# temperature slider changes the blue-to-red ratio sixteen times; the tint
# slider changes green by a factor of 1.68. Wide on purpose: on the tested
# unit a neutral white needs red and green cut to roughly a quarter of blue,
# which should sit mid-slider, not at its end.
TEMP_SPAN = 2.0
TINT_SPAN = 0.75

# ------------------------------------------------------------------- config

CONFIG_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
    "keyboard-konf",
)
CALIBRATION_FILE = os.path.join(CONFIG_DIR, "calibration.json")


class DeviceNotFound(Exception):
    """No lighting controller matching our VID:PID was found."""


# ======================================================================
# Device discovery and transport
# ======================================================================


def _declares_lighting_report(node: str) -> bool:
    """True if this hidraw node advertises feature report 0xCC.

    Both interfaces of the controller share a VID:PID, so the ID alone is
    not enough to tell them apart. The report descriptor is.
    """
    path = "/sys/class/hidraw/%s/device/report_descriptor" % node
    try:
        with open(path, "rb") as handle:
            return b"\x85\xcc" in handle.read()
    except OSError:
        return False


def find_device() -> str:
    """Return the /dev/hidrawN path of the lighting controller.

    Never returns a hardcoded path: the kernel may renumber nodes across
    reboots, so the device is matched by VID:PID and then confirmed by the
    report it declares.
    """
    for uevent in sorted(glob.glob("/sys/class/hidraw/hidraw*/device/uevent")):
        try:
            with open(uevent) as handle:
                text = handle.read()
        except OSError:
            continue
        for line in text.splitlines():
            if not line.startswith("HID_ID="):
                continue
            parts = line.split(":")
            if len(parts) != 3:
                continue
            if parts[1][-4:].upper() != VENDOR_ID or parts[2][-4:].upper() != PRODUCT_ID:
                continue
            node = uevent.split("/")[4]
            if _declares_lighting_report(node):
                return "/dev/" + node
    raise DeviceNotFound(
        "no device %s:%s declaring feature report 0x%02X"
        % (VENDOR_ID, PRODUCT_ID, REPORT_ID)
    )


def _hidiocsfeature(size: int) -> int:
    """ioctl request number for HIDIOCSFEATURE(size)."""
    return (3 << 30) | (size << 16) | (ord("H") << 8) | 0x06


def write_report(path: str, buffer: bytearray) -> str:
    """Send the report. Returns the open mode that worked, for diagnostics.

    Opens write-only first on purpose: this node also carries HID input
    reports, and a descriptor without read access cannot read() them. Falls
    back to read-write only if the kernel refuses the ioctl on a write-only
    descriptor.
    """
    request = _hidiocsfeature(len(buffer))
    last_error: OSError | None = None
    for mode, label in ((os.O_WRONLY, "write-only"), (os.O_RDWR, "read-write")):
        try:
            descriptor = os.open(path, mode)
        except OSError as error:
            last_error = error
            continue
        try:
            fcntl.ioctl(descriptor, request, buffer)
            return label
        except OSError as error:
            last_error = error
        finally:
            os.close(descriptor)
    raise last_error if last_error else OSError("could not write the report")


# ======================================================================
# Report layout
# ======================================================================


def parse_hex(text: str) -> Color:
    """Parse '#rrggbb' or 'rrggbb' into an (r, g, b) tuple."""
    digits = text.strip().lstrip("#")
    if len(digits) != 6:
        raise ValueError("expected 6 hex digits, got %r" % text)
    try:
        return (int(digits[0:2], 16), int(digits[2:4], 16), int(digits[4:6], 16))
    except ValueError:
        raise ValueError("not a hex colour: %r" % text) from None


def to_hex(color: Color) -> str:
    """Format an (r, g, b) tuple as '#RRGGBB'."""
    return "#%02X%02X%02X" % tuple(color)


def validate(zones: list[Color], brightness: int) -> None:
    """Reject anything malformed before it reaches the device."""
    if len(zones) != ZONE_COUNT:
        raise ValueError("expected %d zones, got %d" % (ZONE_COUNT, len(zones)))
    for index, zone in enumerate(zones, start=1):
        if len(zone) != 3:
            raise ValueError("zone %d is not an (r, g, b) triple" % index)
        for channel in zone:
            if not isinstance(channel, int) or not 0 <= channel <= 255:
                raise ValueError("zone %d has a channel outside 0-255" % index)
    if brightness not in BRIGHTNESS_LEVELS:
        raise ValueError("brightness must be one of %r" % (BRIGHTNESS_LEVELS,))


def build_report(zones: list[Color], brightness: int,
                 effect: int = EFFECT_STATIC,
                 speed: int = SPEED_DEFAULT) -> bytearray:
    """Build the 33-byte buffer: report ID followed by a 32-byte payload.

    `zones` are the bytes the controller should receive (already encoded).
    Zone 1 is the left of the keyboard (Esc/Tab side), verified on hardware.
    """
    validate(zones, brightness)
    buffer = bytearray(BUFFER_LEN)
    buffer[0] = REPORT_ID
    buffer[1] = HEADER
    buffer[2] = effect
    buffer[3] = speed
    buffer[4] = brightness
    for index, (red, green, blue) in enumerate(zones):
        base = 5 + index * 3
        buffer[base] = red
        buffer[base + 1] = green
        buffer[base + 2] = blue
    return buffer


def build_off_report() -> bytearray:
    """The report Lenovo's own software sends for "off": header, then zeros.

    Effect 0, speed 0, brightness 0, black zones. Documented in the
    LenovoLegionToolkit source and verified visually on the 2024 LOQ sibling;
    confirmed on the supported model as well.
    """
    buffer = bytearray(BUFFER_LEN)
    buffer[0] = REPORT_ID
    buffer[1] = HEADER
    return buffer


def hexdump(data: bytes) -> str:
    return " ".join("%02X" % byte for byte in data)


# ======================================================================
# Colour: sRGB <-> linear light, white balance, colour temperature
# ======================================================================


def srgb_to_linear(value: int) -> float:
    """Decode one 8-bit sRGB channel to linear light in 0.0-1.0."""
    c = value / 255.0
    if c <= 0.04045:
        return c / 12.92
    return ((c + 0.055) / 1.055) ** 2.4


def linear_to_srgb(value: float) -> int:
    """Encode linear light in 0.0-1.0 back to an 8-bit sRGB channel."""
    value = min(max(value, 0.0), 1.0)
    if value <= 0.0031308:
        c = value * 12.92
    else:
        c = 1.055 * value ** (1 / 2.4) - 0.055
    return int(round(c * 255))


def linear_to_duty(value: float) -> int:
    """Quantise linear light to the controller's 0-255 duty byte."""
    value = min(max(value, 0.0), 1.0)
    if FIRMWARE_GAMMA != 1.0:
        value = value ** (1.0 / FIRMWARE_GAMMA)
    return int(round(value * 255))


BOOST_MIN = 1.0
BOOST_MAX = 4.0


def encode(color: Color, gains: Gains = NO_GAINS, boost: float = 1.0) -> Color:
    """Turn a screen colour into the bytes the controller should receive.

    sRGB -> linear light -> per-channel white-balance gain -> x boost -> duty
    byte. `boost` (1.0-4.0) trades accuracy for light: the balanced white
    keeps blue at full drive, so every colour is held down to the blue
    channel's level; above 1.0 all channels are multiplied and whatever hits
    255 is clipped, which brightens everything and lets white drift warm.
    """
    return tuple(
        linear_to_duty(srgb_to_linear(channel) * gain * boost)
        for channel, gain in zip(color, gains)
    )


def max_boost(colors: list[Color], gains: list[Gains]) -> float:
    """The largest boost at which no channel of any zone clips.

    Hue, saturation and the balance between zones stay as calibrated; only
    the overall light goes up to what the hardware can give for this colour.
    """
    peak = max(
        (srgb_to_linear(channel) * gain
         for color, zone_gains in zip(colors, gains)
         for channel, gain in zip(color, zone_gains)),
        default=0.0,
    )
    return 1.0 / peak if peak > 1e-6 else 1.0


def _planckian_xy(kelvin: float) -> tuple[float, float]:
    """Chromaticity of a black body (Kim et al. 2002 fit, 1667-25000 K)."""
    t = float(kelvin)
    if t <= 4000:
        x = -0.2661239e9 / t ** 3 - 0.2343589e6 / t ** 2 + 0.8776956e3 / t + 0.179910
    else:
        x = -3.0258469e9 / t ** 3 + 2.1070379e6 / t ** 2 + 0.2226347e3 / t + 0.240390
    if t <= 2222:
        y = -1.1063814 * x ** 3 - 1.34811020 * x ** 2 + 2.18555832 * x - 0.20219683
    elif t <= 4000:
        y = -0.9549476 * x ** 3 - 1.37418593 * x ** 2 + 2.09137015 * x - 0.16748867
    else:
        y = 3.0817580 * x ** 3 - 5.87338670 * x ** 2 + 3.75112997 * x - 0.37001483
    return x, y


def _daylight_xy(kelvin: float) -> tuple[float, float]:
    """Chromaticity of CIE daylight (4000-25000 K); 6504 K is exactly D65."""
    t = float(kelvin)
    if t <= 7000:
        x = -4.6070e9 / t ** 3 + 2.9678e6 / t ** 2 + 0.09911e3 / t + 0.244063
    else:
        x = -2.0064e9 / t ** 3 + 1.9018e6 / t ** 2 + 0.24748e3 / t + 0.237040
    y = -3.000 * x ** 2 + 2.870 * x - 0.275
    return x, y


def kelvin_to_srgb(kelvin: float) -> Color:
    """White of the given colour temperature as a full-brightness sRGB colour.

    From 4000 K up the CIE daylight locus is used, so 6500 K is the screen's
    own white (D65) and comes out as (255, 255, 255). Below 4000 K daylight
    is not defined and the Planckian (black body) locus takes over. The
    brightest channel is always 255.
    """
    kelvin = min(max(float(kelvin), KELVIN_MIN), KELVIN_MAX)
    x, y = _daylight_xy(kelvin) if kelvin >= 4000 else _planckian_xy(kelvin)
    big_x, big_y, big_z = x / y, 1.0, (1.0 - x - y) / y
    red = 3.2406 * big_x - 1.5372 * big_y - 0.4986 * big_z
    green = -0.9689 * big_x + 1.8758 * big_y + 0.0415 * big_z
    blue = 0.0557 * big_x - 0.2040 * big_y + 1.0570 * big_z
    peak = max(red, green, blue)
    return tuple(linear_to_srgb(max(c, 0.0) / peak) for c in (red, green, blue))


def gains_from_sliders(temp: float, tint: float) -> Gains:
    """Two photo-editor style sliders (-1..1) -> three multipliers, max 1.0.

    temp > 0 cools the white (less red and green relative to blue), temp < 0
    warms it; tint > 0 goes magenta (less green), tint < 0 goes green. The
    multipliers are normalised so the strongest channel keeps full drive.
    """
    temp = min(max(float(temp), -1.0), 1.0)
    tint = min(max(float(tint), -1.0), 1.0)
    log_red = -TEMP_SPAN * temp
    log_green = -TEMP_SPAN * temp - TINT_SPAN * tint
    log_blue = TEMP_SPAN * temp
    raw = [2.0 ** v for v in (log_red, log_green, log_blue)]
    peak = max(raw)
    return tuple(v / peak for v in raw)


LEVEL_MIN = 0.05


def gains_with_level(temp: float, tint: float, level: float) -> Gains:
    """Balance from the two sliders, then the whole zone scaled by `level`.

    `level` (LEVEL_MIN..1.0) dims a zone as a whole; it is how a zone that
    came out brighter than its neighbours is brought down to match them.
    """
    level = min(max(float(level), LEVEL_MIN), 1.0)
    return tuple(v * level for v in gains_from_sliders(temp, tint))


def sliders_from_gains(gains: Gains) -> tuple[float, float, float]:
    """Inverse of gains_with_level: (temp, tint, level) for stored gains.

    Lets a calibration computed elsewhere (for example from a photo) be
    stored with slider positions the GUI can show. Positions are clamped to
    the slider range, so gains outside it are only approximated.
    """
    red, green, blue = (max(float(v), 1e-6) for v in gains)
    level = max(red, green, blue)
    red, green, blue = red / level, green / level, blue / level
    temp = -math.log2(red / blue) / (2 * TEMP_SPAN)
    tint = (math.log2(red) - math.log2(green)) / TINT_SPAN
    temp = min(max(temp, -1.0), 1.0)
    tint = min(max(tint, -1.0), 1.0)
    level = min(max(level, LEVEL_MIN), 1.0)
    return temp, tint, level


# ======================================================================
# Calibration file
# ======================================================================


# The white balance is kept per zone: the four zones can have visibly
# different native whites (on the tested unit the left one is bluer), so one
# set of gains cannot make them match. Each entry is {"temp", "tint",
# "gains"}: the two slider positions and the three multipliers they produce.


def default_zone_calibration() -> dict:
    return {"temp": 0.0, "tint": 0.0, "level": 1.0, "gains": list(NO_GAINS)}


def _parse_zone_entry(entry) -> dict:
    gains = [float(v) for v in entry["gains"]]
    if len(gains) != 3 or not all(0.0 < v <= 1.0 for v in gains):
        raise ValueError("gains out of range")
    temp = min(max(float(entry.get("temp", 0.0)), -1.0), 1.0)
    tint = min(max(float(entry.get("tint", 0.0)), -1.0), 1.0)
    level = min(max(float(entry.get("level", max(gains))), LEVEL_MIN), 1.0)
    return {"temp": temp, "tint": tint, "level": level, "gains": gains}


def load_calibration() -> list[dict]:
    """Return one {"temp", "tint", "gains"} per zone; defaults without a file.

    Accepts the first file format too (one set at the top level), which is
    applied to all four zones.
    """
    try:
        with open(CALIBRATION_FILE) as handle:
            data = json.load(handle)
        if "zones" in data:
            zones = [_parse_zone_entry(entry) for entry in data["zones"]]
            if len(zones) != ZONE_COUNT:
                raise ValueError("expected %d zones" % ZONE_COUNT)
            return zones
        single = _parse_zone_entry(data)
        return [dict(single, gains=list(single["gains"])) for _ in range(ZONE_COUNT)]
    except (OSError, ValueError, KeyError, TypeError):
        return [default_zone_calibration() for _ in range(ZONE_COUNT)]


def _load_file() -> dict:
    try:
        with open(CALIBRATION_FILE) as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def load_boost() -> float:
    """The saved brightness boost (1.0 when there is none)."""
    try:
        value = float(_load_file().get("boost", 1.0))
    except (ValueError, TypeError):
        return 1.0
    return min(max(value, BOOST_MIN), BOOST_MAX)


def load_max_brightness() -> bool:
    """Whether every colour is to be pushed to the brightest non-clipping level."""
    return bool(_load_file().get("max_brightness", False))


def save_calibration(zones: list[dict], boost: float | None = None,
                     max_brightness: bool | None = None) -> None:
    """Write the calibration atomically; a half-written file is never seen.

    A setting left out keeps whatever the file already holds.
    """
    if len(zones) != ZONE_COUNT:
        raise ValueError("expected %d zones" % ZONE_COUNT)
    if boost is None:
        boost = load_boost()
    if max_brightness is None:
        max_brightness = load_max_brightness()
    payload = {"zones": [
        {"temp": round(float(z["temp"]), 4), "tint": round(float(z["tint"]), 4),
         "level": round(float(z.get("level", max(z["gains"]))), 4),
         "gains": [round(float(v), 4) for v in z["gains"]]}
        for z in zones
    ], "boost": round(min(max(float(boost), BOOST_MIN), BOOST_MAX), 3),
        "max_brightness": bool(max_brightness)}
    os.makedirs(CONFIG_DIR, exist_ok=True)
    temporary = CALIBRATION_FILE + ".tmp"
    with open(temporary, "w") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")
    os.replace(temporary, CALIBRATION_FILE)


def current_gains() -> list[Gains]:
    """The saved multipliers, one (r, g, b) triple per zone."""
    return [tuple(z["gains"]) for z in load_calibration()]


def encode_zones(colors: list[Color], gains: list[Gains],
                 boost: float = 1.0) -> list[Color]:
    """Encode four screen colours, each with its own zone's gains."""
    return [encode(color, gain, boost) for color, gain in zip(colors, gains)]


# ======================================================================
# One-call apply
# ======================================================================


def effective_boost(zones: list[Color], gains: list[Gains],
                    boost: float | None = None,
                    max_brightness: bool | None = None) -> float:
    """The boost to use: the max-brightness one when that mode is on, else
    the given or saved fixed boost."""
    if max_brightness is None:
        max_brightness = load_max_brightness()
    if max_brightness:
        return max_boost(zones, gains)
    return load_boost() if boost is None else boost


def apply_zones(zones: list[Color], brightness: int = BRIGHTNESS_DEFAULT,
                gains: list[Gains] | None = None, raw: bool = False,
                boost: float | None = None,
                max_brightness: bool | None = None) -> str:
    """Encode four screen colours and send them. Returns the device path.

    With raw=True the values go to the controller untouched (hardware tests).
    """
    if gains is None:
        gains = current_gains()
    boost = effective_boost(zones, gains, boost, max_brightness)
    encoded = list(zones) if raw else encode_zones(zones, gains, boost)
    report = build_report(encoded, brightness)
    device = find_device()
    write_report(device, report)
    return device
