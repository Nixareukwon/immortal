import os
import unittest
from unittest.mock import AsyncMock, patch

with patch("dotenv.load_dotenv"), patch.dict(os.environ, {}, clear=True):
    import bot as bot_module
    from test_panel_twitch import TwitchTests
    from web_panel import PANEL_KEY


class ChannelTests(TwitchTests):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.panel = self.app[PANEL_KEY]
        runtime = self.panel.runtime
        runtime.join_channel = AsyncMock(return_value=True)
        runtime.leave_channel = AsyncMock(return_value=True)
        runtime.send_chat = AsyncMock(return_value={"sent": True})
        runtime.snapshot = lambda: {
            "status": "running", "connected": True, "nick": "bot", "error": "", "started_at": 0,
            "logs": [], "channels": list(bot_module.CHANNELS),
        }

    def moderation_payload(self):
        return {"enabled": True, "video": False, "lang": False, "mode": 2, "size": 100, "main_size": 100,
                "percent": 50, "spam_messages": 3, "spam_seconds": 5, "max_link_length": 100,
                "timeouts": [1, 10]}

    async def member(self, twitch=None):
        token = await self.invitation()
        self.assertEqual((await self.register(token)).status, 200)
        session = await self.login_as("tester", "my-personal-password")
        if twitch is not None:
            await self.link_account(session, twitch=twitch)
        return session

    async def connect_channel(self, session, twitch):
        """Подключить канал так, как это делает панель: через вход в Twitch-аккаунт канала."""
        start = await self.flow_token("channel", session)
        self.twitch.device_identity = AsyncMock(return_value=twitch)
        response = await self.poll_flow(start["flow"], session)
        self.assertEqual(response.status, 200)
        return await response.json()

    async def test_channel_flow_requires_session(self):
        self.assertEqual((await self.start_flow("channel")).status, 401)

    async def test_owner_connects_and_removes_channel(self):
        owner = await self.login_as()
        data = await self.connect_channel(owner, ("7001", "newstreamer"))
        self.assertEqual((data["status"], data["login"]), ("channel", "newstreamer"))
        self.assertTrue(data["joined"])
        self.assertIn("newstreamer", bot_module.CHANNELS)
        self.panel.runtime.join_channel.assert_awaited_with("newstreamer")
        state = await (await self.client.get("/api/state", headers=owner)).json()
        self.assertIn("newstreamer", state["channels"])
        self.assertEqual(state["channel_owners"]["newstreamer"], "owner")
        self.assertEqual(state["identity"], {"username": "owner", "owner": True})
        self.assertTrue(state["add_channel"]["allowed"])
        # Повторное подключение уже подключённого канала отклоняется.
        duplicate = await self.connect_channel(owner, ("7002", "newstreamer"))
        self.assertEqual(duplicate["status"], "channel-error")
        self.assertIn("уже подключён", duplicate["error"])
        # Старое подключение простым вводом названия больше не существует.
        typed = await self.client.post("/api/channels", headers=owner, json={"channel": "typed"})
        self.assertIn(typed.status, (404, 405))
        self.assertNotIn("typed", bot_module.CHANNELS)
        # Канал из .env через панель не подключается и не отключается.
        with patch.dict(os.environ, {"TWITCH_CHANNELS": "smailrofl"}):
            self.panel.channels.apply()
            self.assertIn("smailrofl", bot_module.CHANNELS)
            env_attempt = await self.connect_channel(owner, ("7003", "smailrofl"))
            self.assertEqual(env_attempt["status"], "channel-error")
            self.assertIn(".env", env_attempt["error"])
            deleted = await self.client.delete("/api/channels/smailrofl", headers=owner)
            self.assertEqual(deleted.status, 400)
            self.assertIn(".env", (await deleted.json())["error"])
        response = await self.client.delete("/api/channels/newstreamer", headers=owner)
        self.assertEqual(response.status, 200)
        self.panel.runtime.leave_channel.assert_awaited_with("newstreamer")
        self.assertNotIn("newstreamer", bot_module.CHANNELS)
        self.assertEqual((await self.client.delete("/api/channels/newstreamer", headers=owner)).status, 404)

    async def test_member_connects_only_own_channel(self):
        member = await self.member(twitch=("5001", "viewer_twitch"))
        state = await (await self.client.get("/api/state", headers=member)).json()
        self.assertEqual(state["channels"], [])
        self.assertTrue(state["add_channel"]["allowed"])
        self.assertEqual(state["add_channel"]["channel"], "viewer_twitch")
        # Авторизация под чужим Twitch-аккаунтом канал не подключает.
        foreign = await self.connect_channel(member, ("5002", "smailrofl"))
        self.assertEqual(foreign["status"], "foreign")
        self.assertEqual(foreign["login"], "smailrofl")
        self.assertEqual(foreign["expected"], "viewer_twitch")
        self.assertNotIn("smailrofl", bot_module.CHANNELS)
        # Свой канал подключается после входа в свой Twitch-аккаунт.
        data = await self.connect_channel(member, ("5001", "viewer_twitch"))
        self.assertEqual((data["status"], data["login"]), ("channel", "viewer_twitch"))
        self.panel.runtime.join_channel.assert_awaited_with("viewer_twitch")
        self.assertIn("viewer_twitch", bot_module.CHANNELS)
        state = await (await self.client.get("/api/state", headers=member)).json()
        self.assertEqual(state["channels"], ["viewer_twitch"])
        self.assertEqual(state["identity"], {"username": "tester", "owner": False})
        self.assertEqual(state["channel_owners"], {"viewer_twitch": "tester"})
        self.assertFalse(state["add_channel"]["allowed"])
        self.assertEqual(state["runtime"]["channels"], ["viewer_twitch"])
        self.assertEqual((await self.client.delete("/api/channels/viewer_twitch", headers=member)).status, 200)
        self.assertNotIn("viewer_twitch", bot_module.CHANNELS)

    async def test_member_without_twitch_link_gets_hint(self):
        member = await self.member()
        refused = await self.start_flow("channel", member)
        self.assertEqual(refused.status, 403)
        self.assertIn("привяжите Twitch", (await refused.json())["error"])
        state = await (await self.client.get("/api/state", headers=member)).json()
        self.assertFalse(state["add_channel"]["allowed"])
        self.assertTrue(state["add_channel"]["needs_link"])
        self.assertIn("Доступ к панели", state["add_channel"]["hint"])

    async def test_custom_variables_are_channel_scoped_and_editable(self):
        owner = await self.login_as()
        await self.connect_channel(owner, ("7001", "newstreamer"))
        payload = {
            "channel": "newstreamer", "name": "greeting",
            "value": "Привет!", "description": "Приветствие канала",
        }
        created = await self.client.post("/api/variables", headers=owner, json=payload)
        self.assertEqual(created.status, 200)
        state = await (await self.client.get("/api/state", headers=owner)).json()
        self.assertEqual(state["custom_variables"], [{
            "channel": "newstreamer", "name": "greeting", "value": "Привет!",
            "description": "Приветствие канала",
        }])
        edited = await self.client.put(
            "/api/variables/greeting", headers=owner,
            json={**payload, "name": "welcome", "value": "Добро пожаловать!"},
        )
        self.assertEqual(edited.status, 200)
        invalid = await self.client.post(
            "/api/variables", headers=owner, json={**payload, "name": "sender"}
        )
        self.assertEqual(invalid.status, 400)
        deleted = await self.client.delete(
            "/api/variables/welcome?channel=newstreamer", headers=owner
        )
        self.assertEqual(deleted.status, 200)
        state = await (await self.client.get("/api/state", headers=owner)).json()
        self.assertEqual(state["custom_variables"], [])

    async def test_member_cannot_manage_foreign_channels(self):
        with patch.dict(os.environ, {"TWITCH_CHANNELS": "smailrofl"}):
            self.panel.channels.apply()
            owner = await self.login_as()
            member = await self.member(twitch=("5001", "viewer_twitch"))
            await self.connect_channel(member, ("5001", "viewer_twitch"))
            denied = await self.client.put("/api/moderation/smailrofl", headers=member, json=self.moderation_payload())
            self.assertEqual(denied.status, 403)
            self.assertIn("другому аккаунту", (await denied.json())["error"])
            denied = await self.client.post("/api/chat", headers=member, json={"channel": "smailrofl", "message": "привет"})
            self.assertEqual(denied.status, 403)
            denied = await self.client.post("/api/commands", headers=member, json={
                "channel": "smailrofl", "name": "тайна", "response": "чужое", "cooldown_seconds": 0, "aliases": []})
            self.assertEqual(denied.status, 403)
            allowed = await self.client.put("/api/moderation/viewer_twitch", headers=member, json=self.moderation_payload())
            self.assertEqual(allowed.status, 200)
            allowed = await self.client.post("/api/chat", headers=member, json={"channel": "viewer_twitch", "message": "привет"})
            self.assertEqual(allowed.status, 200)
            self.panel.runtime.send_chat.assert_awaited_with("viewer_twitch", "привет")
            self.assertEqual((await self.client.put("/api/moderation/smailrofl", headers=owner, json=self.moderation_payload())).status, 200)
            state = await (await self.client.get("/api/state", headers=member)).json()
            self.assertEqual(state["channels"], ["viewer_twitch"])
            self.assertEqual([item["channel"] for item in state["moderation"]], ["viewer_twitch"])

    async def test_settings_are_separated_per_channel(self):
        with patch.dict(os.environ, {"TWITCH_CHANNELS": "smailrofl"}):
            self.panel.channels.apply()
            owner = await self.login_as()
            await self.connect_channel(owner, ("7001", "second_channel"))
            created = await self.client.post("/api/commands", headers=owner, json={
                "channel": "second_channel", "name": "boop", "response": "привет",
                "cooldown_seconds": 0, "aliases": []})
            self.assertEqual(created.status, 200)
            self.assertIsNotNone(self.panel.db.get_custom("second_channel", "boop"))
            self.assertIsNone(self.panel.db.get_custom("smailrofl", "boop"))
            state = await (await self.client.get("/api/state", headers=owner)).json()
            self.assertEqual([item["channel"] for item in state["commands"]], ["second_channel"])
            self.assertEqual(state["stats"]["second_channel"]["commands"], 1)
            self.assertEqual(state["stats"]["smailrofl"]["commands"], 0)
            timer = await self.client.post("/api/timers", headers=owner, json={
                "channel": "second_channel", "message": "тестовый таймер", "interval_minutes": 5, "enabled": True})
            self.assertEqual(timer.status, 200)
            self.assertEqual([row["id"] for row in self.panel.db.list_timers("smailrofl")], [])
            self.assertEqual(len(self.panel.db.list_timers("second_channel")), 1)
            points = await self.client.post("/api/points", headers=owner, json={
                "channel": "second_channel", "name": "viewer", "amount": 10})
            self.assertEqual(points.status, 200)
            self.assertEqual(self.panel.db.get_points("second_channel", "viewer"), 10)
            self.assertEqual(self.panel.db.get_points("smailrofl", "viewer"), 0)
            word = await self.client.post("/api/banned-words", headers=owner, json={
                "channel": "second_channel", "word": "слово"})
            self.assertEqual(word.status, 200)
            self.assertEqual(self.panel.db.list_banned_words("second_channel"), ["слово"])
            self.assertEqual(self.panel.db.list_banned_words("smailrofl"), [])
            alias = await self.client.post("/api/categories", headers=owner, json={
                "channel": "second_channel", "alias": "valo", "category": "VALORANT"})
            self.assertEqual(alias.status, 200)
            self.assertEqual(state["channels"], list(bot_module.CHANNELS))
            # Изменение и удаление требуют явный канал.
            name = (await created.json())["name"]
            self.assertEqual((await self.client.delete(f"/api/commands/{name}", headers=owner)).status, 400)
            self.assertEqual((await self.client.delete(f"/api/commands/{name}?channel=second_channel", headers=owner)).status, 200)
            self.assertIsNone(self.panel.db.get_custom("second_channel", "boop"))
            self.assertEqual((await self.client.delete(f"/api/banned-words/слово?channel=second_channel", headers=owner)).status, 200)
            self.assertEqual((await self.client.delete(f"/api/categories/valo?channel=second_channel", headers=owner)).status, 200)
            timer_id = (await timer.json())["id"]
            self.assertEqual((await self.client.delete(f"/api/timers/{timer_id}?channel=second_channel", headers=owner)).status, 200)
            self.assertEqual(self.panel.db.list_timers("second_channel"), [])

    async def test_owner_can_remove_member_channel(self):
        member = await self.member(twitch=("5001", "viewer_twitch"))
        owner = await self.login_as()
        await self.connect_channel(member, ("5001", "viewer_twitch"))
        self.assertEqual((await self.client.delete("/api/channels/viewer_twitch", headers=member)).status, 200)
        await self.connect_channel(member, ("5001", "viewer_twitch"))
        state = await (await self.client.get("/api/state", headers=owner)).json()
        self.assertEqual(state["channel_owners"]["viewer_twitch"], "tester")
        self.assertEqual((await self.client.delete("/api/channels/viewer_twitch", headers=owner)).status, 200)

    async def test_member_cannot_remove_or_manage_other_members_channel(self):
        first = await self.member(twitch=("5001", "viewer_twitch"))
        await self.connect_channel(first, ("5001", "viewer_twitch"))
        token = await self.invitation()
        self.assertEqual((await self.register(token, "tester2", "my-personal-password")).status, 200)
        second = await self.login_as("tester2", "my-personal-password")
        self.assertEqual((await self.client.delete("/api/channels/viewer_twitch", headers=second)).status, 403)
        denied = await self.client.put("/api/moderation/viewer_twitch", headers=second, json=self.moderation_payload())
        self.assertEqual(denied.status, 403)

    async def test_member_cannot_control_bot_or_music(self):
        member = await self.member(twitch=("5001", "viewer_twitch"))
        self.panel.runtime.start = AsyncMock(return_value={})
        self.panel.music.reader = AsyncMock(return_value=[])
        for action in ("start", "stop"):
            denied = await self.client.post(f"/api/bot/{action}", headers=member, json={})
            self.assertEqual(denied.status, 403)
            self.assertIn("владелец", (await denied.json())["error"])
        self.panel.runtime.start.assert_not_awaited()
        self.panel.runtime.stop.assert_not_awaited()
        payload = {"enabled": True, "source": "", "hide_paused": True}
        self.assertEqual((await self.client.put("/api/music", headers=member, json=payload)).status, 403)
        self.assertEqual((await self.client.get("/api/music", headers=member)).status, 403)
        state = await (await self.client.get("/api/state", headers=member)).json()
        self.assertIsNone(state["music"])
        # Владелец сохраняет полный доступ к боту и музыке.
        owner = await self.login_as()
        self.assertEqual((await self.client.post("/api/bot/stop", headers=owner, json={})).status, 200)
        self.panel.runtime.stop.assert_awaited()
        allowed = await self.client.put("/api/music", headers=owner, json=payload)
        self.assertEqual(allowed.status, 200)
        self.assertTrue((await allowed.json())["settings"]["enabled"])
        self.assertEqual((await self.client.get("/api/music", headers=owner)).status, 200)

    async def test_member_journal_hides_foreign_chat(self):
        member = await self.member(twitch=("5001", "viewer_twitch"))
        self.panel.runtime.snapshot = lambda: {
            "status": "running", "connected": True, "nick": "bot", "error": "", "started_at": 0,
            "logs": [
                {"time": 1, "level": "info", "message": "[чат] zoil: секрет чужого канала"},
                {"time": 1, "level": "info", "message": "[отправлено] #smailrofl: чужое сообщение"},
                {"time": 1, "level": "info", "message": "[OK] Вошёл в чат #smailrofl"},
            ],
            "channels": ["smailrofl"],
        }
        state = await (await self.client.get("/api/state", headers=member)).json()
        self.assertEqual([entry["message"] for entry in state["runtime"]["logs"]],
                         ["[OK] Вошёл в чат #smailrofl"])
        owner = await self.login_as()
        state = await (await self.client.get("/api/state", headers=owner)).json()
        self.assertEqual(len(state["runtime"]["logs"]), 3)

    async def test_channel_limit(self):
        owner = await self.login_as()
        conn = self.panel.db.conn
        for index in range(25):
            conn.execute("INSERT INTO panel_channels(channel, username, created) VALUES(?,?,?)",
                         (f"extra{index}", "owner", float(index)))
        conn.commit()
        refused = await self.start_flow("channel", owner)
        self.assertEqual(refused.status, 409)
        self.assertIn("лимит", (await refused.json())["error"])


if __name__ == "__main__":
    unittest.main()
