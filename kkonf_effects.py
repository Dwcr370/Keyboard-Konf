"""Frame generators for the daemon.

An effect turns time into four colours, one per zone (zone 1 first), in
linear light with channels 0.0-1.0. The daemon applies the white balance,
brightness and the crossfade afterwards, so effects never see the device.

Every effect is a pure function of time and of the sensor snapshot the
daemon hands it (lock keys, battery, the shell's colour scheme, the clock):
nothing is kept between frames, which keeps the daemon trivially restartable
and the effects easy to test. Randomness, where an effect needs it, is a hash
of time, for the same reason. Effects that read the snapshot are "reactive":
the daemon keeps rendering them like moving ones and writes a frame only
when the bytes change.

The hardware blends neighbouring zones at their seams, so everything here is
smooth on purpose: a band moving across four zones reads as a wave, a hard
edge would not read at all.
"""

from __future__ import annotations

import colorsys
import math

import kkonf_core as core

Lin = tuple[float, float, float]

ZONES = core.ZONE_COUNT
TAU = 2.0 * math.pi
BLACK_LIN: Lin = (0.0, 0.0, 0.0)

# ------------------------------------------------------------- colour helpers


def _decode(channel: float) -> float:
    """sRGB 0.0-1.0 -> linear light (float version of core.srgb_to_linear)."""
    channel = min(max(channel, 0.0), 1.0)
    if channel <= 0.04045:
        return channel / 12.92
    return ((channel + 0.055) / 1.055) ** 2.4


def lin(color: core.Color) -> Lin:
    return tuple(_decode(c / 255.0) for c in color)


def lin_hex(text: str) -> Lin:
    return lin(core.parse_hex(text))


def hsv(hue: float, saturation: float = 1.0, value: float = 1.0) -> Lin:
    """Hue 0.0-1.0 (wrapping) as a fully bright sRGB colour, in linear light."""
    red, green, blue = colorsys.hsv_to_rgb(hue % 1.0, saturation, value)
    return (_decode(red), _decode(green), _decode(blue))


def hsv_srgb(hue: float, saturation: float = 1.0) -> core.Color:
    red, green, blue = colorsys.hsv_to_rgb(hue % 1.0, saturation, 1.0)
    return (int(round(red * 255)), int(round(green * 255)), int(round(blue * 255)))


def mix(a: Lin, b: Lin, k: float) -> Lin:
    k = min(max(k, 0.0), 1.0)
    return tuple(x + (y - x) * k for x, y in zip(a, b))


def scale(a: Lin, k: float) -> Lin:
    k = min(max(k, 0.0), 1.0)
    return tuple(x * k for x in a)


def gradient(colors: list[Lin], position: float) -> Lin:
    """Colour at `position` 0.0-1.0 along a gradient through `colors`."""
    if len(colors) == 1:
        return colors[0]
    position = min(max(position, 0.0), 1.0) * (len(colors) - 1)
    index = min(int(position), len(colors) - 2)
    return mix(colors[index], colors[index + 1], position - index)


def smooth(x: float) -> float:
    """0 -> 1 -> 0 over one unit of x, cosine shaped (no corners)."""
    return 0.5 - 0.5 * math.cos(TAU * x)


def period(speed: float, base: float) -> float:
    """Seconds per cycle: `base` at speed 0.5, four times shorter at 1.0,
    four times longer at 0.0."""
    speed = min(max(speed, 0.0), 1.0)
    return base * 16.0 ** (0.5 - speed)


def noise(t: float, zone: int, salt: float = 0.0) -> float:
    """Smooth pseudo-random 0.0-1.0 that differs per zone: three sines with
    incommensurate frequencies and zone-dependent phases."""
    f1, f2, f3 = 1.7, 2.9, 5.3
    phase = zone * 1.618 + salt
    value = (math.sin(TAU * f1 * t + phase) * 0.5
             + math.sin(TAU * f2 * t + phase * 2.3) * 0.3
             + math.sin(TAU * f3 * t + phase * 3.7) * 0.2)
    return 0.5 + 0.5 * value


def hash01(*values: float) -> float:
    """Deterministic 0.0-1.0 from a few numbers (the classic sin hash)."""
    acc = 0.0
    for index, value in enumerate(values):
        acc += value * (12.9898 + 78.233 * index)
    return math.sin(acc) * 43758.5453 % 1.0


