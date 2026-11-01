import QtQuick
import Quickshell
import QtTest
import "Omarkets" as Omarkets
ShellRoot {
  id: shell
  property string launched: ""
  readonly property bool withNote: Quickshell.env("OMARKETS_DESK_NOTE") === "1"
  QtObject {
    id: host
    function launch(url) { shell.launched = url }
    function close() { popup.close() }
  }
  // A rich W1 payload: grouped rows with nq_effect, sparklines and the hero.
  function quote(symbol, value, pct, bp, group, effect, label) {
    return { symbol: symbol, label: label + " (long)", short_label: label, group: group, nq_effect: effect,
      value: value, change_pct: pct, change_bp: bp, feed_status: "delayed", status: "stale",
      market_closed: true, as_of: "2026-10-30T20:49:57Z" }
  }
  function richRows() {
    return [quote("ES", 7615.5, -1.79, null, "equities", "hurts", "S&P 500 futures"),
      quote("NQ", 28542, -3.45, null, "equities", "hurts", "Nasdaq 100 futures"),
      quote("US2Y", 4.592, -1.37, -6.4, "rates", "helps", "2-year Treasury yield"),
      quote("US10Y", 4.931, -1.16, -5.8, "rates", "helps", "10-year Treasury yield"),
      quote("DXY", 101.96, -0.07, null, "dollar_volatility", "neutral", "US dollar index"),
      quote("VIX", 19.42, 12.91, null, "dollar_volatility", "hurts", "S&P volatility index"),
      quote("VXN", 28.5, 13.19, null, "dollar_volatility", null, "Nasdaq volatility index")]
  }
  function line(n, slope) {
    var values = []
    for (var i = 0; i < n; i++) values.push(i === 5 ? null : 100 + slope * i + Math.sin(i / 3))
    return { start: "2026-10-29T20:00:00Z", end: "2026-10-30T20:45:00Z", values: values }
  }
  QtObject {
    id: fake
    property string apiPort: "8765"
    property string transportState: "connected"
    property string focusSymbol: "NQ"
    property var status: ({ version: 2 })
    property var crossMarket: shell.richRows()
    property var strip: []
    property var primary: shell.richRows()[1]
    property var nextEvent: ({ name: "JOLTS job openings", importance: "high", time_et: "10:00 EST",
      time_local: "22:00", scheduled_at: new Date(Date.now() + 2 * 86400000 + 9 * 3600000).toISOString() })
    property var riskTone: ({ tone: "closed", label: "markets closed", line: "Risk tone: markets closed" })
    property var deskNote: shell.withNote ? ({ id: "move:ES:20261014T13", kind: "move", title: "ES moved sharply", created_at: "2026-10-14T13:05:00Z", headline: "ES fell 1.2% in an hour", summary: "Sellers pushed the index lower after the open.", tone: "negative", tone_for: "ES" }) : null
    property var themeColors: ({ green: "#549e6a", red: "#ff5345", orange: "#a2734b", blue: "#509475", background: "#111c18" })
    property var sparklines: ({ NQ: shell.line(96, -0.4), ES: shell.line(96, -0.2), US2Y: shell.line(96, -0.1),
      US10Y: shell.line(96, -0.1), DXY: shell.line(96, 0), VIX: shell.line(96, 0.3), VXN: shell.line(96, 0.3) })
    property var hero: ({ symbol: "NQ", prior_week_high: 29880, prior_week_low: 28410,
      prior_week_high_text: "29,880.00", prior_week_low_text: "28,410.00", prior_settle: 29562,
      prior_settle_text: "29,562.00", delayed: true })
    property var providers: [{ name: "desk", health: "ok", detail: null }]
    property string dataHealth: "ok"
    property string aiHealth: "ok"
    property var session: ({ phase: "closed", maintenance_break: false,
      next_open_at: new Date(Date.now() + 7 * 3600000 + 10 * 60000).toISOString(), next_open_local: "Mon 06:00 UTC+7" })
    property real ageSeconds: 1
    function refresh() {}
  }
  Omarkets.Panel {
    id: popup
    service: fake
    hostWidget: host
  }
  Timer { interval: 500; running: true; onTriggered: { try { test.test_desk() } catch (error) { console.error("DESK_QML_ERROR " + error); Qt.exit(1) } } }
  TestCase {
    id: test
    name: "OmarketsDeskPanel"
    when: false
    optional: true
    function ensure(value, message) { if (!value) throw new Error(message || "assertion failed") }
    function walk(object, name) {
      if (object.objectName === name) return object
      var children = object.data || object.children || []
      for (var i=0; i<children.length; i++) { var found=walk(children[i],name); if (found) return found }
      return null
    }
    function texts(object) {
      var out = typeof object.text === "string" && object.visible !== false ? [object.text] : []
      var children = object.data || object.children || []
      for (var i=0; i<children.length; i++) out = out.concat(texts(children[i]))
      return out
    }
    function test_desk() {
      popup.open()
      wait(400)
      var note = walk(popup, "omarkets-desk-note")
      ensure(note !== null && note.visible === shell.withNote, "desk note shown only when present")
      if (shell.withNote) {
        var tone = walk(popup, "omarkets-desk-tone")
        ensure(tone !== null && tone.text === "Negative for ES", "tone names the note's instrument: " + (tone && tone.text))
        ensure(popup.deskToneText({ tone: "supportive", tone_for: null }) === "Supportive", "no instrument named when unknown")
      }
      // "Open Omarkets" opens the Today screen (no section).
      walk(popup, "omarkets-open-button").clicked()
      ensure(shell.launched === "http://127.0.0.1:8765/", "opens Today: " + shell.launched)
      // One session status in the header; no per-row CLOSED clutter.
      var session = walk(popup, "omarkets-session")
      ensure(session !== null && session.visible, "session status shown")
      ensure(popup.sessionText().indexOf("Closed · opens in 7h") === 0, "one closed line: " + popup.sessionText())
      ensure(texts(popup).every(function(t) { return t.indexOf("CLOSED") < 0 && t !== "closed" }), "no per-row CLOSED")
      ensure(walk(popup, "omarkets-diagnostics-button") === null, "no diagnostics chrome")
      // The hero is the focus, with its week range and a sparkline.
      ensure(walk(popup, "omarkets-hero").visible, "hero shown")
      ensure(walk(popup, "omarkets-hero-week").text === "Last week 28,410.00 – 29,880.00", "hero week range")
      ensure(walk(popup, "omarkets-hero-spark").visible, "hero sparkline shown")
      ensure(Qt.colorEqual(walk(popup, "omarkets-hero-pill").color, "#ff5345"), "hero pill hurts NQ")
      // Rows are grouped by the core's group, without the focus.
      for (var g of ["equities", "rates", "dollar_volatility"])
        ensure(walk(popup, "omarkets-group-" + g) !== null, "group " + g)
      ensure(walk(popup, "omarkets-row-NQ") === null, "focus is the hero, not a row")
      ensure(walk(popup, "omarkets-row-ES") !== null, "ES row shown")
      // Pill colours follow the core's nq_effect, never the sign of the move.
      ensure(Qt.colorEqual(walk(popup, "omarkets-pill-US2Y").color, "#549e6a"), "falling yield helps NQ: green")
      ensure(Qt.colorEqual(walk(popup, "omarkets-pill-VIX").color, "#ff5345"), "rising VIX hurts NQ: red")
      ensure(Qt.colorEqual(walk(popup, "omarkets-pill-DXY").color, popup.dim), "neutral is dim")
      ensure(walk(popup, "omarkets-pill-VXN").color.a === 0, "no effect from the core: no fill")
      ensure(walk(popup, "omarkets-pill-US2Y").text === "-6.4bp", "yields in bp")
      ensure(walk(popup, "omarkets-footer-status").text.indexOf("all systems OK") > 0, "footer health")
      // State colours come from the active Omarchy palette, and follow it.
      ensure(Qt.colorEqual(popup.upColor, "#549e6a"), "up colour follows theme green " + popup.upColor)
      ensure(Qt.colorEqual(popup.downColor, "#ff5345"), "down colour follows theme red")
      ensure(Qt.colorEqual(popup.warnColor, "#a2734b"), "warn colour follows theme orange")
      ensure(Qt.colorEqual(popup.effectColor("helps"), "#549e6a"), "helps uses theme green")
      fake.themeColors = { green: "#40a02b", red: "#d20f39", orange: "#fe640b", blue: "#1e66f5" }
      ensure(Qt.colorEqual(popup.upColor, "#40a02b"), "theme switch retints the popup")
      ensure(Qt.colorEqual(walk(popup, "omarkets-pill-US2Y").color, "#40a02b"), "theme switch retints pills")
      fake.themeColors = null
      ensure(Qt.colorEqual(popup.upColor, "#9ece6a"), "fallback when no palette is available")
      // An older core/collector (before W1 PR1): no group, effect, sparklines or hero.
      fake.crossMarket = shell.richRows().map(function(q) {
        return { symbol: q.symbol, label: q.label, value: q.value, change_pct: q.change_pct, change_bp: q.change_bp,
          feed_status: q.feed_status, status: q.status, market_closed: true, as_of: q.as_of } })
      fake.primary = fake.crossMarket[1]
      fake.sparklines = ({})
      fake.hero = null
      wait(100)
      ensure(walk(popup, "omarkets-group-rows") !== null, "ungrouped rows still render")
      ensure(walk(popup, "omarkets-row-US2Y") !== null, "rows render without W1 fields")
      ensure(walk(popup, "omarkets-pill-US2Y").color.a === 0, "no effect: no colour invented")
      ensure(walk(popup, "omarkets-hero").visible && !walk(popup, "omarkets-hero-spark").visible, "hero without sparkline")
      ensure(walk(popup, "omarkets-hero-week").text === "", "no week range without hero levels")
      // Transport loss: one status, the footer explains.
      fake.transportState = "unavailable"
      ensure(popup.sessionText() === "Unavailable", "header says unavailable")
      ensure(walk(popup, "omarkets-footer-status").text.indexOf("last response") >= 0, "footer gives the age")
      console.log("DESK_QML_OK")
      Qt.quit()
    }
  }
}
