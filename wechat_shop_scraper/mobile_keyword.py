from __future__ import annotations

import html
import json
import locale
import re
import socket
import subprocess
import sys
import threading
import time
import typing
import urllib.request
import xml.etree.ElementTree as ET
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Callable, Iterable

from typing_extensions import NotRequired

from .model import ProductCard


if not hasattr(typing, "NotRequired"):
    typing.NotRequired = NotRequired  # type: ignore[attr-defined]


PRODUCTS_EXPRESSION = r"""(() => {
  const clean = value => String(value || "").replace(/\s+/g, " ").trim();
  const imageFrom = card => {
    const withAttribute = card.querySelector("[imageurl]");
    if (withAttribute && withAttribute.getAttribute("imageurl")) {
      return withAttribute.getAttribute("imageurl");
    }
    const candidates = Array.from(card.querySelectorAll("[style*='background-image']"));
    for (const element of candidates) {
      const background = element.style.backgroundImage || getComputedStyle(element).backgroundImage;
      const match = background.match(/url\(["']?(.*?)["']?\)/);
      if (match && /wst\.wxapp|snscosdownload|qpic\.cn/.test(match[1])) return match[1];
    }
    return "";
  };
  return Array.from(document.querySelectorAll("[data-fcn-product-card]"))
    .map(card => {
      const report = card.getAttribute("data-report-id") || card.getAttribute("data-back-report-id") || "";
      const ids = report.match(/\b1\d{13}\b/g) || [];
      const titleNode = card.querySelector(".mask-highlight-title__inner, .product-card__title");
      const salesText = clean(card.querySelector(".product-card__sales")?.innerText);
      const salesMatch = salesText.match(/(\d+(?:\.\d+)?)\s*([万千]?)/) || [];
      return {
        product_id: ids[0] || "",
        title: clean(titleNode?.innerText),
        main_image_url: imageFrom(card),
        price: clean(card.querySelector(".product-card__price-display")?.innerText),
        sales_text: salesText,
        sales_number: salesMatch[1] || "",
        sales_unit: salesMatch[2] || "",
        shop_name: clean(card.querySelector(".ui-source-title, .source-name")?.innerText),
        kind: card.closest(".live-box") ? "直播商品" : "商品"
      };
    })
    .filter(item => item.product_id && item.title && item.main_image_url);
})()"""

KEYWORD_EXPRESSION = r"""(() => {
  const state = window.Global || {};
  const stable = state.stableField || {};
  return {
    keyword: String(
    stable.displayQuery ||
    stable.query ||
    state.query ||
    state.returnQuery ||
    state.previousQuery ||
    ""
    ).trim(),
    search_id: String(stable.searchId || state.searchId || "").trim()
  };
})()"""

PAGE_STATE_EXPRESSION = r"""(() => {
  const clean = value => String(value || "").replace(/\s+/g, " ").trim();
  const visible = element => {
    if (!element) return false;
    const box = element.getBoundingClientRect();
    const style = getComputedStyle(element);
    return box.width > 0 && box.height > 0 && style.display !== "none" &&
      style.visibility !== "hidden" && style.opacity !== "0";
  };
  const point = element => {
    if (!visible(element)) return null;
    const box = element.getBoundingClientRect();
    return {x: box.left + box.width / 2, y: box.top + box.height / 2};
  };
  const state = window.Global || {};
  const stable = state.stableField || {};
  const keyword = String(
    stable.displayQuery || stable.query || state.query || state.returnQuery ||
    state.previousQuery || ""
  ).trim();
  const scroller = document.scrollingElement || document.documentElement;
  const productTab = Array.from(document.querySelectorAll(".unit__item"))
    .find(element => {
      const title = element.querySelector(".unit__item-title");
      return visible(element) && clean(title?.innerText) === "商品";
    });
  const filterToggle = document.querySelector(".advanced-search__more-info");
  const goodReviewInput = document.querySelector(
    'input[name="FinderProductAdvanceSearchServiceRecommendFilter"][value="3"]'
  );
  const goodReviewLabel = goodReviewInput?.closest("label");
  const priceFields = Array.from(document.querySelectorAll(
    ".filter[data-fcn-input-range] .range__input, .range .range__input"
  )).slice(0, 2);
  const keyboardKeys = Array.from(document.querySelectorAll("[data-key]"))
    .filter(element => visible(element));
  const keyboardDigits = {};
  for (const element of keyboardKeys) {
    const key = element.getAttribute("data-key") || "";
    if (/^\d$/.test(key)) keyboardDigits[key] = point(element);
  }
  const keyboardConfirm = keyboardKeys.find(
    element => element.getAttribute("data-key") === "confirm"
  );
  const keyboardDelete = Array.from(document.querySelectorAll(".btn.del"))
    .find(element => visible(element));
  const confirmButton = Array.from(document.querySelectorAll("a.weui-btn_primary"))
    .find(element => visible(element) && clean(element.innerText) === "确定");
  const bodyText = clean(document.body?.innerText);
  const tail = bodyText.slice(-1000);
  const riskMarkers = [
    "安全验证", "操作频繁", "访问过于频繁", "请完成验证", "登录后继续",
    "请先登录", "网络错误", "页面加载失败"
  ];
  const emptyMarkers = ["暂无相关商品", "没有找到相关商品", "暂无搜索结果"];
  const endMarkers = ["没有更多了", "没有更多商品", "已经到底了", "暂无更多"];
  return {
    keyword,
    search_id: String(stable.searchId || state.searchId || "").trim(),
    product_count: document.querySelectorAll("[data-fcn-product-card]").length,
    scroll_top: Math.round(scroller?.scrollTop || 0),
    scroll_height: Math.round(scroller?.scrollHeight || 0),
    client_height: Math.round(scroller?.clientHeight || 0),
    at_bottom: Boolean(scroller &&
      scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 8),
    explicit_end: endMarkers.some(marker => tail.includes(marker)),
    empty_result: emptyMarkers.some(marker => bodyText.includes(marker)),
    risk_marker: riskMarkers.find(marker => bodyText.includes(marker)) || "",
    product_tab_available: Boolean(productTab),
    product_tab_selected: Boolean(productTab?.classList.contains("selected")),
    product_tab_point: point(productTab),
    filter_panel_open: Boolean(confirmButton && visible(confirmButton)),
    good_review_available: Boolean(goodReviewLabel && visible(goodReviewLabel)),
    good_review_checked: Boolean(goodReviewInput?.checked),
    filter_toggle_point: point(filterToggle),
    good_review_point: point(goodReviewLabel),
    confirm_point: point(confirmButton),
    min_price_text: clean(priceFields[0]?.innerText),
    max_price_text: clean(priceFields[1]?.innerText),
    min_price_active: Boolean(priceFields[0]?.classList.contains("active")),
    max_price_active: Boolean(priceFields[1]?.classList.contains("active")),
    min_price_point: point(priceFields[0]),
    max_price_point: point(priceFields[1]),
    keyboard_open: Boolean(keyboardConfirm && visible(keyboardConfirm)),
    keyboard_digits: keyboardDigits,
    keyboard_delete_point: point(keyboardDelete),
    keyboard_confirm_point: point(keyboardConfirm)
  };
})()"""

FILTER_CONFIRM_EXPRESSION = r"""(() => {
  const clean = value => String(value || "").replace(/\s+/g, " ").trim();
  const button = Array.from(document.querySelectorAll("a.weui-btn_primary"))
    .find(element => clean(element.innerText) === "确定");
  if (!button) return false;
  button.click();
  return true;
})()"""

GOOD_REVIEW_CLICK_EXPRESSION = r"""(() => {
  const input = document.querySelector(
    'input[name="FinderProductAdvanceSearchServiceRecommendFilter"][value="3"]'
  );
  if (!input) return false;
  input.click();
  return true;
})()"""

SCROLL_PRICE_FIELD_EXPRESSION = r"""(() => {
  const index = __FIELD_INDEX__;
  const fields = Array.from(document.querySelectorAll(
    ".filter[data-fcn-input-range] .range__input, .range .range__input"
  ));
  const field = fields[index];
  const wrapper = field?.closest(".ui-content-wrapper-scroll");
  if (!field || !wrapper) return false;
  const fieldBox = field.getBoundingClientRect();
  const wrapperBox = wrapper.getBoundingClientRect();
  if (fieldBox.bottom > wrapperBox.bottom || fieldBox.top < wrapperBox.top) {
    const offset = fieldBox.bottom > wrapperBox.bottom
      ? fieldBox.bottom - wrapperBox.bottom + 12
      : fieldBox.top - wrapperBox.top - 12;
    wrapper.scrollTop = Math.max(
      0,
      Math.min(wrapper.scrollHeight - wrapper.clientHeight, wrapper.scrollTop + offset)
    );
    wrapper.dispatchEvent(new Event("scroll", {bubbles: true}));
  }
  return true;
})()"""

PRICE_FIELD_CLICK_EXPRESSION = r"""(() => {
  const index = __FIELD_INDEX__;
  const fields = Array.from(document.querySelectorAll(
    ".filter[data-fcn-input-range] .range__input, .range .range__input"
  ));
  const field = fields[index];
  if (!field) return false;
  field.click();
  return true;
})()"""

RESET_SCROLL_EXPRESSION = r"""(() => {
  const scroller = document.scrollingElement || document.documentElement;
  if (!scroller) return false;
  scroller.scrollTop = 0;
  window.scrollTo(0, 0);
  scroller.dispatchEvent(new Event("scroll", {bubbles: true}));
  return true;
})()"""

SCROLL_NEXT_EXPRESSION = r"""(() => {
  const scroller = document.scrollingElement || document.documentElement;
  if (!scroller) return null;
  const before = scroller.scrollTop;
  const step = Math.max(420, Math.floor(scroller.clientHeight * 0.82));
  scroller.scrollTop = Math.min(
    scroller.scrollHeight - scroller.clientHeight,
    scroller.scrollTop + step
  );
  window.scrollTo(0, scroller.scrollTop);
  scroller.dispatchEvent(new Event("scroll", {bubbles: true}));
  return {
    before: Math.round(before),
    after: Math.round(scroller.scrollTop),
    height: Math.round(scroller.scrollHeight),
    client: Math.round(scroller.clientHeight)
  };
})()"""


LogCallback = Callable[[str], None]
ProductsCallback = Callable[[list[ProductCard]], None]
ReadyCallback = Callable[[], None]
KeywordCallback = Callable[[str], None]
ProgressCallback = Callable[[str, int, int, str], None]
CompleteCallback = Callable[[], None]
ErrorCallback = Callable[[str], None]
KeywordFinishedCallback = Callable[[str, int, int, str], None]

CONNECTION_USB = "USB数据线"
CONNECTION_WIFI = "WiFi无线"
EXPECTED_FRIDA_VERSION = "17.17.0"
FRIDA_SERVER_REMOTE_PATH = f"/data/local/tmp/frida-server-{EXPECTED_FRIDA_VERSION}"
FRIDA_SERVER_START_TIMEOUT = 8.0
FRIDA_SETUP_VERIFY_PORT = 27044
FRIDA_SERVER_ASSET_NAMES = {
    "arm64-v8a": f"frida-server-{EXPECTED_FRIDA_VERSION}-android-arm64",
}
KEYWORD_STABLE_SECONDS = 2.0
RESULT_WAIT_TIMEOUT_SECONDS = 60.0
KEYWORD_RUN_TIMEOUT_SECONDS = 30.0 * 60.0
SCROLL_INTERVAL_SECONDS = 0.6
END_STABLE_ROUNDS = 6
KEYWORD_RETRY_SECONDS = 60.0
MAX_KEYWORD_RETRIES = 3
MAX_CONSECUTIVE_FAILED_KEYWORDS = 2
DEFAULT_WECHAT_RESTART_KEYWORD_INTERVAL = 30
MAX_WECHAT_INSTANCES = 6
SEARCH_HEALTHCHECK_KEYWORDS = ("手机", "女装", "鞋子")
PAGE_REENTRY_MAX_ATTEMPTS = 3
PAGE_REENTRY_RETRY_SECONDS = 5.0
PAGE_REENTRY_ACTIVITY_TIMEOUT_SECONDS = 15.0
DEBUG_FORWARD_RECOVERY_ATTEMPTS = 3
DEBUG_FORWARD_RECOVERY_DELAY_SECONDS = 0.25
WECHAT_COLD_START_SETTLE_SECONDS = 8.0
WECHAT_SEARCH_SETTLE_SECONDS = 3.0
BOOTSTRAP_SUBMIT_MAX_ATTEMPTS = 10
BOOTSTRAP_SUBMIT_RETRY_SECONDS = 1.0
WECHAT_LAUNCHER_ACTIVITY = "com.tencent.mm.ui.LauncherUI"
WECHAT_SHOP_ACTIVITY = "com.tencent.mm.plugin.appbrand.ui.AppBrandPluginUI"
WECHAT_SHOP_ACTIVITIES = {
    WECHAT_SHOP_ACTIVITY,
    f"{WECHAT_SHOP_ACTIVITY}1",
}
WECHAT_SEARCH_ACTIVITY = (
    "com.tencent.mm.plugin.webview.ui.tools.fts.MMFTSSOSHomeWebViewUI"
)


