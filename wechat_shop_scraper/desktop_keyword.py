from __future__ import annotations

import ctypes
import hashlib
import json
import re
import subprocess
import threading
import time
import typing
import uuid
from dataclasses import dataclass, replace
from ctypes import wintypes
from pathlib import Path
from typing import Any, Callable, Iterable

from typing_extensions import NotRequired

from .desktop_proto import WmpfCdpBridge, encode_action_command_base64
from .desktop_bootstrap import ShopBootstrapTimeout, ShopBootstrapEntryUnavailable
from .model import ProductCard


if not hasattr(typing, "NotRequired"):
    typing.NotRequired = NotRequired  # type: ignore[attr-defined]


SHOP_APP_ID = "wxca8d4b8e8feedc2a"
SEARCH_PAGE_ROUTE = "subpackages/searchCenter/pages/globalSearch/globalSearch"
SHOP_HOME_ROUTE = "subpackages/shopWindow/pages/wxShopHome/index"
SHOP_HOME_ROUTES = {SHOP_HOME_ROUTE, "subpackages/shopWindow/pages/wxShopHomeLite/index"}
MAX_RECOVERY_SHOPS = 3
MAX_KEYWORD_RETRIES = 3
KEYWORD_RETRY_SECONDS = 60.0
RUNTIME_RECOVERY_RETRY_SECONDS = 2.0
HOST_RECOVERY_BACKOFF_SECONDS = (15.0, 30.0, 60.0, 120.0)
MAX_RUNTIME_RECOVERY_ATTEMPTS = 5
PAGE_REENTRY_MAX_ATTEMPTS = 3
PAGE_REENTRY_RETRY_SECONDS = 5.0
PAGE_READ_MAX_ATTEMPTS = 3
PAGE_READ_RETRY_SECONDS = 2.0
PAGE_READ_TIMEOUT_SECONDS = 30.0
_PROFILE_CACHE: dict[tuple[str, int, int, str, int, int], DesktopWechatProfile] = {}
_INITIALIZED_HOSTS: set[tuple[int, str]] = set()


LogCallback = Callable[[str], None]
ProductsCallback = Callable[[list[ProductCard]], None]
ReadyCallback = Callable[[], None]
KeywordCallback = Callable[[str], None]
ProgressCallback = Callable[[str, int, int, str], None]
CompleteCallback = Callable[[], None]
ErrorCallback = Callable[[str], None]
KeywordFinishedCallback = Callable[[str, int, int, str], None]


class DesktopWechatError(RuntimeError):
    pass


class DesktopHostLostError(DesktopWechatError):
    """The native connection was lost; the whole launch transaction is invalid."""


class DesktopRecoveryEntryUnavailable(DesktopWechatError):
    """The approved normal launch entry needs to become available again."""


class DesktopCollectionContextError(DesktopWechatError):
    """Transport exists, but the app-service collection context is not usable."""


class DesktopRecoveryExhausted(DesktopCollectionContextError):
    """Automatic recovery reached its safety cap without a usable session."""


class DesktopHookCompatibilityError(DesktopWechatError):
    """A WMPF child died immediately after an unverified native hook ran."""


def _is_transient_desktop_error(error: object) -> bool:
    message = str(error).casefold()
    if any(word in message for word in ("安全验证", "操作频繁", "访问过于频繁", "请完成验证", "请先登录", "登录后继续")):
        return False
    return isinstance(error, (TimeoutError, DesktopHostLostError, DesktopRecoveryEntryUnavailable,
                              DesktopCollectionContextError)) or any(
        word in message
        for word in (
            "timeout", "timed out", "超时", "connection", "连接", "调试通道",
            "采集通道未就绪", "运行环境尚未就绪",
            "session不存在", "session未绑定", "cdp客户端不存在", "cdp客户端不可用",
            "cdp 客户端仍未绑定", "cdp 客户端尚未绑定",
            "页面仍在初始化", "搜索页脚本未返回",
            "system error", "execution context",
            "script has been destroyed", "process not found", "session is detached",
        )
    )


def _is_structural_collection_error(message: object) -> bool:
    text = str(message).casefold()
    return any(token in text for token in (
        "search-module-unavailable", "require is not defined", "modules is not a function",
        "productsearchstore", "ec cardpackagesearch", "ec card package search",
    ))


def _invalidates_native_connection(error: object) -> bool:
    """Return whether recovery should poison the native host state.

    A live WMPF process with an unregistered app-service/CDP session is a
    context problem, not a detached process.  Marking it as native-lost makes
    every later call fail and causes needless stop/start hook churn.
    """
    if isinstance(error, DesktopCollectionContextError):
        return False
    text = str(error).casefold()
    return not any(token in text for token in (
        "session未绑定", "session不存在", "cdp客户端不可用",
        "cdp 客户端仍未绑定", "cdp 客户端尚未绑定",
        "微信正常启动连接未完成", "小店正常启动失败",
    ))


@dataclass(frozen=True)
class DesktopWechatProfile:
    weixin_sha256: str
    flue_sha256: str
    weixin_size: int
    flue_size: int
    weixin_ipc_task_offset: int
    wmpf_on_command_offset: int
    wmpf_reply_offset: int
    wmpf_load_start_offset: int
    wmpf_cdp_filter_offset: int
    scene_offsets: tuple[int, ...]
    source: str
    validation_note: str = ""


SUPPORTED_PROFILE = DesktopWechatProfile(
    weixin_sha256="8bf9ae1edf52676bbfa2e0c4024af207c0f9bba589758f99de467515c90635e1",
    flue_sha256="81a63467fb6647fef66bcde02594227ab457d9af39431499ba7a54f922849e74",
    weixin_size=198059560,
    flue_size=236554280,
    weixin_ipc_task_offset=88358416,
    wmpf_on_command_offset=40533472,
    wmpf_reply_offset=40540688,
    wmpf_load_start_offset=47086672,
    wmpf_cdp_filter_offset=60265824,
    scene_offsets=(64, 1536, 8, 1472, 16, 456),
    source="微信 4.1.13.63 / WMPF 25558",
)

# Official 4.1.13.65 installer (SHA256 A1978685...B57C5E1), statically
# compared against the working .63/25558 kernels. These are function starts,
# not guessed offsets or a relaxed version/size-only allowlist. See the binary
# comparison report and docs/desktop-4.1.13.65-compatibility-20260910.md.
_WEIXIN_65 = dict(
    weixin_sha256="8f7406a8a465e851ee10eaecccb572d0e5b4e00d480c770938c714c396097b74",
    weixin_size=198060584,
    weixin_ipc_task_offset=0x5444400,
)
_WMPF_25560 = dict(
    flue_sha256="60630cc1ba346b0e485fbf1981a07a650d1566e004c5b2b84933544860e2b3cf",
    flue_size=236554288,
    wmpf_load_start_offset=0x2ce7cf0,
    # Runtime A/B on 4.1.13.65/25560 showed the relocated CDP-filter address
    # is not called during page creation and its old +8 write is unsafe.  Keep
    # that hook disabled.  The scene gate is the part that actually produces a
    # usable app-service session on this kernel.
    wmpf_cdp_filter_offset=0,
    scene_offsets=(64, 1536, 8, 1472, 16, 456),
)
_WEIXIN_4159 = dict(
    # WeChat 4.1.15.9 installed on this machine. The IPC dispatcher was
    # matched against the 4.1.13.65 function shape and its stable call-site
    # string references; do not infer this from the product version alone.
    weixin_sha256="1b599c66ada05ab12f9850cffe9d44314abb0440fde3d74623ada1c71dc94508",
    weixin_size=201517608,
    weixin_ipc_task_offset=0x54167c0,
)
_WMPF_25710 = dict(
    # WMPF 2.5.7.25710. The reply function is the unique normalized match
    # from the verified 25560 HostCdpReplyBinding::Reply implementation.
    flue_sha256="3211ee33fd42f96edf8390e641af671a47b77d03cc09c4723dafdbae5cf75665",
    flue_size=237164080,
    # 25710's load-start and filter functions changed shape. Keeping these
    # hooks disabled avoids attaching to an unverified address; the CDP
    # bridge and verified reply hook remain available for an existing page.
    wmpf_load_start_offset=0,
    wmpf_cdp_filter_offset=0,
    scene_offsets=(),
)
_STATIC_COMPAT_NOTE = "此内核组合已完成静态接口核对，尚待该版本真实采集验证；建议先测试自动恢复"
# WeChat and WMPF update independently. Each explicitly allowed pair retains
# its own exact file hashes and the offsets for its actual two components.
SUPPORTED_PROFILES = (
    SUPPORTED_PROFILE,
    replace(SUPPORTED_PROFILE, **_WEIXIN_65, source="微信 4.1.13.65 / WMPF 25558",
            validation_note=_STATIC_COMPAT_NOTE),
    replace(SUPPORTED_PROFILE, **_WMPF_25560, source="微信 4.1.13.63 / WMPF 25560",
            validation_note=_STATIC_COMPAT_NOTE),
    replace(SUPPORTED_PROFILE, **_WEIXIN_65, **_WMPF_25560, source="微信 4.1.13.65 / WMPF 25560",
            validation_note=_STATIC_COMPAT_NOTE),
    replace(SUPPORTED_PROFILE, **_WEIXIN_4159, **_WMPF_25710,
            wmpf_on_command_offset=0,
            wmpf_reply_offset=0x26ba220,
            source="微信 4.1.15.9 / WMPF 25710",
            validation_note="已匹配本机微信 4.1.15.9 / WMPF 25710；仅启用已核对的 IPC 与回包入口，未验证的页面钩子已关闭"),
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_desktop_profile(
    weixin_path: str | Path,
    flue_path: str | Path,
) -> DesktopWechatProfile:
    weixin = Path(weixin_path)
    flue = Path(flue_path)
    if not weixin.is_file() or not flue.is_file():
        raise DesktopWechatError("无法读取电脑版微信的 Weixin.dll 或 flue.dll")
    weixin_stat, flue_stat = weixin.stat(), flue.stat()
    candidates = [profile for profile in SUPPORTED_PROFILES
                  if profile.weixin_size == weixin_stat.st_size and profile.flue_size == flue_stat.st_size]
    if not candidates:
        raise DesktopWechatError(
            "当前电脑版微信内核组合暂未适配。现支持微信 4.1.13.63 / 4.1.13.65 / 4.1.15.9，"
            "小程序 WMPF 25558 / 25560 / 25710 的指定文件；请不要强行运行，以免微信崩溃。"
            f"文件大小：Weixin.dll={weixin_stat.st_size}，flue.dll={flue_stat.st_size}"
        )
    cache_key = (
        str(weixin.resolve()).casefold(),
        weixin_stat.st_size,
        weixin_stat.st_mtime_ns,
        str(flue.resolve()).casefold(),
        flue_stat.st_size,
        flue_stat.st_mtime_ns,
    )
    cached = _PROFILE_CACHE.get(cache_key)
    if cached is not None:
        return cached
    weixin_hash, flue_hash = _sha256(weixin).lower(), _sha256(flue).lower()
    for profile in candidates:
        if (weixin_hash == profile.weixin_sha256 and flue_hash == profile.flue_sha256):
            _PROFILE_CACHE.clear()
            _PROFILE_CACHE[cache_key] = profile
            return profile
    raise DesktopWechatError(
        "当前电脑版微信文件与已核对版本不一致，已停止挂载以保护微信进程。"
        f"文件校验标识：Weixin.dll={weixin_hash[:16]}，flue.dll={flue_hash[:16]}"
    )


def _powershell_json(command: str) -> Any:
    wrapped = (
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
        + command
        + " | ConvertTo-Json -Compress"
    )
    try:
        output = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command", wrapped],
            stderr=subprocess.STDOUT,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).decode("utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError) as exc:
        raise DesktopWechatError(f"无法读取电脑版微信进程：{exc}") from exc
    text = output.lstrip("\ufeff").strip()
    if not text:
        return []
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise DesktopWechatError("电脑版微信进程信息解析失败") from exc


def _visible_weixin_identity() -> tuple[int, int]:
    rows = _powershell_json(
        "Get-Process Weixin -ErrorAction SilentlyContinue | "
        "Select-Object Id,MainWindowHandle"
    )
    if isinstance(rows, dict):
        rows = [rows]
    candidates = {
        int(row.get("Id") or 0) for row in rows if isinstance(row, dict)
    }
    candidates.discard(0)
    if not candidates:
        return 0, 0
    result = (0, 0)
    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    @callback_type
    def callback(hwnd: int, _parameter: int) -> bool:
        nonlocal result
        if not user32.IsWindowVisible(hwnd):
            return True
        process_id = ctypes.c_ulong()
        thread_id = int(user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id)) or 0)
        if int(process_id.value) in candidates:
            result = int(process_id.value), thread_id
            return False
        return True

    user32.EnumWindows(callback, 0)
    return result


def _wmpf_root_pid(weixin_pid: int) -> int:
    rows = _powershell_json(
        "Get-CimInstance Win32_Process | "
        "Where-Object {($_.Name -in @('WeChatAppEx.exe','Weixin.exe')) -and "
        "$_.CommandLine -match '--wmpf_root_dir=' -and "
        "$_.CommandLine -notmatch '--type='} | "
        "Select-Object ProcessId,ParentProcessId,ExecutablePath,CreationDate,CommandLine"
    )
    if isinstance(rows, dict):
        rows = [rows]
    entries = [row for row in rows if isinstance(row, dict)]
    if not entries:
        return 0

    def score(row: dict[str, Any]) -> tuple[int, int, str]:
        command = str(row.get("CommandLine") or "").casefold()
        parent = int(row.get("ParentProcessId") or 0)
        return (
            int(parent == weixin_pid),
            int("--web-translate" not in command),
            str(row.get("CreationDate") or ""),
        )

    selected = max(entries, key=score)
    return int(selected.get("ProcessId") or 0)


