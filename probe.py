#!/usr/bin/env python3
"""Probe for the Lenovo Legion 4-zone RGB keyboard (ITE 8295, USB 048d:c994).

Safe by default: without --write, nothing is ever sent to the device.

The keyboard exposes a vendor HID feature report, ID 0xCC, 32 bytes of payload
(confirmed by reading the device's own report descriptor). The open-source
reference implementations send 32 bytes total with 0xCC as the first byte,
while the descriptor implies 33 bytes total (report ID + 32 payload bytes).
That one-byte ambiguity is exactly what this probe resolves: --write tries the
33-byte form first, then falls back to the 32-byte form.
"""

from __future__ import annotations

import argparse
import fcntl
import glob
import os
import sys

VENDOR_ID = "048D"
PRODUCT_ID = "C994"

REPORT_ID = 0xCC
PAYLOAD_LEN = 32
HEADER = 0x16

EFFECTS = {"static": 1, "breath": 3, "wave": 4, "hue": 6}

COLORS = {
    "red": (255, 0, 0),
    "green": (0, 255, 0),
    "blue": (0, 0, 255),
    "white": (255, 255, 255),
    "yellow": (255, 255, 0),
    "cyan": (0, 255, 255),
    "magenta": (255, 0, 255),
    "orange": (255, 80, 0),
    "off": (0, 0, 0),
}


def _ioc(direction: int, type_: int, nr: int, size: int) -> int:
    return (direction << 30) | (size << 16) | (type_ << 8) | nr


def hidiocsfeature(size: int) -> int:
    return _ioc(3, ord("H"), 0x06, size)


def hidiocgfeature(size: int) -> int:
    return _ioc(3, ord("H"), 0x07, size)


def declares_cc_report(node: str) -> bool:
    """True if this hidraw node's descriptor declares feature report 0xCC."""
    path = "/sys/class/hidraw/%s/device/report_descriptor" % node
    try:
        with open(path, "rb") as handle:
            descriptor = handle.read()
    except OSError:
        return False
    return b"\x85\xcc" in descriptor


def find_devices() -> list[tuple[str, bool]]:
    """Return [(/dev/hidrawN, declares_cc_report), ...] matching our VID:PID."""
    matches = []
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
            if parts[1][-4:].upper() == VENDOR_ID and parts[2][-4:].upper() == PRODUCT_ID:
                node = uevent.split("/")[4]
                matches.append(("/dev/" + node, declares_cc_report(node)))
    return matches


def pick_device() -> str | None:
    """Prefer the node that actually declares report 0xCC."""
    for path, declares in find_devices():
        if declares:
            return path
    return None


def build_payload(effect: int, speed: int, brightness: int,
                  zones: list[tuple[int, int, int]],
                  wave_rtl: bool = False, wave_ltr: bool = False) -> bytearray:
    """Build the 32-byte payload that follows the report ID."""
    payload = bytearray(PAYLOAD_LEN)
    payload[0] = HEADER
    payload[1] = effect
    payload[2] = speed
    payload[3] = brightness
    for index, (red, green, blue) in enumerate(zones):
        payload[4 + index * 3] = red
        payload[5 + index * 3] = green
        payload[6 + index * 3] = blue
    payload[17] = 1 if wave_rtl else 0
    payload[18] = 1 if wave_ltr else 0
    return payload


def frame(payload: bytearray, length: int) -> bytearray:
    """Wrap the payload for ioctl: report ID first, truncated to `length`."""
    buffer = bytearray([REPORT_ID]) + payload
    return buffer[:length]


def hexdump(data: bytes) -> str:
    return " ".join("%02X" % byte for byte in data)


def send_feature(path: str, buffer: bytearray) -> None:
    descriptor = os.open(path, os.O_RDWR)
    try:
        fcntl.ioctl(descriptor, hidiocsfeature(len(buffer)), buffer)
    finally:
        os.close(descriptor)


