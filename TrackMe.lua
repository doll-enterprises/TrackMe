-- TrackMe: a lightweight personal damage breakdown.
--
-- Data source: the COMBAT_LOG_EVENT_UNFILTERED event read through
-- CombatLogGetCurrentEventInfo(). That is the structured, localization-proof
-- feed behind the "Combat Log" chat tab -- we never parse chat text.
--
-- Two views:
--   Current  -- resets every time you enter combat (your per-combat phase).
--   Overall  -- accumulates across fights until you press Clear.
-- Pet/guardian damage is attributed to you but tagged "(Pet)" everywhere.

local ADDON_NAME = ...

------------------------------------------------------------------------------
-- Small helpers
------------------------------------------------------------------------------

local band = bit.band
local bor  = bit.bor

-- Fall back to the literal bit values if Midnight removed these globals, so a
-- nil constant can never break loading or the band() checks below.
local AFFILIATION_MINE = COMBATLOG_OBJECT_AFFILIATION_MINE or 0x00000001
local TYPE_PET         = COMBATLOG_OBJECT_TYPE_PET         or 0x00001000
local TYPE_GUARDIAN    = COMBATLOG_OBJECT_TYPE_GUARDIAN    or 0x00002000
local PET_MASK         = bor(TYPE_PET, TYPE_GUARDIAN)

-- Colour damage bars by spell school (bitmask; we colour by the dominant one).
local SCHOOL_COLORS = {
    [1]  = { 1.00, 0.90, 0.55 }, -- Physical
    [2]  = { 1.00, 0.95, 0.70 }, -- Holy
    [4]  = { 1.00, 0.50, 0.20 }, -- Fire
    [8]  = { 0.35, 0.95, 0.35 }, -- Nature
    [16] = { 0.55, 0.90, 1.00 }, -- Frost
    [32] = { 0.55, 0.35, 0.95 }, -- Shadow
    [64] = { 0.90, 0.55, 1.00 }, -- Arcane
}
local DEFAULT_COLOR = { 0.55, 0.60, 0.90 }
local PET_TINT      = { 0.35, 0.80, 0.85 } -- fallback tint for pet rows

local MELEE_ICON = "Interface\\Icons\\INV_Sword_04"

local function SchoolColor(school)
    return SCHOOL_COLORS[school] or DEFAULT_COLOR
end

local function SpellIcon(spellId)
    if not spellId then return MELEE_ICON end
    if C_Spell and C_Spell.GetSpellTexture then
        return C_Spell.GetSpellTexture(spellId) or MELEE_ICON
    end
    return (GetSpellTexture and GetSpellTexture(spellId)) or MELEE_ICON
end

-- 12345 -> "12.3k", 1234567 -> "1.23M"
local function Abbrev(n)
    n = n or 0
    if n >= 1e6 then
        return string.format("%.2fM", n / 1e6)
    elseif n >= 1e4 then
        return string.format("%.1fk", n / 1e3)
    end
    return tostring(math.floor(n + 0.5))
end

local function Commas(n)
    local s = tostring(math.floor((n or 0) + 0.5))
    return (s:reverse():gsub("(%d%d%d)", "%1,"):reverse():gsub("^,", ""))
end

------------------------------------------------------------------------------
-- Data model
------------------------------------------------------------------------------

local playerGUID

-- A spell record aggregates every damage event under one bar.
--   key         unique id ("s"..spellId / "p"..spellId / "swing" / "pswing")
--   name        display name
--   spellId     nil for melee
--   isPet       true when the source was a pet/guardian
--   school      last-seen spell school (for colour)
--   casts       SPELL_CAST_SUCCESS count
--   normalHits/normalTotal/normalMax   non-crit damage
--   critHits/critTotal/critMax         crit damage
--   total       normalTotal + critTotal
local function NewRecord(key, name, spellId, isPet, school)
    return {
        key = key, name = name, spellId = spellId, isPet = isPet, school = school,
        casts = 0,
        normalHits = 0, normalTotal = 0, normalMax = 0,
        critHits = 0,   critTotal = 0,   critMax = 0,
        total = 0,
    }
