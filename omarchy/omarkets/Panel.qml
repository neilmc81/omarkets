import QtQuick
import Quickshell
import qs.Commons
import qs.Ui

// Omarkets compact popup panel.
//
// Presentation only. It reads the OmarketsService the bar widget owns and renders
// the core's deterministic, already-computed state: one session status, the focus
// instrument as a hero (price, change, 24h sparkline, last week's range), the
// latest desk note headline, grouped context rows, the next economic event and a
// short footer. Colours mean "effect on NQ" and come only from the core's
// per-quote `nq_effect`; the panel never computes a value, never decides an
// effect and never triggers a model call.

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
  readonly property color accent: root.themeColor("accent", Color.accent)
  // Text on a filled pill: the theme background, so it reads on any palette.
  readonly property color onPill: root.themeColor("background", Color.background)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

  readonly property string transportState: root.service ? root.service.transportState : "unavailable"
  readonly property bool online: root.transportState === "connected"
  readonly property bool showSnapshot: !!(root.service && root.service.status)
  readonly property string focusSymbol: root.service && typeof root.service.focusSymbol === "string"
    && root.service.focusSymbol !== "" ? root.service.focusSymbol : "NQ"
  // Prefer the wider context strip (ES/US10Y/DXY/VXN as well) when the optional
  // context payload is present; fall back to the bar strip.
  readonly property var strip: (root.service && root.service.crossMarket && root.service.crossMarket.length > 0)
    ? root.service.crossMarket
    : (root.service && root.service.strip ? root.service.strip : [])
  // The hero is the focus's own quote; never another instrument's.
  readonly property var primary: {
    var own = root.service ? root.service.primary : null
    if (own && own.symbol === root.focusSymbol) return own
    for (var i = 0; i < root.strip.length; i++)
      if (root.strip[i] && root.strip[i].symbol === root.focusSymbol) return root.strip[i]
    return null
  }
  // Prior-week range / prior settle for the focus; only when it is the focus's.
  readonly property var hero: root.service && root.service.hero && root.service.hero.symbol === root.focusSymbol
    ? root.service.hero : null
  readonly property var sparklines: root.service && root.service.sparklines ? root.service.sparklines : ({})
  readonly property var nextEvent: root.service ? root.service.nextEvent : null
  readonly property var riskTone: root.service ? root.service.riskTone : null
  readonly property var session: root.service ? root.service.session : null
  readonly property string dataHealth: root.service ? String(root.service.dataHealth || "unknown") : "unknown"
  readonly property string aiHealth: root.service ? String(root.service.aiHealth || "unknown") : "unknown"
  readonly property double ageSeconds: root.service ? root.service.ageSeconds : -1

  // Latest plain-English desk note: the headline is the popup's one-liner.
  readonly property var deskNote: root.service && root.service.deskNote ? root.service.deskNote : null
  readonly property var deskTones: ({ supportive: "Supportive", negative: "Negative",
    mixed: "Mixed", neutral: "Neutral" })
  // "Negative for NQ": the tone word plus the instrument the note's tone is about.
  function deskToneText(note) {
    var word = note ? root.deskTones[note.tone] : ""
    if (!word) return ""
    return note.tone_for ? word + " for " + note.tone_for : word
  }
  function deskToneColor(tone) {
    switch (String(tone || "")) {
      case "supportive": return root.upColor
      case "negative": return root.downColor
      case "mixed": return root.warnColor
      default: return root.dim
    }
  }

  // Effect on NQ, exactly as the core reported it. Unknown/null has no colour.
  function effectColor(effect) {
    switch (String(effect || "")) {
      case "helps": return root.upColor
      case "hurts": return root.downColor
      case "neutral": return root.dim
      default: return root.dim
    }
  }
  function effectKnown(effect) {
    return effect === "helps" || effect === "hurts" || effect === "neutral"
  }
  // A shape as well as a colour, so the meaning survives colour blindness.
  function effectGlyph(effect) {
    switch (String(effect || "")) {
      case "helps": return "▲"
      case "hurts": return "▼"
      case "neutral": return "–"
      default: return ""
    }
  }
  function effectWords(effect) {
    switch (String(effect || "")) {
      case "helps": return "helps NQ"
      case "hurts": return "weighs on NQ"
      case "neutral": return "little effect on NQ"
      default: return "not rated for NQ"
    }
  }

  // Refresh the countdowns once a second while open. This is a display timer; it
  // does not poll the network (the service owns polling).
  property double nowMs: Date.now()
  Timer {
    interval: 1000
    running: root.opened === true
    repeat: true
    onTriggered: root.nowMs = Date.now()
  }

  function isYield(symbol) { return symbol === "US2Y" || symbol === "US10Y" }
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
  function quoteValue(q) {
    if (!q) return "—"
    return root.isYield(q.symbol) ? root.fmtValue(q.value, 3) + (q.value === null || q.value === undefined ? "" : "%")
      : root.fmtValue(q.value, 2)
  }
  // Yields move in basis points; everything else in percent.
  function quoteChange(q) {
    if (!q) return "—"
    return q.change_bp !== null && q.change_bp !== undefined ? root.fmtBp(q.change_bp) : root.fmtPct(q.change_pct)
  }
  // A live feed problem, not "the market is shut": shown by dimming the row.
  function quoteDegraded(q) {
    return !!q && !q.market_closed && (q.status === "stale" || q.status === "unavailable")
  }
  function sparkValues(symbol) {
    var line = root.sparklines ? root.sparklines[symbol] : null
    return line && Array.isArray(line.values) ? line.values : []
  }
  function rowLabel(q) {
    return String(q.short_label || q.label || "")
  }

  // Rows grouped as the core labels them (equities, rates, dollar/volatility),
  // in that order. Without `group` (an older core) the rows stay in one
  // untitled block in the core's order.
  readonly property var groupOrder: ["equities", "rates", "dollar_volatility"]
  readonly property var groupTitles: ({ equities: "EQUITIES", rates: "RATES",
    dollar_volatility: "DOLLAR · VOLATILITY" })
  readonly property var groups: {
    var byKey = {}
    var keys = []
    for (var i = 0; i < root.strip.length; i++) {
      var q = root.strip[i]
      if (!q || q.symbol === root.focusSymbol) continue
      var key = typeof q.group === "string" ? q.group : ""
      if (!byKey[key]) { byKey[key] = []; keys.push(key) }
      byKey[key].push(q)
    }
    keys.sort(function(a, b) {
      var ia = root.groupOrder.indexOf(a), ib = root.groupOrder.indexOf(b)
      return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib)
    })
    return keys.map(function(k) {
      return { key: k, title: k === "" ? "" : (root.groupTitles[k] || k.toUpperCase().replace(/_/g, " · ")), rows: byKey[k] }
    })
  }

  function countdown(iso) {
    if (!iso) return ""
    var when = Date.parse(String(iso)); if (isNaN(when)) return ""
    var delta = when - root.nowMs
    if (delta <= 0) return "now"
    var mins = Math.floor(delta / 60000)
    if (mins < 60) return Math.max(1, mins) + "m"
    var hours = Math.floor(mins / 60)
    if (hours < 48) return hours + "h " + (mins % 60) + "m"
    return Math.floor(hours / 24) + "d " + (hours % 24) + "h"
  }
  function localTime(iso, format) {
    var date = new Date(String(iso || ""))
    return isNaN(date.getTime()) ? "" : Qt.formatDateTime(date, format)
  }

  // One status line for the whole popup (no per-row CLOSED).
  readonly property color sessionColor: {
    if (root.transportState === "unresponsive") return root.warnColor
    if (!root.online) return root.downColor
    if (!root.session) return root.dim
    if (root.session.maintenance_break) return root.warnColor
    switch (String(root.session.phase || "")) {
      case "rth": return root.upColor
      case "overnight": return root.infoColor
      case "closed": return root.warnColor
      default: return root.dim
    }
  }
  function sessionText() {
    if (root.transportState === "unresponsive") return "Retrying"
    if (!root.online) return "Unavailable"
    if (!root.session) return ""
    if (root.session.maintenance_break) return "Maintenance break"
    switch (String(root.session.phase || "")) {
      case "rth": return "Regular session"
      case "overnight": return "Overnight session"
      case "closed": {
        var opens = root.countdown(root.session.next_open_at)
        return opens && opens !== "now" ? "Closed · opens in " + opens : "Closed"
      }
      default: return String(root.session.phase || "")
    }
  }
  function lastResponseText() {
    if (root.ageSeconds < 0) return root.showSnapshot
      ? "last response time unavailable" : "no successful response yet"
    if (root.ageSeconds < 60) return "last response " + Math.round(root.ageSeconds) + "s ago"
    return "last response " + Math.round(root.ageSeconds / 60) + "m ago"
  }
  // The core's own health badges, in words; never inferred.
  function healthText() {
    if (root.dataHealth === "ok" && root.aiHealth === "ok") return "all systems OK"
    var parts = []
    if (root.dataHealth !== "ok") parts.push("data " + root.dataHealth)
    if (root.aiHealth !== "ok") parts.push("AI " + root.aiHealth)
    return parts.join(" · ")
  }
  function footerText() {
    if (!root.online)
      return (root.showSnapshot ? "Last known snapshot · " : "") + root.lastResponseText()
    var asOf = root.primary ? root.localTime(root.primary.as_of, "ddd HH:mm") : ""
    return (asOf ? "Data as of " + asOf + " · " : "") + root.healthText()
  }
  readonly property color footerColor: !root.online ? (root.transportState === "unresponsive" ? root.warnColor : root.downColor)
    : (root.dataHealth === "ok" && root.aiHealth === "ok" ? root.dim
      : (root.dataHealth === "down" || root.aiHealth === "down" ? root.downColor : root.warnColor))

  function importanceDots(importance) {
    switch (String(importance || "")) {
      case "critical": return 3
      case "high": return 2
      case "medium": return 1
      default: return 0
    }
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

  // ---- reusable pieces

  // A line chart of core-provided values; null buckets are gaps, never zero.
  component Sparkline: Canvas {
    id: spark
    property var values: []
    property color lineColor: root.dim
    property real fillAlpha: 0.22
    antialiasing: true
    onValuesChanged: requestPaint()
    onLineColorChanged: requestPaint()
    onWidthChanged: requestPaint()
    onHeightChanged: requestPaint()
    onPaint: {
      var ctx = getContext("2d")
      ctx.reset()
      var vals = Array.isArray(spark.values) ? spark.values : []
      var lo = Infinity, hi = -Infinity, count = 0
      for (var i = 0; i < vals.length; i++) {
        var v = vals[i]
        if (typeof v !== "number" || !isFinite(v)) continue
        lo = Math.min(lo, v); hi = Math.max(hi, v); count++
      }
      if (count < 2 || spark.width <= 0 || spark.height <= 0) return
      var pad = 2
      var h = spark.height - pad * 2
      var step = spark.width / Math.max(1, vals.length - 1)
      var span = hi - lo
      function yAt(value) { return span > 0 ? pad + (1 - (value - lo) / span) * h : pad + h / 2 }
      var c = spark.lineColor
      var grad = ctx.createLinearGradient(0, 0, 0, spark.height)
      grad.addColorStop(0, Qt.rgba(c.r, c.g, c.b, spark.fillAlpha))
      grad.addColorStop(1, Qt.rgba(c.r, c.g, c.b, 0))
      // Each unbroken run is filled to the baseline, then stroked.
      var runs = []
      var run = []
      for (var j = 0; j < vals.length; j++) {
        var value = vals[j]
        if (typeof value === "number" && isFinite(value)) run.push([j * step, yAt(value)])
        else if (run.length) { runs.push(run); run = [] }
      }
      if (run.length) runs.push(run)
      ctx.lineWidth = 1.5
      ctx.lineJoin = "round"
      ctx.strokeStyle = c
      for (var r = 0; r < runs.length; r++) {
        var pts = runs[r]
        if (pts.length < 2) continue
        ctx.beginPath()
        ctx.moveTo(pts[0][0], spark.height)
        for (var k = 0; k < pts.length; k++) ctx.lineTo(pts[k][0], pts[k][1])
        ctx.lineTo(pts[pts.length - 1][0], spark.height)
        ctx.closePath()
        ctx.fillStyle = grad
        ctx.fill()
        ctx.beginPath()
        ctx.moveTo(pts[0][0], pts[0][1])
        for (var m = 1; m < pts.length; m++) ctx.lineTo(pts[m][0], pts[m][1])
        ctx.stroke()
      }
    }
  }

  // A change pill filled by the core's effect on NQ; outlined when unrated.
  component ChangePill: Rectangle {
    id: pill
    property string text: ""
    property var effect: null
    property real fontSize: Style.font.body
    readonly property bool rated: root.effectKnown(pill.effect)
    implicitWidth: pillText.implicitWidth + Style.spacing.lg * 2
    implicitHeight: pillText.implicitHeight + Style.spacing.xs * 2
    radius: Style.spacing.sm
    color: pill.rated ? root.effectColor(pill.effect) : "transparent"
    border.width: pill.rated ? 0 : 1
    border.color: root.dim
    Text {
      id: pillText
      anchors.centerIn: parent
      text: pill.text
      color: pill.rated ? root.onPill : root.fg
      font.family: root.fontFamily
      font.pixelSize: pill.fontSize
      font.bold: pill.rated
    }
  }

  component Divider: Rectangle {
    height: 1
    color: root.dim
    opacity: 0.35
  }

  KeyboardPanel {
    id: panel
    anchorItem: root.anchorItem
    owner: root.hostWidget || root
    bar: root.bar
    open: root.opened
    centerOnBar: false
    objectName: "omarkets-keyboard-panel"
    focusTarget: openButton
    contentWidth: panel.fittedContentWidth(Style.space(460))
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
        spacing: Style.spacing.xxl

        // ---- header: name + the one session status
        Item {
          width: parent.width
          implicitHeight: Math.max(title.implicitHeight, statusRow.implicitHeight)
          Text {
            id: title
            anchors.left: parent.left
            anchors.verticalCenter: parent.verticalCenter
            text: "OMARKETS"
            color: root.accent
            font.family: root.fontFamily
            font.pixelSize: Style.font.title
            font.bold: true
          }
          Row {
            id: statusRow
            objectName: "omarkets-session"
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            spacing: Style.spacing.md
            visible: statusText.text !== ""
            Rectangle {
              anchors.verticalCenter: parent.verticalCenter
              width: Style.spacing.lg; height: width; radius: width / 2
              color: root.sessionColor
            }
            Text {
              id: statusText
              text: root.sessionText()
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
            }
          }
        }

        Text {
          visible: !root.online
          width: parent.width
          text: (root.transportState === "unresponsive"
            ? "Omarkets is temporarily unresponsive. Retrying." : "Omarkets is unavailable. Retrying.")
          color: root.transportState === "unresponsive" ? root.warnColor : root.downColor
          wrapMode: Text.WordWrap
          font.family: root.fontFamily
          font.pixelSize: Style.font.bodySmall
        }

        Divider { width: parent.width; visible: root.showSnapshot }

        // ---- hero: the focus instrument
        Column {
          id: heroBlock
          objectName: "omarkets-hero"
          width: parent.width
          spacing: Style.spacing.md
          visible: root.showSnapshot && root.primary !== null

          Item {
            width: parent.width
            implicitHeight: Math.max(heroLeft.implicitHeight, heroRight.implicitHeight)
            Row {
              id: heroLeft
              anchors.left: parent.left
              anchors.top: parent.top
              anchors.right: heroRight.left
              anchors.rightMargin: Style.spacing.lg
              spacing: Style.spacing.xl
              Text {
                id: heroSymbol
                text: root.focusSymbol
                color: root.fg
                font.family: root.fontFamily
                font.pixelSize: Style.font.displayLarge
                font.bold: true
              }
              Text {
                anchors.baseline: heroSymbol.baseline
                width: heroLeft.width - heroSymbol.width - heroLeft.spacing
                elide: Text.ElideRight
                text: root.primary ? root.rowLabel(root.primary) : ""
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.bodySmall
              }
            }
            Column {
              id: heroRight
              anchors.right: parent.right
              anchors.top: parent.top
              spacing: Style.spacing.sm
              Text {
                anchors.right: parent.right
                text: root.quoteValue(root.primary)
                color: root.fg
                font.family: root.fontFamily
                font.pixelSize: Style.font.display
                font.bold: true
              }
              ChangePill {
                objectName: "omarkets-hero-pill"
                anchors.right: parent.right
                text: root.quoteChange(root.primary)
                effect: root.primary ? root.primary.nq_effect : null
                fontSize: Style.font.title
              }
            }
          }

          Sparkline {
            objectName: "omarkets-hero-spark"
            width: parent.width
            height: Style.space(56)
            visible: root.sparkValues(root.focusSymbol).length > 1
            values: root.sparkValues(root.focusSymbol)
            lineColor: root.primary && root.effectKnown(root.primary.nq_effect)
              ? root.effectColor(root.primary.nq_effect) : root.fg
          }

          Item {
            width: parent.width
            implicitHeight: Math.max(weekText.implicitHeight, settleText.implicitHeight)
            visible: weekText.text !== "" || settleText.text !== ""
            Text {
              id: weekText
              objectName: "omarkets-hero-week"
              anchors.left: parent.left
              text: root.hero && root.hero.prior_week_low_text && root.hero.prior_week_high_text
                ? "Last week " + root.hero.prior_week_low_text + " – " + root.hero.prior_week_high_text : ""
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }
            Text {
              id: settleText
              anchors.right: parent.right
              text: root.hero && root.hero.prior_settle_text ? "Prior settle " + root.hero.prior_settle_text : ""
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }
          }
        }

        // ---- the desk's one-line headline (or the core's risk-tone line)
        Column {
          objectName: "omarkets-desk-note"
          width: parent.width
          spacing: Style.spacing.md
          visible: root.showSnapshot && root.deskNote !== null
          Text {
            width: parent.width
            text: root.deskNote ? String(root.deskNote.headline || "") : ""
            textFormat: Text.PlainText
            wrapMode: Text.WordWrap
            maximumLineCount: 3
            elide: Text.ElideRight
            color: root.fg
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
          }
          Rectangle {
            objectName: "omarkets-desk-tone-pill"
            visible: toneText.text !== ""
            implicitWidth: toneText.implicitWidth + Style.spacing.lg * 2
            implicitHeight: toneText.implicitHeight + Style.spacing.xs * 2
            radius: Style.spacing.sm
            color: root.deskToneColor(root.deskNote ? root.deskNote.tone : "")
            Text {
              id: toneText
              objectName: "omarkets-desk-tone"
              anchors.centerIn: parent
              text: root.deskToneText(root.deskNote)
              color: root.onPill
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
              font.bold: true
            }
          }
        }
        Text {
          objectName: "omarkets-risk-tone"
          visible: root.showSnapshot && root.deskNote === null && root.riskTone !== null
          width: parent.width
          text: root.riskTone ? String(root.riskTone.line || "") : ""
          textFormat: Text.PlainText
          wrapMode: Text.WordWrap
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.bodySmall
        }

        // ---- grouped context rows
        Repeater {
          model: root.showSnapshot ? root.groups : []
          delegate: Column {
            id: groupBlock
            required property var modelData
            objectName: "omarkets-group-" + (modelData.key || "rows")
            width: col.width
            spacing: Style.spacing.lg
            Divider { width: parent.width }
            Text {
              visible: groupBlock.modelData.title !== ""
              text: groupBlock.modelData.title
              color: root.accent
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
              font.bold: true
            }
            Repeater {
              model: groupBlock.modelData.rows
              delegate: Item {
                id: quoteRow
                required property var modelData
                objectName: "omarkets-row-" + String(modelData.symbol || "")
                readonly property var effect: modelData.nq_effect
                readonly property int sparkW: Style.space(96)
                readonly property int valueW: Style.space(72)
                readonly property int pillW: Style.space(66)
                readonly property int glyphW: Style.space(12)
                readonly property int gap: Style.spacing.lg
                width: col.width
                implicitHeight: Math.max(nameCol.implicitHeight, Style.space(32))
                opacity: root.quoteDegraded(modelData) ? 0.55 : 1

                Column {
                  id: nameCol
                  anchors.left: parent.left
                  anchors.verticalCenter: parent.verticalCenter
                  width: parent.width - quoteRow.sparkW - quoteRow.valueW - quoteRow.pillW - quoteRow.glyphW - quoteRow.gap * 4
                  spacing: 0
                  Text {
                    width: parent.width
                    elide: Text.ElideRight
                    text: String(quoteRow.modelData.symbol || "")
                    color: root.fg
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.title
                    font.bold: true
                  }
                  Text {
                    width: parent.width
                    elide: Text.ElideRight
                    text: root.rowLabel(quoteRow.modelData)
                    color: root.dim
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                  }
                }
                Sparkline {
                  objectName: "omarkets-spark-" + String(quoteRow.modelData.symbol || "")
                  anchors.left: nameCol.right
                  anchors.leftMargin: quoteRow.gap
                  anchors.verticalCenter: parent.verticalCenter
                  width: quoteRow.sparkW
                  height: Math.max(Style.space(26), nameCol.implicitHeight - Style.spacing.sm)
                  values: root.sparkValues(quoteRow.modelData.symbol)
                  lineColor: root.effectKnown(quoteRow.effect) ? root.effectColor(quoteRow.effect) : root.dim
                }
                Text {
                  id: valueText
                  anchors.right: pillBox.left
                  anchors.rightMargin: quoteRow.gap
                  anchors.verticalCenter: parent.verticalCenter
                  width: quoteRow.valueW
                  horizontalAlignment: Text.AlignRight
                  text: root.quoteValue(quoteRow.modelData)
                  color: root.fg
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.body
                }
                Item {
                  id: pillBox
                  anchors.right: glyph.left
                  anchors.rightMargin: quoteRow.gap
                  anchors.verticalCenter: parent.verticalCenter
                  width: quoteRow.pillW
                  height: pill.implicitHeight
                  ChangePill {
                    id: pill
                    objectName: "omarkets-pill-" + String(quoteRow.modelData.symbol || "")
                    anchors.right: parent.right
                    width: Math.max(implicitWidth, parent.width)
                    text: root.quoteChange(quoteRow.modelData)
                    effect: quoteRow.effect
                    fontSize: Style.font.bodySmall
                  }
                }
                Text {
                  id: glyph
                  anchors.right: parent.right
                  anchors.verticalCenter: parent.verticalCenter
                  width: quoteRow.glyphW
                  horizontalAlignment: Text.AlignHCenter
                  text: root.effectGlyph(quoteRow.effect)
                  color: root.effectColor(quoteRow.effect)
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.body
                }
                MouseArea {
                  id: rowHover
                  anchors.fill: parent
                  hoverEnabled: true
                  acceptedButtons: Qt.NoButton
                }
                PanelToolTip {
                  visible: rowHover.containsMouse
                  text: root.rowLabel(quoteRow.modelData) + ": " + root.quoteChange(quoteRow.modelData)
                    + " · " + root.effectWords(quoteRow.effect)
                }
              }
            }
          }
        }

        Divider { width: parent.width; visible: root.showSnapshot }

        // ---- next event, one line
        Item {
          objectName: "omarkets-next-event"
          width: parent.width
          visible: root.showSnapshot
          implicitHeight: Math.max(nextLabel.implicitHeight, Style.space(18))
          Text {
            id: nextLabel
            anchors.left: parent.left
            anchors.verticalCenter: parent.verticalCenter
            text: "NEXT"
            color: root.accent
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
            font.bold: true
          }
          Text {
            id: eventWhen
            anchors.left: nextLabel.right
            anchors.leftMargin: Style.spacing.xxl
            anchors.verticalCenter: parent.verticalCenter
            visible: root.nextEvent !== null
            text: root.nextEvent ? root.localTime(root.nextEvent.scheduled_at, "ddd HH:mm") : ""
            color: root.fg
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
          }
          Text {
            id: eventName
            anchors.left: root.nextEvent ? eventWhen.right : nextLabel.right
            anchors.leftMargin: Style.spacing.xxl
            anchors.right: root.nextEvent ? eventDots.left : parent.right
            anchors.rightMargin: Style.spacing.lg
            anchors.verticalCenter: parent.verticalCenter
            elide: Text.ElideRight
            text: root.nextEvent ? String(root.nextEvent.name || "") : "No high-importance event scheduled."
            color: root.nextEvent ? root.fg : root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
          }
          Row {
            id: eventDots
            anchors.right: eventIn.left
            anchors.rightMargin: Style.spacing.xl
            anchors.verticalCenter: parent.verticalCenter
            spacing: Style.spacing.sm
            visible: root.nextEvent !== null
            Repeater {
              model: 3
              delegate: Rectangle {
                required property int index
                readonly property bool lit: index < root.importanceDots(root.nextEvent ? root.nextEvent.importance : "")
                width: Style.spacing.md + 1; height: width; radius: width / 2
                color: lit ? root.warnColor : "transparent"
                border.width: lit ? 0 : 1
                border.color: root.dim
              }
            }
          }
          Text {
            id: eventIn
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            visible: root.nextEvent !== null
            text: root.nextEvent ? "in " + root.countdown(root.nextEvent.scheduled_at) : ""
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
          }
        }

        Divider { width: parent.width }

        // ---- footer: actions + one quiet status line
        Item {
          width: parent.width
          implicitHeight: actions.implicitHeight
          Row {
            id: actions
            anchors.left: parent.left
            spacing: Style.spacing.md
            Button {
              id: openButton
              objectName: "omarkets-open-button"
              focusable: true
              onActiveFocusChanged: if (activeFocus) root.reveal(openButton)
              text: "Open Omarkets"
              onClicked: root.openOmarkets("")
            }
            Button {
              id: calendarButton
              objectName: "omarkets-calendar-button"
              focusable: true
              onActiveFocusChanged: if (activeFocus) root.reveal(calendarButton)
              text: "Calendar"
              onClicked: root.openOmarkets("calendar")
            }
            Button {
              id: refreshButton
              objectName: "omarkets-refresh-button"
              focusable: true
              onActiveFocusChanged: if (activeFocus) root.reveal(refreshButton)
              iconText: "󰑐"
              tooltipText: "Refresh now (reloads data; never runs the AI)"
              // Reloads state only; never triggers a model call.
              onClicked: if (root.service) root.service.refresh()
            }
          }
        }

        Text {
          objectName: "omarkets-footer-status"
          width: parent.width
          elide: Text.ElideRight
          text: root.footerText()
          color: root.footerColor
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
        }
      }
    }
  }
}
