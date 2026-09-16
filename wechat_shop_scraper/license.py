from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes as wintypes
import hashlib
import json
import os
import platform
import re
import ssl
import tempfile
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


LICENSE_API_URL = (
    "https://api.dianchao1688.com/v1/software-auth/license-key/validate"
)
PRODUCT_ID = "wechat-shop-scraper"
SOFTWARE_VERSION = "1.1.0"
CODE_PATTERN = re.compile(r"^WXS-[A-HJ-NP-Z2-9]{4}(?:-[A-HJ-NP-Z2-9]{4}){3}$")
DPAPI_ENTROPY = b"WeChatShopScraper License v1"


class LicenseError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class LicenseAuthorization:
    license_id: str
    code_hint: str
    activated_at: str | None
    expires_at: str | None
    device_name: str


class DataProtector(Protocol):
    def protect(self, data: bytes) -> bytes: ...

    def unprotect(self, data: bytes) -> bytes: ...


class WindowsDpapiProtector:
    class _DataBlob(ctypes.Structure):
        _fields_ = [
            ("cbData", ctypes.c_uint32),
            ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
        ]

    @classmethod
    def _blob(cls, data: bytes) -> tuple[_DataBlob, object]:
        buffer = ctypes.create_string_buffer(data)
        blob = cls._DataBlob(
            len(data),
            ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)),
        )
        return blob, buffer

    def protect(self, data: bytes) -> bytes:
        return self._crypt(data, decrypt=False)

    def unprotect(self, data: bytes) -> bytes:
        return self._crypt(data, decrypt=True)

    def _crypt(self, data: bytes, *, decrypt: bool) -> bytes:
        if os.name != "nt":
            raise OSError("Windows DPAPI 只能在 Windows 上使用")
        input_blob, input_buffer = self._blob(data)
        entropy_blob, entropy_buffer = self._blob(DPAPI_ENTROPY)
        output_blob = self._DataBlob()
        crypt32 = ctypes.windll.crypt32  # type: ignore[attr-defined]
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        blob_pointer = ctypes.POINTER(self._DataBlob)
        crypt32.CryptProtectData.argtypes = [
            blob_pointer,
            wintypes.LPCWSTR,
            blob_pointer,
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.DWORD,
            blob_pointer,
        ]
        crypt32.CryptProtectData.restype = wintypes.BOOL
        crypt32.CryptUnprotectData.argtypes = [
            blob_pointer,
            ctypes.POINTER(wintypes.LPWSTR),
            blob_pointer,
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.DWORD,
            blob_pointer,
        ]
        crypt32.CryptUnprotectData.restype = wintypes.BOOL
        kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
        kernel32.LocalFree.restype = wintypes.HLOCAL
        if decrypt:
            success = crypt32.CryptUnprotectData(
                ctypes.byref(input_blob),
                None,
                ctypes.byref(entropy_blob),
                None,
                None,
                0,
                ctypes.byref(output_blob),
            )
        else:
            success = crypt32.CryptProtectData(
                ctypes.byref(input_blob),
                "微信小店采集卡密",
                ctypes.byref(entropy_blob),
                None,
                None,
                0,
                ctypes.byref(output_blob),
            )
        _ = input_buffer, entropy_buffer
        if not success:
            raise ctypes.WinError()
        try:
            return ctypes.string_at(output_blob.pbData, output_blob.cbData)
        finally:
            kernel32.LocalFree(output_blob.pbData)


