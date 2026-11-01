import QtQuick
import Quickshell

ShellRoot {
  Loader {
    id: loader
    source: "file://" + Quickshell.env("OMARKETS_ROOT")
      + "/omarchy/omarkets/OmarketsService.qml"
  }

  Timer {
    interval: 100
    running: true
    onTriggered: {
      var service = loader.item
      if (!service) {
        Qt.exit(40)
        return
      }
      if (typeof service.technical !== "undefined" || typeof service.latestAnalysis !== "undefined"
          || typeof service.latestMaterial !== "undefined") {
        Qt.exit(45)
        return
      }
      var good = { updatedAt: new Date().toISOString(), consecutiveFailures: 0,
        status: { version: 2 } }
      service.parse(JSON.stringify(good))
      service.nowMs = service.dataUpdatedAtMs - 1000
      if (service.ageSeconds !== 0 || service.transportState !== "connected") {
        Qt.exit(41)
        return
      }
      good.consecutiveFailures = 1
      service.parse(JSON.stringify(good))
      if (service.transportState !== "unresponsive") {
        Qt.exit(42)
        return
      }
      good.consecutiveFailures = 3
      service.parse(JSON.stringify(good))
      if (service.transportState !== "unavailable") {
        Qt.exit(43)
        return
      }
      good.consecutiveFailures = 0
      service.parse(JSON.stringify(good))
      if (service.transportState !== "connected") { Qt.exit(44); return }
      // W1 additive fields: quote effect/labels, sparklines and the hero.
      var rich = JSON.parse(JSON.stringify(good))
      rich.status.strip = [{symbol: "NQ", short_label: "Nasdaq 100 futures", group: "equities", nq_effect: "hurts"}]
      rich.context = {cross_market: [{symbol: "US2Y", short_label: "2-year Treasury yield", group: "rates", nq_effect: null}]}
      rich.sparklines = {interval: "15m", generated_at: null, lines: {NQ: {start: "2026-10-30T13:30:00Z", end: null, values: [1.5, null, 2]}}}
      rich.hero = {symbol: "NQ", prior_week_high: 29880, prior_week_low: null, prior_week_high_text: "29,880.00", delayed: true}
      service.parse(JSON.stringify(rich))
      if (service.snapshot.hero.symbol !== "NQ" || service.sparklines.NQ.values.length !== 3
          || service.hero.prior_week_high !== 29880 || service.strip[0].nq_effect !== "hurts") { Qt.exit(49); return }
      service.parse(JSON.stringify(good))
      if (service.hero !== null || Object.keys(service.sparklines).length !== 0) { Qt.exit(50); return }
      var original = service.snapshot
      var malformed = ["{", "[]", '{"status":{"data_health":123}}']
      for (var status of [{strip:[null]}, {strip:[7]}, {strip:[{symbol:"NQ",value:"wrong"}]}, {providers:[null]}, {providers:[{name:"api",detail:[]}]}, {primary:[]}, {session:5}, {next_event:{name:[]}}])
        malformed.push(JSON.stringify({status:status}))
      malformed.push(JSON.stringify({status:{},context:{cross_market:[null]}}))
      malformed.push(JSON.stringify({status:{},context:{cross_market:[{symbol:4}]}}))
      malformed.push(JSON.stringify({status:{},desk:{note:{headline:5}}}))
      malformed.push(JSON.stringify({status:{strip:[{symbol:"NQ",nq_effect:1}]}}))
      malformed.push(JSON.stringify({status:{},sparklines:{lines:[]}}))
      malformed.push(JSON.stringify({status:{},sparklines:{lines:{NQ:{values:["x"]}}}}))
      malformed.push(JSON.stringify({status:{},hero:{symbol:"NQ",prior_week_high:"29,880"}}))
      malformed.push(JSON.stringify({status:{},hero:{prior_week_high:1}}))
      for (var bad of malformed) {
        service.parse(bad)
        if (service.snapshot !== original) { Qt.exit(46); return }
      }
      if (service.transportState !== "unavailable") { Qt.exit(47); return }
      service.parse(JSON.stringify(good))
      Qt.exit(service.transportState === "connected" ? 0 : 48)
    }
  }
}
