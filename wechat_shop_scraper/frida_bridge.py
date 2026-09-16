from __future__ import annotations

import queue
import re
import subprocess
import threading
import typing
from datetime import datetime
from pathlib import Path
from typing import Callable

from typing_extensions import NotRequired

from .model import ProductCard
from .parser import ProductCardParser


if not hasattr(typing, "NotRequired"):
    typing.NotRequired = NotRequired  # type: ignore[attr-defined]


LISTENER_JS = r"""
'use strict';

const ilink2 = Process.findModuleByName('ilink2.dll');
const WANT = {
  GetStoreWindowProducts: 1,
  CGetClassificationProductListV2: 1,
  CGetDefaultClassificationProductListV2: 1,
  BatchGetProducts: 1
};

function readStdStringInfo(p) {
  try {
    if (p.isNull()) return null;
    const size = Number(p.add(0x10).readU64());
    const capacity = Number(p.add(0x18).readU64());
    if (size <= 0 || size > 0x1000000) return null;
    const dataPtr = capacity < 16 ? p : p.readPointer();
    return { ptr: dataPtr, size: size };
  } catch (e) {
    return null;
  }
}

function asciiHead(ptr, n) {
  try {
    const bytes = new Uint8Array(ptr.readByteArray(n));
    let text = '';
    for (let i = 0; i < bytes.length; i++) {
      const c = bytes[i];
      text += c >= 0x20 && c <= 0x7e ? String.fromCharCode(c) : '.';
    }
    return text;
  } catch (e) {
    return '';
  }
}

function methodFrom(text) {
  const quoted = text.match(/"method":"([^"]+)"/);
  if (quoted) return quoted[1];
  for (const name in WANT) {
    if (text.indexOf(name) >= 0) return name;
  }
  return '';
}

function looksLikeListing(text, size) {
  const hasCards = text.indexOf('product_cards') >= 0;
  const hasId = /1000\d{9}/.test(text) || text.indexOf('product_id') >= 0;
  const hasTitle = text.indexOf('title') >= 0;
  const hasPrice = text.indexOf('selling_price') >= 0 || text.indexOf('market_price') >= 0;
  return hasCards || (hasId && (hasTitle || hasPrice || size >= 2500));
}

if (!ilink2) {
  send({ type: 'fatal', message: '目标进程里还没有加载 ilink2.dll' });
} else {
  const pending = [];
  let listingUntil = 0;
  let hookCount = 0;

  ilink2.enumerateExports().forEach(function (entry) {
    if (entry.name.indexOf('SendRequestAsync') >= 0 && entry.name.indexOf('NoPB') >= 0) {
      Interceptor.attach(entry.address, {
        onEnter(args) {
          const info = readStdStringInfo(args[1]);
          if (!info) return;
          const method = methodFrom(asciiHead(info.ptr, Math.min(info.size, 3000)));
          if (!WANT[method]) return;
          pending.push({ method: method, time: Date.now() });
          if (pending.length > 30) pending.shift();
          listingUntil = Date.now() + 20000;
          send({ type: 'request', method: method, size: info.size });
        }
      });
      hookCount++;
    }

    if (entry.name.indexOf('ReceiveCloudCgiResponse') >= 0 && entry.name.indexOf('NoPB') >= 0) {
      Interceptor.attach(entry.address, {
        onEnter(args) {
          this.textPtr = args[3];
          this.code = args[1].toInt32();
        },
        onLeave() {
          if (Date.now() > listingUntil) return;
          const info = readStdStringInfo(this.textPtr);
          if (!info || info.size < 100) return;
          const head = asciiHead(info.ptr, Math.min(info.size, 80000));
          if (!looksLikeListing(head, info.size)) return;
          while (pending.length && Date.now() - pending[0].time > 20000) pending.shift();
          let method = head.indexOf('product_cards') >= 0 ? 'GetStoreWindowProducts' : 'listing';
          if (pending.length) method = pending.shift().method;
          send(
            { type: 'response', method: method, code: this.code, size: info.size },
            info.ptr.readByteArray(info.size)
          );
        }
      });
      hookCount++;
    }
  });

  function hookNapi(name, encoding) {
    const flue = Process.findModuleByName('flue.dll');
    if (!flue) return;
    const address = flue.findExportByName(name);
    if (!address) return;
    Interceptor.attach(address, {
      onEnter(args) {
        if (Date.now() > listingUntil) return;
        const size = args[2].toInt32();
        if (size < 80 || size > 0x400000) return;
        let text = '';
        try {
          text = encoding === 'utf16'
            ? args[1].readUtf16String(Math.min(size, 80000))
            : args[1].readUtf8String(Math.min(size, 160000));
        } catch (e) {
          return;
        }
        if (!text || text.indexOf('product_id') < 0) return;
        if (text.indexOf('title') < 0 && text.indexOf('selling_price') < 0) return;
        send({ type: 'plain_text', via: name, text: text.slice(0, 160000) });
      }
    });
    hookCount++;
  }

  ['napi_create_string_utf8', 'napi_create_string_latin1',
   'napi_get_value_string_utf8', 'napi_get_value_string_latin1']
    .forEach(function (name) { hookNapi(name, 'utf8'); });
  ['napi_create_string_utf16', 'napi_get_value_string_utf16',
   'node_api_create_external_string_utf16']
    .forEach(function (name) { hookNapi(name, 'utf16'); });

  send({ type: 'ready', hooks: hookCount });
}
"""


