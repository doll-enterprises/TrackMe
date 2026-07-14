#!/usr/bin/env python3
"""
TrackMe - a standalone live damage breakdown for WoW: Midnight.

Midnight (patch 12.0) removed addon access to the combat log, so an in-game
addon can no longer show per-spell crit/hit/cast detail. But WoW still writes
the FULL detail to disk in a WoWCombatLog-*.txt file when you type /combatlog.
This app tails the newest such file live and renders the breakdown the game
hides from addons:

  * a sorted list of your (and your pets') spells by damage, and
  * click any spell for casts / hits / crit-vs-non-crit / average / biggest.

Usage:
    py trackme.py [logs-folder-or-file] [your-character-name]

The character name is optional - by default you are auto-detected as the most
active player in the log. Pass a name (e.g. "Naereith") to force it if you play
in a group.
"""

import csv
import glob
import json
import os
import re
import sys
import time
from collections import Counter, deque
from datetime import datetime

import tkinter as tk
from tkinter import colorchooser
from tkinter import font as tkfont

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

# A folder (we follow the newest WoWCombatLog-*.txt in it) or a specific file.
DEFAULT_LOG_PATH = (
    r"E:\Blizzard\World of Warcraft\_retail_\World of Warcraft\_retail_\Logs"
)

REFRESH_MS = 1000     # how often we poll the log file (WoW flushes in bursts)
CATCHUP_MS = 15       # poll cadence while a backlog is being chewed through
READ_CHUNK = 1_000_000  # max bytes consumed per poll (keeps the UI responsive)
COMBAT_GAP = 5.0      # seconds of no activity that ends the "current" fight
MAX_ROWS = 22         # spell rows drawn in the main list
HIT_STORE_CAP = 2000  # individual hits kept per spell (bounds memory)
HIT_SHOW = 400        # individual hits drawn in the detail view

# Deaths tab: on every player death we snapshot the damage they took in the
# few seconds beforehand so you can see what killed them.
DEATH_WINDOW = 5.0    # seconds of incoming damage kept before a death
INCOMING_CAP = 64     # incoming hits buffered per player (bounds memory)
DEATH_KEEP = 50       # death records retained for the Deaths tab

FIGHT_KEEP = 50       # finished fights retained for the Fights tab

# Saved UI settings (character name, window geometry, log path, pin state).
SETTINGS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "trackme_settings.json")


def load_settings():
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(data):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except OSError:
        pass          # e.g. read-only folder; settings are a nicety, not vital

# Combat-log object-flag bits.
AFFILIATION_MINE = 0x00000001  # set by WoW on the logging character (and its pets)
TYPE_PLAYER   = 0x00000400
TYPE_PET      = 0x00001000
TYPE_GUARDIAN = 0x00002000
PET_MASK      = TYPE_PET | TYPE_GUARDIAN

# Damage sub-events we track. They share a 10-field damage suffix that (after
# stripping Midnight's trailing "ST"/"AOE" tag) always sits at the END of the
# line, so we index it from the end and never care whether advanced logging is
# on or how many advanced fields there are.
DAMAGE_EVENTS = {
    "SPELL_DAMAGE", "SPELL_PERIODIC_DAMAGE", "RANGE_DAMAGE",
    "SPELL_BUILDING_DAMAGE", "DAMAGE_SHIELD",
}

# Every event type the Meter actually consumes. Anything else (aura spam,
# heals, energizes - the bulk of a log) is skipped BEFORE the expensive
# csv/timestamp parse; see quick_event().
INTERESTING_EVENTS = DAMAGE_EVENTS | {
    "SWING_DAMAGE", "SPELL_CAST_SUCCESS", "ENVIRONMENTAL_DAMAGE",
    "UNIT_DIED", "ZONE_CHANGE", "ENCOUNTER_START",
}


def quick_event(line):
    """Cheaply extract the event name ('7/13/2026 00:15:35.123-4  EVENT,...')
    without regex or csv. Returns None if the line has no event field."""
    i = line.find(",")
    if i < 0:
        return None
    j = line.rfind(" ", 0, i)
    return line[j + 1:i]

SCHOOL_COLORS = {
    1:  "#d9b96a",  # Physical
    2:  "#f4e6a8",  # Holy
    4:  "#ff7847",  # Fire
    8:  "#59d97a",  # Nature
    16: "#5cc8f2",  # Frost
    32: "#9b7bff",  # Shadow
    64: "#e879e8",  # Arcane
}
DEFAULT_COLOR = "#7f8dd9"
PET_COLOR     = "#4ecfc4"

# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

LINE_RE = re.compile(
    r"^(?P<ts>\d{1,2}/\d{1,2}/\d{4}\s+\d{1,2}:\d{2}:\d{2}\.\d+(?:[-+]\d+)?)"
    r"\s+(?P<payload>\S.*)$"
)
NUM_RE = re.compile(r"^-?\d+(?:\.\d+)?$")


def is_value(tok):
    """A real suffix field is a number or the literal 'nil'. Tags like 'ST' are not."""
    return tok == "nil" or bool(NUM_RE.match(tok))


# strptime is the hottest call when chewing through a large log, and events
# arrive in bursts within the same second - so cache epoch per whole second.
_TS_CACHE = {}


def parse_timestamp(ts):
    # "7/13/2026 00:15:35.123-4" -> seconds part (cached) + fraction.
    main, _, rest = ts.partition(".")
    base = _TS_CACHE.get(main)
    if base is None:
        try:
            base = datetime.strptime(main.strip(),
                                     "%m/%d/%Y %H:%M:%S").timestamp()
        except ValueError:
            return None
        if len(_TS_CACHE) > 200_000:      # bound memory across huge sessions
            _TS_CACHE.clear()
        _TS_CACHE[main] = base
    digits = ""
    for ch in rest:                        # fraction ends at the tz offset
        if ch.isdigit():
            digits += ch
        else:
            break
    return base + (int(digits) / 10 ** len(digits) if digits else 0.0)


def parse_line(line):
    m = LINE_RE.match(line)
    if not m:
        return None
    ts = parse_timestamp(m.group("ts"))
    if ts is None:
        ts = time.time()
    try:
        fields = next(csv.reader([m.group("payload")]))
    except (csv.Error, StopIteration):
        return None
    return ts, fields


def to_int(value, base=10, default=0):
    try:
        return int(value, base)
    except (ValueError, TypeError):
        return default

# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------


class SpellRecord:
    __slots__ = ("key", "name", "spell_id", "is_pet", "school",
                 "casts", "n_hits", "n_total", "n_max",
                 "c_hits", "c_total", "c_max", "total", "events")

    def __init__(self, key, name, spell_id, is_pet, school):
        self.key, self.name, self.spell_id = key, name, spell_id
        self.is_pet, self.school = is_pet, school
        self.casts = 0
        self.n_hits = self.n_total = self.n_max = 0
        self.c_hits = self.c_total = self.c_max = 0
        self.total = 0
        self.events = deque(maxlen=HIT_STORE_CAP)  # (ts, amount, crit, target)

    def add_damage(self, amount, crit, ts, target):
        if crit:
            self.c_hits += 1
            self.c_total += amount
            self.c_max = max(self.c_max, amount)
        else:
            self.n_hits += 1
            self.n_total += amount
            self.n_max = max(self.n_max, amount)
        self.total += amount
        self.events.append((ts, amount, crit, target))

    @property
    def hits(self):
        return self.n_hits + self.c_hits

    @property
    def crit_pct(self):
        return (self.c_hits / self.hits * 100) if self.hits else 0.0


