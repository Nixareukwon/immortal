"""Network-backed command variables, using the bot's shared HTTP connection.

Twitch Helix is used where it exposes the requested information. Subscription
duration comes from IVR's public API (Helix does not expose subscription dates),
with IRC subscription badges as a fallback for the message author. Horoscopes
come from Ignio's published XML feed, fetched at most once per hour.
"""

import asyncio
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
import html
import json
import logging
import random
import re
import time
import xml.etree.ElementTree as ET

import aiohttp


log = logging.getLogger(__name__)


class VariableServices:
    NAMES = frozenset({
        "accountage", "emote", "followage", "followcount", "horoscope",
        "subage", "subcount", "uptime",
    })
    REQUEST_TIMEOUT = 8
    RESOLVE_TIMEOUT = 15
    CACHE_LIMIT = 512
    HOROSCOPE_URL = "https://ignio.com/r/export/utf/xml/daily/com.xml"
    SUBAGE_URL = "https://api.ivr.fi/v2/twitch/subage/{user}/{channel}"
    SIGNS = {
        "aries": "Овен", "taurus": "Телец", "gemini": "Близнецы",
        "cancer": "Рак", "leo": "Лев", "virgo": "Дева", "libra": "Весы",
        "scorpio": "Скорпион", "sagittarius": "Стрелец", "capricorn": "Козерог",
        "aquarius": "Водолей", "pisces": "Рыбы",
    }

    def __init__(self, bot):
        self.bot = bot
        self._cache = OrderedDict()
        self._streams = OrderedDict()
        self._horoscope_lock = asyncio.Lock()

    @staticmethod
    def _login(value):
        login = str(value).strip().lstrip("@#").lower()
        if not re.fullmatch(r"[a-z0-9_]{1,25}", login, flags=re.ASCII):
            raise ValueError("укажи корректный логин Twitch")
        return login

    def _get_cached(self, key):
        cached = self._cache.get(key)
        if cached is None:
            return None
        expires, value, error = cached
        if time.monotonic() >= expires:
            del self._cache[key]
            return None
        self._cache.move_to_end(key)
        if error:
            raise RuntimeError("сервис временно недоступен")
        return value

    def _put_cached(self, key, value, seconds, *, error=False):
        self._cache[key] = (time.monotonic() + seconds, value, error)
        self._cache.move_to_end(key)
        while len(self._cache) > self.CACHE_LIMIT:
            self._cache.popitem(last=False)

    async def _cached(self, key, seconds, fetch):
        cached = self._get_cached(key)
        if cached is not None:
            return cached
        try:
            result = await fetch()
        except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError, ValueError):
            self._put_cached(key, None, min(seconds, 15), error=True)
            raise
        self._put_cached(key, result, seconds)
        return result

    async def _helix(self, endpoint, *, params=None, cache_seconds=30):
        key = ("helix", endpoint, tuple(sorted((params or {}).items())))

        async def fetch():
            result = await asyncio.wait_for(
                self.bot.twitch_api("GET", endpoint, params=params),
                timeout=self.REQUEST_TIMEOUT,
            )
            if not isinstance(result, dict):
                raise ValueError("некорректный ответ Twitch")
            return result

        return await self._cached(key, cache_seconds, fetch)

    async def _public(self, url, *, xml=False):
        if self.bot.http_session is None or self.bot.http_session.closed:
            self.bot.http_session = aiohttp.ClientSession()
        async with self.bot.http_session.get(
            url, timeout=aiohttp.ClientTimeout(total=self.REQUEST_TIMEOUT)
        ) as response:
            response.raise_for_status()
            # Bound public-provider input before parsing JSON or XML.
            try:
                content = await response.content.readexactly(262145)
            except asyncio.IncompleteReadError as error:
                content = error.partial
            else:
                raise ValueError("слишком большой ответ сервиса")
            if xml:
                if b"<!DOCTYPE" in content.upper() or b"<!ENTITY" in content.upper():
                    raise ValueError("некорректный XML сервиса")
                return ET.fromstring(content)
            data = json.loads(content)
            if not isinstance(data, dict):
                raise ValueError("некорректный ответ сервиса")
            return data

    async def user_data(self, username):
        username = self._login(username)
        result = await self._helix(
            "users", params={"login": username}, cache_seconds=3600
        )
        users = result.get("data", [])
        if not users:
            raise ValueError(f"пользователь @{username} не найден")
        user = users[0]
        if user.get("id"):
            self.bot.twitch_id_cache[username] = str(user["id"])
        return user

    async def _user_id(self, username):
        username = self._login(username)
        cached = self.bot.twitch_id_cache.get(username)
        if cached:
            return str(cached)
        return str((await self.user_data(username))["id"])

    async def follow_data(self, username, channel_name):
        if "moderator:read:followers" not in self.bot.scopes:
            raise RuntimeError("нужен OAuth scope moderator:read:followers")
        return await self._helix("channels/followers", params={
            "broadcaster_id": await self._user_id(channel_name),
            "user_id": await self._user_id(username), "first": 1,
        })

    async def follower_count(self, channel_name):
        # Twitch returns the total even without follower-list permissions.
        return await self._helix("channels/followers", params={
            "broadcaster_id": await self._user_id(channel_name), "first": 1,
        })

    async def subscription_data(self, username, channel_name):
        broadcaster_id = await self._subscription_broadcaster(channel_name)
        return await self._helix("subscriptions", params={
            "broadcaster_id": broadcaster_id,
            "user_id": await self._user_id(username), "first": 1,
        })

    async def _subscription_broadcaster(self, channel_name):
        if "channel:read:subscriptions" not in self.bot.scopes:
            raise RuntimeError("нужен OAuth scope channel:read:subscriptions")
        broadcaster_id = await self._user_id(channel_name)
        if broadcaster_id != str(self.bot.moderator_id):
            raise RuntimeError("нужен OAuth токен владельца канала")
        return broadcaster_id

    async def channel_emotes(self, channel_name):
        result = await self._helix("chat/emotes", params={
            "broadcaster_id": await self._user_id(channel_name),
        }, cache_seconds=600)
        return [item["name"] for item in result.get("data", []) if item.get("name")]

    @staticmethod
    def _timestamp(value):
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            result = result.replace(tzinfo=timezone.utc)
        return result.timestamp()

    def _elapsed(self, timestamp):
        return self.bot.format_elapsed(max(0, time.time() - timestamp))

    @staticmethod
    def _months(value):
        value = int(value)
        if not 0 <= value <= 10000:
            raise ValueError("некорректный срок подписки")
        if value == 0:
            return "меньше месяца"
        last, last_two = value % 10, value % 100
        unit = "месяцев" if 11 <= last_two <= 14 else (
            "месяц" if last == 1 else "месяца" if 2 <= last <= 4 else "месяцев"
        )
        return f"{value} {unit}"

    @staticmethod
    def _badge_months(tags):
        badge_info = (tags or {}).get("badge-info", "")
        if isinstance(badge_info, dict):
            badges = badge_info
        elif isinstance(badge_info, str):
            badges = dict(
                badge.split("/", 1) for badge in badge_info.split(",") if "/" in badge
            )
        else:
            badges = {}
        for name in ("subscriber", "founder"):
            try:
                months = int(badges[name])
                if 0 <= months <= 10000:
                    return months
            except (KeyError, ValueError, TypeError):
                continue
        return None

    async def _subage(self, username, channel_name, *, tags=None):
        months = self._badge_months(tags)
        if months is not None:
            return self._months(months)
        url = self.SUBAGE_URL.format(user=username, channel=channel_name)
        result = await self._cached(("subage", username, channel_name), 300,
                                    lambda: self._public(url))
        if result.get("statusHidden"):
            return f"@{username} скрыл информацию о подписке"
        if "cumulative" not in result:
            raise ValueError("сервис не вернул срок подписки")
        cumulative = result["cumulative"]
        if cumulative is None:
            return f"@{username} не подписан на канал"
        return self._months(cumulative["months"])

    async def _horoscope_feed(self):
        key = ("horoscope-feed",)
        async with self._horoscope_lock:
            cached = self._get_cached(key)
            if cached is not None:
                return cached
            try:
                result = await self._public(self.HOROSCOPE_URL, xml=True)
            except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError,
                    ValueError, ET.ParseError):
                # Ignio explicitly disallows fetching XML more than once/hour.
                self._put_cached(key, None, 3600, error=True)
                raise
            self._put_cached(key, result, 3600)
            return result

    async def _horoscope(self, argument):
        argument = argument.strip().casefold()
        signs = {**{key: key for key in self.SIGNS},
                 **{label.casefold(): key for key, label in self.SIGNS.items()}}
        sign = signs.get(argument)
        if sign is None:
            return ("неизвестный знак зодиака; укажи свой знак после команды: "
                    "овен, телец, близнецы, рак, лев, дева, весы, скорпион, "
                    "стрелец, козерог, водолей или рыбы")
        root = await self._horoscope_feed()
        dates = root.find("date")
        today = datetime.now(timezone(timedelta(hours=3))).strftime("%d.%m.%Y")
        period = next((key for key, value in dates.attrib.items() if value == today), None) if dates is not None else None
        if period is None:
            return "гороскоп на сегодня ещё не опубликован"
        text = " ".join(root.findtext(f"{sign}/{period}", "").split())
        if not text:
            raise ValueError("сервис не вернул гороскоп")
        return f"{self.SIGNS[sign]}: {html.unescape(text)} (ignio.com)"

    def observe_stream(self, channel_name, stream):
        """Remember stream/category transitions; the first category start is unknown.

        A caller polling Helix may supply its snapshots here. Exact category
        changes predating the bot's observations cannot be recovered by Helix.
        """
        channel_name = self._login(channel_name)
        previous = self._streams.get(channel_name)
        now = time.time()
        if stream:
            stream_id = str(stream.get("id", stream.get("started_at", "")))
            game_id = stream.get("game_id", stream.get("game_name", ""))
            changed_at = None
            if previous and previous.get("stream_id") == stream_id:
                changed_at = previous.get("game_since")
                if previous.get("game_id") != game_id:
                    changed_at = now
            self._streams[channel_name] = {
                "stream_id": stream_id, "game_id": game_id,
                "game_since": changed_at, "ended_at": None,
            }
        elif previous and previous.get("stream_id"):
            self._streams[channel_name] = {"ended_at": now}
        if channel_name in self._streams:
            self._streams.move_to_end(channel_name)
        while len(self._streams) > self.CACHE_LIMIT:
            self._streams.popitem(last=False)

    @staticmethod
    def _video_duration(value):
        match = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", value)
        if match is None or not any(match.groups()):
            raise ValueError("неизвестная длительность записи")
        hours, minutes, seconds = (int(part or 0) for part in match.groups())
        return hours * 3600 + minutes * 60 + seconds

    async def _uptime(self, channel_name):
        result = await self._helix("streams", params={"user_login": channel_name})
        streams = result.get("data", [])
        stream = streams[0] if streams else None
        self.observe_stream(channel_name, stream)
        if stream:
            text = "Стрим идёт " + self._elapsed(self._timestamp(stream["started_at"]))
            game = stream.get("game_name")
            if game:
                text += f" | Играем в {game}"
                since = self._streams[channel_name]["game_since"]
                if since is not None:
                    text += " уже " + self._elapsed(since)
            return text
        observed_end = self._streams.get(channel_name, {}).get("ended_at")
        if observed_end is not None:
            return ("Стрим офлайн; с конца прошлого стрима прошло примерно "
                    + self._elapsed(observed_end))
        videos = await self._helix("videos", params={
            "user_id": await self._user_id(channel_name), "type": "archive", "first": 1,
        }, cache_seconds=60)
        archives = videos.get("data", [])
        if not archives:
            return "стрим офлайн; нет данных о конце прошлого стрима"
        video = archives[0]
        ended_at = self._timestamp(video["created_at"]) + self._video_duration(video["duration"])
        return "Стрим офлайн; с конца прошлого стрима прошло " + self._elapsed(ended_at)

    async def resolve(self, name, argument, *, username, channel_name, tags=None):
        """Resolve an external variable without allowing a provider to stop chat."""
        name = name.casefold()
        if name not in self.NAMES:
            raise ValueError(f"неизвестная внешняя переменная: {name}")
        try:
            return await asyncio.wait_for(
                self._resolve(name, argument, username=username,
                              channel_name=channel_name, tags=tags),
                timeout=self.RESOLVE_TIMEOUT,
            )
        except ValueError as error:
            return str(error)
        except (aiohttp.ClientError, asyncio.TimeoutError, KeyError, TypeError,
                RuntimeError, ET.ParseError):
            # Do not echo upstream exception text: HTTP errors may include headers.
            log.warning("Данные переменной %s недоступны", name)
            if name == "followage":
                return ("данные followage недоступны: нужен moderator:read:followers "
                        "и права модератора канала")
            if name == "subcount":
                return ("данные subcount недоступны: нужен токен владельца канала "
                        "с channel:read:subscriptions")
            return f"данные {name} временно недоступны"

    async def _resolve(self, name, argument, *, username, channel_name, tags):
        channel_name = self._login(channel_name)
        username = self._login(username) if username else ""
        args = argument.split()
        if name == "horoscope":
            return await self._horoscope(argument)
        if name == "accountage":
            user = await self.user_data(args[0] if args else username)
            return self._elapsed(self._timestamp(user["created_at"]))
        if name == "emote":
            emotes = await self.channel_emotes(channel_name)
            return random.choice(emotes) if emotes else "в канале нет доступных эмотов"
        if name == "followcount":
            result = await self.follower_count(channel_name)
            return str(int(result["total"]))
        if name == "followage":
            target = self._login(args[0]) if args else username
            target_channel = self._login(args[1]) if len(args) > 1 else channel_name
            result = await self.follow_data(target, target_channel)
            follows = result["data"]
            if not follows:
                return f"@{target} не отслеживает канал"
            elapsed = self._elapsed(self._timestamp(follows[0]["followed_at"]))
            subject = "Вы зафолловлены" if target == username else f"@{target} зафолловлен"
            return f"{subject} на канал {target_channel} уже {elapsed}"
        if name == "subage":
            target = self._login(args[0]) if args else username
            return await self._subage(target, channel_name,
                                      tags=tags if target == username else None)
        if name == "subcount":
            broadcaster_id = await self._subscription_broadcaster(channel_name)
            result = await self._helix("subscriptions", params={
                "broadcaster_id": broadcaster_id, "first": 1,
            })
            return str(int(result["total"]))
        return await self._uptime(channel_name)
