"""Exercise the transport -> CDP envelope -> collector result boundary."""
import json
import unittest

from wechat_shop_scraper.desktop_proto import (
    WmpfCdpBridge, decode_debug_message, wrap_cdp_payload,
)


class CdpResultTests(unittest.TestCase):
    def bridge_with_reply(self, result):
        bridge = WmpfCdpBridge()
        bridge._clients.append(object())
        bridge._runtime_ready.set()
        bridge._setup_complete.set()
        bridge._contexts.append("shop-page")

        def reply_to_request(frame):
            request = json.loads(decode_debug_message(frame)["data"]["payload"])
            response = {"id": request["id"], "result": result}
            bridge._handle_message(wrap_cdp_payload(json.dumps(response), 1, request["id"]))

        bridge._send_raw = reply_to_request
        return bridge

    def test_reads_search_state_from_cdp_remote_object(self):
        state = json.dumps({"keyword": "秋冬焦糖麂皮托特包", "loading": False})
        bridge = self.bridge_with_reply({"result": {"type": "string", "value": state}})
        self.assertEqual(bridge.evaluate("readSearchState()", timeout=0.1), state)

    def test_does_not_swallow_cdp_javascript_exception(self):
        bridge = self.bridge_with_reply({
            "result": {"type": "object", "subtype": "error"},
            "exceptionDetails": {"text": "ReferenceError: require is not defined"},
        })
        with self.assertRaisesRegex(RuntimeError, "require is not defined"):
            bridge.evaluate("require('productSearchStore')", timeout=0.1)

    def test_reads_false_and_zero_without_treating_them_as_missing(self):
        for value in (False, 0, ""):
            with self.subTest(value=value):
                bridge = self.bridge_with_reply({"result": {"value": value}})
                self.assertEqual(bridge.evaluate("readValue()", timeout=0.1), value)


if __name__ == "__main__":
    unittest.main()
