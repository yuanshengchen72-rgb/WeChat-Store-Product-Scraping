from __future__ import annotations

import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class WindowsLauncherTests(unittest.TestCase):
    def test_batch_launchers_are_ascii_safe_for_cmd(self) -> None:
        for name in ("安装依赖.bat", "启动采集.bat", "INSTALL.bat", "START.bat"):
            payload = (ROOT / name).read_bytes()
            payload.decode("ascii")

    def test_startup_check_returns_a_visible_result(self) -> None:
        completed = subprocess.run(
            ["cmd.exe", "/d", "/c", "START.bat", "--check"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        output = completed.stdout + completed.stderr
        self.assertRegex(output, r"\[(?:OK|ERROR)\]")
        self.assertNotIn("is not recognized as an internal or external command", output)


if __name__ == "__main__":
    unittest.main()