def _has_visible_shop_window(pid: int) -> bool:
    """Check the real top-level WMPF window, including Chromium-hosted shells.

    Get-Process.MainWindowHandle is zero for some WeChatAppEx builds even
    though EnumWindows exposes the visible 小店 shell.  The old check treated
    that valid host as an uninitialized background process and forced a long
    recovery loop.
    """
    try:
        user32 = ctypes.windll.user32
        enum_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        found = False
        @enum_type
        def visit(hwnd, _):
            nonlocal found
            if found:
                return False
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value != int(pid) or not user32.IsWindowVisible(hwnd):
                return True
            title = ctypes.create_unicode_buffer(128)
            user32.GetWindowTextW(hwnd, title, 128)
            if title.value in {"小店", "小店与卡包"}:
                found = True
                return False
            return True
        user32.EnumWindows(visit, 0)
        return found
    except (AttributeError, OSError):
        return False


def _launch_wmpf_host(weixin_pid: int, timeout: float = 15.0) -> int:
    uri = f"weixin://launchapplet/?app_id={SHOP_APP_ID}"
    try:
        result = int(
            ctypes.windll.shell32.ShellExecuteW(  # type: ignore[attr-defined]
                None,
                "open",
                uri,
                None,
                None,
                0,
            )
        )
    except (AttributeError, OSError) as exc:
        raise DesktopWechatError("无法调用微信小店后台启动协议") from exc
    if result <= 32:
        raise DesktopWechatError(
            f"微信小店后台启动协议未被微信接收（错误码 {result}）"
        )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pid = _wmpf_root_pid(weixin_pid)
        if pid:
            # The translation/background WMPF can predate this launch. Merely
            # finding its PID is not evidence that the URI opened a shop.
            # The caller still validates native initialization and the real
            # search runtime after this first, window-level confirmation.
            row = _powershell_json(
                f"Get-Process -Id {int(pid)} -ErrorAction SilentlyContinue | "
                "Select-Object MainWindowHandle,MainWindowTitle"
            )
            if (isinstance(row, dict) and row.get("MainWindowHandle")
                    and row.get("MainWindowTitle") in {"小店", "小店与卡包"}):
                return pid
        time.sleep(0.3)
    raise DesktopWechatError(
        "微信小店启动协议已调用，但未打开小店窗口；"
        "后台进程存在不代表小店已就绪，需要可用的正常启动入口；无需退出微信"
    )


