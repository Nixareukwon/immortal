"""Local web control panel. Run with: python web_panel.py."""

import argparse
import asyncio
import base64
import math
from contextlib import contextmanager
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import sys
import time
from urllib.parse import urlsplit

from aiohttp import web

# При запуске скриптом (`python web_panel.py`) модуль называется `__main__`.
# Ленивые `from web_panel import InputError` внутри panel_* импортировали бы
# вторую копию этого файла с другим классом InputError, и middleware панели
# не распознал бы такие ошибки — они превращались бы в 400. Регистрируем
# себя под каноничным именем, чтобы все ссылались на один модуль.
sys.modules.setdefault("web_panel", sys.modules[__name__])

import bot as bot_module
from command_variables import VARIABLE_NAMES, contains_variable, needs_sender, parse_template
from panel_runtime import Runtime
from panel_accounts import Accounts
from panel_channels import Channels
from panel_twitch import TwitchAuth
from music_widget import MusicWidget, BROWSER_PROVIDERS, cover_data_url


ROOT = Path(__file__).resolve().parent
PANEL_KEY = web.AppKey("panel", object)
NAME_RE = re.compile(r"\w+\Z", re.UNICODE)
CUSTOM_VARIABLE_RE = re.compile(r"[a-z]{1,30}\Z", re.ASCII)
CHANNEL_RE = re.compile(r"[a-z0-9_]{1,25}\Z")
SESSION_TTL = 12 * 3600


class InputError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def text_field(data, key, limit, label=None):
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise InputError(f"Заполните поле «{label or key}».")
    value = value.strip()
    if len(value) > limit or "\x00" in value:
        raise InputError(f"Поле «{label or key}»: максимум {limit} символов.")
    return value


def integer(data, key, lower, upper, default=None):
    value = data.get(key, default)
    if type(value) is not int or not lower <= value <= upper:
        raise InputError(f"{key}: введите целое число от {lower} до {upper}.")
    return value


def boolean(data, key, default=False):
    value = data.get(key, default)
    if type(value) is not bool:
        raise InputError(f"{key}: ожидается переключатель да/нет.")
    return value


def command_name(value):
    if not isinstance(value, str):
        raise InputError("Укажите имя команды.")
    value = value.strip().removeprefix(bot_module.PREFIX).casefold()
    if not 1 <= len(value) <= 60 or not NAME_RE.fullmatch(value):
        raise InputError("Имя команды: 1–60 букв, цифр или символов подчёркивания.")
    return value


def builtin_catalog():
    descriptions = {
        "help": "Справка по командам бота", "commands": "Список команд канала",
        "points": "Баланс зрителя", "top": "Таблица лидеров", "daily": "Ежедневная награда",
        "addpoints": "Изменить баланс зрителя", "addcmd": "Добавить пользовательскую команду",
        "delcmd": "Удалить команду", "editcmd": "Редактировать команду",
        "timer": "Управление автоматическими сообщениями", "af": "Настройки антифлуда",
        "timeouts": "Длительности таймаутов", "banword": "Пользовательские запрещённые слова",
        "gamealias": "Алиасы категорий Twitch", "permit": "Временно разрешить ссылки",
        "allow": "Временно отключить антифлуд для зрителя", "disallow": "Отозвать разрешение",
        "so": "Рекомендовать другой канал", "title": "Название трансляции",
        "game": "Категория трансляции", "massban": "Массовая модерация по фразе",
        "weather": "Погода в городе", "uptime": "Длительность трансляции",
        "8ball": "Шуточный ответ на вопрос", "roulette": "Игра с монетами",
        "gamble": "Игра с монетами", "duel": "Дуэль зрителей",
    }
    result = []
    for value in vars(bot_module.Bot).values():
        if hasattr(value, "name") and hasattr(value, "aliases") and hasattr(value, "callback"):
            result.append({"name": value.name, "aliases": list(value.aliases or []),
                           "description": descriptions.get(value.name, "Встроенная команда чата")})
    return sorted(result, key=lambda item: item["name"])