end

local function NewSegment()
    return {
        spells    = {},   -- key -> record
        total     = 0,    -- total damage in the segment
        activeTime = 0,   -- accumulated seconds spent in combat
        startTime = nil,  -- GetTime() of current combat, when inCombat
        inCombat  = false,
    }
end

local current = NewSegment()
local overall = NewSegment()

local viewMode = "current"  -- "current" | "overall"

local function ActiveSegment()
    return (viewMode == "overall") and overall or current
end

-- Seconds of combat represented by a segment (live while fighting).
local function SegTime(seg)
    local t = seg.activeTime
    if seg.inCombat and seg.startTime then
        t = t + (GetTime() - seg.startTime)
    end
    return (t < 1) and 1 or t
end

local function GetOrCreate(seg, key, name, spellId, isPet, school)
    local r = seg.spells[key]
    if not r then
        r = NewRecord(key, name, spellId, isPet, school)
        seg.spells[key] = r
    else
        if school then r.school = school end
    end
    return r
end

local function AddDamageTo(seg, key, name, spellId, isPet, school, amount, crit)
    local r = GetOrCreate(seg, key, name, spellId, isPet, school)
    if crit then
        r.critHits  = r.critHits + 1
        r.critTotal = r.critTotal + amount
        if amount > r.critMax then r.critMax = amount end
    else
        r.normalHits  = r.normalHits + 1
        r.normalTotal = r.normalTotal + amount
        if amount > r.normalMax then r.normalMax = amount end
    end
    r.total = r.total + amount
    seg.total = seg.total + amount
end

local function AddCastTo(seg, key, name, spellId, isPet, school)
    local r = GetOrCreate(seg, key, name, spellId, isPet, school)
    r.casts = r.casts + 1
end

-- Record into both the current and overall segments at once.
local function RecordDamage(key, name, spellId, isPet, school, amount, crit)
    AddDamageTo(current, key, name, spellId, isPet, school, amount, crit)
    AddDamageTo(overall, key, name, spellId, isPet, school, amount, crit)
end

local function RecordCast(key, name, spellId, isPet, school)
    AddCastTo(current, key, name, spellId, isPet, school)
    AddCastTo(overall, key, name, spellId, isPet, school)
end

------------------------------------------------------------------------------
-- Combat log parsing
------------------------------------------------------------------------------

local CombatLogGetCurrentEventInfo = CombatLogGetCurrentEventInfo

-- Debug instrumentation: toggle with /tm debug, inspect with /tm status.
local debugOn = false
local dbg = { events = 0, mine = 0, dmg = 0, printed = 0 }

-- Defined here (before OnCombatLog) so the handler can call it; the fancier
-- Print() is a separate local declared later.
local function DPrint(msg)
    DEFAULT_CHAT_FRAME:AddMessage("|cffffcc00[TrackMe:dbg]|r " .. tostring(msg))
end

-- Returns "player", "pet", or nil for a combat-log source.
local function ClassifySource(guid, flags)
    if guid == playerGUID then return "player" end
    if flags and band(flags, AFFILIATION_MINE) ~= 0 and band(flags, PET_MASK) ~= 0 then
        return "pet"
    end
    return nil
end