def loaded_module_path(process_id: int, module_name: str) -> Path | None:
    class MODULEENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("th32ModuleID", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("GlblcntUsage", wintypes.DWORD),
            ("ProccntUsage", wintypes.DWORD),
            ("modBaseAddr", ctypes.POINTER(ctypes.c_ubyte)),
            ("modBaseSize", wintypes.DWORD),
            ("hModule", wintypes.HMODULE),
            ("szModule", wintypes.WCHAR * 256),
            ("szExePath", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000008 | 0x00000010, int(process_id))
    invalid_handle = ctypes.c_void_p(-1).value
    if not snapshot or snapshot == invalid_handle:
        return None
    entry = MODULEENTRY32W()
    entry.dwSize = ctypes.sizeof(entry)
    try:
        success = bool(kernel32.Module32FirstW(snapshot, ctypes.byref(entry)))
        while success:
            if str(entry.szModule).casefold() == module_name.casefold():
                path = Path(str(entry.szExePath))
                return path if path.is_file() else None
            success = bool(kernel32.Module32NextW(snapshot, ctypes.byref(entry)))
    finally:
        kernel32.CloseHandle(snapshot)
    return None


def _build_weixin_script(offset: int) -> str:
    return f"""
'use strict';
const module = Process.findModuleByName('Weixin.dll');
if (!module) throw new Error('Weixin.dll not loaded');
const dispatch = new NativeFunction(module.base.add({int(offset)}), 'void', ['pointer']);
const held = new Map();
const HOLD_TIMEOUT_MS = 120000;
function utf8ByteLength(value) {{
  try {{ return unescape(encodeURIComponent(String(value))).length; }}
  catch (_) {{ return String(value).length; }}
}}
function writeStdString(object, value) {{
  const data = Memory.allocUtf8String(value);
  const byteLength = utf8ByteLength(value);
  object.writePointer(data);
  object.add(0x10).writeU64(byteLength);
  object.add(0x18).writeU64(Math.max(0x1f, byteLength));
  return data;
}}
function releaseDispatch(callId) {{
  const key = String(callId || '');
  const item = held.get(key);
  if (!item) return false;
  clearTimeout(item.timer);
  held.delete(key);
  return true;
}}
rpc.exports = {{
  dispatch(payload, threadId) {{
    let callId = '';
    try {{ callId = String(JSON.parse(payload).call_id || ''); }} catch (_) {{}}
    if (!callId) throw new Error('missing dispatch call id');
    if (held.has(callId)) throw new Error('duplicate dispatch call id');
    if (held.size >= 256) throw new Error('dispatch hold limit reached');
    const task = Memory.alloc(0x80);
    task.writeByteArray(new Uint8Array(0x80));
    const data = writeStdString(task.add(0x28), payload);
    const timer = setTimeout(function() {{ releaseDispatch(callId); }}, HOLD_TIMEOUT_MS);
    held.set(callId, {{task:task, data:data, timer:timer}});
    return Process.runOnThread(Number(threadId), function() {{ dispatch(task); }}).then(
      function() {{ return {{callId:callId, task:String(task),data:String(data)}}; }}
    );
  }},
  releaseDispatch(callId) {{
    return releaseDispatch(callId);
  }}
}};
"""


def _build_wmpf_script(profile: DesktopWechatProfile) -> str:
    scene_offsets = json.dumps(list(profile.scene_offsets), separators=(",", ":"))
    return f"""
'use strict';
const module = Process.findModuleByName('flue.dll');
if (!module) throw new Error('flue.dll not loaded');
const loadStartOffset={profile.wmpf_load_start_offset};
const cdpFilterOffset={profile.wmpf_cdp_filter_offset};
const sceneOffsets={scene_offsets};
const knownScenes=[1000,1005,1007,1008,1027,1035,1053,1074,1145,1178,1256,1260,1302,1308];
let scenePatched=false;
function isBadPtr(value) {{
  if(!value||value.isNull()) return true;
  const invalid=['0xaaaaaaaaaaaaaaaa','0xcccccccccccccccc','0xcdcdcdcdcdcdcdcd','0xfefefefefefefefe','0xdddddddddddddddd','0xfeeefeeefeeefeee'];
  return invalid.some(item=>{{try{{return value.equals(ptr(item));}}catch(e){{return false;}}}});
}}
function patchScene(a1) {{
  if(scenePatched||sceneOffsets.length!==6||isBadPtr(a1)) return;
  try {{
    const readAt=(value,offset)=>{{const next=value.add(offset).readPointer();if(isBadPtr(next))throw new Error('invalid scene pointer');return next;}};
    const config=readAt(readAt(a1,sceneOffsets[0]),sceneOffsets[1]);
    const scene=readAt(readAt(readAt(config,sceneOffsets[2]),sceneOffsets[3]),sceneOffsets[4]).add(sceneOffsets[5]);
    const current=scene.readInt();
    if(!knownScenes.includes(current)) return;
    scene.writeInt(1101); scenePatched=scene.readInt()===1101;
  }} catch(e) {{}}
}}
if(loadStartOffset) Interceptor.attach(module.base.add(loadStartOffset), {{onEnter(args){{
  try{{if(args[1].and(0xff).toInt32()!==1)args[1]=args[1].and(ptr('0xffffffffffffff00')).or(1);patchScene(args[0]);}}catch(e){{}}
}}}});
if(cdpFilterOffset) Interceptor.attach(module.base.add(cdpFilterOffset), {{
  onEnter(args){{this.inputValue=args[0];}},
  onLeave(){{try{{const value=this.inputValue.readPointer();if(isBadPtr(value))return;const status=value.add(8);if(status.readU32()===6)status.writeU32(0);}}catch(e){{}}}}
}});
function readString(value) {{
  try{{const marker=value.add(0x17).readS8();const size=marker<0?Number(value.add(8).readU64()):marker;if(size<0||size>67108864)return '';const data=marker<0?value.readPointer():value;return data.readUtf8String(size)||'';}}catch(e){{return '';}}
}}
Interceptor.attach(module.base.add({profile.wmpf_reply_offset}), {{onEnter(args){{
  const rows=[];try{{const begin=args[2].readPointer();const end=args[2].add(Process.pointerSize).readPointer();for(let item=begin;item.compare(end)<0&&rows.length<128;item=item.add(0x18))rows.push(readString(item));}}catch(e){{}}
  send({{event:'reply',error_code:args[1].toInt32(),session_id:readString(args[0].add(8)),command_id:readString(args[0].add(0x20)),call_id:readString(args[0].add(0x38)),error:readString(args[3]),responses:rows}});
}}}});
rpc.exports={{resetScenePatch(){{scenePatched=false;return true;}}}};
send({{event:'ready',pid:Process.id}});
"""


def _json_result(value: Any) -> Any:
    current = value
    for _index in range(3):
        if not isinstance(current, str):
            break
        text = current.strip()
        if not text:
            return ""
        try:
            current = json.loads(text)
        except (ValueError, TypeError, json.JSONDecodeError):
            return current
    return current


def _decode_xweb_response(raw: str) -> Any:
    try:
        response = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise DesktopWechatError("电脑版微信返回了无效数据") from exc
    if not isinstance(response, dict):
        raise DesktopWechatError("电脑版微信返回结构无效")
    try:
        code = int(response.get("code") or 0)
    except (TypeError, ValueError):
        code = -1
    if code:
        message = response.get("message") or response.get("error") or f"错误码 {code}"
        raise DesktopWechatError(str(message))
    if "data" in response:
        data = response.get("data")
        if isinstance(data, dict) and "event" in data and "data" in data:
            event_data = data.get("data")
            if (
                isinstance(event_data, dict)
                and str(data.get("event") or "").endswith("OnEvaluateJavaScript")
                and "result" in event_data
            ):
                return event_data.get("result")
            return event_data
        return data
    if "result" in response:
        return response.get("result")
    return response


SEARCH_START_SCRIPT = r"""
(()=>{try{
  const req=__REQUEST__;
  const pages=typeof getCurrentPages==='function'?getCurrentPages():[];
  const page=pages.length?pages[pages.length-1]:null;
  if(!page||String(page.route||page.__route__||'').indexOf('globalSearch')<0)
    return JSON.stringify({ok:false,error:'not-on-search'});
  const keyword=String(req.keyword||'').trim();
  page.setData({inputValue:keyword});
  let method='';
  if(typeof page.goSearch==='function'){
    page.goSearch({query:keyword,searchType:1});
    method='goSearch';
  }
  else if(typeof page.handleSearch==='function'){
    page.handleSearch({detail:{query:keyword,searchType:1}});
    method='handleSearch';
  }
  else return JSON.stringify({ok:false,error:'no-search-function'});
  return JSON.stringify({ok:true,method:method});
}catch(e){return JSON.stringify({ok:false,error:String(e)});}})()
"""


SEARCH_FILTER_SCRIPT = r"""
(()=>{try{
  const req=__REQUEST__;
  const pages=typeof getCurrentPages==='function'?getCurrentPages():[];
  const page=pages.length?pages[pages.length-1]:null;
  if(!page||String(page.route||page.__route__||'').indexOf('globalSearch')<0)
    return JSON.stringify({ok:false,error:'not-on-search'});
  const keyword=String(req.keyword||'').trim();
  const clone=value=>JSON.parse(JSON.stringify(value||{}));
  const filters=clone((page.data&&page.data.halfScreenFilter)||{});
  if(!filters||typeof filters!=='object'||Object.keys(filters).length===0)
    return JSON.stringify({ok:false,error:'filter-not-ready'});
  let found=false;
  const label=node=>String(node&&(node.name||node.title||node.label||node.text||node.wording||node.desc)||'');
  const mark=(node,parent)=>{
    if(!node||typeof node!=='object')return;
    if(label(node).indexOf('好评多')>=0){
      found=true;
      ['selected','isSelected','isActive','checked'].forEach(key=>{if(key in node)node[key]=true;});
      if('value' in node&&parent&&typeof parent==='object'){
        ['selectedValue','selectValue','currentValue'].forEach(key=>{if(key in parent)parent[key]=node.value;});
      }
    }
    Object.keys(node).forEach(key=>{
      const value=node[key];
      if(Array.isArray(value))value.forEach(item=>mark(item,node));
      else if(value&&typeof value==='object')mark(value,node);
    });
  };
  mark(filters,null);
  if(!found)return JSON.stringify({ok:true,skipped:true,reason:'no-good-reviews'});
  if(filters.priceRange){
    if(req.minPrice!==null)filters.priceRange.minPrice=String(req.minPrice);
    if(req.maxPrice!==null)filters.priceRange.maxPrice=String(req.maxPrice);
  }
  const sortList=clone((page.data&&page.data.sortList)||[]);
  page.setData({inputValue:keyword,sortList:sortList,halfScreenFilter:filters});
  if(typeof page.onFilterChange!=='function')
    return JSON.stringify({ok:false,error:'no-filter-function'});
  page.onFilterChange({sortList:sortList,halfScreenFilter:filters,searchType:'filter'});
  return JSON.stringify({ok:true,skipped:false});
}catch(e){return JSON.stringify({ok:false,error:String(e)});}})()
"""


SEARCH_STATE_SCRIPT = r"""
(()=>{try{
  const pages=typeof getCurrentPages==='function'?getCurrentPages():[];
  const page=pages.length?pages[pages.length-1]:null;
  const mod=require('subpackages/searchCenter/store/productSearchStore.js');
  const store=mod.productSearchStore||{};
  return JSON.stringify({
    route:String(page&&(page.route||page.__route__)||''),
    keyword:String(store.searchKeyword||''),
    offset:Number(store.offset||0),limit:Number(store.limit||10),
    hasMore:!!store.hasMore,searchCommInfo:store.searchCommInfo||{},
    loading:!!((page&&page.data&&(page.data.loading||page.data.bottomLoading))||store.loading)
  });
}catch(e){return JSON.stringify({route:'',keyword:'',offset:0,limit:10,hasMore:false,searchCommInfo:{},loading:false,error:String(e)});}})()
"""


SEARCH_PAGE_SCRIPT = r"""
(async()=>{try{
  const offset=__OFFSET__;
  const searchInfo=__SEARCH_INFO__;
  const mod=require('subpackages/searchCenter/store/productSearchStore.js');
  const api=require('subpackages/searchCenter/_shared/cgi/ecSearch/index.js');
  const store=mod.productSearchStore;
  if(!store||!api||typeof api.ecCardPackageSearch!=='function')
    return JSON.stringify({ok:false,error:'search-module-unavailable'});
  const pageOffset=offset===null?Number(store.offset||0):Number(offset);
  const request={reqType:Number(store.reqType||56),query:String(store.searchKeyword||''),
    offset:pageOffset,size:Number(store.limit||10),
    searchCommInfo:searchInfo===null?(store.searchCommInfo||{}):searchInfo,
    sessionId:String(store.sessionIdSearch||''),queryScene:store.parentSearchId,
    parentsearchid:String(store.reportParentSearchId||''),language:'zh_CN',clientVersion:10};
  if(store.selectedSlideFilter)request.slideFilterV2=store.selectedSlideFilter;
  const response=await api.ecCardPackageSearch(request);
  const msg=response&&response.msg||{};
  const sales=value=>{const text=String(value||'').normalize('NFKC').replace(/,/g,'').toLowerCase();const m=text.match(/[0-9.]+/);if(!m)return 0;let n=Number(m[0])||0;if(text.includes('万')||text.includes('w'))n*=10000;else if(text.includes('千')||text.includes('k'))n*=1000;return Math.round(n);};
  const image=value=>{const seen=new Set();const url=value=>typeof value==='string'&&/^https?:\/\//.test(value)?value:'';const walk=(node,depth)=>{if(depth>4||node===null||node===undefined||seen.has(node))return '';const direct=url(node);if(direct)return direct;if(typeof node!=='object')return '';seen.add(node);for(const key of ['mainImage','mainImg','mainImageUrl','mainImgUrl','primaryImage','primaryImg','imgUrl','imageUrl']){const hit=walk(node[key],depth+1);if(hit)return hit;}for(const key of ['mainImages','imageList','imageUrls','imgUrls','images','pics','picList']){const list=node[key];if(Array.isArray(list)){for(const item of list){const hit=walk(item,depth+1);if(hit)return hit;}}}for(const key of ['productCard','productInfo','goodsInfo','itemInfo']){const hit=walk(node[key],depth+1);if(hit)return hit;}return '';};return walk(value,0);};
  const products=[];
  (Array.isArray(msg.itemList)?msg.itemList:[]).forEach(item=>{
    const productItem=item&&item.productItem||{};const card=productItem.productCard;if(!card)return;
    if(item.liveItem||productItem.liveCard||card.isLive||card.liveStatus)return;
    const shopCard=productItem.shopCard||{};const profile=shopCard.profileInfo||{};
    const productId=String(card.productId||'');if(!/^\d{8,20}$/.test(productId))return;
    const salesText=String(card.salesWording||card.sales||'');
    products.push({productId:productId,title:String(card.title||''),mainImage:image(card)||image(productItem),
      sellingPrice:Number(card.sellingPrice||card.price||0),sales:sales(salesText),salesKnown:/\d/.test(salesText),
      salesText:salesText,shop:String(profile.shopWindowProfileName||card.shopName||''),
      appid:String(card.appid||card.realappid||card.realAppid||'')});
  });
  const itemCount=Array.isArray(msg.itemList)?msg.itemList.length:0;
  const reportedNext=Number(msg.nextOffset||msg.offset||0);
  const nextOffset=reportedNext>pageOffset?reportedNext:pageOffset+itemCount;
  return JSON.stringify({ok:true,offset:pageOffset,nextOffset:nextOffset,
    hasMore:!!msg.hasMore,searchCommInfo:msg.searchCommInfo||request.searchCommInfo||{},
    products:products,itemCount:itemCount});
}catch(e){return JSON.stringify({ok:false,error:String(e),stack:e&&e.stack});}})()
"""


OPEN_SEARCH_SCRIPT = f"""
(()=>{{
  const target='/{SEARCH_PAGE_ROUTE}';
  const pages=typeof getCurrentPages==='function'?getCurrentPages():[];
  const page=pages.length?pages[pages.length-1]:null;
  if(page&&String(page.route||page.__route__||'').indexOf('globalSearch')>=0)
    return JSON.stringify({{ok:true,already:true}});
  try{{
    if(typeof wx==='undefined')return JSON.stringify({{ok:false,error:'wx-unavailable'}});
    // XWeb.EvaluateJavaScript on WeChat 4.1.13.65 does not reliably await
    // an async IIFE. Schedule navigation through callbacks and return a
    // synchronous acknowledgement; the caller verifies the route separately.
    const navigate=()=>{{
      try{{
        wx.navigateTo({{url:target,success:()=>{{}},fail:()=>{{
          try{{wx.redirectTo({{url:target,success:()=>{{}},fail:()=>{{}}}});}}catch(e){{}}
        }}}});
      }}catch(e){{
        try{{wx.redirectTo({{url:target,success:()=>{{}},fail:()=>{{}}}});}}catch(_e){{}}
      }}
    }};
    if(typeof wx.loadSubpackage==='function'){{
      let scheduled=false;
      for(const name of ['subpackages/searchCenter/','subpackages/searchCenter']){{
        try{{wx.loadSubpackage({{name,success:navigate,fail:navigate}});scheduled=true;break;}}catch(e){{}}
      }}
      if(!scheduled)navigate();
    }}else navigate();
    return JSON.stringify({{ok:true,pending:true}});
  }}catch(e){{return JSON.stringify({{ok:false,error:String(e)}});}}
}})()
"""


SHOP_HOME_STATE_SCRIPT = r"""
(()=>{
  const pages=typeof getCurrentPages==='function'?getCurrentPages():[];
  const page=pages.length?pages[pages.length-1]:{};
  const data=page.data||{};
  return JSON.stringify({route:String(page.route||page.__route__||''),
    appid:String(data.currentAppid||''),name:String(data.storeName||''),
    ready:!!(data.currentAppid&&Array.isArray(data.tabs)&&data.tabs.length)});
})()
"""


class DesktopWechatBackend:
    def __init__(self, log: LogCallback | None = None, *, host_state_path: Path | None = None,
                 normal_shop_opener: Callable[..., None] | None = None,
                 on_recovery_status: LogCallback | None = None) -> None:
        self.log = log or (lambda _message: None)
        self.on_recovery_status = on_recovery_status or (lambda _message: None)
        self.cdp_bridge = WmpfCdpBridge(self.log)
        self.device: Any = None
        self.weixin_session: Any = None
        self.wmpf_session: Any = None
        self.weixin_script: Any = None
        self.wmpf_script: Any = None
        self.weixin_pid = 0
        self.wmpf_pid = 0
        self.thread_id = 0
        self.profile: DesktopWechatProfile | None = None
        self._xweb_replies_available: bool | None = None
        self._counter = 0
        self._pending: dict[str, tuple[threading.Event, dict[str, Any]]] = {}
        self._async_commands: dict[str, str] = {}
        self._lock = threading.RLock()
        # Serialize attach/dispatch/detach. A stop must never unload a script
        # while a different thread is still inside its native dispatch RPC.
        self._native_lock = threading.RLock()
        self.cancel_event = threading.Event()
        self._resume_requires_reopen = False
        self.host_state_path = host_state_path
        self.normal_shop_opener = normal_shop_opener
        self._normal_bootstrap_in_progress = False
        # The built-in recovery entry already renders the search page before
        # the Frida/CDP connection is attached.  Keep that fact so startup can
        # reuse the visible page instead of closing it and launching a second
        # applet session immediately.
        self._normal_entry_opened_search = False
        # The route probe can time out on WeChat 4.1.13.65 even though the
        # normal opener has rendered a usable search page.
        self._normal_entry_search_reused = False
        self._search_page_ready = False
        self._native_lost = threading.Event()
        self._native_fatal_error: DesktopWechatError | None = None
        self._hooks_attached_at = 0.0
        self._launch_connection: tuple[Any, ...] | None = None
        self._host_identity: tuple[int, str] | None = None
        self._recovery_shops: list[dict[str, str]] = []
        self._shop_state_path = (
            host_state_path.with_name("desktop-recovery-shops.json")
            if host_state_path is not None else None
        )
        if self._shop_state_path is not None:
            try:
                self._remember_recovery_shops(
                    json.loads(self._shop_state_path.read_text(encoding="utf-8")), persist=False
                )
            except (OSError, ValueError, TypeError):
                pass

    def _remember_recovery_shops(self, rows: object, *, persist: bool = True) -> None:
        # Only public shop IDs already returned by the user's own searches.
        # Never reuse page cursors, authentication data or a hard-coded shop.
        if not isinstance(rows, list):
            return
        candidates: list[dict[str, str]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            appid = str(row.get("appid") or "")
            if appid == SHOP_APP_ID or not re.fullmatch(r"wx[0-9A-Za-z]{10,30}", appid):
                continue
            if any(shop["appid"] == appid for shop in candidates):
                continue
            candidates.append({"appid": appid, "shop": str(row.get("shop") or "")[:120]})
            if len(candidates) >= MAX_RECOVERY_SHOPS:
                break
        combined = candidates + [shop for shop in self._recovery_shops
                                 if not any(new["appid"] == shop["appid"] for new in candidates)]
        combined = combined[:MAX_RECOVERY_SHOPS]
        if combined == self._recovery_shops:
            return
        self._recovery_shops = combined
        if persist and self._shop_state_path is not None:
            try:
                self._shop_state_path.parent.mkdir(parents=True, exist_ok=True)
                temporary = self._shop_state_path.with_suffix(".tmp")
                temporary.write_text(json.dumps(combined, ensure_ascii=False), encoding="utf-8")
                temporary.replace(self._shop_state_path)
            except OSError:
                self.log("小店首页恢复入口暂未保存到磁盘；本次采集仍可使用内存中的入口")

    def _require_initialized_host(self, pid: int) -> None:
        # A background --web-translate host exists before WeChat initializes
        # its applet services. Sending XWeb commands then caused flue.dll CHECK
        # crashes on this supported build. Never use PID existence as readiness.
        row = _powershell_json(
            f"Get-Process -Id {int(pid)} -ErrorAction SilentlyContinue | "
            "Select-Object @{Name='started';Expression={$_.StartTime.ToUniversalTime().Ticks}},"
            "MainWindowHandle,MainWindowTitle"
        )
        if not isinstance(row, dict) or not row.get("started"):
            raise DesktopWechatError("电脑版微信小程序进程已退出，请重新打开一个小店名片")
        identity = (pid, str(row["started"]))
        visible_shop = (
            bool(row.get("MainWindowHandle"))
            and row.get("MainWindowTitle") in {"小店", "小店与卡包"}
        ) or _has_visible_shop_window(pid)
        remembered = identity in _INITIALIZED_HOSTS
        if not remembered and self.host_state_path is not None:
            try:
                saved = json.loads(self.host_state_path.read_text(encoding="utf-8"))
                remembered = saved == {"pid": identity[0], "started": identity[1]}
            except (OSError, ValueError, TypeError):
                pass
        if not visible_shop and not remembered:
            raise DesktopWechatError(
                "微信小店尚未初始化，已阻止不安全的后台连接。"
                "请先在文件传输助手点开一个小店名片，等店铺出现后再开始采集；无需退出微信"
            )
        self._host_identity = identity

    def _remember_initialized_host(self) -> None:
        # Record only after the actual globalSearch page responds successfully.
        # Process creation time prevents stale markers accepting reused PIDs.
        if self._host_identity is None:
            return
        _INITIALIZED_HOSTS.add(self._host_identity)
        if self.host_state_path is not None:
            try:
                self.host_state_path.parent.mkdir(parents=True, exist_ok=True)
                self.host_state_path.write_text(json.dumps({
                    "pid": self._host_identity[0], "started": self._host_identity[1],
                }), encoding="utf-8")
            except OSError:
                self.log("无法保存小店初始化状态；下次打开软件时请保持小店窗口打开")

    @property
    def active(self) -> bool:
        return bool(
            self.weixin_session and self.wmpf_session and self.weixin_script
            and self.wmpf_script and not self._native_lost.is_set()
            and getattr(self.weixin_session, "is_detached", False) is not True
            and getattr(self.wmpf_session, "is_detached", False) is not True
        )

    @property
    def needs_recovery(self) -> bool:
        """Whether a native detach occurred after the last successful start."""
        return self._native_lost.is_set()

    def _connection_signature(self) -> tuple[Any, ...]:
        return (self.weixin_session, self.wmpf_session, self.weixin_script, self.wmpf_script)

    def _check_launch_connection(self) -> None:
        # Native commands must not silently start a replacement connection in
        # the middle of Close -> reset -> Launch. Its CDP epoch belongs elsewhere.
        if self._native_fatal_error is not None:
            raise self._native_fatal_error
        if self._launch_connection is not None and (
            not self.active or any(a is not b for a, b in
                                   zip(self._launch_connection, self._connection_signature()))
        ):
            raise DesktopHostLostError("小程序宿主连接已失效，取消旧启动任务，等待完整恢复")

    def _mark_native_lost(self, detail: str) -> None:
        # A Frida callback must never take _native_lock: dispatch may be waiting
        # for this callback while holding that lock. Wake pending calls instead.
        first = not self._native_lost.is_set()
        self._native_lost.set()
        error = self._native_fatal_error or DesktopHostLostError(
            f"小程序宿主连接已失效：{detail}"
        )
        with self._lock:
            pending = list(self._pending.values())
            self._async_commands.clear()
        for event, holder in pending:
            holder['error'] = error
            event.set()
        self.cdp_bridge.prepare_relaunch()
        if first:
            self.log(str(error) + "；已作废旧页面，不再向失效会话发送启动命令")

    def _on_native_detached(self, session: Any, role: str, reason: str, _crash: Any = None) -> None:
        with self._lock:
            current = self.wmpf_session if role == '小程序' else self.weixin_session
            if current is not session:
                return  # Intentional release or a late callback from an older host.
            if (role == '小程序' and 'process-terminated' in str(reason).casefold()
                    and self.profile is not None
                    and 'WMPF 25560' in self.profile.source
                    and self._hooks_attached_at
                    and time.monotonic() - self._hooks_attached_at <= 30.0):
                self._native_fatal_error = DesktopHookCompatibilityError(
                    "WMPF 25560 小程序宿主在采集钩子装载后立即退出；"
                    "当前内核偏移尚未完成实机验证，已停止自动重启，避免反复崩溃。"
                    "请改用已验证的 25558 内核，或更新到包含 25560 实机偏移的版本。"
                )
            self._mark_native_lost(f"{role}进程 {self.wmpf_pid if role == '小程序' else self.weixin_pid}，{reason}")

    @property
    def fatal_error(self) -> DesktopWechatError | None:
        return self._native_fatal_error

    def _check_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise DesktopWechatError("电脑版微信采集已停止")

    def _on_wmpf_message(self, message: dict[str, Any], _data: bytes | None) -> None:
        if message.get("type") == "error":
            self.log("电脑版微信 Frida 错误：" + str(message.get("description") or ""))
            return
        payload = message.get("payload")
        if not isinstance(payload, dict) or payload.get("event") != "reply":
            return
        call_id = str(payload.get("call_id") or "")
        with self._lock:
            pending = self._pending.get(call_id)
            async_method = self._async_commands.pop(call_id, None)
        if async_method:
            try:
                if int(payload.get("error_code") or 0):
                    raise DesktopWechatError(str(payload.get("error") or payload["error_code"]))
                for row in payload.get("responses") or []:
                    _decode_xweb_response(str(row))
            except (DesktopWechatError, ValueError, TypeError) as exc:
                self.log(f"小程序后台命令回包失败：{async_method}；{exc}")
        if pending:
            event, holder = pending
            holder["reply"] = payload
            event.set()

    def start(self) -> None:
        with self._native_lock:
            self._start_connection()

    def _bootstrap_normal_shop(self, *, force_relaunch: bool = False) -> None:
        self._check_cancelled()
        if self.normal_shop_opener is None:
            raise DesktopWechatError("未配置小店正常启动入口")
        self._invoke_normal_entry(self.normal_shop_opener, force_relaunch=force_relaunch)

    def _bootstrap_normal_shop_compat(self, *, force_relaunch: bool = False) -> None:
        """Call launch extensions that may predate the relaunch flag."""
        try:
            self._bootstrap_normal_shop(force_relaunch=force_relaunch)
        except TypeError as exc:
            if (force_relaunch
                    and "unexpected keyword argument 'force_relaunch'" in str(exc)):
                self._bootstrap_normal_shop()
            else:
                raise

    def _rearm_normal_entry_after_attach(self) -> None:
        """Re-dispatch the normal entry after hooks are mounted.

        A normal entry may have rendered the search page before Frida was
        attached.  On WMPF 25560 that first page can remain visible while its
        XWeb session is unregistered.  Re-dispatching the same verified entry
        after the filter hook is installed gives WMPF a chance to register a
        fresh page session without closing the host or stacking native
        CloseApplet/LaunchApplet calls.
        """
        if (self.normal_shop_opener is None
                or not self._normal_entry_opened_search
                or self.cdp_bridge.runtime_ready):
            return
        self.log("小程序页面已先打开；采集钩子已就绪，关闭并重新打开正常入口以注册调试会话")
        self._normal_bootstrap_in_progress = True
        try:
            self._bootstrap_normal_shop_compat(force_relaunch=True)
        finally:
            self._normal_bootstrap_in_progress = False
        if not self.cdp_bridge.wait_runtime(15.0, cancel_event=self.cancel_event):
            raise DesktopWechatError(
                "正常入口已重新触发，但小程序调试会话仍未注册；当前关键词保留"
            )
        self.log("小程序调试会话已注册，继续建立采集连接")

    def _invoke_normal_entry(self, opener: Callable[..., None], *,
                             force_relaunch: bool = False) -> None:
        self._check_cancelled()
        weixin_pid, _ = _visible_weixin_identity()
        if not weixin_pid:
            raise DesktopWechatError("没有找到已登录的电脑版微信主窗口")
        try:
            entry_kwargs = dict(
                weixin_pid=weixin_pid, get_host_pid=lambda: _wmpf_root_pid(weixin_pid),
                cancel_event=self.cancel_event, log=self.log,
            )
            if force_relaunch:
                entry_kwargs["force_relaunch"] = True
            try:
                opener(**entry_kwargs)
            except TypeError as exc:
                # Keep compatibility with user supplied launchers written
                # before force_relaunch was added. Only retry the exact
                # signature mismatch; never hide a TypeError from inside the
                # launcher itself.
                if (force_relaunch
                        and "unexpected keyword argument 'force_relaunch'" in str(exc)):
                    entry_kwargs.pop("force_relaunch", None)
                    opener(**entry_kwargs)
                else:
                    raise
        except ShopBootstrapEntryUnavailable as exc:
            self._check_cancelled()
            raise DesktopRecoveryEntryUnavailable(str(exc)) from exc
        except ShopBootstrapTimeout as exc:
            self._check_cancelled()
            raise DesktopWechatError(f"普通小店页面等待超时：{exc}") from exc
        except Exception as exc:
            self._check_cancelled()
            raise DesktopWechatError(f"小店正常启动失败：{exc}") from exc
        self._check_cancelled()

    def _wait_recovery_entry(self, error: DesktopRecoveryEntryUnavailable, attempt: int) -> None:
        delay = HOST_RECOVERY_BACKOFF_SECONDS[min(attempt - 1, len(HOST_RECOVERY_BACKOFF_SECONDS) - 1)]
        message = (f"等待自动恢复入口：{error}；{delay:g} 秒后重新检查。"
                   "入口恢复后自动继续，不计关键词失败次数；可随时点停止")
        self.log(message)
        self.on_recovery_status(message)
        if self.cancel_event.wait(delay):
            self._check_cancelled()

    def prepare_recovery_entry(self) -> None:
        """Validate the normal entry before collecting, without closing a shop."""
        check = getattr(self.normal_shop_opener, "check_entry", None)
        if not callable(check):
            return
        attempt = 0
        while True:
            self._check_cancelled()
            try:
                self._invoke_normal_entry(check)
                description = getattr(self.normal_shop_opener, 'entry_description', None)
                if not isinstance(description, str):
                    description = '已识别独立文件传输助手中的 lining李宁卡片'
                self.log(f"自动恢复入口检查通过：{description}")
                self.on_recovery_status("自动恢复入口已确认，正在建立采集连接")
                return
            except DesktopRecoveryEntryUnavailable as exc:
                attempt += 1
                self._wait_recovery_entry(exc, attempt)

    def _start_connection(self) -> None:
        self._check_cancelled()
        if self.active:
            return
        self._normal_entry_opened_search = False
        self._normal_entry_search_reused = False
        self._search_page_ready = False
        if self.weixin_session or self.wmpf_session:
            self.stop()
        try:
            import frida  # type: ignore
        except ImportError as exc:
            raise DesktopWechatError("缺少 frida，无法连接电脑版微信") from exc
        weixin_pid, thread_id = _visible_weixin_identity()
        if not weixin_pid or not thread_id:
            raise DesktopWechatError("没有找到已登录的电脑版微信主窗口")
        # Recovery may deliberately keep the WMPF root alive while the old
        # page is replaced after hooks are mounted.  Preserve that marker so
        # _rearm_normal_entry_after_attach performs the one required rebuild.
        normal_entry_opened_search = bool(self._normal_bootstrap_in_progress)
        wmpf_pid = _wmpf_root_pid(weixin_pid)
        if not wmpf_pid:
            if self.normal_shop_opener is not None and not self._normal_bootstrap_in_progress:
                self._bootstrap_normal_shop()
                normal_entry_opened_search = True
                wmpf_pid = _wmpf_root_pid(weixin_pid)
            else:
                self.log("正在打开电脑版微信“小店与卡包”…")
                wmpf_pid = _launch_wmpf_host(weixin_pid)
        try:
            self._require_initialized_host(wmpf_pid)
        except DesktopWechatError:
            if self.normal_shop_opener is None or self._normal_bootstrap_in_progress:
                raise
            self._bootstrap_normal_shop()
            normal_entry_opened_search = True
            wmpf_pid = _wmpf_root_pid(weixin_pid)
            # Still enforce the guard on the refreshed PID; never write a marker
            # just because the normal opener returned successfully.
            self._require_initialized_host(wmpf_pid)
        weixin_path = loaded_module_path(weixin_pid, "Weixin.dll")
        flue_path = loaded_module_path(wmpf_pid, "flue.dll")
        if weixin_path is None or flue_path is None:
            raise DesktopWechatError("无法读取电脑版微信已加载的内核文件")
        self.log("正在校验电脑版微信版本（首次约需几秒）…")
        profile = validate_desktop_profile(weixin_path, flue_path)
        if profile.validation_note:
            self.log(profile.validation_note)
        self._check_cancelled()
        try:
            self.cdp_bridge.start()
            self._native_lost.clear()
            self.device = frida.get_local_device()
            self.wmpf_session = self.device.attach(wmpf_pid)
            wmpf_session = self.wmpf_session
            wmpf_session.on('detached', lambda reason, crash=None:
                            self._on_native_detached(wmpf_session, '小程序', reason, crash))
            self.wmpf_script = self.wmpf_session.create_script(
                _build_wmpf_script(profile)
            )
            self.wmpf_script.on("message", self._on_wmpf_message)
            self.wmpf_script.load()
            self._hooks_attached_at = time.monotonic()
            self.weixin_session = self.device.attach(weixin_pid)
            weixin_session = self.weixin_session
            weixin_session.on('detached', lambda reason, crash=None:
                              self._on_native_detached(weixin_session, '微信', reason, crash))
            self.weixin_script = self.weixin_session.create_script(
                _build_weixin_script(profile.weixin_ipc_task_offset)
            )
            self.weixin_script.load()
            self.weixin_pid = weixin_pid
            self.wmpf_pid = wmpf_pid
            self.thread_id = thread_id
            self.profile = profile
            self._check_cancelled()
            self._normal_entry_opened_search = normal_entry_opened_search
            self._rearm_normal_entry_after_attach()
            self._probe_xweb_version()
            self.log(
                f"电脑版微信采集通道已连接：{profile.source}；"
                f"微信 PID {weixin_pid}，小程序 PID {wmpf_pid}"
            )
        except Exception as exc:
            if self.cancel_event.is_set() and self.active:
                # A cancelled startup is still a reusable attached connection.
                return
            self.stop()
            if isinstance(exc, DesktopWechatError):
                raise
            raise DesktopWechatError(f"电脑版微信采集通道启动失败：{exc}") from exc

    def _next_call_id(self) -> str:
        with self._lock:
            self._counter += 1
            return f"desktop-keyword-{int(time.time() * 1000)}-{self._counter}"

    def _release_native_dispatch(self, script: Any, call_id: str) -> None:
        """Release the native task/string only after its reply was consumed.

        The Frida RPC allocates the native command on the script heap.  The
        Weixin worker may read it after ``dispatch`` returns, so dropping the
        JS references in the RPC promise is unsafe.  The script keeps them
        until this bounded, best-effort acknowledgement (or its timer).
        """
        if script is None:
            return
        try:
            release = getattr(script.exports_sync, "release_dispatch", None)
            if release is not None:
                release(call_id)
        except Exception as exc:
            # Detach/crash during cleanup is already handled by the pending
            # call; never replace the original collection error with cleanup.
            self.log(f"原生后台命令引用释放未完成：{exc}")

    def _probe_xweb_version(self) -> None:
        try:
            self.call("XWeb.version", {}, timeout=8.0)
            self._xweb_replies_available = True
        except DesktopWechatError as exc:
            message = str(exc).casefold()
            if "unsupported" in message or "不支持" in message:
                # Version metadata is independent of page evaluation. On
                # 25560 it is unsupported while a registered native session
                # DOES expose getCurrentPages/require (live verified).
                self._xweb_replies_available = None
                return
            if "后台命令超时" in message or "backend command timeout" in message:
                self._xweb_replies_available = None
                self.log(
                    "电脑版微信版本探测未回包，将继续实际检测小程序采集能力"
                )
                return
            raise

    def _ensure_collection_runtime(self, timeout: float) -> None:
        """Verify app-service globals, registering the native session only once.

        CDP AppIndex on 25560 is a rendering context, not the module runtime.
        Its ready flag can safely gate native launch, but cannot prove that
        keyword/store/API scripts work. Never route this probe through the
        automatic CDP fallback or treat a visible search window as success.
        """
        self._check_cancelled()
        script = "JSON.stringify({pages:typeof getCurrentPages,modules:typeof require,wx:typeof wx})"
        last_error = "逻辑层能力尚未返回"

        def probe() -> bool:
            nonlocal last_error
            self._check_cancelled()
            try:
                value = self.call("XWeb.EvaluateJavaScript", {"script": script}, timeout=5.0)
                if isinstance(value, dict) and "success" in value:
                    if not value.get("success"):
                        raise DesktopWechatError(str(value.get("error") or "采集能力探测失败"))
                    value = value.get("data")
                value = _json_result(value)
                if (isinstance(value, dict) and value.get("pages") == "function"
                        and value.get("modules") == "function" and value.get("wx") == "object"):
                    self._xweb_replies_available = True
                    return True
                last_error = f"逻辑层能力未就绪：{value}"
            except (DesktopWechatError, TimeoutError) as exc:
                self._check_cancelled()
                if not _is_transient_desktop_error(exc):
                    raise
                last_error = str(exc)
            return False

        if probe():
            return
        self._xweb_replies_available = None

        # Prefer the already-approved normal shop entry.  It rebuilds the
        # app-service page without sending CloseApplet/LaunchApplet through a
        # half-registered WMPF session (the crash path seen on 25560).
        if self.normal_shop_opener is not None and not self._normal_entry_opened_search:
            self.log("采集逻辑层未绑定；先重新触发已验证的小店入口，不直接强制重启小程序")
            self._normal_bootstrap_in_progress = True
            try:
                self._bootstrap_normal_shop_compat(force_relaunch=True)
                self._normal_entry_opened_search = True
            finally:
                self._normal_bootstrap_in_progress = False
            if self.cdp_bridge.runtime_ready:
                if probe():
                    self.log("正常小店入口已重新注册采集逻辑层")
                    return
            elif not self.cdp_bridge.wait_runtime(min(timeout, 15.0), cancel_event=self.cancel_event):
                raise DesktopCollectionContextError(f"注册采集会话前小程序尚未就绪：{last_error}")
            if probe():
                self.log("正常小店入口已重新注册采集逻辑层")
                return

        if not self.cdp_bridge.runtime_ready:
            if not self.cdp_bridge.wait_runtime(min(timeout, 15.0), cancel_event=self.cancel_event):
                raise DesktopCollectionContextError(f"注册采集会话前小程序尚未就绪：{last_error}")
        self.log("搜索窗口已打开，但采集逻辑层未绑定；正在注册原生采集会话")
        self._launch_applet(timeout)
        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline:
            if probe():
                self.log("原生采集逻辑层验证通过：页面、模块系统和 wx 均可用")
                return
            if self.cancel_event.wait(min(0.25, max(0.0, deadline - time.monotonic()))):
                self._check_cancelled()
        raise DesktopCollectionContextError(f"原生采集会话注册后仍不可用：{last_error}")

    def call_batch(
        self, requests: list[dict[str, Any]], timeout: float = 10.0,
        *, _allow_stopped: bool = False,
    ) -> list[Any]:
        if not _allow_stopped:
            self._check_cancelled()
        if not requests:
            return []
        self._check_launch_connection()
        if not self.active:
            if _allow_stopped:
                raise DesktopWechatError("电脑版微信连接已关闭")
            if self.weixin_session or self.wmpf_session or self._native_lost.is_set():
                raise DesktopHostLostError("小程序宿主连接已失效，需要完整恢复后再执行命令")
            self.start()
        call_id = self._next_call_id()
        encoded_requests = [
            json.dumps(
                {
                    "id": index,
                    "method": str(item.get("method") or ""),
                    "params": item.get("params") or {},
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            for index, item in enumerate(requests, start=1)
        ]
        req_args = encode_action_command_base64(
            "robin-local",
            call_id,
            encoded_requests,
            timeout_ms=int(timeout * 1000),
        )
        payload = json.dumps(
            {"call_id": call_id, "req_args": req_args}, separators=(",", ":")
        )
        event = threading.Event()
        holder: dict[str, Any] = {}
        dispatch_script = self.weixin_script
        with self._lock:
            self._pending[call_id] = (event, holder)
        try:
            try:
                with self._native_lock:
                    if not _allow_stopped:
                        self._check_cancelled()
                    self._check_launch_connection()
                    if not self.active:
                        raise DesktopHostLostError("发送前小程序宿主连接已失效")
                    dispatch_script.exports_sync.dispatch(payload, self.thread_id)
            except Exception as exc:
                raise DesktopWechatError(f"电脑版微信后台命令发送失败：{exc}") from exc
            if not event.wait(max(0.5, timeout)):
                raise DesktopWechatError(
                    f"电脑版微信后台命令超时：{requests[0].get('method') or 'unknown'}"
                )
            if holder.get("error"):
                if isinstance(holder['error'], DesktopHostLostError):
                    raise holder['error']
                raise DesktopWechatError(str(holder["error"]))
            if not _allow_stopped:
                self._check_cancelled()
            reply = holder.get("reply") or {}
            if int(reply.get("error_code") or 0):
                raise DesktopWechatError(
                    str(reply.get("error") or f"错误码 {reply.get('error_code')}")
                )
            return [
                _decode_xweb_response(str(row))
                for row in (reply.get("responses") or [])
            ]
        finally:
            with self._lock:
                self._pending.pop(call_id, None)
            with self._native_lock:
                self._release_native_dispatch(dispatch_script, call_id)

    def call(
        self, method: str, params: dict[str, Any] | None = None, timeout: float = 10.0
    ) -> Any:
        rows = self.call_batch(
            [{"method": method, "params": params or {}}], timeout=timeout
        )
        return rows[0] if rows else None

    def dispatch_async(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._check_cancelled()
        self._check_launch_connection()
        if not self.active:
            if self.weixin_session or self.wmpf_session or self._native_lost.is_set():
                raise DesktopHostLostError("小程序宿主连接已失效，需要完整恢复后再执行命令")
            self.start()
        call_id = self._next_call_id()
        request = json.dumps(
            {"id": 1, "method": method, "params": params or {}},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        req_args = encode_action_command_base64(
            "robin-local",
            call_id,
            [request],
            timeout_ms=int(max(0.5, timeout) * 1000),
        )
        payload = json.dumps(
            {"call_id": call_id, "req_args": req_args}, separators=(",", ":")
        )
        with self._lock:
            self._async_commands[call_id] = method
            while len(self._async_commands) > 8:
                self._async_commands.pop(next(iter(self._async_commands)))
        try:
            with self._native_lock:
                self._check_cancelled()
                self._check_launch_connection()
                if not self.active:
                    raise DesktopHostLostError("发送前小程序宿主连接已失效")
                self.weixin_script.exports_sync.dispatch(payload, self.thread_id)
        except Exception as exc:
            with self._lock:
                self._async_commands.pop(call_id, None)
            raise DesktopWechatError(f"电脑版微信后台命令发送失败：{exc}") from exc

    def evaluate(
        self, script: str, timeout: float = 10.0, *, prefer_cdp: bool = False
    ) -> Any:
        self._check_cancelled()
        # Long async page requests must not run inside XWeb's fixed 5000 ms
        # evaluation window. CDP awaits the promise with our explicit deadline.
        if prefer_cdp or getattr(self, "_xweb_replies_available", None) is False:
            return self._evaluate_with_cdp(script, timeout)
        try:
            result = self.call(
                "XWeb.EvaluateJavaScript", {"script": script}, timeout=timeout
            )
            if isinstance(result, dict) and "success" in result:
                if not result.get("success"):
                    raise DesktopWechatError(str(result.get("error") or "电脑版微信脚本执行失败"))
                result = result.get("data")
            result = _json_result(result)
            # Some XWeb versions return a successful envelope containing an
            # unsuccessful JS result, instead of raising at the command boundary.
            if (isinstance(result, dict) and result.get("ok") is False
                    and _is_transient_desktop_error(result.get("error"))):
                raise DesktopWechatError(str(result["error"]))
        except DesktopWechatError as exc:
            self._check_cancelled()
            if not _is_transient_desktop_error(exc):
                raise
            self.log("电脑版微信后台脚本超时或连接异常，正在改用页面调试通道")
            return self._evaluate_with_cdp(script, timeout, original_error=exc)
        return result

    def _evaluate_with_cdp(
        self,
        script: str,
        timeout: float,
        original_error: DesktopWechatError | None = None,
    ) -> Any:
        self._check_cancelled()
        if not self.cdp_bridge.wait_runtime(min(max(timeout, 1.0), 10.0)):
            if original_error is not None:
                raise original_error
            raise DesktopWechatError("电脑版微信小程序调试通道尚未就绪")
        try:
            return _json_result(self.cdp_bridge.evaluate(script, timeout))
        except (RuntimeError, TimeoutError) as cdp_exc:
            raise DesktopWechatError(
                f"电脑版微信页面脚本执行失败：{str(cdp_exc) or type(cdp_exc).__name__}"
            ) from cdp_exc

    def _launch_applet(self, timeout: float = 25.0, *, entry_url: str = SEARCH_PAGE_ROUTE) -> None:
        with self._native_lock:
            self._check_cancelled()
            if not self.active:
                self.start()
            self._launch_connection = self._connection_signature()
            try:
                self._check_launch_connection()
                self._launch_applet_on_connection(timeout, entry_url=entry_url)
            finally:
                self._launch_connection = None

    def _launch_applet_on_connection(self, timeout: float, *, entry_url: str) -> None:
        self._check_cancelled()
        # A WeChatAppEx process can have a visible window before WMPF has
        # created its XWeb/CDP runtime.  Sending CloseApplet or LaunchApplet
        # in that gap is unsafe on 4.1.13.x: flue.dll asserts and the host can
        # take Weixin down with it.  The normal-entry recovery path is the only
        # safe way to initialise such a host; never stack native commands on a
        # half-created session.
        if (not self.cdp_bridge.runtime_ready
                and not self._normal_entry_opened_search):
            raise DesktopHostLostError(
                "小程序宿主尚未完成初始化，已阻止原生关闭/启动；等待正常入口恢复"
            )
        generation = self.cdp_bridge.connection_generation
        self.log(f"小程序重开前连接状态：{self.cdp_bridge.diagnostic_state()}")
        self.log("正在完整关闭“小店与卡包”小程序（保留微信登录）")
        # Relaunching an already-open assistant applet can register its native
        # observer twice. Close the previous applet session before a fresh launch.
        try:
            self.call("XWeb.CloseApplet", {"app_id": SHOP_APP_ID}, timeout=5.0)
        except DesktopWechatError as exc:
            self._check_cancelled()
            self._check_launch_connection()
            # A fresh session can legitimately have nothing to close. A timeout
            # is NOT confirmation: launching on top of it risks double binding.
            if not any(word in str(exc) for word in ('session未绑定小程序', 'session不存在')):
                self._mark_native_lost(f"关闭命令未确认：{exc}")
                raise DesktopHostLostError(f"小程序关闭未确认，需要从正常卡片恢复连接：{exc}") from exc
            self.log("新会话尚未绑定小程序，直接建立新的搜索会话")
        self._check_launch_connection()
        # A ready flag from the previous page cannot validate this launch.
        self.cdp_bridge.prepare_relaunch()
        if self.wmpf_script is not None:
            try:
                with self._native_lock:
                    self._check_cancelled()
                    self.wmpf_script.exports_sync.reset_scene_patch()
            except Exception as exc:
                self._check_cancelled()
                self._mark_native_lost(f"启动状态重置失败：{exc}")
                raise DesktopHostLostError(f"小程序启动状态重置失败，取消本次启动：{exc}") from exc
        self._check_launch_connection()
        params = {
            "app_id": SHOP_APP_ID,
            "entry_url": entry_url,
            "version_type": 0,
        }
        self.log("正在重新打开小程序，等待新的页面连接")
        self.dispatch_async("XWeb.LaunchApplet", params, timeout=30.0)
        ready = self.cdp_bridge.wait_runtime_refresh(
            generation, max(5.0, timeout), cancel_event=self.cancel_event,
            abort_event=self._native_lost,
        )
        self._check_cancelled()
        self._check_launch_connection()
        if ready:
            return
        self._check_cancelled()
        raise DesktopWechatError(
            "电脑版微信“小店与卡包”已打开，但内部采集通道未就绪；"
            f"需要重建小程序连接（{self.cdp_bridge.diagnostic_state()}）"
        )

    def open_search_page(self, force: bool = False, timeout: float = 30.0) -> None:
        if force:
            self._search_page_ready = False
        elif self._search_page_ready:
            return
        try:
            self._open_search_page_once(force=force, timeout=timeout)
            self._search_page_ready = True
        except (DesktopWechatError, TimeoutError) as exc:
            self._check_cancelled()
            if not _is_transient_desktop_error(exc):
                raise
            self.log(f"搜索页连接未恢复，改为自动关闭小程序→打开普通小店首页→返回搜索：{exc}")
            if self.normal_shop_opener is not None:
                self._recover_normal_runtime(timeout)
                self._search_page_ready = True
                return
            self._restore_shop_home(timeout=timeout)
            # The home page is now verified in this new session. Navigate within
            # it; another LaunchApplet here would discard the recovered session.
            self._navigate_search_page(timeout)
            self._wait_search_handover(timeout)
            self._search_page_ready = True

    def _wait_search_handover(self, timeout: float) -> None:
        # wxShopHomeLite -> globalSearch can replace the debug socket AFTER the
        # first successful route read. Let that handover settle, without opening
        # another applet while the replacement socket is still arriving.
        if self._normal_entry_search_reused:
            self.log("已复用内置入口显示的搜索页，跳过不可靠的路由稳定探测")
            self._normal_entry_search_reused = False
            return
        deadline = time.monotonic() + timeout
        stable = 0
        previous_generation = -1
        while time.monotonic() < deadline:
            self._check_cancelled()
            generation = self.cdp_bridge.connection_generation
            state: Any = None
            if self.cdp_bridge.runtime_ready:
                try:
                    state = _json_result(self.evaluate(SEARCH_STATE_SCRIPT, timeout=5.0))
                except (DesktopWechatError, TimeoutError) as exc:
                    self._check_cancelled()
                    if not _is_transient_desktop_error(exc):
                        raise
            if (isinstance(state, dict) and "globalSearch" in str(state.get("route") or "")
                    and self.cdp_bridge.runtime_ready
                    and generation == self.cdp_bridge.connection_generation):
                stable = stable + 1 if generation == previous_generation else 1
            else:
                stable = 0
            previous_generation = generation
            if stable >= 3:
                self.log("小店首页恢复完成，搜索页面及连接已稳定，将继续当前关键词")
                return
            if self.cancel_event.wait(0.25):
                self._check_cancelled()
        raise DesktopWechatError("小店首页已打开，但返回搜索页后的连接稳定检查超时")

    def _restore_shop_home(self, timeout: float = 30.0) -> None:
        self._check_cancelled()
        if self.normal_shop_opener is not None:
            self._restore_normal_shop(timeout)
            return
        if not self._recovery_shops:
            raise DesktopWechatError(
                "小店首页恢复失败：还没有可用的店铺入口。请先正常打开一个小店并继续采集；"
                "新版读取到商品后会自动记住店铺，后续可自动恢复。当前词断点保留"
            )
        last_error: Exception | None = None
        for index, shop in enumerate(tuple(self._recovery_shops), start=1):
            self._check_cancelled()
            self.log(f"正在自动打开已记录的小店首页（{index}/{len(self._recovery_shops)}）")
            try:
                route = (f"{SHOP_HOME_ROUTE}?entrance_id=order_center_search"
                         f"&flags=7&onLive=0&realappid={shop['appid']}")
                self._launch_applet(timeout, entry_url=route)
                deadline = time.monotonic() + timeout
                state: Any = None
                while time.monotonic() < deadline:
                    self._check_cancelled()
                    try:
                        state = _json_result(self.evaluate(SHOP_HOME_STATE_SCRIPT, timeout=5.0))
                        if (isinstance(state, dict) and state.get("ready") is True
                                and state.get("appid") == shop["appid"]
                                and str(state.get("route") or "").lstrip("/") in SHOP_HOME_ROUTES):
                            self.log("普通小店首页已加载并核对店铺身份，正在返回关键词搜索页")
                            return
                    except (DesktopWechatError, TimeoutError) as exc:
                        self._check_cancelled()
                        if not _is_transient_desktop_error(exc):
                            raise
                        last_error = exc
                    if self.cancel_event.wait(0.25):
                        self._check_cancelled()
                summary = ({key: state.get(key) for key in ("route", "appid", "ready")}
                           if isinstance(state, dict) else {"state": "unavailable"})
                raise DesktopWechatError(
                    "小店首页加载超时，尚未核对到目标店铺内容："
                    + json.dumps(summary, ensure_ascii=False)
                )
            except (DesktopWechatError, TimeoutError) as exc:
                self._check_cancelled()
                if not _is_transient_desktop_error(exc):
                    raise
                last_error = exc
                self.log(f"本次小店首页恢复未完成：{exc}")
        raise DesktopWechatError(f"小店首页恢复失败，已保留当前词断点：{last_error}") from last_error

    def _restore_normal_shop(self, timeout: float) -> None:
        with self._native_lock:
            # A socket detach can leave the real 小店 window and search page
            # alive.  Rebind that host first; closing it here is what made
            # 4.1.13.65 lose the only working launch path and enter a cooldown.
            weixin_pid, _ = _visible_weixin_identity()
            existing_root = _wmpf_root_pid(weixin_pid) if weixin_pid else 0
            if existing_root and _has_visible_shop_window(existing_root):
                self.stop()
                self._check_cancelled()
                self._normal_bootstrap_in_progress = True
                try:
                    self.start()
                finally:
                    self._normal_bootstrap_in_progress = False
                self.log("检测到现有小店窗口，未关闭小程序，已重新绑定采集连接")
                self._normal_entry_opened_search = True
                return
            self.stop()
            self._check_cancelled()
            self._normal_bootstrap_in_progress = True
            try:
                # Match the user's working recovery sequence exactly: release
                # the failed session, close/open through the normal entry, wait for
                # its real content, THEN connect like a fresh Continue action.
                self._bootstrap_normal_shop_compat(force_relaunch=True)
                self.start()
            finally:
                self._normal_bootstrap_in_progress = False
            # The normal entry already rendered the real search page before
            # the fresh Frida/CDP connection was attached.  Do not click the
            # entry a second time: on WeChat 4.1.13.65 that replaces the WMPF
            # root process and invalidates the connection just established.
            self.log("小店已完成正常初始化，复用当前搜索页和新建采集连接")
            self._normal_entry_opened_search = True

    def _try_rebind_existing_shop(self, timeout: float) -> bool:
        """Rebind a still-running WMPF host before tearing down the UI.

        A transient CDP/Frida detach does not necessarily mean that the visible
        小店 window died.  Closing and reopening in that state races WeChat's
        host replacement and used to send the collector into a long recovery
        cooldown.  If the current root process is still present, attach to it
        as a fresh Continue action and reuse its existing globalSearch page.
        """
        self._check_cancelled()
        weixin_pid, _ = _visible_weixin_identity()
        if not weixin_pid or not _wmpf_root_pid(weixin_pid):
            return False
        try:
            with self._native_lock:
                self.stop()
                self._check_cancelled()
                self._normal_bootstrap_in_progress = True
                try:
                    self.start()
                finally:
                    self._normal_bootstrap_in_progress = False
            if not self.cdp_bridge.wait_runtime(min(timeout, 12.0), cancel_event=self.cancel_event):
                return False
            state = _json_result(self.evaluate(SEARCH_STATE_SCRIPT, timeout=5.0))
            if not isinstance(state, dict) or "globalSearch" not in str(state.get("route") or ""):
                return False
            self._remember_initialized_host()
            self._resume_requires_reopen = False
            self._normal_entry_opened_search = False
            self._normal_entry_search_reused = True
            self.log("检测到小店宿主仍在运行，已快速重绑现有搜索页，继续当前关键词")
            return True
        except (DesktopWechatError, TimeoutError):
            self.stop()
            return False

    def _recover_normal_runtime(self, timeout: float) -> None:
        # Connection recovery is not a product-search attempt. Retain the word
        # and retry with capped, cancellable backoff instead of exhausting its
        # three attempts on empty/dead hosts. No native calls during cooldown.
        attempt = 0
        while True:
            self._check_cancelled()
            try:
                self._restore_normal_shop(timeout)
                # _restore_normal_shop ends with the normal opener already
                # showing the real search page.  Do not issue a second XWeb
                # navigation on 4.1.13.65: that command commonly returns an
                # empty result and invalidates the freshly attached socket.
                self._normal_entry_opened_search = True
                self._resume_requires_reopen = False
                self._open_search_page_once(timeout=timeout)
                self._wait_search_handover(timeout)
                self.on_recovery_status("自动恢复完成，继续当前关键词")
                return
            except (DesktopWechatError, TimeoutError) as exc:
                self._check_cancelled()
                if not _is_transient_desktop_error(exc):
                    raise
                # A detached native socket can often be rebound in place.  A
                # live host with an unbound session is different: rebind would
                # only repeat stop/start on the same page, so let the next
                # normal-entry recovery rebuild it once instead.
                if not isinstance(exc, DesktopCollectionContextError):
                    try:
                        if self._try_rebind_existing_shop(timeout):
                            self.on_recovery_status("已重绑现有小店，继续当前关键词")
                            return
                    except DesktopWechatError:
                        pass
                attempt += 1
                if attempt >= MAX_RUNTIME_RECOVERY_ATTEMPTS:
                    message = (
                        f"小程序自动恢复已达到安全上限（{MAX_RUNTIME_RECOVERY_ATTEMPTS} 次）：{exc}；"
                        "已保留当前关键词和断点，避免继续反复重启微信"
                    )
                    self.on_recovery_status(message)
                    raise DesktopRecoveryExhausted(message) from exc
                # The next UI recovery closes the window normally. Do not send
                # another native CloseApplet through the suspect connection.
                if _invalidates_native_connection(exc):
                    self._mark_native_lost(str(exc))
                else:
                    self.log(f"采集会话尚未绑定，保留微信宿主并执行一次正常入口重建：{exc}")
                self.stop()
                delay = HOST_RECOVERY_BACKOFF_SECONDS[min(attempt - 1, len(HOST_RECOVERY_BACKOFF_SECONDS) - 1)]
                if isinstance(exc, DesktopRecoveryEntryUnavailable):
                    self._wait_recovery_entry(exc, attempt)
                    continue
                else:
                    self.log(
                        f"小程序连接恢复第 {attempt} 次未完成：{exc}；"
                        f"进入自动恢复等待，{delay:g} 秒后重新打开小店。"
                        "当前关键词和已采商品保留，不计关键词失败次数；可随时点停止"
                    )
                    self.on_recovery_status(f"小程序恢复中，{delay:g} 秒后重试；当前词保留")
                if self.cancel_event.wait(delay):
                    self._check_cancelled()

    def _open_search_page_once(self, force: bool = False, timeout: float = 30.0) -> None:
        self._check_cancelled()
        self._ensure_collection_runtime(timeout)
        if not force and not self._resume_requires_reopen and self._normal_entry_opened_search:
            # BuiltinShopOpener has already shown the real search page.  Give
            # its debug socket time to arrive and reuse that page; launching a
            # second applet here races the first page and leaves a blank UI with
            # no searchable runtime (the failure seen on 4.1.13.65).
            self.log("内置入口已显示搜索页，等待已有页面连接，不重复打开小程序")
            if self.cdp_bridge.wait_runtime(min(timeout, 15.0), cancel_event=self.cancel_event):
                try:
                    # The normal built-in opener has already navigated the
                    # visible page to globalSearch.  On WeChat 4.1.13.65 the
                    # follow-up XWeb navigate command commonly returns an
                    # empty result and races the newly attached CDP socket;
                    # verify the current route first and reuse it when ready.
                    state = _json_result(self.evaluate(SEARCH_STATE_SCRIPT, timeout=5.0))
                    if isinstance(state, dict) and "globalSearch" in str(state.get("route") or ""):
                        self._remember_initialized_host()
                        self._resume_requires_reopen = False
                        self._normal_entry_search_reused = True
                        self._normal_entry_opened_search = False
                        return
                    self._navigate_search_page(timeout)
                    self._normal_entry_opened_search = False
                    return
                except (DesktopWechatError, TimeoutError) as exc:
                    self._check_cancelled()
                    if not _is_transient_desktop_error(exc):
                        raise
                    # A rendered window is not evidence that the collector
                    # can read state. Recover instead of reporting false success.
                    raise
            self._normal_entry_opened_search = False
        # If the visible page was opened before the debug hooks were attached,
        # reopen it through the already-mounted normal entry so the new page
        # can create a CDP client.  Otherwise the UI is visible but searches
        # fail with “session未绑定或CDP客户端不可用”.
        if (not force and self.normal_shop_opener is not None
                and not self._resume_requires_reopen
                and not self.cdp_bridge.runtime_ready):
            # Do not close/reopen a still-visible 小店 just because the debug
            # socket was late.  Reopening here destroys the only usable host on
            # WeChat 4.1.13.65 and sends recovery back to the card-entry loop.
            weixin_pid, _ = _visible_weixin_identity()
            existing_root = _wmpf_root_pid(weixin_pid) if weixin_pid else 0
            if existing_root and _has_visible_shop_window(existing_root):
                self.log("当前小店窗口仍在但调试会话未注册，立即通过正常入口重新注册")
                self._normal_bootstrap_in_progress = True
                try:
                    self._bootstrap_normal_shop_compat(force_relaunch=True)
                finally:
                    self._normal_bootstrap_in_progress = False
                if not self.cdp_bridge.wait_runtime(
                        min(timeout, 15.0), cancel_event=self.cancel_event):
                    raise DesktopWechatError(
                        "小店窗口仍在但 CDP 客户端尚未绑定；保留现有页面重试"
                    )
            else:
                self.log("当前搜索页未绑定采集通道，保持微信登录并通过已挂载入口重新打开")
                self._normal_bootstrap_in_progress = True
                try:
                    self._bootstrap_normal_shop()
                finally:
                    self._normal_bootstrap_in_progress = False
            self._normal_entry_opened_search = False
            if not self.cdp_bridge.wait_runtime(
                    min(timeout, 15.0), cancel_event=self.cancel_event):
                raise DesktopWechatError(
                    "内置入口已显示搜索页，但 CDP 客户端仍未绑定；将自动重试当前关键词"
                )
            state = _json_result(self.evaluate(SEARCH_STATE_SCRIPT, timeout=5.0))
            if isinstance(state, dict) and "globalSearch" in str(state.get("route") or ""):
                self._remember_initialized_host()
                self._resume_requires_reopen = False
                self._normal_entry_search_reused = True
                return
            self._navigate_search_page(timeout)
            return
        # A normal card click can already have produced a fully usable search
        # page before the collector is started (or before a paused run is
        # continued).  On WeChat 4.1.13.65, issuing OPEN_SEARCH_SCRIPT again
        # in that state is destructive: XWeb may acknowledge it with None and
        # leave the page usable but make the collector report "搜索没有启动".
        # Verify the live route first and reuse it, even for force=True.
        if self.cdp_bridge.runtime_ready:
            try:
                current_state = _json_result(
                    self.evaluate(SEARCH_STATE_SCRIPT, timeout=5.0)
                )
            except (DesktopWechatError, TimeoutError):
                current_state = None
            if (isinstance(current_state, dict)
                    and "globalSearch" in str(current_state.get("route") or "")):
                self._remember_initialized_host()
                self._resume_requires_reopen = False
                self._normal_entry_search_reused = True
                self._normal_entry_opened_search = False
                self.log("当前小店已是可用搜索页，复用现有页面，不重复导航")
                return

        if force or self._resume_requires_reopen or not self.cdp_bridge.runtime_ready:
            self._launch_applet(timeout)
        self._navigate_search_page(timeout)

    def _navigate_search_page(self, timeout: float) -> None:
        self._check_cancelled()
        try:
            result = self.evaluate(OPEN_SEARCH_SCRIPT, timeout=10.0)
            result = _json_result(result)
            # During the .65/WMPF handover XWeb can acknowledge the command
            # without returning the async JavaScript value.  The page is
            # often still navigating in this state. Give the native page a
            # short retry window first; only then use CDP if a real context
            # has appeared. CDP cannot evaluate a page-less socket.
            if result in (None, ""):
                self.log("搜索页切换返回空结果，等待页面导航完成后重试")
                for _attempt in range(3):
                    if self.cancel_event.wait(0.6):
                        self._check_cancelled()
                    result = _json_result(self.evaluate(OPEN_SEARCH_SCRIPT, timeout=10.0))
                    if result not in (None, ""):
                        break
                if result in (None, ""):
                    self.log("搜索页仍未返回结果，正在通过页面调试通道重试")
                    result = _json_result(
                        self.evaluate(OPEN_SEARCH_SCRIPT, timeout=10.0, prefer_cdp=True)
                    )
        except (DesktopWechatError, RuntimeError) as exc:
            raise DesktopWechatError(f"无法进入电脑版微信商品搜索页：{exc}") from exc
        if not isinstance(result, dict) or not result.get("ok"):
            detail = (
                result.get("error")
                if isinstance(result, dict) and result.get("error")
                else "搜索页脚本未返回，页面仍在初始化"
            )
            raise DesktopWechatError(
                "无法进入电脑版微信商品搜索页：" + str(detail)
            )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = _json_result(self.evaluate(SEARCH_STATE_SCRIPT, 5.0))
            if isinstance(state, dict) and "globalSearch" in str(state.get("route") or ""):
                self._remember_initialized_host()
                self._resume_requires_reopen = False
                return
            if self.cancel_event.wait(0.25):
                self._check_cancelled()
        raise DesktopWechatError("电脑版微信商品搜索页加载超时")

    def _wait_search_stable(
        self,
        keyword: str,
        *,
        timeout: float,
        stage: str,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        state: dict[str, Any] = {}
        stable = 0
        error_streak = 0
        last_error = ""
        while time.monotonic() < deadline:
            if self.cancel_event.is_set():
                raise DesktopWechatError("电脑版微信采集已停止")
            raw_state = _json_result(self.evaluate(SEARCH_STATE_SCRIPT, 5.0))
            if raw_state is None:
                try:
                    raw_state = _json_result(
                        self._evaluate_page_async(SEARCH_STATE_SCRIPT, timeout=5.0)
                    )
                except (DesktopWechatError, RuntimeError, TimeoutError):
                    raw_state = None
            if isinstance(raw_state, dict):
                state = raw_state
                if state.get("error"):
                    error_streak += 1
                    last_error = str(state["error"])
                    stable = 0
                    # One stale state from the previous page is common during
                    # handover.  Structural module errors remain fatal once
                    # repeated, while ordinary initialization/transport
                    # errors get the rest of the bounded window to recover.
                    if (_is_structural_collection_error(last_error) and error_streak >= 2) or (
                        not _is_structural_collection_error(last_error) and error_streak >= 6
                    ):
                        raise DesktopCollectionContextError(
                            f"{stage}状态读取失败：{last_error}"
                        )
                    if self.cancel_event.wait(0.35):
                        raise DesktopWechatError("电脑版微信采集已停止")
                    continue
                error_streak = 0
            if (
                keyword == str(state.get("keyword") or "").strip()
                and not state.get("loading")
            ):
                stable += 1
                if stable >= 3:
                    return state
            else:
                stable = 0
            if self.cancel_event.wait(0.35):
                raise DesktopWechatError("电脑版微信采集已停止")
        detail = f"；最后错误：{last_error}" if last_error else ""
        raise DesktopCollectionContextError(f"关键词“{keyword}”{stage}结果未稳定{detail}")

    def _evaluate_page_async(self, script: str, timeout: float) -> Any:
        """Return immediately from each JS evaluation; poll the async result.

        Neither XWeb's fixed 5 s window nor CDP's awaitPromise is reliable for
        slow CGI calls on this runtime. Only the page job itself awaits the CGI.
        """
        key = json.dumps("__collector_page_" + uuid.uuid4().hex)
        expression = script.strip().rstrip(";")
        start_script = (
            "(()=>{const key=" + key + ";const box={done:false};"
            "globalThis[key]=box;box.timer=setTimeout(()=>{delete globalThis[key];},60000);"
            "Promise.resolve().then(()=> (" + expression + ")).then(value=>{"
            "box.value=value;box.done=true;},error=>{box.value={ok:false,error:String(error)};"
            "box.done=true;});return JSON.stringify({jobStarted:true});})()"
        )
        poll_script = (
            "(()=>{const box=globalThis[" + key + "];"
            "if(!box)return JSON.stringify({missing:true});"
            "if(!box.done)return JSON.stringify({done:false});"
            "const result=JSON.stringify({done:true,value:box.value});"
            "clearTimeout(box.timer);delete globalThis[" + key + "];return result;})()"
        )
        cleanup_script = (
            "(()=>{const key=" + key + ";const box=globalThis[key];"
            "if(box)clearTimeout(box.timer);delete globalThis[key];return true;})()"
        )
        deadline = time.monotonic() + timeout
        cleaned = False
        try:
            started = _json_result(self.evaluate(start_script, timeout=5.0))
            if not isinstance(started, dict) or not started.get("jobStarted"):
                raise DesktopWechatError(f"分页异步任务未启动：{started}")
            while time.monotonic() < deadline:
                self._check_cancelled()
                state = _json_result(self.evaluate(
                    poll_script, timeout=min(5.0, max(0.1, deadline-time.monotonic()))
                ))
                if isinstance(state, dict) and state.get("done"):
                    cleaned = True
                    return _json_result(state.get("value"))
                if isinstance(state, dict) and state.get("missing"):
                    raise DesktopWechatError("分页任务所在运行环境已失效，需要重建连接")
                if self.cancel_event.wait(0.25):
                    self._check_cancelled()
            raise DesktopWechatError(f"分页请求等待超时（{timeout:g} 秒）")
        finally:
            if not cleaned and not self.cancel_event.is_set():
                try:
                    self.evaluate(cleanup_script, timeout=2.0)
                except Exception:
                    pass  # The job also expires itself after 60 seconds.

    def _read_search_page(self, script: str, page_number: int) -> dict[str, Any]:
        """Retry one immutable request; commit its products/cursor only on success."""
        for attempt in range(1, PAGE_READ_MAX_ATTEMPTS + 1):
            if self.cancel_event.is_set():
                raise DesktopWechatError("电脑版微信采集已停止")
            try:
                page = _json_result(self._evaluate_page_async(
                    script, timeout=PAGE_READ_TIMEOUT_SECONDS
                ))
                if not isinstance(page, dict) or not page.get("ok"):
                    detail = str(page.get("error") if isinstance(page, dict) else page)
                    error_type = (DesktopCollectionContextError
                                  if detail == "search-module-unavailable" or "require is not defined" in detail
                                  else DesktopWechatError)
                    raise error_type(detail)
                if self.cancel_event.is_set():
                    raise DesktopWechatError("电脑版微信采集已停止")
                return page
            except (DesktopWechatError, RuntimeError, TimeoutError) as exc:
                if self.cancel_event.is_set():
                    raise DesktopWechatError("电脑版微信采集已停止") from exc
                if attempt >= PAGE_READ_MAX_ATTEMPTS or not _is_transient_desktop_error(exc):
                    error_type = (DesktopCollectionContextError
                                  if isinstance(exc, DesktopCollectionContextError) else DesktopWechatError)
                    raise error_type(f"第 {page_number} 页读取失败：{exc}") from exc
                delay = PAGE_READ_RETRY_SECONDS * attempt
                self.log(
                    f"第 {page_number} 页读取超时或连接异常；已保存前面的商品，"
                    f"{delay:g} 秒后只重试本页（{attempt}/{PAGE_READ_MAX_ATTEMPTS - 1}）：{exc}"
                )
                if self.cancel_event.wait(delay):
                    raise DesktopWechatError("电脑版微信采集已停止") from exc
        raise AssertionError("unreachable page retry state")

    def collect_keyword(
        self,
        keyword: str,
        min_price: int | None,
        max_price: int | None,
        on_page: ProductsCallback,
    ) -> str:
        if self.cancel_event.is_set():
            raise DesktopWechatError("电脑版微信采集已停止")
        self.open_search_page(timeout=25.0)
        request = json.dumps(
            {"keyword": keyword, "minPrice": min_price, "maxPrice": max_price},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        script = SEARCH_START_SCRIPT.replace("__REQUEST__", request)
        result = _json_result(self.evaluate(script, timeout=5.0))
        if result is None:
            # WeChat 4.1.13.65 can expose the search page visually while its
            # JS bridge returns an empty value.  Use the same scoped UIA
            # search box as the built-in opener, limited to the 小店 window.
            enter_keyword = getattr(self.normal_shop_opener, "enter_search_keyword", None)
            if callable(enter_keyword):
                try:
                    weixin_pid, _ = _visible_weixin_identity()
                    enter_keyword(
                        weixin_pid=weixin_pid,
                        get_host_pid=lambda: _wmpf_root_pid(weixin_pid),
                        cancel_event=self.cancel_event,
                        keyword=keyword,
                        log=self.log,
                    )
                    result = {"ok": True, "method": "uia"}
                    self.log("搜索脚本无回包，已通过小店可见搜索框输入关键词")
                except Exception as exc:
                    self.log(f"小店可见搜索框后备入口失败：{exc}")
        if result is None:
            try:
                result = _json_result(
                    self._evaluate_page_async(script, timeout=8.0)
                )
            except (DesktopWechatError, RuntimeError, TimeoutError):
                result = None
        if not isinstance(result, dict) or not result.get("ok"):
            error_type = (DesktopCollectionContextError
                          if isinstance(result, dict) and result.get("error") == "not-on-search"
                          else DesktopWechatError)
            raise error_type(
                "搜索没有启动："
                + str(result.get("error") if isinstance(result, dict) else result)
            )
        self._wait_search_stable(keyword, timeout=15.0, stage="搜索")

        filter_script = SEARCH_FILTER_SCRIPT.replace("__REQUEST__", request)
        filter_deadline = time.monotonic() + 6.0
        filter_result: Any = None
        while time.monotonic() < filter_deadline:
            filter_result = _json_result(self.evaluate(filter_script, timeout=5.0))
            if filter_result is None:
                try:
                    filter_result = _json_result(
                        self._evaluate_page_async(filter_script, timeout=8.0)
                    )
                except (DesktopWechatError, RuntimeError, TimeoutError):
                    filter_result = None
            if isinstance(filter_result, dict) and filter_result.get("ok"):
                break
            error = (
                str(filter_result.get("error") or "")
                if isinstance(filter_result, dict)
                else str(filter_result)
            )
            if error != "filter-not-ready":
                break
            if self.cancel_event.wait(0.3):
                self._check_cancelled()
        if not isinstance(filter_result, dict) or not filter_result.get("ok"):
            error_type = (DesktopCollectionContextError
                          if isinstance(filter_result, dict) and filter_result.get("error") == "filter-not-ready"
                          else DesktopWechatError)
            raise error_type(
                "筛选没有完成："
                + str(
                    filter_result.get("error")
                    if isinstance(filter_result, dict)
                    else filter_result
                )
            )
        if filter_result.get("skipped"):
            self.log(f"关键词“{keyword}”筛选面板没有“好评多”，已直接跳过")
            return "无“好评多”，已跳过"

        if self.cancel_event.wait(0.45):
            self._check_cancelled()
        state = self._wait_search_stable(keyword, timeout=15.0, stage="筛选")

        offset: int | None = 0
        search_info: Any = state.get("searchCommInfo") or {}
        seen_cursors: set[str] = set()
        total = 0
        page_number = 0
        while True:
            self._check_cancelled()
            page_number += 1
            encoded_offset = "null" if offset is None else str(max(0, int(offset)))
            encoded_info = json.dumps(
                search_info, ensure_ascii=False, separators=(",", ":")
            )
            page_script = (
                SEARCH_PAGE_SCRIPT.replace("__OFFSET__", encoded_offset)
                .replace("__SEARCH_INFO__", encoded_info)
            )
            page = self._read_search_page(page_script, page_number)
            cards = desktop_cards_from_rows(page.get("products") or [], keyword)
            self._check_cancelled()
            self._remember_recovery_shops(page.get("products"))
            if cards:
                on_page(cards)
                total += len(cards)
            if not page.get("hasMore"):
                break
            next_offset = int(page.get("nextOffset") or 0)
            next_info = page.get("searchCommInfo") or {}
            cursor = json.dumps(
                [next_offset, next_info], ensure_ascii=False, sort_keys=True
            )
            if cursor in seen_cursors or next_offset <= int(offset or 0):
                raise DesktopWechatError("电脑版微信搜索分页游标没有继续前进")
            seen_cursors.add(cursor)
            offset = next_offset
            search_info = next_info
        self.log(
            f"关键词“{keyword}”已采集完成，微信返回末页（共 {page_number} 页），"
            f"共读取 {total} 个普通商品"
        )
        return "已完成"

    def recover_search_runtime(self) -> None:
        # Reopening creates a new search session. The caller must replay the
        # current keyword, never carry the old session's cursor across reconnects.
        self.log("当前词连接恢复：保留已保存商品，完整关闭并重新打开小程序；当前词从第一页重采并去重")
        self._check_cancelled()
        if self.normal_shop_opener is not None:
            self._recover_normal_runtime(timeout=30.0)
            return
        self.start()
        if self.cancel_event.is_set():
            raise DesktopWechatError("电脑版微信采集已停止")
        self.open_search_page(force=True, timeout=30.0)

    def restart_search_runtime(self) -> None:
        self.log("定时重进：当前词已采完，正在完整重开“小店与卡包”小程序")
        self.open_search_page(force=True, timeout=30.0)

    def _cancel_pending(self) -> None:
        with self._lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for event, holder in pending:
            holder["error"] = "电脑版微信采集已停止"
            event.set()
        self.cdp_bridge.cancel_pending()

    def pause(self) -> None:
        """Cancel this run but retain the healthy native connection for Start."""
        self.cancel_event.set()
        self._search_page_ready = False
        self._resume_requires_reopen = True
        self._cancel_pending()

    def stop(self) -> None:
        """Final resource release, only after collection/startup have drained."""
        self._cancel_pending()
        with self._native_lock:
            self._release_connection()

    def _release_connection(self) -> None:
        # During a blank/unready host, CloseApplet is precisely the command
        # that can trigger flue.dll's internal CHECK.  Detaching our hooks is
        # sufficient for Stop and lets WeChat close the window itself.
        if self.active and self.thread_id and self.cdp_bridge.runtime_ready:
            try:
                # Finish the assistant's applet session while its reply hook is
                # still present. Cancellation remains set for collection calls.
                self.call_batch(
                    [{"method": "XWeb.CloseApplet", "params": {"app_id": SHOP_APP_ID}}],
                    timeout=2.0, _allow_stopped=True,
                )
            except Exception:
                pass
        self.cdp_bridge.stop()
        scripts = (self.weixin_script, self.wmpf_script)
        sessions = (self.weixin_session, self.wmpf_session)
        # Clear identities before deliberate detach so its late callback cannot
        # invalidate this or a subsequent connection.
        with self._lock:
            self.weixin_script = None
            self.wmpf_script = None
            self.weixin_session = None
            self.wmpf_session = None
            self._async_commands.clear()
        for script in scripts:
            try:
                if script is not None:
                    script.unload()
            except Exception:
                pass
        for session in sessions:
            try:
                if session is not None:
                    session.detach()
            except Exception:
                pass
        self.weixin_script = None
        self.wmpf_script = None
        self.weixin_session = None
        self.wmpf_session = None
        self.device = None
        self.weixin_pid = 0
        self.wmpf_pid = 0
        self.thread_id = 0
        self.profile = None
        self._xweb_replies_available = None
        self._native_fatal_error = None
        self._hooks_attached_at = 0.0
        self._host_identity = None
        self._normal_entry_opened_search = False


def desktop_cards_from_rows(rows: Iterable[Any], keyword: str) -> list[ProductCard]:
    cards: dict[str, ProductCard] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        product_id = str(row.get("productId") or "").strip()
        title = str(row.get("title") or "").strip()
        image = str(row.get("mainImage") or "").strip()
        if not re.fullmatch(r"\d{8,20}", product_id) or not title:
            continue
        try:
            price = int(row.get("sellingPrice") or 0)
        except (TypeError, ValueError):
            price = 0
        sales_text = str(row.get("salesText") or "").strip()
        sales_known = bool(row.get("salesKnown")) or bool(re.search(r"\d", sales_text))
        try:
            sales = int(row.get("sales") or 0) if sales_known else None
        except (TypeError, ValueError):
            sales = None
        cards[product_id] = ProductCard(
            product_id=product_id,
            keyword=keyword,
            title=title,
            main_image_url=image,
            price_fen=price if price > 0 else None,
            sales=sales,
            sales_text=sales_text,
            sales_exact=True,
            shop_name=str(row.get("shop") or "").strip(),
            source="电脑版微信关键词搜索",
        )
    return list(cards.values())


class DesktopKeywordListener:
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
        restart_keyword_interval: int = 30,
        backend: DesktopWechatBackend | None = None,
        retry_seconds: float = KEYWORD_RETRY_SECONDS,
    ) -> None:
        self.keyword = keyword.strip()
        self.on_products = on_products
        self.log = log or (lambda _message: None)
        self.on_ready = on_ready or (lambda: None)
        self.on_keyword = on_keyword or (lambda _keyword: None)
        self.on_progress = on_progress or (
            lambda _keyword, _index, _total, _stage: None
        )
        self.on_complete = on_complete or (lambda: None)
        self.on_error = on_error or (lambda _message: None)
        self.on_keyword_finished = on_keyword_finished or (
            lambda _keyword, _index, _total, _outcome: None
        )
        self.keywords: list[str] = []
        for item in keywords or ():
            normalized = str(item).strip()
            if normalized and normalized not in self.keywords:
                self.keywords.append(normalized)
        if not 0 <= start_index <= len(self.keywords):
            raise ValueError("关键词起始位置超出列表范围")
        if min_price is not None and min_price < 0:
            raise ValueError("最低价不能小于 0")
        if max_price is not None and max_price < 0:
            raise ValueError("最高价不能小于 0")
        if min_price is not None and max_price is not None and min_price > max_price:
            raise ValueError("最低价不能大于最高价")
        if restart_keyword_interval < 0:
            raise ValueError("微信重进间隔必须是非负整数")
        self.start_index = start_index
        self.min_price = min_price
        self.max_price = max_price
        self.restart_keyword_interval = restart_keyword_interval
        self.retry_seconds = max(0.0, retry_seconds)
        self.backend = backend or DesktopWechatBackend(self.log)
        self._stop = threading.Event()
        if isinstance(self.backend, DesktopWechatBackend):
            self.backend.cancel_event = self._stop
        self._worker: threading.Thread | None = None

    def start(self) -> None:
        if self._worker and self._worker.is_alive():
            raise RuntimeError("电脑版微信关键词采集已经在运行")
        self._stop.clear()
        if isinstance(self.backend, DesktopWechatBackend):
            self.backend.prepare_recovery_entry()
            if self._stop.is_set():
                return
        for attempt in range(1, PAGE_REENTRY_MAX_ATTEMPTS + 1):
            try:
                self.backend.start()
                if self._stop.is_set():
                    return
                if attempt > 1:
                    self.backend.open_search_page(force=True, timeout=30.0)
                else:
                    self.backend.open_search_page(timeout=30.0)
                break
            except Exception as exc:
                if self._stop.is_set():
                    return
                if (isinstance(self.backend, DesktopWechatBackend)
                        and self.backend.normal_shop_opener is not None
                        and _is_transient_desktop_error(exc)):
                    self.log(f"启动时小程序连接失效，改为完整自动恢复，不消耗关键词次数：{exc}")
                    self.backend.recover_search_runtime()
                    break
                if "尚未初始化" in str(exc):
                    raise
                if attempt >= PAGE_REENTRY_MAX_ATTEMPTS or not _is_transient_desktop_error(exc):
                    raise
                self.log(
                    f"电脑版微信启动连接暂未就绪，正在复用连接重试搜索页"
                    f"（{attempt}/{PAGE_REENTRY_MAX_ATTEMPTS - 1}）：{exc}"
                )
                if self._stop.wait(PAGE_REENTRY_RETRY_SECONDS):
                    return
        if self._stop.is_set():
            return
        self._worker = threading.Thread(
            target=self._automation_loop, name="desktop-keyword", daemon=True
        )
        self._worker.start()

    def stop(self) -> None:
        self._stop.set()
        # Stop is a run-level cancellation, not native connection teardown.
        self.backend.pause()
        if (self._worker and self._worker.is_alive()
                and self._worker is not threading.current_thread()):
            self._worker.join()
        if not self._worker or not self._worker.is_alive():
            self._worker = None

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

    def _reenter_search_page(self) -> None:
        last_error: Exception | None = None
        for attempt in range(1, PAGE_REENTRY_MAX_ATTEMPTS + 1):
            try:
                self.backend.restart_search_runtime()
                return
            except Exception as exc:
                if self._stop.is_set():
                    raise DesktopWechatError("电脑版微信小店重进已停止") from exc
                last_error = exc
                if attempt >= PAGE_REENTRY_MAX_ATTEMPTS:
                    break
                self.log(
                    f"电脑版微信小店重进第 {attempt}/{PAGE_REENTRY_MAX_ATTEMPTS} "
                    f"次失败：{exc}；{PAGE_REENTRY_RETRY_SECONDS:g} 秒后重试"
                )
                if self._stop.wait(PAGE_REENTRY_RETRY_SECONDS):
                    raise DesktopWechatError("电脑版微信小店重进已停止") from exc
        raise DesktopWechatError(
            f"电脑版微信小店连续重进 {PAGE_REENTRY_MAX_ATTEMPTS} 次失败，"
            f"已暂停：{last_error or '未知错误'}"
        ) from last_error

    def _automation_loop(self) -> None:
        total = len(self.keywords)
        finished_since_reentry = (
            self.start_index % self.restart_keyword_interval
            if self.restart_keyword_interval > 0
            else 0
        )
        try:
            self.on_ready()
            for index, target_keyword in enumerate(
                self.keywords[self.start_index :], start=self.start_index + 1
            ):
                if self._stop.is_set():
                    return
                fatal_error = getattr(self.backend, "fatal_error", None)
                if isinstance(fatal_error, Exception):
                    raise fatal_error
                needs_recovery = getattr(self.backend, "needs_recovery", False)
                if isinstance(needs_recovery, bool) and needs_recovery:
                    self.log("检测到小程序宿主已退出，先自动重建连接再开始下一个关键词")
                    self.on_progress(
                        target_keyword, index, total, "小程序已退出，正在自动恢复"
                    )
                    self.backend.recover_search_runtime()
                retry_number = 0
                last_keyword_error: Exception | None = None
                while not self._stop.is_set():
                    try:
                        if retry_number and _is_transient_desktop_error(last_keyword_error):
                            self.backend.recover_search_runtime()
                        self.keyword = target_keyword
                        self.on_keyword(target_keyword)
                        self.on_progress(
                            target_keyword, index, total, "正在搜索并应用筛选"
                        )
                        outcome = self.backend.collect_keyword(
                            target_keyword,
                            self.min_price,
                            self.max_price,
                            self.on_products,
                        )
                    except Exception as exc:
                        if self._stop.is_set():
                            return
                        message = str(exc)
                        if self._is_fatal_automation_error(message):
                            raise
                        last_keyword_error = exc
                        if retry_number < MAX_KEYWORD_RETRIES:
                            retry_number += 1
                            reconnecting = _is_transient_desktop_error(exc)
                            delay = min(self.retry_seconds, RUNTIME_RECOVERY_RETRY_SECONDS) if reconnecting else self.retry_seconds
                            recovery_stage = "重开小程序并重试当前词" if reconnecting else "重试当前词"
                            self.log(
                                f"关键词“{target_keyword}”本次失败：{message}；"
                                f"{delay:g} 秒后{recovery_stage}（{retry_number}/{MAX_KEYWORD_RETRIES}）"
                            )
                            self.on_progress(
                                target_keyword,
                                index,
                                total,
                                f"恢复中，{delay:g} 秒后{recovery_stage}（{retry_number}/{MAX_KEYWORD_RETRIES}）",
                            )
                            if self._stop.wait(delay):
                                return
                            continue
                        raise DesktopWechatError(
                            f"当前关键词重试 {MAX_KEYWORD_RETRIES} 次后仍失败，已暂停并保留断点；"
                            f"停在第 {index}/{total} 个“{target_keyword}”，没有跳过：{message}"
                        ) from exc
                    if self._stop.is_set():
                        return
                    self.on_progress(target_keyword, index, total, outcome)
                    self.on_keyword_finished(target_keyword, index, total, outcome)
                    finished_since_reentry += 1
                    break
                if (
                    self.restart_keyword_interval > 0
                    and finished_since_reentry >= self.restart_keyword_interval
                    and index < total
                ):
                    next_keyword = self.keywords[index]
                    self.on_progress(
                        next_keyword, index + 1, total, "正在重进电脑版小店搜索页"
                    )
                    self._reenter_search_page()
                    finished_since_reentry = 0
            if not self._stop.is_set():
                self.log(f"全部 {total} 个关键词已通过电脑版微信采集完成")
                self.on_complete()
        except Exception as exc:
            if not self._stop.is_set():
                message = str(exc)
                self.log("电脑版微信自动采集已暂停：" + message)
                self.on_error(message)


__all__ = [
    "DesktopKeywordListener",
    "DesktopWechatBackend",
    "DesktopWechatError",
    "desktop_cards_from_rows",
    "validate_desktop_profile",
]