class Panel:
    def __init__(self, db_path, runtime, password, public_origin=""):
        self.db = bot_module.Database(str(db_path), compact_timers=False)
        self.db.conn.execute("PRAGMA busy_timeout=5000")
        self.runtime = runtime
        self.password = password
        self.public_origin = public_origin
        self.csrf_token = secrets.token_urlsafe(32)
        self.sessions = {}
        self.login_attempts = {}
        self.accounts = Accounts(self)
        self.twitch = TwitchAuth(self)
        self.channels = Channels(self)
        runtime.channels = self.channels
        self.music = MusicWidget(self.db.conn)
        self.catalog = builtin_catalog()
        self.reserved = {name.casefold() for item in self.catalog for name in [item["name"], *item["aliases"]]}

    @contextmanager
    def transaction(self):
        self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.db.conn
            self.db.conn.commit()
        except BaseException:
            self.db.conn.rollback()
            raise

    def authenticated(self, request):
        if not self.password:
            return True
        session_id = request.cookies.get("immortal_session", "")
        expires = self.sessions.get(session_id, 0)
        if expires <= time.time():
            self.sessions.pop(session_id, None)
            return False
        return True

    async def body(self, request):
        if request.path != "/api/music/browser" and len(await request.read()) > 16 * 1024:
            raise InputError("Слишком большой запрос.", 413)
        try:
            data = await request.json()
        except (ValueError, UnicodeError):
            raise InputError("Не удалось прочитать данные формы.") from None
        if not isinstance(data, dict):
            raise InputError("Ожидается объект с полями формы.")
        return data

    async def index(self, request):
        if not self.authenticated(request):
            raise web.HTTPFound("/login")
        html = (ROOT / "templates" / "panel.html").read_text(encoding="utf-8")
        return web.Response(text=html.replace("{{csrf_token}}", self.csrf_token), content_type="text/html")

    async def login(self, request):
        if request.method == "GET":
            if self.authenticated(request):
                raise web.HTTPFound("/")
            html = (ROOT / "templates" / "login.html").read_text(encoding="utf-8")
            html = html.replace("{{csrf_token}}", self.csrf_token).replace(
                "{{twitch_hidden}}", "" if self.twitch.configured else "hidden")
            return web.Response(text=html, content_type="text/html")
        data = await self.body(request)
        remote = request.remote or "local"
        attempts = [stamp for stamp in self.login_attempts.get(remote, []) if stamp > time.time() - 60]
        if len(attempts) >= 10:
            raise InputError("Слишком много попыток. Подождите минуту.", 429)
        username = data.get("username", "")
        username = username.strip().lower() if isinstance(username, str) else "!invalid"
        supplied = data.get("password")
        # Reserve the attempt before an expensive password hash can yield.
        self.login_attempts[remote] = [*attempts, time.time()]
        if username in {"", "owner"}:
            valid = isinstance(supplied, str) and hmac.compare_digest(supplied.encode(), self.password.encode())
            username = "owner"
        else:
            valid = await self.accounts.verify(username, supplied)
        if not valid:
            raise InputError("Неверный логин или пароль. Если аккаунта ещё нет, откройте ссылку-приглашение от владельца.", 401)
        self.login_attempts.pop(remote, None)
        return self.accounts.session(request, username)

    async def logout(self, request):
        self.sessions.pop(request.cookies.get("immortal_session", ""), None)
        response = web.json_response({"ok": True})
        response.del_cookie("immortal_session", path="/")
        return response

    async def state(self, request):
        conn = self.db.conn
        username = self.accounts.identity(request)
        owner = username == "owner"
        channels = self.channels.visible(username)
        visible = set(channels)
        # Настройки разделены по каналам: каждый видит только свои каналы.
        commands = [dict(row) for row in conn.execute(
            "SELECT channel, name, response, cooldown_seconds, aliases FROM custom_commands "
            "ORDER BY channel, name")]
        for item in commands:
            item["aliases"] = json.loads(item["aliases"])
        timers = []
        for row in conn.execute(
                "SELECT id, channel, message, interval_minutes, enabled FROM timers ORDER BY channel, id"):
            item = dict(row)
            item["enabled"] = bool(item["enabled"])
            timers.append(item)
        users = [dict(row) for row in conn.execute(
            "SELECT channel, name, points FROM users ORDER BY channel, points DESC, name")]
        quotes = [dict(row) for row in conn.execute(
            "SELECT id, channel, text, author FROM quotes ORDER BY channel, id DESC")]
        banned_words = [dict(row) for row in conn.execute(
            "SELECT channel, word FROM custom_banned_words ORDER BY channel, word")]
        category_aliases = [dict(row) for row in conn.execute(
            "SELECT channel, alias, category FROM category_aliases ORDER BY channel, alias")]
        custom_variables = [dict(row) for row in conn.execute(
            "SELECT channel, name, value, description FROM custom_variables ORDER BY channel, name")]
        moderation_channels = sorted(set(bot_module.CHANNELS) | {
            row[0] for row in conn.execute("SELECT channel FROM antiflood_settings")
        })
        def belongs(channel):
            return channel in visible
        commands = [item for item in commands if belongs(item["channel"])]
        timers = [item for item in timers if belongs(item["channel"])]
        users = [item for item in users if belongs(item["channel"])]
        quotes = [item for item in quotes if belongs(item["channel"])]
        banned_words = [item for item in banned_words if belongs(item["channel"])]
        category_aliases = [item for item in category_aliases if belongs(item["channel"])]
        custom_variables = [item for item in custom_variables if belongs(item["channel"])]
        moderation_channels = [channel for channel in moderation_channels if belongs(channel)]
        stats = {}
        for channel in channels:
            channel_timers = [item for item in timers if item["channel"] == channel]
            channel_users = [item for item in users if item["channel"] == channel]
            stats[channel] = {
                "commands": sum(item["channel"] == channel for item in commands),
                "timers": len(channel_timers),
                "active_timers": sum(item["enabled"] for item in channel_timers),
                "users": len(channel_users),
                "points": sum(item["points"] for item in channel_users),
                "quotes": sum(item["channel"] == channel for item in quotes),
            }
        runtime = self.runtime.snapshot()
        if not owner:
            # Журнал бота общий для всех каналов: содержимое чужого чата не показываем.
            private_lines = ("[чат]", "[отправлено]")
            runtime = {**runtime, "channels": channels,
                       "logs": [entry for entry in runtime["logs"]
                                if not entry["message"].startswith(private_lines)]}
        return web.json_response({
            "prefix": bot_module.PREFIX, "points_name": bot_module.POINTS_NAME,
            "channels": channels,
            "identity": {"username": username, "owner": owner},
            "channel_owners": self.channels.owners_map(channels),
            "add_channel": self.channels.add_status(username),
            "stats": stats,
            "runtime": runtime, "commands": commands, "timers": timers,
            "users": users, "quotes": quotes,
            "moderation": [{"channel": channel, "settings": self.db.get_antiflood_settings(channel)}
                           for channel in moderation_channels],
            "banned_words": banned_words,
            "default_banned_words": sorted(bot_module.BANNED_WORDS),
            "category_aliases": category_aliases,
            "custom_variables": custom_variables,
            "builtin_commands": self.catalog,
            "music": self.music.panel_state() if owner else None,
        })

    async def music_settings(self, request):
        if self.accounts.identity(request) != "owner":
            raise InputError("Раздел «Музыка для OBS» доступен только владельцу панели.", 403)
        if request.method == "PUT":
            data = await self.body(request)
            source = data.get("source", "")
            if not isinstance(source, str) or len(source) > 300:
                raise InputError("Выберите приложение для музыки.")
            self.music.configure(boolean(data, "enabled"), source, boolean(data, "hide_paused", True))
        await self.music.refresh()
        return web.json_response(self.music.panel_state())

    async def music_overlay(self, request):
        if not self.music.valid_token(request.query.get("token", "")):
            raise InputError("Неверная ссылка виджета. Скопируйте её из панели.", 403)
        if request.path.endswith("/state"):
            await self.music.refresh()
            return web.json_response(self.music.overlay_state())
        return web.FileResponse(ROOT / "templates" / "music_overlay.html")

    async def music_browser(self, request):
        if not self.music.valid_browser_token(request.query.get("token", "")):
            raise InputError("Неверный ключ расширения.", 403)
        data = await self.body(request)
        provider = data.get("provider")
        if provider not in BROWSER_PROVIDERS:
            raise InputError("Разрешены только Spotify, Яндекс Музыка и YouTube Music.")
        title = text_field(data, "title", 500)
        artist = data.get("artist", "")
        if not isinstance(artist, str) or len(artist) > 300:
            raise InputError("Некорректный исполнитель.")
        values = {}
        for key in ("position", "duration"):
            value = data.get(key, 0)
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 604800:
                raise InputError("Некорректное время воспроизведения.")
            values[key] = value
        cover = None
        encoded = data.get("cover", "")
        if not isinstance(encoded, str) or len(encoded) > 1400000:
            raise InputError("Некорректная обложка.")
        if encoded:
            try:
                cover = cover_data_url(base64.b64decode(encoded, validate=True))
            except ValueError:
                raise InputError("Некорректная обложка.") from None
        key = text_field(data, "client", 100)
        self.music.browser_report(key, {"source": provider, "title": title, "artist": artist,
            "playing": boolean(data, "playing"), "current": True, "cover": cover, **values})
        return web.json_response({"ok": True})

    async def commands(self, request):
        original = request.match_info.get("name")
        if request.method == "DELETE":
            channel = self.query_channel(request)
            with self.transaction() as conn:
                if not conn.execute("DELETE FROM custom_commands WHERE channel=? AND name=?",
                                    (channel, original)).rowcount:
                    raise InputError("Команда уже удалена. Обновите страницу.", 404)
            return web.json_response({"ok": True})
        data = await self.body(request)
        channel = self.channel_field(data, request)
        name = command_name(data.get("name"))
        response = text_field(data, "response", 4000, "Ответ")
        custom_variable_names = {
            item["name"] for item in self.db.list_custom_variables(channel)
        }
        parse_template(response, custom_variable_names)
        cooldown = integer(data, "cooldown_seconds", 0, 86400, 0)
        aliases = data.get("aliases", [])
        if not isinstance(aliases, list) or len(aliases) > 5:
            raise InputError("Можно указать до 5 алиасов команды.")
        aliases = [command_name(alias) for alias in aliases]
        names = [name, *aliases]
        if len(set(names)) != len(names):
            raise InputError("Имена и алиасы команды должны отличаться.")
        if self.reserved.intersection(names):
            raise InputError("Это имя или алиас занят встроенной командой бота.")
        with self.transaction() as conn:
            rows = conn.execute(
                "SELECT name, aliases FROM custom_commands WHERE channel=?", (channel,)).fetchall()
            if original is not None and not any(row["name"] == original for row in rows):
                raise InputError("Команда уже удалена. Обновите страницу.", 404)
            if original is None and len(rows) >= 30:
                raise InputError("Достигнут лимит: 30 пользовательских команд.")
            occupied = {value for row in rows if row["name"] != original
                        for value in [row["name"], *json.loads(row["aliases"]) ]}
            if occupied.intersection(names):
                raise InputError("Имя или алиас уже используется другой командой.", 409)
            if original is not None:
                conn.execute("DELETE FROM custom_commands WHERE channel=? AND name=?", (channel, original))
            conn.execute(
                "INSERT INTO custom_commands(channel,name,response,cooldown_seconds,aliases) "
                "VALUES(?,?,?,?,?)",
                (channel, name, response, cooldown, json.dumps(aliases, ensure_ascii=False)))
        bot = getattr(self.runtime, "bot", None)
        if bot is not None:
            bot.custom_command_uses.clear()
        return web.json_response({"ok": True, "name": name})

    async def timers(self, request):
        timer_id = request.match_info.get("id")
        if request.method == "DELETE":
            channel = self.query_channel(request)
            # Preserve stable IDs in the panel; the chat command retains its existing numbering behavior.
            with self.transaction() as conn:
                if not conn.execute("DELETE FROM timers WHERE id=? AND channel=?",
                                    (timer_id, channel)).rowcount:
                    raise InputError("Таймер уже удалён. Обновите страницу.", 404)
            return web.json_response({"ok": True})
        data = await self.body(request)
        channel = self.channel_field(data, request)
        message = text_field(data, "message", 450, "Сообщение")
        custom_variable_names = {
            item["name"] for item in self.db.list_custom_variables(channel)
        }
        template_parts = parse_template(message, custom_variable_names)
        if contains_variable(template_parts, "spam"):
            raise InputError("В таймере нельзя использовать переменную {spam}.")
        if needs_sender(template_parts):
            raise InputError("В таймере недоступны переменные {sender} и {user}: у него нет отправителя.")
        interval = integer(data, "interval_minutes", 1, 1440)
        enabled = boolean(data, "enabled", True)
        with self.transaction() as conn:
            current = conn.execute("SELECT * FROM timers WHERE id=? AND channel=?",
                                   (timer_id, channel)).fetchone() if timer_id else None
            if timer_id and current is None:
                raise InputError("Таймер уже удалён. Обновите страницу.", 404)
            active = conn.execute("SELECT COUNT(*) FROM timers WHERE channel=? AND enabled=1 AND id<>?",
                                  (channel, timer_id or -1)).fetchone()[0]
            if enabled and active >= 5:
                raise InputError("Одновременно могут работать не больше 5 таймеров канала.")
            last_sent = (current["last_sent"] if current and current["enabled"] and enabled
                         and current["interval_minutes"] == interval else time.time())
            if timer_id:
                conn.execute(
                    "UPDATE timers SET message=?,interval_minutes=?,enabled=?,last_sent=? "
                    "WHERE id=? AND channel=?",
                    (message, interval, int(enabled), last_sent, timer_id, channel))
            else:
                timer_id = conn.execute(
                    "INSERT INTO timers(channel,message,interval_minutes,enabled,last_sent) "
                    "VALUES(?,?,?,?,?)",
                    (channel, message, interval, int(enabled), last_sent)).lastrowid
        return web.json_response({"ok": True, "id": int(timer_id)})

    async def points(self, request):
        data = await self.body(request)
        channel = self.channel_field(data, request)
        name = text_field(data, "name", 25, "Имя зрителя").lstrip("@").lower()
        if not CHANNEL_RE.fullmatch(name):
            raise InputError("Имя Twitch может содержать латинские буквы, цифры и подчёркивание.")
        amount = integer(data, "amount", -1_000_000_000, 1_000_000_000)
        self.db.add_points(channel, name, amount)
        return web.json_response({"ok": True, "points": self.db.get_points(channel, name)})

    async def quotes(self, request):
        quote_id = request.match_info.get("id")
        if request.method == "DELETE":
            channel = self.query_channel(request)
            with self.transaction() as conn:
                if not conn.execute("DELETE FROM quotes WHERE id=? AND channel=?",
                                    (quote_id, channel)).rowcount:
                    raise InputError("Цитата не найдена.", 404)
            return web.json_response({"ok": True})
        data = await self.body(request)
        channel = self.channel_field(data, request)
        text = text_field(data, "text", 2000, "Цитата")
        author = text_field(data, "author", 100, "Автор")
        with self.transaction() as conn:
            if quote_id:
                if not conn.execute("UPDATE quotes SET text=?,author=? WHERE id=? AND channel=?",
                                    (text, author, quote_id, channel)).rowcount:
                    raise InputError("Цитата не найдена.", 404)
            else:
                quote_id = conn.execute("INSERT INTO quotes(channel,text,author) VALUES(?,?,?)",
                                        (channel, text, author)).lastrowid
        return web.json_response({"ok": True, "id": int(quote_id)})

    def require_channel(self, request, channel):
        username = self.accounts.identity(request)
        if not self.channels.may_manage(username, channel):
            if channel not in bot_module.CHANNELS:
                raise InputError("Канал не подключён к боту.")
            raise InputError("Этот канал принадлежит другому аккаунту панели.", 403)

    def channel_field(self, data, request):
        """Канал из тела запроса с проверкой прав текущего аккаунта."""
        channel = text_field(data, "channel", 25, "Канал").lower().lstrip("#")
        if not CHANNEL_RE.fullmatch(channel):
            raise InputError("Канал: 1–25 латинских букв, цифр или символов подчёркивания.")
        self.require_channel(request, channel)
        return channel

    def query_channel(self, request):
        """Канал из параметра ?channel= с проверкой прав текущего аккаунта."""
        channel = (request.query.get("channel") or "").strip().lower().lstrip("#")
        if not CHANNEL_RE.fullmatch(channel):
            raise InputError("Укажите канал: ?channel=<логин>.")
        self.require_channel(request, channel)
        return channel

    async def moderation(self, request):
        channel = request.match_info["channel"].lower().lstrip("#")
        self.require_channel(request, channel)
        data = await self.body(request)
        settings = {key: boolean(data, key) for key in ("enabled", "video", "lang")}
        for key, lower, upper in (("mode", 1, 3), ("size", 0, 10000), ("main_size", 0, 10000),
                                  ("percent", 0, 100), ("spam_messages", 0, 50),
                                  ("spam_seconds", 1, 60), ("max_link_length", 1, 2000)):
            settings[key] = integer(data, key, lower, upper, bot_module.ANTIFLOOD_DEFAULTS[key])
        durations = data.get("timeouts")
        if not isinstance(durations, list) or not 1 <= len(durations) <= 5 or any(
            type(value) is not int or not 1 <= value <= 1_209_600 for value in durations
        ):
            raise InputError("Таймауты: от 1 до 5 целых чисел, каждое от 1 до 1209600 секунд.")
        settings["timeouts"] = durations
        self.db.set_antiflood_settings(channel, settings)
        return web.json_response({"ok": True})

    async def banned_words(self, request):
        if request.method == "DELETE":
            channel = self.query_channel(request)
            if not self.db.delete_banned_word(channel, request.match_info["word"]):
                raise InputError("Пользовательское слово не найдено.", 404)
        else:
            data = await self.body(request)
            channel = self.channel_field(data, request)
            word = " ".join(text_field(data, "word", 60, "Слово").casefold().split())
            with self.transaction() as conn:
                if conn.execute("SELECT 1 FROM custom_banned_words WHERE channel=? AND word=?",
                                (channel, word)).fetchone():
                    raise InputError("Это слово уже добавлено.", 409)
                if conn.execute("SELECT COUNT(*) FROM custom_banned_words WHERE channel=?",
                                (channel,)).fetchone()[0] >= 30:
                    raise InputError("Достигнут лимит: 30 пользовательских запрещённых слов.")
                conn.execute("INSERT INTO custom_banned_words(channel, word) VALUES(?, ?)", (channel, word))
        return web.json_response({"ok": True})

    async def categories(self, request):
        if request.method == "DELETE":
            channel = self.query_channel(request)
            if not self.db.delete_category_alias(channel, request.match_info["alias"]):
                raise InputError("Алиас категории не найден.", 404)
        else:
            data = await self.body(request)
            channel = self.channel_field(data, request)
            alias = text_field(data, "alias", 30, "Алиас").casefold()
            category = text_field(data, "category", 100, "Категория")
            with self.transaction() as conn:
                exists = conn.execute("SELECT 1 FROM category_aliases WHERE channel=? AND alias=?",
                                      (channel, alias)).fetchone()
                if not exists and conn.execute("SELECT COUNT(*) FROM category_aliases WHERE channel=?",
                                               (channel,)).fetchone()[0] >= 20:
                    raise InputError("Достигнут лимит: 20 алиасов категорий.")
                conn.execute("INSERT INTO category_aliases(channel,alias,category) VALUES(?,?,?) "
                             "ON CONFLICT(channel,alias) DO UPDATE SET category=excluded.category",
                             (channel, alias, category))
        return web.json_response({"ok": True})

    async def custom_variables(self, request):
        original = request.match_info.get("name")
        if request.method == "DELETE":
            channel = self.query_channel(request)
            with self.transaction() as conn:
                if not conn.execute(
                    "DELETE FROM custom_variables WHERE channel=? AND name=?",
                    (channel, original),
                ).rowcount:
                    raise InputError("Переменная уже удалена. Обновите страницу.", 404)
            return web.json_response({"ok": True})

        data = await self.body(request)
        channel = self.channel_field(data, request)
        name = text_field(data, "name", 30, "Имя переменной").casefold()
        if not CUSTOM_VARIABLE_RE.fullmatch(name) or name in VARIABLE_NAMES:
            raise InputError(
                "Имя переменной: от 1 до 30 латинских букв; "
                "имя не должно совпадать со встроенной переменной."
            )
        value = text_field(data, "value", 1000, "Значение")
        description = text_field(data, "description", 300, "Описание")
        with self.transaction() as conn:
            current = conn.execute(
                "SELECT 1 FROM custom_variables WHERE channel=? AND name=?",
                (channel, original),
            ).fetchone() if original else None
            if original and current is None:
                raise InputError("Переменная уже удалена. Обновите страницу.", 404)
            existing = conn.execute(
                "SELECT 1 FROM custom_variables WHERE channel=? AND name=?",
                (channel, name),
            ).fetchone()
            if existing and name != original:
                raise InputError("Переменная с таким именем уже существует.", 409)
            if not original and conn.execute(
                "SELECT COUNT(*) FROM custom_variables WHERE channel=?", (channel,)
            ).fetchone()[0] >= 50:
                raise InputError("Достигнут лимит: 50 пользовательских переменных.")
            if original:
                conn.execute(
                    "DELETE FROM custom_variables WHERE channel=? AND name=?",
                    (channel, original),
                )
            conn.execute(
                "INSERT INTO custom_variables(channel,name,value,description) VALUES(?,?,?,?)",
                (channel, name, value, description),
            )
        return web.json_response({"ok": True, "name": name})

    async def bot_action(self, request):
        if self.accounts.identity(request) != "owner":
            raise InputError("Запускать и останавливать бота может только владелец панели.", 403)
        if request.match_info["action"] == "start":
            try:
                await self.twitch.apply_owner_token()
            except Exception:
                # Сбой обновления токена не должен мешать запуску бота.
                pass
            await self.runtime.start()
        else:
            await self.runtime.stop()
        return web.json_response({"ok": True, "runtime": self.runtime.snapshot()})

    async def chat(self, request):
        data = await self.body(request)
        channel = text_field(data, "channel", 25, "Канал").lower().lstrip("#")
        self.require_channel(request, channel)
        message = text_field(data, "message", 450, "Сообщение")
        if "\n" in message or "\r" in message:
            raise InputError("Сообщение должно занимать одну строку.")
        await self.runtime.send_chat(channel, message)
        return web.json_response({"ok": True})


