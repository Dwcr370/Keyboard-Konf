pragma ComponentBehavior: Bound

import "../../components"
import QtQuick
import QtQuick.Layouts
import Caelestia.Config
import qs.components
import qs.components.controls
import qs.services

// A classic colour wheel: hue around the circle, saturation towards the
// centre, value on its own slider; red, green and blue sliders and a hex
// field next to it for those who think in numbers. Emits `picked` with a
// #RRGGBB string on every change; `colour` is what the daemon currently has.
ColumnLayout {
    id: root

    // What the daemon shows. While the user drags, the daemon's replies lag
    // behind the pointer (changes are sent every 50 ms and answered later),
    // so the wheel and the sliders follow `shown`, the colour the user is
    // at, and take `colour` only when it is not a stale echo.
    property string colour: "#FFFFFF"
    property string shown: "#FFFFFF"
    property string lastSent: ""

    signal picked(string hex)

    readonly property real size: 200
    property real hue: 0
    property real sat: 0
    property real val: 1

    spacing: Tokens.spacing.normal

    function hexOf(c: color): string {
        return c.toString().toUpperCase();
    }

    function fromHsv(): string {
        return hexOf(Qt.hsva(hue, sat, val, 1));
    }

    function emitHsv(): void {
        choose(fromHsv());
    }

    // The user picked a colour here: show it at once, send it, and ignore
    // echoes of older colours for a moment.
    function choose(hex: string): void {
        lastSent = hex;
        settle.restart();
        adopt(hex);
        picked(hex);
    }

    function adopt(hex: string): void {
        if (shown === hex)
            return;
        shown = hex;
        const c = Qt.color(hex);
        if (hexOf(c) === fromHsv())
            return; // the wheel already stands there
        if (c.hsvSaturation > 0 && c.hsvValue > 0)
            hue = c.hsvHue;
        sat = c.hsvSaturation;
        val = c.hsvValue;
        wheel.requestPaint();
    }

    onColourChanged: {
        if (settle.running && colour !== lastSent)
            return; // a reply to an earlier drag position
        adopt(colour);
    }

    Timer {
        id: settle

        interval: 400
    }

    onValChanged: wheel.requestPaint()

    Item {
        Layout.alignment: Qt.AlignHCenter
        implicitWidth: root.size
        implicitHeight: root.size

        Canvas {
            id: wheel

            anchors.fill: parent

            onPaint: {
                const ctx = getContext("2d");
                const w = width, h = height;
                const cx = w / 2, cy = h / 2, r = w / 2 - 2;
                ctx.clearRect(0, 0, w, h);
                const step = 2;
                for (let deg = 0; deg < 360; deg += step) {
                    ctx.beginPath();
                    ctx.moveTo(cx, cy);
                    ctx.arc(cx, cy, r, (deg - 0.5) * Math.PI / 180, (deg + step + 0.5) * Math.PI / 180);
                    ctx.closePath();
                    ctx.fillStyle = Qt.hsva(deg / 360, 1, root.val, 1).toString();
                    ctx.fill();
                }
                const grey = Qt.rgba(root.val, root.val, root.val, 1);
                const fade = ctx.createRadialGradient(cx, cy, 0, cx, cy, r);
                fade.addColorStop(0, grey.toString());
                fade.addColorStop(1, Qt.rgba(root.val, root.val, root.val, 0).toString());
                ctx.beginPath();
                ctx.arc(cx, cy, r, 0, 2 * Math.PI);
                ctx.closePath();
                ctx.fillStyle = fade;
                ctx.fill();
            }
        }

        Rectangle {
            id: marker

            readonly property real r: wheel.width / 2 - 2
            readonly property real angle: root.hue * 2 * Math.PI

            width: 16
            height: 16
            radius: 8
            x: wheel.width / 2 + r * root.sat * Math.cos(angle) - width / 2
            y: wheel.height / 2 + r * root.sat * Math.sin(angle) - height / 2
            color: Qt.hsva(root.hue, root.sat, root.val, 1)
            border.width: 2
            border.color: root.val > 0.5 && root.sat < 0.5 ? "#000000" : "#FFFFFF"
        }

        MouseArea {
            id: area

            anchors.fill: parent
            preventStealing: true // the sidebar is a Flickable: a drag here is ours
            cursorShape: Qt.CrossCursor

            function pick(mx: real, my: real): void {
                const r = wheel.width / 2 - 2;
                const dx = mx - wheel.width / 2, dy = my - wheel.height / 2;
                const dist = Math.sqrt(dx * dx + dy * dy);
                root.sat = Math.min(1, dist / r);
                if (dist > 1)
                    root.hue = (Math.atan2(dy, dx) / (2 * Math.PI) + 1) % 1;
                root.emitHsv();
            }

            onPressed: event => pick(event.x, event.y)
            onPositionChanged: event => {
                if (pressed)
                    pick(event.x, event.y);
            }
        }
    }

    SliderInput {
        Layout.fillWidth: true

        label: qsTr("Value")
        value: root.val * 100
        from: 0
        to: 100
        suffix: "%"
        validator: IntValidator {
            bottom: 0
            top: 100
        }
        formatValueFunction: v => Math.round(v).toString()
        parseValueFunction: t => parseInt(t)

        onValueModified: v => {
            root.val = v / 100;
            root.emitHsv();
        }
    }

    Repeater {
        model: [qsTr("Red"), qsTr("Green"), qsTr("Blue")]

        SliderInput {
            id: channel

            required property int index
            required property string modelData
            readonly property color c: Qt.color(root.shown)

            Layout.fillWidth: true

            label: modelData
            value: Math.round((index === 0 ? c.r : index === 1 ? c.g : c.b) * 255)
            from: 0
            to: 255
            stepSize: 1
            validator: IntValidator {
                bottom: 0
                top: 255
            }
            formatValueFunction: v => Math.round(v).toString()
            parseValueFunction: t => parseInt(t)

            onValueModified: v => {
                const parts = [c.r, c.g, c.b];
                parts[index] = Math.round(v) / 255;
                root.choose(root.hexOf(Qt.rgba(parts[0], parts[1], parts[2], 1)));
            }
        }
    }

    RowLayout {
        Layout.fillWidth: true
        spacing: Tokens.spacing.normal

        StyledText {
            text: qsTr("Hex")
        }

        Item {
            Layout.fillWidth: true
        }

        StyledInputField {
            id: hexField

            Layout.preferredWidth: 110
            validator: RegularExpressionValidator {
                regularExpression: /#?[0-9a-fA-F]{0,6}/
            }

            Component.onCompleted: text = root.shown

            Connections {
                function onShownChanged(): void {
                    if (!hexField.hasFocus)
                        hexField.text = root.shown;
                }

                target: root
            }

            onEditingFinished: {
                const t = text.trim().replace(/^#?/, "#");
                if (/^#[0-9a-fA-F]{6}$/.test(t))
                    root.choose(t.toUpperCase());
                else
                    text = root.shown;
            }
        }
    }
}
