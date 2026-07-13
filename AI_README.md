# AI_README — TrackMe internals & working notes

> Orientation doc for an AI/engineer picking up this repo. `README.md` is the
> **user**-facing guide; this file explains **how the code thinks** and records
> the traps that are easy to get wrong. Add to the "Traps" section whenever a new
> gotcha surfaces.

## What this is

A standalone Python 3 + Tkinter desktop app (`trackme.py`, stdlib only, no pip)
that live-tails WoW's on-disk `WoWCombatLog-*.txt` and renders a per-spell damage
breakdown — plus a **Deaths** tab that snapshots the incoming damage in the last
~5 s before any player in your group dies.

### Why an app and not an addon (the whole reason this project exists)

WoW **Midnight (patch 12.0)** removed addon access to the combat log:
`COMBAT_LOG_EVENT_UNFILTERED` now throws `ADDON_ACTION_FORBIDDEN`, and Combat Log
chat text is wrapped in "Secret Values" addons can't read. The official
replacement `C_DamageMeter` gives total/DPS/overkill/pet but **not**
crit/non-crit, hit count, cast count, or biggest hit. That detail only survives
in the on-disk log written by `/combatlog` — readable only by an out-of-game
tool. So TrackMe pivoted from an addon to this app. The old `TrackMe.lua` /
`TrackMe.toc` are kept for reference but are a dead end on Midnight.

## How it works — data flow

```
Tailer.poll()            -> reads new bytes from the newest WoWCombatLog-*.txt
  parse_line(line)       -> (timestamp, [csv fields])  or None
    Meter.feed(ts,fields)-> attributes damage / tracks deaths
      Segment(current)   -> the current fight (resets after 5s idle)
      Segment(overall)   -> the whole session
      Meter.incoming[]   -> per-player rolling buffer of damage TAKEN
      Meter.deaths[]     -> DeathRecord snapshots on UNIT_DIED
TrackMeUI._tick()        -> every 1s: poll, feed, redraw (Tk after-loop)
```

- **Tailer** follows the *newest* `WoWCombatLog*.txt` in the Logs folder (Midnight
  writes a fresh timestamped file per session). It remembers a byte offset,
  reads only new bytes, keeps a trailing partial line in `self.buffer`, and
  signals a `reset` when a new file appears or the file is truncated.
- **parse_line** splits the leading timestamp off with `LINE_RE`, then parses the
  rest as CSV (`csv.reader`) so quoted spell names with commas survive.
- **Meter.feed** is the brain. Two independent concerns run in it:
  1. **"My" damage** (for the Damage tab): only events sourced by *you* or your
     pets are aggregated into `current`/`overall` Segments.
  2. **Death tracking** (for the Deaths tab): runs for *every* player, before the
     "is this mine?" gate, so it sees the whole group.
- **Segment** aggregates per-spell `SpellRecord`s and tracks `active_time`
  (sum of gaps < `COMBAT_GAP`) for DPS.
- **TrackMeUI** is a single `tk.Canvas` drawn imperatively each tick. There are no
  widgets per row — everything is `create_rectangle`/`create_text`, and clicks
  are resolved against `self.click_regions` (y-band -> action). Navigation is
  plain state: `tab` (damage/fights/deaths), `view` (current/overall), `mode`
  (list/detail), `detail_key`, `death_sel`, `fight_sel`.

### UI design system (2026-07 overhaul)

- **Palette** (constants at the top of the UI section): `BG` deep charcoal-blue,
  `PANEL` bars, `CARD` row backgrounds, `TRACK` inputs/chips, `ACCENT` cyan
  (`ACCENT_INK` for text on accent), `DANGER` red for deaths/overkill,
  `CRIT_COLOR` gold — **crits are gold everywhere, always** (bars, numbers,
  columns, timeline).
- **Damage rows**: light text on `CARD` (never dark-on-bar), rank + name left,
  mono damage + share right, and a **slim dual-segment bar**: school color =
  non-crit share, gold = crit share of that spell's damage. Summary line shows
  session crit%.
