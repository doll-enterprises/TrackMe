# TrackMe

A standalone live damage breakdown for **World of Warcraft: Midnight** — the
per-spell crit / hit / cast detail the in-game UI no longer lets addons show.

## Why this is an app, not an addon

Patch 12.0 (Midnight) removed addon access to the combat log:
`COMBAT_LOG_EVENT_UNFILTERED` now errors when you try to register it, and the
Combat Log chat text is wrapped in "Secret Values" that addons can't read. So
an in-game addon **cannot** compute per-spell crit/hit/average/biggest anymore.

WoW still writes the full detail to disk via `/combatlog`
(`WoWCombatLog.txt`). This app tails that file live and renders the breakdown.

## Requirements

The app uses **only the Python standard library — there is nothing to
`pip install`.** You just need Python with Tkinter and the `py`/`pyw` launcher,
which the official Windows installer includes by default.

On most Windows machines this is already present. To install it fresh:

1. Download Python from <https://www.python.org/downloads/windows/> (the
   official installer — **not** the Microsoft Store version).
2. Run the installer and make sure these are enabled (both are on by default):
   - ✅ **py launcher** — installs `py` and `pyw` (the windowless launcher the
     `.vbs` uses).
   - ✅ **tcl/tk and IDLE** (under *Optional Features*) — this is Tkinter, the
     entire GUI.
3. Verify in a terminal — both commands should succeed:

   ```
   pyw --version
   py -c "import tkinter; print('tkinter OK')"
   ```

Windows Script Host (`wscript.exe`), which runs the `.vbs` launcher, is built
into Windows — nothing to install for that.

> Troubleshooting: if `TrackMe.vbs` does nothing, re-run the Python installer →
> **Modify** and enable **py launcher** (if `pyw` is missing) and/or **tcl/tk
> and IDLE** (if the window flashes and vanishes — that means Tkinter is
> missing, common with Microsoft Store Python).

## Setup

1. **In game, once per session:** type `/combatlog` to start writing the log.
   (Type it again to stop.) Advanced Combat Logging is optional — plain logging
   already includes damage, crit and spell info.
2. **Run the app.** Double-click **`TrackMe.vbs`** — it opens just the app
   window, with **no command-prompt window** alongside it. Or from a terminal:

   ```
   py trackme.py
   ```

   Note: use `py`, not `python` — on Windows `python` may be a Microsoft Store
   stub that isn't the real interpreter. `TrackMe.vbs` uses `pyw`, the
   windowless launcher, for you.

   By default it watches the WoW `Logs` folder and follows the **newest**
   `WoWCombatLog-*.txt` (Midnight writes a fresh timestamped file each session).
   To point elsewhere, or to pre-set your character name:

   ```
   py trackme.py "D:\path\to\Logs"  Naereith
   ```

## Using it

- The main window lists your (and your pets') spells by damage, sorted — each
  row's bar is split by color: **spell-school color for normal damage, gold for
  the crit share**. Crits are gold everywhere in the app.
- **Go back** from any detail view three ways: click the breadcrumb bar at the
  top, press **Esc**, or **right-click**.
- **Tabs:** **Damage** (live breakdown), **Fights** (pick any finished fight
  from the dropdown — named by boss/zone — to see its spell breakdown plus any
  deaths that happened during it; a fight ends after ~5s out of combat or on a
  boss pull), and **Deaths** (any group player's death with the last 5s of
  damage they took).
- **Pin** keeps the window on top of WoW. Your name, window size/position, pin
  state, and colors are remembered between launches (`trackme_settings.json`).
- **Settings tab** — set your character name (blank = auto-detect) and
  customize the app's colors: click any color row to open a picker (accent,
  crit gold, background, rows, text, and more), or reset to the defaults.
- **You:** field — leave blank to auto-detect (the most active player in the log),
  or type your character name to lock onto it. Changing it re-scans instantly.
- **Click a spell** to open a detail window: casts, hits, crit rate, total, DPS,
  and a **Non-crit vs Crit** table of hits / damage / average / biggest.
- **Overall / Current** button toggles between the whole session and the current
  fight (which resets after ~5s out of combat, or on an encounter start).
- **Reset** clears everything.

## Notes

- Player detection: by default you're the most active player in the log; your
  pets are attributed by their owner GUID. Type your name in the **You:** field
  if you play in a group and want to be certain. (The combat log's
  "affiliation: mine" flag is unreliable on Midnight, so it isn't used.)
- On launch it reads the current log file from the start, so existing combat in
  this session shows immediately; it then follows new lines live.
- Updates arrive in small bursts — WoW flushes the log file every few seconds, so
  "live" has a second or two of latency. That's a client behavior, not the app.
- `trackme.py` is the app; `TrackMe.vbs` is the launcher. `test_parse.py` is a
  regression test for the log parser and the death snapshot — run it with
  `py test_parse.py`.
