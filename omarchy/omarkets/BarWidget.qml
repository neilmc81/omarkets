import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Commons as Commons
import qs.Ui

// Omarkets bar widget.
//
// A compact launcher only: it shows the Omarkets finance icon and opens the popup,
// which renders the full cross-asset strip, next event and diagnostics sourced
// from the Omarkets core via OmarketsService (one shared poller). It holds no
// market logic and never shows stale/delayed data as live.
//
// Layout:  nf-md-finance icon (U+F081F), plus a marker when degraded
// The bar carries no market numbers; click for the panel. An offline/unreachable
// transport is signalled with an explicit marker, not colour alone.

BarWidget {
  id: root
  moduleName: "omarkets"

  readonly property string home: Quickshell.env("HOME") || ""
  // Honor XDG_CONFIG_HOME so the collector is found on non-default setups.
  readonly property string configHome: Quickshell.env("XDG_CONFIG_HOME") || home + "/.config"
  readonly property string apiPort: String(root.setting("apiPort", 8765))
  readonly property string updaterPath: configHome + "/omarchy/plugins/omarkets/update.py"

  OmarketsService {
    id: service
    bar: root.bar
    updater: root.updaterPath
    apiPort: root.apiPort
  }

  // Omarchy's theme switch retints Color.* through shell IPC. Collect again
  // straight away so the palette roles the popup uses follow it, not 30s later.
  Timer {
    id: themeRefresh
    interval: 400
    onTriggered: service.refresh()
  }
  Connections {
    target: Color
    function onBackgroundChanged() { themeRefresh.restart() }
    function onAccentChanged() { themeRefresh.restart() }
    function onForegroundChanged() { themeRefresh.restart() }
  }

  // Shape contract for the bar popup routing (see clock BarWidget).
  readonly property bool opened: panelLoader.item ? panelLoader.item.opened === true : false
  readonly property bool popoutSwitchClosing: panelLoader.item ? panelLoader.item.popoutSwitchClosing === true : false

  function open() { if (panelLoader.item) panelLoader.item.open() }
  function close() { if (panelLoader.item) panelLoader.item.close() }
  function closeForPopoutSwitch() { if (panelLoader.item) panelLoader.item.closeForPopoutSwitch() }
  function togglePanel() { if (panelLoader.item) panelLoader.item.toggle() }
  function refresh() { service.refresh() }

  // --- formatting (display only; no market math) ---------------------------

  function fmtValue(v, digits) {
    if (v === null || v === undefined) return "—"
    var n = Number(v)
    if (isNaN(n)) return "—"
    return n.toLocaleString(Qt.locale("en_US"), "f", digits === undefined ? 2 : digits)
  }

  function fmtChangePct(v) {
    if (v === null || v === undefined) return "—"
    var n = Number(v)
    if (isNaN(n)) return "—"
    return (n >= 0 ? "+" : "") + n.toFixed(2) + "%"
  }

  // Compact countdown; the core supplies the authoritative UTC instant.
  // Hours far out, minutes near, exact time inside five minutes.
  function fmtCountdown(iso) {
    if (!iso) return ""
    var when = Date.parse(String(iso))
    if (isNaN(when)) return ""
    var delta = when - Date.now()
    if (delta <= 0) return "now"
    var mins = Math.floor(delta / 60000)
    if (mins < 5) {
      return new Date(when).toLocaleTimeString(Qt.locale("en_US"),
        { hour: "2-digit", minute: "2-digit", hour12: false })
    }
    if (mins < 60) return mins + "m"
    var hours = Math.floor(mins / 60)
    if (hours < 48) return hours + "h"
    return Math.floor(hours / 24) + "d"
  }

  function eventShortName(event) {
    if (!event) return ""
    var code = event.event_code ? String(event.event_code) : ""
    // Prefer a short recognizable code (CPI, NFP, FOMC); else the first token.
    var known = ["CPI", "PCE", "NFP", "FOMC", "GDP", "PPI", "ISM", "JOLTS"]
    if (known.indexOf(code) !== -1) return code
    var name = String(event.name || "")
    return name.split(" ")[0] || code
  }

  // --- bar content ---------------------------------------------------------

  // Transport is unusable when the collector cannot reach the backend OR when
  // the file has not been refreshed for a while. Either way the bar must not
  // present last-known values as current.
  readonly property bool transportDown: service.transportState === "unavailable"
  readonly property bool transportRetrying: service.transportState === "unresponsive"

  // The bar is a launcher: the label opens the popup, where the full strip,
  // next event and diagnostics are rendered. Market numbers no longer clutter
  // the bar. An unreachable/stale backend is still flagged explicitly.
  // nf-md-finance (U+F081F) in the bar's Nerd Font stands in for the label.
  readonly property string barIcon: String.fromCodePoint(0xF081F)
  readonly property string barText: root.transportDown ? root.barIcon + " \u26a0"
    : (root.transportRetrying ? root.barIcon + " \u25d0" : root.barIcon)

  // Colour is a hint only; the text/marker carries the meaning. An unreachable
  // backend is red so it is never mistaken for a healthy launcher.
  readonly property var themeColors: service.themeColors
  function themeColor(role, fallback) {
    var value = root.themeColors ? root.themeColors[role] : null
    return (typeof value === "string" && /^#[0-9a-fA-F]{6}$/.test(value)) ? value : fallback
  }
  readonly property color barColor: root.transportDown ? root.themeColor("red", Commons.Color.urgent)
    : (root.transportRetrying ? root.themeColor("orange", "#e0af68") : (bar ? bar.foreground : Commons.Color.foreground))

  readonly property string tooltip: {
    if (root.transportDown)
      return "Omarkets unavailable · last response " + (service.ageSeconds >= 0
        ? Math.round(service.ageSeconds / 60) + "m ago" : "unknown") + "\nRetrying"
    if (root.transportRetrying)
      return "Omarkets temporarily unresponsive · retrying\nLast response "
        + (service.ageSeconds >= 0 ? Math.round(service.ageSeconds) + "s ago" : "unknown")
    // The core picks the primary (the focus instrument); the bar only names it.
    var nq = service.primary
    if (!nq) return "Omarkets — " + service.focusSymbol + " unavailable\nClick for market context"
    var line = nq.symbol + " " + root.fmtValue(nq.value) + " " + root.fmtChangePct(nq.change_pct)
      + " · feed " + (nq.market_closed ? "closed" : nq.status === "stale" || nq.status === "unavailable" ? nq.status : String(nq.feed_status || "unavailable")).toUpperCase()
    if (service.nextEvent) {
      line += "\n" + root.eventShortName(service.nextEvent) + " "
        + (service.nextEvent.time_et || "") + " · in " + root.fmtCountdown(service.nextEvent.scheduled_at)
    }
    line += "\nClick for market context"
    return line
  }

  function injectPanel() {
    var target = panelLoader.item
    if (!target) return
    if ("bar" in target) target.bar = root.bar
    if ("settings" in target) target.settings = root.settings
    if ("anchorItem" in target) target.anchorItem = button
    if ("hostWidget" in target) target.hostWidget = root
    if ("service" in target) target.service = service
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  onBarChanged: injectPanel()
  onSettingsChanged: injectPanel()

  Loader {
    id: panelLoader
    active: true
    source: Qt.resolvedUrl("Panel.qml")
    visible: false
    onLoaded: {
      root.injectPanel()
      Qt.callLater(root.injectPanel)
    }
  }

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.barText
    fontSize: Style.bar.iconFont
    // Colour is a hint only; the text/markers carry the meaning.
    foreground: root.barColor
    tooltipText: root.tooltip
    onPressed: function(b) {
      if (b === Qt.LeftButton) {
        root.togglePanel()
      } else if (b === Qt.MiddleButton) {
        root.refresh()
      }
    }
  }
}
