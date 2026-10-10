"""核心逻辑的单元测试（通过环境变量隔离目录，不触碰真实 ~/.ssh）。"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from ssh_hub.config import ENV_STORE_DIR, ENV_SSH_DIR  # noqa: E402
from ssh_hub.ssh import is_transport_failure, CONNECT_TIMEOUT, CMD_TIMEOUT  # noqa: E402
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


class TransportFailureTest(unittest.TestCase):
    """锁定 ①②：传输层失败判定 + 非零退出码不触发回退。"""

    def test_transport_failure_codes(self):
        # OpenSSH 连接/认证层错误
        self.assertTrue(is_transport_failure(255))
        # 本工具超时
        self.assertTrue(is_transport_failure(124))
        # 远端命令自身非零（不应触发回退）
        self.assertFalse(is_transport_failure(1))
        self.assertFalse(is_transport_failure(2))
        self.assertFalse(is_transport_failure(22))  # curl -f 5xx
        self.assertFalse(is_transport_failure(127))  # command not found
        self.assertFalse(is_transport_failure(0))
        self.assertFalse(is_transport_failure(130))  # SIGINT
        self.assertFalse(is_transport_failure(254))  # 边界：非 255

    def test_connect_timeout_is_15s(self):
        self.assertEqual(CONNECT_TIMEOUT, 15)

    def test_cmd_timeout_is_120s(self):
        self.assertEqual(CMD_TIMEOUT, 120)

    def _mock_env(self):
        """构造 mock 环境：_store_and_inventory 返回 (Path, Inventory)。"""
        from pathlib import Path
        mock_inv = patch.object("ssh_hub.cli._store_and_inventory")
        return mock_inv

    def _make_args(self, command):
        """构造 cmd_run 所需的 args 命名空间。"""
        return type("Args", (), {
            "alias": "test",
            "command": command,
            "cwd": None,
            "timeout": None,
        })()

    def _fake_inv(self):
        from pathlib import Path
        fake_inv = Inventory(Path(tempfile.mkdtemp()))
        fake_inv.add(Server(alias="test", host="1.2.3.4"))
        return fake_inv

    def test_run_nonzero_no_fallback(self):
        """远端命令返回非零（如 exit 1）时，不应触发密码回退。"""
        from ssh_hub import cli
        from pathlib import Path

        fake_inv = self._fake_inv()
        with patch.object(cli, "_store_and_inventory", return_value=(Path("/tmp"), fake_inv)), \
             patch.object(cli, "run_ssh", return_value=(1, None)) as mock_ssh, \
             patch.object(cli, "try_run_with_password") as mock_fb:
            rc = cli.cmd_run(self._make_args(["--", "false"]))
            self.assertEqual(rc, 1)
            mock_ssh.assert_called_once()
            mock_fb.assert_not_called()  # 关键：不触发回退

    def test_run_transport_failure_triggers_fallback(self):
        """传输层失败（255）时，应触发密码回退。"""
        from ssh_hub import cli
        from pathlib import Path

        fake_inv = self._fake_inv()
        with patch.object(cli, "_store_and_inventory", return_value=(Path("/tmp"), fake_inv)), \
             patch.object(cli, "run_ssh", return_value=(255, "Permission denied")) as mock_ssh, \
             patch.object(cli, "try_run_with_password", return_value=0) as mock_fb:
            rc = cli.cmd_run(self._make_args(["--", "hostname"]))
            self.assertEqual(rc, 0)
            mock_ssh.assert_called_once()
            mock_fb.assert_called_once()  # 关键：触发了回退

    def test_run_transport_failure_fallback_fails(self):
        """传输层失败 + 回退也失败时，返回回退的退出码。"""
        from ssh_hub import cli
        from pathlib import Path

        fake_inv = self._fake_inv()
        with patch.object(cli, "_store_and_inventory", return_value=(Path("/tmp"), fake_inv)), \
             patch.object(cli, "run_ssh", return_value=(255, "key denied")) as mock_ssh, \
             patch.object(cli, "try_run_with_password", return_value=255) as mock_fb:
            rc = cli.cmd_run(self._make_args(["--", "hostname"]))
            self.assertEqual(rc, 255)
            mock_fb.assert_called_once()


if __name__ == "__main__":
    unittest.main()