def zone_offset(zone: int, direction: str, span: float = ZONES) -> float:
    """Phase offset so a travelling pattern moves from zone 1 to 4 for "ltr"
    (left to right as the user sits) and back for "rtl". `span` is how many
    zone widths one period covers: ZONES wraps seamlessly (the rainbow), more
    than ZONES lets a band leave one edge before it enters the other."""
    offset = zone / span
    return -offset if direction == "ltr" else offset


# ------------------------------------------------------------- parameters

# Every parameter an effect may use, with its type and default. Effects
# declare which ones they use; the daemon keeps one params dict for all
# effects, so switching effects keeps the colours you picked.
PARAMS: dict[str, dict] = {
    "color":      {"type": "color", "default": "#FFFFFF", "label": "Colour"},
    "color2":     {"type": "color", "default": "#000000", "label": "Second colour"},
    "colors":     {"type": "colors", "default": ["#FF4000", "#FFC000", "#00C0FF", "#8000FF"],
                   "label": "Zone colours"},
    "palette":    {"type": "colors", "default": ["#00FF80", "#2040FF", "#8000FF"],
                   "label": "Palette"},
    "kelvin":     {"type": "kelvin", "default": core.DEFAULT_KELVIN,
                   "min": core.KELVIN_MIN, "max": core.KELVIN_MAX, "label": "Temperature"},
    "speed":      {"type": "range", "default": 0.5, "min": 0.0, "max": 1.0, "label": "Speed"},
    "direction":  {"type": "choice", "default": "ltr", "choices": ["ltr", "rtl"],
                   "label": "Direction"},
    "floor":      {"type": "range", "default": 0.08, "min": 0.0, "max": 0.9, "label": "Minimum"},
    "saturation": {"type": "range", "default": 1.0, "min": 0.0, "max": 1.0, "label": "Saturation"},
    "flicker":    {"type": "range", "default": 0.5, "min": 0.0, "max": 1.0, "label": "Flicker"},
    "role":       {"type": "choice", "default": "primary",
                   "choices": ["primary", "secondary", "tertiary", "error"],
                   "label": "Scheme colour"},
    "color_caps": {"type": "color", "default": "#FF4000", "label": "Caps Lock colour"},
    "color_num":  {"type": "color", "default": "#00FF40", "label": "Num Lock colour"},
    "kelvin2":    {"type": "kelvin", "default": 3000.0,
                   "min": core.KELVIN_MIN, "max": core.KELVIN_MAX, "label": "Night temperature"},
}

# What the daemon knows about the outside world, with the values an effect
# sees when a source is missing (no battery, no shell scheme).
SENSOR_DEFAULTS: dict = {
    "caps": False,          # Caps Lock LED on
    "num": False,           # Num Lock LED on
    "battery": None,        # 0-100, or None without a battery
    "charging": False,      # on mains power
    "scheme": {},           # role -> "#rrggbb" from the shell's colour scheme
    "hour": 12.0,           # local time of day, 0.0-24.0
}


def _clean_color(value, strict: bool) -> str | None:
    try:
        return core.to_hex(core.parse_hex(str(value)))
    except ValueError:
        if strict:
            raise ValueError("not a colour: %r" % (value,))
        return None


def _clean_colors(value, strict: bool) -> list[str] | None:
    if isinstance(value, str):
        value = [part for part in value.split(",") if part.strip()]
    if not isinstance(value, (list, tuple)) or not 1 <= len(value) <= 8:
        if strict:
            raise ValueError("expected 1-8 colours, got %r" % (value,))
        return None
    cleaned = [_clean_color(item, strict) for item in value]
    if any(item is None for item in cleaned):
        return None
    return cleaned


def clean_params(params, strict: bool = False) -> dict:
    """Validate a params dict. Unknown keys are dropped; bad values raise
    ValueError when strict, else are dropped. Only given keys are returned."""
    if not isinstance(params, dict):
        if strict:
            raise ValueError("params must be an object")
        return {}
    cleaned: dict = {}
    for key, value in params.items():
        spec = PARAMS.get(key)
        if spec is None:
            if strict:
                raise ValueError("unknown parameter %r" % key)
            continue
        kind = spec["type"]
        try:
            if kind == "color":
                result = _clean_color(value, strict)
            elif kind == "colors":
                result = _clean_colors(value, strict)
            elif kind in ("kelvin", "range"):
                number = float(value)
                if math.isnan(number):
                    raise ValueError("nan")
                result = min(max(number, spec["min"]), spec["max"])
            elif kind == "choice":
                if value not in spec["choices"]:
                    raise ValueError("%r must be one of %r" % (key, spec["choices"]))
                result = value
            else:
                result = None
        except (TypeError, ValueError) as error:
            if strict:
                raise ValueError("bad value for %r: %s" % (key, error)) from None
            result = None
        if result is not None:
            cleaned[key] = result
    return cleaned