@web.middleware
async def protections(request, handler):
    panel = request.app[PANEL_KEY]
    try:
        try:
            hostname = urlsplit("//" + request.host).hostname
        except ValueError:
            hostname = None
        external = panel.public_origin and request.host == urlsplit(panel.public_origin).netloc
        if external:
            try:
                local_proxy = ipaddress.ip_address(request.remote or "").is_loopback
            except ValueError:
                local_proxy = False
            if not local_proxy or request.headers.get("X-Forwarded-Proto") != "https":
                raise InputError("Требуется защищённое подключение через туннель.", 403)
            try:
                visitor = str(ipaddress.ip_address(request.headers.get("CF-Connecting-IP", "")))
            except ValueError:
                raise InputError("Не удалось определить подключение туннеля.", 403)
            request = request.clone(scheme="https", remote=visitor)
        elif hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise InputError("Панель доступна только через локальный адрес.", 403)
        browser_report = request.path == "/api/music/browser" and request.method == "POST"
        public = (browser_report or request.path in {"/login", "/register", "/auth/twitch/start", "/auth/twitch/poll"}
                  or request.path.startswith("/static/")
                  or (request.method == "GET" and request.path in {"/overlay/music", "/overlay/music/state"}))
        if request.method not in {"GET", "HEAD", "OPTIONS"} and not browser_report:
            origin = request.headers.get("Origin")
            if origin and origin != f"{request.scheme}://{request.host}":
                raise InputError("Запрос с другого сайта отклонён.", 403)
            if not hmac.compare_digest(request.headers.get("X-CSRF-Token", ""), panel.csrf_token):
                raise InputError("Обновите страницу и повторите действие.", 403)
        if not public and request.path != "/" and not panel.authenticated(request):
            raise InputError("Войдите в панель управления.", 401)
        response = await handler(request)
    except InputError as error:
        response = web.json_response({"error": str(error)}, status=error.status)
    except (ValueError, TypeError) as error:
        response = web.json_response({"error": str(error)}, status=400)
    except sqlite3.IntegrityError:
        panel.db.conn.rollback()
        response = web.json_response({"error": "Данные уже изменены. Обновите страницу."}, status=409)
    except sqlite3.OperationalError:
        panel.db.conn.rollback()
        response = web.json_response({"error": "База занята. Повторите действие через несколько секунд."}, status=503)
    except RuntimeError as error:
        response = web.json_response({"error": str(error)}, status=409)
    except web.HTTPException as error:
        response = error
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    )
    return response