local function OnCombatLog()
    -- Grab the common prefix plus enough sub-params to cover spell crit (p21).
    local _, subevent, _, sourceGUID, _, sourceFlags, _, _, _, _, _,
          p12, p13, p14, p15, _, _, p18, _, _, p21 = CombatLogGetCurrentEventInfo()

    if debugOn then dbg.events = dbg.events + 1 end
    local src = ClassifySource(sourceGUID, sourceFlags)
    if not src then return end
    if debugOn then dbg.mine = dbg.mine + 1 end
    local isPet = (src == "pet")

    if subevent == "SPELL_DAMAGE" or subevent == "SPELL_PERIODIC_DAMAGE"
       or subevent == "RANGE_DAMAGE" then
        local spellId, spellName, school, amount, crit = p12, p13, p14, p15, p21
        if not amount or amount <= 0 then return end
        local key = (isPet and "p" or "s") .. tostring(spellId)
        RecordDamage(key, spellName or "Unknown", spellId, isPet, school, amount, crit and true or false)
        if debugOn then
            dbg.dmg = dbg.dmg + 1
            if dbg.printed < 8 then
                dbg.printed = dbg.printed + 1
                DPrint(("%s%s hit %d%s"):format(spellName or "?",
                    isPet and " (pet)" or "", amount, crit and " CRIT" or ""))
            end
        end

    elseif subevent == "SWING_DAMAGE" then
        -- Swing params shift left (no spell fields): amount=p12, crit=p18.
        local amount, crit = p12, p18
        if not amount or amount <= 0 then return end
        local key  = isPet and "pswing" or "swing"
        local name = isPet and "Melee" or "Auto Attack"
        RecordDamage(key, name, nil, isPet, 1, amount, crit and true or false)

    elseif subevent == "SPELL_CAST_SUCCESS" then
        local spellId, spellName, school = p12, p13, p14
        local key = (isPet and "p" or "s") .. tostring(spellId)
        RecordCast(key, spellName or "Unknown", spellId, isPet, school)
    end
end

------------------------------------------------------------------------------
-- Combat start / end -> segment timing
------------------------------------------------------------------------------

local function OnCombatStart()
    -- Current view represents "this fight": wipe and start fresh.
    wipe(current.spells)
    current.total = 0
    current.activeTime = 0
    current.startTime = GetTime()
    current.inCombat = true
    -- Overall keeps its data; just start counting combat time again.
    overall.startTime = GetTime()
    overall.inCombat = true
end

local function OnCombatEnd()
    local now = GetTime()
    if current.startTime then current.activeTime = current.activeTime + (now - current.startTime) end
    if overall.startTime then overall.activeTime = overall.activeTime + (now - overall.startTime) end
    current.inCombat = false
    overall.inCombat = false
    current.startTime = nil
    overall.startTime = nil
end

------------------------------------------------------------------------------
-- Sorted query for display
------------------------------------------------------------------------------

local sortBuffer = {}
local function SortByTotal(a, b) return a.total > b.total end