def full_params(params: dict) -> dict:
    """The given params over the defaults."""
    merged = {key: spec["default"] for key, spec in PARAMS.items()}
    merged.update(params)
    return merged


# ------------------------------------------------------------- effects


class Effect:
    def __init__(self, name: str, label: str, moving: bool, uses: tuple[str, ...],
                 render, palette=None, reactive: bool = False) -> None:
        self.name = name
        self.label = label
        self.moving = moving
        self.reactive = reactive
        self.uses = uses
        self._render = render
        self._palette = palette

    def render(self, t: float, p: dict, s: dict | None = None) -> list[Lin]:
        """Four linear-light colours for time t (seconds), full params p and
        the sensor snapshot s (SENSOR_DEFAULTS when not given)."""
        return self._render(t, p, s if s is not None else SENSOR_DEFAULTS)

    def palette(self, p: dict) -> list[core.Color]:
        """The sRGB colours this effect shows at full intensity: what the
        max-brightness boost must not clip."""
        if self._palette is not None:
            return self._palette(p)
        return [core.parse_hex(p["color"])]

    def describe(self) -> dict:
        return {
            "name": self.name, "label": self.label, "moving": self.moving,
            "reactive": self.reactive,
            "params": [dict(PARAMS[key], name=key) for key in self.uses],
        }


REGISTRY: dict[str, Effect] = {}
ORDER: list[str] = []


def _effect(name: str, label: str, moving: bool, uses: tuple[str, ...], palette=None,
            reactive: bool = False):
    def register(render):
        REGISTRY[name] = Effect(name, label, moving, uses, render, palette, reactive)
        ORDER.append(name)
        return render
    return register


def get(name: str) -> Effect:
    return REGISTRY[name]


def describe() -> list[dict]:
    return [REGISTRY[name].describe() for name in ORDER]


_HUES = [hsv_srgb(i / 12.0) for i in range(12)]


def _four(colors: list[str]) -> list[Lin]:
    """Expand 1-8 colours to one per zone: four as given, otherwise spread
    evenly as a gradient across the zones."""
    stops = [lin_hex(c) for c in colors]
    if len(stops) == ZONES:
        return stops
    return [gradient(stops, zone / (ZONES - 1)) for zone in range(ZONES)]


# --- still


@_effect("white", "White", False, ("kelvin",),
         palette=lambda p: [core.kelvin_to_srgb(p["kelvin"])])
def _white(t, p, s):
    return [lin(core.kelvin_to_srgb(p["kelvin"]))] * ZONES


@_effect("static", "Static colour", False, ("color",))
def _static(t, p, s):
    return [lin_hex(p["color"])] * ZONES


@_effect("gradient", "Gradient", False, ("colors",),
         palette=lambda p: [core.parse_hex(c) for c in p["colors"]])
def _gradient(t, p, s):
    return _four(p["colors"])


# --- breathing


@_effect("breathe", "Breathe", True, ("color", "speed", "floor"))
def _breathe(t, p, s):
    k = p["floor"] + (1.0 - p["floor"]) * smooth(t / period(p["speed"], 4.0))
    return [scale(lin_hex(p["color"]), k)] * ZONES


@_effect("breathe2", "Breathe, two colours", True, ("color", "color2", "speed"),
         palette=lambda p: [core.parse_hex(p["color"]), core.parse_hex(p["color2"])])
def _breathe2(t, p, s):
    k = smooth(t / period(p["speed"], 6.0))
    return [mix(lin_hex(p["color"]), lin_hex(p["color2"]), k)] * ZONES


@_effect("heartbeat", "Heartbeat", True, ("color", "speed"))
def _heartbeat(t, p, s):
    u = (t / period(p["speed"], 1.6)) % 1.0
    beat = math.exp(-((u - 0.10) / 0.06) ** 2) + 0.8 * math.exp(-((u - 0.30) / 0.07) ** 2)
    return [scale(lin_hex(p["color"]), 0.05 + 0.95 * min(1.0, beat))] * ZONES


