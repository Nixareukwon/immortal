"""Current weather for command variables, using Open-Meteo and a shared session."""

import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import logging
import math
import time

import aiohttp


log = logging.getLogger(__name__)


class _WeatherUnavailable(RuntimeError):
    pass


class _CityNotFound(_WeatherUnavailable):
    pass


@dataclass
class _CityLock:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


@dataclass
class _WeatherResult:
    text: str
    fetched_at: float
    observation: str = ""


class WeatherService:
    GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
    FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
    WEATHER_TTL = 600
    GEOCODING_TTL = 86400
    STALE_TTL = 3600
    FAILURE_TTL = 30
    NOT_FOUND_TTL = 60
    CACHE_LIMIT = 256
    TOTAL_TIMEOUT = 14
    REQUEST_TIMEOUT = 4
    MAX_ATTEMPTS = 2
    RETRY_DELAY = 0.3
    MAX_RETRY_DELAY = 1.5
    CURRENT_FIELDS = (
        "temperature_2m,relative_humidity_2m,apparent_temperature,"
        "weather_code,wind_speed_10m"
    )
    DESCRIPTIONS = {
        0: "ясно", 1: "преимущественно ясно", 2: "переменная облачность",
        3: "пасмурно", 45: "туман", 48: "изморозь",
        51: "лёгкая морось", 53: "морось", 55: "сильная морось",
        56: "лёгкая ледяная морось", 57: "сильная ледяная морось",
        61: "небольшой дождь", 63: "дождь", 65: "сильный дождь",
        66: "небольшой ледяной дождь", 67: "сильный ледяной дождь",
        71: "небольшой снег", 73: "снег", 75: "сильный снег", 77: "снежная крупа",
        80: "небольшой ливень", 81: "ливень", 82: "очень сильный ливень",
        85: "небольшой снежный ливень", 86: "сильный снежный ливень",
        95: "гроза", 96: "гроза с небольшим градом", 97: "сильная гроза",
        99: "гроза с сильным градом",
    }

    def __init__(self, bot):
        self.bot = bot
        self.weather_cache = OrderedDict()
        self.geocoding_cache = OrderedDict()
        self.failure_cache = OrderedDict()
        self._city_locks = {}

    def _remember(self, cache, key, value):
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > self.CACHE_LIMIT:
            cache.popitem(last=False)

    def _fresh_weather(self, key):
        cached = self.weather_cache.get(key)
        if cached is not None:
            age = time.monotonic() - cached.fetched_at
            if age < self.WEATHER_TTL:
                self.weather_cache.move_to_end(key)
                return cached.text
            if age > self.STALE_TTL:
                del self.weather_cache[key]
        return None

    def _recent_failure(self, key):
        cached = self.failure_cache.get(key)
        if cached is not None:
            expires_at, message = cached
            if time.monotonic() < expires_at:
                self.failure_cache.move_to_end(key)
                return message
            del self.failure_cache[key]
        return None

    def _unavailable(self, city, key, *, not_found=False):
        now = time.monotonic()
        cached = self.weather_cache.get(key)
        stale_deadline = None
        if cached is not None and now - cached.fetched_at <= self.STALE_TTL:
            stale_deadline = cached.fetched_at + self.STALE_TTL
            age = max(1, int((now - cached.fetched_at) // 60))
            observation = f", данные на {cached.observation}" if cached.observation else ""
            result = (f"{cached.text} (кеш: {age} мин. назад{observation}; "
                      "обновить не удалось)")
        elif not_found:
            result = f"город «{city}» не найден; уточни название или страну"
        else:
            result = f"погода для города «{city}» временно недоступна; попробуй позже"
        ttl = self.NOT_FOUND_TTL if not_found else self.FAILURE_TTL
        expires_at = min(now + ttl, stale_deadline) if stale_deadline is not None else now + ttl
        self._remember(self.failure_cache, key, (expires_at, result))
        return result

    async def get_weather(self, city):
        city = " ".join(city.split()) if isinstance(city, str) else ""
        if not city or len(city) > 100:
            return "укажи город: {weather город}"
        key = city.casefold()
        fresh = self._fresh_weather(key)
        if fresh is not None:
            return fresh
        failure = self._recent_failure(key)
        if failure is not None:
            return failure
        try:
            # The timeout includes waiting for a concurrent request for this city.
            return await asyncio.wait_for(
                self._get_locked(city, key), timeout=self.TOTAL_TIMEOUT
            )
        except asyncio.TimeoutError:
            log.warning("Истекло время получения погоды для города %s", city)
            return self._unavailable(city, key)

    async def _get_locked(self, city, key):
        entry = self._city_locks.setdefault(key, _CityLock())
        entry.users += 1
        try:
            async with entry.lock:
                fresh = self._fresh_weather(key)
                if fresh is not None:
                    return fresh
                failure = self._recent_failure(key)
                if failure is not None:
                    return failure
                try:
                    location = await self._location(city, key)
                    forecast = await self._request_json(self.FORECAST_URL, {
                        "latitude": location["latitude"],
                        "longitude": location["longitude"],
                        "current": self.CURRENT_FIELDS,
                        "temperature_unit": "celsius", "wind_speed_unit": "kmh",
                        "timezone": "auto",
                    })
                    current = forecast.get("current")
                    if not isinstance(current, dict):
                        raise _WeatherUnavailable("отсутствуют текущие погодные данные")
                    result = _WeatherResult(
                        self._format_weather(location, current), time.monotonic(),
                        self._observation(current.get("time")),
                    )
                    self._remember(self.weather_cache, key, result)
                    self.failure_cache.pop(key, None)
                    return result.text
                except _CityNotFound:
                    return self._unavailable(city, key, not_found=True)
                except (aiohttp.ClientError, asyncio.TimeoutError, _WeatherUnavailable,
                        KeyError, ValueError, TypeError, AttributeError, RuntimeError):
                    log.warning("Не удалось обновить погоду для города %s", city)
                    return self._unavailable(city, key)
        finally:
            entry.users -= 1
            if entry.users == 0:
                self._city_locks.pop(key, None)

    async def _location(self, city, key):
        cached = self.geocoding_cache.get(key)
        if cached is not None:
            fetched_at, location = cached
            if time.monotonic() - fetched_at < self.GEOCODING_TTL:
                self.geocoding_cache.move_to_end(key)
                return location
            del self.geocoding_cache[key]
        response = await self._request_json(self.GEOCODING_URL, {
            "name": city, "count": 1, "language": "ru", "format": "json",
        })
        locations = response.get("results", [])
        if not isinstance(locations, list):
            raise _WeatherUnavailable("некорректный ответ геокодинга")
        if not locations:
            raise _CityNotFound(city)
        location = locations[0]
        if not isinstance(location, dict) or not isinstance(location.get("name"), str):
            raise _WeatherUnavailable("некорректное название города")
        if not location["name"].strip():
            raise _WeatherUnavailable("отсутствует название города")
        latitude = self._number(location.get("latitude"))
        longitude = self._number(location.get("longitude"))
        if latitude is None or longitude is None or not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
            raise _WeatherUnavailable("некорректные координаты города")
        self._remember(self.geocoding_cache, key, (time.monotonic(), location))
        return location

    def _retry_after(self, value):
        if not value:
            return self.RETRY_DELAY
        try:
            delay = float(value)
        except (TypeError, ValueError):
            try:
                retry_at = parsedate_to_datetime(value)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                delay = (retry_at - datetime.now(timezone.utc)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                return self.RETRY_DELAY
        # Long Retry-After values must not be shortened into an early retry.
        if not math.isfinite(delay) or delay > self.MAX_RETRY_DELAY:
            return None
        return max(0, delay)

    async def _request_json(self, url, params):
        if self.bot.http_session is None or self.bot.http_session.closed:
            self.bot.http_session = aiohttp.ClientSession()
        for attempt in range(self.MAX_ATTEMPTS):
            delay = self.RETRY_DELAY
            try:
                async with self.bot.http_session.get(
                    url, params=params,
                    timeout=aiohttp.ClientTimeout(total=self.REQUEST_TIMEOUT),
                ) as response:
                    if response.status >= 400:
                        retryable = response.status == 429 or 500 <= response.status < 600
                        delay = self._retry_after(response.headers.get("Retry-After"))
                        if not retryable or attempt + 1 == self.MAX_ATTEMPTS or delay is None:
                            raise _WeatherUnavailable("ошибка погодного сервиса")
                    else:
                        result = await response.json(content_type=None)
                        if not isinstance(result, dict):
                            raise _WeatherUnavailable("некорректный ответ погодного сервиса")
                        if result.get("error"):
                            overloaded = "overload" in str(result.get("reason", "")).casefold()
                            if not overloaded or attempt + 1 == self.MAX_ATTEMPTS:
                                raise _WeatherUnavailable("ошибка погодного сервиса")
                        else:
                            return result
            except (aiohttp.ClientError, asyncio.TimeoutError):
                if attempt + 1 == self.MAX_ATTEMPTS:
                    raise
            if attempt + 1 < self.MAX_ATTEMPTS:
                await asyncio.sleep(delay)
        raise _WeatherUnavailable("погодный сервис недоступен")

    @staticmethod
    def _number(value):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            try:
                if math.isfinite(value):
                    return value
            except (OverflowError, TypeError):
                pass
        return None

    @staticmethod
    def _temperature(value):
        rounded = round(value)
        return f"{rounded:+d}°C" if rounded else "0°C"

    @staticmethod
    def _observation(value):
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%H:%M")
            except ValueError:
                pass
        return ""

    def _format_weather(self, location, current):
        temperature = self._number(current.get("temperature_2m"))
        if temperature is None:
            raise _WeatherUnavailable("нет температуры для этого города")
        apparent = self._number(current.get("apparent_temperature"))
        humidity = self._number(current.get("relative_humidity_2m"))
        wind = self._number(current.get("wind_speed_10m"))
        code = self._number(current.get("weather_code"))
        description = self.DESCRIPTIONS.get(code, "погодные условия неизвестны")
        temperature_text = self._temperature(temperature)
        if apparent is not None:
            temperature_text += f" (ощущается как {self._temperature(apparent)})"
        parts = [description, temperature_text]
        if humidity is not None and 0 <= humidity <= 100:
            parts.append(f"влажность {humidity:.0f}%")
        if wind is not None and wind >= 0:
            parts.append(f"ветер {wind:.0f} км/ч")
        place = " ".join(location["name"].split())
        country = location.get("country")
        if isinstance(country, str) and country.strip():
            place += ", " + " ".join(country.split())
        return f"{place}: {', '.join(parts)}"
