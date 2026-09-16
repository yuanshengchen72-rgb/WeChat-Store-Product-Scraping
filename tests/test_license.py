import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wechat_shop_scraper.license import (
    LicenseClient,
    LicenseStorage,
    PRODUCT_ID,
    SOFTWARE_VERSION,
    normalize_code,
)


VALID_CODE = "WXS-ABCD-EFGH-JKLM-NPQR"


class XorProtector:
    def protect(self, data: bytes) -> bytes:
        return bytes(value ^ 0xA5 for value in data)

    def unprotect(self, data: bytes) -> bytes:
        return self.protect(data)


class FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self.body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


class LicenseStorageTests(unittest.TestCase):
    def test_installation_id_is_stable_and_code_is_not_stored_in_plaintext(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = LicenseStorage(Path(directory), XorProtector())

            first_id = storage.get_or_create_installation_id()
            second_id = storage.get_or_create_installation_id()
            storage.save_code(VALID_CODE.lower())

            self.assertEqual(first_id, second_id)
            self.assertEqual(storage.load_code(), VALID_CODE)
            self.assertNotIn(
                VALID_CODE.encode("ascii"),
                storage.license_path.read_bytes(),
            )

    def test_clear_code_preserves_installation_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = LicenseStorage(Path(directory), XorProtector())
            installation_id = storage.get_or_create_installation_id()
            storage.save_code(VALID_CODE)

            storage.clear_code()

            self.assertIsNone(storage.load_code())
            self.assertEqual(storage.get_or_create_installation_id(), installation_id)


class LicenseClientTests(unittest.TestCase):
    def test_validate_posts_product_device_and_version(self) -> None:
        response = FakeResponse(
            {
                "success": True,
                "code": "AUTHORIZED",
                "msg": "卡密验证成功。",
                "data": {
                    "id": "license-id",
                    "product": PRODUCT_ID,
                    "codeHint": "WXS-ABCD…NPQR",
                    "activatedAt": "2026-08-25T12:00:00Z",
                    "expiresAt": "2026-09-24T12:00:00Z",
                    "deviceName": "TEST-PC",
                },
            }
        )
        installation_id = "00000000-0000-0000-0000-000000000001"
        with mock.patch(
            "wechat_shop_scraper.license.urllib.request.urlopen",
            return_value=response,
        ) as urlopen, mock.patch(
            "wechat_shop_scraper.license.device_name",
            return_value="TEST-PC",
        ):
            authorization = LicenseClient(timeout=3).validate(
                VALID_CODE,
                installation_id,
            )

        request = urlopen.call_args.args[0]
        sent = json.loads(request.data.decode("utf-8"))
        self.assertEqual(sent["code"], VALID_CODE)
        self.assertEqual(sent["product"], PRODUCT_ID)
        self.assertEqual(sent["installationId"], installation_id)
        self.assertEqual(sent["deviceName"], "TEST-PC")
        self.assertEqual(sent["softwareVersion"], SOFTWARE_VERSION)
        self.assertEqual(authorization.license_id, "license-id")
        self.assertEqual(authorization.expires_at, "2026-09-24T12:00:00Z")

    def test_invalid_code_is_rejected_before_network_request(self) -> None:
        with mock.patch(
            "wechat_shop_scraper.license.urllib.request.urlopen"
        ) as urlopen:
            with self.assertRaisesRegex(ValueError, "卡密格式"):
                LicenseClient().validate(
                    "not-a-license",
                    "00000000-0000-0000-0000-000000000001",
                )
        urlopen.assert_not_called()

    def test_normalize_code_accepts_lowercase_and_trims_spaces(self) -> None:
        self.assertEqual(normalize_code(f"  {VALID_CODE.lower()}  "), VALID_CODE)


if __name__ == "__main__":
    unittest.main()