@_effect("candle", "Candle", True, ("color", "flicker"))
def _candle(t, p, s):
    base = lin_hex(p["color"])
    return [scale(base, 1.0 - 0.7 * p["flicker"] * noise(t, zone)) for zone in range(ZONES)]


# --- movement


@_effect("wave", "Wave", True, ("color", "color2", "speed", "direction"),
         palette=lambda p: [core.parse_hex(p["color"]), core.parse_hex(p["color2"])])
def _wave(t, p, s):
    """A band of `color` circling the keyboard over `color2`, period of four
    zones, so it re-enters at zone 1 as it leaves zone 4. A longer period
    leaves a dark pause the eye notices, and a wider band reads as the whole
    keyboard blinking."""
    fg, bg = lin_hex(p["color"]), lin_hex(p["color2"])
    phase = t / period(p["speed"], 3.0)
    return [mix(bg, fg, smooth(phase + zone_offset(zone, p["direction"])) ** 2)
            for zone in range(ZONES)]


@_effect("rainbow", "Rainbow", True, ("speed", "direction", "saturation"),
         palette=lambda p: _HUES)
def _rainbow(t, p, s):
    phase = t / period(p["speed"], 8.0)
    return [hsv(phase + zone_offset(zone, p["direction"]), p["saturation"])
            for zone in range(ZONES)]


@_effect("hue", "Hue cycle", True, ("speed", "saturation"), palette=lambda p: _HUES)
def _hue(t, p, s):
    return [hsv(t / period(p["speed"], 10.0), p["saturation"])] * ZONES


@_effect("scanner", "Scanner", True, ("color", "color2", "speed"),
         palette=lambda p: [core.parse_hex(p["color"]), core.parse_hex(p["color2"])])
def _scanner(t, p, s):
    fg, bg = lin_hex(p["color"]), lin_hex(p["color2"])
    u = (t / period(p["speed"], 2.0)) % 1.0
    position = (ZONES - 1) * (2.0 * u if u < 0.5 else 2.0 - 2.0 * u)
    return [mix(bg, fg, max(0.0, 1.0 - abs(zone - position)) ** 2) for zone in range(ZONES)]


@_effect("comet", "Comet", True, ("color", "color2", "speed", "direction"),
         palette=lambda p: [core.parse_hex(p["color"]), core.parse_hex(p["color2"])])
def _comet(t, p, s):
    fg, bg = lin_hex(p["color"]), lin_hex(p["color2"])
    travel = ZONES + 3.0  # off one edge, across, off the other
    head = ((t / period(p["speed"], 2.5)) % 1.0) * travel - 1.5
    frame = []
    for zone in range(ZONES):
        index = zone if p["direction"] == "ltr" else ZONES - 1 - zone
        behind = head - index
        w = math.exp(-behind / 1.0) if behind >= 0 else max(0.0, 1.0 + behind * 2.0)
        frame.append(mix(bg, fg, w))
    return frame


@_effect("ripple", "Ripple", True, ("color", "color2", "speed"),
         palette=lambda p: [core.parse_hex(p["color"]), core.parse_hex(p["color2"])])
def _ripple(t, p, s):
    fg, bg = lin_hex(p["color"]), lin_hex(p["color2"])
    radius = ((t / period(p["speed"], 3.0)) % 1.0) * 3.0
    centre = (ZONES - 1) / 2.0
    return [mix(bg, fg, max(0.0, 1.0 - abs(abs(zone - centre) - radius) / 0.9) ** 2)
            for zone in range(ZONES)]


@_effect("sparkle", "Sparkle", True, ("color", "color2", "speed"),
         palette=lambda p: [core.parse_hex(p["color"]), core.parse_hex(p["color2"])])
def _sparkle(t, p, s):
    fg, bg = lin_hex(p["color"]), lin_hex(p["color2"])
    slot = period(p["speed"], 1.2)
    frame = []
    for zone in range(ZONES):
        local = t / slot + zone * 0.37
        lit = hash01(math.floor(local), zone) < 0.35
        frame.append(mix(bg, fg, smooth(local % 1.0) if lit else 0.0))
    return frame


