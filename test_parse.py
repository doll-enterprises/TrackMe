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


# --------------------------------------------------------------------------
# Death tracking: incoming damage to a player + UNIT_DIED snapshot.
# --------------------------------------------------------------------------

def incoming(secs, event, src_name, dst_guid, dst_name, sname, amount,
             overkill=0, crit=False, tag=None):
    """A line where the DESTINATION is a player (dst_flags has TYPE_PLAYER)."""
    base = [event, "Creature-9", src_name, "0x10a28", "0x0",
            dst_guid, dst_name, "0x511", "0x80000000"]   # 0x511 => player dest
    prefix = [] if event == "SWING_DAMAGE" else ["100", sname, "0x1"]
    advanced = [dst_guid] + [str(i) for i in range(18)]
    suffix = [str(amount), str(overkill), "1", "-1", "0", "0", "0",
              "1" if crit else "nil", "nil", "nil"]
    fields = base + prefix + advanced + suffix + ([tag] if tag else [])
    buf = io.StringIO()
    csv.writer(buf).writerow(fields)
    return f"7/13/2026 00:15:{secs:06.3f}-4  " + buf.getvalue().strip()


def unit_died(secs, guid, name):
    fields = ["UNIT_DIED", "0000000000000000", "nil", "0x80000000", "0x80000000",
              guid, name, "0x511", "0x80000000", "0"]
    buf = io.StringIO()
    csv.writer(buf).writerow(fields)
    return f"7/13/2026 00:15:{secs:06.3f}-4  " + buf.getvalue().strip()


VG, VNAME = "Player-9-DEAD", "Victim-Realm-US"

md = trackme.Meter()
# Old hit outside the 5s window (should be excluded from the snapshot).
md.feed(*trackme.parse_line(incoming(30.000, "SPELL_DAMAGE", "Boss", VG, VNAME, "Cleave", 1000)))
# Hits inside the window.
md.feed(*trackme.parse_line(incoming(38.000, "SWING_DAMAGE", "Boss", VG, VNAME, "", 5000)))
md.feed(*trackme.parse_line(incoming(39.500, "SPELL_DAMAGE", "Boss", VG, VNAME, "Pyroblast", 8000, crit=True, tag="ST")))
md.feed(*trackme.parse_line(incoming(40.000, "SPELL_DAMAGE", "Boss", VG, VNAME, "Execute", 20000, overkill=4000, tag="ST")))
# Damage to a different player must not appear in this victim's snapshot.
md.feed(*trackme.parse_line(incoming(39.900, "SPELL_DAMAGE", "Boss", "Player-9-ALIVE", "Other", "Fireball", 700)))
md.feed(*trackme.parse_line(unit_died(40.010, VG, VNAME)))

assert len(md.deaths) == 1, len(md.deaths)
d = md.deaths[0]
assert d.name == VNAME, d.name
assert len(d.events) == 3, [e[3] for e in d.events]        # old 1000-hit excluded
assert d.total_taken() == 5000 + 8000 + 20000, d.total_taken()
blow = d.killing_blow()
assert blow[3] == "Execute" and blow[2] == 4000, blow      # fatal = overkill hit
assert [e[3] for e in d.events] == ["Melee", "Pyroblast", "Execute"]  # chronological
print("death snapshot:", d.name, [f"{e[3]}={e[1]}" for e in d.events])


# --------------------------------------------------------------------------
# Self-detection: the AFFILIATION_MINE (0x1) flag beats the most-active
# fallback. WoW sets 0x1 only on the logging character, so it is definitive.
# --------------------------------------------------------------------------

mm = trackme.Meter()
YOU, DECOY = "Player-7-MINE", "Player-7-DECOY"
# A decoy player is far more active but lacks the MINE bit (0x548 has no 0x1).
for _ in range(20):
    mm.feed(*trackme.parse_line(
        line("SPELL_DAMAGE", DECOY, "Decoy", "0x548", "1", "Bolt", "0x20", "0", 100, False, "ST")))
# You appear once, flagged MINE (0x511 includes the 0x1 bit).
mm.feed(*trackme.parse_line(
    line("SPELL_DAMAGE", YOU, "Fordtruck", "0x511", "2", "Strike", "0x1", "0", 50, False, "ST")))
