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
import os
import re
import sys
import time
from collections import Counter, deque
from datetime import datetime

import tkinter as tk
from tkinter import font as tkfont

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

# A folder (we follow the newest WoWCombatLog-*.txt in it) or a specific file.
DEFAULT_LOG_PATH = (
    r"E:\Blizzard\World of Warcraft\_retail_\World of Warcraft\_retail_\Logs"
)

REFRESH_MS = 1000     # how often we poll the log file (WoW flushes in bursts)
COMBAT_GAP = 5.0      # seconds of no activity that ends the "current" fight
MAX_ROWS = 22         # spell rows drawn in the main list
HIT_STORE_CAP = 2000  # individual hits kept per spell (bounds memory)
HIT_SHOW = 400        # individual hits drawn in the detail view

# Deaths tab: on every player death we snapshot the damage they took in the
# few seconds beforehand so you can see what killed them.
DEATH_WINDOW = 5.0    # seconds of incoming damage kept before a death
INCOMING_CAP = 64     # incoming hits buffered per player (bounds memory)
DEATH_KEEP = 50       # death records retained for the Deaths tab

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

SCHOOL_COLORS = {
    1:  "#e6cc80",  # Physical
    2:  "#fff2b3",  # Holy
    4:  "#ff8033",  # Fire
    8:  "#4dff4d",  # Nature
    16: "#80e5ff",  # Frost
    32: "#8c66ff",  # Shadow
    64: "#e680ff",  # Arcane
}
DEFAULT_COLOR = "#8f99e6"
PET_COLOR     = "#59d1d9"

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


def parse_timestamp(ts):
    core = re.sub(r"[-+]\d+$", "", ts).strip()
    try:
        return datetime.strptime(core, "%m/%d/%Y %H:%M:%S.%f").timestamp()
    except ValueError:
        return None


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
        # Death tracking (for ALL players, not just "you").
        self.incoming = {}               # player-GUID -> deque of recent hits taken
        self.deaths = deque(maxlen=DEATH_KEEP)

    def reset_all(self):
        self.current.reset()
        self.overall.reset()
        self.incoming.clear()
        self.deaths.clear()

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
        if event == "UNIT_DIED":
            self._note_death(ts, fields)
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

        elif event == "ENCOUNTER_START":
            self.current.reset()

    def _advance(self, ts):
        if self.current.last_ts is not None and (ts - self.current.last_ts) > COMBAT_GAP:
            self.current.reset()
        self.current.note_time(ts)
        self.overall.note_time(ts)

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

        if self.pos is None:
            self.pos = 0                      # read the current file from start
        if size < self.pos:                   # file truncated -> new session
            self.pos = 0
            self.buffer = ""
            reset = True
        if size == self.pos:
            return [], reset, target

        with open(target, "r", encoding="utf-8", errors="replace") as f:
            f.seek(self.pos)
            data = f.read()
            self.pos = f.tell()

        self.buffer += data
        lines = self.buffer.split("\n")
        self.buffer = lines.pop()             # keep trailing partial line
        return lines, reset, target

# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------

BG      = "#14141a"
PANEL   = "#1d1d26"
TRACK   = "#26262f"
TEXT    = "#e8e8ee"
SUBTEXT = "#9aa0ad"
ACCENT  = "#66ccff"


def abbrev(n):
    n = n or 0
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 10_000:
        return f"{n / 1000:.1f}k"
    return str(int(round(n)))


def commas(n):
    return f"{int(round(n or 0)):,}"


CRIT_COLOR = "#ffcc66"


