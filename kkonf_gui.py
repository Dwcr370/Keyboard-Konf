#!/usr/bin/env python3
"""GTK4 front-end for keyboard-konf.

Writes to the lighting controller directly: no sudo, no helper (the udev
rule in udev/ grants access). Everything picked here is a screen colour;
kkonf_core turns it into controller bytes through the sRGB curve and the
saved white balance, so what you see in the swatch is what the keys show.

Two windows:

  * the main window: four zones, a colour per zone, a white by colour
    temperature, the two hardware brightness levels; changes apply live;
  * the calibration window: a black full-screen page with a grey strip right
    above the keyboard and two sliders (warm/cold, green/magenta) that move
    the white balance live until the keys match the strip.

Run `kkonf gui`, or `kkonf calibrate` to open the calibration page directly.
"""

from __future__ import annotations

import copy
import os
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import kkonf_core as core  # noqa: E402

DEFAULT_COLOR = (255, 255, 255)
CHANNELS = ("R", "G", "B")
APPLY_DELAY_MS = 50  # coalesce slider traffic into at most ~20 reports/s
STRIP_GREY = (180, 180, 180)  # the reference strip: screen white, dimmed
UDEV_HINT = "No access to the device. Install the udev rule (README, Install section)."

# Colours for checking the calibration: screen swatch vs. keyboard.
CHECK_SWATCHES = (
    ("White", (255, 255, 255)),
    ("Orange", (255, 128, 0)),
    ("Purple", (128, 0, 128)),
    ("Pink", (255, 105, 180)),
    ("Cyan", (0, 200, 255)),
    ("Pastel", (200, 220, 255)),
)


class Backlight:
    """The single writer to the device, plus the white balance in use.

    Writes are coalesced: sliders emit dozens of changes per second, the
    controller gets at most one report every APPLY_DELAY_MS.
    """

    def __init__(self) -> None:
        self.zones_cal: list[dict] = core.load_calibration()
        self.boost: float = core.load_boost()
        self.max_brightness: bool = core.load_max_brightness()
        self.brightness: int = core.BRIGHTNESS_DEFAULT
        self.on_status = None  # callable(str), set by the window
        self._pending: list[core.Color] | None = None
        self._source: int | None = None

    @property
    def gains(self) -> list[core.Gains]:
        return [tuple(zone["gains"]) for zone in self.zones_cal]

    def set_zone(self, index: int, temp: float, tint: float, level: float) -> None:
        """Set one zone's balance (two sliders) and its brightness level."""
        temp = min(max(temp, -1.0), 1.0)
        tint = min(max(tint, -1.0), 1.0)
        level = min(max(level, core.LEVEL_MIN), 1.0)
        self.zones_cal[index] = {
            "temp": temp, "tint": tint, "level": level,
            "gains": list(core.gains_with_level(temp, tint, level)),
        }

    def send(self, zones: list[core.Color]) -> None:
        self._pending = list(zones)
        if self._source is None:
            self._source = GLib.timeout_add(APPLY_DELAY_MS, self._flush)

    def _flush(self) -> bool:
        self._source = None
        zones, self._pending = self._pending, None
        try:
            device = core.apply_zones(zones, self.brightness, self.gains,
                                      boost=self.boost, max_brightness=self.max_brightness)
        except core.DeviceNotFound as error:
            message = "Device not found: %s" % error
        except PermissionError:
            message = UDEV_HINT
        except OSError as error:
            message = "Write error: %s" % error
        else:
            message = "Applied (%s)" % device
        if self.on_status is not None:
            self.on_status(message)
        return GLib.SOURCE_REMOVE


def css_color(color: core.Color) -> str:
    return "rgb(%d, %d, %d)" % tuple(color)


def gains_summary(zones_cal: list[dict]) -> str:
    return "   ".join(
        "z%d  R %.2f  G %.2f  B %.2f" % (index, *zone["gains"])
        for index, zone in enumerate(zones_cal, start=1)
    )


ZONE_CHOICES = ("All", "1", "2", "3", "4")


# ======================================================================
# Calibration window
# ======================================================================


