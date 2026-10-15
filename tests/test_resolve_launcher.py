from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from services import resolve_launcher as rl

FUSCRIPT = Path(
    "/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion/fuscript"
)

# Fake Resolve API in Lua. Runs the importer with pcall (Resolve logs uncaught
# errors) and records what it did into RESULT as key=value lines.
_LUA_HARNESS = """
local calls = {}
local existing = {}
for name in string.gmatch(EXISTING, "[^|]+") do existing[name] = true end
local function Project(name)
  local p = {name = name, settings = {}}
  function p:SetSetting(k, v) self.settings[k] = v; return true end
  function p:GetMediaPool()
    local mp = {}
    function mp:ImportTimelineFromFile(path, opts)
      calls.import_path = path
      calls.import_source = tostring(opts.importSourceClips)
      calls.timeline_name = opts.timelineName
      if IMPORT_OK then return {} end
      return nil
    end
    return mp
  end
  function p:SetCurrentTimeline(t) calls.current = "yes"; return true end
  function p:GetName() return self.name end
  return p
end
local pm = {}
function pm:CreateProject(name)
  if existing[name] then return nil end
  existing[name] = true
  calls.created = name
  calls.project = Project(name)
  return calls.project
end
function pm:SaveProject() calls.saved = "yes"; return true end
local fake = {}
function fake:GetProjectManager() return pm end
function fake:OpenPage(page) calls.page = page; return true end
rawset(_G, "resolve", fake)
local store = {["Wordly.LastImportId"] = LAST_ID}
local fake_fusion = {}
function fake_fusion:GetData(k) return store[k] end
function fake_fusion:SetData(k, v) store[k] = v end
rawset(_G, "fusion", fake_fusion)
local ok, err = pcall(dofile, SCRIPT)
local out = io.open(RESULT, "w")
out:write("ok=" .. tostring(ok) .. "\\n")
out:write("error=" .. tostring(err) .. "\\n")
out:write("last_id=" .. tostring(store["Wordly.LastImportId"]) .. "\\n")
for _, k in ipairs({"created", "import_path", "import_source", "timeline_name", "current", "saved", "page"}) do
  out:write(k .. "=" .. tostring(calls[k]) .. "\\n")
end
if calls.project then
  for _, k in ipairs({"timelineResolutionWidth", "timelineResolutionHeight", "timelineFrameRate"}) do
    out:write(k .. "=" .. tostring(calls.project.settings[k]) .. "\\n")
  end
end
out:close()
"""


