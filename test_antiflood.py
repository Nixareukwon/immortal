"""Offline regressions for false positives and actual antiflood violations."""

import asyncio
import importlib
import os
import unittest
from collections import deque
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock, patch


# Do not load credentials, open the live database, or connect to Twitch.
with patch("dotenv.load_dotenv"), patch.dict(os.environ, {}, clear=True):
    bot_module = importlib.import_module("bot")
    legacy_module = importlib.import_module("smailrofltwitchbot")


class AntifloodTests(unittest.IsolatedAsyncioTestCase):
    module = bot_module

    def setUp(self):
        self.settings = {**self.module.ANTIFLOOD_DEFAULTS, "enabled": True}
        self.bot = SimpleNamespace(
            nick="robot",
            permits={},
            antiflood_history={},
            antiflood_violations={},
            recent_messages=deque(),
            antiflood_settings=lambda channel: self.settings,
            visual_message_size=self.module.Bot.visual_message_size,
            moderate_user=AsyncMock(),
            delete_chat_message=AsyncMock(),
        )
        self.bot.handle_antiflood = MethodType(self.module.Bot.handle_antiflood, self.bot)
        log_patch = patch(f"{self.module.__name__}.log")
        log_patch.start()
        self.addCleanup(log_patch.stop)

    async def check_message(self, content, *, username="viewer", channel="streamer", at=1000):
        message = SimpleNamespace(
            content=content,
            author=SimpleNamespace(name=username),
            channel=SimpleNamespace(name=channel),
        )
        with patch(f"{self.module.__name__}.time.time", return_value=at):
            return await self.bot.handle_antiflood(message, channel, username, False)

    async def test_conversation_from_screenshots_is_allowed(self):
        messages = (
            ("rostiics", "Смайл", 1000),
            ("rostiics", "Дай денег на комп с видюхой 1080", 1001),
            ("eighteeen_18", "Серв новый?", 1002),
            ("rostiics", "Чуть чуть не хватает", 1003),
            ("lastf1re_", "это Лёха так болтает?", 1004),
            ("shampooo", "демагогия", 1005),
            ("shampooo", "Что такое демогогия", 1006),
            ("lastf1re_", "салам салам", 1007),
        )
        for username, content, at in messages:
            with self.subTest(content=content):
                self.assertFalse(await self.check_message(content, username=username, at=at))
        self.bot.moderate_user.assert_not_awaited()
        self.assertEqual(self.bot.antiflood_violations, {})

    async def test_natural_repetition_and_word_fragments_are_allowed(self):
        for index, content in enumerate((
            "салам салам",
            "нет нет нет, всё нормально",
            "добрый день добрый день",
            "привет вет вет вет",
            "вет вет вет ветер",
        )):
            with self.subTest(content=content):
                self.assertFalse(await self.check_message(content, username=f"viewer{index}"))
        self.bot.moderate_user.assert_not_awaited()

    async def test_four_word_or_phrase_copies_are_muted(self):
        for index, content in enumerate((
            "спам спам спам спам",
            "спам спам спам спам!",
            "купи сейчас купи сейчас купи сейчас купи сейчас",
            "текст спам спам спам спам продолжение",
            "Спам\tспам  спам\nспам",
        )):
            username = f"viewer{index}"
            with self.subTest(content=content):
                self.bot.moderate_user.reset_mock()
                self.assertTrue(await self.check_message(content, username=username))
                self.bot.moderate_user.assert_awaited_once_with(
                    "streamer", username, duration=10, reason="Антифлуд: повтор фразы"
                )

    async def test_fourth_identical_message_within_five_seconds_is_muted(self):
        self.assertFalse(await self.check_message("привет", at=1000))
        self.assertFalse(await self.check_message("Привет", at=1001))
        self.assertFalse(await self.check_message("  привет  ", at=1002))
        self.assertTrue(await self.check_message("привет", at=1003))
        self.bot.moderate_user.assert_awaited_once_with(
            "streamer", "viewer", duration=10,
            reason="Антифлуд: повтор одинакового сообщения",
        )

    async def test_old_messages_do_not_count_as_rapid_repetition(self):
        for at in (1000, 1006, 1012):
            self.assertFalse(await self.check_message("привет", at=at))
        self.bot.moderate_user.assert_not_awaited()

    async def test_repetition_history_is_separate_for_each_user_and_channel(self):
        for channel in ("first", "second"):
            for username in ("alice", "bob"):
                for at in (1000, 1001):
                    self.assertFalse(await self.check_message(
                        "привет", username=username, channel=channel, at=at
                    ))
        self.bot.moderate_user.assert_not_awaited()

    async def test_message_rate_limit_still_applies(self):
        for index in range(4):
            self.assertFalse(await self.check_message(f"сообщение {index}"))
        self.assertTrue(await self.check_message("пятое сообщение"))
        self.bot.moderate_user.assert_awaited_once_with(
            "streamer", "viewer", duration=10,
            reason="Антифлуд: слишком много сообщений за короткое время",
        )

    async def test_screenshot_single_caps_message_is_not_muted_in_any_mode(self):
        for mode in (1, 2, 3):
            self.settings["mode"] = mode
            self.assertFalse(await self.check_message(
                "СУКА У МЕНЯ 3К БЫЛО ТЕПЕРЬ КОСАРЬ", username=f"viewer{mode}"
            ))
        self.bot.moderate_user.assert_not_awaited()

    async def test_third_caps_message_in_ten_seconds_is_muted(self):
        for text, at in (("ВОТ ЭТО СЕЙЧАС БЫЛО КРУТО", 1000),
                         ("Я ТАКОГО ВООБЩЕ НЕ ОЖИДАЛ", 1003)):
            self.assertFalse(await self.check_message(text, at=at))
        self.assertTrue(await self.check_message("ДАВАЙТЕ ПОПРОБУЕМ ЕЩЁ РАЗ", at=1006))
        self.bot.moderate_user.assert_awaited_once_with(
            "streamer", "viewer", duration=10, reason="Антифлуд: слишком много заглавных букв"
        )
        self.assertFalse(self.bot.antiflood_caps)

    async def test_spaced_caps_messages_do_not_accumulate(self):
        for text, at in (("ВОТ ЭТО СЕЙЧАС БЫЛО КРУТО", 1000),
                         ("Я ТАКОГО ВООБЩЕ НЕ ОЖИДАЛ", 1006),
                         ("ДАВАЙТЕ ПОПРОБУЕМ ЕЩЁ РАЗ", 1012)):
            self.assertFalse(await self.check_message(text, at=at))
        self.bot.moderate_user.assert_not_awaited()

    async def test_caps_history_is_per_user_and_channel(self):
        for channel in ("first", "second"):
            for username in ("alice", "bob"):
                for at in (1000, 1001):
                    self.assertFalse(await self.check_message(
                        "ВОТ ЭТО СЕЙЧАС БЫЛО КРУТО", username=username, channel=channel, at=at
                    ))
        self.bot.moderate_user.assert_not_awaited()

    async def test_mixed_case_and_short_exclamations_are_not_caps_flood(self):
        for index, text in enumerate(("ДА!", "УРА!", "ОГО!", "ВОТ ЭТО ДА, но бывает и такое")):
            self.assertFalse(await self.check_message(text, at=1000 + index * 2))
        self.bot.moderate_user.assert_not_awaited()

    async def test_two_or_three_long_messages_do_not_trigger_repeat_mute(self):
        text = "Привет, сегодня наконец получилось зайти на трансляцию."
        self.assertGreater(self.bot.visual_message_size(text), self.settings["main_size"])
        for at in (1000, 1001, 1002):
            self.assertFalse(await self.check_message(text, at=at))
        self.bot.moderate_user.assert_not_awaited()

    async def test_four_long_copies_within_twelve_seconds_are_spam(self):
        text = "Привет, сегодня наконец получилось зайти на трансляцию."
        for at in (1000, 1003, 1006):
            self.assertFalse(await self.check_message(text, at=at))
        self.assertTrue(await self.check_message(text, at=1009))

    async def test_failed_timeout_does_not_escalate_next_penalty(self):
        self.bot.moderate_user.side_effect = RuntimeError("unavailable")
        await self.check_message("спам спам спам спам")
        self.bot.moderate_user.side_effect = None
        await self.check_message("спам спам спам спам", at=1001)
        self.assertEqual(self.bot.moderate_user.await_args.kwargs["duration"], 10)

    async def test_successful_timeout_clears_old_repetition_history(self):
        text = "Привет, сегодня наконец получилось зайти на трансляцию."
        for at in (1000, 1001, 1002, 1003):
            await self.check_message(text, at=at)
        self.assertEqual(self.bot.antiflood_history[("streamer", "viewer")], deque())
        self.assertFalse(await self.check_message(text, at=1013))

    async def test_deletion_failure_does_not_prevent_timeout(self):
        self.bot.delete_chat_message.side_effect = RuntimeError("no permission")
        await self.check_message("https://example.com/" + "a" * 130)
        self.bot.moderate_user.assert_awaited_once()

    async def test_concurrent_violations_issue_one_timeout(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow_timeout(*args, **kwargs):
            entered.set()
            await release.wait()

        self.bot.moderate_user.side_effect = slow_timeout
        first = asyncio.create_task(self.check_message("спам спам спам спам"))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            self.assertTrue(await self.check_message("спам спам спам спам"))
        finally:
            release.set()
            await first
        self.bot.moderate_user.assert_awaited_once()
        self.assertFalse(self.bot.antiflood_pending)


class LegacyAntifloodTests(AntifloodTests):
    module = legacy_module


if __name__ == "__main__":
    unittest.main()