LogCallback = Callable[[str], None]
ProductsCallback = Callable[[list[ProductCard]], None]
ReadyCallback = Callable[[], None]


def _wechat_process_rows() -> list[tuple[int, str]]:
    command = (
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
        "Get-CimInstance Win32_Process -Filter \"Name='WeChatAppEx.exe'\" | "
        "ForEach-Object { '{0}|{1}' -f $_.ProcessId, $_.CommandLine }"
    )
    try:
        output = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command", command],
            stderr=subprocess.STDOUT,
            timeout=12,
        ).decode("utf-8", errors="ignore")
    except (OSError, subprocess.SubprocessError):
        return []
    rows: list[tuple[int, str]] = []
    for line in output.splitlines():
        pid_text, separator, command_line = line.lstrip("\ufeff").partition("|")
        if separator and pid_text.strip().isdigit():
            rows.append((int(pid_text.strip()), command_line.strip()))
    return rows


def find_ilink_service_pid() -> int | None:
    rows = _wechat_process_rows()
    for pid, command_line in rows:
        if re.search(r"ILinkServiceHost", command_line, re.I):
            return pid
    return None


class FridaShopListener:
    def __init__(
        self,
        on_products: ProductsCallback,
        log: LogCallback | None = None,
        on_ready: ReadyCallback | None = None,
        debug_dir: str | Path | None = None,
    ) -> None:
        self.on_products = on_products
        self.log = log or (lambda _message: None)
        self.on_ready = on_ready or (lambda: None)
        self.debug_dir = Path(debug_dir) if debug_dir else None
        self.parser = ProductCardParser()
        self.session = None
        self.script = None
        self.pid: int | None = None
        self.ready = False
        self._queue: queue.Queue[tuple[bytes | str, str]] = queue.Queue()
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._debug_index = 0

    def start(self, pid: int | None = None) -> None:
        if self.session is not None:
            raise RuntimeError("监听已经在运行")
        try:
            import frida  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "缺少 frida。请先运行：python -m pip install -r requirements.txt"
            ) from exc
        self.pid = pid or find_ilink_service_pid()
        if not self.pid:
            raise RuntimeError(
                "找不到微信 ILinkServiceHost。请先打开微信小店，并停在商品列表页。"
            )
        self._stop.clear()
        self._worker = threading.Thread(target=self._parse_loop, daemon=True)
        self._worker.start()
        try:
            self.log(f"正在连接微信进程 PID {self.pid}")
            self.session = frida.attach(self.pid)
            self.script = self.session.create_script(LISTENER_JS)
            self.script.on("message", self._on_message)
            self.script.load()
        except Exception:
            self.stop()
            raise

    def stop(self) -> None:
        self.ready = False
        self._stop.set()
        if self.script is not None:
            try:
                self.script.unload()
            except Exception:
                pass
            self.script = None
        if self.session is not None:
            try:
                self.session.detach()
            except Exception:
                pass
            self.session = None
        if self._worker and self._worker.is_alive():
            self._worker.join(timeout=1.5)
        self._worker = None

    def _on_message(self, message: dict, data: bytes | None) -> None:
        if message.get("type") == "error":
            self.log("Frida 错误：" + str(message.get("stack") or message.get("description")))
            return
        payload = message.get("payload") or {}
        kind = payload.get("type")
        if kind == "fatal":
            self.log("监听失败：" + str(payload.get("message") or "未知错误"))
        elif kind == "ready":
            self.ready = True
            self.log(f"监听已就绪，共挂载 {payload.get('hooks', 0)} 个 Hook")
            self.on_ready()
        elif kind == "request":
            self.log(f"发现列表请求：{payload.get('method')}")
        elif kind == "response" and data:
            source = str(payload.get("method") or "listing")
            self._queue.put((bytes(data), source))
        elif kind == "plain_text":
            text = payload.get("text")
            if isinstance(text, str) and text:
                self._queue.put((text, "napi:" + str(payload.get("via") or "plain")))

    def _parse_loop(self) -> None:
        while not self._stop.is_set() or not self._queue.empty():
            try:
                payload, source = self._queue.get(timeout=0.3)
            except queue.Empty:
                continue
            try:
                cards = self.parser.parse(payload, source=source)
                if cards:
                    self.log(f"列表包解析出 {len(cards)} 个商品")
                    self.on_products(cards)
                elif isinstance(payload, bytes):
                    self._save_unparsed(payload, source)
                    self.log(f"收到 {len(payload)} 字节列表包，但未识别出商品，已留调试样本")
            except Exception as exc:
                self.log(f"解析列表包失败：{exc}")
            finally:
                self._queue.task_done()

    def _save_unparsed(self, payload: bytes, source: str) -> None:
        if self.debug_dir is None or self._debug_index >= 30:
            return
        try:
            self.debug_dir.mkdir(parents=True, exist_ok=True)
            self._debug_index += 1
            safe_source = re.sub(r"[^A-Za-z0-9_.-]+", "_", source)[:40] or "listing"
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            (self.debug_dir / f"{stamp}_{safe_source}_{self._debug_index:02d}.bin").write_bytes(
                payload
            )
        except OSError:
            pass
