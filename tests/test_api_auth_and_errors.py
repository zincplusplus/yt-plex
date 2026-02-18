import asyncio
import importlib
import json
import os
import sys
import tempfile
import unittest

from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError
from starlette.requests import Request


def make_request(headers=None):
    headers = headers or {}
    raw_headers = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "path": "/",
        "raw_path": b"/",
        "headers": raw_headers,
        "query_string": b"",
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 12345),
    }
    return Request(scope)


class TestApiAuthAndErrors(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_cwd = os.getcwd()
        self._old_admin = os.environ.get("YT_PLEX_ADMIN_KEY")
        self._old_operator = os.environ.get("YT_PLEX_OPERATOR_KEY")
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._old_cwd)
        self._tmp.cleanup()

        if self._old_admin is None:
            os.environ.pop("YT_PLEX_ADMIN_KEY", None)
        else:
            os.environ["YT_PLEX_ADMIN_KEY"] = self._old_admin

        if self._old_operator is None:
            os.environ.pop("YT_PLEX_OPERATOR_KEY", None)
        else:
            os.environ["YT_PLEX_OPERATOR_KEY"] = self._old_operator

        for name in ("server", "videoqueue", "scanner", "settings", "runtime_state"):
            sys.modules.pop(name, None)

    def _load_server(self, admin_key="", operator_key=""):
        if admin_key:
            os.environ["YT_PLEX_ADMIN_KEY"] = admin_key
        else:
            os.environ.pop("YT_PLEX_ADMIN_KEY", None)

        if operator_key:
            os.environ["YT_PLEX_OPERATOR_KEY"] = operator_key
        else:
            os.environ.pop("YT_PLEX_OPERATOR_KEY", None)

        for name in ("server", "videoqueue", "scanner", "settings", "runtime_state"):
            sys.modules.pop(name, None)

        import server  # noqa: PLC0415
        return importlib.reload(server)

    def test_require_role_for_mutations(self):
        srv = self._load_server(admin_key="admin-secret", operator_key="op-secret")

        with self.assertRaises(HTTPException) as ctx:
            srv._require_role(make_request(), "operator")
        self.assertEqual(ctx.exception.status_code, 401)

        role = srv._require_role(make_request({"x-api-key": "op-secret"}), "operator")
        self.assertEqual(role, "operator")

        with self.assertRaises(HTTPException) as ctx:
            srv._require_role(make_request({"x-api-key": "op-secret"}), "admin")
        self.assertEqual(ctx.exception.status_code, 403)

    def test_settings_model_validation(self):
        srv = self._load_server(admin_key="admin-secret", operator_key="op-secret")
        with self.assertRaises(Exception):
            srv.SettingsUpdateRequest(scan_interval=0)

    def test_validation_error_response_shape(self):
        srv = self._load_server()
        exc = RequestValidationError([
            {"loc": ("body", "scan_interval"), "msg": "Input should be greater than or equal to 1", "type": "greater_than_equal"}
        ])
        resp = asyncio.run(srv.request_validation_exception_handler(make_request(), exc))
        self.assertEqual(resp.status_code, 400)
        body = json.loads(resp.body.decode())
        self.assertEqual(body["error"]["code"], "validation_error")
        self.assertIsInstance(body["error"].get("fields"), list)

    def test_dead_letter_endpoint_filters(self):
        srv = self._load_server()
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(srv.queue_dead_letter(status="bad"))
        self.assertEqual(ctx.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
