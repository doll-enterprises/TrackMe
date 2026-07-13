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

## Setup

1. **In game, once per session:** type `/combatlog` to start writing the log.
   (Type it again to stop.) Advanced Combat Logging is optional — plain logging
   already includes damage, crit and spell info.
2. **Run the app** (Python 3, uses only the standard library / Tkinter).
   Double-click **`TrackMe.bat`**, or from a terminal:

   ```
   py trackme.py
   ```

   Note: use `py`, not `python` — on Windows `python` may be a Microsoft Store
   stub that isn't the real interpreter. `TrackMe.bat` uses `py` for you.

   By default it watches the WoW `Logs` folder and follows the **newest**
   `WoWCombatLog-*.txt` (Midnight writes a fresh timestamped file each session).
   To point elsewhere, or to pre-set your character name:

   ```
   py trackme.py "D:\path\to\Logs"  Naereith
   ```

## Using it

- The main window lists your (and your pets') spells by damage, sorted, with a
  bar, total, and % — pet rows are tagged `(pet)`.
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
- `trackme.py` is the app. `test_parse.py` is a regression test for the log
  parser and the death snapshot — run it with `py test_parse.py`, or just
  double-click **`Test.bat`** (it pauses so you can read the result).
- `TrackMe.lua` / `TrackMe.toc` are the original in-game addon, kept for
  reference — they can't provide the crit/hit detail on Midnight (see above).