@unittest.skipUnless(FUSCRIPT.is_file(), "DaVinci Resolve's Lua interpreter is not installed")
class TestLuaImportScript(unittest.TestCase):
    """Runs the real Wordly Import.lua in Resolve's interpreter against a fake Resolve."""

    def _run(self, tmp: Path, *, existing=(), import_ok=True, timeline_exists=True, write_pending=True, last_id=None):
        pending = tmp / "pending.lua"
        timeline = tmp / 'Sunday "AM".fcpxml'
        if timeline_exists:
            timeline.write_text("<fcpxml/>", encoding="utf-8")
        request_id = rl.write_pending_import(timeline, "Sunday", path=pending) if write_pending else ""
        script = tmp / "Wordly Import.lua"
        script.write_text(rl.render_import_script(pending), encoding="utf-8")
        result = tmp / "result.txt"
        harness = tmp / "harness.lua"
        last = "nil" if last_id is None else rl._lua_string(request_id if last_id == "same" else last_id)
        harness.write_text(
            f"SCRIPT = {rl._lua_string(str(script))}\n"
            f"RESULT = {rl._lua_string(str(result))}\n"
            f"EXISTING = {rl._lua_string('|'.join(existing))}\n"
            f"IMPORT_OK = {'true' if import_ok else 'false'}\n"
            f"LAST_ID = {last}\n" + _LUA_HARNESS,
            encoding="utf-8",
        )
        proc = subprocess.run([str(FUSCRIPT), "-l", "lua", str(harness)], capture_output=True, text=True, timeout=60)
        self.assertTrue(result.is_file(), proc.stdout + proc.stderr)
        calls = dict(line.split("=", 1) for line in result.read_text(encoding="utf-8").splitlines())
        return calls, timeline, request_id

    def test_creates_vertical_project_and_imports_timeline(self) -> None:
        with TemporaryDirectory() as tmp:
            calls, timeline, request_id = self._run(Path(tmp))
        self.assertEqual(calls["ok"], "true", calls["error"])
        self.assertEqual(calls["created"], "Sunday")
        self.assertEqual(calls["import_path"], str(timeline.resolve()))
        self.assertEqual(calls["import_source"], "true")
        self.assertEqual(calls["timelineResolutionWidth"], "1080")
        self.assertEqual(calls["timelineResolutionHeight"], "1920")
        self.assertEqual(calls["timelineFrameRate"], "30")
        self.assertEqual((calls["current"], calls["saved"], calls["page"]), ("yes", "yes", "edit"))
        self.assertEqual(calls["last_id"], request_id)

    def test_existing_project_name_gets_suffix(self) -> None:
        with TemporaryDirectory() as tmp:
            calls, _, _ = self._run(Path(tmp), existing=("Sunday",))
        self.assertEqual(calls["created"], "Sunday (2)")

    def test_import_failure_raises_wordly_failed(self) -> None:
        with TemporaryDirectory() as tmp:
            calls, _, _ = self._run(Path(tmp), import_ok=False)
        self.assertEqual(calls["ok"], "false")
        self.assertTrue(calls["error"].startswith("WORDLY_FAILED: Resolve could not import"))

    def test_missing_timeline_fails_before_creating_project(self) -> None:
        with TemporaryDirectory() as tmp:
            calls, _, _ = self._run(Path(tmp), timeline_exists=False)
        self.assertTrue(calls["error"].startswith("WORDLY_FAILED: Timeline file not found"))
        self.assertEqual(calls["created"], "nil")

    def test_repeat_click_for_same_request_does_nothing(self) -> None:
        with TemporaryDirectory() as tmp:
            calls, _, _ = self._run(Path(tmp), last_id="same")
        self.assertEqual(calls["ok"], "true")
        self.assertEqual(calls["created"], "nil")

    def test_new_request_runs_after_an_older_one(self) -> None:
        with TemporaryDirectory() as tmp:
            calls, _, _ = self._run(Path(tmp), last_id="older-request")
        self.assertEqual(calls["created"], "Sunday")

    def test_nothing_pending_does_nothing(self) -> None:
        with TemporaryDirectory() as tmp:
            calls, _, _ = self._run(Path(tmp), write_pending=False)
        self.assertEqual(calls["ok"], "true")
        self.assertEqual(calls["created"], "nil")


class TestImportWatcher(unittest.TestCase):
    def _logs(self, tmp: Path) -> tuple[Path, Path]:
        main = tmp / "davinci_resolve.log"
        debug = tmp / "ResolveDebug.txt"
        main.write_text("old | Start saving project Sunday\n", encoding="utf-8")
        debug.write_text("old | WORDLY_FAILED: stale\n", encoding="utf-8")
        return main, debug

    @staticmethod
    def _append(path: Path, text: str) -> None:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(text)

    def test_success_after_import_and_save(self) -> None:
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            main, _ = self._logs(tmp_path)
            timeline = tmp_path / "t.fcpxml"
            watcher = rl.ImportWatcher(timeline, "Sunday", log_dir=tmp_path)
            self.assertIsNone(watcher.poll())  # earlier log lines are ignored
            self._append(main, "x | Current project pointer changed to (Sunday (2)) from project library\n")
            self.assertIsNone(watcher.poll())
            self.assertTrue(watcher.started)
            self._append(main, f"x | Import Log (Info) - Start importing {timeline.resolve()}.\n")
            self._append(main, "x | Start saving project Sunday (2)\n")
            self.assertEqual(watcher.poll(), ("imported", "Sunday (2)"))

    def test_failure_from_debug_log(self) -> None:
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _, debug = self._logs(tmp_path)
            watcher = rl.ImportWatcher(tmp_path / "t.fcpxml", "Sunday", log_dir=tmp_path)
            self._append(debug, ".../Wordly Import.lua:60: WORDLY_FAILED: Resolve could not import x\n")
            self.assertEqual(watcher.poll(), ("failed", "Resolve could not import x"))

    def test_save_of_other_project_is_ignored(self) -> None:
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            main, _ = self._logs(tmp_path)
            timeline = tmp_path / "t.fcpxml"
            watcher = rl.ImportWatcher(timeline, "Sunday", log_dir=tmp_path)
            self._append(main, f"x | Start importing {timeline.resolve()}.\nx | Start saving project Other\n")
            self.assertIsNone(watcher.poll())