local function GetSpellList(seg)
    wipe(sortBuffer)
    for _, r in pairs(seg.spells) do
        if r.total > 0 then
            sortBuffer[#sortBuffer + 1] = r
        end
    end
    table.sort(sortBuffer, SortByTotal)
    return sortBuffer
end

------------------------------------------------------------------------------
-- UI: shared frame styling (texture-based, no BackdropTemplate dependency)
------------------------------------------------------------------------------

local function StyleWindow(f, r, g, b, a)
    f.bg = f:CreateTexture(nil, "BACKGROUND")
    f.bg:SetAllPoints()
    f.bg:SetColorTexture(r or 0.05, g or 0.05, b or 0.07, a or 0.92)

    -- 1px border via four edge textures.
    local top = f:CreateTexture(nil, "BORDER");    top:SetColorTexture(0.30,0.30,0.35,1)
    top:SetPoint("TOPLEFT"); top:SetPoint("TOPRIGHT"); top:SetHeight(1)
    local bot = f:CreateTexture(nil, "BORDER");    bot:SetColorTexture(0.30,0.30,0.35,1)
    bot:SetPoint("BOTTOMLEFT"); bot:SetPoint("BOTTOMRIGHT"); bot:SetHeight(1)
    local lft = f:CreateTexture(nil, "BORDER");    lft:SetColorTexture(0.30,0.30,0.35,1)
    lft:SetPoint("TOPLEFT"); lft:SetPoint("BOTTOMLEFT"); lft:SetWidth(1)
    local rgt = f:CreateTexture(nil, "BORDER");    rgt:SetColorTexture(0.30,0.30,0.35,1)
    rgt:SetPoint("TOPRIGHT"); rgt:SetPoint("BOTTOMRIGHT"); rgt:SetWidth(1)
end

local function MakeButton(parent, text, width)
    local b = CreateFrame("Button", nil, parent)
    b:SetSize(width or 60, 18)
    local bg = b:CreateTexture(nil, "BACKGROUND")
    bg:SetAllPoints(); bg:SetColorTexture(0.18, 0.18, 0.22, 1)
    b.bg = bg
    local fs = b:CreateFontString(nil, "OVERLAY", "GameFontNormalSmall")
    fs:SetPoint("CENTER"); fs:SetText(text)
    b.text = fs
    b:SetScript("OnEnter", function(self) self.bg:SetColorTexture(0.28, 0.28, 0.34, 1) end)
    b:SetScript("OnLeave", function(self) self.bg:SetColorTexture(0.18, 0.18, 0.22, 1) end)
    return b
end

------------------------------------------------------------------------------
-- UI: detail window
------------------------------------------------------------------------------

local detailFrame
local detailKey  -- which spell record the detail window is showing

local function BuildDetailWindow()
    local f = CreateFrame("Frame", nil, UIParent)
    f:SetSize(300, 250)
    f:SetPoint("CENTER", 320, 0)
    f:SetFrameStrata("DIALOG")
    f:SetMovable(true); f:EnableMouse(true); f:RegisterForDrag("LeftButton")
    f:SetScript("OnDragStart", f.StartMoving)
    f:SetScript("OnDragStop", f.StopMovingOrSizing)
    f:SetClampedToScreen(true)
    StyleWindow(f, 0.06, 0.06, 0.09, 0.96)

    f.icon = f:CreateTexture(nil, "ARTWORK")
    f.icon:SetSize(22, 22)
    f.icon:SetPoint("TOPLEFT", 8, -8)
    f.icon:SetTexCoord(0.07, 0.93, 0.07, 0.93)

    f.title = f:CreateFontString(nil, "OVERLAY", "GameFontNormal")
    f.title:SetPoint("LEFT", f.icon, "RIGHT", 6, 0)
    f.title:SetPoint("RIGHT", f, "RIGHT", -26, 0)
    f.title:SetJustifyH("LEFT")
    f.title:SetWordWrap(false)

    local close = MakeButton(f, "X", 18)
    close:SetPoint("TOPRIGHT", -6, -8)
    close:SetScript("OnClick", function() f:Hide() end)

    f.body = f:CreateFontString(nil, "OVERLAY", "GameFontHighlightSmall")
    f.body:SetPoint("TOPLEFT", 12, -40)
    f.body:SetPoint("BOTTOMRIGHT", -12, 12)
    f.body:SetJustifyH("LEFT")
    f.body:SetJustifyV("TOP")

    f:Hide()
    detailFrame = f
end

local function RefreshDetail()
    if not detailFrame or not detailFrame:IsShown() or not detailKey then return end
    local seg = ActiveSegment()
    local r = seg.spells[detailKey]
    if not r then
        detailFrame.body:SetText("No data for this spell in the current view.")
        return
    end

    detailFrame.icon:SetTexture(SpellIcon(r.spellId))
    local petTag = r.isPet and "  |cff88ccff(Pet)|r" or ""
    detailFrame.title:SetText(r.name .. petTag)

    local totalHits = r.normalHits + r.critHits
    local critPct   = (totalHits > 0) and (r.critHits / totalHits * 100) or 0
    local normalAvg = (r.normalHits > 0) and (r.normalTotal / r.normalHits) or 0
    local critAvg   = (r.critHits   > 0) and (r.critTotal   / r.critHits)   or 0
    local dps       = r.total / SegTime(seg)
    local pctOfTot  = (seg.total > 0) and (r.total / seg.total * 100) or 0
    local castsStr  = (r.casts > 0) and tostring(r.casts) or "—"

    local lines = {}
    lines[#lines+1] = string.format("|cffffd200Casts:|r %s      |cffffd200Hits:|r %d", castsStr, totalHits)
    lines[#lines+1] = string.format("|cffffd200Crit rate:|r %.1f%%  (%d crit / %d normal)", critPct, r.critHits, r.normalHits)
    lines[#lines+1] = string.format("|cffffd200Total:|r %s  (%.1f%% of your damage)", Commas(r.total), pctOfTot)
    lines[#lines+1] = string.format("|cffffd200DPS:|r %s", Abbrev(dps))
    lines[#lines+1] = " "
    lines[#lines+1] = "|cffaaaaaa                 Non-crit          Crit|r"
    lines[#lines+1] = string.format("|cffffd200Hits|r        %10d    %10d", r.normalHits, r.critHits)
    lines[#lines+1] = string.format("|cffffd200Damage|r    %12s  %12s", Abbrev(r.normalTotal), Abbrev(r.critTotal))
    lines[#lines+1] = string.format("|cffffd200Average|r   %12s  %12s", Abbrev(normalAvg), Abbrev(critAvg))
    lines[#lines+1] = string.format("|cffffd200Biggest|r   %12s  %12s", Abbrev(r.normalMax), Abbrev(r.critMax))

    detailFrame.body:SetText(table.concat(lines, "\n"))
end

local function ShowDetail(key)
    if not detailFrame then BuildDetailWindow() end
    detailKey = key
    detailFrame:Show()
    RefreshDetail()
end

------------------------------------------------------------------------------
-- UI: main window
------------------------------------------------------------------------------

local MAX_ROWS   = 14
local ROW_HEIGHT = 18
local mainFrame
local rows = {}

local function BuildRow(parent, index)
    local row = CreateFrame("Button", nil, parent)
    row:SetSize(1, ROW_HEIGHT)
    row:SetPoint("TOPLEFT", 6, -(46 + (index - 1) * (ROW_HEIGHT + 1)))
    row:SetPoint("RIGHT", parent, "RIGHT", -6, 0)

    row.fill = row:CreateTexture(nil, "BACKGROUND")
    row.fill:SetPoint("TOPLEFT")
    row.fill:SetPoint("BOTTOMLEFT")
    row.fill:SetWidth(1)

    row.track = row:CreateTexture(nil, "BACKGROUND", nil, -1)
    row.track:SetAllPoints()
    row.track:SetColorTexture(0.12, 0.12, 0.15, 0.8)

    row.icon = row:CreateTexture(nil, "ARTWORK")
    row.icon:SetSize(ROW_HEIGHT - 4, ROW_HEIGHT - 4)
    row.icon:SetPoint("LEFT", 2, 0)
    row.icon:SetTexCoord(0.1, 0.9, 0.1, 0.9)

    row.left = row:CreateFontString(nil, "OVERLAY", "GameFontHighlightSmall")
    row.left:SetPoint("LEFT", row.icon, "RIGHT", 4, 0)
    row.left:SetJustifyH("LEFT")
    row.left:SetWordWrap(false)

    row.right = row:CreateFontString(nil, "OVERLAY", "GameFontHighlightSmall")
    row.right:SetPoint("RIGHT", -4, 0)
    row.right:SetJustifyH("RIGHT")

    row.left:SetPoint("RIGHT", row.right, "LEFT", -4, 0)

    row:SetScript("OnClick", function(self)
        if self.spellKey then ShowDetail(self.spellKey) end
    end)
    row:SetScript("OnEnter", function(self)
        if self.topWidth then self.fill:SetAlpha(1) end
    end)
    row:SetScript("OnLeave", function(self) self.fill:SetAlpha(0.55) end)

    return row
end

local function RefreshMain()
    if not mainFrame or not mainFrame:IsShown() then return end
    local seg  = ActiveSegment()
    local list = GetSpellList(seg)

    local dps = seg.total / SegTime(seg)
    mainFrame.header:SetText(string.format(
        "%s  |cffffffff%s|r  (|cff66ccff%s DPS|r)",
        (viewMode == "overall") and "Overall" or "Current",
        Abbrev(seg.total), Abbrev(dps)))

    local topTotal = list[1] and list[1].total or 1
    local width = mainFrame:GetWidth() - 12

    for i = 1, MAX_ROWS do
        local row = rows[i]
        local r = list[i]
        if r then
            row.spellKey = r.key
            local color = SchoolColor(r.school)
            if r.isPet then color = PET_TINT end
            row.fill:SetColorTexture(color[1], color[2], color[3], 1)
            row.fill:SetAlpha(0.55)
            local frac = (topTotal > 0) and (r.total / topTotal) or 0
            row.fill:SetWidth(math.max(1, width * frac))
            row.topWidth = width

            row.icon:SetTexture(SpellIcon(r.spellId))
            local petTag = r.isPet and " |cff88ccff(Pet)|r" or ""
            row.left:SetText(string.format("%d. %s%s", i, r.name, petTag))
            local pct = (seg.total > 0) and (r.total / seg.total * 100) or 0
            row.right:SetText(string.format("%s  |cffaaaaaa%.0f%%|r", Abbrev(r.total), pct))
            row:Show()
        else
            row.spellKey = nil
            row:Hide()
        end
    end

    mainFrame.empty:SetShown(list[1] == nil)
end

local function BuildMainWindow()
    local f = CreateFrame("Frame", nil, UIParent)
    f:SetSize(250, 46 + MAX_ROWS * (ROW_HEIGHT + 1) + 6)
    f:SetPoint("CENTER")
    f:SetFrameStrata("MEDIUM")
    f:SetMovable(true); f:EnableMouse(true); f:RegisterForDrag("LeftButton")
    f:SetScript("OnDragStart", f.StartMoving)
    f:SetScript("OnDragStop", f.StopMovingOrSizing)
    f:SetClampedToScreen(true)
    StyleWindow(f)

    -- Title bar
    f.titleBar = f:CreateTexture(nil, "ARTWORK")
    f.titleBar:SetPoint("TOPLEFT", 1, -1)
    f.titleBar:SetPoint("TOPRIGHT", -1, -1)
    f.titleBar:SetHeight(20)
    f.titleBar:SetColorTexture(0.10, 0.10, 0.14, 1)

    f.title = f:CreateFontString(nil, "OVERLAY", "GameFontNormalSmall")
    f.title:SetPoint("TOPLEFT", 8, -5)
    f.title:SetText("|cff66ccffTrackMe|r")

    local close = MakeButton(f, "X", 18)
    close:SetPoint("TOPRIGHT", -4, -2)
    close:SetScript("OnClick", function() f:Hide() end)

    f.toggle = MakeButton(f, "Overall", 62)
    f.toggle:SetPoint("TOPRIGHT", close, "TOPLEFT", -4, 0)
    f.toggle:SetScript("OnClick", function(self)
        viewMode = (viewMode == "current") and "overall" or "current"
        self.text:SetText((viewMode == "current") and "Overall" or "Current")
        RefreshMain(); RefreshDetail()
    end)

    f.clear = MakeButton(f, "Clear", 44)
    f.clear:SetPoint("TOPRIGHT", f.toggle, "TOPLEFT", -4, 0)
    f.clear:SetScript("OnClick", function()
        current = NewSegment(); overall = NewSegment()
        if detailFrame then detailFrame:Hide() end
        RefreshMain()
    end)

    -- Segment header line (total / dps)
    f.header = f:CreateFontString(nil, "OVERLAY", "GameFontHighlightSmall")
    f.header:SetPoint("TOPLEFT", 8, -26)
    f.header:SetJustifyH("LEFT")

    f.empty = f:CreateFontString(nil, "OVERLAY", "GameFontDisableSmall")
    f.empty:SetPoint("TOP", 0, -70)
    f.empty:SetText("No damage recorded yet.")

    for i = 1, MAX_ROWS do
        rows[i] = BuildRow(f, i)
    end

    -- Throttled live refresh, driven by a timer that only runs while the
    -- window is shown. Using C_Timer instead of a per-frame OnUpdate keeps
    -- TrackMe's code off the execution stack when idle/hidden, so it won't be
    -- named as a bystander in unrelated taint (e.g. Blizzard Edit Mode errors).
    f:SetScript("OnShow", function(self)
        RefreshMain()
        RefreshDetail()
        if not self.ticker then
            self.ticker = C_Timer.NewTicker(0.5, function()
                RefreshMain()
                RefreshDetail()
            end)
        end
    end)
    f:SetScript("OnHide", function(self)
        if self.ticker then
            self.ticker:Cancel()
            self.ticker = nil
        end
    end)

    mainFrame = f
end

local function ToggleMain()
    if not mainFrame then BuildMainWindow() end
    if mainFrame:IsShown() then
        mainFrame:Hide()
    else
        mainFrame:Show()
        RefreshMain()
    end
end

------------------------------------------------------------------------------
-- Events & slash commands
------------------------------------------------------------------------------

local function Print(msg)
    DEFAULT_CHAT_FRAME:AddMessage("|cff66ccff[TrackMe]|r " .. tostring(msg))
end

local driver = CreateFrame("Frame")
driver:RegisterEvent("PLAYER_LOGIN")
driver:RegisterEvent("COMBAT_LOG_EVENT_UNFILTERED")
driver:RegisterEvent("PLAYER_REGEN_DISABLED")
driver:RegisterEvent("PLAYER_REGEN_ENABLED")
driver:SetScript("OnEvent", function(_, event)
    if event == "COMBAT_LOG_EVENT_UNFILTERED" then
        OnCombatLog()
    elseif event == "PLAYER_REGEN_DISABLED" then
        OnCombatStart()
    elseif event == "PLAYER_REGEN_ENABLED" then
        OnCombatEnd()
    elseif event == "PLAYER_LOGIN" then
        playerGUID = UnitGUID("player")
        BuildMainWindow()
        mainFrame:Show()
        RefreshMain()
        Print("loaded. /trackme to toggle the window, /trackme help for commands.")
    end
end)

SLASH_TRACKME1 = "/trackme"
SLASH_TRACKME2 = "/tm"
SlashCmdList["TRACKME"] = function(input)
    local arg = (input or ""):lower():gsub("^%s+", ""):gsub("%s+$", "")
    if arg == "clear" or arg == "reset" then
        current = NewSegment(); overall = NewSegment()
        if detailFrame then detailFrame:Hide() end
        RefreshMain()
        Print("data cleared.")
    elseif arg == "overall" then
        viewMode = "overall"
        if mainFrame then mainFrame.toggle.text:SetText("Current") end
        if not mainFrame:IsShown() then ToggleMain() else RefreshMain() end
    elseif arg == "current" then
        viewMode = "current"
        if mainFrame then mainFrame.toggle.text:SetText("Overall") end
        if not mainFrame:IsShown() then ToggleMain() else RefreshMain() end
    elseif arg == "debug" then
        debugOn = not debugOn
        dbg.events, dbg.mine, dbg.dmg, dbg.printed = 0, 0, 0, 0
        Print("debug logging: " .. (debugOn and "ON (hit something)" or "OFF"))
    elseif arg == "status" then
        local nSpells = 0
        for _ in pairs(current.spells) do nSpells = nSpells + 1 end
        Print("---- status ----")
        Print("playerGUID = " .. tostring(playerGUID))
        Print(("constants: MINE=%s PET_MASK=%s")
            :format(tostring(AFFILIATION_MINE), tostring(PET_MASK)))
        Print(("CLEU registered = %s"):format(
            tostring(driver:IsEventRegistered("COMBAT_LOG_EVENT_UNFILTERED"))))
        Print(("events seen=%d, yours=%d, damage recorded=%d")
            :format(dbg.events, dbg.mine, dbg.dmg))
        Print(("current.total=%s, spells=%d, view=%s")
            :format(tostring(current.total), nSpells, viewMode))
        Print(("window exists=%s shown=%s")
            :format(tostring(mainFrame ~= nil),
                    tostring(mainFrame and mainFrame:IsShown())))
        if not debugOn then Print("(run /tm debug first, then fight, to populate counters)") end
    elseif arg == "help" then
        Print("Commands:")
        Print("  /trackme          toggle the window")
        Print("  /trackme current  show the current fight")
        Print("  /trackme overall  show accumulated totals")
        Print("  /trackme clear    wipe all recorded data")
        Print("  /trackme debug    toggle combat-log debug logging")
        Print("  /trackme status   dump internal state for troubleshooting")
    else
        ToggleMain()
    end
end
