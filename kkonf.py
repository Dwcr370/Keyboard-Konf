#!/usr/bin/env python3
"""keyboard-konf command line.

    kkonf white              white at 6500 K (the screen's white)
    kkonf white 4000         warm white; kkonf white 8000 is a cold one
    kkonf '#ff8000'          one colour on all four zones
    kkonf '#f00,#0f0,#00f,#fff' is NOT accepted: use six hex digits per zone,
                             zone 1 (Esc side) first
    kkonf off | on | toggle  off, back on, or whichever it is not
    kkonf effect wave --color '#00c0ff' --speed 0.7
                             a moving effect (needs the daemon); kkonf effects
                             lists them with their parameters
    kkonf next               the next effect in the cycle (for a hotkey)
    kkonf apply              write the current state again (Fn+Space switches the
                             controller to its own modes and tells nobody)
    kkonf preset Ocean       apply a saved preset; kkonf presets lists them
    kkonf preset Mine --save save what the daemon shows now under that name
    kkonf preset Mine --delete
    kkonf brightness 60      software brightness, 0-100%
    kkonf max on|off         the brightest level that keeps the hue
    kkonf status             what the daemon currently shows
    kkonf daemon             run the daemon in the foreground
    kkonf gui                the GTK window
    kkonf calibrate          the white-balance page of the GTK window

With the daemon running, colours go through it, so they are remembered,
crossfaded and restored after sleep. Without it, white, colours and off are
written to the device directly. --direct forces the latter.

Colours are screen colours: they go through the sRGB curve and the saved
white-balance gains before reaching the controller. --raw skips both.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import kkonf_core as core  # noqa: E402
import kkonf_ipc as ipc  # noqa: E402

UDEV_HINT = (
    "no permission to write to the lighting device; install the udev rule:\n"
    "  sudo cp udev/60-keyboard-konf.rules /etc/udev/rules.d/\n"
    "  sudo udevadm control --reload-rules && sudo udevadm trigger --subsystem-match=hidraw"
)

DAEMON_HINT = "the daemon is not running; start it with `kkonf daemon`"

DAEMON_ONLY = ("on", "toggle", "next", "apply", "effect", "effects", "brightness", "max",
               "status", "preset", "presets")


def parse_zones(text: str) -> list[core.Color]:
    """One '#rrggbb' for all zones, or four comma-separated ones."""
    parts = [part.strip() for part in text.split(",") if part.strip()]
    if len(parts) == 1:
        return [core.parse_hex(parts[0])] * core.ZONE_COUNT
    if len(parts) == core.ZONE_COUNT:
        return [core.parse_hex(part) for part in parts]
    raise ValueError("give one colour or exactly %d, got %d" % (core.ZONE_COUNT, len(parts)))


def describe(zones: list[core.Color], encoded: list[core.Color]) -> str:
    lines = []
    for index, (wanted, sent) in enumerate(zip(zones, encoded), start=1):
        lines.append("  zone %d: screen %s -> device %s"
                     % (index, core.to_hex(wanted), core.to_hex(sent)))
    return "\n".join(lines)


def print_state(state: dict, show_on: bool = True) -> None:
    """One line: on/off, effect, brightness, max, and the parameters this
    effect actually uses (the daemon keeps the others for when you switch back)."""
    import kkonf_effects as effects
    effect = effects.REGISTRY.get(state.get("effect"))
    uses = effect.uses if effect is not None else ()
    params = effects.full_params(state.get("params", {}))
    shown = ", ".join("%s=%s" % (key, ",".join(params[key]) if isinstance(params[key], list)
                                 else params[key])
                      for key in uses)
    print("%s  effect %s  brightness %d%%  max %s%s" % (
        ("on " if state.get("on") else "off") if show_on else "   ", state.get("effect"),
        round(float(state.get("brightness", 1.0)) * 100), "on" if state.get("max") else "off",
        ("  (%s)" % shown) if shown else ""))


def daemon_set(patch: dict) -> dict:
    """Send a 'set' and return the reply; raises ipc.DaemonUnavailable."""
    reply = ipc.request({"cmd": "set", "state": patch})
    if not reply.get("ok"):
        raise ValueError(reply.get("error", "the daemon refused the change"))
    return reply


def effect_params(args) -> dict:
    """Effect parameters from the command line options."""
    params: dict = {}
    if args.color is not None:
        params["color"] = args.color
    if args.color2 is not None:
        params["color2"] = args.color2
    if args.colors is not None:
        params["colors"] = [part.strip() for part in args.colors.split(",") if part.strip()]
    if args.palette is not None:
        params["palette"] = [part.strip() for part in args.palette.split(",") if part.strip()]
    if args.speed is not None:
        params["speed"] = args.speed
    if args.direction is not None:
        params["direction"] = args.direction
    for item in args.param or []:
        if "=" not in item:
            raise ValueError("--param wants name=value, got %r" % item)
        name, value = item.split("=", 1)
        params[name.strip()] = value.strip()
    return params


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="kkonf",
        description="Lenovo Legion 4-zone keyboard backlight.",
        epilog="examples: kkonf white | kkonf white 4000 | kkonf '#ff8000' | "
               "kkonf effect breathe --color '#ff8000' | kkonf off | kkonf status",
    )
    parser.add_argument("what",
                        help="white | #rrggbb | four comma-separated #rrggbb | off | on | toggle | "
                             "effect | effects | next | apply | preset | presets | brightness | max | "
                             "status | daemon | gui | calibrate")
    parser.add_argument("arg", nargs="?",
                        help="kelvin for 'white', the effect name for 'effect', the preset "
                             "name for 'preset', 0-100 for 'brightness', on/off for 'max'")
    direct = parser.add_argument_group("direct writes (no daemon)")
    direct.add_argument("--direct", action="store_true",
                        help="write to the device even if the daemon is running")
    direct.add_argument("--brightness", type=int, choices=core.BRIGHTNESS_LEVELS,
                        default=core.BRIGHTNESS_DEFAULT,
                        help="hardware level, 1 or 2 (default 2)")
    direct.add_argument("--boost", type=float, default=None,
                        help="brightness boost %.0f-%.0f (default: the saved one; 1 = balanced white)"
                             % (core.BOOST_MIN, core.BOOST_MAX))
    direct.add_argument("--raw", action="store_true",
                        help="send the bytes exactly as given: no gamma, no white balance")
    direct.add_argument("--dry-run", action="store_true",
                        help="print what would be sent and exit")
    parser.add_argument("--max", action="store_true",
                        help="push the colour to the brightest level that does not clip "
                             "(hue, saturation and zone balance kept)")
    eff = parser.add_argument_group("effect parameters (with the daemon)")
    eff.add_argument("--color", help="main colour, #rrggbb")
    eff.add_argument("--color2", help="second or background colour, #rrggbb")
    eff.add_argument("--colors", help="zone colours for 'gradient', comma-separated")
    eff.add_argument("--palette", help="colours for 'aurora', comma-separated")
    eff.add_argument("--speed", type=float, help="0 (slowest) to 1 (fastest)")
    eff.add_argument("--direction", choices=("ltr", "rtl"), help="ltr = from the Esc side")
    eff.add_argument("--param", action="append", metavar="NAME=VALUE",
                     help="any other parameter, e.g. --param floor=0.2")
    pre = parser.add_argument_group("presets (with the daemon)")
    pre.add_argument("--save", action="store_true",
                     help="with 'preset NAME': save the current state under NAME")
    pre.add_argument("--delete", action="store_true", help="with 'preset NAME': remove NAME")
    daemon = parser.add_argument_group("daemon")
    daemon.add_argument("--fps", type=int, default=None, help="frame rate for moving effects")
    daemon.add_argument("--log-input", action="store_true",
                        help="log the lighting interface's input reports (hotkey debugging)")
    args = parser.parse_args(argv)

    what = args.what.strip().lower()

    if what in ("gui", "calibrate"):
        import kkonf_gui  # GTK is only needed here
        return kkonf_gui.main(["--calibrate"] if what == "calibrate" else [])

    if what == "daemon":
        import kkonfd
        daemon_args = ["--log-input"] if args.log_input else []
        if args.fps is not None:
            daemon_args += ["--fps", str(args.fps)]
        return kkonfd.main(daemon_args)

    # ----- commands that only make sense with the daemon
    try:
        if what == "status":
            print_state(ipc.request({"cmd": "get"})["state"])
            return 0
        if what == "effects":
            for entry in ipc.request({"cmd": "effects"})["effects"]:
                params = ", ".join(p["name"] for p in entry["params"])
                kind = "reactive" if entry.get("reactive") else \
                    "moving  " if entry["moving"] else "still   "
                print("  %-10s %-28s %s %s" % (entry["name"], entry["label"], kind, params))
            return 0
        if what == "presets":
            reply = ipc.request({"cmd": "presets"})
            for name, state in reply["presets"].items():
                print("  %-16s" % (name + ":"), end="")
                print_state(state, show_on=False)
            if reply["colours"]:
                print("  my colours: " + " ".join(reply["colours"]))
            return 0
        if what == "preset":
            if not args.arg:
                parser.error("which preset? `kkonf presets` lists them")
            if args.save and args.delete:
                parser.error("--save or --delete, not both")
            command = "save_preset" if args.save else "delete_preset" if args.delete else "preset"
            reply = ipc.request({"cmd": command, "name": args.arg})
            if not reply.get("ok"):
                raise ValueError(reply.get("error", "the daemon refused"))
            if command == "preset":
                print_state(reply["state"])
            else:
                print("%s: %s" % ("saved" if args.save else "deleted", args.arg))
            return 0
        if what in ("toggle", "next", "apply"):
            print_state(ipc.request({"cmd": what})["state"])
            return 0
        if what == "on":
            print_state(daemon_set({"on": True})["state"])
            return 0
        if what == "brightness":
            try:
                percent = float(args.arg)
            except (TypeError, ValueError):
                parser.error("brightness wants a number 0-100")
            if not 0 <= percent <= 100:
                parser.error("brightness wants a number 0-100")
            print_state(daemon_set({"brightness": percent / 100.0})["state"])
            return 0
        if what == "max":
            if args.arg not in ("on", "off"):
                parser.error("max wants 'on' or 'off'")
            print_state(daemon_set({"max": args.arg == "on"})["state"])
            return 0
        if what == "effect":
            if not args.arg:
                parser.error("which effect? `kkonf effects` lists them")
            patch: dict = {"effect": args.arg.strip().lower(), "on": True}
            params = effect_params(args)
            if params:
                patch["params"] = params
            if args.max:
                patch["max"] = True
            print_state(daemon_set(patch)["state"])
            return 0
    except ipc.DaemonUnavailable as error:
        print("error: %s\n(%s)" % (DAEMON_HINT, error), file=sys.stderr)
        return 3
    except ValueError as error:
        print("error: %s" % error, file=sys.stderr)
        return 1

    # ----- white, colours, off: through the daemon when it runs, else direct
    if what == "off":
        zones = [(0, 0, 0)] * core.ZONE_COUNT
        patch = {"on": False}
    elif what == "white":
        kelvin = core.DEFAULT_KELVIN
        if args.arg is not None:
            try:
                kelvin = float(args.arg)
            except ValueError:
                parser.error("kelvin must be a number")
        if not core.KELVIN_MIN <= kelvin <= core.KELVIN_MAX:
            parser.error("kelvin must be between %d and %d" % (core.KELVIN_MIN, core.KELVIN_MAX))
        zones = [core.kelvin_to_srgb(kelvin)] * core.ZONE_COUNT
        patch = {"on": True, "effect": "white", "params": {"kelvin": kelvin}}
    else:
        if args.arg is not None:
            parser.error("a second argument only goes with white, effect, brightness or max")
        try:
            zones = parse_zones(args.what)
        except ValueError as error:
            parser.error(str(error))
        if len(set(zones)) == 1:
            patch = {"on": True, "effect": "static", "params": {"color": core.to_hex(zones[0])}}
        else:
            patch = {"on": True, "effect": "gradient",
                     "params": {"colors": [core.to_hex(zone) for zone in zones]}}
    if args.max:
        patch["max"] = True

    direct_only = args.direct or args.raw or args.dry_run or args.boost is not None \
        or args.brightness != core.BRIGHTNESS_DEFAULT
    if not direct_only:
        try:
            reply = daemon_set(patch)
        except ipc.DaemonUnavailable:
            pass  # no daemon: fall through to the direct write
        except ValueError as error:
            print("error: %s" % error, file=sys.stderr)
            return 1
        else:
            print("applied via the daemon")
            print_state(reply["state"])
            return 0

    if what == "off":
        encoded = zones
        report = core.build_off_report()
        gains = core.current_gains()
        boost = 1.0
    else:
        gains = core.current_gains()
        if args.boost is not None and not core.BOOST_MIN <= args.boost <= core.BOOST_MAX:
            parser.error("boost must be between %.0f and %.0f" % (core.BOOST_MIN, core.BOOST_MAX))
        if args.max:
            max_brightness = True
        elif args.boost is not None:
            max_brightness = False
        else:
            max_brightness = None  # the saved setting decides
        boost = core.effective_boost(zones, gains, args.boost, max_brightness)
        encoded = zones if args.raw else core.encode_zones(zones, gains, boost)
        report = core.build_report(encoded, args.brightness)

    if args.dry_run:
        print("report (%d bytes): %s" % (len(report), core.hexdump(report)))
        print(describe(zones, encoded))
        if not args.raw and what != "off":
            print("  gains per zone: " + "; ".join(
                "%d: %.3f %.3f %.3f" % (index, *gain) for index, gain in enumerate(gains, start=1)))
            fixed = args.boost if args.boost is not None else core.load_boost()
            print("  boost: %.2f%s" % (boost, " (max brightness)" if boost != fixed else ""))
        return 0

    try:
        device = core.find_device()
    except core.DeviceNotFound as error:
        print("error: %s" % error, file=sys.stderr)
        return 2
    try:
        core.write_report(device, report)
    except PermissionError:
        print("error: %s" % UDEV_HINT, file=sys.stderr)
        return 1
    except OSError as error:
        print("error: cannot write to %s: %s" % (device, error), file=sys.stderr)
        return 1
    print("applied to %s (direct)" % device)
    return 0


if __name__ == "__main__":
    sys.exit(main())
