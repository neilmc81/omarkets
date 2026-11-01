# Omarkets Omarchy shell plugin

A thin Omarchy/Quickshell bar widget and popup for the Omarkets workstation.

- **Bar**: a compact launcher icon (Nerd Font `nf-md-finance`, U+F081F). An orange marker means a collection
  failed and a retry is pending; a red warning means repeated failures or an
  aged snapshot. The bar carries no market numbers.
- **Popup**: one session status line; the focus instrument (NQ by default) as a
  hero with price, change, a 24-hour sparkline, last week's range and the prior
  settle; the latest desk note's headline and tone; the other instruments
  grouped as Equities, Rates and Dollar · Volatility, each on two lines (symbol
  and short name) with a 24-hour sparkline, value and change pill; the next
  high-importance event on one line with a live countdown; and a footer with
  Open Omarkets, Calendar, Refresh and a single "data as of · health" line.
- **Notifications**: delivered by the Omarkets core (Phase 8), not this plugin.

## Thin by design

The plugin holds **no market logic**. It reads
`$XDG_STATE_HOME/omarkets/bar.json`, which `update.py` writes from the local
Omarkets API (`127.0.0.1:8765`). Every value and every feed status is copied
verbatim from the core; nothing is calculated, inferred, or invented here.

Colour means **effect on NQ**, not direction. A change pill (and its sparkline)
is green when the core's `nq_effect` says the move helps NQ, red when it hurts,
a faint grey when neutral, and an unfilled outline when the core gives no
effect (an older core, or a quote the risk-tone rules leave out). A ▲/▼/– next to each pill
says the same without colour. A falling yield is therefore green. The popup
never works an effect out itself.

A shut market is said once, in the header ("Closed · opens in 15h 34m"), not
on every row. A row whose feed is stale or unavailable while its market is open
is dimmed.

Demo data never looks real. A quote whose feed is `demo` is dimmed, tagged
**DEMO** beside its symbol, and drawn without an effect colour (its pill is an
outline, its tooltip says "demo data, not a real price"); the footer then leads
with "DEMO data, not real prices" and turns the warning colour, even when the
health badges are all OK. Delayed quotes are said once in the footer ("· delayed") when
every quote on screen is delayed, the usual case; when the feeds are mixed, each
delayed row carries a small "delayed" tag and the footer says "some delayed".

Missing sparklines, hero levels, short names or groups (an older core or
collector) leave those parts out; the rows still render.

The collector retains the last successful status and its `updatedAt` timestamp
when a request fails. It writes the failure count separately, so the popup can
show the last known snapshot with its age and retry every 15 seconds. Three
consecutive failures or a snapshot older than two minutes shows unavailable.
Opening the popup uses the latest collected snapshot and does not launch an
extra request; the background poller and explicit Refresh action handle updates.
The display clock is synchronized when a new file is read, so a valid fresh
timestamp cannot briefly appear to be "in the future" and trigger a false
unavailable warning.
The core supplies the optional UTC `session.next_open_at` and a compact local
rendering, `session.next_open_local` ("Mon 05:00 EDT", or "UTC+7" when the zone
has no short name); the popup shows the latter when the market is closed.

The Gate 7 QML repair obeys the core `interpretation_usable` decision, exposes
original ID/time/context/evidence cutoff, and opens an identity-pinned Analysts
link. Keyboard focus, scrolling and popup bounds are tested using actual isolated
Quickshell components. Malformed consumed objects and array elements retain the
previous good snapshot.

Frozen deployments must point the installed plugin at the same release as the
backend/web. Keep the previous artifact and symlink target for rollback. Only
update maintained workspace QML after the installed link leaves the workspace;
otherwise source edits can hot-reload independently of the backend. QML does not
assemble notes or call a model. See the current repair ledger for the
actual installed release and its live acceptance.

The bar tooltip shows the focus quote's feed status as text:

| Status | Meaning |
| --- | --- |
| LIVE | live feed |
| DELAYED | delayed source |
| CLOSED | shut market; the last value from the latest session |
| STALE | no fresh quote recently |
| DEMO | synthetic (demo) |
| UNAVAILABLE | unavailable / API unreachable |

## Install

From the repository root:

```bash
./omarchy/install-plugin                 # link the plugin + add it to the bar
./omarchy/install-plugin --bindings      # also add launcher, SUPER+CTRL+M popup and calendar hotkeys
./omarchy/install-plugin --port 8765     # configure the API port
```

The installer links `~/.config/omarchy/plugins/omarkets` to this directory, adds
the widget to the bar if missing, and optionally appends a marked hotkey block to
`~/.config/hypr/bindings.lua`. It never overwrites a real directory or an
unrelated symlink.

Re-running `--bindings` migrates the installer-owned legacy popup shortcut from
`SUPER+SHIFT+M` to `SUPER+CTRL+M`, with a backup, preserving stock Music. An
occupied replacement shortcut is refused.

If the bar does not refresh or a plugin rescan retains old popup code, run
`omarchy restart shell` to start a fresh QML engine.

## Files

| File | Role |
| --- | --- |
| `manifest.json` | Omarchy plugin manifest (`bar-widget`) |
| `update.py` | Fetches `GET /api/v1/shell/status` (plus context, desk, theme, sparklines and the focus's key levels) → `bar.json` (stdlib only, atomic) |
| `OmarketsService.qml` | The single shared poller/FileView; widgets consume it |
| `BarWidget.qml` | Bar label + panel wiring |
| `Panel.qml` | Compact popup (session, focus hero, desk headline, grouped rows, next event, footer) |

## Startup

The backend is one process and one `systemd --user` unit installed from
`omarchy/omarkets.service`; it also serves the built workstation. The QML layer
never starts or manages the backend. The unit carries no settings of its own:
the service reads the project `.env`, and anything unset uses the code
defaults (desk notes for NQ, ES and YM on `desk:v9`). No drop-in is required;
`omarchy/smoke-defaults` checks a clean install.

```bash
systemctl --user status omarkets
journalctl --user -u omarkets -f
```

## Hotkeys

| Binding | Action |
| --- | --- |
| `SUPER + M` | Open/focus Omarkets |
| `SUPER + CTRL + M` | Toggle the popup |
| `SUPER + ALT + M` | Open the economic calendar |

## Uninstall

```bash
./omarchy/uninstall-plugin
```

Removes the plugin link, launcher, user service and the marked hotkey block. It
never deletes Omarkets data or the repository, and backs up `bindings.lua`
before editing it.