@_effect("aurora", "Aurora", True, ("palette", "speed"),
         palette=lambda p: [core.parse_hex(c) for c in p["palette"]])
def _aurora(t, p, s):
    stops = [lin_hex(c) for c in p["palette"]]
    slow = period(p["speed"], 12.0)
    frame = []
    for zone in range(ZONES):
        a = 0.5 + 0.5 * math.sin(TAU * t / slow + zone * 1.9)
        b = 0.5 + 0.5 * math.sin(TAU * t / (slow * 1.61) + zone * 0.7 + 1.3)
        frame.append(scale(gradient(stops, 0.6 * a + 0.4 * b), 0.6 + 0.4 * b))
    return frame


@_effect("blink", "Blink", True, ("color", "speed"))
def _blink(t, p, s):
    on = ((t / period(p["speed"], 2.0)) % 1.0) < 0.5
    return [lin_hex(p["color"]) if on else (0.0, 0.0, 0.0)] * ZONES


# --- reactive: the snapshot decides, time only smooths

_SCHEME_FALLBACK = "#B0C8EC"  # the primary of a blue Material scheme


def _scheme_colour(p: dict, s: dict) -> str:
    return s.get("scheme", {}).get(p["role"]) or _SCHEME_FALLBACK


@_effect("system", "System colour", False, ("role",),
         palette=lambda p: [core.parse_hex(_SCHEME_FALLBACK)], reactive=True)
def _system(t, p, s):
    """The shell's Material colour, so the keyboard follows the wallpaper."""
    return [lin_hex(_scheme_colour(p, s))] * ZONES


@_effect("locks", "Caps / Num Lock", False, ("color", "color_caps", "color_num"),
         palette=lambda p: [core.parse_hex(p[k]) for k in ("color", "color_caps", "color_num")],
         reactive=True)
def _locks(t, p, s):
    """Zone 1 (Esc side) in its own colour while Caps Lock is on, zone 4 (the
    numeric block) while Num Lock is on, the base colour elsewhere."""
    frame = [lin_hex(p["color"])] * ZONES
    if s.get("caps"):
        frame[0] = lin_hex(p["color_caps"])
    if s.get("num"):
        frame[ZONES - 1] = lin_hex(p["color_num"])
    return frame


_BATTERY_STOPS = [lin_hex("#FF2000"), lin_hex("#FFB000"), lin_hex("#30FF40")]


@_effect("battery", "Battery", True, (),
         palette=lambda p: [core.parse_hex(c) for c in ("#FF2000", "#FFB000", "#30FF40")],
         reactive=True)
def _battery(t, p, s):
    """Charge as a bar from zone 1: a quarter per zone, the colour from red
    (empty) through amber to green (full). The zone being filled breathes
    while charging; below 15% on battery the whole keyboard breathes red."""
    level = s.get("battery")
    if level is None:
        return [BLACK_LIN] * ZONES
    fraction = min(max(level / 100.0, 0.0), 1.0)
    colour = gradient(_BATTERY_STOPS, fraction)
    pulse = 0.4 + 0.6 * smooth(t / 3.0)
    if fraction <= 0.15 and not s.get("charging"):
        return [scale(_BATTERY_STOPS[0], pulse)] * ZONES
    frame = []
    for zone in range(ZONES):
        fill = min(max(fraction * ZONES - zone, 0.0), 1.0)
        if s.get("charging") and 0.0 < fill < 1.0:
            fill *= pulse
        elif s.get("charging") and fill == 0.0 and zone == int(fraction * ZONES):
            fill = 0.3 * pulse  # the next zone to fill breathes faintly
        frame.append(scale(colour, max(fill, 0.04)))
    return frame


@_effect("daylight", "Time of day", False, ("kelvin", "kelvin2"),
         palette=lambda p: [core.kelvin_to_srgb(p["kelvin"]), core.kelvin_to_srgb(p["kelvin2"])],
         reactive=True)
def _daylight(t, p, s):
    """White that follows the clock: `kelvin` at 13:00, `kelvin2` at 01:00,
    a cosine in between (no steps at sunset)."""
    hour = s.get("hour", 12.0)
    day = 0.5 - 0.5 * math.cos(TAU * (hour - 1.0) / 24.0)
    kelvin = p["kelvin2"] + (p["kelvin"] - p["kelvin2"]) * day
    return [lin(core.kelvin_to_srgb(kelvin))] * ZONES
