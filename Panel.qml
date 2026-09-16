import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

// Window layout templates.
//
// The bar entry opens a panel listing saved templates. "Save" snapshots every
// window on every workspace (app, launch command, workspace, monitor, tiling
// geometry, floating/fullscreen state) into a named template; picking a
// template restores it: windows that are already open are moved into place,
// missing apps are launched, windows that are not part of the template are
// closed, and each workspace's dwindle tree is rebuilt to match the snapshot.
//
// All Hyprland work happens in layouts.py, which streams one JSON event per
// line. The panel only renders those events, so the same helper also backs
// the IPC target, keybindings and the optional login restore.
Panel {
  id: root

  moduleName: "funcoder.window-layouts"
  ipcTarget: "funcoder.window-layouts"
  manageIpc: false

  readonly property color foreground: bar ? bar.barForeground : Color.foreground
  readonly property color dim: Qt.darker(foreground, 1.4)
  readonly property color urgent: bar && bar.urgent !== undefined ? bar.urgent : Color.urgent
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property string helper: {
    var url = String(Qt.resolvedUrl("layouts.py"))
    return url.indexOf("file://") === 0 ? decodeURIComponent(url.slice(7)) : url
  }

  readonly property bool closeOthers: setting("closeOthers", true) === true
  readonly property int launchTimeout: Math.max(5, Number(setting("launchTimeout", 45)) || 45)
  readonly property bool autosave: setting("autosave", true) === true
  readonly property int autosaveMinutes: Math.max(1, Number(setting("autosaveMinutes", 5)) || 5)
  readonly property string startupTemplate: String(setting("startupTemplate", "")).trim()

  property var templates: []
  property int currentWindows: 0
  property int currentWorkspaces: 0
  property string lastApplied: ""

  property int selectedIndex: 0
  property bool cursorActive: false

  property bool busy: false
  property string busyName: ""
  property int progressPlaced: 0
  property int progressTotal: 0
  property string statusText: ""
  property bool statusIsError: false

  // "", "delete" or "overwrite"; confirmName is the template it targets.
  property string confirmMode: ""
  property string confirmName: ""

  readonly property int maxRows: 6
  readonly property real progressFraction: progressTotal > 0 ? Math.min(1, progressPlaced / progressTotal) : 0

  visible: true
  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  // ---- helpers ---------------------------------------------------------------

  function templateIndex(name) {
    for (var i = 0; i < templates.length; i++) if (templates[i].name === name) return i
    return -1
  }

  function whenText(iso) {
    if (!iso) return ""
    var d = new Date(iso)
    if (isNaN(d.getTime())) return ""
    var pad = function(n) { return n < 10 ? "0" + n : String(n) }
    var now = new Date()
    var time = pad(d.getHours()) + ":" + pad(d.getMinutes())
    if (d.toDateString() === now.toDateString()) return "today " + time
    var months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    return d.getDate() + " " + months[d.getMonth()] + " " + time
  }

  function metaText(tpl) {
    if (!tpl) return ""
    var parts = []
    var n = Number(tpl.windows) || 0
    parts.push(n + (n === 1 ? " window" : " windows"))
    if (tpl.workspaces && tpl.workspaces.length > 0) parts.push("ws " + tpl.workspaces.join(" "))
    var when = whenText(tpl.savedAt)
    if (when !== "") parts.push(when)
    return parts.join("  ·  ")
  }

  function setStatus(text, isError) {
    statusText = text || ""
    statusIsError = isError === true
  }

  function move(delta) {
    if (templates.length === 0) return
    if (!cursorActive) { cursorActive = true; return }
    selectedIndex = Math.max(0, Math.min(templates.length - 1, selectedIndex + delta))
    templateList.positionViewAtIndex(selectedIndex, ListView.Contain)
  }

  // ---- actions -----------------------------------------------------------------

  function refresh() {
    if (listProc.running) return
    listProc.command = [helper, "list"]
    listProc.running = true
  }

  function parseList(raw) {
    try {
      var parsed = JSON.parse(String(raw || "{}"))
      templates = Array.isArray(parsed.templates) ? parsed.templates : []
      currentWindows = parsed.current ? Number(parsed.current.windows) || 0 : 0
      currentWorkspaces = parsed.current ? Number(parsed.current.workspaces) || 0 : 0
      if (parsed.lastApplied) lastApplied = String(parsed.lastApplied)
    } catch (e) {
      console.warn(moduleName + ": invalid list output", e)
      templates = []
    }
    if (selectedIndex >= templates.length) selectedIndex = Math.max(0, templates.length - 1)
  }

  function runAction(args) {
    if (actionProc.running) return false
    setStatus("", false)
    actionProc.command = [helper].concat(args)
    actionProc.running = true
    return true
  }

  function applyTemplate(name) {
    if (busy || !name) return
    var args = ["apply", name, "--timeout", String(launchTimeout), "--notify"]
    if (!closeOthers) args.push("--keep-others")
    if (!runAction(args)) return
    busy = true
    busyName = name
    progressPlaced = 0
    progressTotal = 0
    // Restoring moves keyboard focus between windows; the layer-shell panel
    // would fight those focus changes, so get out of the way.
    close()
  }

  function saveTemplate(name, force) {
    name = String(name || "").trim()
    if (busy) return
    if (name === "") {
      setStatus("Give the template a name first", true)
      nameField.forceActiveFocus()
      return
    }
    if (!force && templateIndex(name) >= 0) {
      askConfirm("overwrite", name)
      return
    }
    if (runAction(["save", name])) {
      busy = true
      busyName = name
    }
  }

  function deleteTemplate(name) {
    if (busy || !name) return
    runAction(["delete", name])
  }

  function askConfirm(mode, name) {
    confirmMode = mode
    confirmName = name
    confirm.selectedIndex = 1
  }

  function resolveConfirm(accepted) {
    var mode = confirmMode
    var name = confirmName
    confirmMode = ""
    confirmName = ""
    if (!accepted) return
    if (mode === "delete") deleteTemplate(name)
    else if (mode === "overwrite") {
      saveTemplate(name, true)
      nameField.text = ""
    }
  }

  function handleEvent(line) {
    var ev
    try { ev = JSON.parse(line) } catch (e) { return }
    if (!ev || !ev.event) return

    if (ev.event === "progress") {
      progressPlaced = Number(ev.placed) || 0
      progressTotal = Number(ev.total) || 0
      setStatus(ev.message || "", false)
    } else if (ev.event === "done") {
      lastApplied = ev.name || busyName
      var msg = "Restored “" + lastApplied + "”"
      if (ev.missing && ev.missing.length > 0) msg += " · missing " + ev.missing.join(", ")
      setStatus(msg, ev.missing && ev.missing.length > 0)
    } else if (ev.event === "saved") {
      setStatus("Saved “" + ev.name + "” · " + ev.windows + (ev.windows === 1 ? " window" : " windows"), false)
    } else if (ev.event === "deleted") {
      setStatus("Deleted “" + ev.name + "”", false)
    } else if (ev.event === "error") {
      setStatus(ev.message || "Something went wrong", true)
    }
  }

  onOpenedChanged: if (opened) {
    refresh()
    confirmMode = ""
    nameField.text = ""
    cursorActive = false
    var idx = templateIndex(lastApplied)
    selectedIndex = idx >= 0 ? idx : 0
    if (!busy && statusIsError === false) setStatus("", false)
  }

  Component.onCompleted: {
    refresh()
    if (startupTemplate !== "") {
      // The helper only restores once per login (runtime marker), so shell
      // restarts and per-monitor widget instances don't re-run it.
      var args = [helper, "startup", startupTemplate, "--timeout", String(launchTimeout), "--notify"]
      if (!closeOthers) args.push("--keep-others")
      Quickshell.execDetached(args)
    }
  }

  // ---- processes -----------------------------------------------------------------

  Process {
    id: listProc
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.parseList(text)
    }
  }

  Process {
    id: actionProc
    stdout: SplitParser {
      onRead: function(line) { root.handleEvent(line) }
    }
    onExited: function(exitCode) {
      root.busy = false
      root.busyName = ""
      if (exitCode !== 0 && !root.statusIsError) root.setStatus("Helper failed (error " + exitCode + ")", true)
      root.refresh()
    }
  }

  Timer {
    interval: root.autosaveMinutes * 60000
    running: root.autosave
    repeat: true
    onTriggered: if (!root.busy) Quickshell.execDetached([root.helper, "autosave"])
  }

  Timer {
    interval: 5000
    running: root.opened
    repeat: true
    onTriggered: root.refresh()
  }

  IpcHandler {
    target: root.ipcTarget

    function open(): void { root.open() }
    function close(): void { root.close() }
    function show(): void { root.open() }
    function hide(): void { root.close() }
    function toggle(): void { root.toggle() }
    function apply(name: string): void { root.applyTemplate(name) }
    function save(name: string): void { root.saveTemplate(name, true) }
  }

  // ---- bar entry -------------------------------------------------------------------

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: "\uF009"
    active: root.busy
    tooltipText: root.busy
      ? "Restoring “" + root.busyName + "”" + (root.progressTotal > 0 ? " · " + root.progressPlaced + "/" + root.progressTotal : "…")
      : "Window layouts"
    onPressed: function(b) { root.toggle() }
  }

  // ---- panel -----------------------------------------------------------------------

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(420))
    contentHeight: panel.fittedContentHeight(Math.max(column.implicitHeight, root.confirmMode !== "" ? Style.space(170) : 0))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      blocked: nameField.activeFocus

      onMoveRequested: function(dx, dy) {
        if (root.confirmMode !== "") {
          if (dx !== 0) confirm.selectedIndex = confirm.selectedIndex === 0 ? 1 : 0
          return
        }
        if (dy !== 0) root.move(dy)
      }
      onActivateRequested: {
        if (root.confirmMode !== "") root.resolveConfirm(confirm.selectedIndex === 1)
        else if (root.cursorActive && root.templates.length > 0) root.applyTemplate(root.templates[root.selectedIndex].name)
      }
      onReturnRequested: activateRequested()
      onDeleteRequested: {
        if (root.confirmMode === "" && root.cursorActive && root.templates.length > 0)
          root.askConfirm("delete", root.templates[root.selectedIndex].name)
      }
      onCloseRequested: {
        if (root.confirmMode !== "") root.resolveConfirm(false)
        else root.close()
      }
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onTextKey: function(t) {
        if (root.confirmMode !== "") return
        nameField.insert(nameField.cursorPosition, t)
        nameField.forceActiveFocus()
      }

      Column {
        id: column
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        spacing: Style.space(14)

        // ---------- Hero ----------
        Item {
          width: parent.width
          implicitHeight: Math.max(heroIcon.implicitHeight, heroLabels.implicitHeight)

          Text {
            id: heroIcon
            textFormat: Text.PlainText
            text: "\uF009"
            color: root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.display
            anchors.left: parent.left
            anchors.verticalCenter: parent.verticalCenter
          }

          Column {
            id: heroLabels
            anchors.left: heroIcon.right
            anchors.leftMargin: Style.space(14)
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            spacing: Style.space(2)

            Text {
              width: parent.width
              text: "Window Layouts"
              textFormat: Text.PlainText
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.title
              font.bold: true
              elide: Text.ElideRight
            }

            Text {
              width: parent.width
              textFormat: Text.PlainText
              text: root.busy
                ? ("Restoring " + root.busyName).toUpperCase()
                : (root.currentWindows + (root.currentWindows === 1 ? " window" : " windows")
                  + " on " + root.currentWorkspaces + (root.currentWorkspaces === 1 ? " workspace" : " workspaces")).toUpperCase()
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              font.bold: true
              font.letterSpacing: 1.2
              elide: Text.ElideRight
            }
          }
        }

        // ---------- Restore progress ----------
        Item {
          visible: root.busy && root.progressTotal > 0
          width: parent.width
          implicitHeight: Style.space(8)

          Rectangle {
            id: track
            anchors.fill: parent
            radius: height / 2
            color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.12)
          }

          Rectangle {
            anchors.left: track.left
            anchors.verticalCenter: track.verticalCenter
            height: track.height
            radius: track.radius
            color: root.foreground
            width: Math.max(track.height, track.width * root.progressFraction)
            Behavior on width { NumberAnimation { duration: 320; easing.type: Easing.OutCubic } }
          }
        }

        PanelSeparator { foreground: root.foreground }

        // ---------- Save ----------
        Column {
          width: parent.width
          spacing: Style.space(10)

          PanelSectionHeader {
            text: "SAVE CURRENT LAYOUT"
            foreground: root.foreground
            fontFamily: root.fontFamily
          }

          Row {
            width: parent.width
            spacing: Style.spacing.sm

            TextField {
              id: nameField
              width: parent.width - saveButton.width - parent.spacing
              placeholderText: "Template name, e.g. Client work…"
              foreground: root.foreground
              enabled: !root.busy
              onAccepted: root.saveTemplate(text, false)
              Keys.onDownPressed: {
                keyCatcher.forceActiveFocus()
                root.cursorActive = true
              }
              Keys.onEscapePressed: {
                if (text !== "") text = ""
                else root.close()
              }
            }

            Button {
              id: saveButton
              iconText: "\uF0C7"
              iconSize: Style.font.title
              text: "Save"
              fontSize: Style.font.bodySmall
              foreground: root.foreground
              fontFamily: root.fontFamily
              bordered: true
              focusable: true
              enabled: !root.busy
              onClicked: root.saveTemplate(nameField.text, false)
            }
          }
        }

        PanelSeparator { foreground: root.foreground }

        // ---------- Templates ----------
        Column {
          width: parent.width
          spacing: Style.space(10)

          PanelSectionHeader {
            text: "TEMPLATES"
            foreground: root.foreground
            fontFamily: root.fontFamily
          }

          ListView {
            id: templateList
            width: parent.width
            height: Math.min(contentHeight, root.maxRows * Style.space(52))
            visible: root.templates.length > 0
            clip: true
            spacing: Style.spacing.xxs
            boundsBehavior: Flickable.StopAtBounds
            interactive: contentHeight > height
            model: root.templates
            delegate: TemplateRow {}
          }

          Text {
            visible: root.templates.length === 0
            width: parent.width
            text: "No templates yet. Arrange your windows across your workspaces, give the layout a name and save it."
            textFormat: Text.PlainText
            wrapMode: Text.WordWrap
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
            horizontalAlignment: Text.AlignHCenter
            topPadding: Style.space(10)
            bottomPadding: Style.space(10)
          }
        }

        Text {
          visible: root.statusText !== ""
          width: parent.width
          text: root.statusText
          textFormat: Text.PlainText
          wrapMode: Text.WordWrap
          color: root.statusIsError ? root.urgent : root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
        }

        Text {
          width: parent.width
          text: "↑↓ select   ·   Enter restore   ·   Del delete   ·   Esc close"
          textFormat: Text.PlainText
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          horizontalAlignment: Text.AlignRight
          elide: Text.ElideLeft
        }
      }

      ConfirmDialog {
        id: confirm
        anchors.fill: parent
        z: 10
        opened: root.confirmMode !== ""
        message: root.confirmMode === "delete"
          ? "Delete the “" + root.confirmName + "” template?"
          : "Replace “" + root.confirmName + "” with the windows that are open right now?"
        confirmText: root.confirmMode === "delete" ? "Delete" : "Replace"
        foreground: root.foreground
        fontFamily: root.fontFamily
        background: Color.popups.background
        onCanceled: root.resolveConfirm(false)
        onConfirmed: root.resolveConfirm(true)
      }
    }
  }

  // One saved template: click restores; trailing actions replace or delete.
  component TemplateRow: CursorSurface {
    id: row
    required property var modelData
    required property int index

    readonly property var tpl: modelData || ({})
    readonly property bool rowSelected: root.cursorActive && root.selectedIndex === index

    width: templateList.width
    implicitHeight: rowContent.implicitHeight + Style.spacing.rowPaddingX
    height: implicitHeight
    hasCursor: rowSelected
    current: tpl.name === root.lastApplied
    foreground: root.foreground

    MouseArea {
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: Qt.PointingHandCursor
      onContainsMouseChanged: if (containsMouse) {
        root.cursorActive = true
        root.selectedIndex = row.index
      }
      onClicked: root.applyTemplate(row.tpl.name)
    }

    Item {
      id: rowContent
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      anchors.leftMargin: Style.spacing.rowPaddingX
      anchors.rightMargin: Style.spacing.sm
      implicitHeight: Math.max(labels.implicitHeight, actions.implicitHeight)

      Column {
        id: labels
        anchors.left: parent.left
        anchors.right: actions.left
        anchors.rightMargin: Style.spacing.md
        anchors.verticalCenter: parent.verticalCenter
        spacing: Style.space(2)

        Text {
          width: parent.width
          text: (row.tpl.auto ? "\uF017 " : "") + (row.tpl.name || "")
          textFormat: Text.PlainText
          elide: Text.ElideRight
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
        }

        Text {
          width: parent.width
          text: root.metaText(row.tpl)
          textFormat: Text.PlainText
          elide: Text.ElideRight
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
        }
      }

      Row {
        id: actions
        anchors.right: parent.right
        anchors.verticalCenter: parent.verticalCenter
        spacing: Style.spacing.xs
        opacity: row.rowSelected ? 1 : 0
        enabled: row.rowSelected && !root.busy

        PanelActionButton {
          iconText: "\uF04B"
          tooltipText: "Restore"
          foreground: root.foreground
          fontFamily: root.fontFamily
          onClicked: root.applyTemplate(row.tpl.name)
        }

        PanelActionButton {
          iconText: "\uF0E2"
          tooltipText: "Replace with the current layout"
          foreground: root.foreground
          fontFamily: root.fontFamily
          onClicked: root.askConfirm("overwrite", row.tpl.name)
        }

        PanelActionButton {
          iconText: "\uF1F8"
          tooltipText: "Delete"
          foreground: root.foreground
          hoverColor: root.urgent
          fontFamily: root.fontFamily
          onClicked: root.askConfirm("delete", row.tpl.name)
        }
      }
    }
  }
}
