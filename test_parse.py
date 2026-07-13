"""Regression test for the Midnight (12.0) combat-log parser in trackme.py.

Builds lines in the real Midnight format observed in WoWCombatLog-*.txt:
advanced logging on (19 advanced fields), a 10-field damage suffix with the crit
flag at index -3, and a trailing spell tag ("ST"/"AOE") on spell-damage lines.
"""
import csv
import io

import trackme


def line(event, guid, name, flags, sid, sname, school, owner, amount, crit, tag=None):
    base = [event, guid, name, flags, "0x0", "Creature-9", "Dummy", "0xa28", "0x80000000"]
    prefix = [] if event == "SWING_DAMAGE" else [sid, sname, school]
    advanced = [guid, owner] + [str(i) for i in range(17)]  # 19 advanced fields
    suffix = [str(amount), str(amount // 2), "-1", "1", "0", "0", "0",
              "1" if crit else "nil", "nil", "nil"]
    fields = base + prefix + advanced + suffix + ([tag] if tag else [])
    buf = io.StringIO()
    csv.writer(buf).writerow(fields)
    return "7/13/2026 00:15:35.123-4  " + buf.getvalue().strip()


PLAYER, PET = "Player-1-AAAA", "Pet-1-BBBB"
PFLAGS, PETFLAGS = "0x548", "0x1111"   # note: no AFFILIATION_MINE bit

m = trackme.Meter()

# The player must dominate to be auto-detected as "you".
for _ in range(10):
    m.feed(*trackme.parse_line(
        line("SPELL_CAST_SUCCESS", PLAYER, "Me", PFLAGS, "133", "Fireball", "0x4", "0", 0, False)))

m.feed(*trackme.parse_line(line("SPELL_DAMAGE", PLAYER, "Me", PFLAGS, "133", "Fireball", "0x4", "0", 1500, True, "ST")))
m.feed(*trackme.parse_line(line("SPELL_DAMAGE", PLAYER, "Me", PFLAGS, "133", "Fireball", "0x4", "0", 1000, False, "AOE")))
m.feed(*trackme.parse_line(line("SWING_DAMAGE", PLAYER, "Me", PFLAGS, "", "", "", "0000000000000000", 500, False)))
m.feed(*trackme.parse_line(line("SPELL_DAMAGE", PET, "Imp", PETFLAGS, "999", "Firebolt", "0x4", PLAYER, 300, True, "ST")))
# A different player's hit must be excluded.
m.feed(*trackme.parse_line(line("SPELL_DAMAGE", "Player-2-ZZZZ", "Other", PFLAGS, "111", "Smite", "0x2", "0", 9999, False, "ST")))

seg = m.overall
fb = seg.spells["you:133"]
assert fb.total == 2500, fb.total
assert (fb.c_hits, fb.n_hits) == (1, 1)
assert (fb.c_max, fb.n_max) == (1500, 1000), (fb.c_max, fb.n_max)
assert fb.casts == 10, fb.casts
assert seg.spells["you:swing"].total == 500
assert seg.spells["pet:999"].is_pet and seg.spells["pet:999"].total == 300
assert "you:111" not in seg.spells                    # other player's spell
assert seg.total == 2500 + 500 + 300, seg.total       # other player excluded
assert m.me_name == "Me"
print("detected you =", m.me_name, m.me())
print("ALL ASSERTIONS PASSED, total =", seg.total)
