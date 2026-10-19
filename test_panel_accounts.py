import asyncio
import hashlib
import time
from test_public_panel import PublicPanelTests
from web_panel import PANEL_KEY


class AccountTests(PublicPanelTests):
    async def test_russian_auth_text_is_preserved(self):
        for path, text in (("/login", "Логин"), ("/register", "Ваш аккаунт")):
            response = await self.client.get(path, headers=self.headers)
            html = await response.text()
            self.assertIn(text, html)
            self.assertNotIn("???", html)
        owner = await self.login_as()
        response = await self.client.get("/access", headers=owner)
        self.assertIn("Создать приглашение", await response.text())
        response = await self.client.post("/login", headers=owner, json={"password": "wrong"})
        self.assertIn("Неверный логин или пароль", (await response.json())["error"])

    async def login_as(self, username="owner", password=None):
        headers = {**self.headers, "X-CSRF-Token": self.app[PANEL_KEY].csrf_token}
        response = await self.client.post("/login", headers=headers, json={"username": username, "password": password or self.password})
        self.assertEqual(response.status, 200)
        return {**headers, "Cookie": "immortal_session=" + response.cookies["immortal_session"].value}

    async def invitation(self):
        self.owner = await self.login_as()
        response = await self.client.post("/api/access/invite", headers=self.owner, json={})
        self.assertEqual(response.status, 200)
        return (await response.json())["url"].split("invite=")[1]

    async def register(self, token, username="tester", password="my-personal-password"):
        return await self.client.post("/register", headers={**self.headers, "X-CSRF-Token": self.app[PANEL_KEY].csrf_token},
            json={"invite": token, "username": username, "password": password, "confirm": password})

    async def test_register_login_revoke(self):
        token = await self.invitation()
        response = await self.register(token)
        self.assertEqual(response.status, 200)
        self.assertTrue(response.cookies["immortal_session"]["secure"])
        row = self.app[PANEL_KEY].db.conn.execute("SELECT * FROM panel_accounts").fetchone()
        self.assertNotEqual(row["hash"], "my-personal-password")
        member = await self.login_as("tester", "my-personal-password")
        self.assertEqual((await self.client.get("/", headers=member)).status, 200)
        self.assertEqual((await self.client.post("/api/access/invite", headers=member, json={})).status, 403)
        self.assertEqual((await self.client.get("/api/access", headers=member)).status, 403)
        self.assertEqual((await self.client.delete("/api/access/tester", headers=self.owner)).status, 200)
        self.assertEqual((await self.client.get("/api/access", headers=member)).status, 401)

    async def test_one_use_even_concurrently(self):
        token = await self.invitation()
        responses = await asyncio.gather(self.register(token, "first"), self.register(token, "second"))
        self.assertEqual(sorted(r.status for r in responses), [200, 400])

    async def test_expired_missing_and_weak_password(self):
        token = await self.invitation()
        self.assertEqual((await self.register(token, password="short")).status, 400)
        self.assertEqual((await self.register("invalid")).status, 400)
        self.app[PANEL_KEY].db.conn.execute("UPDATE panel_invites SET expires=?", (time.time()-1,))
        self.app[PANEL_KEY].db.conn.commit()
        self.assertEqual((await self.register(token)).status, 400)

    async def test_failed_registration_does_not_consume_invite(self):
        token = await self.invitation()
        self.assertEqual((await self.register(token, username="owner")).status, 400)
        self.assertEqual((await self.register(token)).status, 200)
        token2 = await self.invitation()
        self.assertEqual((await self.register(token2)).status, 409)
        self.assertEqual((await self.register(token2, username="new_user")).status, 200)

    async def test_registration_csrf_and_anonymous_invitation(self):
        self.assertEqual((await self.client.get("/register", headers=self.headers)).status, 200)
        self.assertEqual((await self.client.post("/register", headers=self.headers, json={})).status, 403)
        headers = {**self.headers, "X-CSRF-Token": self.app[PANEL_KEY].csrf_token}
        self.assertEqual((await self.client.post("/api/access/invite", headers=headers, json={})).status, 401)
