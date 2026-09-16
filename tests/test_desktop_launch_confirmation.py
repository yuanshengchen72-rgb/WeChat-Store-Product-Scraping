"""A successful URI dispatch or background PID is not a visible shop launch."""
import ctypes
import itertools
import unittest
from unittest import mock

from wechat_shop_scraper import desktop_keyword as desktop


class LaunchConfirmationTests(unittest.TestCase):
    def invoke(self, rows, pid=4321):
        with mock.patch.object(ctypes.windll.shell32, 'ShellExecuteW', return_value=33), \
             mock.patch.object(desktop, '_wmpf_root_pid', return_value=pid), \
             mock.patch.object(desktop, '_powershell_json', side_effect=rows) as snapshot, \
             mock.patch.object(desktop.time, 'monotonic', side_effect=itertools.count(0, 0.25)), \
             mock.patch.object(desktop.time, 'sleep'):
            result = desktop._launch_wmpf_host(11, timeout=1)
        return result, snapshot.call_count

    def test_preexisting_background_host_is_not_launch_success(self):
        with self.assertRaisesRegex(desktop.DesktopWechatError, '未打开小店窗口'):
            self.invoke([{'MainWindowHandle': 0}] * 4)

    def test_unrelated_window_is_not_shop_launch_success(self):
        with self.assertRaisesRegex(desktop.DesktopWechatError, '未打开小店窗口'):
            self.invoke([{'MainWindowHandle': 12, 'MainWindowTitle': '翻译'}] * 4)

    def test_waits_for_actual_shop_window_after_background_pid(self):
        result, snapshots = self.invoke([
            {'MainWindowHandle': 0},
            {'MainWindowHandle': 12, 'MainWindowTitle': '小店'},
        ])
        self.assertEqual(result, 4321)
        self.assertEqual(snapshots, 2)

    def test_missing_host_is_not_misreported_as_logged_out(self):
        with self.assertRaisesRegex(desktop.DesktopWechatError, '启动协议已调用') as raised:
            self.invoke([], pid=0)
        self.assertNotIn('确认微信已经登录', str(raised.exception))


if __name__ == '__main__':
    unittest.main()
