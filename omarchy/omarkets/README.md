# Omarkets Omarchy shell plugin

A thin Omarchy/Quickshell bar widget and popup for the Omarkets workstation.

- **Bar**: a compact launcher icon (Nerd Font `nf-md-finance`, U+F081F). An orange marker means a collection
  failed and a retry is pending; a red warning means repeated failures or an
  aged snapshot. The bar carries no market numbers.
- **Popup**: cross-asset strip (NQ, ES, US2Y, US10Y, DXY, VIX), each with value,
  change, change %, and feed status; the next scheduled economic events with a
  live countdown; core health; and the data-source timestamp.
- **Notifications**: delivered by the Omarkets core (Phase 8), not this plugin.

## Thin by design

The plugin holds **no market logic**. It reads
`$XDG_STATE_HOME/omarkets/bar.json`, which `update.py` writes from the local
Omarkets API (`127.0.0.1:8765`). Every value and every feed status is copied
verbatim from the core; nothing is calculated, inferred, or invented here.

The status label is deliberate: a delayed or stale quote is shown as such, a
shut market shows CLOSED, and nothing is coloured as a live tick.

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

Each quote in the popup strip shows its status as text:

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
| `Panel.qml` | Compact popup (latest desk note, strip, next event, health) |

## Startup

The backend is one process and one `systemd --user` unit installed from
`omarchy/omarkets.service`; it also serves the built workstation. The QML layer
never starts or manages the backend. The unit carries no settings of its own:
the service reads the project `.env`, and anything unset uses the code
defaults (desk notes for NQ and ES on `desk:v9`). No drop-in is required;
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
