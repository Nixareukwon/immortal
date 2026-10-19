import os
from unittest.mock import AsyncMock

from panel_twitch import OWNER_SCOPES
from test_panel_accounts import AccountTests
from web_panel import PANEL_KEY

DEVICE = {"device_code": "device-1", "user_code": "ABCDEFGH",
          "verification_uri": "https://www.twitch.tv/activate?device-code=ABCDEFGH",
          "interval": 5, "expires_in": 1800}

OWNER_TOKEN = {"access_token": "owner-token", "refresh_token": "owner-refresh",
               "expires_in": 14400, "scope": ["channel:read:editors", "user:read:chat"]}


class TwitchTests(AccountTests):
    async def asyncSetUp(self):
        self.env_before = os.environ.get("TWITCH_BROADCASTER_TOKEN")
        await super().asyncSetUp()
        self.panel = self.app[PANEL_KEY]
        self.twitch = self.panel.twitch
        self.twitch.client_id = "test-client"
        self.twitch.device_start = AsyncMock(return_value=dict(DEVICE))
        self.twitch.device_token = AsyncMock(return_value={"access_token": "token-1"})
        self.twitch.device_identity = AsyncMock(return_value=("1001", "viewer_twitch"))

    async def asyncTearDown(self):
        if self.env_before is None:
            os.environ.pop("TWITCH_BROADCASTER_TOKEN", None)
        else:
            os.environ["TWITCH_BROADCASTER_TOKEN"] = self.env_before
        await super().asyncTearDown()

    def open_headers(self, session=None):
        headers = {**self.headers, "X-CSRF-Token": self.panel.csrf_token}
        return {**headers, **session} if session else headers

    async def start_flow(self, purpose="login", session=None):
        return await self.client.post("/auth/twitch/start", headers=self.open_headers(session), json={"purpose": purpose})

    async def poll_flow(self, flow, session=None):
        return await self.client.post("/auth/twitch/poll", headers=self.open_headers(session), json={"flow": flow})

    async def flow_token(self, purpose="login", session=None):
        response = await self.start_flow(purpose, session)
        self.assertEqual(response.status, 200)
        return await response.json()

    async def link_account(self, session, twitch=("1001", "viewer_twitch")):
        start = await self.flow_token("link", session)
        self.twitch.device_identity = AsyncMock(return_value=twitch)
        response = await self.poll_flow(start["flow"], session)
        self.assertEqual(response.status, 200)
        return await response.json()

    async def link_owner(self):
        self.owner = await self.login_as()
        self.twitch.device_token = AsyncMock(return_value=dict(OWNER_TOKEN))
        data = await self.link_account(self.owner, twitch=("7132", "owner_twitch"))
        self.assertEqual((data["status"], data["full"]), ("linked", True))
        return data

    async def test_start_login_flow(self):
        data = await self.flow_token("login")
        self.assertIn("flow", data)
        self.assertEqual(data["code"], "ABCDEFGH")
        self.assertTrue(data["uri"].startswith("https://www.twitch.tv/"))
        self.assertFalse(data["full"])
        self.twitch.device_start.assert_awaited()

    async def test_login_requires_linked_twitch(self):
        start = await self.flow_token("login")
        response = await self.poll_flow(start["flow"])
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["status"], "unlinked")

    async def test_link_then_twitch_login(self):
        token = await self.invitation()
        self.assertEqual((await self.register(token, "tester")).status, 200)
        member = await self.login_as("tester", "my-personal-password")
        data = await self.link_account(member)
        self.assertEqual((data["status"], data["login"]), ("linked", "viewer_twitch"))
        self.assertEqual(self.panel.twitch.links(), {"tester": "viewer_twitch"})
        state = await self.client.get("/api/twitch", headers=member)
        self.assertEqual((await state.json())["link"], {"login": "viewer_twitch"})
        start = await self.flow_token("login")
        response = await self.poll_flow(start["flow"])
        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertEqual((body["status"], body["login"]), ("ok", "viewer_twitch"))
        cookie = response.cookies["immortal_session"].value
        page = await self.client.get("/", headers={**self.headers, "Cookie": "immortal_session=" + cookie})
        self.assertEqual(page.status, 200)

    async def test_link_requires_session(self):
        self.assertEqual((await self.start_flow("link")).status, 401)

    async def test_twitch_cannot_link_two_accounts(self):
        token = await self.invitation()
        self.assertEqual((await self.register(token, "tester")).status, 200)
        member = await self.login_as("tester", "my-personal-password")
        await self.link_account(member)
        token2 = await self.invitation()
        self.assertEqual((await self.register(token2, "second")).status, 200)
        second = await self.login_as("second", "my-personal-password")
        start = await self.flow_token("link", second)
        response = await self.poll_flow(start["flow"], second)
        self.assertEqual(response.status, 409)
        self.assertEqual(self.panel.twitch.links(), {"tester": "viewer_twitch"})

    async def test_unlink_rules(self):
        token = await self.invitation()
        self.assertEqual((await self.register(token, "tester")).status, 200)
        member = await self.login_as("tester", "my-personal-password")
        await self.link_account(member)
        response = await self.client.post("/api/twitch/unlink", headers=member, json={"username": "owner"})
        self.assertEqual(response.status, 403)
        response = await self.client.post("/api/twitch/unlink", headers=self.owner, json={"username": "tester"})
        self.assertEqual(response.status, 200)
        self.assertEqual(self.panel.twitch.links(), {})
        await self.link_account(member, twitch=("2002", "second_twitch"))
        response = await self.client.post("/api/twitch/unlink", headers=member, json={})
        self.assertEqual(response.status, 200)
        self.assertEqual(self.panel.twitch.links(), {})

    async def test_start_rate_limit(self):
        for _ in range(10):
            self.assertEqual((await self.start_flow()).status, 200)
        self.assertEqual((await self.start_flow()).status, 429)

    async def test_csrf_and_state_protection(self):
        response = await self.client.post("/auth/twitch/start", headers=self.headers, json={"purpose": "login"})
        self.assertEqual(response.status, 403)
        response = await self.client.get("/api/twitch", headers=self.headers)
        self.assertEqual(response.status, 401)

    async def test_unknown_and_expired_flow(self):
        response = await self.poll_flow("missing")
        self.assertEqual((await response.json())["status"], "expired")
        start = await self.flow_token("login")
        for flow in self.twitch.flows.values():
            flow["expires"] = 0
        response = await self.poll_flow(start["flow"])
        self.assertEqual((await response.json())["status"], "expired")
        self.assertEqual(self.twitch.flows, {})

    async def test_pending_and_denied_statuses(self):
        start = await self.flow_token("login")
        self.twitch.device_token = AsyncMock(return_value={"message": "authorization_pending"})
        self.assertEqual((await (await self.poll_flow(start["flow"])).json())["status"], "pending")
        for flow in self.twitch.flows.values():
            flow["next_poll"] = 0
        self.twitch.device_token = AsyncMock(return_value={"message": "access_denied"})
        self.assertEqual((await (await self.poll_flow(start["flow"])).json())["status"], "denied")
        self.assertEqual(self.twitch.flows, {})

    async def test_revoke_account_removes_twitch_link(self):
        token = await self.invitation()
        self.assertEqual((await self.register(token, "tester")).status, 200)
        member = await self.login_as("tester", "my-personal-password")
        await self.link_account(member)
        response = await self.client.delete("/api/access/tester", headers=self.owner)
        self.assertEqual(response.status, 200)
        self.assertEqual(self.panel.twitch.links(), {})
        listing = await self.client.get("/api/access", headers=self.owner)
        self.assertEqual((await listing.json())["twitch"], {})

    async def test_unconfigured_client(self):
        self.twitch.client_id = ""
        self.assertEqual((await self.start_flow("login")).status, 503)
        owner = await self.login_as()
        state = await self.client.get("/api/twitch", headers=owner)
        self.assertFalse((await state.json())["configured"])

    async def test_login_page_shows_twitch_button(self):
        response = await self.client.get("/login", headers=self.headers)
        html = await response.text()
        self.assertIn("twitch-login", html)
        self.assertNotIn("{{twitch_hidden}}", html)
        self.assertNotIn('class="twitch-entry" hidden', html)
        self.twitch.client_id = ""
        response = await self.client.get("/login", headers=self.headers)
        self.assertIn('class="twitch-entry" hidden', await response.text())

    async def test_owner_link_requests_all_scopes_and_stores_token(self):
        data = await self.link_owner()
        self.assertEqual(data["login"], "owner_twitch")
        self.assertEqual(len(OWNER_SCOPES), 81)
        self.assertIn("channel:manage:broadcast", OWNER_SCOPES)
        self.assertEqual(self.twitch.device_start.await_args.args[0], " ".join(OWNER_SCOPES))
        row = self.twitch.token_row()
        self.assertEqual((row["twitch_login"], row["refresh_token"]), ("owner_twitch", "owner-refresh"))
        self.assertEqual(os.environ.get("TWITCH_BROADCASTER_TOKEN"), "owner-token")
        payload = await (await self.client.get("/api/twitch", headers=self.owner)).json()
        self.assertTrue(payload["enhanced"])
        self.assertEqual(payload["scopes"], 2)

    async def test_member_link_keeps_minimal_scopes(self):
        token = await self.invitation()
        self.assertEqual((await self.register(token, "tester")).status, 200)
        member = await self.login_as("tester", "my-personal-password")
        data = await self.link_account(member)
        self.assertFalse(data["full"])
        self.assertEqual(self.twitch.device_start.await_args.args[0], "")
        self.assertIsNone(self.twitch.token_row())
        self.assertEqual(os.environ.get("TWITCH_BROADCASTER_TOKEN"), self.env_before)

    async def test_owner_token_refreshed_when_expired(self):
        await self.link_owner()
        self.panel.db.conn.execute("UPDATE panel_twitch_tokens SET expires=0")
        self.panel.db.conn.commit()
        self.twitch.device_refresh = AsyncMock(return_value={
            "access_token": "owner-token-2", "refresh_token": "owner-refresh-2",
            "expires_in": 14400, "scope": ["channel:read:editors"]})
        self.assertEqual(await self.twitch.owner_token(), "owner-token-2")
        self.twitch.device_refresh.assert_awaited_once_with("owner-refresh")
        row = self.twitch.token_row()
        self.assertEqual((row["access_token"], row["refresh_token"]), ("owner-token-2", "owner-refresh-2"))
        await self.twitch.apply_owner_token()
        self.assertEqual(os.environ.get("TWITCH_BROADCASTER_TOKEN"), "owner-token-2")

    async def test_owner_unlink_removes_stored_token(self):
        await self.link_owner()
        self.assertIsNotNone(self.twitch.token_row())
        response = await self.client.post("/api/twitch/unlink", headers=self.owner, json={})
        self.assertEqual(response.status, 200)
        self.assertIsNone(self.twitch.token_row())
        self.assertEqual(self.twitch.links(), {})
        self.assertEqual(os.environ.get("TWITCH_BROADCASTER_TOKEN"), self.env_before)

    async def test_refresh_failure_shown_in_state(self):
        await self.link_owner()
        self.panel.db.conn.execute("UPDATE panel_twitch_tokens SET expires=0")
        self.panel.db.conn.commit()
        self.twitch.device_refresh = AsyncMock(side_effect=RuntimeError("invalid client"))
        self.assertIsNone(await self.twitch.apply_owner_token())
        payload = await (await self.client.get("/api/twitch", headers=self.owner)).json()
        self.assertIn("invalid client", payload["refresh_error"])