class TestInstallImportScript(unittest.TestCase):
    def test_install_writes_lua_script_with_pending_path(self) -> None:
        with TemporaryDirectory() as tmp, mock.patch.object(rl.sys, "platform", "darwin"):
            legacy = Path(tmp) / "Wordly Import.py"
            legacy.write_text("old", encoding="utf-8")
            target = rl.install_import_script(Path(tmp))
            self.assertIsNotNone(target)
            text = target.read_text(encoding="utf-8")
            self.assertFalse(legacy.exists())
        self.assertEqual(target.name, "Wordly Import.lua")
        self.assertIn(rl._lua_string(str(rl.PENDING_IMPORT_PATH.resolve())), text)
        self.assertNotIn("__WORDLY_", text)


class TestOpenResolveProject(unittest.TestCase):
    def _patches(self, tmp: Path, menu_items: list[str]):
        return [
            mock.patch.object(rl.sys, "platform", "darwin"),
            mock.patch.object(rl, "PENDING_IMPORT_PATH", tmp / "pending.lua"),
            mock.patch.object(rl, "find_resolve_app", return_value=Path("/Apps/R.app")),
            mock.patch.object(rl, "install_import_script"),
            mock.patch.object(rl, "_resolve_running", return_value=True),
            mock.patch.object(rl, "_scripts_menu_items", return_value=menu_items),
            mock.patch.object(rl.subprocess, "Popen"),
        ]

    def test_clicks_menu_and_returns_project_name(self) -> None:
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            timeline = tmp_path / "t.fcpxml"
            timeline.write_text("<fcpxml/>", encoding="utf-8")
            outcomes = iter([None, ("imported", "Sunday")])
            patches = self._patches(tmp_path, ["Comp", rl.IMPORT_SCRIPT_NAME]) + [
                mock.patch.object(rl.ImportWatcher, "poll", side_effect=lambda: next(outcomes)),
                mock.patch.object(rl, "_click_import_menu_item"),
                mock.patch.object(rl.time, "sleep"),
            ]
            for p in patches:
                p.start()
            try:
                name = rl.open_resolve_project(timeline, project_name="Sunday")
                clicks = rl._click_import_menu_item.call_count  # type: ignore[attr-defined]
            finally:
                for p in reversed(patches):
                    p.stop()
        self.assertEqual(name, "Sunday")
        self.assertEqual(clicks, 1)

    def test_failure_from_script_is_raised(self) -> None:
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            timeline = tmp_path / "t.fcpxml"
            timeline.write_text("<fcpxml/>", encoding="utf-8")
            patches = self._patches(tmp_path, [rl.IMPORT_SCRIPT_NAME]) + [
                mock.patch.object(rl.ImportWatcher, "poll", return_value=("failed", "bad xml")),
                mock.patch.object(rl, "_click_import_menu_item"),
            ]
            for p in patches:
                p.start()
            try:
                with self.assertRaises(rl.ResolveOpenError) as ctx:
                    rl.open_resolve_project(timeline)
            finally:
                for p in reversed(patches):
                    p.stop()
        self.assertIn("bad xml", str(ctx.exception))

    def test_already_running_without_script_explains_restart(self) -> None:
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            timeline = tmp_path / "t.fcpxml"
            timeline.write_text("<fcpxml/>", encoding="utf-8")
            patches = self._patches(tmp_path, ["Comp", "No Scripts"]) + [
                mock.patch.object(rl.time, "monotonic", side_effect=[0.0, 0.0, 100.0]),
                mock.patch.object(rl.time, "sleep"),
            ]
            for p in patches:
                p.start()
            try:
                with self.assertRaises(rl.ResolveOpenError) as ctx:
                    rl.open_resolve_project(timeline)
            finally:
                for p in reversed(patches):
                    p.stop()
        self.assertIn("Quit and reopen Resolve", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
