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
  QtObject {
    id: fake
    property string apiPort: "8765"
    property string transportState: "connected"
    property var status: ({})
    property var crossMarket: []
    property var strip: []
    property var nextEvent: null
    property var deskNote: shell.withNote ? ({ id: "move:ES:20261014T13", kind: "move", title: "ES moved sharply", created_at: "2026-10-14T13:05:00Z", headline: "ES fell 1.2% in an hour", summary: "Sellers pushed the index lower after the open.", tone: "negative", tone_for: "ES" }) : null
    property var themeColors: ({ green: "#549e6a", red: "#ff5345", orange: "#a2734b", blue: "#509475" })
    property var providers: [{ name: "desk", health: "ok", detail: null }]
    property string dataHealth: "ok"
    property string aiHealth: "ok"
    property var session: null
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
      // State colours come from the active Omarchy palette, and follow it.
      ensure(Qt.colorEqual(popup.upColor, "#549e6a"), "up colour follows theme green " + popup.upColor)
      ensure(Qt.colorEqual(popup.downColor, "#ff5345"), "down colour follows theme red")
      ensure(Qt.colorEqual(popup.warnColor, "#a2734b"), "warn colour follows theme orange")
      ensure(Qt.colorEqual(popup.signedColor(1), "#549e6a"), "positive change uses theme green")
      fake.themeColors = { green: "#40a02b", red: "#d20f39", orange: "#fe640b", blue: "#1e66f5" }
      ensure(Qt.colorEqual(popup.upColor, "#40a02b"), "theme switch retints the popup")
      fake.themeColors = null
      ensure(Qt.colorEqual(popup.upColor, "#9ece6a"), "fallback when no palette is available")
      console.log("DESK_QML_OK")
      Qt.quit()
    }
  }
}
