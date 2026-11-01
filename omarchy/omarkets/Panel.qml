import QtQuick
import Quickshell
import qs.Commons
import qs.Ui

// Omarkets compact popup panel.
//
// Presentation only. It reads the OmarketsService the bar widget owns and renders
// the core's deterministic, already-computed state: the latest plain-English
// desk note, the market strip, the next economic event, and a DATA-vs-AI
// diagnostics split. It never computes a value and never triggers a model call.

Panel {
  id: root
  moduleName: "omarkets"
  // Opened through the shell's bar-widget routing (Bar.findPanelWidget), which is
  // what `omarchy-shell shell toggle omarkets` and a hotkey use.
  ipcTarget: "omarkets"
  manageIpc: false

  property var anchorItem: null
  property var hostWidget: null
  // Injected by the BarWidget; the single shared connection.
  property var service: null

  readonly property color fg: bar ? bar.foreground : Color.foreground
  readonly property color dim: Qt.darker(fg, 1.55)
  readonly property color accent: Color.accent
  // State colours follow the active Omarchy theme. Omarchy's shell retints
  // Color.* instantly; the extra roles come from the core's /api/v1/theme via
  // the collector, refreshed on every theme switch (see BarWidget).
  readonly property var themeColors: root.service && root.service.themeColors ? root.service.themeColors : null
  function themeColor(role, fallback) {
    var value = root.themeColors ? root.themeColors[role] : null
    return (typeof value === "string" && /^#[0-9a-fA-F]{6}$/.test(value)) ? value : fallback
  }
  readonly property color upColor: root.themeColor("green", "#9ece6a")
  readonly property color downColor: root.themeColor("red", Color.urgent)
  readonly property color warnColor: root.themeColor("orange", "#e0af68")
  readonly property color infoColor: root.themeColor("blue", Color.accent)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

  readonly property string transportState: root.service ? root.service.transportState : "unavailable"
  readonly property bool online: root.transportState === "connected"
  readonly property bool showSnapshot: root.service && root.service.status !== null
  // Prefer the wider context strip (adds ES/US10Y/DXY/VXN) when the optional
  // context payload is present; fall back to the bar strip.
  readonly property var strip: (root.service && root.service.crossMarket.length > 0)
    ? root.service.crossMarket
    : (root.service ? root.service.strip : [])
  readonly property var nextEvent: root.service ? root.service.nextEvent : null
  readonly property var riskTone: root.service ? root.service.riskTone : null
  function toneColor(tone) {
    switch (String(tone || "")) {
      case "risk_on": case "leaning_risk_on": return root.upColor
      case "risk_off": case "leaning_risk_off": return root.downColor
      case "mixed": return root.warnColor
      default: return root.dim
    }
  }
  // Latest plain-English desk note (headline + summary).
  readonly property var deskNote: root.service && root.service.deskNote ? root.service.deskNote : null
  readonly property var deskKinds: ({ primer: "WEEK AHEAD", premarket: "PRE-MARKET", release: "ECONOMIC DATA",
    fed: "FEDERAL RESERVE", earnings: "EARNINGS", move: "BIG MOVE", ask: "MARKET NOW",
    review: "WEEK IN REVIEW", auction: "TREASURY AUCTION" })
  readonly property var deskTones: ({ supportive: "Supportive", negative: "Negative",
    mixed: "Mixed", neutral: "Neutral" })
  // "Negative for ES": the tone word plus the instrument the note's tone is about.
  function deskToneText(note) {
    var word = note ? root.deskTones[note.tone] : ""
    if (!word) return ""
    return note.tone_for ? word + " for " + note.tone_for : word
  }
  function deskTime(iso) {
    var date = new Date(iso)
    return isNaN(date.getTime()) ? "" : Qt.formatDateTime(date, "ddd HH:mm")
  }
  property bool diagnosticsOpen: false
  readonly property var providers: root.service ? root.service.providers : []
  readonly property string dataHealth: root.service ? root.service.dataHealth : "unknown"
  readonly property string aiHealth: root.service ? root.service.aiHealth : "unknown"
  readonly property var session: root.service ? root.service.session : null
  readonly property double ageSeconds: root.service ? root.service.ageSeconds : -1

  // Refresh the countdown once a second while open. This is a display timer; it
  // does not poll the network (the service owns polling).
  property double nowMs: Date.now()
  Timer {
    interval: 1000
    running: root.opened === true
    repeat: true
    onTriggered: root.nowMs = Date.now()
  }

  function fmtValue(v, digits) {
    if (v === null || v === undefined) return "—"
    var n = Number(v); if (isNaN(n)) return "—"
    return n.toLocaleString(Qt.locale("en_US"), "f", digits === undefined ? 2 : digits)
  }
  function fmtPct(v) {
    if (v === null || v === undefined) return "—"
    var n = Number(v); if (isNaN(n)) return "—"
    return (n >= 0 ? "+" : "") + n.toFixed(2) + "%"
  }
  function fmtBp(v) {
    if (v === null || v === undefined) return "—"
    var n = Number(v); if (isNaN(n)) return "—"
    return (n >= 0 ? "+" : "") + n.toFixed(1) + "bp"
  }
  function signedColor(v) {
    if (v === null || v === undefined) return root.dim
    var n = Number(v); if (isNaN(n) || n === 0) return root.dim
    return n > 0 ? root.upColor : root.downColor
  }
  // Feed status is displayed as text and colour; never inferred.
  function feedColor(status) {
    switch (String(status || "")) {
      case "live": return root.upColor
      case "delayed": return root.warnColor
      case "stale": return root.warnColor
      case "demo": return root.infoColor
      default: return root.downColor
    }
  }
  function healthColor(health) {
    switch (String(health || "")) {
      case "ok": return root.upColor
      case "degraded": return root.warnColor
      case "disabled": return root.dim
      case "down": return root.downColor
      default: return root.dim
    }
  }
  function importanceColor(importance) {
    switch (String(importance || "")) {
      case "critical": return root.downColor
      case "high": return root.warnColor
      default: return root.dim
    }
  }
  function countdown(iso) {
    if (!iso) return ""
    var when = Date.parse(String(iso)); if (isNaN(when)) return ""
    var delta = when - root.nowMs
    if (delta <= 0) return "now"
    var mins = Math.floor(delta / 60000)
    if (mins < 5) {
      return new Date(when).toLocaleTimeString(Qt.locale("en_US"),
        { hour: "2-digit", minute: "2-digit", hour12: false })
    }
    if (mins < 60) return mins + "m"
    var hours = Math.floor(mins / 60)
    if (hours < 48) return hours + "h " + (mins % 60) + "m"
    return Math.floor(hours / 24) + "d " + (hours % 24) + "h"
  }
  function valueOf(shellValue) {
    if (!shellValue) return null
    return shellValue.availability === "available" ? shellValue.value : null
  }
  function sessionLabel() {
    if (!root.session) return ""
    if (root.session.maintenance_break) return "Maintenance break"
    switch (String(root.session.phase || "")) {
      case "rth": return "RTH"
      case "overnight": return "Overnight"
      case "closed": return "Market closed"
      default: return String(root.session.phase || "")
    }
  }
  function ageText() {
    var age = root.ageSeconds
    if (age < 0) return "no data"
    if (age < 90) return "updated just now"
    return "updated " + Math.round(age / 60) + "m ago"
  }
  function lastResponseText() {
    if (root.ageSeconds < 0) return root.showSnapshot
      ? "Last response time unavailable" : "No successful response yet"
    if (root.ageSeconds < 60) return "Last response " + Math.round(root.ageSeconds) + "s ago"
    return "Last response " + Math.round(root.ageSeconds / 60) + "m ago"
  }
  function sessionText() {
    if (!root.session || String(root.session.phase || "") !== "closed") return ""
    // The core pre-renders the time with a short zone ("Mon 05:00 EDT", "UTC+7").
    var opens = String(root.session.next_open_local || "")
    return opens ? "Market closed · opens " + opens : "Market closed"
  }
  // Deep-link into the workstation at a section. The frontend reads ?section=.
  // No section opens the Today screen with the notes.
  function openOmarkets(section) {
    var url = "http://127.0.0.1:" + String(root.service ? root.service.apiPort : "8765") + "/"
    if (section) url += "?section=" + section
    if (root.hostWidget && typeof root.hostWidget.launch === "function") {
      root.hostWidget.launch(url)
    } else if (root.bar) {
      Quickshell.execDetached(["omarchy-launch-webapp", url])
    }
  }

  function reveal(item) {
    var y = item.mapToItem(col, 0, 0).y
    if (y < scroll.contentY) scroll.contentY = y
    else if (y + item.height > scroll.contentY + scroll.height)
      scroll.contentY = Math.min(Math.max(0, scroll.contentHeight - scroll.height), y + item.height - scroll.height)
  }

  function inject() {
    // hostWidget may arrive after the panel loads.
  }

  KeyboardPanel {
    id: panel
    anchorItem: root.anchorItem
    owner: root.hostWidget || root
    bar: root.bar
    open: root.opened
    centerOnBar: false
    objectName: "omarkets-keyboard-panel"
    focusTarget: diagnosticsButton
    contentWidth: panel.fittedContentWidth(460)
    contentHeight: panel.fittedContentHeight(col.implicitHeight + 16)

    Flickable {
      id: scroll
      objectName: "omarkets-panel-scroll"
      Keys.onEscapePressed: root.close()
      Keys.onDownPressed: contentY = Math.min(Math.max(0, contentHeight - height), contentY + 32)
      Keys.onUpPressed: contentY = Math.max(0, contentY - 32)
      Keys.onPressed: function(event) {
        if (event.key === Qt.Key_PageDown) { contentY = Math.min(Math.max(0, contentHeight - height), contentY + height); event.accepted = true }
        else if (event.key === Qt.Key_PageUp) { contentY = Math.max(0, contentY - height); event.accepted = true }
      }
      anchors.fill: parent
      contentWidth: width
      contentHeight: col.implicitHeight
      clip: true
      boundsBehavior: Flickable.StopAtBounds

      Column {
        id: col
        width: scroll.width
        spacing: 8

        // ---- header
        Row {
          width: parent.width
          spacing: 8
          Text {
            text: "OMARKETS"
            color: root.fg
            font.family: root.fontFamily
            font.pixelSize: Style.font.subtitle
            font.bold: true
          }
          Item { width: parent.width - 210; height: 1 }
          Text {
            text: root.online ? "CONNECTED" : (root.transportState === "unresponsive" ? "RETRYING" : "UNAVAILABLE")
            color: root.online ? root.upColor : (root.transportState === "unresponsive" ? root.warnColor : root.downColor)
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }
        }

        Text {
          visible: !root.online
          width: parent.width
          text: (root.transportState === "unresponsive"
            ? "Omarkets is temporarily unresponsive. Retrying · "
            : "Omarkets is unavailable. Retrying · ") + root.lastResponseText()
          color: root.transportState === "unresponsive" ? root.warnColor : root.downColor
          wrapMode: Text.WordWrap
          font.family: root.fontFamily
          font.pixelSize: Style.font.bodySmall
        }

        Text {
          visible: root.showSnapshot
          width: parent.width
          wrapMode: Text.Wrap
          text: root.online ? ("API connected · Data " + String(root.dataHealth).toUpperCase()
            + " · AI " + String(root.aiHealth).toUpperCase()) : "Last known snapshot"
          color: root.online ? root.healthColor(root.dataHealth) : root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
        }
        Text {
          visible: root.showSnapshot && root.sessionText() !== ""
          width: parent.width
          wrapMode: Text.Wrap
          text: root.sessionText()
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.bodySmall
        }

        // ---- latest desk note
        Column {
          objectName: "omarkets-desk-note"
          width: parent.width
          spacing: 3
          visible: root.showSnapshot && root.deskNote !== null
          Text {
            width: parent.width
            elide: Text.ElideRight
            text: root.deskNote ? ((root.deskKinds[root.deskNote.kind] || "NOTE") + " · " + root.deskTime(root.deskNote.created_at)) : ""
            color: root.accent
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }
          Text {
            width: parent.width
            text: root.deskNote ? root.deskNote.headline : ""
            textFormat: Text.PlainText
            wrapMode: Text.WordWrap
            color: root.fg
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
            font.bold: true
          }
          Text {
            width: parent.width
            text: root.deskNote ? root.deskNote.summary : ""
            textFormat: Text.PlainText
            wrapMode: Text.WordWrap
            color: root.fg
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
          }
          Text {
            width: parent.width
            objectName: "omarkets-desk-tone"
            text: root.deskToneText(root.deskNote)
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }
        }

        // ---- market
        Column {
          width: parent.width
          spacing: 2
          visible: root.showSnapshot && root.strip.length > 0
          Repeater {
            model: root.strip
            delegate: Row {
              required property var modelData
              width: col.width
              spacing: 8
              Text {
                width: 56
                text: String(modelData.symbol || "")
                color: root.fg
                font.family: root.fontFamily
                font.pixelSize: Style.font.body
                font.bold: true
              }
              Text {
                width: 80
                text: modelData.change_bp !== null && modelData.change_bp !== undefined
                  ? root.fmtBp(modelData.change_bp) : root.fmtPct(modelData.change_pct)
                color: root.signedColor(modelData.change_bp !== null && modelData.change_bp !== undefined
                  ? modelData.change_bp : modelData.change_pct)
                horizontalAlignment: Text.AlignRight
                font.family: root.fontFamily
                font.pixelSize: Style.font.body
              }
              Text {
                width: 70
                text: root.fmtValue(modelData.value, modelData.symbol === "US2Y" || modelData.symbol === "US10Y" ? 3 : 2)
                color: root.fg
                horizontalAlignment: Text.AlignRight
                font.family: root.fontFamily
                font.pixelSize: Style.font.body
              }
              Text {
                width: parent.width - 224
                elide: Text.ElideRight
                text: (modelData.market_closed ? "closed" : modelData.status === "stale" || modelData.status === "unavailable" ? modelData.status : String(modelData.feed_status || "unavailable")).toUpperCase()
                color: modelData.market_closed ? root.dim : root.feedColor(modelData.feed_status)
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
              }
            }
          }
        }

        // ---- risk tone (rule-based, computed by the core)
        Text {
          objectName: "omarkets-risk-tone"
          visible: root.showSnapshot && root.riskTone !== null
          width: parent.width
          text: root.riskTone ? String(root.riskTone.line || "") : ""
          textFormat: Text.PlainText
          wrapMode: Text.WordWrap
          color: root.toneColor(root.riskTone ? root.riskTone.tone : "")
          font.family: root.fontFamily
          font.pixelSize: Style.font.bodySmall
        }

        Rectangle { width: parent.width; height: 1; color: root.dim; opacity: 0.3; visible: root.showSnapshot }

        // ---- next event
        Text {
          visible: root.showSnapshot
          text: "NEXT EVENT"
          color: root.accent
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
        }
        Column {
          width: parent.width
          spacing: 2
          visible: root.showSnapshot && root.nextEvent !== null
          Text {
            text: root.nextEvent ? String(root.nextEvent.name || "") : ""
            color: root.importanceColor(root.nextEvent ? root.nextEvent.importance : "")
            wrapMode: Text.WordWrap
            width: parent.width
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
          }
          Text {
            width: parent.width
            wrapMode: Text.Wrap
            // time_et already includes the zone (e.g. "08:30 EDT").
            text: root.nextEvent
              ? ((root.nextEvent.time_et || "—") + (root.nextEvent.time_local ? " / " + root.nextEvent.time_local + " local" : ""))
              : ""
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
          }
          Text {
            width: parent.width
            wrapMode: Text.Wrap
            text: root.nextEvent ? ("in " + root.countdown(root.nextEvent.scheduled_at)) : ""
            color: root.fg
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
          }
        }
        Text {
          visible: root.showSnapshot && root.nextEvent === null
          width: parent.width
          wrapMode: Text.Wrap
          text: "No high-importance event scheduled."
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.bodySmall
        }

        Button {
          id: diagnosticsButton
          objectName: "omarkets-diagnostics-button"
          focusable: true
          onActiveFocusChanged: if (activeFocus) root.reveal(diagnosticsButton)
          text: root.diagnosticsOpen ? "Hide sources and diagnostics" : "Sources and diagnostics"
          onClicked: root.diagnosticsOpen = !root.diagnosticsOpen
        }
        // ---- diagnostics
        Text {
          visible: root.diagnosticsOpen && root.showSnapshot && root.providers.length > 0
          text: "HEALTH"
          color: root.accent
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
        }
        Column {
          width: parent.width
          spacing: 1
          visible: root.diagnosticsOpen && root.showSnapshot && root.providers.length > 0
          Repeater {
            model: root.providers
            delegate: Row {
              required property var modelData
              width: col.width
              spacing: 6
              Text {
                width: 72
                elide: Text.ElideRight
                text: String(modelData.name || "")
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.bodySmall
              }
              Text {
                width: 72
                elide: Text.ElideRight
                text: String(modelData.health || "unknown")
                color: root.healthColor(modelData.health)
                font.family: root.fontFamily
                font.pixelSize: Style.font.bodySmall
              }
              Text {
                width: parent.width - 156
                text: String(modelData.detail || "")
                color: root.dim
                elide: Text.ElideRight
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
              }
            }
          }
        }

        Rectangle { width: parent.width; height: 1; color: root.dim; opacity: 0.3 }

        // ---- actions
        Flow {
          width: parent.width
          spacing: 6
          Button {
            objectName: "omarkets-open-button"
            focusable: true
            onActiveFocusChanged: if (activeFocus) root.reveal(this)
            text: "Open Omarkets"
            onClicked: root.openOmarkets("")
          }
          Button {
            focusable: true
            onActiveFocusChanged: if (activeFocus) root.reveal(this)
            text: "Calendar"
            onClicked: root.openOmarkets("calendar")
          }
          Button {
            focusable: true
            onActiveFocusChanged: if (activeFocus) root.reveal(this)
            text: "Refresh"
            // Reloads state only; never triggers a model call.
            onClicked: if (root.service) root.service.refresh()
          }
        }

        Text {
          width: parent.width
          text: "Presentation only. Values, feed status and health come from the Omarkets core."
          color: root.dim
          wrapMode: Text.WordWrap
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
        }
      }
    }
  }
}
