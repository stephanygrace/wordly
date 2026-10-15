-- Wordly -> DaVinci Resolve importer (runs inside Resolve: Workspace > Scripts).
--
-- Installed by Wordly into Resolve's Utility scripts folder. Resolve Free only
-- allows scripting from inside the app and its Scripts menu lists Lua scripts
-- (not Python), so this is Lua. Wordly writes a pending-import file and then
-- clicks this menu item; the script creates a project, imports the FCPXML
-- timeline and opens it on the Edit page.
--
-- Resolve's Lua sandbox can read files (dofile) but not write or delete them,
-- so Wordly follows progress through Resolve's own logs, failures are raised
-- as "WORDLY_FAILED: ..." errors (logged to ResolveDebug.txt), and the last
-- handled request id is kept with fusion:SetData so a repeat click is a no-op.

local PENDING_PATH = __WORDLY_PENDING_PATH__
local LAST_ID_KEY = "Wordly.LastImportId"

local function fail(message)
    error("WORDLY_FAILED: " .. message, 0)
end

local function file_exists(path)
    if bmd and bmd.fileexists then
        return bmd.fileexists(path)
    end
    local fh = io and io.open(path, "r")
    if fh then
        fh:close()
        return true
    end
    return false
end

local function last_id()
    if fusion and fusion.GetData then
        local ok, value = pcall(function() return fusion:GetData(LAST_ID_KEY) end)
        if ok then return value end
    end
    return nil
end

local function remember_id(id)
    if fusion and fusion.SetData then
        pcall(function() fusion:SetData(LAST_ID_KEY, id) end)
    end
end

local function main()
    local ok, job = pcall(dofile, PENDING_PATH)
    if not ok or type(job) ~= "table" then
        print("[Wordly] Nothing to import - click Generate in Wordly first.")
        return
    end
    if last_id() == job.id then
        print("[Wordly] This export was already imported.")
        return
    end
    remember_id(job.id)

    local name = job.project_name
    if not file_exists(job.timeline) then
        fail("Timeline file not found: " .. tostring(job.timeline))
    end

    local app = resolve or Resolve()
    local pm = app:GetProjectManager()
    local project = pm:CreateProject(name)
    local n = 2
    while not project and n < 100 do
        project = pm:CreateProject(name .. " (" .. n .. ")")
        n = n + 1
    end
    if not project then
        fail("Could not create a Resolve project named " .. name)
    end

    project:SetSetting("timelineResolutionWidth", tostring(job.width))
    project:SetSetting("timelineResolutionHeight", tostring(job.height))
    project:SetSetting("timelineFrameRate", tostring(job.fps))
    project:SetSetting("timelinePlaybackFrameRate", tostring(job.fps))

    local timeline = project:GetMediaPool():ImportTimelineFromFile(
        job.timeline,
        { timelineName = name, importSourceClips = true }
    )
    if not timeline then
        fail("Resolve could not import " .. job.timeline)
    end

    project:SetCurrentTimeline(timeline)
    app:OpenPage("edit")
    -- Saving is the last step; Wordly treats "Start saving project" as done.
    pm:SaveProject()
    print("[Wordly] Imported " .. job.timeline .. " into project " .. project:GetName())
end

main()
