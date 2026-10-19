"""Offline weather checks: provider failures, nullable data, caching and cancellation."""

import asyncio
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

import aiohttp

from weather_service import WeatherService


class FakeResponse:
    def __init__(self, payload=None, *, status=200, headers=None):
        self.status = status
        self.headers = headers or {}
        self.json = AsyncMock(return_value=payload)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class WeatherServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.location = {
            "name": "Рига", "country": "Латвия", "latitude": 56.946,
            "longitude": 24.10589,
        }
        self.current = {
            "time": "2026-10-17T12:15", "temperature_2m": 8.2,
            "apparent_temperature": 5.1, "relative_humidity_2m": 79,
            "weather_code": 3, "wind_speed_10m": 13.8,
        }
        self.session = SimpleNamespace(closed=False, get=Mock())
        self.bot = SimpleNamespace(http_session=self.session)
        self.service = WeatherService(self.bot)
        self.service.RETRY_DELAY = 0.001

    def successful_responses(self, *, current=None):
        return [
            FakeResponse({"results": [self.location]}),
            FakeResponse({"current": self.current if current is None else current}),
        ]

    async def populate(self):
        self.session.get.side_effect = self.successful_responses()
        return await self.service.get_weather("Riga")

    async def test_current_weather_format_and_explicit_units(self):
        result = await self.populate()
        self.assertEqual(
            result,
            "Рига, Латвия: пасмурно, +8°C (ощущается как +5°C), влажность 79%, ветер 14 км/ч",
        )
        params = self.session.get.call_args_list[1].kwargs["params"]
        self.assertEqual(params["wind_speed_unit"], "kmh")
        self.assertEqual(params["temperature_unit"], "celsius")
        self.assertNotIn("daily", params)

    async def test_normalized_case_and_whitespace_share_weather_cache(self):
        first = await self.populate()
        self.assertEqual(await self.service.get_weather("  RIGA \n "), first)
        self.assertEqual(self.session.get.call_count, 2)

    async def test_multiword_city_and_country_are_forwarded_whole(self):
        self.session.get.side_effect = self.successful_responses()
        await self.service.get_weather("  Нижний   Новгород, RU  ")
        params = self.session.get.call_args_list[0].kwargs["params"]
        self.assertEqual(params["name"], "Нижний Новгород, RU")

    async def test_geocoding_is_cached_longer_than_current_weather(self):
        await self.populate()
        self.service.weather_cache["riga"].fetched_at -= self.service.WEATHER_TTL + 1
        self.session.get.side_effect = [FakeResponse({"current": self.current})]
        result = await self.service.get_weather("Riga")
        self.assertIn("+8°C", result)
        self.assertEqual(self.session.get.call_count, 3)
        self.assertEqual(self.session.get.call_args.args[0], self.service.FORECAST_URL)

    async def test_nullable_optional_fields_are_omitted(self):
        current = {"temperature_2m": 0, "weather_code": 77,
                   "apparent_temperature": None, "wind_speed_10m": None}
        self.session.get.side_effect = self.successful_responses(current=current)
        self.assertEqual(
            await self.service.get_weather("Riga"), "Рига, Латвия: снежная крупа, 0°C"
        )

    async def test_missing_primary_temperature_is_friendly_error(self):
        self.session.get.side_effect = self.successful_responses(current={"temperature_2m": None})
        result = await self.service.get_weather("Riga")
        self.assertIn("временно недоступна", result)
        self.assertNotIn("None", result)

    async def test_invalid_numeric_optional_values_are_omitted(self):
        current = {**self.current, "apparent_temperature": float("nan"),
                   "relative_humidity_2m": 101, "wind_speed_10m": -1}
        self.session.get.side_effect = self.successful_responses(current=current)
        self.assertEqual(await self.service.get_weather("Riga"), "Рига, Латвия: пасмурно, +8°C")

    async def test_extremely_large_numeric_value_is_not_formatted(self):
        current = {**self.current, "wind_speed_10m": 10 ** 1000}
        self.session.get.side_effect = self.successful_responses(current=current)
        result = await self.service.get_weather("Riga")
        self.assertIn("+8°C", result)
        self.assertNotIn("ветер", result)

    async def test_zero_humidity_and_wind_are_not_missing(self):
        current = {**self.current, "temperature_2m": -0.1,
                   "relative_humidity_2m": 0, "wind_speed_10m": 0}
        self.session.get.side_effect = self.successful_responses(current=current)
        result = await self.service.get_weather("Riga")
        self.assertIn("0°C", result)
        self.assertNotIn("-0°C", result)
        self.assertIn("влажность 0%", result)
        self.assertIn("ветер 0 км/ч", result)

    async def test_all_documented_weather_codes_have_descriptions(self):
        for code in (0, 1, 2, 3, 45, 48, 51, 53, 55, 56, 57, 61, 63, 65,
                     66, 67, 71, 73, 75, 77, 80, 81, 82, 85, 86, 95, 96, 97, 99):
            with self.subTest(code=code):
                result = self.service._format_weather(self.location, {**self.current, "weather_code": code})
                self.assertNotIn("неизвестны", result)

    async def test_overload_retries_once_and_marks_stale_weather(self):
        first = await self.populate()
        self.service.weather_cache["riga"].fetched_at -= 601
        self.session.get.side_effect = [FakeResponse(status=503), FakeResponse(status=503)]
        result = await self.service.get_weather("Riga")
        self.assertTrue(result.startswith(first))
        self.assertIn("кеш: 10 мин. назад", result)
        self.assertIn("данные на 12:15", result)
        self.assertIn("обновить не удалось", result)
        self.assertEqual(self.session.get.call_count, 4)
        self.assertEqual(await self.service.get_weather("Riga"), result)
        self.assertEqual(self.session.get.call_count, 4)

    async def test_weather_older_than_hour_is_not_returned(self):
        await self.populate()
        self.service.weather_cache["riga"].fetched_at -= 3601
        self.session.get.side_effect = [FakeResponse(status=503), FakeResponse(status=503)]
        result = await self.service.get_weather("Riga")
        self.assertIn("временно недоступна", result)
        self.assertNotIn("+8°C", result)

    async def test_cached_failure_expires_before_stale_data_becomes_too_old(self):
        await self.populate()
        self.service.weather_cache["riga"].fetched_at -= 3590
        self.session.get.side_effect = [FakeResponse(status=503), FakeResponse(status=503)]
        self.assertIn("кеш", await self.service.get_weather("Riga"))
        expires, _ = self.service.failure_cache["riga"]
        hard_deadline = self.service.weather_cache["riga"].fetched_at + self.service.STALE_TTL
        self.assertLessEqual(expires, hard_deadline)

    async def test_short_retry_after_can_recover(self):
        self.session.get.side_effect = [
            FakeResponse(status=429, headers={"Retry-After": "0"}),
            *self.successful_responses(),
        ]
        self.assertIn("+8°C", await self.service.get_weather("Riga"))
        self.assertEqual(self.session.get.call_count, 3)

    async def test_long_retry_after_is_not_ignored(self):
        self.session.get.side_effect = [FakeResponse(status=429, headers={"Retry-After": "120"})]
        self.assertIn("временно недоступна", await self.service.get_weather("Riga"))
        self.assertEqual(self.session.get.call_count, 1)

    async def test_nonretryable_http_error_is_not_repeated(self):
        self.session.get.side_effect = [FakeResponse(status=400)]
        self.assertIn("временно недоступна", await self.service.get_weather("Riga"))
        self.assertEqual(self.session.get.call_count, 1)

    async def test_json_error_payload_is_not_formatted_as_weather(self):
        self.session.get.side_effect = [FakeResponse({"error": True, "reason": "invalid parameter"})]
        self.assertIn("временно недоступна", await self.service.get_weather("Riga"))
        self.assertEqual(self.session.get.call_count, 1)

    async def test_json_overload_payload_retries_once(self):
        self.session.get.side_effect = [
            FakeResponse({"error": True, "reason": "The service is overloaded"}),
            *self.successful_responses(),
        ]
        self.assertIn("+8°C", await self.service.get_weather("Riga"))
        self.assertEqual(self.session.get.call_count, 3)

    async def test_network_failure_is_retried_once(self):
        self.session.get.side_effect = [aiohttp.ClientConnectionError(), *self.successful_responses()]
        self.assertIn("+8°C", await self.service.get_weather("Riga"))
        self.assertEqual(self.session.get.call_count, 3)

    async def test_missing_city_is_cached_and_distinguished_from_service_failure(self):
        self.session.get.side_effect = [FakeResponse({})]
        result = await self.service.get_weather("Unknown")
        self.assertIn("не найден", result)
        self.assertEqual(await self.service.get_weather("unknown"), result)
        self.assertEqual(self.session.get.call_count, 1)

    async def test_empty_and_oversized_city_do_not_contact_provider(self):
        for city in ("", " \n ", "x" * 101, None):
            with self.subTest(city=city):
                self.assertIn("укажи город", await self.service.get_weather(city))
        self.session.get.assert_not_called()

    async def test_concurrent_requests_share_one_provider_fetch(self):
        async def fetch(url, params):
            await asyncio.sleep(0.01)
            return {"results": [self.location]} if url == self.service.GEOCODING_URL else {"current": self.current}

        self.service._request_json = AsyncMock(side_effect=fetch)
        first, second = await asyncio.gather(
            self.service.get_weather("Riga"), self.service.get_weather("RIGA")
        )
        self.assertEqual(first, second)
        self.assertEqual(self.service._request_json.await_count, 2)
        self.assertFalse(self.service._city_locks)

    async def test_cancelled_holder_and_waiter_leave_no_locks_or_background_tasks(self):
        entered = asyncio.Event()
        blocked = asyncio.Event()

        async def fetch(url, params):
            entered.set()
            await blocked.wait()

        self.service._request_json = AsyncMock(side_effect=fetch)
        holder = asyncio.create_task(self.service.get_weather("Riga"))
        await entered.wait()
        waiter = asyncio.create_task(self.service.get_weather("RIGA"))
        await asyncio.sleep(0)
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        holder.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await holder
        self.assertFalse(self.service._city_locks)
        self.assertEqual(self.service._request_json.await_count, 1)

    async def test_whole_operation_timeout_releases_city_lock(self):
        self.service.TOTAL_TIMEOUT = 0.01
        blocker = asyncio.Event()

        async def fetch(*args):
            await blocker.wait()

        self.service._request_json = AsyncMock(side_effect=fetch)
        result = await self.service.get_weather("Riga")
        self.assertIn("временно недоступна", result)
        self.assertFalse(self.service._city_locks)

    async def test_caches_have_bounded_size(self):
        self.service.CACHE_LIMIT = 2

        async def fetch(url, params):
            return {"results": [self.location]} if url == self.service.GEOCODING_URL else {"current": self.current}

        self.service._request_json = AsyncMock(side_effect=fetch)
        for city in ("Riga", "Berlin", "Paris"):
            await self.service.get_weather(city)
        self.assertEqual(len(self.service.weather_cache), 2)
        self.assertEqual(len(self.service.geocoding_cache), 2)
        self.assertNotIn("riga", self.service.weather_cache)


if __name__ == "__main__":
    unittest.main()
