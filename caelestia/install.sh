#!/bin/sh
# The Keyboard section for caelestia shell: install, re-apply, remove.
#
#   sudo caelestia/install.sh apply    copy the files to /usr/local/share/keyboard-konf/
#                                      and patch the installed shell from there
#   sudo caelestia/install.sh remove   undo the patch, delete the copies and the hook
#
# The pacman hook runs `apply` from that root-owned copy after every shell
# upgrade, so the section comes back on its own and nothing in a home
# directory is ever run as root. The shell does not watch its files: restart
# it afterwards (caelestia shell -k; caelestia shell -d).
#
# Rollback: `sudo install.sh remove`, or `rm` the hook and reinstall the
# shell package.
set -eu

SHELL_DIR=/etc/xdg/quickshell/caelestia
SECTIONS=$SHELL_DIR/modules/controlcenter/appearance/sections
PANE=$SHELL_DIR/modules/controlcenter/appearance/AppearancePane.qml
STAGE=/usr/local/share/keyboard-konf
HOOK=/etc/pacman.d/hooks/caelestia-keyboard-konf.hook
FILES="KeyboardSection.qml KeyboardColourWheel.qml KeyboardPalette.qml"
HERE=$(dirname "$(readlink -f "$0")")

case "${1:-}" in
apply)
    [ -d "$SECTIONS" ] || { echo "no caelestia shell at $SHELL_DIR" >&2; exit 1; }
    # Run from a checkout: refresh the root-owned copy first. The hook runs
    # the copy itself, and then there is nothing to refresh.
    if [ "$HERE" != "$(readlink -f "$STAGE")" ]; then
        install -d -m 755 "$STAGE"
        for f in $FILES patch-appearance.py caelestia-keyboard-konf.hook; do
            install -m 644 "$HERE/$f" "$STAGE/$f"
        done
        install -m 755 "$HERE/install.sh" "$STAGE/install.sh"
        echo "copied to $STAGE"
    fi
    for f in $FILES; do
        [ -f "$STAGE/$f" ] || { echo "missing $STAGE/$f" >&2; exit 1; }
    done
    [ -f "$PANE.kkonf-orig" ] || cp "$PANE" "$PANE.kkonf-orig"
    for f in $FILES; do
        cp "$STAGE/$f" "$SECTIONS/$f"
    done
    if ! python3 "$STAGE/patch-appearance.py" apply "$PANE"; then
        echo "AppearancePane.qml has changed shape; the section is NOT registered" >&2
        exit 3
    fi
    # Replaced whenever it differs, so a hook from an older install that
    # pointed somewhere else is updated too.
    if ! cmp -s "$STAGE/caelestia-keyboard-konf.hook" "$HOOK"; then
        install -D -m 644 "$STAGE/caelestia-keyboard-konf.hook" "$HOOK"
        echo "hook installed: $HOOK"
    fi
    echo "applied; restart the shell: caelestia shell -k; caelestia shell -d"
    ;;
remove)
    python3 "$STAGE/patch-appearance.py" remove "$PANE" 2>/dev/null \
        || python3 "$HERE/patch-appearance.py" remove "$PANE"
    for f in $FILES; do
        rm -f "$SECTIONS/$f"
    done
    rm -f "$PANE.kkonf-orig" "$HOOK"
    rm -rf "$STAGE"
    echo "removed; restart the shell: caelestia shell -k; caelestia shell -d"
    ;;
*)
    sed -n '2,14p' "$0"
    exit 2
    ;;
esac
