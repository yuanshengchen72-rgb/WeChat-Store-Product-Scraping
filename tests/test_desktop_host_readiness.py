import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wechat_shop_scraper.desktop_keyword import DesktopWechatBackend, DesktopWechatError


class HostReadinessTests(unittest.TestCase):
    def test_uninitialized_host_is_rejected_before_native_attach(self):
        backend = DesktopWechatBackend()
        with mock.patch("wechat_shop_scraper.desktop_keyword._visible_weixin_identity", return_value=(11, 22)), mock.patch(
            "wechat_shop_scraper.desktop_keyword._wmpf_root_pid", return_value=33
        ), mock.patch("wechat_shop_scraper.desktop_keyword._powershell_json", return_value={"started": "cold-test", "MainWindowHandle": 0}), mock.patch(
            "wechat_shop_scraper.desktop_keyword.loaded_module_path"
        ) as native_module:
            with self.assertRaisesRegex(DesktopWechatError, "尚未初始化"):
                backend.start()
        native_module.assert_not_called()

    def test_verified_host_can_resume_with_closed_applet_window(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "host.json"
            backend = DesktopWechatBackend(host_state_path=marker)
            with mock.patch("wechat_shop_scraper.desktop_keyword._powershell_json", return_value={"started": "warm-test", "MainWindowHandle": 123, "MainWindowTitle": "小店"}):
                backend._require_initialized_host(987)
            backend._remember_initialized_host()
            fresh = DesktopWechatBackend(host_state_path=marker)
            with mock.patch("wechat_shop_scraper.desktop_keyword._powershell_json", return_value={"started": "warm-test", "MainWindowHandle": 0}):
                fresh._require_initialized_host(987)
            with mock.patch("wechat_shop_scraper.desktop_keyword._powershell_json", return_value={"started": "new-process", "MainWindowHandle": 0}):
                with self.assertRaisesRegex(DesktopWechatError, "尚未初始化"):
                    fresh._require_initialized_host(987)


if __name__ == "__main__":
    unittest.main()
