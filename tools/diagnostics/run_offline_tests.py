"""Run unit tests with a hard barrier against native Frida attachment."""
from pathlib import Path
import sys
import typing
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / '.tools/native-bootstrap'))
from typing_extensions import NotRequired

if not hasattr(typing, "NotRequired"):
    typing.NotRequired = NotRequired

if __name__ == "__main__":
    with patch("frida.core.Device.attach", side_effect=AssertionError("离线测试禁止挂载真实进程")), \
         patch("frida.core.Device.spawn", side_effect=AssertionError("离线测试禁止启动真实目标")), \
         patch("wechat_shop_scraper.desktop_bootstrap._ShopUI.__init__", side_effect=AssertionError("离线测试禁止访问真实微信界面")):
        suite = (unittest.defaultTestLoader.loadTestsFromNames(sys.argv[1:])
                 if len(sys.argv) > 1 else unittest.defaultTestLoader.discover("tests"))
        result = unittest.TextTestRunner(verbosity=1).run(suite)
    raise SystemExit(not result.wasSuccessful())
