import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from aiohttp.test_utils import TestClient, TestServer

with patch("dotenv.load_dotenv"), patch.dict(os.environ, {}, clear=True):
    from web_panel import create_app, PANEL_KEY


class PublicPanelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.password = "a-long-sharing-password-for-tests"
        self.app = create_app(db_path=Path(self.temp.name) / "test.db", runtime=SimpleNamespace(stop=AsyncMock()),
                              password=self.password, public_origin="https://example.trycloudflare.com")
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()
        self.headers = {"Host": "example.trycloudflare.com", "X-Forwarded-Proto": "https", "CF-Connecting-IP": "203.0.113.7"}

    async def asyncTearDown(self):
        await self.client.close()
        self.temp.cleanup()

    async def test_public_root_requires_login(self):
        response = await self.client.get("/", headers=self.headers, allow_redirects=False)
        self.assertEqual(response.status, 302)
        self.assertEqual(response.headers["Location"], "/login")
        response = await self.client.get("/api/state", headers=self.headers)
        self.assertEqual(response.status, 401)

    async def test_https_login_and_cookie(self):
        headers = {**self.headers, "Origin": "https://example.trycloudflare.com", "X-CSRF-Token": self.app[PANEL_KEY].csrf_token}
        response = await self.client.post("/login", headers=headers, json={"password": self.password})
        self.assertEqual(response.status, 200)
        cookie = response.cookies["immortal_session"]
        self.assertTrue(cookie["secure"])
        self.assertTrue(cookie["httponly"])
        headers["Cookie"] = "immortal_session=" + cookie.value
        response = await self.client.get("/", headers=headers)
        self.assertEqual(response.status, 200)

    async def test_bad_origin_host_or_proxy_rejected(self):
        for changes in ({"Host": "evil.example"}, {"X-Forwarded-Proto": "http"}, {"CF-Connecting-IP": "invalid"}):
            response = await self.client.get("/login", headers={**self.headers, **changes})
            self.assertEqual(response.status, 403)
        response = await self.client.post("/login", json={"password": self.password}, headers={**self.headers,
            "Origin": "https://evil.example", "X-CSRF-Token": self.app[PANEL_KEY].csrf_token})
        self.assertEqual(response.status, 403)

    def test_public_mode_requires_strong_password(self):
        with self.assertRaises(ValueError):
            create_app(password="", public_origin="https://example.trycloudflare.com")

    async def test_login_limit_uses_visitor_address(self):
        headers = {**self.headers, "X-CSRF-Token": self.app[PANEL_KEY].csrf_token}
        for _ in range(10):
            response = await self.client.post("/login", headers=headers, json={"password": "wrong"})
            self.assertEqual(response.status, 401)
        response = await self.client.post("/login", headers=headers, json={"password": self.password})
        self.assertEqual(response.status, 429)
        response = await self.client.post("/login", headers={**headers, "CF-Connecting-IP": "203.0.113.8"}, json={"password": self.password})
        self.assertEqual(response.status, 200)
