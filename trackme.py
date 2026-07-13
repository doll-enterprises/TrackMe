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
from collections import Counter
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

# Combat-log object-flag bits.
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
                 "c_hits", "c_total", "c_max", "total")

    def __init__(self, key, name, spell_id, is_pet, school):
        self.key, self.name, self.spell_id = key, name, spell_id
        self.is_pet, self.school = is_pet, school
        self.casts = 0
        self.n_hits = self.n_total = self.n_max = 0
        self.c_hits = self.c_total = self.c_max = 0
        self.total = 0

    def add_damage(self, amount, crit):
        if crit:
            self.c_hits += 1
            self.c_total += amount
            self.c_max = max(self.c_max, amount)
        else:
            self.n_hits += 1
            self.n_total += amount
            self.n_max = max(self.n_max, amount)
        self.total += amount

    @property
    def hits(self):
        return self.n_hits + self.c_hits

    @property
    def crit_pct(self):
        return (self.c_hits / self.hits * 100) if self.hits else 0.0


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

    def record(self, key, name, spell_id, is_pet, school, amount, crit):
        self._rec(key, name, spell_id, is_pet, school).add_damage(amount, crit)
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
        self.me_name = None

    def reset_all(self):
        self.current.reset()
        self.overall.reset()

    def me(self):
        """The GUID we attribute damage to (auto = most active player)."""
        if self.forced_guid:
            return self.forced_guid
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

        if self.forced_name and (flags & TYPE_PLAYER) and self.forced_name in src_name.lower():
            self.forced_guid = src_guid

        # Which player does this event belong to?
        if flags & TYPE_PLAYER:
            self.player_counts[src_guid] += 1
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
            self.current.record(key, spell_name, spell_id, is_pet, school, amount, crit)
            self.overall.record(key, spell_name, spell_id, is_pet, school, amount, crit)

        elif event == "SWING_DAMAGE":
            amount = to_int(fields[-10])
            crit = fields[-3] == "1"
            if amount <= 0:
                return
            key = "pet:swing" if is_pet else "you:swing"
            name = "Melee (Pet)" if is_pet else "Melee"
            self._advance(ts)
            self.current.record(key, name, None, is_pet, 1, amount, crit)
            self.overall.record(key, name, None, is_pet, 1, amount, crit)

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


