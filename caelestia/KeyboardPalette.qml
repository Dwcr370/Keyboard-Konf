pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Layouts
import Caelestia.Config
import qs.components
import qs.components.controls
import qs.services

// Ready-made colours in three rows: the shell's own Material scheme (it
// follows the wallpaper), twelve basic hues in a pastel, a pure and a deep
// variant, and the user's own list kept by the daemon.
ColumnLayout {
    id: root

    property string current: "#FFFFFF"
    property var mine: []

    signal picked(string hex)
    signal addMine(string hex)
    signal removeMine(string hex)

    readonly property real swatch: 40
    readonly property bool currentIsMine: mine.includes(current)

    readonly property var systemColours: [
        {label: qsTr("Primary"), c: Colours.palette.m3primary},
        {label: qsTr("Secondary"), c: Colours.palette.m3secondary},
        {label: qsTr("Tertiary"), c: Colours.palette.m3tertiary},
        {label: qsTr("Error"), c: Colours.palette.m3error},
        {label: qsTr("Primary container"), c: Colours.palette.m3primaryContainer},
        {label: qsTr("Tertiary container"), c: Colours.palette.m3tertiaryContainer}
    ]

    readonly property var basicColours: {
        const list = [];
        for (const [s, v] of [[0.45, 1.0], [1.0, 1.0], [1.0, 0.5]])
            for (let i = 0; i < 12; i++)
                list.push(Qt.hsva(i / 12, s, v, 1));
        return list;
    }

    spacing: Tokens.spacing.small

    function hexOf(c: color): string {
        return c.toString().toUpperCase();
    }

    component Swatch: StyledRect {
        id: sw

        required property color c
        readonly property bool selected: root.hexOf(c) === root.current

        implicitWidth: root.swatch
        implicitHeight: root.swatch
        radius: root.swatch / 2
        color: c
        border.width: selected ? 3 : 0
        border.color: Colours.palette.m3onSurface

        StateLayer {
            function onClicked(): void {
                root.picked(root.hexOf(sw.c));
            }

            color: Colours.palette.m3onSurface
        }
    }

    component Caption: StyledText {
        color: Colours.palette.m3onSurfaceVariant
        font.pointSize: Tokens.font.size.small
    }

    Caption {
        text: qsTr("System")
    }

    Flow {
        Layout.fillWidth: true
        spacing: Tokens.spacing.small

        Repeater {
            model: root.systemColours

            Swatch {
                required property var modelData

                c: modelData.c
            }
        }
    }

    Caption {
        Layout.topMargin: Tokens.spacing.small
        text: qsTr("Basic")
    }

    Flow {
        Layout.fillWidth: true
        spacing: Tokens.spacing.small

        Repeater {
            model: root.basicColours

            Swatch {
                required property color modelData

                c: modelData
            }
        }
    }

    Caption {
        Layout.topMargin: Tokens.spacing.small
        text: qsTr("Mine")
    }

    Flow {
        Layout.fillWidth: true
        spacing: Tokens.spacing.small

        Repeater {
            model: root.mine

            Swatch {
                required property string modelData

                c: modelData
            }
        }

        IconButton {
            icon: root.currentIsMine ? "remove" : "add"
            type: IconButton.Tonal
            implicitWidth: root.swatch
            implicitHeight: root.swatch
            padding: 0
            font.pointSize: Tokens.font.size.large
            onClicked: root.currentIsMine ? root.removeMine(root.current) : root.addMine(root.current)
        }
    }
}