assert mm.mine_guid == YOU, mm.mine_guid
assert mm.me() == YOU, mm.me()            # MINE wins over the busier decoy
assert mm.me_name == "Fordtruck", mm.me_name
print("self-detect via MINE bit:", mm.me(), mm.me_name)


# --------------------------------------------------------------------------
# Fight history: a >5s gap (or ENCOUNTER_START) archives the finished fight
# into meter.fights instead of discarding it.
# --------------------------------------------------------------------------

def at(secs, evline):
    """Rewrite the timestamp seconds of a generated line."""
    return evline.replace("00:15:35.123", f"00:15:{secs:06.3f}")


mf = trackme.Meter()
Y = "Player-8-YOU"
# Fight 1: two hits on Dummy A, 1s apart.
mf.feed(*trackme.parse_line(at(10.0, line("SPELL_DAMAGE", Y, "Me", "0x511", "133", "Fireball", "0x4", "0", 1000, False, "ST"))))
mf.feed(*trackme.parse_line(at(11.0, line("SPELL_DAMAGE", Y, "Me", "0x511", "133", "Fireball", "0x4", "0", 2000, True, "ST"))))
# >5s gap -> fight 1 archived, fight 2 begins.
mf.feed(*trackme.parse_line(at(20.0, line("SPELL_DAMAGE", Y, "Me", "0x511", "116", "Frostbolt", "0x10", "0", 500, False, "ST"))))

assert len(mf.fights) == 1, len(mf.fights)
f1 = mf.fights[0]
assert f1.total == 3000, f1.total
assert f1.main_target() == "Dummy", f1.main_target()
assert abs(f1.duration() - 1.0) < 0.01, f1.duration()
assert mf.current.total == 500                      # fight 2 is the new current
# The archived fight must be a DIFFERENT object than current (no aliasing).
assert f1 is not mf.current
# ENCOUNTER_START also archives (it has NO unit flags, so it must be handled
# before the player/pet gate in feed()).
mf.feed(*trackme.parse_line(
    '7/13/2026 00:15:21.000-4  ENCOUNTER_START,2902,"Big Boss",8,5,2549'))
assert len(mf.fights) == 2 and mf.fights[1].total == 500, len(mf.fights)
assert mf.current.total == 0
assert mf.current.encounter == "Big Boss"           # boss name stamped early
print("fight history:", [(f.main_target(), f.total) for f in mf.fights])


# --------------------------------------------------------------------------
# Fight labels (zone > player) and deaths_during association.
# --------------------------------------------------------------------------

mz = trackme.Meter()
mz.feed(*trackme.parse_line(
    '7/13/2026 00:15:05.000-4  ZONE_CHANGE,1911,"Mugambala",0'))
# Fight in Mugambala: 2 hits, and a player dies mid-fight.
mz.feed(*trackme.parse_line(at(10.0, line("SPELL_DAMAGE", Y, "Me", "0x511", "133", "Fireball", "0x4", "0", 1000, False, "ST"))))
mz.feed(*trackme.parse_line(incoming(11.000, "SPELL_DAMAGE", "Boss", VG, VNAME, "Smash", 9000, overkill=100, tag="ST")))
mz.feed(*trackme.parse_line(unit_died(11.010, VG, VNAME)))
mz.feed(*trackme.parse_line(at(12.0, line("SPELL_DAMAGE", Y, "Me", "0x511", "133", "Fireball", "0x4", "0", 1000, False, "ST"))))
# Gap -> archive. Second fight elsewhere with no deaths.
mz.feed(*trackme.parse_line(
    '7/13/2026 00:15:20.000-4  ZONE_CHANGE,0,"Silvermoon City",0'))
mz.feed(*trackme.parse_line(at(25.0, line("SPELL_DAMAGE", Y, "Me", "0x511", "116", "Frostbolt", "0x10", "0", 700, False, "ST"))))

fz = mz.fights[0]
assert fz.zone == "Mugambala" and fz.label() == "Mugambala", (fz.zone, fz.label())
assert mz.current.zone == "Silvermoon City", mz.current.zone
d_in = mz.deaths_during(fz)
assert len(d_in) == 1 and d_in[0].name == VNAME, d_in
assert mz.deaths_during(mz.current) == []           # no deaths in fight 2
# Encounter label beats zone label.
fz.encounter = "Raszageth"
assert fz.label() == "Raszageth"
print("zone label + deaths_during:", fz.zone, [d.name for d in d_in])
print("ALL ASSERTIONS PASSED, total =", seg.total)