class DeathRecord:
    """A player death plus the incoming damage that led up to it."""
    __slots__ = ("ts", "name", "guid", "events")

    def __init__(self, ts, name, guid, events):
        self.ts = ts          # timestamp of the UNIT_DIED event
        self.name = name      # dead player's name
        self.guid = guid      # dead player's GUID
        # events: list of (ts, amount, overkill, spell_name, source_name, crit, school)
        self.events = events

    def total_taken(self):
        return sum(e[1] for e in self.events)

    def killing_blow(self):
        """The fatal hit: the last event carrying overkill, else the last hit."""
        if not self.events:
            return None
        fatal = self.events[-1]
        for e in self.events:
            if e[2] > 0:      # overkill > 0 marks the blow that dropped them
                fatal = e
        return fatal


class Segment:
    def __init__(self):
        self.spells = {}
        self.total = 0
        self.active_time = 0.0
        self.first_ts = None
        self.last_ts = None
        self.targets = Counter()   # target name -> damage dealt
        self.zone = None           # zone the fight happened in (ZONE_CHANGE)
        self.encounter = None      # boss name if it was a pull (ENCOUNTER_START)

    def reset(self):
        self.__init__()

    def note_time(self, ts):
        if self.last_ts is not None:
            dt = ts - self.last_ts
            if 0 < dt < COMBAT_GAP:
                self.active_time += dt
        if self.first_ts is None:
            self.first_ts = ts
        self.last_ts = ts

    def _rec(self, key, name, spell_id, is_pet, school):
        rec = self.spells.get(key)
        if rec is None:
            rec = SpellRecord(key, name, spell_id, is_pet, school)
            self.spells[key] = rec
        elif school:
            rec.school = school
        return rec

    def record(self, key, name, spell_id, is_pet, school, amount, crit, ts, target):
        self._rec(key, name, spell_id, is_pet, school).add_damage(amount, crit, ts, target)
        self.total += amount
        if target:
            self.targets[target] += amount

    def main_target(self):
        return self.targets.most_common(1)[0][0] if self.targets else "Unknown"

    def label(self):
        """Fight name: boss pull > zone > main target (last resort)."""
        return self.encounter or self.zone or self.main_target()

    def duration(self):
        if self.first_ts is None or self.last_ts is None:
            return 0.0
        return self.last_ts - self.first_ts

    def record_cast(self, key, name, spell_id, is_pet, school):
        self._rec(key, name, spell_id, is_pet, school).casts += 1

    def dps(self):
        return self.total / self.active_time if self.active_time > 0.5 else 0.0

    def sorted_spells(self):
        rows = [r for r in self.spells.values() if r.total > 0]
        rows.sort(key=lambda r: r.total, reverse=True)
        return rows


class Meter:
    """Turns parsed log lines into recorded damage for you and your pets."""

    def __init__(self, forced_name=None):
        self.current = Segment()
        self.overall = Segment()
        self.forced_name = forced_name.lower() if forced_name else None
        self.forced_guid = None
        self.player_counts = Counter()   # Player-GUID -> event count
        self._locked_guid = None
        self.mine_guid = None            # player GUID flagged AFFILIATION_MINE (0x1)
        self.me_name = None
        self.zone = None                 # current zone name (from ZONE_CHANGE)
        # Death tracking (for ALL players, not just "you").
        self.incoming = {}               # player-GUID -> deque of recent hits taken
        self.deaths = deque(maxlen=DEATH_KEEP)
        # Finished fights, oldest -> newest (Fights tab history).
        self.fights = deque(maxlen=FIGHT_KEEP)

    def reset_all(self):
        self.current = Segment()
        self.overall = Segment()
        self.incoming.clear()
        self.deaths.clear()
        self.fights.clear()

    def me(self):
        """The GUID we attribute damage to.

        Priority: (1) a name you typed, (2) the AFFILIATION_MINE flag — WoW sets
        it only on the logging character, so it's a definitive self-marker — and
        only then (3) the most-active-player fallback, for the rare log where the
        MINE bit never appears.
        """
        if self.forced_guid:
            return self.forced_guid
        if self.mine_guid:
            return self.mine_guid
        if self._locked_guid:
            return self._locked_guid
        if not self.player_counts:
            return None
        guid, count = self.player_counts.most_common(1)[0]
        if count >= 10:
            self._locked_guid = guid   # lock once one player clearly dominates
        return guid

    @staticmethod
    def _owner_guid(fields, event):
        # Advanced params begin right after the prefix; the 2nd of them is the
        # owner GUID (index 10 for swings which have no prefix, else 13).
        idx = 10 if event == "SWING_DAMAGE" else 13
        return fields[idx] if len(fields) > idx else "?"

    def feed(self, ts, fields):
        if not fields or len(fields) < 4:
            return
        event = fields[0]

        # Drop Midnight's trailing spell tag(s) ("ST"/"AOE") so the damage
        # suffix is always the last 10 fields.
        while len(fields) > 12 and not is_value(fields[-1]):
            fields.pop()

        flags = to_int(fields[3], 16)
        src_guid, src_name = fields[1], fields[2]
        target = fields[6] if len(fields) > 6 else ""

        # Death tracking runs for every player (not just "you") and before the
        # "is this my damage?" gate below, so it sees the whole group.
        # Non-unit events have no unit flags in fields[3], so they must be
        # handled BEFORE the player/pet flag gate below (it would drop them).
        if event == "UNIT_DIED":
            self._note_death(ts, fields)
            return
        if event == "ZONE_CHANGE":       # ZONE_CHANGE,zoneID,"Zone Name",diff
            if len(fields) > 2 and fields[2]:
                self.zone = fields[2]
            return
        if event == "ENCOUNTER_START":   # ENCOUNTER_START,id,"Boss Name",...
            self._end_fight()
            if len(fields) > 2 and fields[2]:
                self.current.encounter = fields[2]
            return
        self._track_incoming(ts, event, fields)

        if self.forced_name and (flags & TYPE_PLAYER) and self.forced_name in src_name.lower():
            self.forced_guid = src_guid

        # Which player does this event belong to?
        if flags & TYPE_PLAYER:
            self.player_counts[src_guid] += 1
            if self.mine_guid is None and (flags & AFFILIATION_MINE):
                self.mine_guid = src_guid   # definitive: this is the logging char
            attr_guid, is_pet = src_guid, False
        elif flags & PET_MASK:
            attr_guid, is_pet = self._owner_guid(fields, event), True
        else:
            return

        me = self.me()
        if me is None or attr_guid != me:
            return
        if not is_pet:
            self.me_name = src_name

        if event in DAMAGE_EVENTS:
            if len(fields) < 12:
                return
            spell_id, spell_name = fields[9], fields[10]
            school = to_int(fields[11], 0)
            amount = to_int(fields[-10])
            crit = fields[-3] == "1"
            if amount <= 0:
                return
            key = ("pet:" if is_pet else "you:") + spell_id
            self._advance(ts)
            self.current.record(key, spell_name, spell_id, is_pet, school, amount, crit, ts, target)
            self.overall.record(key, spell_name, spell_id, is_pet, school, amount, crit, ts, target)

        elif event == "SWING_DAMAGE":
            amount = to_int(fields[-10])
            crit = fields[-3] == "1"
            if amount <= 0:
                return
            key = "pet:swing" if is_pet else "you:swing"
            name = "Melee (Pet)" if is_pet else "Melee"
            self._advance(ts)
            self.current.record(key, name, None, is_pet, 1, amount, crit, ts, target)
            self.overall.record(key, name, None, is_pet, 1, amount, crit, ts, target)

        elif event == "SPELL_CAST_SUCCESS":
            if len(fields) < 11:
                return
            spell_id, spell_name = fields[9], fields[10]
            school = to_int(fields[11], 0) if len(fields) > 11 else 0
            key = ("pet:" if is_pet else "you:") + spell_id
            self._advance(ts)
            self.current.record_cast(key, spell_name, spell_id, is_pet, school)
            self.overall.record_cast(key, spell_name, spell_id, is_pet, school)

    def _end_fight(self):
        """Archive the current fight into history (if it saw damage) and start fresh.

        NOTE: we REPLACE self.current rather than reset it in place — the old
        Segment object lives on inside self.fights, so mutating it would corrupt
        the archived history.
        """
        if self.current.total > 0:
            self.fights.append(self.current)
        self.current = Segment()

    def _advance(self, ts):
        if self.current.last_ts is not None and (ts - self.current.last_ts) > COMBAT_GAP:
            self._end_fight()
        if self.current.zone is None and self.zone:
            self.current.zone = self.zone     # stamp where the fight happens
        self.current.note_time(ts)
        self.overall.note_time(ts)

    def deaths_during(self, seg):
        """Deaths that happened inside this fight (with a grace tail: you can
        die moments after your own last damage event)."""
        if seg.first_ts is None:
            return []
        end = (seg.last_ts or seg.first_ts) + COMBAT_GAP
        return [d for d in self.deaths if seg.first_ts <= d.ts <= end]

    # -- death tracking -------------------------------------------------------

    def _track_incoming(self, ts, event, fields):
        """Buffer damage TAKEN by any player, keyed by the victim's GUID."""
        if len(fields) < 8:
            return
        dest_flags = to_int(fields[7], 16)
        if not (dest_flags & TYPE_PLAYER):     # only care about damage to players
            return

        if event in DAMAGE_EVENTS:
            if len(fields) < 12:
                return
            spell_name = fields[10]
            school = to_int(fields[11], 0)
        elif event == "SWING_DAMAGE":
            spell_name, school = "Melee", 1
        elif event == "ENVIRONMENTAL_DAMAGE":
            # base 9 fields, then the environmental type (Falling/Fire/Drowning/...)
            spell_name = fields[9] if len(fields) > 9 else "Environment"
            school = 0
        else:
            return

        amount = to_int(fields[-10])
        if amount <= 0:
            return
        overkill = to_int(fields[-9])
        crit = fields[-3] == "1"
        victim = fields[5]
        source = fields[2] if fields[2] not in ("nil", "") else None

        dq = self.incoming.get(victim)
        if dq is None:
            dq = deque(maxlen=INCOMING_CAP)
            self.incoming[victim] = dq
        dq.append((ts, amount, overkill, spell_name, source, crit, school))

    def _note_death(self, ts, fields):
        """On UNIT_DIED for a player, snapshot the last DEATH_WINDOW s of damage."""
        if len(fields) < 8:
            return
        dest_flags = to_int(fields[7], 16)
        if not (dest_flags & TYPE_PLAYER):     # ignore NPC/pet deaths
            return
        guid, name = fields[5], fields[6]
        dq = self.incoming.get(guid)
        events = [e for e in dq if e[0] >= ts - DEATH_WINDOW] if dq else []
        self.deaths.append(DeathRecord(ts, name, guid, events))