class CalibrationWindow(Adw.Window):
    """Black full-screen page: reference strip at the bottom, sliders on top."""

    def __init__(self, backlight: Backlight, on_saved, **kwargs) -> None:
        super().__init__(**kwargs)
        self.backlight = backlight
        self.on_saved = on_saved
        self.saved = copy.deepcopy(backlight.zones_cal)
        self.target: int | None = None  # None = all zones, else zone index 0-3
        # "All" moves every zone by the same amount from where it was when
        # "All" was selected, so the differences between zones survive.
        self._origin: list[dict] = copy.deepcopy(backlight.zones_cal)
        self._base = (0.0, 0.0, 1.0)
        self.check_color: core.Color = CHECK_SWATCHES[0][1]
        self._syncing = False

        self.set_title("White balance")
        self.add_css_class("calib")
        self._css = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_display(
            self.get_display(), self._css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        self.set_content(self._build())
        self._sync_sliders()
        self._paint_strip()

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)
        self.connect("close-request", self._on_close_request)

        self.fullscreen()
        self._send()

    # ---------------------------------------------------------------- layout

    def _build(self) -> Gtk.Widget:
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        panel.set_margin_top(36)
        panel.set_margin_start(24)
        panel.set_margin_end(24)

        title = Gtk.Label(label="White balance", xalign=0)
        title.add_css_class("title-1")
        panel.append(title)

        hint = Gtk.Label(xalign=0)
        hint.set_wrap(True)
        hint.set_label(
            "Look at the grey strip at the bottom and at the keys below it. “All” shifts "
            "every zone together, keeping the differences between them; a zone number "
            "adjusts only that zone. Zone 1 is on the left, by Esc. “Level” dims a zone "
            "that is brighter than its neighbours. Check other colours with the buttons "
            "below. Esc closes without saving."
        )
        hint.add_css_class("dim-label")
        panel.append(hint)

        zone_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        zone_row.set_halign(Gtk.Align.CENTER)
        zone_caption = Gtk.Label(label="Zone")
        zone_caption.add_css_class("dim-label")
        zone_caption.set_margin_end(6)
        zone_row.append(zone_caption)
        first_zone: Gtk.ToggleButton | None = None
        for index, name in enumerate(ZONE_CHOICES):
            button = Gtk.ToggleButton(label=name)
            if first_zone is None:
                first_zone = button
                button.set_active(True)
                button.set_tooltip_text("The sliders set the same balance for all four zones")
            else:
                button.set_group(first_zone)
            button.connect("toggled", self._on_zone, index)
            zone_row.append(button)
        panel.append(zone_row)

        self.temp_scale = self._slider_row(
            panel, "Warm", "Cold", -100, 100, 0, self._on_slider)
        self.tint_scale = self._slider_row(
            panel, "Green", "Magenta", -100, 100, 0, self._on_slider)
        self.level_scale = self._slider_row(
            panel, "Level", "", int(core.LEVEL_MIN * 100), 100, 100, self._on_slider)

        self.gains_label = Gtk.Label(xalign=0)
        self.gains_label.add_css_class("dim-label")
        self.gains_label.add_css_class("monospace")
        panel.append(self.gains_label)
        self._show_gains()

        swatches = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        swatches.set_halign(Gtk.Align.CENTER)
        first: Gtk.ToggleButton | None = None
        for name, color in CHECK_SWATCHES:
            button = Gtk.ToggleButton(label=name)
            if first is None:
                first = button
                button.set_active(True)
            else:
                button.set_group(first)
            button.connect("toggled", self._on_swatch, color)
            swatches.append(button)
        panel.append(swatches)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        buttons.set_halign(Gtk.Align.END)
        reset = Gtk.Button(label="Reset")
        reset.connect("clicked", self._on_reset)
        close = Gtk.Button(label="Close")
        close.connect("clicked", lambda *_: self.close())
        save = Gtk.Button(label="Save")
        save.add_css_class("suggested-action")
        save.connect("clicked", self._on_save)
        for widget in (reset, close, save):
            buttons.append(widget)
        panel.append(buttons)

        self.status = Gtk.Label(xalign=0)
        self.status.add_css_class("dim-label")
        self.status.set_wrap(True)
        panel.append(self.status)

        clamp = Adw.Clamp()
        clamp.set_maximum_size(820)
        clamp.set_child(panel)
        root.append(clamp)

        spacer = Gtk.Box()
        spacer.set_vexpand(True)
        root.append(spacer)

        self.strip = Gtk.Box()
        self.strip.set_name("strip")
        self.strip.set_size_request(-1, 190)
        root.append(self.strip)
        return root

    def _slider_row(self, parent: Gtk.Box, left: str, right: str,
                    low: int, high: int, value: int, handler) -> Gtk.Scale:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        left_label = Gtk.Label(label=left)
        left_label.set_size_request(90, -1)
        left_label.set_xalign(1.0)
        scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, low, high, 1)
        scale.set_hexpand(True)
        scale.set_draw_value(True)
        scale.set_value_pos(Gtk.PositionType.BOTTOM)
        scale.add_mark(0 if low < 0 else high, Gtk.PositionType.TOP, None)
        scale.set_value(value)
        scale.connect("value-changed", handler)
        right_label = Gtk.Label(label=right)
        right_label.set_size_request(90, -1)
        right_label.set_xalign(0.0)
        row.append(left_label)
        row.append(scale)
        row.append(right_label)
        parent.append(row)
        return scale

    # ----------------------------------------------------------------- state

    def _paint_strip(self) -> None:
        shown = self.check_color
        if shown == CHECK_SWATCHES[0][1]:
            shown = STRIP_GREY  # full white glares; a dimmed white has the same tint
        self._css.load_from_string(
            "window.calib { background-color: black; }\n"
            "#strip { background-color: %s; }" % css_color(shown)
        )

    def _show_gains(self) -> None:
        self.gains_label.set_text(gains_summary(self.backlight.zones_cal))

    def _sync_sliders(self) -> None:
        """Show the selected zone's positions; for "All" the average of all."""
        zones = self.backlight.zones_cal
        if self.target is None:
            self._origin = copy.deepcopy(zones)
            count = float(len(zones))
            temp = sum(z["temp"] for z in zones) / count
            tint = sum(z["tint"] for z in zones) / count
            level = sum(z["level"] for z in zones) / count
            self._base = (temp, tint, level)
        else:
            entry = zones[self.target]
            temp, tint, level = entry["temp"], entry["tint"], entry["level"]
        self._syncing = True
        self.temp_scale.set_value(round(temp * 100))
        self.tint_scale.set_value(round(tint * 100))
        self.level_scale.set_value(round(level * 100))
        self._syncing = False
        self._show_gains()

    def _send(self) -> None:
        self.backlight.send([self.check_color] * core.ZONE_COUNT)

    def _set_status(self, text: str) -> None:
        self.status.set_text(text)

    # --------------------------------------------------------------- signals

    def _on_zone(self, button: Gtk.ToggleButton, index: int) -> None:
        if not button.get_active():
            return
        self.target = None if index == 0 else index - 1
        self._sync_sliders()

    def _on_slider(self, _scale: Gtk.Scale) -> None:
        if self._syncing:
            return
        temp = self.temp_scale.get_value() / 100.0
        tint = self.tint_scale.get_value() / 100.0
        level = self.level_scale.get_value() / 100.0
        if self.target is None:
            d_temp, d_tint, d_level = (temp - self._base[0], tint - self._base[1],
                                       level - self._base[2])
            for index, origin in enumerate(self._origin):
                self.backlight.set_zone(index, origin["temp"] + d_temp,
                                        origin["tint"] + d_tint,
                                        origin["level"] + d_level)
        else:
            self.backlight.set_zone(self.target, temp, tint, level)
        self._show_gains()
        self._send()

    def _on_swatch(self, button: Gtk.ToggleButton, color: core.Color) -> None:
        if not button.get_active():
            return
        self.check_color = color
        self._paint_strip()
        self._send()

    def _on_reset(self, _button: Gtk.Button) -> None:
        targets = range(core.ZONE_COUNT) if self.target is None else (self.target,)
        for index in targets:
            self.backlight.set_zone(index, core.DEFAULT_TEMP, core.DEFAULT_TINT, 1.0)
        self._sync_sliders()
        self._send()

    def _on_save(self, _button: Gtk.Button) -> None:
        try:
            core.save_calibration(self.backlight.zones_cal, self.backlight.boost,
                                  self.backlight.max_brightness)
        except (OSError, ValueError) as error:
            self._set_status("Could not save: %s" % error)
            return
        self.saved = copy.deepcopy(self.backlight.zones_cal)
        self._set_status("Saved to %s" % core.CALIBRATION_FILE)
        self.on_saved()

    def _on_key(self, _controller, keyval, _keycode, _state) -> bool:
        if keyval == Gdk.KEY_Escape:
            self.close()
            return True
        return False

    def _on_close_request(self, _window) -> bool:
        # Unsaved slider moves are dropped: back to the saved balance.
        self.backlight.zones_cal = copy.deepcopy(self.saved)
        self.on_saved()
        return False


