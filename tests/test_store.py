"""核心逻辑的单元测试（通过环境变量隔离目录，不触碰真实 ~/.ssh）。"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from ssh_hub.config import ENV_STORE_DIR, ENV_SSH_DIR  # noqa: E402
from ssh_hub.store import HubError, Inventory, Server  # noqa: E402


class InventoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Path(self.tmp.name) / "store"

    def tearDown(self):
        self.tmp.cleanup()

    def test_crud_roundtrip(self):
        inv = Inventory(self.store)
        self.assertEqual(inv.list_servers(), [])
        inv.add(Server(alias="web01", host="1.2.3.4", user="root", key="keys/web01"))
        got = inv.get("web01")
        self.assertEqual(got.host, "1.2.3.4")
        self.assertEqual(got.key, "keys/web01")
        self.assertTrue(got.created_at)
        with self.assertRaises(HubError):
            inv.add(Server(alias="web01", host="9.9.9.9"))
        inv.remove("web01")
        self.assertEqual(inv.list_servers(), [])
        with self.assertRaises(HubError):
            inv.get("web01")

    def test_invalid_alias_rejected(self):
        inv = Inventory(self.store)
        with self.assertRaises(HubError):
            inv.add(Server(alias="bad alias!", host="x"))


class CliSmokeTest(unittest.TestCase):
    """在隔离的临时目录中完整跑一遍 CLI 主流程。"""

    def _run(self, *argv: str) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env[ENV_STORE_DIR] = str(self.store)
        env[ENV_SSH_DIR] = str(self.ssh_dir)
        return subprocess.run(
            [sys.executable, "-m", "ssh_hub", *argv],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(PROJECT_ROOT),
        )

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Path(self.tmp.name) / "store"
        self.ssh_dir = Path(self.tmp.name) / "ssh"

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_flow(self):
        r = self._run("init")
        self.assertEqual(r.returncode, 0, r.stderr)

        r = self._run("add", "web01", "--host", "192.168.1.10", "--user", "ubuntu", "--generate")
        self.assertEqual(r.returncode, 0, r.stderr)

        r = self._run("list")
        self.assertIn("web01", r.stdout)
        r = self._run("list", "--json")
        self.assertIn("web01", r.stdout)

        r = self._run("sync")
        self.assertEqual(r.returncode, 0, r.stderr)
        fragment = self.ssh_dir / "dsh_ssh_hub_config"
        self.assertTrue(fragment.exists())
        text = fragment.read_text(encoding="utf-8")
        self.assertIn("Host web01", text)
        self.assertIn("IdentityFile", text)
        self.assertTrue((self.ssh_dir / "dsh_hub_web01").exists())
        main_cfg = (self.ssh_dir / "config").read_text(encoding="utf-8")
        self.assertIn("Include dsh_ssh_hub_config", main_cfg)

        # sync 幂等：重复执行不会重复注入 Include
        r = self._run("sync")
        self.assertEqual(r.returncode, 0, r.stderr)
        main_cfg2 = (self.ssh_dir / "config").read_text(encoding="utf-8")
        self.assertEqual(main_cfg2.count("Include dsh_ssh_hub_config"), 1)

        r = self._run("backup", "--dir", str(self.tmp.name))
        self.assertEqual(r.returncode, 0, r.stderr)
        backups = list(Path(self.tmp.name).glob("dsh-ssh-hub-backup-*.tar.gz"))
        self.assertEqual(len(backups), 1)

    def test_unknown_alias_errors(self):
        r = self._run("connect", "nope")
        self.assertNotEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
