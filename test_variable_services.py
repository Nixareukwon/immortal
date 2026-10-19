"""Offline checks for permission handling and external-variable data sources."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import xml.etree.ElementTree as ET

from variable_services import VariableServices


class VariableServicesTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = SimpleNamespace(
            scopes=set(), moderator_id="10", http_session=None,
            twitch_id_cache={"streamer": "10", "viewer": "20", "other": "30"},
            twitch_api=AsyncMock(), format_elapsed=lambda value: f"{int(value)} сек.",
        )
        self.services = VariableServices(self.bot)

    async def resolve(self, name, argument="", **kwargs):
        context = {"username": "viewer", "channel_name": "streamer"}
        context.update(kwargs)
        return await self.services.resolve(name, argument, **context)

    async def test_follower_count_does_not_require_follower_scope(self):
        self.bot.twitch_api.return_value = {"total": 1234}
        self.assertEqual(await self.resolve("followcount"), "1234")
        self.bot.twitch_api.assert_awaited_once_with(
            "GET", "channels/followers", params={"broadcaster_id": "10", "first": 1}
        )

    async def test_follower_requests_use_filtered_endpoint_and_cache(self):
        self.bot.scopes.add("moderator:read:followers")
        self.bot.twitch_api.return_value = {"data": [{"followed_at": "2020-01-01T00:00:00Z"}]}
        with patch("variable_services.time.time", return_value=1577836860):
            result = await self.resolve("followage", "@other #streamer")
            self.assertEqual(result, "@other зафолловлен на канал streamer уже 60 сек.")
            self.assertEqual(await self.resolve("followage", "other streamer"), result)
        self.bot.twitch_api.assert_awaited_once_with(
            "GET", "channels/followers",
            params={"broadcaster_id": "10", "user_id": "30", "first": 1},
        )

    async def test_missing_follower_scope_is_not_false_unfollow(self):
        result = await self.resolve("followage")
        self.assertIn("moderator:read:followers", result)
        self.assertNotIn("не отслеживает", result)
        self.bot.twitch_api.assert_not_awaited()

    async def test_subcount_uses_broadcaster_endpoint_and_total(self):
        self.bot.scopes.add("channel:read:subscriptions")
        self.bot.twitch_api.return_value = {"total": 42, "points": 84, "data": []}
        self.assertEqual(await self.resolve("subcount"), "42")
        self.bot.twitch_api.assert_awaited_once_with(
            "GET", "subscriptions", params={"broadcaster_id": "10", "first": 1}
        )

    async def test_subcount_rejects_moderator_token(self):
        self.bot.scopes.add("channel:read:subscriptions")
        self.bot.moderator_id = "99"
        self.assertIn("токен владельца", await self.resolve("subcount"))
        self.bot.twitch_api.assert_not_awaited()

    async def test_subage_uses_authoritative_badge_months(self):
        self.services._public = AsyncMock()
        self.assertEqual(
            await self.resolve("subage", tags={"badge-info": "subscriber/23"}), "23 месяца"
        )
        self.services._public.assert_not_awaited()

    async def test_subage_for_other_user_does_not_reuse_author_badge(self):
        self.services._public = AsyncMock(return_value={
            "statusHidden": False, "cumulative": {"months": 12},
        })
        self.assertEqual(
            await self.resolve("subage", "other", tags={"badge-info": "subscriber/23"}),
            "12 месяцев",
        )
        self.services._public.assert_awaited_once_with(
            "https://api.ivr.fi/v2/twitch/subage/other/streamer"
        )

    async def test_subage_hidden_status_remains_hidden(self):
        self.services._public = AsyncMock(return_value={"statusHidden": True, "cumulative": None})
        self.assertIn("скрыл", await self.resolve("subage"))

    async def test_account_age_uses_created_timestamp(self):
        self.bot.twitch_api.return_value = {
            "data": [{"id": "99", "created_at": "2020-01-01T00:00:00Z"}],
        }
        with patch("variable_services.time.time", return_value=1577836920):
            self.assertEqual(await self.resolve("accountage", "newuser"), "120 сек.")
        self.assertEqual(self.bot.twitch_id_cache["newuser"], "99")

    async def test_actual_horoscope_feed_shared_between_signs(self):
        today = datetime.now(timezone(timedelta(hours=3))).strftime("%d.%m.%Y")
        root = ET.fromstring(
            f'<horo><date today="{today}"/><aries><today>Текст Овна.</today></aries>'
            '<taurus><today>Текст Тельца.</today></taurus></horo>'
        )
        self.services._public = AsyncMock(return_value=root)
        self.assertEqual(await self.resolve("horoscope", "Овен"), "Овен: Текст Овна. (ignio.com)")
        self.assertEqual(await self.resolve("horoscope", "taurus"), "Телец: Текст Тельца. (ignio.com)")
        self.services._public.assert_awaited_once()

    async def test_stale_horoscope_is_not_presented_as_todays(self):
        root = ET.fromstring('<horo><date today="01.01.2000"/><aries><today>Старое.</today></aries></horo>')
        self.services._public = AsyncMock(return_value=root)
        self.assertIn("ещё не опубликован", await self.resolve("horoscope", "овен"))

    async def test_horoscope_failure_not_refetched_more_than_hourly(self):
        self.services._public = AsyncMock(side_effect=asyncio.TimeoutError)
        self.assertIn("недоступны", await self.resolve("horoscope", "овен"))
        self.assertIn("недоступны", await self.resolve("horoscope", "телец"))
        self.services._public.assert_awaited_once()

    async def test_offline_uptime_uses_archive_end_not_start(self):
        self.bot.twitch_api.side_effect = [
            {"data": []},
            {"data": [{"created_at": "2020-01-01T00:00:00Z", "duration": "1h20m30s"}]},
        ]
        with patch("variable_services.time.time", return_value=1577845230):
            self.assertEqual(
                await self.resolve("uptime"),
                "Стрим офлайн; с конца прошлого стрима прошло 3600 сек.",
            )

    async def test_online_uptime_tracks_category_change(self):
        stream = {"id": "1", "game_id": "a", "game_name": "Minecraft",
                  "started_at": "2020-01-01T00:00:00Z"}
        with patch("variable_services.time.time", return_value=1577836800):
            self.services.observe_stream("streamer", stream)
        changed = {**stream, "game_id": "b", "game_name": "Portal"}
        with patch("variable_services.time.time", return_value=1577836860):
            self.services.observe_stream("streamer", changed)
        self.bot.twitch_api.return_value = {"data": [changed]}
        with patch("variable_services.time.time", return_value=1577836920):
            self.assertEqual(
                await self.resolve("uptime"), "Стрим идёт 120 сек. | Играем в Portal уже 60 сек."
            )

    async def test_observed_stream_end_works_without_archive_access(self):
        stream = {"id": "1", "started_at": "2020-01-01T00:00:00Z"}
        with patch("variable_services.time.time", return_value=1577836800):
            self.services.observe_stream("streamer", stream)
        with patch("variable_services.time.time", return_value=1577836860):
            self.services.observe_stream("streamer", None)
        self.bot.twitch_api.return_value = {"data": []}
        with patch("variable_services.time.time", return_value=1577836920):
            self.assertEqual(
                await self.resolve("uptime"),
                "Стрим офлайн; с конца прошлого стрима прошло примерно 60 сек.",
            )
        self.bot.twitch_api.assert_awaited_once_with(
            "GET", "streams", params={"user_login": "streamer"}
        )

    async def test_timer_context_without_sender_allows_channel_variables(self):
        self.bot.twitch_api.return_value = {"total": 50}
        self.assertEqual(await self.resolve("followcount", username=""), "50")

    async def test_network_timeout_has_a_bounded_chat_response(self):
        self.services.RESOLVE_TIMEOUT = 0.01
        blocker = asyncio.Event()

        async def wait_forever(*args, **kwargs):
            await blocker.wait()

        self.bot.twitch_api.side_effect = wait_forever
        self.assertIn("недоступны", await self.resolve("emote"))


if __name__ == "__main__":
    unittest.main()