# --------------------------------------------------------------------------
# Log tailing (follows the newest WoWCombatLog-*.txt)
# --------------------------------------------------------------------------


class Tailer:
    def __init__(self, path):
        self.given = path
        self.current_file = None
        self.pos = None
        self.buffer = ""
        self.size = 0          # last known file size (for progress reporting)

    def backlog(self):
        """Bytes still unread in the current file (drives the progress bar)."""
        if self.pos is None:
            return 0
        return max(0, self.size - self.pos)

    def _resolve(self):
        p = self.given
        if os.path.isfile(p):
            return p
        folder = p if os.path.isdir(p) else (os.path.dirname(p) or ".")
        matches = glob.glob(os.path.join(folder, "WoWCombatLog*.txt"))
        return max(matches, key=os.path.getmtime) if matches else None

    def poll(self):
        """Return (lines, session_reset, file_path_or_None)."""
        target = self._resolve()
        if not target:
            return [], False, None

        reset = False
        if target != self.current_file:      # a newer session file appeared
            self.current_file = target
            self.pos = None
            self.buffer = ""
            reset = True

        try:
            size = os.path.getsize(target)
        except OSError:
            return [], reset, target
        self.size = size

        if self.pos is None:
            self.pos = 0                      # read the current file from start
        if size < self.pos:                   # file truncated -> new session
            self.pos = 0
            self.buffer = ""
            reset = True
        if size == self.pos:
            return [], reset, target

        # Read at most READ_CHUNK bytes per poll: a huge backlog (first launch
        # against a long session) is consumed across many quick ticks instead
        # of freezing the UI in one giant read.
        with open(target, "r", encoding="utf-8", errors="replace") as f:
            f.seek(self.pos)
            data = f.read(READ_CHUNK)
            self.pos = f.tell()

        self.buffer += data
        lines = self.buffer.split("\n")
        self.buffer = lines.pop()             # keep trailing partial line
        return lines, reset, target

# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------

BG      = "#0e1118"   # window background (deep charcoal-blue)
PANEL   = "#151a24"   # header / status / breadcrumb bars
CARD    = "#171c28"   # list-row cards
TRACK   = "#212836"   # inputs, chips, inactive controls
TEXT    = "#e9ecf4"
SUBTEXT = "#7e8698"
ACCENT  = "#59d1ff"   # electric cyan
ACCENT_INK = "#0b0e14"  # text drawn ON an accent background
DANGER  = "#ff6b7a"   # deaths / overkill
CRIT_COLOR = "#ffc75c"  # crits are ALWAYS this gold, everywhere

# User-themable colors (Settings tab): global name -> (label, default).
# apply_theme() rebinds the module-level names above; the canvas reads them
# live on every redraw, and _restyle_widgets() refreshes the static widgets.
THEME = {
    "ACCENT":     ("Accent", ACCENT),
    "CRIT_COLOR": ("Crit gold", CRIT_COLOR),
    "DANGER":     ("Deaths / overkill", DANGER),
    "PET_COLOR":  ("Pet bars", PET_COLOR),
    "TEXT":       ("Text", TEXT),
    "SUBTEXT":    ("Muted text", SUBTEXT),
    "BG":         ("Background", BG),
    "PANEL":      ("Panels", PANEL),
    "CARD":       ("Rows", CARD),
    "TRACK":      ("Inputs / chips", TRACK),
}
HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def apply_theme(overrides):
    """Rebind the palette globals from a {name: '#rrggbb'} dict (bad values
    and unknown keys are ignored, so a hand-edited settings file can't break
    the app)."""
    for key, val in (overrides or {}).items():
        if key in THEME and isinstance(val, str) and HEX_RE.match(val):
            globals()[key] = val


