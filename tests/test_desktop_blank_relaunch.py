"""Regression cases for an open, connected but blank applet after relaunch."""
import threading
import json
import unittest
from unittest import mock

from wechat_shop_scraper.desktop_proto import WmpfCdpBridge, decode_debug_message, wrap_cdp_payload


class BlankRelaunchTests(unittest.TestCase):
    def test_cdp_reply_matches_inner_request_id_not_transport_operation_id(self):
        for transport_id in (0, 98765):
            with self.subTest(transport_id=transport_id):
                bridge = WmpfCdpBridge()
                def send(frame):
                    request = json.loads(decode_debug_message(frame)["data"]["payload"])
                    reply = json.dumps({"id": request["id"], "result": {"value": "ok"}})
                    bridge._on_message(wrap_cdp_payload(reply, 1, transport_id))
                bridge._send_raw = send
                self.assertEqual(bridge.call("Runtime.enable", timeout=0.01)["result"], {"value": "ok"})

    def test_cdp_event_cannot_consume_pending_command(self):
        bridge = WmpfCdpBridge()
        def send(frame):
            request = json.loads(decode_debug_message(frame)["data"]["payload"])
            request_id = request["id"]
            event = json.dumps({"method": "Runtime.executionContextsCleared", "params": {}})
            bridge._on_message(wrap_cdp_payload(event, 1, request_id))
            reply = json.dumps({"id": request_id, "result": {"ack": True}})
            bridge._on_message(wrap_cdp_payload(reply, 2, request_id))
        bridge._send_raw = send
        self.assertEqual(bridge.call("Runtime.enable", timeout=0.01)["result"], {"ack": True})

    def connected_bridge(self):
        bridge = WmpfCdpBridge()
        bridge._clients = [mock.Mock()]
        bridge._connection_generation = 7
        bridge._setup_generation = 7
        bridge._setup_complete.set()
        bridge._runtime_ready.set()
        return bridge

    def test_new_setup_on_persistent_socket_unblocks_reopened_applet(self):
        bridge = self.connected_bridge()
        previous_connection = bridge.connection_generation
        bridge.prepare_relaunch()
        bridge.call = mock.Mock(return_value={"result": {}})
        # The socket remains connected, but WeChat starts a fresh JS setup.
        with mock.patch("wechat_shop_scraper.desktop_proto.decode_debug_message",
                        return_value={"category": "setupContext", "data": {}}):
            bridge._on_message(b"new setup", bridge._clients[0])
        self.assertTrue(bridge._setup_complete.wait(1))
        self.assertTrue(bridge.wait_runtime_refresh(previous_connection, 0.02),
                        "新页面已完成握手，仍仅因 TCP 连接没有更换被判为通道未就绪")

    def test_all_setup_commands_timeout_must_not_claim_ready(self):
        bridge = self.connected_bridge()
        bridge.prepare_relaunch()
        bridge.call = mock.Mock(side_effect=TimeoutError("no response"))
        bridge._initialize_debug_runtime(7)
        self.assertFalse(bridge.runtime_ready,
                         "三个初始化命令全部失败，不能报告新页面已就绪")

    def test_old_setup_worker_cannot_validate_a_new_relaunch(self):
        bridge = self.connected_bridge()
        entered, release = threading.Event(), threading.Event()
        def call(*_args, **_kwargs):
            entered.set()
            release.wait(1)
            return {"result": {}}
        bridge.call = call
        worker = threading.Thread(target=bridge._initialize_debug_runtime, args=(7,))
        worker.start()
        self.assertTrue(entered.wait(1))
        bridge.prepare_relaunch()
        release.set()
        worker.join(1)
        self.assertFalse(bridge.runtime_ready,
                         "上次页面未结束的初始化线程污染了本次重开状态")

    def test_stale_ready_flag_does_not_satisfy_relaunch_wait(self):
        bridge = self.connected_bridge()
        generation = bridge.connection_generation
        bridge.prepare_relaunch()
        bridge._setup_complete.set()
        bridge._runtime_ready.set()
        bridge._setup_generation = generation
        self.assertFalse(bridge.wait_runtime_refresh(generation, 0.02))

    def test_old_context_worker_stops_before_sending_to_new_page(self):
        bridge = self.connected_bridge()
        bridge._contexts = ["shared-context-id"]
        entered, release = threading.Event(), threading.Event()
        sent = []
        def call(method, *_args, **_kwargs):
            sent.append(method)
            entered.set()
            release.wait(1)
            return {"result": {}}
        bridge.call = call
        worker = threading.Thread(target=bridge._enable_context, args=("shared-context-id",))
        worker.start()
        self.assertTrue(entered.wait(1))
        bridge.prepare_relaunch()
        bridge._contexts = ["shared-context-id"]
        release.set()
        worker.join(1)
        self.assertEqual(sent, ["Runtime.enable"])
        self.assertFalse(bridge._enabled_contexts)


if __name__ == "__main__":
    unittest.main()
