from __future__ import annotations

import os
import stat
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from services import claude_client as cc


class TestApiKey(unittest.TestCase):
    def setUp(self) -> None:
        self.env = mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""})
        self.env.start()
        self.keychain = mock.patch.object(cc, "_keychain_key", return_value=None)
        self.keychain.start()

    def tearDown(self) -> None:
        self.keychain.stop()
        self.env.stop()

    def test_save_writes_private_env_file_and_keeps_other_lines(self) -> None:
        with TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text("OTHER=1\nANTHROPIC_API_KEY=old\n", encoding="utf-8")
            cc.save_api_key("  sk-ant-new  ", env)
            self.assertEqual(env.read_text(encoding="utf-8"), "OTHER=1\nANTHROPIC_API_KEY=sk-ant-new\n")
            self.assertEqual(stat.S_IMODE(env.stat().st_mode), 0o600)
            self.assertEqual(cc.api_key(env), "sk-ant-new")

    def test_reads_quoted_and_exported_values(self) -> None:
        with TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text('# comment\nexport ANTHROPIC_API_KEY="sk-ant-quoted"\n', encoding="utf-8")
            self.assertEqual(cc.api_key(env), "sk-ant-quoted")

    def test_environment_variable_wins(self) -> None:
        with TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text("ANTHROPIC_API_KEY=from-file\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "from-env"}):
                self.assertEqual(cc.api_key(env), "from-env")

    def test_missing_key_raises(self) -> None:
        with TemporaryDirectory() as tmp, self.assertRaises(cc.MissingApiKey):
            cc.api_key(Path(tmp) / ".env")

    def test_env_file_is_git_ignored(self) -> None:
        import subprocess

        result = subprocess.run(["git", "check-ignore", ".env"], cwd=cc.ROOT, capture_output=True, text=True)
        self.assertEqual(result.stdout.strip(), ".env")



class TestWorkspace(unittest.TestCase):
    def test_workspace_header_added_when_configured(self) -> None:
        with TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ, {"ANTHROPIC_API_KEY": "sk-ant-x", "ANTHROPIC_WORKSPACE_ID": ""}
        ):
            env = Path(tmp) / ".env"
            env.write_text("ANTHROPIC_WORKSPACE_ID=wrkspc_123\n", encoding="utf-8")
            self.assertEqual(cc.workspace_id(env), "wrkspc_123")
            with mock.patch.object(cc, "ENV_FILE", env):
                headers = cc.client()._custom_headers
            self.assertEqual(headers.get("anthropic-workspace-id"), "wrkspc_123")
            env.write_text("", encoding="utf-8")
            self.assertIsNone(cc.workspace_id(env))

if __name__ == "__main__":
    unittest.main()
