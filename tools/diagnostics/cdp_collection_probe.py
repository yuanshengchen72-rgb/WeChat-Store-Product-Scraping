"""Real keyword-collector acceptance, isolated from user's product/checkpoint data."""
import argparse
from dataclasses import replace
import json
import logging
from pathlib import Path
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from wechat_shop_scraper.desktop_keyword import DesktopWechatBackend, SEARCH_STATE_SCRIPT, _powershell_json
from wechat_shop_scraper.desktop_builtin import ResilientShopOpener


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--collect", action="store_true")
    parser.add_argument("--inspect", action="store_true")
    parser.add_argument("--filter-probe", action="store_true")
    parser.add_argument("--native-launch-probe", action="store_true")
    parser.add_argument("--keyword", default="秋冬焦糖麂皮托特包")
    parser.add_argument("--log", default="cdp-collection-probe.log")
    args = parser.parse_args()
    output = Path(__file__).resolve().parent / args.log
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout),
                                  logging.FileHandler(output, encoding="utf-8")])
    if _powershell_json("Get-Process | Where-Object { $_.ProcessName -eq '微信小店商品列表采集' } | Select-Object Id"):
        raise RuntimeError("Collector still running; refusing a second controller")
    backend = DesktopWechatBackend(logging.info, normal_shop_opener=ResilientShopOpener())
    if args.filter_probe:
        from wechat_shop_scraper import desktop_keyword as dk
        build_script = dk._build_wmpf_script
        def with_filter_trace(profile):
            # Diagnostic only: restore the old pointer chain so the filter
            # candidate is exercised, while leaving its write operation removed.
            profile = replace(profile, scene_offsets=(64, 1536, 8, 1472, 16, 456))
            script = build_script(profile)
            script = script.replace("let scenePatched=false;", "let scenePatched=false;let probeCount=0;")
            script = script.replace(
                "onLeave(){try{const value=this.inputValue.readPointer();if(isBadPtr(value))return;const status=value.add(8);if(status.readU32()===6)status.writeU32(0);}",
                "onLeave(retval){try{if(probeCount++<12){const value=this.inputValue.readPointer();send({event:'filter-probe',input:String(this.inputValue),object:String(value),returned:String(retval)});}}")
            logging.info("FILTER_SCRIPT_HAS_TRACE=%s", "filter-probe" in script)
            return script
        dk._build_wmpf_script = with_filter_trace
        wmpf_message = backend._on_wmpf_message
        def trace_native(message, data):
            if (message.get('payload') or {}).get('event') == 'filter-probe':
                logging.info("FILTER_TRACE=%s", message['payload'])
            return wmpf_message(message,data)
        backend._on_wmpf_message = trace_native
    real_call = backend.cdp_bridge.call
    traced = 0

    def trace_call(method, params=None, **kwargs):
        nonlocal traced
        response = real_call(method, params, **kwargs)
        if method == "Runtime.evaluate" and traced < 5:
            traced += 1
            logging.info("[CDP-PROBE] raw_evaluate_reply=%s", json.dumps(response, ensure_ascii=False)[:2400])
        return response

    backend.cdp_bridge.call = trace_call
    if args.inspect:
        worker_reply = threading.Event()
        worker_values = []
        from wechat_shop_scraper.desktop_proto import decode_debug_message
        handle = backend.cdp_bridge._handle_message
        def trace_message(raw):
            message = decode_debug_message(raw)
            data = message.get("data") or {}
            if message.get("category") in {"chromeDevtools", "chromeDevtoolsResult"}:
                payload = json.loads(data.get("payload") or "{}")
                if payload.get("method") == "Target.receivedMessageFromTarget":
                    worker_values.append(payload.get("params"))
                    worker_reply.set()
                if payload.get("method") in {"Runtime.executionContextCreated", "Target.receivedMessageFromTarget"}:
                    logging.info("CONTEXT_EVENT=%s", json.dumps(payload, ensure_ascii=False)[:3000])
            elif message.get("category") != "ping":
                logging.info("CONTEXT_MESSAGE=%s", message)
            return handle(raw)
        backend.cdp_bridge._handle_message = trace_message
    try:
        backend.start()
        if args.native_launch_probe:
            if not backend.cdp_bridge.wait_runtime(1):
                backend._bootstrap_normal_shop()
                assert backend.cdp_bridge.wait_runtime(15)
            logging.info("BEFORE_NATIVE_LAUNCH=%s", backend.cdp_bridge.diagnostic_state())
            backend._launch_applet(timeout=25)
            logging.info("AFTER_NATIVE_LAUNCH=%s", backend.cdp_bridge.diagnostic_state())
            probe = "JSON.stringify({pages:typeof getCurrentPages,modules:typeof require,wx:typeof wx})"
            logging.info("NATIVE_JS=%s", backend.call("XWeb.EvaluateJavaScript", {"script":probe}, timeout=5))
            logging.info("CDP_JS=%s", backend.evaluate(probe, prefer_cdp=True))
            return
        if args.filter_probe:
            backend._open_search_page_once(timeout=25.0)
            logging.info("APPINDEX_DEBUGGER=%s", backend.cdp_bridge.call('Debugger.enable'))
            logging.info("APPINDEX_RUNTIME=%s", backend.cdp_bridge.call('Runtime.enable'))
            logging.info("EXECUTION_CONTEXTS=%s", backend.cdp_bridge._execution_contexts)
            return
        if args.inspect:
            if not backend.cdp_bridge.wait_runtime(1):
                backend._bootstrap_normal_shop()
                assert backend.cdp_bridge.wait_runtime(15)
            targets = backend.cdp_bridge.call("Target.getTargets")["result"]["targetInfos"]
            logging.info("CONTEXT_IDS=%s", backend.cdp_bridge._execution_contexts)
            expression = "JSON.stringify({sentinel:24680,pages:typeof getCurrentPages,modules:typeof require,wx:typeof wx,window:typeof window,keys:Object.keys(globalThis).filter(k=>/service|worker|context|require|wx|page/i.test(k))})"
            logging.info("APPINDEX_ENV=%s", backend.evaluate(expression, prefer_cdp=True))
            backend.cdp_bridge._target_session_id = ""
            logging.info("DEFAULT_ENV=%s", backend.cdp_bridge.call("Runtime.evaluate", {"expression":expression,"returnByValue":True}, timeout=4))
            shop = next(t for t in targets if "wxca8d4b8e8feedc2a" in t.get("url", ""))
            for target in targets:
                if target.get("type") != "worker" or target.get("browserContextId") != shop.get("browserContextId"):
                    continue
                session = backend.cdp_bridge.call("Target.attachToTarget", {"targetId":target["targetId"],"flatten":True})["result"]["sessionId"]
                backend.cdp_bridge._target_session_id = session
                try:
                    for method in ("Runtime.runIfWaitingForDebugger", "Debugger.enable", "Runtime.enable"):
                        try:
                            logging.info("WORKER_INIT %s %s",method, backend.cdp_bridge.call(method, timeout=1))
                        except (RuntimeError, TimeoutError) as exc:
                            logging.info("WORKER_INIT %s %s",method,exc)
                    for context in (target["targetId"], ""):
                        try:
                            logging.info("SHOP_WORKER_ENV target=%s context=%s value=%s", target["targetId"], context, backend.cdp_bridge.call("Runtime.evaluate", {"expression":expression,"returnByValue":True}, jscontext_id=context, timeout=1))
                        except (RuntimeError, TimeoutError) as exc:
                            logging.info("SHOP_WORKER_EVAL_ERROR %s", exc)
                except (RuntimeError, TimeoutError) as exc:
                    logging.info("SHOP_WORKER_ERROR target=%s error=%s", target["targetId"], exc)
                finally:
                    backend.cdp_bridge._target_session_id = ""
                    backend.cdp_bridge.call("Target.detachFromTarget", {"sessionId":session})
            return
        backend.open_search_page(timeout=25.0)
        value = backend.evaluate("JSON.stringify({sentinel:24680,pages:typeof getCurrentPages,modules:typeof require})", timeout=5.0)
        logging.info("SENTINEL=%s", value)
        logging.info("SEARCH_STATE=%s", backend.evaluate(SEARCH_STATE_SCRIPT, timeout=5.0))
        assert isinstance(value, dict) and value.get("sentinel") == 24680 and value.get("modules") == "function", "Real collection context unavailable"
        if args.collect:
            counts = []
            def page(cards):
                counts.append(len(cards))
                logging.info("COLLECTED page=%d products=%d", len(counts), len(cards))
                for card in cards[:1]:
                    logging.info("PRODUCT_SAMPLE=%s", repr(card)[:1500])
            result = backend.collect_keyword(args.keyword, 30, 1000, page)
            logging.info("ACCEPTANCE keyword=%s outcome=%s pages=%d products=%d", args.keyword, result, len(counts), sum(counts))
            assert result == "已完成" and sum(counts) > 0, "No real products collected"
    finally:
        backend.pause()
        backend.stop()
        logging.info("CONTROLLER_RELEASED")


if __name__ == "__main__":
    main()