class WechatSearchUnavailable(RuntimeError):
    """The active WeChat instance can no longer return search results."""


def _asset_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS")) / "wechat_shop_scraper" / "assets"
    return Path(__file__).resolve().parent / "assets"


def _creation_flags() -> int:
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _available_local_port(preferred: int) -> int:
    if not 0 <= preferred <= 65535:
        raise ValueError("本机端口必须在 0 到 65535 之间")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", preferred))
        except OSError:
            probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _run_adb_binary(
    adb: Path, *arguments: str, timeout: float = 15
) -> str:
    completed = subprocess.run(
        [str(adb), *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
        creationflags=_creation_flags(),
    )
    output = completed.stdout.decode("utf-8", errors="replace").strip()
    if "�" in output:
        output = completed.stdout.decode(
            locale.getpreferredencoding(False), errors="replace"
        ).strip()
    if completed.returncode:
        raise RuntimeError(output or f"ADB 失败：{' '.join(arguments)}")
    return output


def normalize_wifi_address(value: str) -> str:
    address = value.strip().replace("：", ":")
    if not address:
        raise RuntimeError("请填写手机 WiFi 地址，例如 192.168.1.23:5555")
    if ":" not in address:
        address += ":5555"
    host, separator, port_text = address.rpartition(":")
    if not separator or not host or not port_text.isdigit():
        raise RuntimeError("WiFi 地址格式不正确，例如 192.168.1.23:5555")
    port = int(port_text)
    if not 1 <= port <= 65535:
        raise RuntimeError("WiFi 地址端口必须在 1 到 65535 之间")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", host):
        raise RuntimeError("WiFi 地址格式不正确，例如 192.168.1.23:5555")
    return f"{host}:{port}"


def _usb_serials(devices_output: str) -> list[str]:
    serials: list[str] = []
    for line in devices_output.splitlines()[1:]:
        columns = line.strip().split()
        if len(columns) >= 2 and columns[1] == "device" and ":" not in columns[0]:
            serials.append(columns[0])
    return serials


def _wifi_ip_from_route(route_output: str) -> str:
    for line in route_output.splitlines():
        device_match = re.search(r"\bdev\s+([^\s]+)", line)
        if not device_match:
            continue
        device = device_match.group(1).lower()
        if not (device.startswith("wlan") or device.startswith("wifi")):
            continue
        address_match = re.search(
            r"\bsrc\s+(\d{1,3}(?:\.\d{1,3}){3})\b", line
        )
        if not address_match:
            continue
        address = address_match.group(1)
        octets = [int(part) for part in address.split(".")]
        if all(0 <= item <= 255 for item in octets) and not address.startswith("127."):
            return address
    raise RuntimeError("无法自动取得手机 WiFi 地址；请确认手机已经连接 WiFi")


def configure_adb_wifi(port: int = 5555) -> str:
    """Use the currently attached USB phone to enable and connect ADB over Wi-Fi."""

    adb = _asset_dir() / "platform-tools" / "adb.exe"
    if not adb.is_file():
        raise RuntimeError(f"便携版缺少 ADB：{adb}")
    devices = _run_adb_binary(adb, "devices")
    serials = _usb_serials(devices)
    if not serials:
        raise RuntimeError("没有找到 USB 调试手机；请先用数据线连接并允许 USB 调试")
    if len(serials) > 1:
        raise RuntimeError("检测到多台 USB 手机；请只保留要启用 WiFi 的一台")
    serial = serials[0]
    route = _run_adb_binary(adb, "-s", serial, "shell", "ip", "route")
    address = f"{_wifi_ip_from_route(route)}:{port}"
    _run_adb_binary(adb, "-s", serial, "tcpip", str(port), timeout=20)
    time.sleep(0.8)
    connected = _run_adb_binary(adb, "connect", address, timeout=20)
    if "connected" not in connected.lower():
        raise RuntimeError(connected or f"无法连接 {address}")
    _run_adb_binary(adb, "-s", address, "get-state", timeout=10)
    return address


def _frida_server_asset_for_abi(abi: str) -> Path:
    normalized = abi.strip().lower()
    asset_name = FRIDA_SERVER_ASSET_NAMES.get(normalized)
    if not asset_name:
        supported = "、".join(sorted(FRIDA_SERVER_ASSET_NAMES))
        raise RuntimeError(
            f"手机 CPU 架构 {abi or '未知'} 暂不支持；当前便携版支持：{supported}"
        )
    asset = _asset_dir() / asset_name
    if not asset.is_file():
        raise RuntimeError(f"便携版缺少手机 Frida Server 文件：{asset.name}")
    return asset


def _remote_frida_pid(adb: Path, serial: str) -> str:
    process_name = Path(FRIDA_SERVER_REMOTE_PATH).name
    try:
        output = _run_adb_binary(
            adb,
            "-s",
            serial,
            "shell",
            "su",
            "-c",
            f"pidof {process_name}",
            timeout=5,
        )
    except RuntimeError:
        return ""
    return " ".join(item for item in output.split() if item.isdigit())


def _stop_other_frida_servers(adb: Path, serial: str) -> None:
    expected_name = Path(FRIDA_SERVER_REMOTE_PATH).name
    try:
        output = _run_adb_binary(
            adb, "-s", serial, "shell", "ps", "-A", timeout=8
        )
    except RuntimeError:
        return
    for line in output.splitlines():
        columns = line.split()
        if len(columns) < 3:
            continue
        process_name = columns[-1]
        pid = columns[1]
        if (
            process_name.startswith("frida-server")
            and process_name != expected_name
            and pid.isdigit()
        ):
            _run_adb_binary(
                adb,
                "-s",
                serial,
                "shell",
                "su",
                "-c",
                f"kill {pid}",
                timeout=8,
            )


def _verify_remote_frida(adb: Path, serial: str) -> None:
    try:
        import frida  # type: ignore
    except ImportError as exc:
        raise RuntimeError("便携版缺少 Frida 电脑端组件") from exc
    actual_version = str(getattr(frida, "__version__", "未知"))
    if actual_version != EXPECTED_FRIDA_VERSION:
        raise RuntimeError(
            f"软件内置 Frida 版本为 {actual_version}，要求 {EXPECTED_FRIDA_VERSION}"
        )

    verify_port = _available_local_port(FRIDA_SETUP_VERIFY_PORT)
    local_endpoint = f"127.0.0.1:{verify_port}"
    try:
        try:
            _run_adb_binary(
                adb,
                "-s",
                serial,
                "forward",
                "--remove",
                f"tcp:{verify_port}",
                timeout=5,
            )
        except RuntimeError:
            pass
        _run_adb_binary(
            adb,
            "-s",
            serial,
            "forward",
            f"tcp:{verify_port}",
            "tcp:27042",
            timeout=8,
        )
        manager = frida.get_device_manager()
        device = manager.add_remote_device(local_endpoint)
        try:
            if not device.enumerate_processes():
                raise RuntimeError("Frida Server 没有返回手机进程")
        finally:
            try:
                manager.remove_remote_device(local_endpoint)
            except Exception:
                pass
    except Exception as exc:
        raise RuntimeError(
            f"Frida Server {EXPECTED_FRIDA_VERSION} 已启动，但通信验证失败"
        ) from exc
    finally:
        try:
            _run_adb_binary(
                adb,
                "-s",
                serial,
                "forward",
                "--remove",
                f"tcp:{verify_port}",
                timeout=5,
            )
        except RuntimeError:
            pass


def setup_frida_server_via_usb(log: LogCallback | None = None) -> str:
    """Install, start and verify the bundled Frida Server on one USB phone."""

    write_log = log or (lambda _message: None)
    adb = _asset_dir() / "platform-tools" / "adb.exe"
    if not adb.is_file():
        raise RuntimeError(f"便携版缺少 ADB：{adb}")
    devices = _run_adb_binary(adb, "devices")
    serials = _usb_serials(devices)
    if not serials:
        raise RuntimeError("没有找到 USB 调试手机；请连接数据线并允许 USB 调试")
    if len(serials) > 1:
        raise RuntimeError("检测到多台 USB 手机；请只保留要配置的一台")
    serial = serials[0]
    model = _run_adb_binary(
        adb, "-s", serial, "shell", "getprop", "ro.product.model", timeout=8
    ).strip()
    abi = _run_adb_binary(
        adb, "-s", serial, "shell", "getprop", "ro.product.cpu.abi", timeout=8
    ).strip()
    write_log(f"已识别手机：{model or serial}；CPU 架构：{abi or '未知'}")
    server_asset = _frida_server_asset_for_abi(abi)

    try:
        root_status = _run_adb_binary(
            adb, "-s", serial, "shell", "su", "-c", "id", timeout=15
        )
    except RuntimeError as exc:
        raise RuntimeError(
            "无法取得手机 Root 权限；请解锁手机并在 Magisk 中允许 Shell Root 权限"
        ) from exc
    if "uid=0" not in root_status:
        raise RuntimeError(
            "手机没有授予 Shell Root 权限；请打开 Magisk 并允许 Shell Root 权限"
        )
    write_log("手机 Root 权限检查通过")

    try:
        remote_details = _run_adb_binary(
            adb,
            "-s",
            serial,
            "shell",
            "su",
            "-c",
            f"ls -l {FRIDA_SERVER_REMOTE_PATH}",
            timeout=8,
        )
    except RuntimeError:
        remote_details = ""
    remote_is_current = (
        FRIDA_SERVER_REMOTE_PATH in remote_details
        and str(server_asset.stat().st_size) in remote_details
    )
    if remote_is_current:
        write_log(f"手机已安装 Frida Server {EXPECTED_FRIDA_VERSION}，无需重复上传")
    else:
        temporary_path = f"{FRIDA_SERVER_REMOTE_PATH}.installing"
        write_log(
            f"正在上传 Frida Server {EXPECTED_FRIDA_VERSION}（约 "
            f"{server_asset.stat().st_size // (1024 * 1024)} MB）…"
        )
        _run_adb_binary(
            adb,
            "-s",
            serial,
            "push",
            str(server_asset),
            temporary_path,
            timeout=180,
        )
        _run_adb_binary(
            adb,
            "-s",
            serial,
            "shell",
            "su",
            "-c",
            f"mv -f {temporary_path} {FRIDA_SERVER_REMOTE_PATH}",
            timeout=15,
        )
        write_log("Frida Server 上传完成")

    _run_adb_binary(
        adb,
        "-s",
        serial,
        "shell",
        "su",
        "-c",
        f"chmod 755 {FRIDA_SERVER_REMOTE_PATH}",
        timeout=8,
    )
    pid = _remote_frida_pid(adb, serial)
    if not pid:
        _stop_other_frida_servers(adb, serial)
        log_path = f"{FRIDA_SERVER_REMOTE_PATH}.log"
        write_log(f"正在启动 Frida Server {EXPECTED_FRIDA_VERSION}…")
        _run_adb_binary(
            adb,
            "-s",
            serial,
            "shell",
            "su",
            "-c",
            (
                f"nohup {FRIDA_SERVER_REMOTE_PATH} >{log_path} 2>&1 "
                "</dev/null &"
            ),
            timeout=10,
        )
        deadline = time.monotonic() + FRIDA_SERVER_START_TIMEOUT
        while time.monotonic() < deadline:
            time.sleep(0.25)
            pid = _remote_frida_pid(adb, serial)
            if pid:
                break
    if not pid:
        raise RuntimeError(
            f"Frida Server 自动启动失败；请检查 Magisk 授权和 "
            f"{FRIDA_SERVER_REMOTE_PATH}.log"
        )
    write_log(f"Frida Server 已运行（PID {pid}），正在验证通信…")
    _verify_remote_frida(adb, serial)
    return (
        f"{model or serial}（{abi}）配置成功；"
        f"Frida Server {EXPECTED_FRIDA_VERSION} 已就绪"
    )


def _price_to_fen(value: Any) -> tuple[int | None, str]:
    text = str(value or "").strip()
    match = re.search(r"\d+(?:\.\d+)?", text.replace(",", ""))
    if not match:
        return None, text
    try:
        fen = int(
            (Decimal(match.group(0)) * 100).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
        )
    except InvalidOperation:
        return None, text
    return fen, text


def _sales_from_row(row: dict[str, Any]) -> tuple[int | None, bool]:
    sales_text = str(row.get("sales_text") or "").strip()
    sales_number = str(row.get("sales_number") or "").strip()
    if not sales_text or not sales_number:
        return None, False
    try:
        number = Decimal(sales_number)
    except InvalidOperation:
        return None, False
    unit = str(row.get("sales_unit") or "")
    if unit == "万":
        number *= 10000
    elif unit == "千":
        number *= 1000
    exact = not unit and "+" not in str(row.get("sales_text") or "")
    return int(number.quantize(Decimal("1"), rounding=ROUND_HALF_UP)), exact


def cards_from_dom(rows: Iterable[Any], keyword: str) -> list[ProductCard]:
    cards: dict[str, ProductCard] = {}
    for raw in rows:
        if not isinstance(raw, dict) or raw.get("kind") != "商品":
            continue
        product_id = str(raw.get("product_id") or "").strip()
        title = html.unescape(str(raw.get("title") or "")).strip()
        image = html.unescape(str(raw.get("main_image_url") or "")).strip()
        if not re.fullmatch(r"1\d{13}", product_id) or not title or not image:
            continue
        price_fen, price_text = _price_to_fen(raw.get("price"))
        sales, sales_exact = _sales_from_row(raw)
        cards[product_id] = ProductCard(
            product_id=product_id,
            keyword=keyword.strip(),
            title=title,
            main_image_url=image,
            price_fen=price_fen,
            price_text=price_text,
            sales=sales,
            sales_text=str(raw.get("sales_text") or "").strip(),
            sales_exact=sales_exact,
            shop_name=html.unescape(str(raw.get("shop_name") or "")).strip(),
            source="安卓微信关键词搜索",
        )
    return list(cards.values())


class MobileKeywordListener:
    """Poll ordinary product cards from the active Android WeChat search WebView."""

    def __init__(
        self,
        keyword: str,
        on_products: ProductsCallback,
        log: LogCallback | None = None,
        on_ready: ReadyCallback | None = None,
        on_keyword: KeywordCallback | None = None,
        keywords: Iterable[str] | None = None,
        on_progress: ProgressCallback | None = None,
        on_complete: CompleteCallback | None = None,
        on_error: ErrorCallback | None = None,
        on_keyword_finished: KeywordFinishedCallback | None = None,
        start_index: int = 0,
        min_price: int | None = None,
        max_price: int | None = None,
        restart_keyword_interval: int = DEFAULT_WECHAT_RESTART_KEYWORD_INTERVAL,
        poll_seconds: float = 1.0,
        port: int = 9223,
        connection: str = CONNECTION_USB,
        device_address: str = "",
        frida_port: int = 27043,
        keyword_stable_seconds: float = KEYWORD_STABLE_SECONDS,
    ) -> None:
        self.keyword = keyword.strip()
        self.on_products = on_products
        self.log = log or (lambda _message: None)
        self.on_ready = on_ready or (lambda: None)
        self.on_keyword = on_keyword or (lambda _keyword: None)
        provided_keywords = keywords if keywords is not None else ()
        self.keywords: list[str] = []
        for item in provided_keywords:
            normalized = str(item).strip()
            if normalized and normalized not in self.keywords:
                self.keywords.append(normalized)
        if not 0 <= start_index <= len(self.keywords):
            raise ValueError("关键词起始位置超出列表范围")
        self.start_index = start_index
        self.on_progress = on_progress or (
            lambda _keyword, _index, _total, _stage: None
        )
        self.on_complete = on_complete or (lambda: None)
        self.on_error = on_error or (lambda _message: None)
        self.on_keyword_finished = on_keyword_finished or (
            lambda _keyword, _index, _total, _outcome: None
        )
        self.min_price = min_price
        self.max_price = max_price
        if self.min_price is not None and self.min_price < 0:
            raise ValueError("最低价不能小于 0")
        if self.max_price is not None and self.max_price < 0:
            raise ValueError("最高价不能小于 0")
        if (
            self.min_price is not None
            and self.max_price is not None
            and self.min_price > self.max_price
        ):
            raise ValueError("最低价不能大于最高价")
        if (
            isinstance(restart_keyword_interval, bool)
            or not isinstance(restart_keyword_interval, int)
            or restart_keyword_interval < 0
        ):
            raise ValueError("微信重启间隔必须是非负整数")
        self.restart_keyword_interval = restart_keyword_interval
        self.poll_seconds = poll_seconds
        self.port = port
        self.connection = connection
        self.device_address = device_address.strip()
        self.frida_port = frida_port
        self.keyword_stable_seconds = max(0.0, keyword_stable_seconds)
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._socket: Any = None
        self._next_id = 1
        self._seen: dict[str, tuple[Any, ...]] = {}
        self._adb = _asset_dir() / "platform-tools" / "adb.exe"
        self._bundle = _asset_dir() / "mobile_search_automation.bundle.js"
        self._launcher_bundle = _asset_dir() / "launcher_user_switch.bundle.js"
        self._native_ui_bundle = _asset_dir() / "native_ui_locator.bundle.js"
        self._wechat_pid = 0
        self._wechat_instances: list[tuple[int, str]] = [(0, "微信")]
        self._wechat_instance_index = 0
        self._wechat_switch_count = 0
        self._adb_serial = ""
        self._frida_remote_endpoint = ""
        self._candidate_context: tuple[str, str] = ("", "")
        self._candidate_since = 0.0
        self._keyword_confirmed = False
        self._last_committed_result_signature: tuple[Any, ...] | None = None
        self._pending_bootstrap_keyword = ""
        self._frida_manager: Any = None
        self._frida_device: Any = None
        self._frida_session: Any = None
        self._automation_script: Any = None

    def start(self) -> None:
        if self._worker and self._worker.is_alive():
            raise RuntimeError("关键词监听已经在运行")
        if not self._adb.is_file():
            raise RuntimeError(f"便携版缺少 ADB：{self._adb}")
        if not self._bundle.is_file():
            raise RuntimeError(f"便携版缺少手机监听脚本：{self._bundle}")
        if not self._launcher_bundle.is_file():
            raise RuntimeError(f"便携版缺少微信分身启动脚本：{self._launcher_bundle}")
        if not self._native_ui_bundle.is_file():
            raise RuntimeError(f"便携版缺少微信界面定位脚本：{self._native_ui_bundle}")
        self._stop.clear()
        self._prepare_adb_connection()
        self._discover_wechat_instances()
        self._ensure_frida_server()
        self._enable_webview_debug()
        self._forward_debug_socket()
        worker_target = self._automation_loop if self.keywords else self._poll_loop
        self._worker = threading.Thread(target=worker_target, daemon=True)
        self._worker.start()

    def stop(self) -> None:
        self._stop.set()
        self._drop_socket()
        if self._worker and self._worker.is_alive():
            self._worker.join(timeout=2.0)
        self._worker = None
        self._release_wechat_runtime(remove_remote=True)
        try:
            self._run_adb("forward", "--remove", f"tcp:{self.port}", timeout=5)
        except Exception:
            pass

    def _release_wechat_runtime(self, remove_remote: bool = False) -> None:
        script = self._automation_script
        self._automation_script = None
        if script is not None:
            try:
                script.unload()
            except Exception:
                pass
        session = self._frida_session
        self._frida_session = None
        if session is not None:
            try:
                session.detach()
            except Exception:
                pass
        self._frida_device = None
        self._wechat_pid = 0
        if remove_remote and self._frida_remote_endpoint:
            try:
                self._run_adb(
                    "forward", "--remove", f"tcp:{self.frida_port}", timeout=5
                )
            except Exception:
                pass
            try:
                manager = self._frida_manager
                if manager is None:
                    import frida  # type: ignore

                    manager = frida.get_device_manager()
                manager.remove_remote_device(self._frida_remote_endpoint)
            except Exception:
                pass
            self._frida_remote_endpoint = ""
        if remove_remote:
            self._frida_manager = None

    def _run_adb(self, *arguments: str, timeout: float = 15) -> str:
        target = ("-s", self._adb_serial) if self._adb_serial else ()
        return _run_adb_binary(self._adb, *target, *arguments, timeout=timeout)

    def _forward_to_available_local_port(
        self,
        preferred: int,
        remote: str,
        label: str,
    ) -> int:
        candidate = preferred
        last_error: RuntimeError | None = None
        for attempt in range(3):
            candidate = _available_local_port(candidate if attempt == 0 else 0)
            try:
                self._run_adb(
                    "forward", f"tcp:{candidate}", remote, timeout=10
                )
            except RuntimeError as exc:
                last_error = exc
                if "cannot bind" not in str(exc).lower():
                    raise
                continue
            if preferred > 0 and candidate != preferred:
                self.log(
                    f"本机端口 {preferred} 已被占用；{label}自动改用 {candidate}"
                )
            return candidate
        raise RuntimeError(
            f"无法为{label}找到可用的本机端口"
        ) from last_error

    def _prepare_adb_connection(self) -> None:
        if self.connection == CONNECTION_WIFI:
            address = normalize_wifi_address(self.device_address)
            output = _run_adb_binary(self._adb, "connect", address, timeout=20)
            if "connected" not in output.lower():
                raise RuntimeError(output or f"无法通过 WiFi 连接手机 {address}")
            self._adb_serial = address
            self._run_adb("get-state", timeout=10)
            self.log(f"ADB 已通过 WiFi 连接 {address}")
            return
        devices = _run_adb_binary(self._adb, "devices")
        serials = _usb_serials(devices)
        if not serials:
            raise RuntimeError("没有找到 USB 调试手机；请连接数据线并允许 USB 调试")
        if len(serials) > 1:
            raise RuntimeError("检测到多台 USB 手机；请只保留要采集的一台")
        self._adb_serial = serials[0]

    def _frida_server_pid(self) -> str:
        process_name = Path(FRIDA_SERVER_REMOTE_PATH).name
        try:
            output = self._run_adb(
                "shell", "su", "-c", f"pidof {process_name}", timeout=5
            )
        except RuntimeError:
            return ""
        return " ".join(item for item in output.split() if item.isdigit())

    @property
    def _wechat_user_id(self) -> int:
        return self._wechat_instances[self._wechat_instance_index][0]

    @property
    def _wechat_instance_name(self) -> str:
        return self._wechat_instances[self._wechat_instance_index][1]

    @staticmethod
    def _process_android_user_id(process_user: str) -> int | None:
        match = re.fullmatch(r"u(\d+)_.+", process_user.strip())
        if match:
            return int(match.group(1))
        if process_user.isdecimal():
            numeric_uid = int(process_user)
            if numeric_uid >= 100000:
                return numeric_uid // 100000
        return None

    def _discover_wechat_instances(self) -> None:
        output = self._run_adb("shell", "pm", "list", "users", timeout=10)
        user_ids = [
            int(value)
            for value in re.findall(r"UserInfo\{(\d+):", output)
        ]
        if not user_ids:
            user_ids = [0]

        installed_ids: list[int] = []
        for user_id in sorted(set(user_ids), key=lambda value: (value != 0, value)):
            try:
                packages = self._run_adb(
                    "shell",
                    "pm",
                    "list",
                    "packages",
                    "--user",
                    str(user_id),
                    "com.tencent.mm",
                    timeout=10,
                )
            except RuntimeError:
                continue
            if any(
                line.strip() == "package:com.tencent.mm"
                for line in packages.splitlines()
            ):
                installed_ids.append(user_id)

        if not installed_ids:
            installed_ids = [0]
        installed_ids = installed_ids[:MAX_WECHAT_INSTANCES]
        clone_number = 0
        named_instances: list[tuple[int, str]] = []
        for user_id in installed_ids:
            if user_id == 0:
                name = "微信"
            else:
                clone_number += 1
                name = f"微信{clone_number}"
            named_instances.append((user_id, name))

        try:
            foreground_user, foreground_activity = (
                self._foreground_activity_context()
            )
        except RuntimeError:
            foreground_user, foreground_activity = None, ""
        if (
            foreground_user is not None
            and foreground_activity.startswith("com.tencent.mm.")
        ):
            active_position = next(
                (
                    index
                    for index, (user_id, _name) in enumerate(named_instances)
                    if user_id == foreground_user
                ),
                0,
            )
            named_instances = (
                named_instances[active_position:]
                + named_instances[:active_position]
            )

        self._wechat_instances = named_instances
        self._wechat_instance_index = 0
        self._wechat_switch_count = 0
        instance_text = "、".join(
            f"{name}（用户 {user_id}）"
            for user_id, name in self._wechat_instances
        )
        if len(self._wechat_instances) > 1:
            self.log(
                f"已自动识别 {len(self._wechat_instances)} 个微信实例："
                f"{instance_text}；搜索失效时将循环切换，最多支持 "
                f"{MAX_WECHAT_INSTANCES} 个"
            )
        else:
            self.log("当前手机仅识别到 1 个微信实例")

    def _current_wechat_pid(self) -> int:
        try:
            output = self._run_adb("shell", "ps", "-A", timeout=8)
        except RuntimeError:
            output = ""
        for line in output.splitlines():
            fields = line.split()
            if len(fields) < 2 or fields[-1] != "com.tencent.mm":
                continue
            process_user_id = self._process_android_user_id(fields[0])
            if process_user_id != self._wechat_user_id or not fields[1].isdigit():
                continue
            return int(fields[1])

        if len(self._wechat_instances) == 1:
            output = self._run_adb(
                "shell", "pidof", "com.tencent.mm", timeout=5
            )
            for item in output.split():
                if item.isdigit():
                    return int(item)
        raise RuntimeError(
            f"{self._wechat_instance_name}正在重新启动，尚未出现主进程"
        )

    def _foreground_activity_context(self) -> tuple[int | None, str]:
        output = self._run_adb(
            "shell", "dumpsys", "activity", "activities", timeout=10
        )
        match = re.search(
            r"topResumedActivity=.*?\bu(\d+)\s+([\w.]+)/([\w.$]+)",
            output,
        )
        if not match:
            raise RuntimeError("无法识别手机当前前台页面")
        user_id_text, package_name, class_name = match.groups()
        if class_name.startswith("."):
            class_name = package_name + class_name
        return int(user_id_text), class_name

    def _foreground_activity(self) -> str:
        return self._foreground_activity_context()[1]

    def _launch_wechat_user_via_launcher(self, user_id: int) -> None:
        try:
            import frida  # type: ignore
        except ImportError as exc:
            raise RuntimeError("缺少 frida，无法启动指定微信分身") from exc
        actual_version = str(getattr(frida, "__version__", "未知"))
        if actual_version != EXPECTED_FRIDA_VERSION:
            raise RuntimeError(
                f"软件内置 Frida 版本为 {actual_version}，但手机端要求 "
                f"{EXPECTED_FRIDA_VERSION}；请使用最新版便携包"
            )

        home_output = self._run_adb(
            "shell",
            "cmd",
            "package",
            "resolve-activity",
            "--brief",
            "-a",
            "android.intent.action.MAIN",
            "-c",
            "android.intent.category.HOME",
            timeout=10,
        )
        home_component = next(
            (
                line.strip()
                for line in reversed(home_output.splitlines())
                if "/" in line and " " not in line.strip()
            ),
            "",
        )
        if not home_component:
            raise RuntimeError("无法识别手机默认桌面启动器")
        launcher_package = home_component.split("/", 1)[0]
        launcher_pids = self._run_adb(
            "shell", "pidof", launcher_package, timeout=8
        )
        launcher_pid = next(
            (int(value) for value in launcher_pids.split() if value.isdigit()),
            0,
        )
        if not launcher_pid:
            raise RuntimeError(f"手机默认桌面 {launcher_package} 尚未运行")

        manager: Any = None
        remote_endpoint = ""
        forwarded_port = 0
        if self.connection == CONNECTION_WIFI:
            forwarded_port = self._forward_to_available_local_port(
                self.frida_port,
                "tcp:27042",
                "微信分身启动",
            )
            self.frida_port = forwarded_port
            remote_endpoint = f"127.0.0.1:{forwarded_port}"
            manager = frida.get_device_manager()
            device = manager.add_remote_device(remote_endpoint)
        else:
            if not self._adb_serial:
                raise RuntimeError("ADB 手机序列号尚未确定")
            device = frida.get_device(self._adb_serial, timeout=10)

        session: Any = None
        script: Any = None
        try:
            device.enumerate_processes()
            session = device.attach(launcher_pid)
            script = session.create_script(
                self._launcher_bundle.read_text(encoding="utf-8")
            )
            script.load()
            result = script.exports_sync.launchwechat(user_id)
            if not isinstance(result, dict) or not result.get("ok"):
                detail = (
                    str(result.get("error") or "未知错误")
                    if isinstance(result, dict)
                    else str(result or "未知错误")
                )
                raise RuntimeError(detail)
        except Exception as exc:
            raise RuntimeError(
                f"无法通过默认桌面启动用户 {user_id} 的微信：{exc}"
            ) from exc
        finally:
            if script is not None:
                try:
                    script.unload()
                except Exception:
                    pass
            if session is not None:
                try:
                    session.detach()
                except Exception:
                    pass
            if manager is not None and remote_endpoint:
                try:
                    manager.remove_remote_device(remote_endpoint)
                except Exception:
                    pass
            if forwarded_port:
                try:
                    self._run_adb(
                        "forward",
                        "--remove",
                        f"tcp:{forwarded_port}",
                        timeout=5,
                    )
                except Exception:
                    pass

    def _wait_for_foreground_activity(
        self,
        expected: set[str],
        timeout: float = PAGE_REENTRY_ACTIVITY_TIMEOUT_SECONDS,
        expected_user_id: int | None = None,
    ) -> str:
        deadline = time.monotonic() + timeout
        last_activity = ""
        while not self._stop.is_set() and time.monotonic() < deadline:
            try:
                if expected_user_id is None:
                    foreground_user = None
                    last_activity = self._foreground_activity()
                else:
                    foreground_user, last_activity = (
                        self._foreground_activity_context()
                    )
            except RuntimeError:
                last_activity = "页面切换中"
                if self._stop.wait(0.5):
                    break
                continue
            if last_activity in expected and (
                expected_user_id is None or foreground_user == expected_user_id
            ):
                return last_activity
            if self._stop.wait(0.5):
                break
        expected_text = "、".join(sorted(expected))
        raise RuntimeError(
            f"等待微信页面超时；期望 {expected_text}"
            + (
                f"（用户 {expected_user_id}）"
                if expected_user_id is not None
                else ""
            )
            + f"，当前 {last_activity or '未知'}"
        )

    def _screen_size(self) -> tuple[int, int]:
        output = self._run_adb("shell", "wm", "size", timeout=8)
        matches = re.findall(r"(?:Physical|Override) size:\s*(\d+)x(\d+)", output)
        if not matches:
            raise RuntimeError("无法读取手机屏幕尺寸")
        width, height = (int(value) for value in matches[-1])
        if width <= 0 or height <= 0:
            raise RuntimeError("手机屏幕尺寸无效")
        return width, height

    def _ui_hierarchy_xml(self) -> str:
        output = self._run_adb(
            "exec-out",
            "uiautomator",
            "dump",
            "/dev/tty",
            timeout=12,
        )
        start = output.find("<?xml")
        end_marker = "</hierarchy>"
        end = output.rfind(end_marker)
        if start < 0 or end < start:
            raise RuntimeError("手机界面控件树尚未就绪")
        return output[start : end + len(end_marker)]

    def _uiautomator_text_center(
        self,
        text: str,
        *,
        prefer_bottom: bool = False,
    ) -> tuple[int, int]:
        try:
            root = ET.fromstring(self._ui_hierarchy_xml())
        except (ET.ParseError, RuntimeError) as exc:
            raise RuntimeError(f"无法读取手机界面中的“{text}”") from exc

        candidates: list[tuple[int, int]] = []
        for node in root.iter("node"):
            node_text = (node.get("text") or "").strip()
            description = (node.get("content-desc") or "").strip()
            if text not in {node_text, description}:
                continue
            if (node.get("visible-to-user") or "true").lower() == "false":
                continue
            match = re.fullmatch(
                r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]",
                node.get("bounds") or "",
            )
            if not match:
                continue
            left, top, right, bottom = (int(value) for value in match.groups())
            if right <= left or bottom <= top:
                continue
            candidates.append(((left + right) // 2, (top + bottom) // 2))

        if not candidates:
            raise RuntimeError(f"当前手机界面未找到“{text}”")
        if prefer_bottom:
            return max(candidates, key=lambda point: point[1])
        return candidates[0]

    def _frida_ui_text_center(
        self,
        text: str,
        *,
        prefer_bottom: bool = False,
    ) -> tuple[int, int]:
        try:
            import frida  # type: ignore
        except ImportError as exc:
            raise RuntimeError("电脑端 Frida 组件不可用") from exc
        if not self._native_ui_bundle.is_file():
            raise RuntimeError(
                f"便携版缺少微信界面定位脚本：{self._native_ui_bundle.name}"
            )

        manager: Any = None
        remote_endpoint = ""
        forwarded_port = 0
        session: Any = None
        script: Any = None
        try:
            if self.connection == CONNECTION_WIFI:
                forwarded_port = self._forward_to_available_local_port(
                    self.frida_port,
                    "tcp:27042",
                    "微信界面文字定位",
                )
                self.frida_port = forwarded_port
                remote_endpoint = f"127.0.0.1:{forwarded_port}"
                manager = frida.get_device_manager()
                device = manager.add_remote_device(remote_endpoint)
            else:
                if not self._adb_serial:
                    raise RuntimeError("ADB 手机序列号尚未确定")
                device = frida.get_device(self._adb_serial, timeout=10)

            session = device.attach(self._current_wechat_pid())
            script = session.create_script(
                self._native_ui_bundle.read_text(encoding="utf-8")
            )
            script.load()
            result = script.exports_sync.locatetext(text, prefer_bottom)
            if not isinstance(result, dict) or not result.get("ok"):
                detail = (
                    str(result.get("error") or "未知错误")
                    if isinstance(result, dict)
                    else str(result or "未知错误")
                )
                raise RuntimeError(detail)
            x = int(result.get("x"))
            y = int(result.get("y"))
            if x < 0 or y < 0:
                raise RuntimeError("识别到的控件坐标无效")
            return x, y
        except Exception as exc:
            raise RuntimeError(f"微信进程内未找到“{text}”：{exc}") from exc
        finally:
            if script is not None:
                try:
                    script.unload()
                except Exception:
                    pass
            if session is not None:
                try:
                    session.detach()
                except Exception:
                    pass
            if manager is not None and remote_endpoint:
                try:
                    manager.remove_remote_device(remote_endpoint)
                except Exception:
                    pass
            if forwarded_port:
                try:
                    self._run_adb(
                        "forward",
                        "--remove",
                        f"tcp:{forwarded_port}",
                        timeout=5,
                    )
                except Exception:
                    pass

    def _ui_text_center(
        self,
        text: str,
        *,
        prefer_bottom: bool = False,
    ) -> tuple[int, int]:
        try:
            return self._uiautomator_text_center(
                text,
                prefer_bottom=prefer_bottom,
            )
        except RuntimeError as system_error:
            try:
                return self._frida_ui_text_center(
                    text,
                    prefer_bottom=prefer_bottom,
                )
            except RuntimeError as process_error:
                raise RuntimeError(
                    f"系统控件树定位失败：{system_error}；"
                    f"进程内定位失败：{process_error}"
                ) from process_error

    def _ensure_frida_server(self) -> None:
        pid = self._frida_server_pid()
        if pid:
            self.log(
                f"手机 Frida Server {EXPECTED_FRIDA_VERSION} 已在运行（PID {pid}）"
            )
            return

        try:
            root_status = self._run_adb("shell", "su", "-c", "id", timeout=8)
        except RuntimeError as exc:
            raise RuntimeError(
                "无法取得手机 Root 权限；请解锁手机并在 Magisk 中允许 Shell Root 权限"
            ) from exc
        if "uid=0" not in root_status:
            raise RuntimeError(
                "手机没有授予 Shell Root 权限；请打开 Magisk 并允许 Shell Root 权限"
            )

        try:
            exists = self._run_adb(
                "shell",
                "su",
                "-c",
                f"ls -l {FRIDA_SERVER_REMOTE_PATH}",
                timeout=8,
            )
        except RuntimeError as exc:
            exists = ""
            missing_error = exc
        else:
            missing_error = None
        if FRIDA_SERVER_REMOTE_PATH not in exists:
            raise RuntimeError(
                f"手机缺少 Frida Server {EXPECTED_FRIDA_VERSION} 文件："
                f"{FRIDA_SERVER_REMOTE_PATH}"
            ) from missing_error

        self.log(f"正在自动启动手机 Frida Server {EXPECTED_FRIDA_VERSION}…")
        self._run_adb(
            "shell",
            "su",
            "-c",
            f"chmod 755 {FRIDA_SERVER_REMOTE_PATH}",
            timeout=8,
        )
        log_path = f"{FRIDA_SERVER_REMOTE_PATH}.log"
        self._run_adb(
            "shell",
            "su",
            "-c",
            (
                f"nohup {FRIDA_SERVER_REMOTE_PATH} >{log_path} 2>&1 "
                "</dev/null &"
            ),
            timeout=8,
        )

        deadline = time.monotonic() + FRIDA_SERVER_START_TIMEOUT
        while time.monotonic() < deadline:
            if self._stop.is_set():
                raise RuntimeError("启动 Frida Server 时采集已停止")
            time.sleep(0.25)
            pid = self._frida_server_pid()
            if pid:
                self.log(
                    f"手机 Frida Server {EXPECTED_FRIDA_VERSION} 自动启动成功"
                    f"（PID {pid}）"
                )
                return
        raise RuntimeError(
            f"手机 Frida Server {EXPECTED_FRIDA_VERSION} 自动启动失败；"
            f"请检查 Magisk 授权或手机日志 {log_path}"
        )

    def _enable_webview_debug(self, target_pid: int | None = None) -> None:
        try:
            import frida  # type: ignore
        except ImportError as exc:
            raise RuntimeError("缺少 frida，无法连接安卓微信") from exc
        actual_version = str(getattr(frida, "__version__", "未知"))
        if actual_version != EXPECTED_FRIDA_VERSION:
            raise RuntimeError(
                f"软件内置 Frida 版本为 {actual_version}，但手机端要求 "
                f"{EXPECTED_FRIDA_VERSION}；请使用最新版便携包"
            )
        try:
            if self.connection == CONNECTION_WIFI:
                self.frida_port = self._forward_to_available_local_port(
                    self.frida_port,
                    "tcp:27042",
                    "Frida 连接",
                )
                self._frida_remote_endpoint = f"127.0.0.1:{self.frida_port}"
                self._frida_manager = frida.get_device_manager()
                device = self._frida_manager.add_remote_device(
                    self._frida_remote_endpoint
                )
            else:
                self._frida_manager = frida.get_device_manager()
                if not self._adb_serial:
                    raise RuntimeError("ADB 手机序列号尚未确定")
                device = frida.get_device(self._adb_serial, timeout=10)
        except Exception as exc:
            raise RuntimeError(
                f"无法连接手机 Frida Server {EXPECTED_FRIDA_VERSION}；"
                "请确认服务已启动且与软件版本一致"
            ) from exc
        try:
            device.enumerate_processes()
        except Exception as exc:
            raise RuntimeError(
                f"手机 Frida Server 通信失败；手机端和软件端都必须为 "
                f"{EXPECTED_FRIDA_VERSION}"
            ) from exc
        attach_pid = target_pid or self._current_wechat_pid()
        ready = threading.Event()
        errors: list[str] = []
        session = device.attach(attach_pid)
        script = session.create_script(self._bundle.read_text(encoding="utf-8"))

        def on_message(message: dict[str, Any], _data: bytes | None) -> None:
            if message.get("type") == "error":
                errors.append(str(message.get("stack") or message))
                ready.set()
                return
            payload = message.get("payload") or {}
            if payload.get("type") == "webview_debug_ready":
                if not payload.get("enabled"):
                    errors.extend(str(item) for item in payload.get("errors") or [])
                ready.set()

        script.on("message", on_message)
        try:
            script.load()
            if not ready.wait(40):
                raise TimeoutError("开启微信搜索页调试超时")
            if errors:
                raise RuntimeError("；".join(errors))
        except Exception:
            try:
                script.unload()
            except Exception:
                pass
            try:
                session.detach()
            except Exception:
                pass
            raise
        self._wechat_pid = attach_pid
        self._frida_device = device
        self._frida_session = session
        self._automation_script = script
        self.log(f"已连接安卓微信 PID {self._wechat_pid}")

    def _forward_debug_socket(self) -> None:
        xweb_name = f"webview_devtools_remote_xweb_{self._wechat_pid}"
        standard_name = f"webview_devtools_remote_{self._wechat_pid}"
        try:
            unix_sockets = self._run_adb(
                "shell", "cat", "/proc/net/unix", timeout=8
            )
        except RuntimeError:
            unix_sockets = ""
        socket_names = [
            name
            for name in (xweb_name, standard_name)
            if name in unix_sockets
        ]
        if not socket_names:
            socket_names = [xweb_name, standard_name]

        errors: list[str] = []
        for index, socket_name in enumerate(socket_names):
            if index:
                try:
                    self._run_adb(
                        "forward", "--remove", f"tcp:{self.port}", timeout=5
                    )
                except Exception:
                    pass
                self.port = 0
            try:
                self.port = self._forward_to_available_local_port(
                    self.port,
                    f"localabstract:{socket_name}",
                    "微信页面连接",
                )
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.port}/json", timeout=3
                ) as response:
                    json.loads(response.read().decode("utf-8"))
            except Exception as exc:
                errors.append(f"{socket_name}: {exc}")
                try:
                    self._run_adb(
                        "forward", "--remove", f"tcp:{self.port}", timeout=5
                    )
                except Exception:
                    pass
                continue
            if socket_name == standard_name:
                self.log(
                    "检测到微信已切换为标准 WebView 调试 socket；"
                    "已自动切换连接"
                )
            return
        raise RuntimeError(
            "微信 WebView 调试 socket 均不可用：" + "；".join(errors)
        )

    def _visible_target(self) -> dict[str, Any]:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{self.port}/json", timeout=5
        ) as response:
            targets = json.loads(response.read().decode("utf-8"))
        fallback: list[dict[str, Any]] = []
        for target in targets if isinstance(targets, list) else []:
            if target.get("type") != "page" or not target.get("webSocketDebuggerUrl"):
                continue
            try:
                description = json.loads(target.get("description") or "{}")
            except (TypeError, ValueError):
                description = {}
            if description.get("visible") and not description.get("empty"):
                return target
            if not description.get("empty"):
                fallback.append(target)
        if fallback:
            self.log(
                "微信 XWeb 未标记可见页面；已自动连接最近的非空搜索页"
            )
            return fallback[0]
        raise RuntimeError("未找到可见的微信关键词搜索结果页")

    def _connect(self) -> None:
        try:
            import websocket  # type: ignore
        except ImportError as exc:
            raise RuntimeError("缺少 websocket-client，关键词模式无法启动") from exc
        target = self._visible_target()
        self._socket = websocket.create_connection(
            target["webSocketDebuggerUrl"],
            timeout=5,
            enable_multithread=True,
            suppress_origin=True,
        )
        self._call("Runtime.enable")

    def _recover_page_connection(self) -> None:
        self._drop_socket()
        current_pid = self._current_wechat_pid()
        if current_pid != self._wechat_pid:
            previous_pid = self._wechat_pid
            self.log(
                f"检测到微信进程已从 {previous_pid} 变为 {current_pid}；"
                "正在自动重新附加"
            )
            self._release_wechat_runtime(remove_remote=True)
            self._enable_webview_debug(current_pid)
        last_error: Exception | None = None
        for attempt in range(1, DEBUG_FORWARD_RECOVERY_ATTEMPTS + 1):
            try:
                self._run_adb(
                    "forward", "--remove", f"tcp:{self.port}", timeout=5
                )
            except Exception:
                pass
            if attempt > 1:
                self.port = 0
            try:
                self._forward_debug_socket()
                self._connect()
            except Exception as exc:
                last_error = exc
                self._drop_socket()
                if attempt >= DEBUG_FORWARD_RECOVERY_ATTEMPTS:
                    break
                self.log(
                    "微信页面调试端口连接失败；正在改用新的本机端口"
                    f"重建（{attempt}/{DEBUG_FORWARD_RECOVERY_ATTEMPTS}）：{exc}"
                )
                connection_error = str(exc).lower()
                if any(
                    marker in connection_error
                    for marker in (
                        "remote end closed",
                        "connection reset",
                        "connection aborted",
                        "websocket connection is closed",
                    )
                ):
                    try:
                        refresh_pid = self._current_wechat_pid()
                        self.log(
                            "检测到微信主进程仍在，但 XWeb 调试连接已经失效；"
                            "正在重新挂载手机自动化"
                        )
                        self._release_wechat_runtime(remove_remote=True)
                        self._enable_webview_debug(refresh_pid)
                        current_pid = refresh_pid
                    except Exception as refresh_exc:
                        last_error = refresh_exc
                        self.log(
                            "重新挂载微信 XWeb 调试失败；将继续重试："
                            f"{refresh_exc}"
                        )
                if self._stop.wait(DEBUG_FORWARD_RECOVERY_DELAY_SECONDS):
                    break
                continue
            self.log(
                f"微信搜索页已自动恢复连接（PID {self._wechat_pid}，"
                f"本机端口 {self.port}）"
            )
            return
        raise RuntimeError(
            "微信页面调试端口连续重建失败："
            f"{last_error or '未知错误'}"
        ) from last_error

    def _call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        if self._socket is None:
            raise RuntimeError("搜索页调试连接尚未建立")
        command_id = self._next_id
        self._next_id += 1
        self._socket.send(
            json.dumps({"id": command_id, "method": method, "params": params or {}})
        )
        while not self._stop.is_set():
            message = json.loads(self._socket.recv())
            if message.get("id") != command_id:
                continue
            if message.get("error"):
                raise RuntimeError(str(message["error"]))
            return message.get("result") or {}
        return {}

    def _read_cards(self, keyword: str | None = None) -> list[ProductCard]:
        result = self._call(
            "Runtime.evaluate",
            {
                "expression": PRODUCTS_EXPRESSION,
                "returnByValue": True,
                "awaitPromise": True,
            },
        )
        rows = ((result.get("result") or {}).get("value") or [])
        return cards_from_dom(rows, keyword if keyword is not None else self.keyword)

    def _read_keyword(self) -> tuple[str, str]:
        result = self._call(
            "Runtime.evaluate",
            {
                "expression": KEYWORD_EXPRESSION,
                "returnByValue": True,
                "awaitPromise": True,
            },
        )
        value = (result.get("result") or {}).get("value")
        if not isinstance(value, dict):
            return str(value or "").strip(), ""
        return (
            str(value.get("keyword") or "").strip(),
            str(value.get("search_id") or "").strip(),
        )

    def _keyword_is_committed(
        self,
        keyword: str,
        search_id: str,
        has_products: bool,
        now: float | None = None,
    ) -> bool:
        timestamp = time.monotonic() if now is None else now
        context = (keyword.strip(), search_id.strip())
        if context != self._candidate_context:
            self._candidate_context = context
            self._candidate_since = timestamp
            return False
        return bool(
            context[0]
            and has_products
            and timestamp - self._candidate_since >= self.keyword_stable_seconds
        )

    @staticmethod
    def _signature(card: ProductCard) -> tuple[Any, ...]:
        return (
            card.keyword,
            card.shop_name,
            card.title,
            card.main_image_url,
            card.price_fen,
            card.sales,
            card.sales_text,
        )

    def _new_or_changed(self, cards: list[ProductCard]) -> list[ProductCard]:
        changed: list[ProductCard] = []
        for card in cards:
            signature = self._signature(card)
            if self._seen.get(card.product_id) == signature:
                continue
            self._seen[card.product_id] = signature
            changed.append(card)
        return changed

    def _evaluate(self, expression: str) -> Any:
        result = self._call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
            },
        )
        return (result.get("result") or {}).get("value")

    def _read_page_state(self) -> dict[str, Any]:
        value = self._evaluate(PAGE_STATE_EXPRESSION)
        if not isinstance(value, dict):
            raise RuntimeError("未读取到微信商品搜索页状态")
        return value

    def _drop_socket(self) -> None:
        socket = self._socket
        self._socket = None
        if socket is not None:
            try:
                socket.close()
            except Exception:
                pass

    def _click_page_point(self, point: Any, label: str) -> None:
        if not isinstance(point, dict):
            raise RuntimeError(f"当前页面未找到“{label}”按钮")
        try:
            x = float(point["x"])
            y = float(point["y"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"“{label}”按钮位置无效") from exc
        for event_type in ("mousePressed", "mouseReleased"):
            self._call(
                "Input.dispatchMouseEvent",
                {
                    "type": event_type,
                    "x": x,
                    "y": y,
                    "button": "left",
                    "clickCount": 1,
                },
            )

    def _restart_wechat_to_me(self) -> None:
        previous_pid = self._wechat_pid
        self._drop_socket()
        try:
            self._run_adb(
                "forward", "--remove", f"tcp:{self.port}", timeout=5
            )
        except Exception:
            pass
        self._release_wechat_runtime(remove_remote=True)

        self.log(
            f"正在完整停止{self._wechat_instance_name}进程"
            f"（用户 {self._wechat_user_id}，原 PID {previous_pid or '未知'}）"
        )
        self._run_adb(
            "shell",
            "am",
            "force-stop",
            "--user",
            str(self._wechat_user_id),
            "com.tencent.mm",
            timeout=12,
        )
        if self._stop.wait(1.0):
            raise RuntimeError("微信完整重启已停止")
        if self._has_multiple_wechat_instances():
            self._launch_wechat_user_via_launcher(self._wechat_user_id)
        else:
            self._run_adb(
                "shell",
                "am",
                "start",
                "--user",
                str(self._wechat_user_id),
                "-n",
                "com.tencent.mm/.ui.LauncherUI",
                timeout=15,
            )
        self._wait_for_foreground_activity(
            {WECHAT_LAUNCHER_ACTIVITY},
            timeout=30.0,
            expected_user_id=self._wechat_user_id,
        )
        self.log(
            f"{self._wechat_instance_name}主界面已经出现，"
            "正在等待冷启动稳定后再挂载手机自动化"
        )
        if self._stop.wait(WECHAT_COLD_START_SETTLE_SECONDS):
            raise RuntimeError("微信完整重启已停止")
        new_pid = self._current_wechat_pid()
        self.log(
            f"{self._wechat_instance_name}已重新启动"
            f"（PID {previous_pid or '未知'} → {new_pid}）；"
            "将先进入搜索页，再重新挂载手机自动化"
        )

    def _click_shop_entry_from_wechat_me(self) -> None:
        width, height = self._screen_size()

        self.log("正在通过系统触控切换到微信“我”页")
        try:
            me_x, me_y = self._ui_text_center("我", prefer_bottom=True)
        except RuntimeError:
            # The bottom navigation position is stable even while accessibility
            # nodes are still loading. This fallback cannot hit a profile menu row.
            me_x = round(width * 0.875)
            me_y = round(height * 0.9583)
        self._run_adb(
            "shell", "input", "tap", str(me_x), str(me_y), timeout=8
        )
        if self._stop.wait(1.5):
            raise RuntimeError("微信完整重启已停止")

        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                shop_x, shop_y = self._ui_text_center("小店与卡包")
            except RuntimeError as exc:
                last_error = exc
                if attempt < 3:
                    self.log(
                        f'“小店与卡包”入口尚未显示，正在重新识别（{attempt}/3）'
                    )
                    if self._stop.wait(1.0):
                        raise RuntimeError("微信完整重启已停止") from exc
                    continue
                break
            self._run_adb(
                "shell",
                "input",
                "tap",
                str(shop_x),
                str(shop_y),
                timeout=8,
            )
            try:
                self._wait_for_foreground_activity(
                    WECHAT_SHOP_ACTIVITIES,
                    timeout=8.0,
                    expected_user_id=self._wechat_user_id,
                )
                return
            except RuntimeError as exc:
                last_error = exc
            if attempt < 3:
                self.log(
                    f'“小店与卡包”入口尚未响应，正在重新识别（{attempt}/3）'
                )
                if self._stop.wait(1.0):
                    raise RuntimeError("微信完整重启已停止")
        raise RuntimeError(
            "从微信“我”页进入“小店与卡包”失败："
            f"{last_error or '没有识别到准确入口，为避免误点已停止'}"
        ) from last_error

    def _open_shop_search_from_wechat_me(
        self,
        bootstrap_keyword: str = "",
    ) -> None:
        self._pending_bootstrap_keyword = ""
        self._click_shop_entry_from_wechat_me()
        self._wait_for_foreground_activity(
            WECHAT_SHOP_ACTIVITIES,
            expected_user_id=self._wechat_user_id,
        )
        if self._stop.wait(2.5):
            raise RuntimeError("微信完整重启已停止")

        width, height = self._screen_size()
        search_x = round(width * 0.5)
        search_y = round(height * 0.10625)
        last_tap_error: Exception | None = None
        for attempt in range(1, 4):
            self._run_adb(
                "shell",
                "input",
                "tap",
                str(search_x),
                str(search_y),
                timeout=8,
            )
            try:
                self._wait_for_foreground_activity(
                    {WECHAT_SEARCH_ACTIVITY},
                    timeout=4.0,
                    expected_user_id=self._wechat_user_id,
                )
                break
            except RuntimeError as exc:
                last_tap_error = exc
                activity = self._foreground_activity()
                if activity not in WECHAT_SHOP_ACTIVITIES:
                    raise
                if attempt < 3:
                    self.log(
                        f'“小店与卡包”搜索框尚未响应，正在重新点击（{attempt}/3）'
                    )
                    if self._stop.wait(1.0):
                        raise RuntimeError("微信完整重启已停止") from exc
        else:
            raise RuntimeError(
                '连续点击“小店与卡包”搜索框 3 次后仍未打开搜索页：'
                f"{last_tap_error or '未知错误'}"
            ) from last_tap_error
        if self._stop.wait(WECHAT_SEARCH_SETTLE_SECONDS):
            raise RuntimeError("微信完整重启已停止")
        self.log("微信搜索页已打开，正在重新挂载手机自动化")
        self._ensure_frida_server()
        self._enable_webview_debug()
        bootstrap_keyword = bootstrap_keyword.strip()
        if bootstrap_keyword:
            self.log(
                f"搜索输入页已就绪，正在先提交“{bootstrap_keyword}”"
                "以打开商品结果页"
            )
            for submit_attempt in range(1, BOOTSTRAP_SUBMIT_MAX_ATTEMPTS + 1):
                try:
                    self._submit_keyword(bootstrap_keyword)
                    break
                except RuntimeError:
                    if submit_attempt >= BOOTSTRAP_SUBMIT_MAX_ATTEMPTS:
                        raise
                    self.log(
                        "微信搜索输入页仍在初始化，正在重新提交关键词"
                        f"（{submit_attempt}/{BOOTSTRAP_SUBMIT_MAX_ATTEMPTS}）"
                    )
                    if self._stop.wait(BOOTSTRAP_SUBMIT_RETRY_SECONDS):
                        raise RuntimeError("微信完整重启已停止")
            if self._stop.wait(WECHAT_SEARCH_SETTLE_SECONDS):
                raise RuntimeError("微信完整重启已停止")
        deadline = time.monotonic() + PAGE_REENTRY_ACTIVITY_TIMEOUT_SECONDS
        last_error: Exception | None = None
        while not self._stop.is_set() and time.monotonic() < deadline:
            try:
                self._recover_page_connection()
                self._pending_bootstrap_keyword = bootstrap_keyword
                return
            except Exception as exc:
                last_error = exc
                if self._stop.wait(1.0):
                    break
        raise RuntimeError(
            "微信搜索页已打开，但调试接口未就绪："
            f"{last_error or '未知错误'}"
        ) from last_error

    def _reenter_shop_search_page(self, next_keyword: str) -> None:
        last_error: Exception | None = None
        for attempt in range(1, PAGE_REENTRY_MAX_ATTEMPTS + 1):
            try:
                self.log(
                    f"已完成 {self.restart_keyword_interval} 个关键词，"
                    "正在完整重启微信并重新进入“小店与卡包”"
                )
                self._restart_wechat_to_me()
                self._open_shop_search_from_wechat_me(next_keyword)
                self.log(
                    "微信完整重启并恢复搜索页完成；"
                    f"将继续搜索“{next_keyword}”"
                )
                return
            except Exception as exc:
                if self._stop.is_set():
                    raise RuntimeError("微信完整重启已停止") from exc
                last_error = exc
                if attempt >= PAGE_REENTRY_MAX_ATTEMPTS:
                    break
                self.log(
                    f"微信完整重启第 {attempt}/{PAGE_REENTRY_MAX_ATTEMPTS} 次失败："
                    f"{exc}；{PAGE_REENTRY_RETRY_SECONDS:g} 秒后重试"
                )
                if self._stop.wait(PAGE_REENTRY_RETRY_SECONDS):
                    raise RuntimeError("微信完整重启已停止") from exc
        raise RuntimeError(
            f"微信完整重启连续 {PAGE_REENTRY_MAX_ATTEMPTS} 次失败，已暂停："
            f"{last_error or '未知错误'}"
        ) from last_error

    def _has_multiple_wechat_instances(self) -> bool:
        return len(self._wechat_instances) > 1

    def _switch_to_next_wechat(self, retry_keyword: str) -> None:
        if not self._has_multiple_wechat_instances():
            raise RuntimeError("当前手机只识别到一个微信，无法切换微信分身")

        old_user_id = self._wechat_user_id
        old_name = self._wechat_instance_name
        old_pid = self._wechat_pid
        next_index = (self._wechat_instance_index + 1) % len(
            self._wechat_instances
        )
        next_user_id, next_name = self._wechat_instances[next_index]
        wrapped = next_index == 0

        self._drop_socket()
        try:
            self._run_adb(
                "forward", "--remove", f"tcp:{self.port}", timeout=5
            )
        except Exception:
            pass
        self._release_wechat_runtime(remove_remote=True)
        self.log(
            f"正在停止{old_name}（用户 {old_user_id}，"
            f"PID {old_pid or '未知'}）并切换到{next_name}"
        )
        self._run_adb(
            "shell",
            "am",
            "force-stop",
            "--user",
            str(old_user_id),
            "com.tencent.mm",
            timeout=12,
        )
        self._wechat_instance_index = next_index
        self._wechat_switch_count += 1
        if wrapped:
            self.log("所有微信实例已轮换一遍，正在回到第一个微信继续循环")

        last_error: Exception | None = None
        for attempt in range(1, PAGE_REENTRY_MAX_ATTEMPTS + 1):
            try:
                self.log(
                    f"正在启动{next_name}（用户 {next_user_id}）；"
                    f"成功后重试“{retry_keyword}”"
                )
                self._restart_wechat_to_me()
                self._open_shop_search_from_wechat_me(retry_keyword)
                self.log(
                    f"已切换到{next_name}并恢复搜索页；"
                    f"正在重新搜索“{retry_keyword}”"
                )
                return
            except Exception as exc:
                if self._stop.is_set():
                    raise RuntimeError("切换微信已由用户停止") from exc
                last_error = exc
                if attempt >= PAGE_REENTRY_MAX_ATTEMPTS:
                    break
                self.log(
                    f"启动{next_name}第 {attempt}/{PAGE_REENTRY_MAX_ATTEMPTS} "
                    f"次失败：{exc}；{PAGE_REENTRY_RETRY_SECONDS:g} 秒后重试"
                )
                if self._stop.wait(PAGE_REENTRY_RETRY_SECONDS):
                    raise RuntimeError("切换微信已由用户停止") from exc
        raise RuntimeError(
            f"切换到{next_name}连续 {PAGE_REENTRY_MAX_ATTEMPTS} 次失败，"
            f"当前关键词“{retry_keyword}”已暂停：{last_error or '未知错误'}"
        ) from last_error

    def _search_healthcheck_keyword(self, target_keyword: str) -> str:
        return next(
            (
                keyword
                for keyword in SEARCH_HEALTHCHECK_KEYWORDS
                if keyword != target_keyword
            ),
            "手机",
        )

    def _verify_current_wechat_search(self, target_keyword: str) -> None:
        if not self._has_multiple_wechat_instances():
            return
        healthcheck_keyword = self._search_healthcheck_keyword(target_keyword)
        self.log(
            f"关键词“{target_keyword}”没有商品；正在用“{healthcheck_keyword}”"
            f"确认{self._wechat_instance_name}是否仍能搜索"
        )
        try:
            self._submit_keyword(healthcheck_keyword)
            cards = self._wait_for_committed_results(healthcheck_keyword)
        except Exception as exc:
            if self._is_fatal_automation_error(str(exc)):
                raise
            raise WechatSearchUnavailable(
                f"{self._wechat_instance_name}搜索自检失败：{exc}"
            ) from exc
        if cards:
            self.log(
                f"{self._wechat_instance_name}搜索功能正常；"
                f"“{target_keyword}”确实没有商品，不切换微信"
            )
            return
        raise WechatSearchUnavailable(
            f"{self._wechat_instance_name}搜索“{target_keyword}”和自检词"
            f"“{healthcheck_keyword}”均无结果"
        )

    def _submit_keyword(self, keyword: str) -> None:
        script = self._automation_script
        if script is None:
            raise RuntimeError("手机自动搜索控制脚本尚未启动")
        self.log(f"正在自动输入并搜索：{keyword}")
        result = script.exports_sync.setsearchkeyword(keyword)
        if not isinstance(result, dict) or not result.get("ok"):
            detail = (
                str(result.get("error") or "未知错误")
                if isinstance(result, dict)
                else str(result or "未知错误")
            )
            raise RuntimeError(f"自动输入关键词失败：{detail}")

    def _wait_for_committed_results(
        self,
        target_keyword: str,
        timeout: float = RESULT_WAIT_TIMEOUT_SECONDS,
        previous_signature: tuple[Any, ...] | None = None,
    ) -> list[ProductCard]:
        deadline = time.monotonic() + timeout
        candidate: tuple[Any, ...] | None = None
        candidate_since = 0.0
        last_retry = ""
        while not self._stop.is_set() and time.monotonic() < deadline:
            try:
                if self._socket is None:
                    self._connect()
                state = self._read_page_state()
                risk_marker = str(state.get("risk_marker") or "")
                if risk_marker:
                    raise RuntimeError(
                        f"微信页面出现“{risk_marker}”；已停止切换关键词，请人工处理"
                    )
                detected = str(state.get("keyword") or "").strip()
                if detected != target_keyword:
                    candidate = None
                    self._stop.wait(self.poll_seconds)
                    continue
                cards = self._read_cards(target_keyword)
                empty_result = bool(state.get("empty_result"))
                signature = (
                    detected,
                    str(state.get("search_id") or ""),
                    tuple(card.product_id for card in cards[:8]),
                    len(cards),
                    empty_result,
                )
                if previous_signature is not None and signature == previous_signature:
                    candidate = None
                    self._stop.wait(self.poll_seconds)
                    continue
                now = time.monotonic()
                if signature != candidate:
                    candidate = signature
                    candidate_since = now
                elif (
                    (cards or empty_result)
                    and now - candidate_since >= self.keyword_stable_seconds
                ):
                    self._last_committed_result_signature = signature
                    return cards
                last_retry = ""
            except Exception as exc:
                message = str(exc)
                if "已停止切换关键词" in message:
                    raise
                if message != last_retry:
                    self.log(f"等待“{target_keyword}”结果时重连：{message}")
                    last_retry = message
                self._drop_socket()
                try:
                    self._recover_page_connection()
                except Exception as recovery_exc:
                    recovery_message = str(recovery_exc)
                    if recovery_message != last_retry:
                        self.log(
                            f"等待微信搜索页恢复：{recovery_message}"
                        )
                        last_retry = recovery_message
                else:
                    last_retry = ""
            self._stop.wait(self.poll_seconds)
        if self._stop.is_set():
            raise RuntimeError("自动采集已由用户停止")
        raise RuntimeError(
            f"关键词“{target_keyword}”在 {int(timeout)} 秒内没有出现稳定结果；"
            "已暂停，避免跳过数据"
        )

    @staticmethod
    def _price_text(value: Any, placeholder: str) -> str:
        text = str(value or "").strip()
        if not text or text == placeholder:
            return ""
        return "".join(character for character in text if character.isdigit())

    def _set_price_field(
        self,
        field: str,
        value: int | None,
    ) -> None:
        if field not in {"min", "max"}:
            raise ValueError(f"未知价格字段：{field}")
        label = "最低价" if field == "min" else "最高价"
        state = self._read_page_state()
        current = self._price_text(state.get(f"{field}_price_text"), label)
        target = "" if value is None else str(value)
        if current == target:
            return

        field_index = 0 if field == "min" else 1
        if not state.get(f"{field}_price_active"):
            scroll_expression = SCROLL_PRICE_FIELD_EXPRESSION.replace(
                "__FIELD_INDEX__", str(field_index)
            )
            if self._evaluate(scroll_expression) is not True:
                raise RuntimeError(f"当前筛选面板没有“{label}”输入框")
            self._stop.wait(0.25)
            state = self._read_page_state()

        for _attempt in range(3):
            if state.get(f"{field}_price_active"):
                break
            try:
                self._click_page_point(state.get(f"{field}_price_point"), label)
            except RuntimeError:
                break
            self._stop.wait(0.35)
            state = self._read_page_state()
        if not state.get(f"{field}_price_active"):
            click_expression = PRICE_FIELD_CLICK_EXPRESSION.replace(
                "__FIELD_INDEX__", str(field_index)
            )
            if self._evaluate(click_expression) is True:
                self._stop.wait(0.35)
                state = self._read_page_state()
        if not state.get(f"{field}_price_active"):
            raise RuntimeError(f"点击“{label}”后输入框没有激活")
        if not state.get("keyboard_open"):
            raise RuntimeError(f"激活“{label}”后没有打开数字键盘")

        delete_count = len(current) + 1 if current else 0
        for _ in range(delete_count):
            self._click_page_point(
                state.get("keyboard_delete_point"), "数字键盘删除"
            )
            self._stop.wait(0.20)

        for digit in target:
            digit_points = state.get("keyboard_digits")
            point = digit_points.get(digit) if isinstance(digit_points, dict) else None
            self._click_page_point(point, f"数字 {digit}")
            self._stop.wait(0.20)

        self._stop.wait(0.25)
        state = self._read_page_state()
        actual = self._price_text(state.get(f"{field}_price_text"), label)
        if actual != target:
            raise RuntimeError(
                f"“{label}”填写失败：期望 {target or '不限'}，"
                f"页面显示 {actual or '不限'}"
            )

    def _apply_good_review_filter(self, keyword: str) -> bool:
        state = self._read_page_state()
        for attempt in range(1, 4):
            if state.get("filter_panel_open"):
                break
            self._click_page_point(state.get("filter_toggle_point"), "筛选")
            self._stop.wait(0.8)
            state = self._read_page_state()
            if not state.get("filter_panel_open") and attempt < 3:
                self.log(f"关键词“{keyword}”筛选面板未打开，正在重试（{attempt}/3）")
        if not state.get("filter_panel_open"):
            raise RuntimeError("连续点击筛选 3 次后仍未打开筛选面板")

        if state.get("good_review_available") is False:
            self.log(
                f"关键词“{keyword}”的筛选面板没有“好评多”，"
                "将关闭面板并直接跳过该关键词"
            )
            try:
                confirmed = self._evaluate(FILTER_CONFIRM_EXPRESSION) is True
            except Exception:
                confirmed = False
            if not confirmed:
                self._click_page_point(state.get("confirm_point"), "确定")
            deadline = time.monotonic() + 8.0
            while not self._stop.is_set() and time.monotonic() < deadline:
                self._stop.wait(0.35)
                state = self._read_page_state()
                if not state.get("filter_panel_open"):
                    return False
            raise RuntimeError("检测到没有“好评多”，但筛选面板无法关闭")

        if not state.get("good_review_checked"):
            try:
                self._click_page_point(state.get("good_review_point"), "好评多")
            except RuntimeError:
                pass
            self._stop.wait(0.45)
            state = self._read_page_state()
            if not state.get("good_review_checked"):
                if self._evaluate(GOOD_REVIEW_CLICK_EXPRESSION) is True:
                    self._stop.wait(0.45)
                    state = self._read_page_state()
            if not state.get("good_review_checked"):
                raise RuntimeError("“好评多”没有成功选中")
        else:
            self.log(f"关键词“{keyword}”的“好评多”已选中")

        self._set_price_field("min", self.min_price)
        self._set_price_field("max", self.max_price)
        state = self._read_page_state()

        try:
            confirmed = self._evaluate(FILTER_CONFIRM_EXPRESSION) is True
        except Exception:
            confirmed = False
        if not confirmed:
            self._click_page_point(state.get("confirm_point"), "确定")
        deadline = time.monotonic() + 8.0
        while not self._stop.is_set() and time.monotonic() < deadline:
            self._stop.wait(0.35)
            state = self._read_page_state()
            if not state.get("filter_panel_open"):
                if self.min_price is None and self.max_price is None:
                    price_note = "不限价格"
                elif self.min_price is None:
                    price_note = f"最高价 {self.max_price} 元"
                elif self.max_price is None:
                    price_note = f"最低价 {self.min_price} 元"
                else:
                    price_note = (
                        f"价格 {self.min_price}–{self.max_price} 元"
                    )
                self.log(
                    f"关键词“{keyword}”已提交“好评多、{price_note}”筛选；"
                    "正在等待筛选结果更新"
                )
                return True
        raise RuntimeError("点击确定后筛选面板没有关闭")

    def _select_product_tab(self, keyword: str) -> bool:
        state = self._read_page_state()
        if not state.get("product_tab_available"):
            raise RuntimeError("当前搜索结果页没有找到顶部“商品”标签")
        if state.get("product_tab_selected"):
            self.log(f"关键词“{keyword}”已位于商品分类")
            return False

        for attempt in range(1, 4):
            self._click_page_point(state.get("product_tab_point"), "商品")
            deadline = time.monotonic() + 6.0
            while not self._stop.is_set() and time.monotonic() < deadline:
                self._stop.wait(0.35)
                state = self._read_page_state()
                detected = str(state.get("keyword") or "").strip()
                if detected and detected != keyword:
                    raise RuntimeError(
                        f"切换商品分类时搜索词变为“{detected}”；已暂停，避免串词"
                    )
                if state.get("product_tab_selected"):
                    self.log(f"关键词“{keyword}”已切换到商品分类")
                    return True
            if attempt < 3:
                self.log(f"关键词“{keyword}”切换商品分类未生效，正在重试（{attempt}/3）")
                state = self._read_page_state()
        if self._stop.is_set():
            raise RuntimeError("自动采集已由用户停止")
        raise RuntimeError("连续点击“商品”3 次后仍未切换到商品分类")

    def _scroll_keyword_to_end(
        self,
        keyword: str,
        expected_search_id: str = "",
    ) -> int:
        self._evaluate(RESET_SCROLL_EXPRESSION)
        scroll_interval = min(self.poll_seconds, SCROLL_INTERVAL_SECONDS)
        self._stop.wait(scroll_interval)
        self._seen.clear()
        collected_ids: set[str] = set()
        stable_rounds = 0
        previous_height = -1
        started = time.monotonic()

        while not self._stop.is_set():
            if time.monotonic() - started > KEYWORD_RUN_TIMEOUT_SECONDS:
                raise RuntimeError(
                    f"关键词“{keyword}”连续采集超过 30 分钟；已暂停，"
                    "请检查页面是否一直重复加载"
                )
            state = self._read_page_state()
            detected = str(state.get("keyword") or "").strip()
            if detected != keyword:
                raise RuntimeError(
                    f"手机搜索词已变为“{detected or '空'}”；已暂停，避免数据串词"
                )
            current_search_id = str(state.get("search_id") or "").strip()
            if expected_search_id and current_search_id != expected_search_id:
                raise RuntimeError(
                    f"关键词“{keyword}”下滑期间筛选结果版本已变化；"
                    "已停止本轮并重新采集，避免筛选前后数据混合"
                )
            risk_marker = str(state.get("risk_marker") or "")
            if risk_marker:
                raise RuntimeError(
                    f"微信页面出现“{risk_marker}”；已暂停，避免遗漏数据"
                )

            cards = self._read_cards(keyword)
            before_count = len(collected_ids)
            collected_ids.update(card.product_id for card in cards)
            new_id_count = len(collected_ids) - before_count
            changed = self._new_or_changed(cards)
            if changed:
                self.log(f"“{keyword}”新增或更新 {len(changed)} 个普通商品")
                self.on_products(changed)

            height = int(state.get("scroll_height") or 0)
            at_bottom = bool(state.get("at_bottom"))
            unchanged_at_bottom = (
                at_bottom and new_id_count == 0 and height == previous_height
            )
            if unchanged_at_bottom:
                stable_rounds += 1
            else:
                stable_rounds = 0
            if state.get("explicit_end") and at_bottom and new_id_count == 0:
                stable_rounds = max(stable_rounds, END_STABLE_ROUNDS - 1)
            if stable_rounds >= END_STABLE_ROUNDS:
                return len(collected_ids)

            previous_height = height
            self._evaluate(SCROLL_NEXT_EXPRESSION)
            self._stop.wait(scroll_interval)

        raise RuntimeError("自动采集已由用户停止")

    def _collect_one_keyword(
        self,
        target_keyword: str,
        index: int,
        total: int,
    ) -> str:
        self.on_progress(target_keyword, index, total, "正在自动输入并搜索")
        if self._pending_bootstrap_keyword == target_keyword:
            self.log(
                f"完整重启时已提交“{target_keyword}”，正在等待商品结果稳定"
            )
            self._pending_bootstrap_keyword = ""
        else:
            self._pending_bootstrap_keyword = ""
            self._submit_keyword(target_keyword)
        initial_cards = self._wait_for_committed_results(target_keyword)
        if not initial_cards:
            self._verify_current_wechat_search(target_keyword)
            self.log(f"关键词“{target_keyword}”没有商品结果，继续下一个")
            self.on_progress(target_keyword, index, total, "无商品，已跳过")
            return "无商品，已跳过"

        self.on_progress(target_keyword, index, total, "正在切换商品分类")
        initial_signature = self._last_committed_result_signature
        product_tab_changed = self._select_product_tab(target_keyword)
        if product_tab_changed:
            product_cards = self._wait_for_committed_results(
                target_keyword,
                previous_signature=initial_signature,
            )
            if not product_cards:
                self.log(f"关键词“{target_keyword}”商品分类没有结果，继续下一个")
                self.on_progress(
                    target_keyword, index, total, "商品分类无结果，已跳过"
                )
                return "商品分类无结果，已跳过"
        product_signature = self._last_committed_result_signature
        self.on_progress(target_keyword, index, total, "正在选择好评多")
        filter_applied = self._apply_good_review_filter(target_keyword)
        if filter_applied is False:
            self.log(
                f"关键词“{target_keyword}”没有“好评多”筛选选项，"
                "已直接跳到下一个关键词"
            )
            self.on_progress(target_keyword, index, total, "无好评多，已跳过")
            return "无好评多，已跳过"
        filtered_cards = self._wait_for_committed_results(
            target_keyword,
            previous_signature=product_signature,
        )
        if not filtered_cards:
            self.log(f"关键词“{target_keyword}”筛选后没有商品，继续下一个")
            self.on_progress(target_keyword, index, total, "筛选后无商品，已跳过")
            return "筛选后无商品，已跳过"
        filtered_signature = self._last_committed_result_signature
        filtered_search_id = (
            str(filtered_signature[1] or "")
            if filtered_signature is not None
            else ""
        )
        self.log(
            f"关键词“{target_keyword}”筛选结果已更新并稳定"
            + (
                f"（搜索版本 {filtered_search_id}）"
                if filtered_search_id
                else ""
            )
        )

        self.keyword = target_keyword
        self._keyword_confirmed = True
        self.on_keyword(target_keyword)
        self.on_progress(target_keyword, index, total, "正在自动下滑采集")
        count = self._scroll_keyword_to_end(
            target_keyword,
            expected_search_id=filtered_search_id,
        )
        self.log(f"关键词“{target_keyword}”已到末尾，本轮发现 {count} 个普通商品")
        self.on_progress(target_keyword, index, total, "已完成")
        return "已完成"

    @staticmethod
    def _is_fatal_automation_error(message: str) -> bool:
        return any(
            marker in message
            for marker in (
                "安全验证",
                "操作频繁",
                "访问过于频繁",
                "请完成验证",
                "登录后继续",
                "请先登录",
            )
        )

    @staticmethod
    def _is_search_result_timeout(message: str) -> bool:
        return "没有出现稳定结果" in message

    def _automation_loop(self) -> None:
        total = len(self.keywords)
        consecutive_failed_keywords = 0
        finished_since_reentry = (
            self.start_index % self.restart_keyword_interval
            if self.restart_keyword_interval > 0
            else 0
        )
        try:
            self.on_ready()
            for index, target_keyword in enumerate(
                self.keywords[self.start_index :],
                start=self.start_index + 1,
            ):
                if self._stop.is_set():
                    return
                retry_number = 0
                while not self._stop.is_set():
                    try:
                        outcome = self._collect_one_keyword(
                            target_keyword, index, total
                        )
                    except Exception as exc:
                        if self._stop.is_set():
                            return
                        message = str(exc)
                        if self._is_fatal_automation_error(message):
                            raise
                        should_switch_wechat = (
                            isinstance(exc, WechatSearchUnavailable)
                            or self._is_search_result_timeout(message)
                        ) and self._has_multiple_wechat_instances()
                        if should_switch_wechat:
                            self.log(
                                f"检测到{self._wechat_instance_name}已无法返回"
                                f"搜索结果：{message}；正在切换下一个微信"
                            )
                            self.on_progress(
                                target_keyword,
                                index,
                                total,
                                "搜索失效，正在切换微信",
                            )
                            self._switch_to_next_wechat(target_keyword)
                            retry_number = 0
                            consecutive_failed_keywords = 0
                            finished_since_reentry = 0
                            continue
                        if retry_number < MAX_KEYWORD_RETRIES:
                            retry_number += 1
                            stage = (
                                f"失败，60 秒后重试（{retry_number}/"
                                f"{MAX_KEYWORD_RETRIES}）"
                            )
                            self.log(
                                f"关键词“{target_keyword}”本次失败：{message}；"
                                f"60 秒后重试（{retry_number}/{MAX_KEYWORD_RETRIES}）"
                            )
                            self.on_progress(target_keyword, index, total, stage)
                            if self._stop.wait(KEYWORD_RETRY_SECONDS):
                                return
                            continue

                        consecutive_failed_keywords += 1
                        if (
                            consecutive_failed_keywords
                            >= MAX_CONSECUTIVE_FAILED_KEYWORDS
                        ):
                            raise RuntimeError(
                                f"连续 2 个关键词重试 3 次后仍失败；"
                                f"当前停在第 {index}/{total} 个“{target_keyword}”："
                                f"{message}"
                            ) from exc
                        self.log(
                            f"关键词“{target_keyword}”重试 3 次后仍失败，"
                            "已记录并跳到下一个关键词"
                        )
                        self.on_progress(
                            target_keyword, index, total, "失败后跳过"
                        )
                        self.on_keyword_finished(
                            target_keyword, index, total, "失败后跳过"
                        )
                        finished_since_reentry += 1
                        break
                    else:
                        consecutive_failed_keywords = 0
                        self.on_keyword_finished(
                            target_keyword, index, total, outcome
                        )
                        finished_since_reentry += 1
                        break
                if (
                    self.restart_keyword_interval > 0
                    and finished_since_reentry >= self.restart_keyword_interval
                    and index < total
                ):
                    next_keyword = self.keywords[index]
                    self.on_progress(
                        next_keyword,
                        index + 1,
                        total,
                        "正在完整重启微信",
                    )
                    self._reenter_shop_search_page(next_keyword)
                    finished_since_reentry = 0
            if not self._stop.is_set():
                self.log(f"全部 {total} 个关键词已自动采集完成")
                self.on_complete()
        except Exception as exc:
            if not self._stop.is_set():
                message = str(exc)
                self.log("自动采集已暂停：" + message)
                self.on_error(message)

    def _poll_loop(self) -> None:
        ready_sent = False
        last_error = ""
        while not self._stop.is_set():
            try:
                if self._socket is None:
                    self._connect()
                detected_keyword, search_id = self._read_keyword()
                if not detected_keyword:
                    raise RuntimeError(
                        "未读取到手机搜索词；请停留在微信商品搜索结果页"
                    )
                cards = self._read_cards(detected_keyword)
                keyword_just_confirmed = False
                if not self._keyword_confirmed or detected_keyword != self.keyword:
                    previous_context = self._candidate_context
                    committed = self._keyword_is_committed(
                        detected_keyword,
                        search_id,
                        bool(cards),
                    )
                    if self._candidate_context != previous_context:
                        self.log(
                            f"检测到已提交关键词：{detected_keyword}；"
                            "等待结果稳定后保存"
                        )
                    if not committed:
                        self._stop.wait(self.poll_seconds)
                        continue
                    self.keyword = detected_keyword
                    self._keyword_confirmed = True
                    self._seen.clear()
                    self.log(f"已确认手机关键词：{self.keyword}")
                    self.on_keyword(self.keyword)
                    keyword_just_confirmed = True
                if not ready_sent:
                    ready_sent = True
                    if not keyword_just_confirmed:
                        self.on_keyword(self.keyword)
                    self.on_ready()
                if detected_keyword != self.keyword:
                    self._stop.wait(self.poll_seconds)
                    continue
                changed = self._new_or_changed(cards)
                if changed:
                    self.log(f"搜索页新增或更新 {len(changed)} 个普通商品")
                    self.on_products(changed)
                last_error = ""
            except Exception as exc:
                message = str(exc)
                if message != last_error:
                    self.log(f"搜索页连接重试：{message}")
                    last_error = message
                socket = self._socket
                self._socket = None
                if socket is not None:
                    try:
                        socket.close()
                    except Exception:
                        pass
            self._stop.wait(self.poll_seconds)
