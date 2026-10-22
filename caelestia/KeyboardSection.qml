pragma ComponentBehavior: Bound

import ".."
import "../../components"
import QtQuick
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import Caelestia.Config
import qs.components
import qs.components.controls
import qs.services

// Keyboard backlight (keyboard-konf). A client of the kkonf daemon over its
// Unix socket: every control sends a change at once and the daemon answers
// with the state it now shows, so this section, the command line and a
// hotkey always agree. Nothing here is stored in shell.json.
CollapsibleSection {
    id: root

    // The Appearance pane hands every section its root; this section keeps
    // no shell settings, so it is accepted and unused.
    property var rootPane: null

    // --- the daemon's state, mirrored
    property bool daemonUp: false
    property bool lightOn: true
    property string effect: "white"
    property var params: ({})
    property real brightness: 1
    property bool maxMode: false
    property var effectList: []
    property var presetMap: ({})
    property var myColours: []

    // --- which colour the swatches, the wheel and the hex field edit: a
    // parameter name, and for "colors" (one per zone) a zone, 0 = all four
    property string target: "color"
    property int targetZone: 0

    readonly property var effectInfo: effectList.find(e => e.name === effect) ?? null
    readonly property var effectParams: effectInfo ? effectInfo.params : []
    readonly property var colourTargets: effectParams.filter(p => p.type === "color" || p.type === "colors")
    readonly property var sliderParams: effectParams.filter(p => p.type === "range" || p.type === "kelvin")
    readonly property var choiceParams: effectParams.filter(p => p.type === "choice")
    readonly property string currentColour: {
        const spec = effectParams.find(p => p.name === target);
        return spec ? colourOf(spec, targetZone) : "#FFFFFF";
    }
    // The row of colour chips is always one row high so the page never
    // jumps: short labels, and a single placeholder chip when the effect has
    // no colour of its own (picking a colour then switches to Static).
    readonly property var targetChips: {
        const chips = [];
        for (const p of colourTargets) {
            if (p.type === "colors") {
                for (let z = 0; z <= 4; z++)
                    chips.push({
                        name: p.name,
                        zone: z,
                        label: z === 0 ? qsTr("All") : String(z),
                        colour: colourOf(p, z),
                        live: true
                    });
            } else {
                chips.push({
                    name: p.name,
                    zone: 0,
                    label: shortLabel(p.name),
                    colour: colourOf(p, 0),
                    live: true
                });
            }
        }
        if (chips.length === 0)
            chips.push({
                name: "",
                zone: 0,
                label: qsTr("Pick a colour for Static"),
                colour: "#FFFFFF",
                live: false
            });
        return chips;
    }

    function shortLabel(name: string): string {
        switch (name) {
        case "color":
            return qsTr("Colour");
        case "color2":
            return qsTr("Second");
        case "color_caps":
            return qsTr("Caps Lock");
        case "color_num":
            return qsTr("Num Lock");
        }
        return name;
    }

    title: qsTr("Keyboard")
    description: daemonUp ? "" : qsTr("The keyboard-konf daemon is not running. Start it with `kkonf daemon` or enable its user service.")
    showBackground: true

    // ------------------------------------------------------------ helpers

    function colourOf(spec: var, zone: int): string {
        const value = params[spec.name] ?? spec.default;
        if (spec.type === "colors")
            return value[zone > 0 ? zone - 1 : 0] ?? "#FFFFFF";
        return value;
    }

    function choiceLabel(name: string, value: string): string {
        if (name === "direction")
            return value === "ltr" ? qsTr("Esc → Num") : qsTr("Num → Esc");
        return value.charAt(0).toUpperCase() + value.slice(1);
    }

    function send(message: var): void {
        socket.write(JSON.stringify(message) + "\n");
    }

    // Changes are merged and sent at most every 50 ms: a slider or a drag on
    // the wheel produces many per second, the daemon saves each one.
    property var pending: null

    function setState(patch: var): void {
        if (pending === null)
            pending = {};
        for (const key of Object.keys(patch)) {
            if (key === "params") {
                pending.params = Object.assign(pending.params ?? {}, patch.params);
            } else {
                pending[key] = patch[key];
            }
        }
        if (!flush.running)
            flush.start();
    }

    function setParam(name: string, value: var): void {
        const p = {};
        p[name] = value;
        setState({
            params: p
        });
    }

    function pickColour(hex: string): void {
        const spec = effectParams.find(p => p.name === target);
        if (!spec) {
            // the effect has no colour (white, battery...): a colour means "static"
            setState({
                effect: "static",
                on: true,
                params: {
                    color: hex
                }
            });
            return;
        }
        if (spec.type === "colors") {
            const zones = (params[spec.name] ?? spec.default).slice(0, 4);
            while (zones.length < 4)
                zones.push(zones[zones.length - 1] ?? hex);
            if (targetZone === 0) {
                for (let i = 0; i < 4; i++)
                    zones[i] = hex;
            } else {
                zones[targetZone - 1] = hex;
            }
            setParam(spec.name, zones);
        } else {
            setParam(spec.name, hex);
        }
    }

    function applyState(state: var): void {
        lightOn = state.on;
        effect = state.effect;
        params = state.params;
        brightness = state.brightness;
        maxMode = state.max;
        if (!colourTargets.some(p => p.name === target)) {
            target = colourTargets.length ? colourTargets[0].name : "color";
            targetZone = 0;
        }
    }

    function handleLine(line: string): void {
        let message;
        try {
            message = JSON.parse(line);
        } catch (e) {
            return;
        }
        daemonUp = true;
        if (message.effects !== undefined)
            effectList = message.effects;
        if (message.presets !== undefined) {
            presetMap = message.presets;
            myColours = message.colours ?? [];
        }
        if (message.state !== undefined)
            applyState(message.state);
        if (message.ok === false)
            console.warn("keyboard-konf:", message.error);
    }

    // ------------------------------------------------------------ socket

    Socket {
        id: socket

        path: {
            const override = Quickshell.env("KKONF_SOCKET");
            if (override)
                return override;
            return `${Quickshell.env("XDG_RUNTIME_DIR") || "/run/user/1000"}/keyboard-konf.sock`;
        }
        connected: true
        parser: SplitParser {
            onRead: line => root.handleLine(line)
        }
    }

    Timer {
        id: flush

        interval: 50
        onTriggered: {
            if (root.pending !== null) {
                root.send({
                    cmd: "set",
                    state: root.pending
                });
                root.pending = null;
            }
        }
    }

    Timer {
        // Until the daemon answers: (re)connect and ask for everything. The
        // socket's connected signals are not reliable, so this polls.
        interval: 2000
        running: true
        repeat: true
        triggeredOnStart: true
        onTriggered: {
            if (!socket.connected) {
                root.daemonUp = false;
                socket.connected = true;
            }
            if (!root.daemonUp) {
                root.send({
                    cmd: "effects"
                });
                root.send({
                    cmd: "presets"
                });
                root.send({
                    cmd: "get"
                });
            }
        }
    }

    // ------------------------------------------------------------ controls

    SwitchRow {
        label: qsTr("Backlight")
        checked: root.lightOn
        enabled: root.daemonUp
        onToggled: checked => root.setState({
            on: checked
        })
    }

    SectionContainer {
        contentSpacing: Tokens.spacing.normal

        SliderInput {
            Layout.fillWidth: true

            label: qsTr("Brightness")
            value: root.brightness * 100
            from: 0
            to: 100
            suffix: "%"
            validator: IntValidator {
                bottom: 0
                top: 100
            }
            formatValueFunction: v => Math.round(v).toString()
            parseValueFunction: t => parseInt(t)

            onValueModified: v => root.setState({
                brightness: v / 100
            })
        }
    }

    SwitchRow {
        label: qsTr("Maximum brightness")
        checked: root.maxMode
        enabled: root.daemonUp
        onToggled: checked => root.setState({
            max: checked
        })
    }

    StyledText {
        Layout.topMargin: Tokens.spacing.normal
        text: qsTr("Effect")
        font.pointSize: Tokens.font.size.larger
        font.weight: 500
    }

    Flow {
        Layout.fillWidth: true
        spacing: Tokens.spacing.small

        Repeater {
            model: root.effectList

            TextButton {
                required property var modelData

                text: modelData.label
                type: TextButton.Tonal
                font.pointSize: Tokens.font.size.normal
                horizontalPadding: Tokens.padding.large
                verticalPadding: Tokens.padding.normal
                checked: root.effect === modelData.name
                onClicked: root.setState({
                    effect: modelData.name,
                    on: true
                })
            }
        }
    }

    // Parameters of the current effect, only the ones it uses. The block
    // grows and shrinks with the effect instead of reserving the height of
    // the largest one, so the page stays short.
    ColumnLayout {
        Layout.fillWidth: true
        spacing: Tokens.spacing.small

    Repeater {
        model: root.sliderParams

        SectionContainer {
            id: sliderRow

            required property var modelData
            readonly property bool kelvin: modelData.type === "kelvin"
            readonly property real current: root.params[modelData.name] ?? modelData.default

            contentSpacing: Tokens.spacing.normal

            SliderInput {
                Layout.fillWidth: true

                label: sliderRow.modelData.label
                value: sliderRow.kelvin ? sliderRow.current : sliderRow.current * 100
                from: sliderRow.kelvin ? sliderRow.modelData.min : 0
                to: sliderRow.kelvin ? sliderRow.modelData.max : 100
                stepSize: sliderRow.kelvin ? 100 : 0
                suffix: sliderRow.kelvin ? "K" : "%"
                validator: IntValidator {
                    bottom: sliderRow.kelvin ? sliderRow.modelData.min : 0
                    top: sliderRow.kelvin ? sliderRow.modelData.max : 100
                }
                formatValueFunction: v => Math.round(v).toString()
                parseValueFunction: t => parseInt(t)

                onValueModified: v => root.setParam(sliderRow.modelData.name, sliderRow.kelvin ? Math.round(v) : v / 100)
            }
        }
    }

    Repeater {
        model: root.choiceParams

        StyledRect {
            id: choiceRow

            required property var modelData
            readonly property string current: root.params[modelData.name] ?? modelData.default

            Layout.fillWidth: true
            implicitHeight: choiceLayout.implicitHeight + Tokens.padding.large * 2
            radius: Tokens.rounding.normal
            color: Colours.layer(Colours.palette.m3surfaceContainer, 2)

            RowLayout {
                id: choiceLayout

                anchors.left: parent.left
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                anchors.margins: Tokens.padding.large
                spacing: Tokens.spacing.small

                StyledText {
                    Layout.fillWidth: true
                    text: choiceRow.modelData.label
                }

                Repeater {
                    model: choiceRow.modelData.choices

                    TextButton {
                        required property string modelData

                        text: root.choiceLabel(choiceRow.modelData.name, modelData)
                        type: TextButton.Tonal
                        font.pointSize: Tokens.font.size.normal
                        horizontalPadding: Tokens.padding.large
                        verticalPadding: Tokens.padding.normal
                        checked: choiceRow.current === modelData
                        onClicked: root.setParam(choiceRow.modelData.name, modelData)
                    }
                }
            }
        }
    }

    }

    // ------------------------------------------------------------ colour

    StyledText {
        Layout.topMargin: Tokens.spacing.normal
        text: qsTr("Colour")
        font.pointSize: Tokens.font.size.larger
        font.weight: 500
    }

    Flow {
        Layout.fillWidth: true
        spacing: Tokens.spacing.small

        Repeater {
            model: root.targetChips

            StyledRect {
                id: chip

                required property var modelData
                readonly property bool selected: modelData.live && root.target === modelData.name && root.targetZone === modelData.zone

                implicitWidth: chipRow.implicitWidth + Tokens.padding.large * 2
                implicitHeight: chipRow.implicitHeight + Tokens.padding.normal * 2
                radius: Tokens.rounding.full
                color: selected ? Colours.palette.m3secondaryContainer : Colours.layer(Colours.palette.m3surfaceContainer, 2)
                border.width: selected ? 0 : 1
                border.color: Qt.alpha(Colours.palette.m3outline, 0.3)

                StateLayer {
                    function onClicked(): void {
                        if (!chip.modelData.live)
                            return;
                        root.target = chip.modelData.name;
                        root.targetZone = chip.modelData.zone;
                    }

                    color: Colours.palette.m3onSurface
                    disabled: !chip.modelData.live
                }

                RowLayout {
                    id: chipRow

                    anchors.centerIn: parent
                    spacing: Tokens.spacing.small

                    Rectangle {
                        implicitWidth: Tokens.font.size.large
                        implicitHeight: Tokens.font.size.large
                        radius: width / 2
                        color: chip.modelData.colour
                        border.width: 1
                        border.color: Qt.alpha(Colours.palette.m3onSurface, 0.3)
                    }

                    StyledText {
                        text: chip.modelData.label
                        font.pointSize: Tokens.font.size.normal
                        color: chip.selected ? Colours.palette.m3onSecondaryContainer : Colours.palette.m3onSurface
                    }
                }
            }
        }
    }

    SectionContainer {
        contentSpacing: Tokens.spacing.normal

        KeyboardPalette {
            Layout.fillWidth: true

            current: root.currentColour
            mine: root.myColours
            onPicked: hex => root.pickColour(hex)
            onAddMine: hex => root.send({
                cmd: "add_colour",
                color: hex
            })
            onRemoveMine: hex => root.send({
                cmd: "remove_colour",
                color: hex
            })
        }
    }

    SectionContainer {
        contentSpacing: Tokens.spacing.normal

        KeyboardColourWheel {
            Layout.fillWidth: true

            colour: root.currentColour
            onPicked: hex => root.pickColour(hex)
        }
    }

    // ------------------------------------------------------------ presets

    StyledText {
        Layout.topMargin: Tokens.spacing.normal
        text: qsTr("Presets")
        font.pointSize: Tokens.font.size.larger
        font.weight: 500
    }

    Repeater {
        model: Object.keys(root.presetMap)

        StyledRect {
            id: presetRow

            required property string modelData

            Layout.fillWidth: true
            implicitHeight: presetLayout.implicitHeight + Tokens.padding.normal * 2
            radius: Tokens.rounding.normal
            color: Colours.layer(Colours.palette.m3surfaceContainer, 2)

            RowLayout {
                id: presetLayout

                anchors.left: parent.left
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                anchors.margins: Tokens.padding.normal
                spacing: Tokens.spacing.small

                StyledText {
                    Layout.fillWidth: true
                    Layout.leftMargin: Tokens.padding.small
                    text: presetRow.modelData
                    elide: Text.ElideRight
                }

                IconButton {
                    icon: "play_arrow"
                    type: IconButton.Tonal
                    font.pointSize: Tokens.font.size.large
                    onClicked: root.send({
                        cmd: "preset",
                        name: presetRow.modelData
                    })
                }

                IconButton {
                    icon: "delete"
                    type: IconButton.Text
                    font.pointSize: Tokens.font.size.large
                    onClicked: root.send({
                        cmd: "delete_preset",
                        name: presetRow.modelData
                    })
                }
            }
        }
    }

    RowLayout {
        Layout.fillWidth: true
        spacing: Tokens.spacing.small

        StyledInputField {
            id: presetName

            Layout.fillWidth: true
            horizontalAlignment: TextInput.AlignLeft
        }

        TextButton {
            text: qsTr("Save current")
            type: TextButton.Filled
            font.pointSize: Tokens.font.size.normal
            horizontalPadding: Tokens.padding.large
            verticalPadding: Tokens.padding.normal
            enabled: root.daemonUp && presetName.text.trim() !== ""
            opacity: enabled ? 1 : 0.5
            onClicked: {
                root.send({
                    cmd: "save_preset",
                    name: presetName.text.trim()
                });
                presetName.text = "";
            }
        }
    }
}
