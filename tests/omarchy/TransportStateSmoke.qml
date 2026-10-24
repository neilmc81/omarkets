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
      var original = service.snapshot
      var malformed = ["{", "[]", '{"status":{"data_health":123}}']
      for (var status of [{strip:[null]}, {strip:[7]}, {strip:[{symbol:"NQ",value:"wrong"}]}, {providers:[null]}, {providers:[{name:"api",detail:[]}]}, {primary:[]}, {session:5}, {next_event:{name:[]}}])
        malformed.push(JSON.stringify({status:status}))
      malformed.push(JSON.stringify({status:{},context:{cross_market:[null]}}))
      malformed.push(JSON.stringify({status:{},context:{cross_market:[{symbol:4}]}}))
      malformed.push(JSON.stringify({status:{},desk:{note:{headline:5}}}))
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
