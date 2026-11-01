import QtQuick
import Quickshell
import Quickshell.Io

// Shared shell-side connection to Omarkets.
//
// One instance is created by the bar widget and handed to the panel, so there is
// exactly one updater process, one FileView and one poll timer per bar — never a
// client per component (spec 23).
//
  // Transport: the installed Quickshell (4.0.4) Io module has no WebSocket type, so
  // this polls a compact, versioned shell endpoint through the stdlib updater,
  // which writes one JSON file this service watches. That is the platform-supported
  // path and the same pattern the sibling cost plugins use.
  //
  // The collector retains the last good snapshot and reports failed attempts.
  // Failed reads use a short retry interval so a recovered API appears promptly.
//
// Failure isolation: every parse is guarded. Malformed JSON, a missing file, an
// unreachable backend or unknown fields all resolve to sane defaults; nothing
// here can throw into the shell.

Item {
  id: root

  // Injected by the bar widget.
  property QtObject bar: null
  // Absolute path to update.py.
  property string updater: ""
  // Omarkets API port.
  property string apiPort: "8765"

  readonly property string home: Quickshell.env("HOME") || ""
  readonly property string stateHome: Quickshell.env("XDG_STATE_HOME") || home + "/.local/state"
  readonly property string dataPath: stateHome + "/omarkets/bar.json"

  // Raw parse of the collector file; null means "no usable data yet".
  property var snapshot: null
  // True once the collector has run at least once this session.
  property bool started: false
  // Consecutive failed collector attempts.
  property int failures: 0
  // When refresh() was last issued, to detect a refresh that produced nothing.
  property double lastRefreshMs: 0
  // Wall-clock (ms) of the last successful parse, for age display.
  property double parsedAtMs: 0
  // The collector's own write time, parsed from the payload. This is the true
  // "how fresh is this file" signal: if the collector cannot reach the backend
  // it leaves an old file, and only this field reveals that.
  property double dataUpdatedAtMs: 0

  readonly property var status: root.snapshot && root.snapshot.status ? root.snapshot.status : null
  // Optional popup payload (wider cross-asset strip). Null when unavailable.
  readonly property var context: root.snapshot && root.snapshot.context ? root.snapshot.context : null
  readonly property var crossMarket: root.context && root.context.cross_market ? root.context.cross_market : []
  readonly property var errors: root.snapshot && root.snapshot.errors ? root.snapshot.errors : null
  readonly property string statusError: root.errors && root.errors.status ? String(root.errors.status) : ""
  // A core/shell contract mismatch is surfaced, not silently mis-rendered.
  readonly property string versionError: root.errors && root.errors.version ? String(root.errors.version) : ""

  // "reachable" means the last collector run fetched the status endpoint AND the
  // core's contract version is one this shell understands.
  readonly property bool reachable: root.status !== null && root.failures === 0
    && root.statusError === "" && root.versionError === ""

  // The core's own data-health badge; never inferred locally.
  readonly property string dataHealth: root.status && root.status.data_health
    ? String(root.status.data_health) : (root.reachable ? "unknown" : "down")
  readonly property string aiHealth: root.status && root.status.ai_health
    ? String(root.status.ai_health) : "unknown"

  // The global focus symbol (NQ by default, or from an older core).
  readonly property string focusSymbol: root.status && typeof root.status.focus === "string" && root.status.focus !== "" ? root.status.focus : "NQ"
  readonly property var primary: root.status && root.status.primary ? root.status.primary : null
  readonly property var strip: root.status && root.status.strip ? root.status.strip : []
  readonly property var session: root.status && root.status.session ? root.status.session : null
  readonly property var nextEvent: root.status && root.status.next_event ? root.status.next_event : null
  // The core's rule-based risk tone line; null when the core has no market state.
  readonly property var riskTone: root.status && root.status.risk_tone ? root.status.risk_tone : null
  readonly property var providers: root.status && root.status.providers ? root.status.providers : []
  // Latest plain-English desk note (headline + summary) from the collector.
  readonly property var desk: root.snapshot && root.snapshot.desk ? root.snapshot.desk : null
  readonly property var deskNote: root.desk && root.desk.note ? root.desk.note : null
  // Active Omarchy palette roles (green/red/orange/blue...) served by the core.
  readonly property var themeColors: root.snapshot && root.snapshot.theme && root.snapshot.theme.colors
    ? root.snapshot.theme.colors : null
  // 24-hour sparklines keyed by symbol ({start, end, values}); {} when unavailable.
  readonly property var sparklines: root.snapshot && root.snapshot.sparklines && root.snapshot.sparklines.lines
    ? root.snapshot.sparklines.lines : ({})
  // The focus instrument's prior-week range and prior settle; null when unavailable.
  readonly property var hero: root.snapshot && root.snapshot.hero ? root.snapshot.hero : null

  // Ticking clock so age-based bindings re-evaluate even when the file is not
  // being rewritten (for example while the backend is down).
  property double nowMs: Date.now()
  Timer {
    interval: 5000
    running: true
    repeat: true
    onTriggered: root.nowMs = Date.now()
  }

  // Transport age, distinct from market-data staleness (which the core reports).
  // Measured from the collector's write time, so a stale file is detected even
  // though QML just read it.
  readonly property double ageSeconds: root.dataUpdatedAtMs > 0
    ? (root.dataUpdatedAtMs <= root.nowMs + 10000
      ? Math.max(0, (root.nowMs - root.dataUpdatedAtMs) / 1000) : -1)
    : -1

  // True when the collector has not produced fresh data for a while (or ever).
  // This is a transport signal only; it never overrides the core's feed_status.
  readonly property bool transportStale: root.ageSeconds < 0 || root.ageSeconds > 120
  readonly property string transportState: {
    if (!root.status || root.versionError !== "" || root.failures >= 3 || root.transportStale)
      return "unavailable"
    if (root.failures > 0 || root.statusError !== "") return "unresponsive"
    return "connected"
  }

  signal refreshed()

  function objectValue(value) {
    return value !== null && typeof value === "object" && !Array.isArray(value)
  }
  // Validate presentation shapes only; Python remains the authority for facts.
  function fields(value, strings, numbers, booleans) {
    if (!objectValue(value)) return false
    for (var key of strings)
      if (value[key] !== undefined && value[key] !== null && typeof value[key] !== "string") return false
    for (var numberKey of numbers)
      if (value[numberKey] !== undefined && value[numberKey] !== null && (typeof value[numberKey] !== "number" || !isFinite(value[numberKey]))) return false
    for (var boolKey of booleans)
      if (value[boolKey] !== undefined && typeof value[boolKey] !== "boolean") return false
    return true
  }
  function quote(value) {
    return fields(value, ["symbol", "label", "short_label", "group", "nq_effect", "feed_status", "status", "as_of"], ["value", "change", "change_pct", "change_bp", "age_seconds"], [])
      && typeof value.symbol === "string"
  }
  function sparkline(value) {
    return fields(value, ["start", "end"], [], []) && Array.isArray(value.values)
      && value.values.every(function(x) { return x === null || (typeof x === "number" && isFinite(x)) })
  }
  function validSnapshot(data) {
    if (!objectValue(data) || !objectValue(data.status)) return false
    var status = data.status
    if (!fields(status, ["generated_at", "data_health", "ai_health"], ["version"], [])) return false
    if (status.primary !== undefined && status.primary !== null && !quote(status.primary)) return false
    if (status.session !== undefined && status.session !== null && !fields(status.session, ["phase", "next_open_at", "next_open_local"], [], ["maintenance_break"])) return false
    if (status.next_event !== undefined && status.next_event !== null && !fields(status.next_event, ["name", "importance", "time_et", "time_local", "scheduled_at"], [], [])) return false
    if (status.risk_tone !== undefined && status.risk_tone !== null && !fields(status.risk_tone, ["tone", "label", "line"], [], [])) return false
    if (status.strip !== undefined && (!Array.isArray(status.strip) || !status.strip.every(quote))) return false
    if (status.providers !== undefined && (!Array.isArray(status.providers) || status.providers.some(function(x) { return !fields(x, ["name", "health", "detail"], [], []) || typeof x.name !== "string" || typeof x.health !== "string" }))) return false
    if (data.desk !== undefined && data.desk !== null && (!fields(data.desk, ["session_text"], [], [])
        || (data.desk.note !== undefined && data.desk.note !== null
          && !fields(data.desk.note, ["id", "kind", "title", "created_at", "headline", "summary", "tone", "tone_for"], [], [])))) return false
    if (data.theme !== undefined && data.theme !== null && (!objectValue(data.theme) || !objectValue(data.theme.colors)
        || Object.keys(data.theme.colors).some(function(k) { return !/^#[0-9a-fA-F]{6}$/.test(String(data.theme.colors[k])) }))) return false
    if (data.sparklines !== undefined && data.sparklines !== null && (!fields(data.sparklines, ["interval", "generated_at"], [], [])
        || !objectValue(data.sparklines.lines)
        || Object.keys(data.sparklines.lines).some(function(k) { return !sparkline(data.sparklines.lines[k]) }))) return false
    if (data.hero !== undefined && data.hero !== null && (!fields(data.hero,
        ["symbol", "trade_date", "contract", "prior_week_high_text", "prior_week_low_text", "prior_settle_text", "week_start", "week_end", "week_unavailable_reason"],
        ["prior_week_high", "prior_week_low", "prior_settle"], ["delayed"]) || typeof data.hero.symbol !== "string")) return false
    if (data.context !== undefined && data.context !== null && (!objectValue(data.context) || (data.context.cross_market !== undefined && (!Array.isArray(data.context.cross_market) || !data.context.cross_market.every(quote))))) return false
    return true
  }

  function parse(content) {
    try {
      var data = JSON.parse(String(content || ""))
      if (!validSnapshot(data)) throw new Error("Invalid shell payload shape")
      root.snapshot = data
      if (root.snapshot) {
        root.parsedAtMs = Date.now()
        // The collector may have written updatedAt after the display timer's
        // previous tick. Refresh the clock before calculating transport age.
        root.nowMs = root.parsedAtMs
        var stamp = Date.parse(String(root.snapshot.updatedAt || ""))
        root.dataUpdatedAtMs = isNaN(stamp) ? 0 : stamp
        root.failures = Math.max(0, Number(root.snapshot.consecutiveFailures || 0))
      }
    } catch (e) {
      // Keep the previous snapshot; a parse failure is treated as unhealthy.
      root.failures = root.failures + 1
    }
  }

  FileView {
    path: root.dataPath
    watchChanges: true
    printErrors: false
    onFileChanged: reload()
    onLoaded: root.parse(text())
    onLoadFailed: root.failures = root.failures + 1
  }

  // Run the updater. Guarded: a missing path or bar must not throw.
  function refresh() {
    if (!root.bar || root.updater === "") return
    root.lastRefreshMs = Date.now()
    root.bar.run(root.updater + " " + root.apiPort)
  }

  // Poll every 30s while healthy and every 15s while recovering.
  Timer {
    interval: root.transportState === "connected" ? 30000 : 15000
    running: true
    repeat: true
    onTriggered: root.refresh()
  }

  // First run once the bar handle exists (Component.onCompleted can fire before
  // `bar` is assigned — the sibling plugins retry for the same reason).
  Timer {
    interval: 2000
    running: !root.started
    repeat: true
    onTriggered: {
      if (root.bar) {
        root.started = true
        root.refresh()
      }
    }
  }

  onBarChanged: {
    if (root.bar && !root.started) {
      root.started = true
      root.refresh()
    }
  }
}
