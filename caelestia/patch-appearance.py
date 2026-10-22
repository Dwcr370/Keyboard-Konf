#!/usr/bin/env python3
"""Register the Keyboard section in caelestia's AppearancePane.qml, or take
it out again. Idempotent: run it twice and the file is the same.

    patch-appearance.py apply  <AppearancePane.qml>
    patch-appearance.py remove <AppearancePane.qml>
    patch-appearance.py check  <AppearancePane.qml>   exit 0 if applied

Three edits, each marked with "keyboard-konf" so they can be found again:
the section after BackgroundSection in the sidebar, and the section in the
"expand all" expression and handler. Exit 3 if the file no longer looks the
way the edits expect (the shell was restructured): nothing is changed then.
"""

import re
import sys

MARK = "keyboard-konf"

SECTION = """
                    KeyboardSection {
                        id: keyboardSection // %s

                        rootPane: sidebarFlickable.rootPane
                    }
""" % MARK

EXPAND_BIND = "&& backgroundSection.expanded\n"
EXPAND_BIND_NEW = "&& backgroundSection.expanded && keyboardSection.expanded /* %s */\n" % MARK
EXPAND_SET = "                                backgroundSection.expanded = shouldExpand;\n"
EXPAND_SET_NEW = EXPAND_SET + \
    "                                keyboardSection.expanded = shouldExpand; // %s\n" % MARK

BACKGROUND_BLOCK = re.compile(
    r"\n( +)BackgroundSection \{\n(?:\1 .*\n|\n)*?\1\}\n")


def apply(text: str) -> str:
    if MARK in text:
        return text
    match = BACKGROUND_BLOCK.search(text)
    if match is None or text.count(EXPAND_BIND) != 1 or text.count(EXPAND_SET) != 1:
        raise SystemExit(3)
    text = text[:match.end()] + SECTION + text[match.end():]
    text = text.replace(EXPAND_BIND, EXPAND_BIND_NEW)
    text = text.replace(EXPAND_SET, EXPAND_SET_NEW)
    return text


def remove(text: str) -> str:
    text = text.replace(SECTION, "")
    text = text.replace(EXPAND_BIND_NEW, EXPAND_BIND)
    text = text.replace(EXPAND_SET_NEW, EXPAND_SET)
    return text


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] not in ("apply", "remove", "check"):
        print(__doc__, file=sys.stderr)
        return 2
    command, path = argv[1], argv[2]
    with open(path) as handle:
        text = handle.read()
    if command == "check":
        return 0 if MARK in text else 1
    result = apply(text) if command == "apply" else remove(text)
    if result != text:
        with open(path, "w") as handle:
            handle.write(result)
        print("%s: %s" % (command, path))
    else:
        print("%s: nothing to do" % command)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