class TrackMeUI:
    def __init__(self, log_path, forced_name=None):
        self.meter = Meter(forced_name)
        self.tailer = Tailer(log_path)
        self.tab = "damage"         # "damage" | "deaths"
        self.view = "current"
        self.mode = "list"          # "list" | "detail"
        self.detail_key = None
        self.death_sel = None       # index into meter.deaths when viewing one
        self.click_regions = []     # (y0, y1, (action, value)) in canvas coords

        self.root = tk.Tk()
        self.root.title("TrackMe - live combat-log breakdown")
        self.root.configure(bg=BG)
        self.root.geometry("440x600")
        self.root.minsize(360, 340)

        self.fixed = tkfont.Font(family="Consolas", size=9)
        self.bold = tkfont.Font(family="Segoe UI", size=10, weight="bold")
        self.small = tkfont.Font(family="Segoe UI", size=8)

        self._build_header()
        if forced_name:
            self.name_var.set(forced_name)

        self.canvas = tk.Canvas(self.root, bg=BG, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, padx=6, pady=2)
        self.canvas.bind("<Button-1>", self._on_click)
        self.canvas.bind("<Configure>", lambda e: self._redraw())
        self.canvas.bind("<MouseWheel>", self._on_wheel)

        self.lbl_status = tk.Label(self.root, text="", fg=SUBTEXT, bg=PANEL,
                                   anchor="w", font=self.small)
        self.lbl_status.pack(fill="x", side="bottom")

        self.root.after(200, self._tick)

    # -- header ---------------------------------------------------------------

    def _build_header(self):
        top = tk.Frame(self.root, bg=PANEL)
        top.pack(fill="x", side="top")
        tk.Label(top, text="TrackMe", fg=ACCENT, bg=PANEL,
                 font=self.bold).pack(side="left", padx=8, pady=6)
        self.btn_view = tk.Button(top, text="Overall", width=8, relief="flat",
                                  bg=TRACK, fg=TEXT, activebackground=ACCENT,
                                  command=self._toggle_view)
        self.btn_view.pack(side="right", padx=6, pady=6)
        tk.Button(top, text="Reset", width=6, relief="flat", bg=TRACK, fg=TEXT,
                  activebackground="#a44", command=self._reset).pack(side="right", pady=6)

        namebar = tk.Frame(self.root, bg=BG)
        namebar.pack(fill="x", padx=8, pady=(4, 0))
        tk.Label(namebar, text="You:", fg=SUBTEXT, bg=BG,
                 font=self.small).pack(side="left")
        self.name_var = tk.StringVar()
        entry = tk.Entry(namebar, textvariable=self.name_var, bg=TRACK, fg=TEXT,
                         insertbackground=TEXT, relief="flat", font=self.small, width=16)
        entry.pack(side="left", padx=4)
        entry.bind("<Return>", self._apply_name)
        entry.bind("<FocusOut>", self._apply_name)
        tk.Label(namebar, text="(blank = auto)", fg=SUBTEXT, bg=BG,
                 font=self.small).pack(side="left", padx=2)

        tabbar = tk.Frame(self.root, bg=BG)
        tabbar.pack(fill="x", padx=8, pady=(4, 0))
        self.btn_dmg = tk.Button(tabbar, text="Damage", width=8, relief="flat",
                                 bg=ACCENT, fg="#101014",
                                 command=lambda: self._set_tab("damage"))
        self.btn_dmg.pack(side="left", padx=(0, 4))
        self.btn_deaths = tk.Button(tabbar, text="Deaths", width=8, relief="flat",
                                    bg=TRACK, fg=TEXT,
                                    command=lambda: self._set_tab("deaths"))
        self.btn_deaths.pack(side="left")

        self.lbl_summary = tk.Label(self.root, text="", fg=TEXT, bg=BG,
                                    anchor="w", font=self.small)
        self.lbl_summary.pack(fill="x", padx=8, pady=(2, 2))

    def _set_tab(self, tab):
        self.tab = tab
        self.mode, self.detail_key = "list", None
        self.death_sel = None
        on, off = (ACCENT, "#101014"), (TRACK, TEXT)
        self.btn_dmg.config(bg=(on if tab == "damage" else off)[0],
                            fg=(on if tab == "damage" else off)[1])
        self.btn_deaths.config(bg=(on if tab == "deaths" else off)[0],
                               fg=(on if tab == "deaths" else off)[1])
        self.canvas.yview_moveto(0)
        self._redraw()

    def _apply_name(self, *_):
        name = self.name_var.get().strip()
        if name.lower() == (self.meter.forced_name or ""):
            return
        self.meter = Meter(name or None)
        self.tailer.current_file = None   # force a full re-read for the new name
        self.tailer.pos = None
        self.tailer.buffer = ""
        self.mode, self.detail_key = "list", None
        self._redraw()

    # -- navigation / events --------------------------------------------------

    def _segment(self):
        return self.meter.overall if self.view == "overall" else self.meter.current

    def _toggle_view(self):
        self.view = "overall" if self.view == "current" else "current"
        self.btn_view.config(text="Current" if self.view == "overall" else "Overall")
        self._redraw()

    def _reset(self):
        self.meter.reset_all()
        self.mode, self.detail_key = "list", None
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

    def _on_click(self, event):
        y = self.canvas.canvasy(event.y)
        for y0, y1, (action, value) in self.click_regions:
            if y0 <= y <= y1:
                if action == "select":
                    self.mode, self.detail_key = "detail", value
                elif action == "back":
                    self.mode, self.detail_key = "list", None
                elif action == "death":
                    self.death_sel = value
                elif action == "dback":
                    self.death_sel = None
                self.canvas.yview_moveto(0)
                self._redraw()
                return

    def _tick(self):
        try:
            lines, reset, path = self.tailer.poll()
            if reset:
                self.meter.reset_all()
            for line in lines:
                parsed = parse_line(line)
                if parsed:
                    self.meter.feed(*parsed)
            self._update_status(path, len(lines))
        except Exception as exc:
            self.lbl_status.config(text=f"error: {exc}")
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

        if self.tab == "deaths":
            self.lbl_summary.config(
                text=f"Deaths this session: {len(self.meter.deaths)}")
            if self.death_sel is not None:
                self._draw_death_detail()
            else:
                self._draw_death_list()
        else:
            self.lbl_summary.config(
                text=f"{'Overall' if self.view == 'overall' else 'Current fight'}"
                     f"    {commas(seg.total)}    {abbrev(seg.dps())} DPS")
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

    def _draw_list(self, seg):
        c = self.canvas
        rows = seg.sorted_spells()
        if not rows:
            c.create_text(12, 14, anchor="w", fill=SUBTEXT, font=self.small,
                          text="No damage yet. Ensure /combatlog is on, then fight.")
            return
        width = c.winfo_width() or 420
        top_total = rows[0].total
        row_h = 22
        y = 2
        for i, r in enumerate(rows[:MAX_ROWS]):
            frac = (r.total / top_total) if top_total else 0
            color = PET_COLOR if r.is_pet else SCHOOL_COLORS.get(r.school, DEFAULT_COLOR)
            c.create_rectangle(0, y, width, y + row_h - 2, fill=TRACK, outline="")
            c.create_rectangle(0, y, max(2, width * frac), y + row_h - 2, fill=color, outline="")
            pct = (r.total / seg.total * 100) if seg.total else 0
            name = f"{i + 1}. {r.name}" + ("  (pet)" if r.is_pet else "")
            c.create_text(6, y + (row_h - 2) / 2, anchor="w", fill="#101014",
                          font=self.fixed, text=name)
            c.create_text(width - 6, y + (row_h - 2) / 2, anchor="e", fill="#101014",
                          font=self.fixed, text=f"{abbrev(r.total)}  {pct:.0f}%")
            self.click_regions.append((y, y + row_h - 2, ("select", r.key)))
            y += row_h

    def _draw_detail(self, seg):
        c = self.canvas
        width = c.winfo_width() or 420
        r = seg.spells.get(self.detail_key)

        # Back button (always available).
        c.create_rectangle(6, 6, 70, 26, fill=TRACK, outline="")
        c.create_text(14, 16, anchor="w", fill=ACCENT, font=self.small, text="← Back")
        self.click_regions.append((6, 26, ("back", None)))

        if r is None or r.total == 0:
            c.create_text(10, 40, anchor="w", fill=SUBTEXT, font=self.small,
                          text="No data for this spell in this view.")
            return

        y = 36
        c.create_text(8, y, anchor="w", fill=TEXT, font=self.bold,
                      text=r.name + ("  (pet)" if r.is_pet else ""))
        y += 22

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
        for idx in range(len(deaths) - 1, -1, -1):     # newest first
            d = deaths[idx]
            c.create_rectangle(0, y, width, y + row_h - 2, fill=PANEL, outline="")
            when = datetime.fromtimestamp(d.ts).strftime("%H:%M:%S")
            c.create_text(8, y + 9, anchor="w", fill=TEXT, font=self.bold, text=d.name)
            c.create_text(width - 8, y + 9, anchor="e", fill=SUBTEXT, font=self.small, text=when)
            blow = d.killing_blow()
            if blow:
                _, amount, _ok, spell, src, _crit, _sch = blow
                who = src or "environment"
                sub = f"killed by {who}  —  {spell} {commas(amount)}"
            else:
                sub = "no incoming damage in the last %ds" % int(DEATH_WINDOW)
            c.create_text(8, y + 24, anchor="w", fill=SUBTEXT, font=self.fixed, text=sub[:60])
            self.click_regions.append((y, y + row_h - 2, ("death", idx)))
            y += row_h

    def _draw_death_detail(self):
        c = self.canvas
        width = c.winfo_width() or 420
        deaths = list(self.meter.deaths)
        if self.death_sel is None or self.death_sel >= len(deaths):
            self.death_sel = None
            self._draw_death_list()
            return
        d = deaths[self.death_sel]

        # Back button.
        c.create_rectangle(6, 6, 70, 26, fill=TRACK, outline="")
        c.create_text(14, 16, anchor="w", fill=ACCENT, font=self.small, text="← Back")
        self.click_regions.append((6, 26, ("dback", None)))

        y = 36
        when = datetime.fromtimestamp(d.ts).strftime("%H:%M:%S")
        c.create_text(8, y, anchor="w", fill=TEXT, font=self.bold,
                      text=f"{d.name} died  {when}")
        y += 22
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
                                   fill=PANEL, outline="")

            # Reserve space on the right for the overkill tag (fatal row only).
            right = width - 8
            ok_text = f"OVK {abbrev(overkill)}" if (is_fatal and overkill > 0) else None
            if ok_text:
                c.create_text(right, y, anchor="e", fill="#ff6b6b",
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

    def run(self):
        self.root.mainloop()


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_LOG_PATH
    name = sys.argv[2] if len(sys.argv) > 2 else None
    TrackMeUI(path, name).run()


if __name__ == "__main__":
    main()