def current_colors():
    return {key: globals()[key] for key in THEME}


def default_colors():
    return {key: default for key, (_label, default) in THEME.items()}


def abbrev(n):
    n = n or 0
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 10_000:
        return f"{n / 1000:.1f}k"
    return str(int(round(n)))


def commas(n):
    return f"{int(round(n or 0)):,}"


def fmt_dur(seconds):
    s = max(0, int(round(seconds or 0)))
    return f"{s // 60}:{s % 60:02d}"


class TrackMeUI:
    def __init__(self, log_path, forced_name=None, settings=None):
        self.meter = Meter(forced_name)
        self.tailer = Tailer(log_path)
        self.settings = settings or {}
        self.tab = "damage"         # "damage" | "deaths" | "fights"
        self.view = "current"
        self.mode = "list"          # "list" | "detail"
        self.detail_key = None
        self.death_sel = None       # DeathRecord OBJECT being viewed (never an
                                    # index: deque eviction shifts indices)
        self.fight_sel = None       # Segment OBJECT from meter.fights (same rule)
        self.pin_on = False
        self.click_regions = []     # (y0, y1, (action, value)) in canvas coords

        self.root = tk.Tk()
        self.root.title("TrackMe - live combat-log breakdown")
        self.root.configure(bg=BG)
        self.root.geometry(self.settings.get("geometry") or "440x600")
        self.root.minsize(360, 340)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._dark_titlebar()

        # Fonts, with graceful fallbacks for older Windows installs.
        fams = set(tkfont.families())
        mono = "Cascadia Mono" if "Cascadia Mono" in fams else "Consolas"
        semi = "Segoe UI Semibold" if "Segoe UI Semibold" in fams else "Segoe UI"
        semi_w = "normal" if semi == "Segoe UI Semibold" else "bold"
        self.fixed = tkfont.Font(family=mono, size=9)
        self.body = tkfont.Font(family="Segoe UI", size=9)
        self.small = tkfont.Font(family="Segoe UI", size=8)
        self.bold = tkfont.Font(family=semi, size=10, weight=semi_w)
        self.big = tkfont.Font(family=semi, size=13, weight=semi_w)

        self._hover = None          # (y0, y1) of the click region under the mouse
        self._mouse_y = None        # last mouse y (canvas coords), for re-hover
        self._skip_draws = 0        # redraw throttle while loading a backlog

        self._build_header()
        self._build_statusbar()
        if forced_name:
            self.name_var.set(forced_name)
        if self.settings.get("pin"):
            self._toggle_pin()

        self.canvas = tk.Canvas(self.root, bg=BG, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, padx=8, pady=(2, 4))
        self.canvas.bind("<Button-1>", self._on_click)
        self.canvas.bind("<Configure>", lambda e: self._redraw())
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Leave>", lambda e: self._set_hover(None))
        # Two extra ways back besides the breadcrumb: right-click and Escape.
        self.canvas.bind("<Button-3>", lambda e: self._go_back())
        self.root.bind("<Escape>", lambda e: self._go_back())

        self.root.after(200, self._tick)

    def _dark_titlebar(self):
        """Ask DWM for a dark title bar so it matches the app (Win10 1809+)."""
        try:
            import ctypes
            self.root.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            value = ctypes.c_int(1)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 20, ctypes.byref(value), ctypes.sizeof(value))
        except Exception:
            pass                      # cosmetic only; never block startup

    # -- header ---------------------------------------------------------------

    def _reg(self, widget, **opts):
        """Register a widget's theme-driven options (opt -> THEME/global name)
        so _restyle_widgets() can re-apply them after a color change."""
        self._styled.append((widget, opts))
        widget.config(**{opt: globals()[key] for opt, key in opts.items()})

    def _build_header(self):
        self._styled = []
        top = tk.Frame(self.root)
        top.pack(fill="x", side="top")
        self._reg(top, bg="PANEL")
        title = tk.Label(top, text="TrackMe", font=self.big)
        title.pack(side="left", padx=10, pady=6)
        self._reg(title, bg="PANEL", fg="ACCENT")
        btn_reset = tk.Button(top, text="Reset", relief="flat", bd=0,
                              font=self.small, cursor="hand2",
                              command=self._reset)
        btn_reset.pack(side="right", padx=(0, 10), pady=6)
        self._reg(btn_reset, bg="PANEL", fg="SUBTEXT",
                  activebackground="TRACK", activeforeground="DANGER")
        self.btn_pin = tk.Button(top, text="Pin", relief="flat", bd=0,
                                 font=self.small, cursor="hand2",
                                 command=self._toggle_pin)
        self.btn_pin.pack(side="right", padx=6, pady=6)
        self._reg(self.btn_pin, bg="PANEL",
                  activebackground="TRACK", activeforeground="ACCENT")
        self.btn_pin.config(fg=SUBTEXT)   # fg is pin-state dependent

        # Tab bar: text + accent underline, view toggle on the right.
        tabbar = tk.Frame(self.root)
        tabbar.pack(fill="x", padx=8, pady=(6, 0))
        self._reg(tabbar, bg="BG")
        self.tab_widgets = {}
        for name, label in (("damage", "Damage"), ("fights", "Fights"),
                            ("deaths", "Deaths"), ("settings", "Settings")):
            wrap = tk.Frame(tabbar)
            wrap.pack(side="left", padx=(0, 2))
            self._reg(wrap, bg="BG")
            lbl = tk.Label(wrap, text=label, font=self.bold,
                           padx=10, pady=3, cursor="hand2")
            lbl.pack()
            self._reg(lbl, bg="BG")
            ind = tk.Frame(wrap, height=2)
            ind.pack(fill="x", padx=6)
            lbl.bind("<Button-1>", lambda e, n=name: self._set_tab(n))
            self.tab_widgets[name] = (lbl, ind)

        self.view_seg = tk.Frame(tabbar)
        self._reg(self.view_seg, bg="TRACK")
        self.view_labels = {}
        for v, txt in (("current", "Current"), ("overall", "Overall")):
            lbl = tk.Label(self.view_seg, text=txt, font=self.small,
                           padx=9, pady=2, cursor="hand2")
            lbl.pack(side="left")
            lbl.bind("<Button-1>", lambda e, vv=v: self._set_view(vv))
            self.view_labels[v] = lbl

        self.lbl_summary = tk.Label(self.root, text="", anchor="w",
                                    font=self.bold)
        self.lbl_summary.pack(fill="x", padx=10, pady=(4, 2))
        self._reg(self.lbl_summary, bg="BG", fg="TEXT")

        # Fight selector (dropdown) - only shown on the Fights tab.
        self.fightbar = tk.Frame(self.root)
        self._reg(self.fightbar, bg="BG")
        lbl_fight = tk.Label(self.fightbar, text="Fight:", font=self.small)
        lbl_fight.pack(side="left")
        self._reg(lbl_fight, bg="BG", fg="SUBTEXT")
        self.fight_var = tk.StringVar(value="")
        self.fight_menu = tk.OptionMenu(self.fightbar, self.fight_var, "")
        self.fight_menu.config(relief="flat", highlightthickness=0,
                               anchor="w", font=self.small)
        self._reg(self.fight_menu, bg="TRACK", fg="TEXT",
                  activebackground="ACCENT", activeforeground="ACCENT_INK")
        self.fight_menu["menu"].config(font=self.small)
        self._reg(self.fight_menu["menu"], bg="PANEL", fg="TEXT",
                  activebackground="ACCENT", activeforeground="ACCENT_INK")
        self.fight_menu.pack(side="left", fill="x", expand=True, padx=4)
        self._fight_items = []        # [(label, Segment)] newest first
        self._fights_sig = None       # change-detector for menu rebuilds

        # Character-name entry: created once, embedded in the canvas by the
        # Settings tab (create_window).
        self.name_var = tk.StringVar()
        self.name_entry = None        # created lazily (needs the canvas)
        self._style_tabs()

    def _make_name_entry(self):
        if self.name_entry is None:
            self.name_entry = tk.Entry(self.canvas, textvariable=self.name_var,
                                       relief="flat", font=self.body, width=20)
            self._reg(self.name_entry, bg="TRACK", fg="TEXT",
                      insertbackground="TEXT")
            self.name_entry.bind("<Return>", self._apply_name)
            self.name_entry.bind("<FocusOut>", self._apply_name)
        return self.name_entry

    def _build_statusbar(self):
        bar = tk.Frame(self.root)
        bar.pack(fill="x", side="bottom")
        self._reg(bar, bg="PANEL")
        self.lbl_status = tk.Label(bar, text="", anchor="w", font=self.small)
        self.lbl_status.pack(side="left", fill="x", expand=True, padx=8, pady=2)
        self._reg(self.lbl_status, bg="PANEL", fg="SUBTEXT")

        # Thin progress strip above the status bar; the accent fill's relwidth
        # is the loading fraction (0 = idle, it reads as a divider line).
        self.progress_track = tk.Frame(self.root, height=3)
        self.progress_track.pack(fill="x", side="bottom")
        self._reg(self.progress_track, bg="TRACK")
        self.progress_fill = tk.Frame(self.progress_track)
        self.progress_fill.place(x=0, y=0, relheight=1.0, relwidth=0.0)
        self._reg(self.progress_fill, bg="ACCENT")

    def _show_progress(self, pos, total):
        frac = (pos / total) if total else 0.0
        self.progress_fill.place_configure(relwidth=max(0.0, min(1.0, frac)))
        self.lbl_status.config(
            text=f"loading log... {frac * 100:.0f}%   "
                 f"({pos / 1e6:.1f} / {total / 1e6:.1f} MB)")

    def _hide_progress(self):
        self.progress_fill.place_configure(relwidth=0.0)

    def _restyle_widgets(self):
        """Re-apply the (possibly changed) palette to every static widget."""
        self.root.configure(bg=BG)
        self.canvas.config(bg=BG)
        for widget, opts in self._styled:
            widget.config(**{opt: globals()[key] for opt, key in opts.items()})
        self._style_tabs()
        self.btn_pin.config(fg=ACCENT if self.pin_on else SUBTEXT)

    def _style_tabs(self):
        for name, (lbl, ind) in self.tab_widgets.items():
            active = name == self.tab
            lbl.config(fg=TEXT if active else SUBTEXT)
            ind.config(bg=ACCENT if active else BG)
        # Current/Overall only applies to the Damage tab.
        if self.tab == "damage":
            self.view_seg.pack(side="right", pady=2)
            self._style_view()
        else:
            self.view_seg.pack_forget()

    def _style_view(self):
        for v, lbl in self.view_labels.items():
            active = v == self.view
            lbl.config(bg=ACCENT if active else TRACK,
                       fg=ACCENT_INK if active else SUBTEXT)

    def _set_view(self, view):
        if view == self.view:
            return
        self.view = view
        self._style_view()
        self._redraw()

    def _set_tab(self, tab):
        self.tab = tab
        self.mode, self.detail_key = "list", None
        self.death_sel = None
        self.fight_sel = None
        self._style_tabs()
        if tab == "fights":
            self.fightbar.pack(fill="x", padx=8, pady=(0, 2),
                               before=self.lbl_summary)
        else:
            self.fightbar.pack_forget()
        self.canvas.yview_moveto(0)
        self._redraw()

    def _toggle_pin(self):
        self.pin_on = not self.pin_on
        self.root.attributes("-topmost", self.pin_on)
        self.btn_pin.config(fg=ACCENT if self.pin_on else SUBTEXT)

    def _save_settings_now(self):
        save_settings({
            "name": self.name_var.get().strip(),
            "geometry": self.root.geometry(),
            "log_path": self.tailer.given,
            "pin": self.pin_on,
            "colors": current_colors(),
        })

    def _on_close(self):
        self._save_settings_now()
        self.root.destroy()

    def _apply_name(self, *_):
        name = self.name_var.get().strip()
        if name.lower() == (self.meter.forced_name or ""):
            return
        self.meter = Meter(name or None)
        self.tailer.current_file = None   # force a full re-read for the new name
        self.tailer.pos = None
        self.tailer.buffer = ""
        self.mode, self.detail_key = "list", None
        self.death_sel = None
        self.fight_sel = None             # old Segment belongs to the old meter
        self._redraw()

    # -- navigation / events --------------------------------------------------

    def _segment(self):
        return self.meter.overall if self.view == "overall" else self.meter.current

    def _go_back(self):
        """One level up: Esc / right-click / breadcrumb all land here."""
        if self.death_sel is not None:
            self.death_sel = None
        elif self.mode == "detail":
            self.mode, self.detail_key = "list", None
        else:
            return
        self.canvas.yview_moveto(0)
        self._redraw()

    def _reset(self):
        self.meter.reset_all()
        self.mode, self.detail_key = "list", None
        self.death_sel = None
        self.fight_sel = None
        self.canvas.yview_moveto(0)
        self._redraw()

    def _on_wheel(self, event):
        # Only scroll when the content is actually taller than the viewport;
        # otherwise the wheel would drift the (fully visible) list up and down.
        bbox = self.canvas.bbox("all")
        if not bbox:
            return
        if (bbox[3] - bbox[1]) <= self.canvas.winfo_height():
            return
        self.canvas.yview_scroll(int(-event.delta / 120), "units")

    def _region_at(self, y):
        for y0, y1, act in self.click_regions:
            if y0 <= y <= y1:
                return (y0, y1, act)
        return None

    def _set_hover(self, band):
        """Draw/clear the hover outline without a full canvas redraw."""
        if band == self._hover:
            return
        self._hover = band
        self.canvas.delete("hover")
        if band:
            w = self.canvas.winfo_width()
            self.canvas.create_rectangle(1, band[0], w - 2, band[1],
                                         outline=ACCENT, width=1, tags="hover")
            self.canvas.config(cursor="hand2")
        else:
            self.canvas.config(cursor="")

    def _on_motion(self, event):
        self._mouse_y = self.canvas.canvasy(event.y)
        hit = self._region_at(self._mouse_y)
        self._set_hover((hit[0], hit[1]) if hit else None)

    def _on_click(self, event):
        y = self.canvas.canvasy(event.y)
        for y0, y1, (action, value) in self.click_regions:
            if y0 <= y <= y1:
                if action == "select":
                    self.mode, self.detail_key = "detail", value
                elif action == "back":
                    self.mode, self.detail_key = "list", None
                elif action == "death":          # value is the DeathRecord
                    self.death_sel = value
                elif action == "dback":
                    self.death_sel = None
                elif action == "color":          # value is a THEME key
                    self._pick_color(value)
                elif action == "colreset":
                    self._reset_colors()
                self.canvas.yview_moveto(0)
                self._redraw()
                return

    def _tick(self):
        catching_up = False
        try:
            lines, reset, path = self.tailer.poll()
            if reset:
                self.meter.reset_all()
            feed = self.meter.feed
            for line in lines:
                # Cheap pre-filter: skip aura/heal/energize spam (the bulk of
                # a log) before paying for csv + timestamp parsing.
                ev = quick_event(line)
                if ev is None or ev not in INTERESTING_EVENTS:
                    continue
                parsed = parse_line(line)
                if parsed:
                    feed(*parsed)
            catching_up = self.tailer.backlog() > 0
            if catching_up:
                self._show_progress(self.tailer.pos or 0, self.tailer.size)
            else:
                self._hide_progress()
                self._update_status(path, len(lines))
        except Exception as exc:
            self.lbl_status.config(text=f"error: {exc}")

        if catching_up:
            # Chew through the backlog in quick small bites; redraw only every
            # few chunks so drawing doesn't slow the load down.
            self._skip_draws = (self._skip_draws + 1) % 5
            if self._skip_draws == 0:
                self._redraw()
            self.root.after(CATCHUP_MS, self._tick)
        else:
            self._redraw()
            self.root.after(REFRESH_MS, self._tick)

    def _update_status(self, path, n_new):
        if not path:
            self.lbl_status.config(
                text="waiting for log - type /combatlog in game, then fight")
        else:
            who = self.meter.me_name or "auto-detecting..."
            live = "LIVE" if n_new else "idle"
            self.lbl_status.config(
                text=f"{live}  you={who}  {os.path.basename(path)}")

    # -- rendering ------------------------------------------------------------

    def _redraw(self):
        seg = self._segment()
        c = self.canvas
        c.delete("all")
        self.click_regions = []

        if self.tab == "settings":
            self.lbl_summary.config(text="Settings")
            self._draw_settings()
        elif self.tab == "deaths":
            self.lbl_summary.config(
                text=f"Deaths this session: {len(self.meter.deaths)}")
            if self.death_sel is not None:
                self._draw_death_detail()
            else:
                self._draw_death_list()
        elif self.tab == "fights":
            self._redraw_fights()
        else:
            summary = f"{commas(seg.total)}   ·   {abbrev(seg.dps())} DPS"
            if seg.total:
                crit_total = sum(r.c_total for r in seg.spells.values())
                summary += f"   ·   {crit_total / seg.total * 100:.0f}% crit"
            # Live fight timer while the current fight has data.
            if self.view == "current" and seg.duration() > 0:
                summary += f"   ·   {fmt_dur(seg.duration())}"
            self.lbl_summary.config(text=summary)
            if self.mode == "detail" and self.detail_key:
                self._draw_detail(seg)
            else:
                self._draw_list(seg)

        # Pin the scroll region's top to 0 and make it at least as tall as the
        # viewport, so short content stays put (no phantom scrolling) while long
        # content still scrolls.
        bbox = c.bbox("all")
        if bbox:
            view_h = c.winfo_height()
            c.configure(scrollregion=(0, 0, bbox[2], max(bbox[3], view_h)))
            if bbox[3] <= view_h:
                c.yview_moveto(0)
        else:
            c.configure(scrollregion=(0, 0, 0, 0))

        # The redraw wiped the hover outline; restore it from the last mouse pos.
        self._hover = None
        if self._mouse_y is not None:
            hit = self._region_at(self._mouse_y)
            self._set_hover((hit[0], hit[1]) if hit else None)

    def _draw_list(self, seg, y0=2):
        c = self.canvas
        rows = seg.sorted_spells()
        if not rows:
            c.create_text(12, y0 + 12, anchor="w", fill=SUBTEXT, font=self.small,
                          text="No damage yet. Ensure /combatlog is on, then fight.")
            return
        width = c.winfo_width() or 420
        top_total = rows[0].total
        row_h = 30
        y = y0
        for i, r in enumerate(rows[:MAX_ROWS]):
            frac = (r.total / top_total) if top_total else 0
            color = PET_COLOR if r.is_pet else SCHOOL_COLORS.get(r.school, DEFAULT_COLOR)
            pct = (r.total / seg.total * 100) if seg.total else 0

            c.create_rectangle(0, y, width, y + row_h - 4, fill=CARD, outline="")
            # Rank + name in light text (always readable, whatever the bar does).
            c.create_text(10, y + 9, anchor="w", fill=SUBTEXT, font=self.small,
                          text=str(i + 1))
            c.create_text(28, y + 9, anchor="w", fill=TEXT, font=self.body,
                          text=r.name + ("  (pet)" if r.is_pet else ""))
            # Right-aligned numbers: damage (mono) then share of total.
            c.create_text(width - 10, y + 9, anchor="e", fill=SUBTEXT,
                          font=self.small, text=f"{pct:.0f}%")
            c.create_text(width - 48, y + 9, anchor="e", fill=TEXT,
                          font=self.fixed, text=abbrev(r.total))
            # Slim bar: school color for non-crit damage, GOLD for the crit share.
            bar_w = max(2, (width - 20) * frac)
            crit_frac = (r.c_total / r.total) if r.total else 0
            split = 10 + bar_w * (1 - crit_frac)
            yb0, yb1 = y + row_h - 10, y + row_h - 6
            if split > 10:
                c.create_rectangle(10, yb0, split, yb1, fill=color, outline="")
            if crit_frac > 0:
                c.create_rectangle(split, yb0, 10 + bar_w, yb1,
                                   fill=CRIT_COLOR, outline="")
            self.click_regions.append((y, y + row_h - 4, ("select", r.key)))
            y += row_h

    def _draw_crumb(self, action, parent, here):
        """Full-width breadcrumb bar: '‹ parent / here'. Click = back.
        Returns the y where content starts."""
        c = self.canvas
        width = c.winfo_width() or 420
        c.create_rectangle(0, 0, width, 26, fill=PANEL, outline="")
        c.create_text(10, 13, anchor="w", fill=ACCENT, font=self.bold, text="‹")
        c.create_text(24, 13, anchor="w", fill=SUBTEXT, font=self.small,
                      text=parent)
        px = 24 + self.small.measure(parent)
        c.create_text(px + 6, 13, anchor="w", fill=SUBTEXT, font=self.small,
                      text="/")
        c.create_text(px + 16, 13, anchor="w", fill=TEXT, font=self.small,
                      text=here)
        self.click_regions.append((0, 26, (action, None)))
        return 40

    def _draw_detail(self, seg):
        c = self.canvas
        width = c.winfo_width() or 420
        r = seg.spells.get(self.detail_key)

        parent = "Fight" if self.tab == "fights" else "All spells"
        name = (r.name + ("  (pet)" if r.is_pet else "")) if r else "?"
        y = self._draw_crumb("back", parent, name)

        if r is None or r.total == 0:
            c.create_text(10, y + 4, anchor="w", fill=SUBTEXT, font=self.small,
                          text="No data for this spell in this view.")
            return

        c.create_text(8, y, anchor="w", fill=TEXT, font=self.big,
                      text=r.name + ("  (pet)" if r.is_pet else ""))
        y += 26

        casts = str(r.casts) if r.casts else "-"
        dps = abbrev(r.total / seg.active_time) if seg.active_time > 0.5 else "-"
        pct_of = (r.total / seg.total * 100) if seg.total else 0
        for text in (
            f"Casts: {casts}     Hits: {r.hits}     Crit: {r.crit_pct:.1f}%",
            f"Total: {commas(r.total)}   ({pct_of:.1f}% of you)     DPS: {dps}",
        ):
            c.create_text(8, y, anchor="w", fill=SUBTEXT, font=self.fixed, text=text)
            y += 16

        # Non-crit vs crit table (right-aligned number columns).
        y += 4
        col_n, col_c = width - 120, width - 20
        n_avg = (r.n_total / r.n_hits) if r.n_hits else 0
        c_avg = (r.c_total / r.c_hits) if r.c_hits else 0
        c.create_text(col_n, y, anchor="e", fill=SUBTEXT, font=self.fixed, text="Non-crit")
        c.create_text(col_c, y, anchor="e", fill=CRIT_COLOR, font=self.fixed, text="Crit")
        y += 16
        for label, nval, cval in (
            ("Hits", r.n_hits, r.c_hits),
            ("Damage", abbrev(r.n_total), abbrev(r.c_total)),
            ("Average", abbrev(n_avg), abbrev(c_avg)),
            ("Biggest", abbrev(r.n_max), abbrev(r.c_max)),
        ):
            c.create_text(8, y, anchor="w", fill=TEXT, font=self.fixed, text=label)
            c.create_text(col_n, y, anchor="e", fill=TEXT, font=self.fixed, text=str(nval))
            c.create_text(col_c, y, anchor="e", fill=CRIT_COLOR, font=self.fixed, text=str(cval))
            y += 16

        # Individual hits, newest first.
        y += 10
        total_hits = len(r.events)
        shown = min(total_hits, HIT_SHOW)
        c.create_text(8, y, anchor="w", fill=ACCENT, font=self.small,
                      text=f"Hits (newest first) - showing {shown} of {total_hits}")
        y += 18
        x_amt = 150
        for ts, amount, crit, target in list(r.events)[-HIT_SHOW:][::-1]:
            when = datetime.fromtimestamp(ts).strftime("%H:%M:%S")
            c.create_text(8, y, anchor="w", fill=SUBTEXT, font=self.fixed, text=when)
            c.create_text(x_amt, y, anchor="e", fill=CRIT_COLOR if crit else TEXT,
                          font=self.fixed, text=commas(amount))
            if crit:
                c.create_text(x_amt + 8, y, anchor="w", fill=CRIT_COLOR,
                              font=self.fixed, text="crit")
            if target:
                c.create_text(x_amt + 48, y, anchor="w", fill=SUBTEXT,
                              font=self.fixed, text=target[:24])
            y += 15

    # -- fights tab -----------------------------------------------------------

    def _rebuild_fight_menu(self):
        """Refill the dropdown when the fight history changes."""
        fights = list(self.meter.fights)
        sig = (len(fights),
               id(fights[0]) if fights else None,
               id(fights[-1]) if fights else None)
        if sig == self._fights_sig:
            return
        self._fights_sig = sig
        self._fight_items = []
        menu = self.fight_menu["menu"]
        menu.delete(0, "end")
        for f in reversed(fights):                 # newest first
            when = datetime.fromtimestamp(f.first_ts).strftime("%H:%M:%S")
            label = (f"{when}  {f.label()[:26]}  "
                     f"({fmt_dur(f.duration())}, {abbrev(f.total)})")
            self._fight_items.append((label, f))
            menu.add_command(label=label,
                             command=lambda seg=f: self._pick_fight(seg))

    def _pick_fight(self, seg):
        self.fight_sel = seg
        self.mode, self.detail_key = "list", None
        self.death_sel = None
        self.canvas.yview_moveto(0)
        self._redraw()

    def _redraw_fights(self):
        self._rebuild_fight_menu()
        fights = self.meter.fights
        if not fights:
            self.fight_var.set("(no fights yet)")
            self.lbl_summary.config(
                text="A fight is archived once you leave combat for 5s.")
            self.canvas.create_text(
                12, 14, anchor="w", fill=SUBTEXT, font=self.small,
                text="No finished fights yet. Fight something, then leave combat.")
            return

        # Default to the newest fight; heal a selection that was evicted or
        # wiped by a session reset.
        if self.fight_sel is None or self.fight_sel not in fights:
            self.fight_sel = fights[-1]
            self.mode, self.detail_key = "list", None
            self.death_sel = None
        f = self.fight_sel

        label = next((lbl for lbl, seg in self._fight_items if seg is f), "")
        if self.fight_var.get() != label:
            self.fight_var.set(label)
        self.lbl_summary.config(
            text=f"{f.label()}    {commas(f.total)}    "
                 f"{abbrev(f.dps())} DPS    {fmt_dur(f.duration())}")

        if self.death_sel is not None:
            self._draw_death_detail()      # its Back returns to the fight view
        elif self.mode == "detail" and self.detail_key:
            self._draw_detail(f)           # its Back returns to the fight view
        else:
            self._draw_fight_view(f)

    def _draw_fight_view(self, f):
        """One fight: header, deaths that happened during it, spell breakdown."""
        c = self.canvas
        width = c.winfo_width() or 420
        y = 12
        when = datetime.fromtimestamp(f.first_ts).strftime("%H:%M:%S")
        c.create_text(8, y, anchor="w", fill=TEXT, font=self.big,
                      text=f.label())
        c.create_text(width - 10, y, anchor="e", fill=SUBTEXT, font=self.small,
                      text=when)
        y += 22
        if f.label() != f.main_target():
            c.create_text(8, y, anchor="w", fill=SUBTEXT, font=self.body,
                          text=f"vs {f.main_target()}")
            y += 18

        deaths = self.meter.deaths_during(f)
        if deaths:
            y += 4
            c.create_text(8, y, anchor="w", fill=DANGER, font=self.small,
                          text=f"DEATHS ({len(deaths)})")
            y += 14
            row_h = 20
            for d in deaths:
                c.create_rectangle(0, y, width, y + row_h - 2,
                                   fill=CARD, outline="")
                c.create_rectangle(0, y, 3, y + row_h - 2, fill=DANGER, outline="")
                dt = datetime.fromtimestamp(d.ts).strftime("%H:%M:%S")
                blow = d.killing_blow()
                desc = f"{dt}  {d.name}"
                if blow:
                    desc += f"  -  {blow[3]} {abbrev(blow[1])}"
                c.create_text(12, y + (row_h - 2) / 2, anchor="w", fill=TEXT,
                              font=self.fixed, text=desc[:58])
                self.click_regions.append((y, y + row_h - 2, ("death", d)))
                y += row_h
        y += 10
        self._draw_list(f, y0=y)

    # -- deaths tab -----------------------------------------------------------

    def _draw_death_list(self):
        c = self.canvas
        deaths = list(self.meter.deaths)
        if not deaths:
            c.create_text(12, 14, anchor="w", fill=SUBTEXT, font=self.small,
                          text="No deaths recorded yet. Deaths of any player "
                               "in your group are captured live.")
            return
        width = c.winfo_width() or 420
        row_h = 34
        y = 2
        for d in reversed(deaths):                     # newest first
            c.create_rectangle(0, y, width, y + row_h - 2, fill=CARD, outline="")
            c.create_rectangle(0, y, 3, y + row_h - 2, fill=DANGER, outline="")
            when = datetime.fromtimestamp(d.ts).strftime("%H:%M:%S")
            c.create_text(12, y + 9, anchor="w", fill=TEXT, font=self.bold, text=d.name)
            c.create_text(width - 8, y + 9, anchor="e", fill=SUBTEXT, font=self.small, text=when)
            blow = d.killing_blow()
            if blow:
                _, amount, _ok, spell, src, _crit, _sch = blow
                who = src or "environment"
                sub = f"killed by {who}  —  {spell} {commas(amount)}"
            else:
                sub = "no incoming damage in the last %ds" % int(DEATH_WINDOW)
            c.create_text(12, y + 24, anchor="w", fill=SUBTEXT, font=self.fixed, text=sub[:60])
            # Click region carries the RECORD, not an index: the deque evicts
            # old deaths, which would silently shift indices under us.
            self.click_regions.append((y, y + row_h - 2, ("death", d)))
            y += row_h + 2

    def _draw_death_detail(self):
        c = self.canvas
        width = c.winfo_width() or 420
        d = self.death_sel
        if d is None or d not in self.meter.deaths:   # evicted or reset
            self.death_sel = None
            if self.tab == "fights" and self.fight_sel is not None:
                self._draw_fight_view(self.fight_sel)
            else:
                self._draw_death_list()
            return

        when = datetime.fromtimestamp(d.ts).strftime("%H:%M:%S")
        parent = "Fight" if self.tab == "fights" else "Deaths"
        y = self._draw_crumb("dback", parent, f"{d.name}  {when}")
        c.create_text(8, y, anchor="w", fill=TEXT, font=self.big,
                      text=f"{d.name} died")
        y += 26
        c.create_text(8, y, anchor="w", fill=SUBTEXT, font=self.fixed,
                      text=f"Damage taken in last {int(DEATH_WINDOW)}s: "
                           f"{commas(d.total_taken())}   ({len(d.events)} hits)")
        y += 20

        if not d.events:
            c.create_text(8, y, anchor="w", fill=SUBTEXT, font=self.small,
                          text="No incoming damage was recorded in the window.")
            return

        c.create_text(8, y, anchor="w", fill=ACCENT, font=self.small,
                      text=f"Timeline (last {int(DEATH_WINDOW)}s, fatal blow on top)")
        y += 18

        # Column geometry. Fixed (monospace) cell width lets us size text safely.
        cw = self.fixed.measure("0") or 7
        x_time = 8            # "-4.2s"  (left-anchored)
        x_amt = 130          # damage number (right-anchored)
        x_spell = 140        # "spell  source"  (left-anchored)
        row_h = 16
        fatal = d.killing_blow()

        # Column headers.
        c.create_text(x_time, y, anchor="w", fill=SUBTEXT, font=self.small, text="time")
        c.create_text(x_amt, y, anchor="e", fill=SUBTEXT, font=self.small, text="damage")
        c.create_text(x_spell, y, anchor="w", fill=SUBTEXT, font=self.small, text="source")
        y += 16

        for e in reversed(d.events):        # newest (fatal) first
            ts, amount, overkill, spell, src, crit, school = e
            dt = ts - d.ts
            color = SCHOOL_COLORS.get(school, DEFAULT_COLOR)
            is_fatal = e is fatal

            if is_fatal:                     # highlight the killing blow's row
                c.create_rectangle(4, y - row_h + 4, width - 4, y + 4,
                                   fill="#2a1a20", outline="")

            # Reserve space on the right for the overkill tag (fatal row only).
            right = width - 8
            ok_text = f"OVK {abbrev(overkill)}" if (is_fatal and overkill > 0) else None
            if ok_text:
                c.create_text(right, y, anchor="e", fill=DANGER,
                              font=self.fixed, text=ok_text)
                right -= self.fixed.measure(ok_text) + 10

            c.create_text(x_time, y, anchor="w", fill=SUBTEXT, font=self.fixed,
                          text=f"{dt:+.1f}s")
            c.create_text(x_amt, y, anchor="e", fill=CRIT_COLOR if crit else TEXT,
                          font=self.fixed, text=commas(amount))
            label = spell + (f"  {src}" if src else "")
            max_chars = max(4, int((right - x_spell) / cw))
            c.create_text(x_spell, y, anchor="w", fill=color, font=self.fixed,
                          text=label[:max_chars])
            y += row_h

    # -- settings tab ---------------------------------------------------------

    def _draw_settings(self):
        c = self.canvas
        width = c.winfo_width() or 420
        y = 14
        c.create_text(8, y, anchor="w", fill=ACCENT, font=self.small,
                      text="CHARACTER")
        y += 20
        c.create_text(8, y, anchor="w", fill=TEXT, font=self.body,
                      text="Your name")
        c.create_window(width - 10, y, anchor="e",
                        window=self._make_name_entry())
        y += 18
        c.create_text(8, y, anchor="w", fill=SUBTEXT, font=self.small,
                      text="Blank = auto-detect. Press Enter to apply "
                           "(re-reads the log).")
        y += 30

        c.create_text(8, y, anchor="w", fill=ACCENT, font=self.small,
                      text="COLORS")
        c.create_text(width - 10, y, anchor="e", fill=SUBTEXT, font=self.small,
                      text="click a row to change")
        y += 16
        row_h = 26
        for key, (label, _default) in THEME.items():
            cur = globals()[key]
            c.create_rectangle(0, y, width, y + row_h - 4, fill=CARD, outline="")
            c.create_rectangle(10, y + 4, 26, y + row_h - 8,
                               fill=cur, outline=TRACK)
            c.create_text(36, y + (row_h - 4) / 2, anchor="w", fill=TEXT,
                          font=self.body, text=label)
            c.create_text(width - 10, y + (row_h - 4) / 2, anchor="e",
                          fill=SUBTEXT, font=self.fixed, text=cur)
            self.click_regions.append((y, y + row_h - 4, ("color", key)))
            y += row_h

        y += 8
        c.create_rectangle(0, y, width, y + 24, fill=CARD, outline="")
        c.create_text(10, y + 11, anchor="w", fill=DANGER, font=self.small,
                      text="Reset colors to defaults")
        self.click_regions.append((y, y + 24, ("colreset", None)))

    def _pick_color(self, key):
        label = THEME[key][0]
        _rgb, hexval = colorchooser.askcolor(
            color=globals()[key], parent=self.root,
            title=f"TrackMe - {label}")
        if hexval:
            globals()[key] = hexval
            self._after_theme_change()

    def _reset_colors(self):
        apply_theme(default_colors())
        self._after_theme_change()

    def _after_theme_change(self):
        self._restyle_widgets()
        self._save_settings_now()     # persist immediately, not just on close
        self._redraw()

    def run(self):
        self.root.mainloop()


def main():
    settings = load_settings()
    apply_theme(settings.get("colors"))
    # CLI args win; then saved settings; then the built-in defaults.
    path = (sys.argv[1] if len(sys.argv) > 1
            else settings.get("log_path") or DEFAULT_LOG_PATH)
    name = (sys.argv[2] if len(sys.argv) > 2
            else settings.get("name") or None)
    TrackMeUI(path, name, settings).run()


if __name__ == "__main__":
    main()