def read_feature(path: str, length: int) -> bytearray:
    buffer = bytearray(length)
    buffer[0] = REPORT_ID
    descriptor = os.open(path, os.O_RDWR)
    try:
        fcntl.ioctl(descriptor, hidiocgfeature(length), buffer)
    finally:
        os.close(descriptor)
    return buffer


def parse_color(text: str) -> tuple[int, int, int]:
    key = text.strip().lower()
    if key in COLORS:
        return COLORS[key]
    digits = key.lstrip("#")
    if len(digits) == 6:
        try:
            return (int(digits[0:2], 16), int(digits[2:4], 16), int(digits[4:6], 16))
        except ValueError:
            pass
    raise SystemExit("unknown color: %s (try a name or #rrggbb)" % text)


def resolve_zones(args) -> list[tuple[int, int, int]]:
    if args.zones:
        names = [part for part in args.zones.split(",") if part.strip()]
        if len(names) != 4:
            raise SystemExit("--zones needs exactly 4 comma-separated colors")
        return [parse_color(name) for name in names]
    return [parse_color(args.color)] * 4


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Probe the Legion 4-zone RGB keyboard. Read-only unless --write."
    )
    parser.add_argument("--write", action="store_true",
                        help="actually send the report to the device")
    parser.add_argument("--read", action="store_true",
                        help="read the current feature report (does not modify anything)")
    parser.add_argument("--color", default="red", help="color name or #rrggbb")
    parser.add_argument("--zones", help="four comma-separated colors, zone 1 to 4")
    parser.add_argument("--effect", default="static", choices=sorted(EFFECTS))
    parser.add_argument("--speed", type=int, default=1, choices=(1, 2, 3, 4))
    parser.add_argument("--brightness", type=int, default=2, choices=(1, 2))
    parser.add_argument("--device", help="override the device path")
    parser.add_argument("--length", type=int, choices=(32, 33),
                        help="force a buffer length instead of trying both")
    args = parser.parse_args()

    print("== device discovery ==")
    candidates = find_devices()
    if not candidates:
        print("no device matching %s:%s found" % (VENDOR_ID, PRODUCT_ID))
        return 1
    for path, declares in candidates:
        mark = "declares report 0xCC  <-- target" if declares else "no 0xCC report"
        print("  %-16s %s" % (path, mark))

    device = args.device or pick_device()
    if device is None:
        print("\nno candidate declares report 0xCC; refusing to guess")
        return 1
    print("\nusing: %s" % device)

    zones = resolve_zones(args)
    payload = build_payload(EFFECTS[args.effect], args.speed, args.brightness, zones)

    print("\n== report that would be sent ==")
    print("effect=%s speed=%d brightness=%d" % (args.effect, args.speed, args.brightness))
    print("zones=%s" % " ".join("#%02X%02X%02X" % zone for zone in zones))
    for length in (33, 32):
        print("  %2d bytes: %s" % (length, hexdump(frame(payload, length))))

    if args.read:
        print("\n== reading current state ==")
        for length in (33, 32):
            try:
                current = read_feature(device, length)
            except OSError as error:
                print("  %2d bytes: failed (%s)" % (length, error))
            else:
                print("  %2d bytes: %s" % (length, hexdump(current)))

    if not args.write:
        print("\nDry run. Nothing was sent. Add --write to actually apply.")
        return 0

    print("\n== writing ==")
    lengths = [args.length] if args.length else [33, 32]
    for length in lengths:
        buffer = frame(payload, length)
        try:
            send_feature(device, buffer)
        except OSError as error:
            print("  %2d bytes: FAILED (%s)" % (length, error))
            continue
        print("  %2d bytes: accepted by the kernel" % length)
        print("\nLook at the keyboard now. If the color changed, %d bytes is the answer." % length)
        return 0

    print("\nEvery attempt failed. If this was a permission error, use sudo.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