# ======================================================================
# Main window
# ======================================================================


class KkonfWindow(Adw.ApplicationWindow):
    def __init__(self, backlight: Backlight, **kwargs) -> None:
        super().__init__(**kwargs)
        self.set_title("Keyboard Konf")
        self.set_default_size(500, -1)

        self.backlight = backlight
        self.backlight.on_status = self._set_status
        self.zones: list[core.Color] = [DEFAULT_COLOR] * core.ZONE_COUNT
        self.selected = 0
        self._syncing = False

        self._css = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_display(
            self.get_display(), self._css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        self.set_content(self._build())
        self._sync_from_zone()

    # ---------------------------------------------------------------- layout

    def _build(self) -> Gtk.Widget:
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        page = Adw.PreferencesPage()
        page.add(self._zone_group())
        page.add(self._color_group())
        page.add(self._white_group())
        page.add(self._boost_group())
        page.add(self._settings_group())
        view.set_content(page)
        return view

    def _zone_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title="Zones")
        group.set_description("Zone 1 is on the left, by Esc and Tab. Changes apply immediately.")

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.set_halign(Gtk.Align.CENTER)

        self.zone_buttons: list[Gtk.ToggleButton] = []
        for index in range(core.ZONE_COUNT):
            swatch = Gtk.Box()
            swatch.set_name("zone%d" % index)
            swatch.set_size_request(66, 46)

            button = Gtk.ToggleButton()
            button.set_child(swatch)
            button.set_tooltip_text("Zone %d" % (index + 1))
            button.set_active(index == self.selected)
            button.connect("toggled", self._on_zone_toggled, index)
            row.append(button)
            self.zone_buttons.append(button)

        fill = Gtk.Button(label="Fill all zones with this colour")
        fill.set_halign(Gtk.Align.CENTER)
        fill.connect("clicked", self._on_fill_all)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.append(row)
        box.append(fill)
        group.add(box)
        return group

    def _color_group(self) -> Adw.PreferencesGroup:
        self.color_group = Adw.PreferencesGroup(title="Zone 1 colour")

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)

        self.preview = Gtk.Box()
        self.preview.set_name("preview")
        self.preview.set_size_request(76, 76)
        self.preview.set_valign(Gtk.Align.CENTER)
        top.append(self.preview)

        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        right.set_valign(Gtk.Align.CENTER)
        right.set_hexpand(True)
        caption = Gtk.Label(label="Colour code", xalign=0)
        caption.add_css_class("dim-label")
        self.hex_entry = Gtk.Entry()
        self.hex_entry.set_max_length(7)
        self.hex_entry.set_placeholder_text("#RRGGBB")
        self.hex_entry.connect("changed", self._on_hex_changed)
        right.append(caption)
        right.append(self.hex_entry)
        top.append(right)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.append(top)

        self.sliders: dict[str, Gtk.Scale] = {}
        for channel, name in enumerate(CHANNELS):
            line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            tag = Gtk.Label(label=name)
            tag.set_size_request(16, -1)
            tag.add_css_class("dim-label")
            scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 255, 1)
            scale.set_hexpand(True)
            scale.set_draw_value(True)
            scale.set_value_pos(Gtk.PositionType.RIGHT)
            scale.connect("value-changed", self._on_slider_changed, channel)
            line.append(tag)
            line.append(scale)
            box.append(line)
            self.sliders[name] = scale

        self.color_group.add(box)
        return self.color_group

    def _white_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title="White")
        group.set_description(
            "Colour temperature of the white on all zones. 6500 K is the screen's white; "
            "lower is warmer, higher is colder."
        )
        line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.kelvin_scale = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, core.KELVIN_MIN, core.KELVIN_MAX, 100)
        self.kelvin_scale.set_hexpand(True)
        self.kelvin_scale.set_draw_value(True)
        self.kelvin_scale.set_value_pos(Gtk.PositionType.RIGHT)
        self.kelvin_scale.set_digits(0)
        self.kelvin_scale.add_mark(core.DEFAULT_KELVIN, Gtk.PositionType.BOTTOM, "6500 K")
        self.kelvin_scale.set_value(core.DEFAULT_KELVIN)
        self.kelvin_scale.connect("value-changed", self._on_kelvin_changed)
        line.append(self.kelvin_scale)

        apply_white = Gtk.Button(label="White")
        apply_white.set_tooltip_text("Fill all zones with white at this temperature")
        apply_white.connect("clicked", self._on_kelvin_changed)
        line.append(apply_white)
        group.add(line)
        return group

    def _boost_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title="Brightness")
        group.set_description(
            "100% is the calibrated white (blue is already at its maximum). Above that, "
            "everything gets brighter, but white gradually drifts warm because blue "
            "cannot go any higher. The value is saved automatically."
        )
        self.boost_scale = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, core.BOOST_MIN * 100, core.BOOST_MAX * 100, 5)
        self.boost_scale.set_hexpand(True)
        self.boost_scale.set_draw_value(True)
        self.boost_scale.set_value_pos(Gtk.PositionType.RIGHT)
        self.boost_scale.set_digits(0)
        self.boost_scale.add_mark(100, Gtk.PositionType.BOTTOM, "100%")
        self.boost_scale.add_mark(200, Gtk.PositionType.BOTTOM, "200%")
        self.boost_scale.add_mark(300, Gtk.PositionType.BOTTOM, "300%")
        self.boost_scale.set_value(round(self.backlight.boost * 100))
        self.boost_scale.connect("value-changed", self._on_boost_changed)
        self.boost_scale.set_sensitive(not self.backlight.max_brightness)
        self._boost_save_source: int | None = None
        group.add(self.boost_scale)

        self.max_switch = Gtk.CheckButton(label="Maximum brightness")
        self.max_switch.set_tooltip_text(
            "Every colour is pushed to the brightest level at which no channel "
            "clips. Hue, saturation and the balance between zones are kept; "
            "the slider above then has no effect."
        )
        self.max_switch.set_active(self.backlight.max_brightness)
        self.max_switch.set_margin_top(6)
        self.max_switch.connect("toggled", self._on_max_toggled)
        group.add(self.max_switch)
        return group

    def _settings_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup()

        bright = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        bright_caption = Gtk.Label(label="Hardware brightness", xalign=0)
        bright_caption.add_css_class("dim-label")
        bright_caption.set_hexpand(True)
        bright.append(bright_caption)
        self.bright_low = Gtk.ToggleButton(label="1")
        self.bright_high = Gtk.ToggleButton(label="2")
        self.bright_high.set_group(self.bright_low)
        self.bright_high.set_active(True)
        self.bright_low.connect("toggled", self._on_brightness)
        holder = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        holder.add_css_class("linked")
        holder.append(self.bright_low)
        holder.append(self.bright_high)
        bright.append(holder)

        calibrate = Gtk.Button(label="White balance…")
        calibrate.set_tooltip_text("Calibrate the white against the screen")
        calibrate.connect("clicked", self._on_calibrate)
        bright.append(calibrate)

        self.status = Gtk.Label(xalign=0)
        self.status.add_css_class("dim-label")
        self.status.set_wrap(True)
        self.status.set_margin_top(6)
        self._set_status("Ready. Per-zone balance: " + gains_summary(self.backlight.zones_cal))

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.append(bright)
        box.append(self.status)
        group.add(box)
        return group

    # ----------------------------------------------------------------- state

    def _current(self) -> core.Color:
        return self.zones[self.selected]

    def _repaint(self) -> None:
        rules = [
            "#zone%d { background-color: %s; border-radius: 6px; }"
            % (index, css_color(color))
            for index, color in enumerate(self.zones)
        ]
        rules.append(
            "#preview { background-color: %s; border-radius: 10px; }"
            % css_color(self._current())
        )
        self._css.load_from_string("\n".join(rules))

    def _sync_from_zone(self) -> None:
        self._syncing = True
        color = self._current()
        self.hex_entry.set_text(core.to_hex(color))
        self.hex_entry.remove_css_class("error")
        for name, value in zip(CHANNELS, color):
            self.sliders[name].set_value(value)
        self.color_group.set_title("Zone %d colour" % (self.selected + 1))
        self._syncing = False
        self._repaint()

    def _changed(self) -> None:
        """A zone colour changed: repaint the swatches and send everything."""
        self._repaint()
        self.backlight.send(self.zones)

    def _set_status(self, text: str) -> None:
        self.status.set_text(text)

    # --------------------------------------------------------------- signals

    def _on_zone_toggled(self, button: Gtk.ToggleButton, index: int) -> None:
        if not button.get_active():
            if not any(other.get_active() for other in self.zone_buttons):
                button.set_active(True)
            return
        for other_index, other in enumerate(self.zone_buttons):
            if other_index != index:
                other.set_active(False)
        self.selected = index
        self._sync_from_zone()

    def _on_fill_all(self, _button: Gtk.Button) -> None:
        self.zones = [self._current()] * core.ZONE_COUNT
        self._changed()

    def _on_hex_changed(self, entry: Gtk.Entry) -> None:
        if self._syncing:
            return
        try:
            color = core.parse_hex(entry.get_text())
        except ValueError:
            entry.add_css_class("error")
            return
        entry.remove_css_class("error")
        self.zones[self.selected] = color
        self._syncing = True
        for name, value in zip(CHANNELS, color):
            self.sliders[name].set_value(value)
        self._syncing = False
        self._changed()

    def _on_slider_changed(self, scale: Gtk.Scale, channel: int) -> None:
        if self._syncing:
            return
        color = list(self._current())
        color[channel] = int(scale.get_value())
        self.zones[self.selected] = tuple(color)
        self._syncing = True
        self.hex_entry.set_text(core.to_hex(self.zones[self.selected]))
        self.hex_entry.remove_css_class("error")
        self._syncing = False
        self._changed()

    def _on_kelvin_changed(self, _widget) -> None:
        if self._syncing:
            return
        white = core.kelvin_to_srgb(self.kelvin_scale.get_value())
        self.zones = [white] * core.ZONE_COUNT
        self._sync_from_zone()
        self.backlight.send(self.zones)

    def _on_brightness(self, _button: Gtk.ToggleButton) -> None:
        self.backlight.brightness = 1 if self.bright_low.get_active() else 2
        self.backlight.send(self.zones)

    def _on_boost_changed(self, scale: Gtk.Scale) -> None:
        self.backlight.boost = scale.get_value() / 100.0
        self.backlight.send(self.zones)
        # Persist a moment after the slider stops moving, not on every tick.
        if self._boost_save_source is not None:
            GLib.source_remove(self._boost_save_source)
        self._boost_save_source = GLib.timeout_add(800, self._save_boost)

    def _save_boost(self) -> bool:
        self._boost_save_source = None
        try:
            core.save_calibration(self.backlight.zones_cal, self.backlight.boost,
                                  self.backlight.max_brightness)
        except (OSError, ValueError) as error:
            self._set_status("Could not save the brightness: %s" % error)
        else:
            if self.backlight.max_brightness:
                self._set_status("Maximum brightness on, saved.")
            else:
                self._set_status("Brightness %.0f%% saved." % (self.backlight.boost * 100))
        return GLib.SOURCE_REMOVE

    def _on_max_toggled(self, button: Gtk.CheckButton) -> None:
        self.backlight.max_brightness = button.get_active()
        self.boost_scale.set_sensitive(not self.backlight.max_brightness)
        self.backlight.send(self.zones)
        self._save_boost()

    def _on_calibrate(self, _button=None) -> None:
        window = CalibrationWindow(self.backlight, self._on_calibration_done,
                                   transient_for=self, application=self.get_application())
        self.backlight.on_status = window._set_status
        window.present()

    def _on_calibration_done(self) -> None:
        """Back from the calibration page: re-apply the zones with its gains."""
        self.backlight.on_status = self._set_status
        self._set_status("Per-zone balance: " + gains_summary(self.backlight.zones_cal))
        self.backlight.send(self.zones)


class KkonfApplication(Adw.Application):
    def __init__(self, open_calibration: bool) -> None:
        # One instance per session normally: a second `kkonf gui` just raises
        # the existing window. KKONF_TEST_INSTANCE=1 starts a separate, non-unique
        # instance, so a test run never takes over a window that is already open.
        flags = Gio.ApplicationFlags.DEFAULT_FLAGS
        if os.environ.get("KKONF_TEST_INSTANCE") == "1":
            flags = Gio.ApplicationFlags.NON_UNIQUE
        super().__init__(application_id="io.github.keyboard_konf.KeyboardKonf", flags=flags)
        self.open_calibration = open_calibration
        self.backlight = Backlight()

    def do_activate(self) -> None:
        window = self.props.active_window
        if window is None:
            # Dark chrome: the calibration page is black and the main window
            # should not glare next to it.
            Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
            window = KkonfWindow(self.backlight, application=self)
        window.present()
        if self.open_calibration:
            self.open_calibration = False
            window._on_calibrate()


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    open_calibration = "--calibrate" in args
    return KkonfApplication(open_calibration).run([sys.argv[0]])


if __name__ == "__main__":
    sys.exit(main())