- **Fonts**: Segoe UI Semibold (headers/`big`), Segoe UI (body), Cascadia Mono
  fallback Consolas (`fixed`) — resolved at startup from `tkfont.families()`.
- **Navigation**: underlined tab labels (`tab_widgets` label+indicator pairs),
  segmented Current/Overall chips (damage tab only), full-width **breadcrumb**
  bar in detail views (`_draw_crumb`, click = back), plus **Esc and right-click**
  → `_go_back()`. Name field lives in the bottom status bar.
- **Hover**: `_on_motion` draws an accent outline (tagged `"hover"`) over the
  click region under the mouse + hand cursor — no full redraw; `_redraw`
  restores it from `_mouse_y` since `delete("all")` wipes it.
- **Dark title bar**: `_dark_titlebar()` via DWM attribute 20 (ctypes, guarded).
- Death rows carry a 3px `DANGER` stripe on the left edge for identity.

## Who is "you"

Detection priority in `Meter.me()`:

1. **Typed name** (`forced_name` → `forced_guid`) — manual override always wins.
2. **AFFILIATION_MINE flag (`0x1`)** — WoW sets this bit only on the logging
   character (`mine_guid`, captured from the first `Player` source carrying it).
   This is the definitive, non-fragile signal. Verified on a real log: the user's
   character was the *only* one of 63 players with the bit set (see Traps #3).
3. **Most-active `Player-*` source** — fallback for the rare log where the MINE
   bit never appears; locks in once one player has ≥10 events (`_locked_guid`).

Pets are attributed to their owner via the advanced-param owner GUID (index 13 for
spell events, 10 for swings). MINE also marks your pets/totems by design.

## The Deaths feature (added on top of the original framework)

Goal: for any player death, show ~5 s of the damage events that killed them.

- `Meter.incoming`: `dict[victim_guid] -> deque(maxlen=INCOMING_CAP)` of
  `(ts, amount, overkill, spell_name, source_name, crit, school)`. Populated by
  `_track_incoming()` for **any** damage whose *destination* flags have
  `TYPE_PLAYER`. Covers `SPELL/RANGE/PERIODIC` damage, `SWING_DAMAGE` (→ "Melee"),
  and `ENVIRONMENTAL_DAMAGE` (falling/fire/drowning; type string at `fields[9]`).
- `Meter.deaths`: `deque(maxlen=DEATH_KEEP)` of `DeathRecord`. On `UNIT_DIED` for
  a player, `_note_death()` snapshots the victim's buffered hits with
  `ts >= death_ts - DEATH_WINDOW` (chronological order preserved).
- `DeathRecord.killing_blow()`: the last hit carrying `overkill > 0` (the blow
  that dropped them past 0), else the last hit.
- UI: `_draw_death_list` (newest first, name + killing blow) and
  `_draw_death_detail` (fixed-column `time | damage | source` timeline, newest
  first so the **fatal blow is on top** and highlighted, school-colored, crit in
  gold, `OVK` tag on the fatal row). Columns are placed by pixel with the spell
  label truncated via `fixed.measure()` so it can't collide with the OVK tag —
  don't switch amounts back to left-anchored or the columns will overlap again.
- Tunables live in Config: `DEATH_WINDOW` (5.0s), `INCOMING_CAP` (64),
  `DEATH_KEEP` (50). Deaths persist across fight resets but clear on
  `reset_all()` (Reset button / new session).

## The Fights tab (per-combat history)

When a fight ends (>`COMBAT_GAP` idle, or an `ENCOUNTER_START`), `Meter._end_fight()`
**archives** the current `Segment` into `meter.fights` (`deque(maxlen=FIGHT_KEEP)`)
instead of discarding it, then **replaces** `self.current` with a fresh Segment.
Never call `current.reset()` on a segment that was archived — `reset()` mutates in
place and would corrupt the history; replacement is the invariant.

- Fights are labeled by `Segment.label()`: **encounter name** (boss pull) >
  **zone** (tracked from `ZONE_CHANGE`, stamped onto the fight in `_advance`) >
  `main_target()` as last resort. The main target still shows as a "vs …"
  subtitle.
- UI: a native `tk.OptionMenu` **dropdown** (`self.fightbar`, packed only on the
  Fights tab via `before=self.lbl_summary`) selects the fight — newest first,
  defaulting to the newest. The canvas below shows the combined
  `_draw_fight_view`: header, **deaths that happened during the fight**
  (`Meter.deaths_during(seg)`, fight span + COMBAT_GAP grace tail; rows click
  into the death timeline), then the spell breakdown. Spell detail and death
  detail both Back into the fight view.
- The dropdown menu is rebuilt only when the history actually changes
  (`_fights_sig` identity signature) — don't rebuild per tick.
- **Selection state holds objects, never deque indices** (`fight_sel` = Segment,
  `death_sel` = DeathRecord): deque eviction shifts indices and silently switches
  records. Both self-heal to a sensible view if the object is evicted/wiped.

## Settings persistence, theming & window behavior

`trackme_settings.json` (next to the script, gitignored) stores `name`,
`geometry`, `log_path`, `pin`, `colors`. Saved on window close and immediately
on any color change (`_save_settings_now`), loaded in `main()`; **CLI args
always win over saved settings.** "Pin" toggles `root.attributes("-topmost")`.

**Theming:** the palette globals (`BG`, `ACCENT`, `CRIT_COLOR`, …) are
user-editable via the **Settings tab**. `THEME` maps global name → (label,
default); `apply_theme()` rebinds the module globals (validating `#rrggbb`, so a
hand-edited settings file can't break startup). Canvas drawings pick the new
values up on the next redraw automatically; **static widgets don't** — they're
registered at build time via `_reg(widget, opt="GLOBALNAME")` into
`self._styled`, and `_restyle_widgets()` re-applies the palette to all of them
after a change. If you add a new tk widget with themed colors, register it with
`_reg` or it will keep its old colors after a palette change. Color rows use the
native `tkinter.colorchooser` dialog; a reset row restores `default_colors()`.
The character-name entry lives on the Settings tab, embedded in the canvas via
`create_window` (`_make_name_entry`, lazy since it needs the canvas).

## Combat-log field layout (Midnight 12.0.7, advanced logging on)

Per line after the timestamp: **base 9 fields** `[event, srcGUID, srcName,
srcFlags, srcRaidFlags, destGUID, destName, destFlags, destRaidFlags]`, then the
**prefix** (spellId/name/school for SPELL events; none for SWING; envType for
ENVIRONMENTAL), then **19 advanced fields**, then the **10-field damage suffix**,
then an optional **trailing tag** (`ST`/`AOE`).

Because the advanced block's presence/size varies, damage fields are indexed
**from the end** after stripping the trailing tag:
`amount = fields[-10]`, `overkill = fields[-9]`, `crit = fields[-3] == "1"`.

## ⚠️ Traps / gotchas (READ before editing the parser)

These are verified against a real log — don't "fix" them back to textbook values.

1. **Crit is at `fields[-3]`, NOT the classic `-4`.** Verified by diffing crit vs
   non-crit hits. Booleans in the log are `1` / `nil`, not `true`/`false`.
2. **Strip the trailing `ST`/`AOE` tag first.** SPELL/RANGE/PERIODIC damage lines
   end with an extra `ST` (single-target) or `AOE` field; SWING lines don't. The
   `while len>12 and not is_value(fields[-1])` loop pops trailing *non-value*
   tokens so the suffix is always the last 10 fields. `is_value` = numeric or
   literal `nil`.
3. **AFFILIATION_MINE (0x1) reliably marks "you"** (corrected 2026-07-13). On a
   real log the logging character (Fordtruck) carried `0x511` — which *includes*
   the 0x1 bit — on 100% of its events, and was the only one of 63 players with
   it. An earlier note calling it unreliable had misidentified "me" (`0x548` =
   a normal *other* group member: PLAYER+CONTROL_PLAYER+FRIENDLY+OUTSIDER, no
   0x1). `me()` now prefers `mine_guid`, with most-active as fallback. **Read the
   MINE bit off `srcFlags` (a source), never `destFlags`.**
4. **Filename is timestamped per session** (`WoWCombatLog-MMDDYY_HHMMSS.txt`), not
   `WoWCombatLog.txt`. Always follow the newest match by mtime.
5. **`SWING_DAMAGE_LANDED` double-counts** — it fires alongside `SWING_DAMAGE` for
   the same hit. Only count `SWING_DAMAGE`.
6. **`UNIT_DIED` has no source** — the dead unit is the *destination*: GUID
   `fields[5]`, name `fields[6]`, flags `fields[7]`. Filter deaths to players via
   `destFlags & TYPE_PLAYER`. Real lines also carry a trailing `0`
   (`unconsciousOnDeath`) — harmless, it's a value token so it isn't stripped.
7. **Incoming damage uses `destFlags` (`fields[7]`), not `srcFlags`.** The Damage
   tab keys off `srcFlags`; the Deaths tab keys off `destFlags`. Don't cross them.
8. **Live latency is ~1-2s and is a WoW client behavior** — the game flushes the
   log in bursts. Worse: WoW only writes **while combat events are being
   generated** — if you die and sit dead, your death isn't flushed until combat
   resumes. Not an app bug; don't chase it.
10. **`ENCOUNTER_START`, `ZONE_CHANGE` (and other non-unit events) have NO unit
   flags** — `fields[3]` is difficultyID/whatever, so any handler placed after
   the `flags & TYPE_PLAYER / PET_MASK` gate in `feed()` is **unreachable**.
   This exact bug shipped: the old encounter-reset branch never fired. Handle
   non-unit events early, next to `UNIT_DIED`. Formats (verified):
   `ZONE_CHANGE,zoneID,"Zone Name",difficultyID` and
   `ENCOUNTER_START,encID,"Boss Name",difficultyID,groupSize,instanceID` —
   name at `fields[2]` in both.
11. **Never `reset()` a Segment that may be archived in `meter.fights`** —
   `reset()` mutates in place via `__init__`; replace the object instead
   (`self.current = Segment()`).
9. **Environment note (not a code trap):** on this user's install, merely
   registering combat-log events surfaced a Blizzard Edit Mode
   `BT4BarExtraActionBar SetPoint` error caused by a stale Bartender4 anchor in
   the account Edit Mode cache — the meter is the messenger, not the cause. Irrelevant
   to the standalone app but historically confusing.

## Running & testing

```
py trackme.py                          # default log folder, auto-detect you
py trackme.py "D:\path\to\Logs" Name   # explicit folder + character
py test_parse.py                       # regression test (parser + deaths)
```

Double-click launchers: **`TrackMe.vbs`** runs the app with **no console window**
(WScript runs `pyw` — the windowless Python launcher — hidden; this is the one to
hand the user). **`TrackMe.bat`** runs it *with* a console (handy for seeing
errors) — both `cd /d "%~dp0"`. **`Test.bat`** runs the regression tests and
pauses. A `.bat` always flashes a console; only `wscript`/`pyw` avoids it — don't
"fix" the no-console launcher back into a `.bat`.

Use `py`, not `python` (Windows Store stub). `test_parse.py` builds lines in the
real Midnight format and asserts both the damage parse and a death snapshot
(window filtering, chronological order, overkill = fatal blow). Run it after any
parser change. The Tk UI isn't unit-tested — smoke-check with `py -c "import
trackme"` and by eye.

## Ideas for extension

- Deaths: add heals received / absorbs to the timeline (`SPELL_HEAL`,
  `SPELL_ABSORBED`) for a fuller "why did they die" picture.
- Deaths: annotate which hits were avoidable (needs encounter knowledge) or show
  the victim's HP% if `SPELL_DAMAGE` advanced fields (current/max HP) are read.
- A "taken" damage meter tab (mirror of the damage tab but for damage received).