def create_app(db_path=None, runtime=None, password=None, public_origin=None):
    origin = (os.getenv("PANEL_PUBLIC_ORIGIN", "") if public_origin is None else public_origin).rstrip("/")
    panel_password = os.getenv("PANEL_PASSWORD", "") if password is None else password
    if origin:
        parsed = urlsplit(origin)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.path or parsed.query
                or parsed.fragment or parsed.username or parsed.password or parsed.port):
            raise ValueError("PANEL_PUBLIC_ORIGIN должен быть HTTPS-адресом без пути и порта.")
        if len(panel_password) < 16:
            raise ValueError("Для внешнего доступа задайте PANEL_PASSWORD длиной не менее 16 символов.")
    path = Path(db_path or os.getenv("BOT_DATABASE_PATH") or ROOT / "bot.db").resolve()
    panel = Panel(path, runtime if runtime is not None else Runtime(path),
                  panel_password, origin)
    app = web.Application(middlewares=[protections], client_max_size=2 * 1024 * 1024)
    app[PANEL_KEY] = panel
    app.router.add_get("/", panel.index)
    app.router.add_route("GET", "/login", panel.login)
    app.router.add_post("/login", panel.login)
    app.router.add_post("/logout", panel.logout)
    app.router.add_get("/register", panel.accounts.register)
    app.router.add_post("/register", panel.accounts.register)
    app.router.add_get("/access", panel.accounts.access)
    app.router.add_get("/api/access", panel.accounts.access)
    app.router.add_post("/api/access/invite", panel.accounts.access)
    app.router.add_delete("/api/access/{username}", panel.accounts.access)
    app.router.add_post("/auth/twitch/start", panel.twitch.start)
    app.router.add_post("/auth/twitch/poll", panel.twitch.poll)
    app.router.add_get("/api/twitch", panel.twitch.state)
    app.router.add_post("/api/twitch/unlink", panel.twitch.unlink)
    app.router.add_get("/api/state", panel.state)
    app.router.add_delete("/api/channels/{channel}", panel.channels.remove)
    app.router.add_get("/api/music", panel.music_settings)
    app.router.add_put("/api/music", panel.music_settings)
    app.router.add_post("/api/music/browser", panel.music_browser)
    app.router.add_get("/overlay/music", panel.music_overlay)
    app.router.add_get("/overlay/music/state", panel.music_overlay)
    for resource, handler, key in (("commands", panel.commands, "name"), ("timers", panel.timers, "id"),
                                    ("quotes", panel.quotes, "id")):
        app.router.add_post(f"/api/{resource}", handler)
        app.router.add_put(f"/api/{resource}/{{{key}}}", handler)
        app.router.add_delete(f"/api/{resource}/{{{key}}}", handler)
    app.router.add_post("/api/points", panel.points)
    app.router.add_put("/api/moderation/{channel}", panel.moderation)
    app.router.add_post("/api/banned-words", panel.banned_words)
    app.router.add_delete("/api/banned-words/{word}", panel.banned_words)
    app.router.add_post("/api/categories", panel.categories)
    app.router.add_delete("/api/categories/{alias}", panel.categories)
    app.router.add_post("/api/variables", panel.custom_variables)
    app.router.add_put("/api/variables/{name}", panel.custom_variables)
    app.router.add_delete("/api/variables/{name}", panel.custom_variables)
    app.router.add_post("/api/bot/{action:start|stop}", panel.bot_action)
    app.router.add_post("/api/chat", panel.chat)
    app.router.add_static("/static/", ROOT / "static", show_index=False, follow_symlinks=False)

    refresh_task = None

    async def start_twitch(application):
        nonlocal refresh_task
        refresh_task = asyncio.create_task(panel.twitch.refresh_loop(), name="twitch-token-refresh")

    async def cleanup(application):
        nonlocal refresh_task
        if refresh_task is not None:
            refresh_task.cancel()
            try:
                await refresh_task
            except asyncio.CancelledError:
                pass
            refresh_task = None
        try:
            await panel.runtime.stop()
        finally:
            try:
                await panel.twitch.close()
            finally:
                panel.db.conn.close()

    app.on_startup.append(start_twitch)
    app.on_cleanup.append(cleanup)
    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Локальная панель управления Twitch-ботом")
    parser.add_argument("--host", choices=("127.0.0.1", "localhost", "::1"), default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    web.run_app(create_app(), host=args.host, port=args.port, print=print)
