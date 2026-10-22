#!/usr/bin/env python3
"""keyboard-konf daemon: the one process that owns the lighting controller.

Why a daemon: the controller cannot be read back and only knows four
built-in effects, so anything beyond a static colour is a process that sends
a new frame many times a second, and anything that should survive a reboot
or a suspend is a process that remembers what it last sent.

What it does:
  - keeps the state (on/off, effect, parameters, brightness, max mode) in
    ~/.config/keyboard-konf/state.json and re-applies it at start and after
    a resume from sleep (logind's PrepareForSleep on the system bus);
  - keeps named presets and the user's own colours in presets.json;
  - renders moving effects at a fixed frame rate, still ones once, and the
    reactive ones (lock keys, battery, the shell's colour scheme, the clock)
    whenever what they look at changes;
  - crossfades every change over FADE_SECONDS in linear light;
  - answers clients on a Unix socket (kkonf_ipc), the CLI and the shell
    section among them, and tells every client about each change;
  - reloads the white balance when calibration.json changes.

It writes the same feature report the CLI always wrote (kkonf_core) and
nothing else. Run it with `kkonf daemon`; stop it with SIGTERM or Ctrl-C.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import signal
import socket
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import gi  # noqa: E402

gi.require_version("GLib", "2.0")
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

try:  # PyGObject >= 3.50 moved the Unix-only calls to their own namespace
    gi.require_version("GLibUnix", "2.0")
    from gi.repository import GLibUnix  # noqa: E402
    signal_add = GLibUnix.signal_add
except (ValueError, ImportError, AttributeError):
    signal_add = GLib.unix_signal_add

import kkonf_core as core  # noqa: E402
import kkonf_effects as effects  # noqa: E402
import kkonf_ipc as ipc  # noqa: E402

STATE_FILE = os.path.join(core.CONFIG_DIR, "state.json")
PRESETS_FILE = os.path.join(core.CONFIG_DIR, "presets.json")
SCHEME_FILE = os.path.join(
    os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"),
    "caelestia", "scheme.json")
SENSOR_INTERVAL = 0.2  # seconds between looks at the LEDs and the battery

DEFAULT_FPS = 25
FADE_SECONDS = 0.3
RESUME_DELAY = 1.5     # seconds between "resumed" and the first write
RETRY_DELAY = 2.0      # seconds between attempts after a failed write

DEFAULT_STATE = {
    "on": True,
    "effect": "white",
    "params": {},
    "brightness": 1.0,
    "max": False,
}

BLACK: effects.Lin = (0.0, 0.0, 0.0)

# What presets.json starts with. All of it is the user's to change or delete.
DEFAULT_PRESETS = {
    "presets": {
        "Daily white": {"effect": "white", "params": {"kelvin": 5000}, "brightness": 1.0},
        "Night": {"effect": "candle", "params": {"color": "#FF9040", "flicker": 0.4},
                  "brightness": 0.4},
        "Ocean": {"effect": "wave", "params": {"color": "#00C0FF", "color2": "#001030",
                                               "speed": 0.5, "direction": "ltr"}},
        "Rainbow": {"effect": "rainbow", "params": {"speed": 0.4}},
    },
    "colours": ["#1818FF"],
}


def log(message: str) -> None:
    print(time.strftime("%H:%M:%S"), message, file=sys.stderr, flush=True)


# ------------------------------------------------------------------ state


def normalise(state, strict: bool = False) -> dict:
    """A complete, valid state from whatever was given.

    strict=True raises ValueError on anything invalid (for client input);
    otherwise invalid parts fall back to defaults (for the file on disk).
    """
    if not isinstance(state, dict):
        if strict:
            raise ValueError("state must be an object")
        state = {}
    result = dict(DEFAULT_STATE)
    result["params"] = {}

    if "on" in state:
        if strict and not isinstance(state["on"], bool):
            raise ValueError("'on' must be true or false")
        result["on"] = bool(state["on"])
    if "max" in state:
        if strict and not isinstance(state["max"], bool):
            raise ValueError("'max' must be true or false")
        result["max"] = bool(state["max"])

    effect = state.get("effect", result["effect"])
    if effect not in effects.REGISTRY:
        if strict:
            raise ValueError("unknown effect %r" % (effect,))
        effect = DEFAULT_STATE["effect"]
    result["effect"] = effect

    if "brightness" in state:
        try:
            value = float(state["brightness"])
        except (TypeError, ValueError):
            if strict:
                raise ValueError("'brightness' must be a number 0-1") from None
            value = 1.0
        result["brightness"] = min(max(value, 0.0), 1.0)

    result["params"] = effects.clean_params(state.get("params", {}), strict)
    return result


def load_state() -> dict:
    try:
        with open(STATE_FILE) as handle:
            return normalise(json.load(handle))
    except (OSError, ValueError):
        return normalise({})


def _write_json(path: str, data) -> None:
    os.makedirs(core.CONFIG_DIR, exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")
    os.replace(temporary, path)


def save_state(state: dict) -> None:
    _write_json(STATE_FILE, state)


def clean_colours(value) -> list[str]:
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        try:
            colour = core.to_hex(core.parse_hex(str(item)))
        except ValueError:
            continue
        if colour not in result:
            result.append(colour)
    return result[:64]


def load_presets() -> dict:
    """{"presets": {name: state}, "colours": [hex]}; the defaults when the
    file is missing, whatever is valid when it is damaged."""
    try:
        with open(PRESETS_FILE) as handle:
            data = json.load(handle)
    except FileNotFoundError:
        data = json.loads(json.dumps(DEFAULT_PRESETS))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    presets = data.get("presets")
    if not isinstance(presets, dict):
        presets = {}
    return {
        "presets": {str(name)[:64]: normalise(state) for name, state in presets.items()
                    if isinstance(state, dict)},
        "colours": clean_colours(data.get("colours")),
    }


def save_presets(data: dict) -> None:
    _write_json(PRESETS_FILE, data)


# ---------------------------------------------------------------- sensors


class Sensors:
    """What the reactive effects look at: the lock-key LEDs and the battery
    from sysfs (read at most every SENSOR_INTERVAL seconds, each read is a
    few microseconds), the shell's colour scheme from its file (reloaded
    when the daemon is told it changed) and the clock."""

    ROLES = ("primary", "secondary", "tertiary", "error")

    def __init__(self) -> None:
        self.values = dict(effects.SENSOR_DEFAULTS)
        self.read_at = -1.0
        self.caps_paths = self._leds("capslock")
        self.num_paths = self._leds("numlock")
        batteries = sorted(glob.glob("/sys/class/power_supply/BAT*"))
        self.battery_dir = batteries[0] if batteries else None
        self.load_scheme()

    @staticmethod
    def _leds(name: str) -> list[str]:
        return sorted(glob.glob("/sys/class/leds/input*::%s/brightness" % name))

    @staticmethod
    def _read(path: str) -> str | None:
        try:
            with open(path) as handle:
                return handle.read().strip()
        except OSError:
            return None

    def load_scheme(self) -> None:
        scheme: dict = {}
        try:
            with open(SCHEME_FILE) as handle:
                colours = json.load(handle).get("colours", {})
            for role in self.ROLES:
                value = colours.get(role)
                if isinstance(value, str):
                    scheme[role] = core.to_hex(core.parse_hex("#" + value.lstrip("#")))
        except (OSError, ValueError, AttributeError):
            pass
        self.values["scheme"] = scheme

    def snapshot(self, now: float) -> dict:
        if now - self.read_at >= SENSOR_INTERVAL:
            self.read_at = now
            values = self.values
            values["caps"] = any(self._read(path) == "1" for path in self.caps_paths)
            values["num"] = any(self._read(path) == "1" for path in self.num_paths)
            if self.battery_dir is not None:
                capacity = self._read(os.path.join(self.battery_dir, "capacity"))
                values["battery"] = int(capacity) if capacity and capacity.isdigit() else None
                status = self._read(os.path.join(self.battery_dir, "status"))
                values["charging"] = status in ("Charging", "Full", "Not charging")
            local = time.localtime()
            values["hour"] = local.tm_hour + local.tm_min / 60.0 + local.tm_sec / 3600.0
        return self.values


# ----------------------------------------------------------------- daemon


class Daemon:
    def __init__(self, fps: int, log_input: bool) -> None:
        self.fps = fps
        self.log_input = log_input
        self.state = load_state()
        self.presets = load_presets()
        self.sensors = Sensors()
        self.gains = core.current_gains()
        self.fixed_boost = core.load_boost()
        self.device: str | None = None
        self.last_bytes: list[core.Color] | None = None
        self.last_out: list[effects.Lin] | None = None
        self.fade_from: list[effects.Lin] | None = None
        self.fade_start = 0.0
        self.ticking = False
        self.retry_pending = False
        self.t0 = time.monotonic()
        self.server: socket.socket | None = None
        self.socket_path = ipc.socket_path()
        self.clients: dict[int, dict] = {}
        self.pending_broadcast = False
        self.pending_presets = False
        self.loop = GLib.MainLoop()
        self._bus = None
        self._monitor = None
        self._scheme_monitor = None

    # --- rendering ---------------------------------------------------

    def _scale(self, effect: effects.Effect, params: dict) -> float:
        if self.state["max"]:
            palette = effect.palette(params)
            boost = core.max_boost(palette, self._palette_gains(len(palette)))
        else:
            boost = self.fixed_boost
        return self.state["brightness"] * boost

    def _palette_gains(self, count: int) -> list[core.Gains]:
        """max_boost pairs each colour with a zone's gains; a palette colour
        may land on any zone, so judge each colour against the strongest
        gains of all zones (the worst case for clipping)."""
        strongest = tuple(max(g[channel] for g in self.gains) for channel in range(3))
        return [strongest] * count

    def _render(self, now: float) -> list[effects.Lin]:
        if not self.state["on"]:
            return [BLACK] * core.ZONE_COUNT
        effect = effects.get(self.state["effect"])
        params = effects.full_params(self.state["params"])
        frame = effect.render(now - self.t0, params, self.sensors.snapshot(now))
        factor = self._scale(effect, params)
        return [
            tuple(min(1.0, channel * gain * factor) for channel, gain in zip(zone, zone_gains))
            for zone, zone_gains in zip(frame, self.gains)
        ]

    def _tick(self) -> bool:
        """Render and send one frame. Returns True while more frames are due."""
        now = time.monotonic()
        out = self._render(now)
        fading = False
        if self.fade_from is not None:
            k = (now - self.fade_start) / FADE_SECONDS
            if k >= 1.0:
                self.fade_from = None
            else:
                fading = True
                out = [effects.mix(a, b, k) for a, b in zip(self.fade_from, out)]
        self.last_out = out
        data = [tuple(core.linear_to_duty(channel) for channel in zone) for zone in out]
        if data != self.last_bytes:
            self._write(data)
        effect = effects.get(self.state["effect"])
        moving = self.state["on"] and (effect.moving or effect.reactive)
        return moving or fading

    def _tick_timer(self) -> bool:
        if self._tick():
            return True
        self.ticking = False
        return False

    def kick(self) -> None:
        """Send a frame now and keep the frame timer running while needed."""
        if self.ticking:
            return
        if self._tick():
            self.ticking = True
            GLib.timeout_add(int(round(1000.0 / self.fps)), self._tick_timer)

    def _write(self, data: list[core.Color]) -> None:
        report = core.build_report(data, core.BRIGHTNESS_DEFAULT)
        try:
            if self.device is None:
                self.device = core.find_device()
                log("device %s" % self.device)
            core.write_report(self.device, report)
            self.last_bytes = data
        except (OSError, core.DeviceNotFound) as error:
            self.device = None
            self.last_bytes = None
            if not self.retry_pending:
                log("write failed: %s; retrying in %.0f s" % (error, RETRY_DELAY))
                self.retry_pending = True
                GLib.timeout_add(int(RETRY_DELAY * 1000), self._retry)

    def _retry(self) -> bool:
        self.retry_pending = False
        self.kick()
        return False

    def force_rewrite(self) -> None:
        """Forget what the device shows (after sleep, a new calibration, a
        reconnect) and send the current frame again."""
        self.last_bytes = None
        self.device = None
        self.kick()

    # --- state changes -----------------------------------------------

    def refresh(self) -> None:
        """Something an effect looks at changed: fade to the new frame."""
        if self.last_out is not None:
            self.fade_from = list(self.last_out)
            self.fade_start = time.monotonic()
        self.kick()

    def change(self, new_state: dict) -> None:
        if self.last_out is not None:
            self.fade_from = list(self.last_out)
            self.fade_start = time.monotonic()
        self.state = new_state
        try:
            save_state(self.state)
        except OSError as error:
            log("cannot save state: %s" % error)
        self.pending_broadcast = True
        self.kick()

    def handle(self, message: dict) -> dict:
        command = message.get("cmd")
        if command == "ping":
            return {"ok": True}
        if command == "get":
            return {"ok": True, "state": self.state}
        if command == "apply":
            # Send the current frame again: the firmware's own Fn+Space
            # handling replaces what we last wrote and tells nobody.
            self.force_rewrite()
            return {"ok": True, "state": self.state}
        if command == "effects":
            return {"ok": True, "effects": effects.describe()}
        if command == "set":
            patch = message.get("state")
            if not isinstance(patch, dict):
                return {"ok": False, "error": "'set' needs a 'state' object"}
            merged = dict(self.state)
            merged["params"] = dict(self.state["params"])
            if "params" in patch:
                if not isinstance(patch["params"], dict):
                    return {"ok": False, "error": "'params' must be an object"}
                merged["params"].update(patch["params"])
            for key in ("on", "effect", "brightness", "max"):
                if key in patch:
                    merged[key] = patch[key]
            try:
                new_state = normalise(merged, strict=True)
            except ValueError as error:
                return {"ok": False, "error": str(error)}
            self.change(new_state)
            return {"ok": True, "state": self.state}
        if command == "toggle":
            self.change(normalise(dict(self.state, on=not self.state["on"])))
            return {"ok": True, "state": self.state}
        if command == "next":
            order = effects.ORDER
            index = (order.index(self.state["effect"]) + 1) % len(order)
            self.change(normalise(dict(self.state, effect=order[index], on=True)))
            return {"ok": True, "state": self.state}
        if command == "presets":
            return self._presets_reply()
        if command in ("preset", "save_preset", "delete_preset"):
            name = message.get("name")
            if not isinstance(name, str) or not name.strip():
                return {"ok": False, "error": "'%s' needs a 'name'" % command}
            name = name.strip()[:64]
            presets = self.presets["presets"]
            if command == "preset":
                if name not in presets:
                    return {"ok": False, "error": "no preset %r" % name}
                self.change(normalise(dict(presets[name], on=True)))
                return {"ok": True, "state": self.state}
            if command == "save_preset":
                presets[name] = normalise(dict(self.state, on=True))
            else:
                if name not in presets:
                    return {"ok": False, "error": "no preset %r" % name}
                del presets[name]
            return self._presets_changed()
        if command in ("add_colour", "remove_colour"):
            try:
                colour = core.to_hex(core.parse_hex(str(message.get("color"))))
            except ValueError:
                return {"ok": False, "error": "'%s' needs a 'color' like #rrggbb" % command}
            colours = self.presets["colours"]
            if command == "add_colour":
                if colour in colours:
                    colours.remove(colour)
                colours.insert(0, colour)
                del colours[64:]
            elif colour in colours:
                colours.remove(colour)
            return self._presets_changed()
        return {"ok": False, "error": "unknown command %r" % (command,)}

    def _presets_reply(self) -> dict:
        return {"ok": True, "presets": self.presets["presets"], "colours": self.presets["colours"]}

    def _presets_changed(self) -> dict:
        try:
            save_presets(self.presets)
        except OSError as error:
            log("cannot save presets: %s" % error)
        self.pending_presets = True
        return self._presets_reply()

    # --- socket server -----------------------------------------------

    def start_server(self) -> None:
        path = self.socket_path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.connect(path)
        except OSError:
            pass
        else:
            probe.close()
            raise SystemExit("another daemon already answers on %s" % path)
        finally:
            probe.close()
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(path)
        os.chmod(path, 0o600)
        server.listen(8)
        server.setblocking(False)
        self.server = server
        GLib.io_add_watch(server.fileno(), GLib.PRIORITY_DEFAULT, GLib.IOCondition.IN,
                          self._on_accept)
        log("listening on %s" % path)

    def _on_accept(self, fd: int, condition) -> bool:
        assert self.server is not None
        try:
            conn, _ = self.server.accept()
        except (BlockingIOError, InterruptedError):
            return True
        conn.setblocking(False)
        client = {"sock": conn, "buffer": ipc.LineBuffer()}
        flags = GLib.IOCondition.IN | GLib.IOCondition.HUP | GLib.IOCondition.ERR
        GLib.io_add_watch(conn.fileno(), GLib.PRIORITY_DEFAULT, flags, self._on_client, client)
        self.clients[conn.fileno()] = client
        return True

    def _drop(self, client: dict) -> None:
        sock = client["sock"]
        self.clients.pop(sock.fileno(), None)
        try:
            sock.close()
        except OSError:
            pass

    def _send(self, client: dict, message: dict) -> None:
        try:
            client["sock"].sendall(ipc.encode(message))
        except OSError:
            self._drop(client)

    def _on_client(self, fd: int, condition, client: dict) -> bool:
        if not condition & GLib.IOCondition.IN:
            self._drop(client)
            return False
        try:
            chunk = client["sock"].recv(65536)
        except (BlockingIOError, InterruptedError):
            return True
        except OSError:
            self._drop(client)
            return False
        if not chunk:
            self._drop(client)
            return False
        for message in client["buffer"].feed(chunk):
            reply = self.handle(message)
            self._send(client, reply)
            if self.pending_broadcast:
                self.pending_broadcast = False
                self.broadcast({"event": "state", "state": self.state})
            if self.pending_presets:
                self.pending_presets = False
                self.broadcast(dict(self._presets_reply(), event="presets"))
        return True

    def broadcast(self, event: dict) -> None:
        for client in list(self.clients.values()):
            self._send(client, event)

    # --- system events -----------------------------------------------

    def watch_sleep(self) -> None:
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
            bus.signal_subscribe("org.freedesktop.login1", "org.freedesktop.login1.Manager",
                                 "PrepareForSleep", "/org/freedesktop/login1", None,
                                 Gio.DBusSignalFlags.NONE, self._on_sleep)
            self._bus = bus
            log("watching logind for sleep and resume")
        except GLib.Error as error:
            log("no system bus (%s): the light is not restored after sleep" % error.message)

    def _on_sleep(self, connection, sender, path, interface, name, parameters) -> None:
        if parameters.unpack()[0]:
            log("suspending")
            return
        log("resumed; restoring in %.1f s" % RESUME_DELAY)
        GLib.timeout_add(int(RESUME_DELAY * 1000), self._after_resume)

    def _after_resume(self) -> bool:
        self.force_rewrite()
        return False

    def watch_calibration(self) -> None:
        try:
            gfile = Gio.File.new_for_path(core.CALIBRATION_FILE)
            self._monitor = gfile.monitor_file(Gio.FileMonitorFlags.NONE, None)
            self._monitor.connect("changed", self._on_calibration_changed)
        except GLib.Error as error:
            log("cannot watch calibration.json (%s)" % error.message)

    def _on_calibration_changed(self, *args) -> None:
        self.gains = core.current_gains()
        self.fixed_boost = core.load_boost()
        log("calibration reloaded")
        self.force_rewrite()

    def watch_scheme(self) -> None:
        """The shell rewrites scheme.json when the wallpaper or the scheme
        changes; the "system" effect follows it."""
        try:
            gfile = Gio.File.new_for_path(SCHEME_FILE)
            self._scheme_monitor = gfile.monitor_file(Gio.FileMonitorFlags.NONE, None)
            self._scheme_monitor.connect("changed", self._on_scheme_changed)
        except GLib.Error as error:
            log("cannot watch %s (%s)" % (SCHEME_FILE, error.message))

    def _on_scheme_changed(self, *args) -> None:
        before = self.sensors.values.get("scheme")
        self.sensors.load_scheme()
        if self.sensors.values.get("scheme") != before:
            log("colour scheme reloaded")
            if self.state["on"] and effects.get(self.state["effect"]).reactive:
                self.refresh()

    def watch_input(self) -> None:
        """Log every input report from the lighting interface (hotkey debugging)."""
        try:
            path = self.device or core.find_device()
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except (OSError, core.DeviceNotFound) as error:
            log("cannot read input reports: %s" % error)
            return
        GLib.io_add_watch(fd, GLib.PRIORITY_DEFAULT, GLib.IOCondition.IN, self._on_input)
        log("logging input reports from %s" % path)

    def _on_input(self, fd: int, condition) -> bool:
        try:
            data = os.read(fd, 64)
        except BlockingIOError:
            return True
        except OSError as error:
            log("input read failed: %s" % error)
            return False
        log("input report %d B: %s" % (len(data), core.hexdump(data)))
        return True

    # --- lifecycle ---------------------------------------------------

    def stop(self, *args) -> bool:
        log("stopping")
        self.loop.quit()
        return False

    def run(self) -> int:
        self.start_server()
        self.watch_sleep()
        self.watch_calibration()
        self.watch_scheme()
        if self.log_input:
            self.watch_input()
        signal_add(GLib.PRIORITY_HIGH, signal.SIGTERM, self.stop)
        signal_add(GLib.PRIORITY_HIGH, signal.SIGINT, self.stop)
        log("state: %s" % json.dumps(self.state, separators=(",", ":")))
        try:
            save_state(self.state)  # so the files exist from the first run
            if not os.path.exists(PRESETS_FILE):
                save_presets(self.presets)
        except OSError as error:
            log("cannot save state: %s" % error)
        self.kick()
        try:
            self.loop.run()
        finally:
            for client in list(self.clients.values()):
                self._drop(client)
            if self.server is not None:
                self.server.close()
            try:
                os.unlink(self.socket_path)
            except OSError:
                pass
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kkonfd",
                                     description="keyboard-konf lighting daemon")
    parser.add_argument("--fps", type=int, default=DEFAULT_FPS,
                        help="frames per second for moving effects (default %d)" % DEFAULT_FPS)
    parser.add_argument("--log-input", action="store_true",
                        help="log input reports from the lighting interface (hotkey debugging)")
    args = parser.parse_args(argv)
    if not 1 <= args.fps <= 60:
        parser.error("fps must be between 1 and 60")
    return Daemon(args.fps, args.log_input).run()


if __name__ == "__main__":
    sys.exit(main())