class LicenseStorage:
    def __init__(
        self,
        root: Path | None = None,
        protector: DataProtector | None = None,
    ) -> None:
        self.root = root or self._default_root()
        self.protector = protector or WindowsDpapiProtector()
        self.installation_path = self.root / "installation.json"
        self.license_path = self.root / "license.dat"

    @staticmethod
    def _default_root() -> Path:
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            return Path(local_app_data) / "WeChatShopScraper"
        return Path.home() / ".wechat-shop-scraper"

    def get_or_create_installation_id(self) -> str:
        try:
            payload = json.loads(self.installation_path.read_text(encoding="utf-8"))
            value = str(payload.get("installation_id", ""))
            return str(uuid.UUID(value))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            value = str(uuid.uuid4())
            self.root.mkdir(parents=True, exist_ok=True)
            self._atomic_write(
                self.installation_path,
                json.dumps(
                    {"installation_id": value},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8"),
            )
            return value

    def load_code(self) -> str | None:
        try:
            encrypted = base64.b64decode(self.license_path.read_bytes(), validate=True)
            code = self.protector.unprotect(encrypted).decode("utf-8")
            return normalize_code(code)
        except (OSError, ValueError, UnicodeDecodeError):
            return None

    def save_code(self, code: str) -> None:
        normalized = normalize_code(code)
        self.root.mkdir(parents=True, exist_ok=True)
        encrypted = self.protector.protect(normalized.encode("utf-8"))
        self._atomic_write(self.license_path, base64.b64encode(encrypted))

    def clear_code(self) -> None:
        try:
            self.license_path.unlink()
        except FileNotFoundError:
            pass

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            dir=path.parent,
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, path)
        finally:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


class LicenseClient:
    def __init__(
        self,
        endpoint: str = LICENSE_API_URL,
        timeout: float = 10.0,
    ) -> None:
        self.endpoint = endpoint
        self.timeout = timeout

    def validate(self, code: str, installation_id: str) -> LicenseAuthorization:
        normalized = normalize_code(code)
        payload = json.dumps(
            {
                "code": normalized,
                "product": PRODUCT_ID,
                "installationId": str(uuid.UUID(installation_id)),
                "deviceName": device_name(),
                "softwareVersion": SOFTWARE_VERSION,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint,
            data=payload,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": f"WeChatShopScraper/{SOFTWARE_VERSION}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=self.timeout,
                context=ssl.create_default_context(),
            ) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            body = exc.read()
            self._raise_response_error(body, exc.code)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise LicenseError(
                "NETWORK",
                "无法连接卡密服务器，请检查网络后重试。",
            ) from exc
        return self._parse_success(body)

    @staticmethod
    def _raise_response_error(body: bytes, status: int) -> None:
        try:
            payload = json.loads(body.decode("utf-8"))
            code = str(payload.get("code") or f"HTTP_{status}")
            message = str(payload.get("msg") or f"卡密验证失败（{status}）。")
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            code = f"HTTP_{status}"
            message = f"卡密验证失败（{status}）。"
        raise LicenseError(code, message)

    @staticmethod
    def _parse_success(body: bytes) -> LicenseAuthorization:
        try:
            payload = json.loads(body.decode("utf-8"))
            if not payload.get("success"):
                raise LicenseError(
                    str(payload.get("code") or "REJECTED"),
                    str(payload.get("msg") or "卡密验证失败。"),
                )
            data = payload["data"]
            return LicenseAuthorization(
                license_id=str(data["id"]),
                code_hint=str(data["codeHint"]),
                activated_at=data.get("activatedAt"),
                expires_at=data.get("expiresAt"),
                device_name=str(data["deviceName"]),
            )
        except LicenseError:
            raise
        except (KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LicenseError(
                "INVALID_RESPONSE",
                "卡密服务器返回异常，请稍后重试。",
            ) from exc


def normalize_code(value: str) -> str:
    normalized = value.strip().upper()
    if not CODE_PATTERN.fullmatch(normalized):
        raise ValueError("卡密格式不正确，请完整粘贴以 WXS- 开头的卡密。")
    return normalized


def device_name() -> str:
    value = platform.node().strip()
    return value[:100] if value else "Windows电脑"


def masked_code(code: str) -> str:
    normalized = normalize_code(code)
    return f"{normalized[:8]}…{normalized[-4:]}"


def installation_fingerprint(installation_id: str) -> str:
    """Return a non-secret short identifier useful in local diagnostics."""
    normalized = str(uuid.UUID(installation_id))
    return hashlib.sha256(normalized.encode("ascii")).hexdigest()[:12]