class TrackMeUI:
    def __init__(self, log_path, forced_name=None):
        self.meter = Meter(forced_name)
        self.tailer = Tailer(log_path)
        self.view = "current"
        self.detail_key = None
        self.row_hits = []

        self.root = tk.Tk()
        self.root.title("TrackMe - live combat-log breakdown")
        self.root.configure(bg=BG)
        self.root.geometry("400x520")
        self.root.minsize(340, 320)

        self.fixed = tkfont.Font(family="Consolas", size=9)
        self.bold = tkfont.Font(family="Segoe UI", size=10, weight="bold")
        self.small = tkfont.Font(family="Segoe UI", size=8)

        self._build_header()
        if forced_name:
            self.name_var.set(forced_name)
        self.canvas = tk.Canvas(self.root, bg=BG, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, padx=6, pady=2)
        self.canvas.bind("<Button-1>", self._on_click)
        self.lbl_status = tk.Label(self.root, text="", fg=SUBTEXT, bg=PANEL,
                                   anchor="w", font=self.small)
        self.lbl_status.pack(fill="x", side="bottom")

        self.detail = None
        self.root.after(200, self._tick)

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

        self.lbl_summary = tk.Label(self.root, text="", fg=TEXT, bg=BG,
                                    anchor="w", font=self.small)
        self.lbl_summary.pack(fill="x", padx=8, pady=(2, 2))

    def _apply_name(self, *_):
        """Type a character name (blank = auto-detect) and re-scan the log for it."""
        name = self.name_var.get().strip()
        if name.lower() == (self.meter.forced_name or ""):
            return
        self.meter = Meter(name or None)
        # Force a full re-read of the newest log so the change applies to
        # everything already logged, not just future events.
        self.tailer.current_file = None
        self.tailer.pos = None
        self.tailer.buffer = ""
        self.detail_key = None
        self._redraw()

    def _segment(self):
        return self.meter.overall if self.view == "overall" else self.meter.current

    def _toggle_view(self):
        self.view = "overall" if self.view == "current" else "current"
        self.btn_view.config(text="Current" if self.view == "overall" else "Overall")
        self._redraw()

    def _reset(self):
        self.meter.reset_all()
        self.detail_key = None
        if self.detail:
            self.detail.destroy()
            self.detail = None
        self._redraw()

    def _on_click(self, event):
        for y0, y1, key in self.row_hits:
            if y0 <= event.y <= y1:
                self._open_detail(key)
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

    def _redraw(self):
        seg = self._segment()
        self.lbl_summary.config(
            text=f"{'Overall' if self.view == 'overall' else 'Current fight'}"
                 f"    {commas(seg.total)}    {abbrev(seg.dps())} DPS")

        c = self.canvas
        c.delete("all")
        self.row_hits = []
        rows = seg.sorted_spells()
        if not rows:
            c.create_text(12, 14, anchor="w", fill=SUBTEXT, font=self.small,
                          text="No damage yet. Ensure /combatlog is on, then fight.")
            self._refresh_detail()
            return

        width = c.winfo_width() or 388
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
            self.row_hits.append((y, y + row_h - 2, r.key))
            y += row_h
        self._refresh_detail()

    def _open_detail(self, key):
        self.detail_key = key
        if self.detail is None or not self.detail.winfo_exists():
            self.detail = tk.Toplevel(self.root, bg=BG)
            self.detail.title("Spell detail")
            self.detail.geometry("340x300")
            self.detail.configure(bg=BG)
            self.detail_text = tk.Label(self.detail, bg=BG, fg=TEXT, justify="left",
                                        anchor="nw", font=self.fixed)
            self.detail_text.pack(fill="both", expand=True, padx=10, pady=10)
        self.detail.deiconify()
        self.detail.lift()
        self._refresh_detail()

    def _refresh_detail(self):
        if self.detail is None or not self.detail.winfo_exists() or not self.detail_key:
            return
        seg = self._segment()
        r = seg.spells.get(self.detail_key)
        if r is None or r.total == 0:
            self.detail_text.config(text="No data for this spell in the current view.")
            return
        n_avg = (r.n_total / r.n_hits) if r.n_hits else 0
        c_avg = (r.c_total / r.c_hits) if r.c_hits else 0
        pct_of = (r.total / seg.total * 100) if seg.total else 0
        casts = str(r.casts) if r.casts else "-"
        dps = abbrev(r.total / seg.active_time) if seg.active_time > 0.5 else "-"
        lines = [
            f"{r.name}" + ("  (pet)" if r.is_pet else ""),
            "-" * 40,
            f"Casts:      {casts:<10} Hits: {r.hits}",
            f"Crit rate:  {r.crit_pct:.1f}%   ({r.c_hits} crit / {r.n_hits} normal)",
            f"Total:      {commas(r.total)}   ({pct_of:.1f}% of you)",
            f"DPS:        {dps}",
            "",
            f"{'':10}{'Non-crit':>12}{'Crit':>12}",
            f"{'Hits':10}{r.n_hits:>12}{r.c_hits:>12}",
            f"{'Damage':10}{abbrev(r.n_total):>12}{abbrev(r.c_total):>12}",
            f"{'Average':10}{abbrev(n_avg):>12}{abbrev(c_avg):>12}",
            f"{'Biggest':10}{abbrev(r.n_max):>12}{abbrev(r.c_max):>12}",
        ]
        self.detail_text.config(text="\n".join(lines))

    def run(self):
        self.root.mainloop()


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_LOG_PATH
    name = sys.argv[2] if len(sys.argv) > 2 else None
    TrackMeUI(path, name).run()


if __name__ == "__main__":
    main()
