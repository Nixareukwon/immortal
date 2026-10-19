import asyncio
import ast
import json
import math
import os
import random
import re
import sqlite3
import sys
import time
import unicodedata
from collections import deque
from datetime import datetime
from pathlib import Path

import aiohttp
from dotenv import load_dotenv
from twitchio.ext import commands

from stream_info import StreamInfo

from command_variables import SenderRequired, TemplateError, Variable, contains_variable, parse_template, needs_sender, render_template
from variable_services import VariableServices
from weather_service import WeatherService

# Чтобы эмодзи и кириллица в чате не ломали вывод в консоли Windows
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

load_dotenv()

TOKEN = os.getenv("TWITCH_TOKEN", "").strip().strip('"').strip("'")
CLIENT_ID = (os.getenv("TWITCH_CLIENT_ID") or "").strip() or None
PREFIX = os.getenv("BOT_PREFIX", "!").strip() or "!"
CHANNELS = [
    c.strip().lstrip("#").lower()
    for c in os.getenv("TWITCH_CHANNELS", "").split(",")
    if c.strip()
]
DEBUG = os.getenv("DEBUG", "1") == "1"      # печатать в консоль все сообщения чата
GREETING = "Перезапуск бота, был выполнен успешно✅"  # сообщение при входе в канал ("" — отключить)

# ---------------- Настройки ----------------
POINTS_NAME = "монет"
POINTS_INTERVAL = 300          # раз в 5 минут начисляем очки активным зрителям
POINTS_PER_TICK = 10
ACTIVE_WINDOW = 600            # "активный" = писал в чат за последние 10 минут
DAILY_BONUS = 200
DAILY_COOLDOWN = 10            # интервал между получениями бонуса, в секундах
TIMER_INTERVAL = 500           # автосообщения раз в 15 минут
OFFLINE_TIMER_EXTRA = 300      # в офлайне увеличиваем интервалы таймеров на 5 минут
STREAM_STATUS_INTERVAL = 60
TIMER_MESSAGES = [
    "Зарабатывай монеты, просто находясь в чате. Проверить баланс: !points",
]
DEFAULT_BANNED_WORDS = {
    "ниггер", "нига", "нага", "nigger", "nigga", "naga", "coon",
    "chink", "kike", "spic", "wetback",
    "пидор", "пидорас", "педик", "гомик", "петух", "faggot", "fag",
    "tranny", "trannies",
    "хиджаб", "белый", "натурал", "гетеросексуал",
    "хохол", "хач", "хачик", "чурка", "чурбан", "узкоглазый",
    "черножопый", "жид", "москаль", "ватник", "сионист",
    "куколд", "конча"
    "ретард",
    "virgin", "девственник", "simp", "симп", "incel", "инцел",
    "cunt", }

BANNED_WORDS = DEFAULT_BANNED_WORDS | {
    word.strip()
    for word in os.getenv("TWITCH_BANNED_WORDS", "").split(",")
    if word.strip()
}
MAX_CAPS_RATIO = 0.7
MIN_CAPS_LEN = 15
LINK_REGEX = re.compile(r"(https?://|www\.|\b[a-z0-9-]+\.(com|ru|net|org|io|gg|tv|ly)\b)", re.I)
PERMIT_SECONDS = 60
ANTIFLOOD_DEFAULTS = {
    "enabled": False,
    "mode": 2,
    "size": 900,
    "main_size": 450,
    "percent": 0,
    "video": False,
    "lang": False,
    "spam_messages": 5,
    "spam_seconds": 3,
    "timeouts": [10, 30, 60],
    "max_link_length": 120,
}
# --------------------------------------------

EIGHT_BALL = [
    "Бесспорно", "Предрешено", "Никаких сомнений", "Да", "Скорее всего",
    "Пока не ясно, попробуй снова", "Спроси позже", "Лучше не рассказывать",
    "Даже не думай", "Мой ответ — нет", "Весьма сомнительно",
]


LOG_LISTENERS = []


def log(*args):
    print(*args, flush=True)
    message = " ".join(str(arg) for arg in args)
    for listener in tuple(LOG_LISTENERS):
        try:
            listener(message)
        except Exception:
            pass


class Database:
    def __init__(self, path=None, *, compact_timers=True):
        path = path or os.getenv("BOT_DATABASE_PATH") or Path(__file__).with_name("bot.db")
        if str(path) != ":memory:":
            path = Path(path).expanduser()
            if not path.is_absolute():
                path = Path(__file__).parent / path
            path = path.resolve()
        self.path = str(path)
        self.conn = sqlite3.connect(self.path, timeout=10)
        self.conn.execute("PRAGMA busy_timeout = 10000")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                channel TEXT NOT NULL,
                name TEXT NOT NULL,
                points INTEGER NOT NULL DEFAULT 0,
                last_daily REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (channel, name)
            );
            CREATE TABLE IF NOT EXISTS custom_commands (
                channel TEXT NOT NULL,
                name TEXT NOT NULL,
                response TEXT NOT NULL,
                cooldown_seconds INTEGER NOT NULL DEFAULT 0,
                aliases TEXT NOT NULL DEFAULT '[]',
                PRIMARY KEY (channel, name)
            );
            CREATE TABLE IF NOT EXISTS quotes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel TEXT NOT NULL,
                text TEXT NOT NULL,
                author TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS timers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel TEXT NOT NULL,
                message TEXT NOT NULL,
                interval_minutes INTEGER NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                last_sent REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS antiflood_settings (
                channel TEXT PRIMARY KEY,
                settings TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS custom_banned_words (
                channel TEXT NOT NULL,
                word TEXT NOT NULL,
                PRIMARY KEY (channel, word)
            );
            CREATE TABLE IF NOT EXISTS category_aliases (
                channel TEXT NOT NULL,
                alias TEXT NOT NULL,
                category TEXT NOT NULL,
                PRIMARY KEY (channel, alias)
            );
            CREATE TABLE IF NOT EXISTS custom_variables (
                channel TEXT NOT NULL,
                name TEXT NOT NULL,
                value TEXT NOT NULL,
                description TEXT NOT NULL,
                PRIMARY KEY (channel, name)
            );
            """
        )
        self.conn.commit()
        self.migrate_channel_columns()
        if compact_timers:
            self.compact_timer_ids()

    def legacy_channel(self):
        """Канал, к которому относились общие настройки до разделения по каналам."""
        channels = [
            value.strip().lstrip("#").lower()
            for value in os.getenv("TWITCH_CHANNELS", "").split(",")
            if value.strip()
        ]
        if channels:
            return channels[0]
        try:
            row = self.conn.execute(
                "SELECT channel FROM panel_channels ORDER BY created LIMIT 1"
            ).fetchone()
        except sqlite3.OperationalError:
            row = None
        return row["channel"] if row else ""

    def migrate_channel_columns(self):
        """Добавить колонку channel старым таблицам и отнести записи к legacy_channel."""
        legacy = self.legacy_channel()

        def columns(table):
            return {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")}

        command_columns = columns("custom_commands")
        if "cooldown_seconds" not in command_columns:
            self.conn.execute(
                "ALTER TABLE custom_commands "
                "ADD COLUMN cooldown_seconds INTEGER NOT NULL DEFAULT 0"
            )
        if "aliases" not in command_columns:
            self.conn.execute(
                "ALTER TABLE custom_commands "
                "ADD COLUMN aliases TEXT NOT NULL DEFAULT '[]'"
            )
        if "channel" not in command_columns:
            self.conn.execute("ALTER TABLE custom_commands RENAME TO custom_commands_legacy")
            self.conn.execute(
                "CREATE TABLE custom_commands ("
                "channel TEXT NOT NULL, name TEXT NOT NULL, response TEXT NOT NULL, "
                "cooldown_seconds INTEGER NOT NULL DEFAULT 0, aliases TEXT NOT NULL DEFAULT '[]', "
                "PRIMARY KEY (channel, name))"
            )
            self.conn.execute(
                "INSERT INTO custom_commands(channel, name, response, cooldown_seconds, aliases) "
                "SELECT ?, name, response, cooldown_seconds, aliases FROM custom_commands_legacy",
                (legacy,),
            )
            self.conn.execute("DROP TABLE custom_commands_legacy")
        if "channel" not in columns("users"):
            self.conn.execute("ALTER TABLE users RENAME TO users_legacy")
            self.conn.execute(
                "CREATE TABLE users ("
                "channel TEXT NOT NULL, name TEXT NOT NULL, "
                "points INTEGER NOT NULL DEFAULT 0, last_daily REAL NOT NULL DEFAULT 0, "
                "PRIMARY KEY (channel, name))"
            )
            self.conn.execute(
                "INSERT INTO users(channel, name, points, last_daily) "
                "SELECT ?, name, points, last_daily FROM users_legacy",
                (legacy,),
            )
            self.conn.execute("DROP TABLE users_legacy")
        if "channel" not in columns("quotes"):
            self.conn.execute("ALTER TABLE quotes ADD COLUMN channel TEXT NOT NULL DEFAULT ''")
            self.conn.execute("UPDATE quotes SET channel=? WHERE channel=''", (legacy,))
        if "channel" not in columns("timers"):
            self.conn.execute("ALTER TABLE timers ADD COLUMN channel TEXT NOT NULL DEFAULT ''")
            self.conn.execute("UPDATE timers SET channel=? WHERE channel=''", (legacy,))
        if "channel" not in columns("custom_banned_words"):
            self.conn.execute("ALTER TABLE custom_banned_words RENAME TO custom_banned_words_legacy")
            self.conn.execute(
                "CREATE TABLE custom_banned_words ("
                "channel TEXT NOT NULL, word TEXT NOT NULL, PRIMARY KEY (channel, word))"
            )
            self.conn.execute(
                "INSERT INTO custom_banned_words(channel, word) "
                "SELECT ?, word FROM custom_banned_words_legacy",
                (legacy,),
            )
            self.conn.execute("DROP TABLE custom_banned_words_legacy")
        if "channel" not in columns("category_aliases"):
            self.conn.execute("ALTER TABLE category_aliases RENAME TO category_aliases_legacy")
            self.conn.execute(
                "CREATE TABLE category_aliases ("
                "channel TEXT NOT NULL, alias TEXT NOT NULL, category TEXT NOT NULL, "
                "PRIMARY KEY (channel, alias))"
            )
            self.conn.execute(
                "INSERT INTO category_aliases(channel, alias, category) "
                "SELECT ?, alias, category FROM category_aliases_legacy",
                (legacy,),
            )
            self.conn.execute("DROP TABLE category_aliases_legacy")
        self.conn.commit()

    def get_antiflood_settings(self, channel):
        row = self.conn.execute(
            "SELECT settings FROM antiflood_settings WHERE channel=?", (channel,)
        ).fetchone()
        settings = dict(ANTIFLOOD_DEFAULTS)
        settings["timeouts"] = list(ANTIFLOOD_DEFAULTS["timeouts"])
        if row:
            settings.update(json.loads(row["settings"]))
        return settings

    def set_antiflood_settings(self, channel, settings):
        self.conn.execute(
            "INSERT INTO antiflood_settings(channel, settings) VALUES(?, ?) "
            "ON CONFLICT(channel) DO UPDATE SET settings=excluded.settings",
            (channel, json.dumps(settings, ensure_ascii=False)),
        )
        self.conn.commit()

    # --- очки ---
    def get_points(self, channel, name):
        row = self.conn.execute(
            "SELECT points FROM users WHERE channel=? AND name=?", (channel, name)
        ).fetchone()
        return row["points"] if row else 0

    def add_points(self, channel, name, amount):
        self.conn.execute(
            "INSERT INTO users(channel, name, points) VALUES(?, ?, ?) "
            "ON CONFLICT(channel, name) DO UPDATE SET points = MAX(0, points + ?)",
            (channel, name, max(0, amount), amount),
        )
        self.conn.commit()

    def get_last_daily(self, channel, name):
        row = self.conn.execute(
            "SELECT last_daily FROM users WHERE channel=? AND name=?", (channel, name)
        ).fetchone()
        return row["last_daily"] if row else 0

    def set_last_daily(self, channel, name, ts):
        self.conn.execute(
            "INSERT INTO users(channel, name, last_daily) VALUES(?, ?, ?) "
            "ON CONFLICT(channel, name) DO UPDATE SET last_daily=?",
            (channel, name, ts, ts),
        )
        self.conn.commit()

    def top(self, channel, limit=5):
        return self.conn.execute(
            "SELECT name, points FROM users WHERE channel=? ORDER BY points DESC LIMIT ?",
            (channel, limit),
        ).fetchall()

    # --- кастомные команды ---
    def get_custom(self, channel, name):
        return self.conn.execute(
            "SELECT name, response, cooldown_seconds, aliases FROM custom_commands "
            "WHERE channel=? AND name=?",
            (channel, name),
        ).fetchone()

    def find_custom(self, channel, name):
        command = self.get_custom(channel, name)
        if command:
            return command
        for row in self.conn.execute(
            "SELECT name, response, cooldown_seconds, aliases FROM custom_commands WHERE channel=?",
            (channel,),
        ):
            if name in json.loads(row["aliases"]):
                return row
        return None

    def custom_name_or_alias_exists(self, channel, name, *, except_command=None):
        if self.get_custom(channel, name) and name != except_command:
            return True
        for row in self.conn.execute(
            "SELECT name, aliases FROM custom_commands WHERE channel=?", (channel,)
        ):
            if row["name"] != except_command and name in json.loads(row["aliases"]):
                return True
        return False

    def custom_count(self, channel):
        return self.conn.execute(
            "SELECT COUNT(*) FROM custom_commands WHERE channel=?", (channel,)
        ).fetchone()[0]

    def set_custom(self, channel, name, response, cooldown_seconds=0):
        if not self.get_custom(channel, name) and self.custom_count(channel) >= 30:
            return False
        self.conn.execute(
            "INSERT INTO custom_commands(channel, name, response, cooldown_seconds) VALUES(?, ?, ?, ?) "
            "ON CONFLICT(channel, name) DO UPDATE SET response=?, cooldown_seconds=?",
            (channel, name, response, cooldown_seconds, response, cooldown_seconds),
        )
        self.conn.commit()
        return True

    def set_custom_aliases(self, channel, name, aliases):
        if len(aliases) > 5:
            return False
        self.conn.execute(
            "UPDATE custom_commands SET aliases=? WHERE channel=? AND name=?",
            (json.dumps(aliases, ensure_ascii=False), channel, name),
        )
        self.conn.commit()
        return True

    def del_custom(self, channel, name):
        cur = self.conn.execute(
            "DELETE FROM custom_commands WHERE channel=? AND name=?", (channel, name)
        )
        self.conn.commit()
        return cur.rowcount > 0

    def list_custom(self, channel):
        return [
            row["name"] for row in self.conn.execute(
                "SELECT name FROM custom_commands WHERE channel=? ORDER BY name", (channel,)
            )
        ]

    def list_custom_variables(self, channel):
        return [
            dict(row) for row in self.conn.execute(
                "SELECT name, value, description FROM custom_variables "
                "WHERE channel=? ORDER BY name", (channel,)
            )
        ]

    def list_banned_words(self, channel):
        return [
            row["word"] for row in self.conn.execute(
                "SELECT word FROM custom_banned_words WHERE channel=? ORDER BY word", (channel,)
            )
        ]

    def banned_words_by_channel(self):
        result = {}
        for row in self.conn.execute("SELECT channel, word FROM custom_banned_words"):
            result.setdefault(row["channel"], []).append(row["word"])
        return result

    def add_banned_word(self, channel, word):
        if self.conn.execute(
            "SELECT COUNT(*) FROM custom_banned_words WHERE channel=?", (channel,)
        ).fetchone()[0] >= 30:
            return False
        try:
            self.conn.execute(
                "INSERT INTO custom_banned_words(channel, word) VALUES(?, ?)", (channel, word)
            )
        except sqlite3.IntegrityError:
            return False
        self.conn.commit()
        return True

    def delete_banned_word(self, channel, word):
        cursor = self.conn.execute(
            "DELETE FROM custom_banned_words WHERE channel=? AND word=?", (channel, word)
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def get_category_alias(self, channel, alias):
        row = self.conn.execute(
            "SELECT category FROM category_aliases WHERE channel=? AND alias=?", (channel, alias)
        ).fetchone()
        return row["category"] if row else None

    def list_category_aliases(self, channel):
        return self.conn.execute(
            "SELECT alias, category FROM category_aliases WHERE channel=? ORDER BY alias", (channel,)
        ).fetchall()

    def set_category_alias(self, channel, alias, category):
        exists = self.conn.execute(
            "SELECT 1 FROM category_aliases WHERE channel=? AND alias=?", (channel, alias)
        ).fetchone()
        count = self.conn.execute(
            "SELECT COUNT(*) FROM category_aliases WHERE channel=?", (channel,)
        ).fetchone()[0]
        if not exists and count >= 20:
            return False
        self.conn.execute(
            "INSERT INTO category_aliases(channel, alias, category) VALUES(?, ?, ?) "
            "ON CONFLICT(channel, alias) DO UPDATE SET category=excluded.category",
            (channel, alias, category),
        )
        self.conn.commit()
        return True

    def delete_category_alias(self, channel, alias):
        cursor = self.conn.execute(
            "DELETE FROM category_aliases WHERE channel=? AND alias=?", (channel, alias)
        )
        self.conn.commit()
        return cursor.rowcount > 0

    # --- цитаты ---
    def add_quote(self, channel, text, author):
        cur = self.conn.execute(
            "INSERT INTO quotes(channel, text, author) VALUES(?, ?, ?)", (channel, text, author)
        )
        self.conn.commit()
        return cur.lastrowid

    def get_quote(self, channel, quote_id=None):
        if quote_id is not None:
            return self.conn.execute(
                "SELECT * FROM quotes WHERE channel=? AND id=?", (channel, quote_id)
            ).fetchone()
        return self.conn.execute(
            "SELECT * FROM quotes WHERE channel=? ORDER BY RANDOM() LIMIT 1", (channel,)
        ).fetchone()

    # --- пользовательские таймеры ---
    def compact_timer_ids(self):
        rows = self.conn.execute("SELECT id FROM timers ORDER BY id").fetchall()
        if not rows:
            self.conn.execute("DELETE FROM sqlite_sequence WHERE name='timers'")
            self.conn.commit()
            return

        offset = rows[-1]["id"] + len(rows)
        temporary_ids = []
        for row in rows:
            temporary_id = row["id"] + offset
            self.conn.execute(
                "UPDATE timers SET id=? WHERE id=?",
                (temporary_id, row["id"]),
            )
            temporary_ids.append(temporary_id)

        self.conn.executemany(
            "UPDATE timers SET id=? WHERE id=?",
            [(new_id, temporary_id) for new_id, temporary_id in enumerate(temporary_ids, 1)],
        )
        cursor = self.conn.execute(
            "UPDATE sqlite_sequence SET seq=? WHERE name='timers'",
            (len(rows),),
        )
        if cursor.rowcount == 0:
            self.conn.execute(
                "INSERT INTO sqlite_sequence(name, seq) VALUES('timers', ?)",
                (len(rows),),
            )
        self.conn.commit()

    def add_timer(self, channel, message, interval_minutes):
        active_count = self.conn.execute(
            "SELECT COUNT(*) FROM timers WHERE channel=? AND enabled=1", (channel,)
        ).fetchone()[0]
        if active_count >= 5:
            return None
        cur = self.conn.execute(
            "INSERT INTO timers(channel, message, interval_minutes, last_sent) VALUES(?, ?, ?, ?)",
            (channel, message, interval_minutes, time.time()),
        )
        self.conn.commit()
        return cur.lastrowid

    def list_timers(self, channel):
        return self.conn.execute(
            "SELECT id, message, interval_minutes, enabled FROM timers WHERE channel=? ORDER BY id",
            (channel,),
        ).fetchall()

    def delete_timer(self, timer_id):
        cur = self.conn.execute("DELETE FROM timers WHERE id=?", (timer_id,))
        self.conn.commit()
        if cur.rowcount > 0:
            self.compact_timer_ids()
        return cur.rowcount > 0

    def set_timer_enabled(self, timer_id, enabled):
        row = self.conn.execute("SELECT channel FROM timers WHERE id=?", (timer_id,)).fetchone()
        if row is None:
            return False
        if enabled:
            active_count = self.conn.execute(
                "SELECT COUNT(*) FROM timers WHERE channel=? AND enabled=1 AND id<>?",
                (row["channel"], timer_id),
            ).fetchone()[0]
            if active_count >= 5:
                return False
            cur = self.conn.execute(
                "UPDATE timers SET enabled=1, last_sent=? WHERE id=?",
                (time.time(), timer_id),
            )
        else:
            cur = self.conn.execute(
                "UPDATE timers SET enabled=0 WHERE id=?",
                (timer_id,),
            )
        self.conn.commit()
        return cur.rowcount > 0

    def due_timers(self, now, extra_seconds=0):
        rows = self.conn.execute(
            "SELECT id, channel, message FROM timers "
            "WHERE enabled=1 AND last_sent + interval_minutes * 60 + ? <= ?",
            (extra_seconds, now),
        ).fetchall()
        if rows:
            self.conn.executemany(
                "UPDATE timers SET last_sent=? WHERE id=?",
                [(now, row["id"]) for row in rows],
            )
            self.conn.commit()
        return rows


def is_privileged(author):
    return bool(author.is_mod or author.is_broadcaster)


def needs_query(parts):
    return any(
        isinstance(part, Variable)
        and (part.name in ("query", "index", "fromindex") or needs_query(part.argument))
        for part in parts
    )


async def validate_token(token):
    """Проверяет токен у Twitch. Возвращает словарь с данными или None."""
    raw = token.removeprefix("oauth:")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://id.twitch.tv/oauth2/validate",
                headers={"Authorization": f"OAuth {raw}"},
            ) as resp:
                if resp.status != 200:
                    return None
                return await resp.json()
    except Exception as e:
        log(f"Не удалось связаться с Twitch для проверки токена: {e!r}")
        return None


class Bot(commands.Bot):
    def __init__(self, token, client_id, moderator_id, scopes, *, db_path=None):
        super().__init__(
            token=token,
            client_id=client_id,
            prefix=PREFIX,
            initial_channels=CHANNELS,
        )
        self.access_token = token.removeprefix("oauth:")
        self.client_id = client_id
        self.http_session = None
        self.moderator_id = str(moderator_id)
        self.scopes = set(scopes)
        self.twitch_id_cache = {}
        self.db = Database(db_path, compact_timers=False)
        self.background_tasks = set()
        self._resources_closed = False
        self.active = {}      # (канал, имя) -> время последнего сообщения
        self.permits = {}     # имя -> до какого времени разрешены ссылки
        self.tasks_started = False
        self.greeted = set()
        self.recent_messages = deque(maxlen=500)
        self.mass_actions = {}
        self.custom_command_uses = {}
        self.repeat_uses = {}
        self.antiflood_history = {}
        self.antiflood_violations = {}
        self.custom_banned_words = self.db.banned_words_by_channel()
        self.banned_words_patterns = {}
        self._database_version = self.db.conn.execute("PRAGMA data_version").fetchone()[0]
        self.streams_online = None
        self.stream_status_checked_at = -STREAM_STATUS_INTERVAL
        self.weather_service = WeatherService(self)
        self.variable_services = VariableServices(self)

    def compile_banned_words_pattern(self, words=None):
        if words is None:
            words = BANNED_WORDS
        words = sorted(set(words), key=len, reverse=True)
        if not words:
            return None
        alternatives = "|".join(re.escape(word) for word in words)
        return re.compile(rf"(?<!\w)(?:{alternatives})(?!\w)", re.IGNORECASE)

    def banned_words_pattern(self, channel):
        """Фильтр запрещённых слов конкретного канала (встроенные + свои)."""
        if channel not in self.banned_words_patterns:
            words = BANNED_WORDS | set(self.custom_banned_words.get(channel, ()))
            self.banned_words_patterns[channel] = self.compile_banned_words_pattern(words)
        return self.banned_words_patterns[channel]

    def refresh_banned_words(self):
        """Notice changes committed by the web panel or another SQLite connection."""
        version = self.db.conn.execute("PRAGMA data_version").fetchone()[0]
        if version != self._database_version:
            self.custom_banned_words = self.db.banned_words_by_channel()
            self.banned_words_patterns = {}
            self._database_version = version

    def create_background_task(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.background_tasks.add(task)
        task.add_done_callback(self.background_tasks.discard)
        return task

    async def twitch_api(self, method, endpoint, *, params=None, payload=None):
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Client-Id": self.client_id,
        }
        if payload is not None:
            headers["Content-Type"] = "application/json"
        if self.http_session is None or self.http_session.closed:
            self.http_session = aiohttp.ClientSession()
        async with self.http_session.request(
            method,
            f"https://api.twitch.tv/helix/{endpoint}",
            headers=headers,
            params=params,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as response:
            if response.status == 204:
                return {}
            data = await response.json(content_type=None)
            if response.status != 200:
                message = data.get("message", f"HTTP {response.status}")
                raise RuntimeError(message)
            return data

    async def close(self):
        if self._resources_closed:
            return
        self._resources_closed = True
        tasks = tuple(self.background_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.background_tasks.clear()
        self.mass_actions.clear()
        try:
            if self._closing is None:
                self._closing = asyncio.Event()
            # TwitchIO 2.x assumes its keep-alive task already exists in close().
            # A panel can cancel while the initial network connection is pending.
            if self._connection._keeper is not None:
                await super().close()
            else:
                self._closing.set()
                self._connection.is_ready.clear()
                if self._connection._websocket is not None:
                    await self._connection._websocket.close()
                if self._http.session is not None and not self._http.session.closed:
                    await self._http.session.close()
        finally:
            try:
                if self.http_session is not None and not self.http_session.closed:
                    await self.http_session.close()
            finally:
                self.db.conn.close()

    async def get_twitch_user_id(self, login):
        login = login.strip().lstrip("@").lower()
        if login not in self.twitch_id_cache:
            result = await self.twitch_api(
                "GET", "users", params={"login": login}
            )
            users = result.get("data", [])
            if not users:
                raise ValueError(f"Пользователь Twitch @{login} не найден.")
            self.twitch_id_cache[login] = str(users[0]["id"])
        return self.twitch_id_cache[login]

    async def are_streams_online(self):
        now = time.monotonic()
        if now - self.stream_status_checked_at >= STREAM_STATUS_INTERVAL:
            params = [("user_login", channel) for channel in CHANNELS]
            result = await self.twitch_api("GET", "streams", params=params)
            streams = {item["user_login"].lower(): item for item in result.get("data", [])}
            for channel in CHANNELS:
                self.variable_services.observe_stream(channel, streams.get(channel))
            self.streams_online = bool(result.get("data"))
            self.stream_status_checked_at = now
        return self.streams_online

    async def get_weather(self, city):
        return await self.weather_service.get_weather(city)

    @staticmethod
    def format_elapsed(seconds):
        seconds = max(0, int(seconds))
        days, seconds = divmod(seconds, 86400)
        hours, seconds = divmod(seconds, 3600)
        minutes = seconds // 60
        parts = []

        def russian_unit(value, forms):
            remainder_100 = value % 100
            remainder_10 = value % 10
            if 11 <= remainder_100 <= 14:
                form = forms[2]
            elif remainder_10 == 1:
                form = forms[0]
            elif 2 <= remainder_10 <= 4:
                form = forms[1]
            else:
                form = forms[2]
            return f"{value} {form}"

        years, days = divmod(days, 365)
        months, days = divmod(days, 30)
        if years:
            parts.append(russian_unit(years, ("год", "года", "лет")))
        if months:
            parts.append(russian_unit(months, ("месяц", "месяца", "месяцев")))
        if days:
            parts.append(russian_unit(days, ("день", "дня", "дней")))
        if not parts:
            if hours:
                parts.append(russian_unit(hours, ("час", "часа", "часов")))
            parts.append(russian_unit(minutes, ("минута", "минуты", "минут")))
        return " ".join(parts)

    @staticmethod
    def calculate_math(expression):
        if len(expression) > 100:
            raise ValueError("выражение слишком длинное")
        operators = {
            ast.Add: lambda a, b: a + b,
            ast.Sub: lambda a, b: a - b,
            ast.Mult: lambda a, b: a * b,
            ast.Div: lambda a, b: a / b,
            ast.FloorDiv: lambda a, b: a // b,
            ast.Mod: lambda a, b: a % b,
            ast.Pow: lambda a, b: a ** b,
        }

        def evaluate(node):
            if isinstance(node, ast.Expression):
                return evaluate(node.body)
            if isinstance(node, ast.Constant) and type(node.value) in (int, float):
                return node.value
            if isinstance(node, ast.BinOp) and type(node.op) in operators:
                left, right = evaluate(node.left), evaluate(node.right)
                if abs(left) > 1e100 or abs(right) > 1e100:
                    raise ValueError("число слишком велико")
                if isinstance(node.op, ast.Pow) and abs(right) > 1000:
                    raise ValueError("степень должна быть не больше 1000")
                return operators[type(node.op)](left, right)
            if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
                value = evaluate(node.operand)
                return value if isinstance(node.op, ast.UAdd) else -value
            raise ValueError("допустимы только арифметические операции и числа")

        value = evaluate(ast.parse(expression, mode="eval"))
        if type(value) not in (int, float) or not math.isfinite(value) or abs(value) > 1e100:
            raise ValueError("результат слишком велик")
        return int(value) if isinstance(value, float) and value.is_integer() else value

    @staticmethod
    def parse_variable_datetime(value):
        value = value.strip()
        value = re.sub(r"\b(\d{1,2})(?:st|nd|rd|th)\b", r"\1", value, flags=re.I)
        months = (
            "january", "february", "march", "april", "may", "june",
            "july", "august", "september", "october", "november", "december",
        )
        month_numbers = {name: index for index, name in enumerate(months, 1)}
        month_numbers.update({name[:3]: index for index, name in enumerate(months, 1)})
        english_date = re.match(
            r"^(?:([a-zA-Z]+)\s+(\d{1,2})|(\d{1,2})\s+([a-zA-Z]+))\s+(\d{4})(.*)$",
            value,
        )
        if english_date:
            month = (english_date[1] or english_date[4]).casefold()
            if month not in month_numbers:
                raise ValueError("неизвестное название месяца")
            day = int(english_date[2] or english_date[3])
            value = f"{english_date[5]}-{month_numbers[month]:02d}-{day:02d}{english_date[6]}"
        value = re.sub(r"^(\d{4})\.(\d{2})\.(\d{2})", r"\1-\2-\3", value)
        value = re.sub(r"\s+([+-]\d{2}:?\d{2})$", r"\1", value)
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
        for date_format in ("%Y-%m-%d %I:%M:%S%p%z", "%Y-%m-%d %I:%M%p%z",
                            "%Y-%m-%d %I:%M:%S%p", "%Y-%m-%d %I:%M%p"):
            try:
                return datetime.strptime(value.upper(), date_format)
            except ValueError:
                continue
        raise ValueError("укажи дату YYYY-MM-DD или 01 Jan 2030 и время с часовым поясом")

    async def resolve_custom_variable(
        self, name, argument, *, username, channel_name, query, channel, selected_chatters,
        tags=None,
    ):
        if name in ("sender", "user"):
            if not username:
                raise SenderRequired("переменная {sender} недоступна в таймере")
            return username
        if name == "channel":
            return channel_name
        if name == "randint":
            try:
                lower, upper = map(int, argument.split())
                if lower > upper or max(abs(lower), abs(upper)) > 10**12:
                    raise ValueError
                return str(random.randint(lower, upper))
            except (ValueError, TypeError):
                return "укажи диапазон: {randint 1 100} (числа от -10^12 до 10^12)"
        if name in ("weather", "horoscope"):
            # A viewer's city/sign overrides the value saved in a command template.
            target = " ".join((query or "").split()) or " ".join(argument.split())
            if not target:
                if name == "weather":
                    return f"укажи город после команды, например {PREFIX}weather Казань"
                return f"укажи знак зодиака после команды, например {PREFIX}гороскоп дева"
            if name == "weather":
                return await self.get_weather(target)
            return await self.variable_services.resolve(
                name, target, username=username or "", channel_name=channel_name, tags=tags,
            )
        if name == "math":
            try:
                return str(self.calculate_math(argument))
            except (SyntaxError, ValueError, ArithmeticError, RecursionError) as error:
                return f"ошибка в выражении: {error}"
        if name in ("countdown", "countup"):
            try:
                target = self.parse_variable_datetime(argument)
                if target.tzinfo is None:
                    target = target.astimezone()
                seconds = (target - datetime.now(target.tzinfo)).total_seconds()
                if name == "countup":
                    seconds = -seconds
                if seconds < 0:
                    return "дата уже прошла" if name == "countdown" else "дата ещё не наступила"
                return self.format_elapsed(seconds)
            except (ValueError, OverflowError, OSError) as error:
                return str(error)
        if name == "chatter":
            if not selected_chatters:
                bot_nick = (self.nick or "").casefold()
                chatters = {
                    chatter.name for chatter in (getattr(channel, "chatters", None) or [])
                    if chatter.name.casefold() != bot_nick
                }
                chatters.update(
                    user for ch, user, *_ in self.recent_messages
                    if ch == channel_name and user.casefold() != bot_nick
                )
                if not chatters:
                    return "нет данных об участниках чата"
                selected_chatters.extend(random.sample(sorted(chatters), len(chatters)))
            try:
                index = int(argument or "1") - 1
                if index < 0:
                    raise ValueError
            except ValueError:
                return "укажи номер участника: {chatter 1}"
            return selected_chatters[index % len(selected_chatters)]
        if name == "commands":
            mode = argument.casefold() or "public"
            if mode not in ("public", "private", "offline", "always"):
                return "укажи тип: {commands public/private/offline/always}"
            # Existing custom commands are public and work both online and offline.
            available = sorted(self.db.list_custom(channel_name)) if mode in ("public", "always") else []
            labels = {"public": "Публичные", "private": "Приватные", "offline": "Офлайн", "always": "Постоянные"}
            return labels[mode] + " команды: " + (", ".join(f"{PREFIX}{item}" for item in available) or "нет")
        return await self.variable_services.resolve(
            name, argument, username=username or "", channel_name=channel_name, tags=tags,
        )

    async def render_custom_response(
        self, template, *, username, channel_name, query="", channel=None, tags=None
    ):
        selected_chatters = []
        custom_variables = {
            item["name"]: item["value"]
            for item in self.db.list_custom_variables(channel_name)
        }

        async def resolve(name, argument):
            if name in custom_variables:
                return custom_variables[name]
            return await self.resolve_custom_variable(
                name, argument, username=username, channel_name=channel_name,
                query=query, channel=channel, selected_chatters=selected_chatters, tags=tags,
            )

        return await render_template(
            template, resolve, query=query, username=username,
            custom_variable_names=custom_variables,
        )

    @staticmethod
    async def send_variable_response(channel, text):
        # Twitch limits chat message length; large lists still need to be complete.
        remaining = " ".join(str(text).split())
        while remaining:
            end = len(remaining) if len(remaining) <= 450 else remaining.rfind(" ", 0, 451)
            if end <= 0:
                end = 450
            await channel.send(remaining[:end])
            remaining = remaining[end:].lstrip()

    async def send_timer_message(self, channel, template):
        custom_variable_names = {
            item["name"] for item in self.db.list_custom_variables(channel.name.lower())
        }
        if contains_variable(parse_template(template, custom_variable_names), "spam"):
            return False
        try:
            response = await self.render_custom_response(
                template, username=None, channel_name=channel.name.lower(), channel=channel,
            )
        except SenderRequired:
            return False
        await self.send_variable_response(channel, response)
        return True

    async def moderate_user(self, channel_name, username, *, duration=None, reason):
        if "moderator:manage:banned_users" not in self.scopes:
            raise RuntimeError(
                "токену необходимо разрешение moderator:manage:banned_users"
            )
        broadcaster_id = await self.get_twitch_user_id(channel_name)
        target_id = await self.get_twitch_user_id(username)
        params = {
            "broadcaster_id": broadcaster_id,
            "moderator_id": self.moderator_id,
        }
        ban_data = {"user_id": target_id, "reason": reason}
        if duration is not None:
            ban_data["duration"] = duration
        await self.twitch_api(
            "POST",
            "moderation/bans",
            params=params,
            payload={"data": ban_data},
        )

    # ---------- события ----------
    async def event_ready(self):
        log(f"[OK] Бот подключён как {self.nick}. Входим в каналы: {', '.join(CHANNELS)}")
        if not self.tasks_started:
            self.tasks_started = True
            self.create_background_task(self.points_loop())
            self.create_background_task(self.timer_loop())

    async def event_channel_joined(self, channel):
        log(f"[OK] Вошёл в чат #{channel.name}")
        if GREETING and channel.name not in self.greeted:
            self.greeted.add(channel.name)
            try:
                await channel.send(GREETING)
            except Exception as e:
                log(f"[ОШИБКА] Не удалось отправить приветствие: {e!r}")

    async def event_raw_data(self, data):
        # Служебные ответы Twitch: причины, почему сообщение не отправилось
        if " NOTICE " in data or "RECONNECT" in data:
            log(f"[TWITCH] {data.strip()}")

    async def event_error(self, error, data=None):
        log(f"[ОШИБКА] {error!r}")

    async def event_message(self, message):
        try:
            if DEBUG:
                who = message.author.name if message.author else "(нет автора)"
                log(f"[чат] {who}: {message.content} (echo={message.echo})")

            if message.echo or message.author is None:
                return

            author = message.author
            name = author.name.lower()
            privileged = is_privileged(author)
            channel_name = message.channel.name.lower()
            self.active[(channel_name, name)] = time.time()
            self.recent_messages.append(
                (channel_name, name, message.content, privileged)
            )

            action = self.mass_actions.get(channel_name)
            if (
                action
                and not privileged
                and name != self.nick.lower()
                and not message.content.lstrip().startswith(PREFIX)
                and action["phrase"] in message.content.casefold()
                and name not in action["users"]
                and len(action["users"]) < 70
            ):
                action["users"].add(name)
                action["queue"].put_nowait(name)

            # Модераторов и стримера не трогаем.
            self.refresh_banned_words()
            banned_pattern = self.banned_words_pattern(channel_name)
            banned_match = (
                banned_pattern.search(message.content)
                if banned_pattern else None
            )
            if banned_match:
                if privileged:
                    log(
                        f"[BANWORD] @{name}: совпало слово из фильтра, "
                        "но сообщение пропущено (автор отмечен модератором/владельцем)."
                    )
                    return
                log(
                    f"[BANWORD] Совпадение: @{name}, слово "
                    f"{banned_match.group(0)!r}, канал #{channel_name}"
                )
                try:
                    await self.moderate_user(
                        channel_name,
                        author.name,
                        reason="Использование запрещённого слова",
                    )
                    log(f"[MOD] @{name} забанен за запрещённое слово.")
                except Exception as error:
                    log(f"[ОШИБКА модерации] Не удалось забанить @{name}: {error!r}")
                return

            if await self.handle_antiflood(message, channel_name, name, privileged):
                return

            if not privileged:
                warning = self.check_moderation(message)
                if warning:
                    await message.channel.send(f"@{author.name}, {warning}")
                    return

            original_content = message.content
            repeat_match = None
            if original_content.lstrip().startswith(PREFIX):
                repeat_match = re.search(
                    r"\s+x(\d+)\s*$", original_content, re.IGNORECASE
                )
            repeat_count = 1
            if repeat_match:
                raw_count = repeat_match.group(1)
                if len(raw_count) > 2 or not 1 <= int(raw_count) <= 15:
                    await message.channel.send("Количество повторов должно быть от 1 до 15.")
                    return
                repeat_count = int(raw_count)
                if repeat_count > 1 and not privileged:
                    await message.channel.send(
                        "Повторять команды могут только модераторы и владелец канала."
                    )
                    return
                message.content = original_content[:repeat_match.start()].rstrip()

            try:
                await self.process_command_message(
                    message, channel_name, name, repeat_count
                )
            finally:
                message.content = original_content
        except Exception as e:
            log(f"[ОШИБКА в event_message] {e!r}")

    async def process_command_message(self, message, channel_name, name, repeat_count):
        content = message.content.strip()
        context = None
        if content.startswith(PREFIX):
            command_parts = content[len(PREFIX):].split(maxsplit=1)
            cmd_name = command_parts[0].casefold() if command_parts else ""
            builtin_command = self.get_command(cmd_name) if cmd_name else None
            if (
                builtin_command
                and len(command_parts) > 1
                and command_parts[1].isdecimal()
                and 2 <= int(command_parts[1]) <= 15
                and is_privileged(message.author)
            ):
                parameters = list(getattr(builtin_command, "params", {}).values())
                context_parameter_count = (
                    2 if getattr(builtin_command, "_instance", None) else 1
                )
                if len(parameters) <= context_parameter_count:
                    repeat_count = int(command_parts[1])
                    original_content = message.content
                    message.content = f"{PREFIX}{cmd_name}"
                    try:
                        context = await self.get_context(message)
                    finally:
                        message.content = original_content
                else:
                    context = None
            if cmd_name and builtin_command is None:
                custom_command = self.db.find_custom(channel_name, cmd_name)
                if custom_command:
                    query = command_parts[1] if len(command_parts) > 1 else ""
                    try:
                        custom_variable_names = {
                            item["name"] for item in self.db.list_custom_variables(channel_name)
                        }
                        template_parts = parse_template(
                            custom_command["response"], custom_variable_names
                        )
                        uses_query = needs_query(template_parts)
                        uses_spam = contains_variable(template_parts, "spam")
                    except TemplateError as error:
                        await message.channel.send(f"Ошибка переменных: {error}")
                        return
                    if uses_spam and query:
                        query_parts = query.rsplit(maxsplit=1)
                        if query_parts[-1].isdecimal():
                            requested_count = int(query_parts[-1])
                            if not 1 <= requested_count <= 15:
                                await message.channel.send(
                                    "Количество повторов должно быть от 1 до 15."
                                )
                                return
                            if requested_count > 1 and not is_privileged(message.author):
                                await message.channel.send(
                                    "Повторять команды могут только модераторы и владелец канала."
                                )
                                return
                            repeat_count = requested_count
                            query = query_parts[0] if len(query_parts) > 1 else ""
                    if (
                        not uses_spam
                        and is_privileged(message.author)
                        and query.isdecimal()
                        and 2 <= int(query) <= 15
                        and not uses_query
                    ):
                        repeat_count = int(query)
                        query = ""
                    if not await self.check_repeat_cooldown(
                        message, channel_name, name, repeat_count
                    ):
                        return
                    cooldown = custom_command["cooldown_seconds"]
                    cooldown_key = (channel_name, name, custom_command["name"])
                    remaining = (
                        cooldown
                        - (time.time() - self.custom_command_uses.get(cooldown_key, 0))
                    )
                    if remaining > 0:
                        await message.channel.send(
                            f"@{message.author.name}, команда будет доступна через "
                            f"{int(remaining + 0.999)} сек."
                        )
                        return
                    try:
                        response = await self.render_custom_response(
                            custom_command["response"],
                            username=message.author.name,
                            channel_name=channel_name,
                            query=query,
                            channel=message.channel,
                            tags=getattr(message, "tags", None),
                        )
                    except TemplateError as error:
                        await message.channel.send(f"Ошибка переменных: {error}")
                        return
                    for _ in range(repeat_count):
                        await self.send_variable_response(message.channel, response)
                    if cooldown:
                        self.custom_command_uses[cooldown_key] = time.time()
                    return

        if context is None:
            context = await self.get_context(message)
        if not context.command:
            return
        if not await self.check_repeat_cooldown(
            message, channel_name, name, repeat_count
        ):
            return
        await self.invoke(context)
        if not context.command or context.args is None:
            return
        for _ in range(repeat_count - 1):
            try:
                args = [context.command._instance, context] if context.command._instance else [context]
                await context.command._callback(*args, *context.args, **context.kwargs)
            except Exception as error:
                log(f"[ОШИБКА повторной команды] {error!r}")
                await self.event_command_error(context, error)
                return

    async def check_repeat_cooldown(self, message, channel_name, username, repeat_count):
        if repeat_count <= 1:
            return True
        key = (channel_name, username)
        now = time.time()
        remaining = 3 - (now - self.repeat_uses.get(key, 0))
        if remaining > 0:
            await message.channel.send(
                f"@{message.author.name}, повторная команда будет доступна через "
                f"{int(remaining + 0.999)} сек."
            )
            return False
        self.repeat_uses[key] = now
        return True

    def antiflood_settings(self, channel_name):
        return self.db.get_antiflood_settings(channel_name)

    @staticmethod
    def visual_message_size(text):
        size = 0
        for character in text:
            category = unicodedata.category(character)
            if category.startswith("M") or category.startswith("C"):
                continue
            codepoint = ord(character)
            if (
                unicodedata.east_asian_width(character) in ("W", "F")
                or codepoint >= 0x1F000
                or 0x2600 <= codepoint <= 0x27BF
            ):
                size += 18
            else:
                size += 9
        return size

    async def delete_chat_message(self, message):
        if "moderator:manage:chat_messages" not in self.scopes:
            log("[ANTIFLOOD] Нельзя удалить сообщение: токену нужен scope moderator:manage:chat_messages.")
            return False
        if not message.id:
            log("[ANTIFLOOD] Нельзя удалить сообщение: Twitch не передал ID сообщения.")
            return False
        await self.twitch_api(
            "DELETE",
            "moderation/chat",
            params={
                "broadcaster_id": await self.get_twitch_user_id(message.channel.name),
                "moderator_id": self.moderator_id,
                "message_id": message.id,
            },
        )
        return True

    async def handle_antiflood(self, message, channel_name, username, privileged):
        settings = self.antiflood_settings(channel_name)
        if (
            not settings["enabled"]
            or privileged
            or username == self.nick.lower()
            or self.permits.get(username, 0) > time.time()
        ):
            return False

        text = message.content
        now = time.time()
        size = self.visual_message_size(text)
        history_key = (channel_name, username)
        history = self.antiflood_history.setdefault(history_key, deque())
        while history and now - history[0][0] > max(12, settings["spam_seconds"]):
            history.popleft()
        normalized = re.sub(r"\s+", " ", text.casefold()).strip()
        recent_spam = [
            item for item in history if now - item[0] <= settings["spam_seconds"]
        ]

        reason = None
        delete_message = False
        url_matches = list(re.finditer(
            r"(?:https?://|www\.)\S+|\b[a-z0-9-]+\.(?:com|ru|net|org|io|gg|tv|ly)(?:/\S*)?",
            text,
            re.IGNORECASE,
        ))
        if any(match.end() - match.start() > settings["max_link_length"] for match in url_matches):
            reason = "слишком длинная ссылка"
            delete_message = True
        elif settings["video"] and not (
            getattr(message.author, "is_vip", False)
            or getattr(message.author, "is_subscriber", False)
        ) and re.search(
            r"(youtube\.com|youtu\.be|tiktok\.com|clips\.twitch\.tv|twitch\.tv/\S+/clip)",
            text,
            re.IGNORECASE,
        ):
            reason = "ссылка на видео запрещена"
        elif settings["size"] and size > settings["size"] * {1: 1.5, 2: 1, 3: 0.75}[settings["mode"]]:
            reason = "слишком большое сообщение"
        elif settings["spam_messages"] and len(recent_spam) + 1 >= settings["spam_messages"]:
            other_size = sum(
                self.visual_message_size(content)
                for channel, user, content, _ in list(self.recent_messages)[-100:]
                if channel == channel_name and user != username
            )
            user_size = sum(item[1] for item in history)
            if not settings["percent"] or user_size >= other_size * settings["percent"] / 100:
                reason = "слишком много сообщений за короткое время"

        if reason is None:
            repeated = sum(
                1 for timestamp, _, previous in history
                if now - timestamp <= 5 and previous == normalized
            )
            if normalized and repeated >= 3:
                reason = "повтор одинакового сообщения"
            # Два-три повтора бывают в обычной речи ("чуть чуть", "нет нет нет").
            # Считаем флудом от четырёх повторов, не совпадая с частями слов.
            elif re.search(r"(?<!\w)(\S.{1,38}?\S)(?:\s+\1){3,}(?!\w)", normalized):
                reason = "повтор фразы"
            elif settings["main_size"] and sum(
                prev_size >= settings["main_size"] and previous == normalized
                for _, prev_size, previous in history
            ) >= 3:
                reason = "повтор сообщения"

        if reason is None:
            letters = [character for character in text if character.isalpha()]
            emoji_count = sum(
                1 for character in text
                if ord(character) >= 0x1F000 or 0x2600 <= ord(character) <= 0x27BF
            )
            caps_ratio = (
                sum(1 for character in letters if character.isupper()) / len(letters)
                if letters else 0
            )
            mode = settings["mode"]
            # An emotional message is not a flood: require three CAPS messages
            # from this user in this channel within ten seconds.
            caps_store = getattr(self, "antiflood_caps", None)
            if caps_store is None:
                caps_store = self.antiflood_caps = {}
            caps_history = caps_store.get(history_key, deque())
            while caps_history and now - caps_history[0] > 10:
                caps_history.popleft()
            is_caps = len(letters) >= MIN_CAPS_LEN and caps_ratio >= 0.85
            if is_caps:
                caps_history.append(now)
                caps_store[history_key] = caps_history
            elif not caps_history:
                caps_store.pop(history_key, None)
            if is_caps and len(caps_history) >= 3:
                reason = "слишком много заглавных букв"
            elif emoji_count >= (12 - mode * 2) and emoji_count / max(1, len(text)) > 0.5:
                reason = "слишком много эмодзи"
            elif settings["lang"] and any(
                any(script in unicodedata.name(character, "") for script in (
                    "CJK", "HIRAGANA", "KATAKANA", "HANGUL", "ARABIC", "DEVANAGARI"
                ))
                for character in text
            ):
                reason = "сообщение содержит запрещённый алфавит"

        history.append((now, size, normalized))
        if reason is None:
            return False

        violation_key = (channel_name, username)
        violations = self.antiflood_violations.setdefault(violation_key, deque())
        while violations and now - violations[0] > 600:
            violations.popleft()
        durations = settings["timeouts"]
        duration = durations[min(len(violations), len(durations) - 1)]

        # Several incoming messages may arrive while the Twitch request is pending.
        pending = getattr(self, "antiflood_pending", None)
        if pending is None:
            pending = self.antiflood_pending = set()
        if violation_key in pending:
            return True
        pending.add(violation_key)

        try:
            if delete_message:
                try:
                    await self.delete_chat_message(message)
                except Exception as error:
                    log(f"[ANTIFLOOD] Ошибка удаления сообщения @{username}: {error!r}")
            await self.moderate_user(
                channel_name,
                message.author.name,
                duration=duration,
                reason=f"Антифлуд: {reason}",
            )
            violations.append(now)
            history.clear()
            getattr(self, "antiflood_caps", {}).pop(history_key, None)
            log(
                f"[ANTIFLOOD] @{username} замучен на {duration} сек. "
                f"в #{channel_name}: {reason}"
            )
        except Exception as error:
            log(f"[ОШИБКА антифлуда] Не удалось замутить @{username}: {error!r}")
        finally:
            pending.discard(violation_key)
        return True

    async def event_command_error(self, ctx, error):
        if isinstance(error, commands.CommandNotFound):
            return
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f"@{ctx.author.name}, подожди {error.retry_after:.0f} сек.")
            return
        if isinstance(error, commands.MissingRequiredArgument):
            if ctx.command.name == "addcmd":
                await ctx.send(
                    f"Использование: {PREFIX}addcmd <имя> [кд_секунды] <текст ответа>"
                )
                return
            await ctx.send(f"@{ctx.author.name}, не хватает аргумента: {error.name}")
            return
        if isinstance(error, commands.BadArgument):
            await ctx.send(f"@{ctx.author.name}, неверный аргумент.")
            return
        log(f"[ОШИБКА команды] {error!r}")

    # ---------- модерация ----------
    def check_moderation(self, message):
        text = message.content
        name = message.author.name.lower()

        if LINK_REGEX.search(text) and self.permits.get(name, 0) < time.time():
            return "ссылки без разрешения модератора запрещены."

        letters = [c for c in text if c.isalpha()]
        if len(letters) >= MIN_CAPS_LEN:
            caps = sum(1 for c in letters if c.isupper())
            if caps / len(letters) > MAX_CAPS_RATIO:
                return "поменьше капса, пожалуйста."
        return None

    async def mass_action_worker(self, channel_name, action):
        channel = self.get_channel(channel_name)
        if channel is None:
            log(f"[ОШИБКА massban] Не найден канал #{channel_name}")
            self.mass_actions.pop(channel_name, None)
            return

        try:
            while True:
                username = await action["queue"].get()
                try:
                    await self.moderate_user(
                        channel_name,
                        username,
                        duration=action["duration"] if action["type"] == "timeout" else None,
                        reason="Совпадение с масс-фразой",
                    )
                    log(f"[MASS {action['type']}] @{username} в #{channel_name}")
                except Exception as error:
                    log(f"[ОШИБКА massban] Не удалось обработать @{username}: {error!r}")
                finally:
                    action["queue"].task_done()
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            raise
        finally:
            if self.mass_actions.get(channel_name) is action:
                self.mass_actions.pop(channel_name, None)

    async def start_mass_action(self, ctx, action_type, phrase, duration=300):
        if not is_privileged(ctx.author):
            await ctx.send("Массовую модерацию могут запускать только модераторы и владелец канала.")
            return
        if "moderator:manage:banned_users" not in self.scopes:
            await ctx.send(
                "Для массового бана/мута токену бота нужно разрешение "
                "moderator:manage:banned_users."
            )
            return

        channel_name = ctx.channel.name.lower()
        if channel_name in self.mass_actions:
            await ctx.send("В этом канале уже запущен массбан или массмут. Сначала используй !stop.")
            return

        phrase = phrase.strip().casefold()
        if not phrase:
            await ctx.send("Укажи фразу, по которой искать сообщения. Автоматический выбор отключён.")
            return
        if len(phrase) > 100:
            await ctx.send("Фраза не должна превышать 100 символов.")
            return
        if action_type == "timeout" and not 1 <= duration <= 1_209_600:
            await ctx.send("Длительность мута должна быть от 1 секунды до 14 дней.")
            return

        action = {
            "type": action_type,
            "duration": duration,
            "phrase": phrase,
            "users": set(),
            "queue": asyncio.Queue(),
        }
        self.mass_actions[channel_name] = action

        for history_channel, username, content, privileged in list(self.recent_messages)[-70:]:
            if (
                history_channel == channel_name
                and not privileged
                and username != self.nick.lower()
                and not content.lstrip().startswith(PREFIX)
                and phrase in content.casefold()
                and username not in action["users"]
                and len(action["users"]) < 70
            ):
                action["users"].add(username)
                action["queue"].put_nowait(username)

        action["task"] = self.create_background_task(
            self.mass_action_worker(channel_name, action)
        )
        verb = "бан" if action_type == "ban" else f"мут на {duration} сек."
        await ctx.send(
            f"✅ Масс{verb} запущен по фразе «{phrase}»: "
            f"в очереди {len(action['users'])} пользователей. "
            "Новые совпадения будут обрабатываться до команды !stop."
        )

    async def stop_mass_action(self, ctx):
        if not is_privileged(ctx.author):
            await ctx.send("Остановить массбан/массмут могут только модераторы и владелец канала.")
            return
        channel_name = ctx.channel.name.lower()
        action = self.mass_actions.get(channel_name)
        if not action:
            await ctx.send("В этом канале нет активного массбана или массмута.")
            return
        self.mass_actions.pop(channel_name, None)
        action["task"].cancel()
        await ctx.send("✅ Массовая модерация остановлена.")

    # ---------- фоновые задачи ----------
    async def points_loop(self):
        while True:
            await asyncio.sleep(POINTS_INTERVAL)
            now = time.time()
            for (channel_name, user), ts in list(self.active.items()):
                if now - ts <= ACTIVE_WINDOW:
                    self.db.add_points(channel_name, user, POINTS_PER_TICK)
                else:
                    del self.active[(channel_name, user)]

    async def timer_loop(self):
        i = 0
        last_legacy_timer = time.time()
        while True:
            await asyncio.sleep(10)
            try:
                streams_online = await self.are_streams_online()
            except Exception as error:
                log(f"[ОШИБКА таймера] Не удалось проверить статус стрима: {error!r}")
                continue

            now = time.time()
            extra_seconds = 0 if streams_online else OFFLINE_TIMER_EXTRA
            for row in self.db.due_timers(now, extra_seconds=extra_seconds):
                channel = self.get_channel(row["channel"])
                if channel:
                    try:
                        await self.send_timer_message(channel, row["message"])
                    except Exception as e:
                        log(f"[ОШИБКА таймера] {e!r}")

            if (
                TIMER_MESSAGES
                and now - last_legacy_timer >= TIMER_INTERVAL + extra_seconds
            ):
                message = TIMER_MESSAGES[i % len(TIMER_MESSAGES)]
                i += 1
                last_legacy_timer = now
                for ch_name in CHANNELS:
                    channel = self.get_channel(ch_name)
                    if channel:
                        try:
                            await self.send_timer_message(channel, message)
                        except Exception as e:
                            log(f"[ОШИБКА таймера] {e!r}")

    # ---------- общие команды ----------

    @commands.command(name="commands", aliases=["help", "команды"])
    async def команды(self, ctx):
        builtin = "ask, dice, 8ball, points, daily, gamble, top, give, timer"
        custom = ", ".join(self.db.list_custom(ctx.channel.name.lower()))
        text = f"Команды: {builtin}"
        if custom:
            text += f" | Кастомные: {custom}"
        text += (
            f" | Модераторам: {PREFIX}banword, {PREFIX}addcmd (до 30 команд), "
            f"{PREFIX}cmdalias (до 5 алиасов на команду), {PREFIX}gamealias "
            "(до 20 алиасов категорий), до 5 активных таймеров. "
            f"Повторы: x2-x15, кулдаун 3 сек.; {PREFIX}mb и {PREFIX}m анализируют "
            "до 70 последних сообщений."
            f" | Антифлуд: {PREFIX}af on/off, {PREFIX}timeouts <секунды>"
        )
        await ctx.send(text)

    @commands.command(name="mb", aliases=["massban"])
    async def massban(self, ctx, *, phrase: str = ""):
        await self.start_mass_action(ctx, "ban", phrase)

    @commands.command(name="m", aliases=["mt", "massmute"])
    async def massmute(self, ctx, *, args: str = ""):
        parts = args.strip().split(maxsplit=1)
        duration = 300
        phrase = args.strip()
        if parts and parts[0].isdigit():
            duration = int(parts[0])
            phrase = parts[1] if len(parts) > 1 else ""
        await self.start_mass_action(ctx, "timeout", phrase, duration)

    @commands.command(name="stop")
    async def stop(self, ctx):
        await self.stop_mass_action(ctx)

    @commands.command(name="title", aliases=["t"])
    async def title(self, ctx, *, new_title: str = ""):
        new_title = new_title.strip()
        if not new_title:
            await ctx.send(f"Использование: {PREFIX}title <новое название>")
            return
        if len(new_title) > 140:
            await ctx.send("Название стрима не должно превышать 140 символов.")
            return

        await StreamInfo(self).update(ctx, title=new_title)

    @commands.command(name="game", aliases=["g"])
    async def game(self, ctx, *, category: str = ""):
        category = category.strip()
        if not category:
            await ctx.send(f"Использование: {PREFIX}game <название категории>")
            return
        category = self.db.get_category_alias(ctx.channel.name.lower(), category.casefold()) or category

        await StreamInfo(self).update(ctx, category=category)

    @commands.command(name="timer", aliases=["timers"])
    async def timer(self, ctx, action: str = "", *, details: str = ""):
        if not is_privileged(ctx.author):
            await ctx.send("Управлять таймерами могут только модераторы и владелец канала.")
            return

        channel_name = ctx.channel.name.lower()
        action = action.lower()
        if action in ("", "list", "список"):
            timers = self.db.list_timers(channel_name)
            if not timers:
                await ctx.send("Пользовательских таймеров пока нет.")
                return
            summary = " | ".join(
                f"#{row['id']} [{'вкл' if row['enabled'] else 'выкл'}, "
                f"{row['interval_minutes']} мин]: {row['message']}"
                for row in timers
            )
            await ctx.send(summary[:450])
            return

        if action in ("del", "delete", "удалить"):
            try:
                timer_id = int(details.strip())
            except ValueError:
                await ctx.send(f"Использование: {PREFIX}timer del <номер>")
                return
            if timer_id not in {row["id"] for row in self.db.list_timers(channel_name)}:
                await ctx.send(f"Таймер #{timer_id} не найден.")
                return
            self.db.delete_timer(timer_id)
            await ctx.send(f"✅ Таймер #{timer_id} успешно удалён.")
            return

        if action in ("on", "off", "вкл", "выкл"):
            try:
                timer_id = int(details.strip())
            except ValueError:
                await ctx.send(f"Использование: {PREFIX}timer {action} <номер>")
                return
            if timer_id not in {row["id"] for row in self.db.list_timers(channel_name)}:
                await ctx.send(f"Таймер #{timer_id} не найден.")
                return
            enabled = action in ("on", "вкл")
            if self.db.set_timer_enabled(timer_id, enabled):
                await ctx.send(
                    f"✅ Таймер #{timer_id} успешно "
                    f"{'включён' if enabled else 'выключен'}."
                )
            else:
                await ctx.send("Можно иметь не более 5 активных таймеров.")
            return

        try:
            interval = int(action)
        except ValueError:
            await ctx.send(
                f"Использование: {PREFIX}timer <минуты> <сообщение> | "
                f"{PREFIX}timer list | {PREFIX}timer on/off/del <номер>. "
                "В офлайне к интервалу добавляется 5 минут."
            )
            return

        message = details.strip()
        if not 1 <= interval <= 1440:
            await ctx.send("Интервал должен быть от 1 до 1440 минут.")
            return
        if not message or len(message) > 450:
            await ctx.send("Укажите сообщение длиной от 1 до 450 символов.")
            return
        try:
            custom_variable_names = {
                item["name"] for item in self.db.list_custom_variables(channel_name)
            }
            template_parts = parse_template(message, custom_variable_names)
            if contains_variable(template_parts, "spam"):
                await ctx.send("Переменную {spam} нельзя использовать в таймере.")
                return
            if needs_sender(template_parts):
                await ctx.send("В таймере нельзя использовать {sender} или {user}: у него нет автора.")
                return
        except TemplateError as error:
            await ctx.send(f"Ошибка переменных: {error}")
            return

        timer_id = self.db.add_timer(channel_name, message, interval)
        if timer_id is None:
            await ctx.send("Можно иметь не более 5 активных таймеров.")
            return
        await ctx.send(
            f"✅ Таймер #{timer_id} успешно создан. "
            f"Сообщение будет отправляться каждые {interval} мин. "
            f"Только в чат #{channel_name}."
        )

    @commands.command(name="dice", aliases=["roll"])
    @commands.cooldown(rate=1, per=5, bucket=commands.Bucket.user)
    async def dice(self, ctx, sides: int = 6):
        sides = max(2, min(sides, 1000))
        await ctx.send(f"@{ctx.author.name} выбросил {random.randint(1, sides)} (d{sides}) 🎲")

    @commands.command(name="8ball", aliases=["шар"])
    @commands.cooldown(rate=1, per=5, bucket=commands.Bucket.user)
    async def eight_ball(self, ctx, *, question: str = ""):
        if not question:
            await ctx.send(f"@{ctx.author.name}, задай вопрос: {PREFIX}8ball <вопрос>")
            return
        await ctx.send(f"🎱 {random.choice(EIGHT_BALL)}, @{ctx.author.name}")

    # ---------- экономика ----------
    @commands.command(name="points", aliases=["баланс", "coins"])
    async def points(self, ctx, user: str = None):
        channel = ctx.channel.name.lower()
        target = (user or ctx.author.name).lstrip("@").lower()
        await ctx.send(f"У @{target}: {self.db.get_points(channel, target)} {POINTS_NAME}")

    @commands.command(name="daily", aliases=["daly"])
    async def daily(self, ctx):
        channel = ctx.channel.name.lower()
        name = ctx.author.name.lower()
        last = self.db.get_last_daily(channel, name)
        now = time.time()
        left = DAILY_COOLDOWN - (now - last)
        if left > 0:
            await ctx.send(f"@{ctx.author.name}, бонус будет доступен через {math.ceil(left)} сек.")
            return
        self.db.add_points(channel, name, DAILY_BONUS)
        self.db.set_last_daily(channel, name, now)
        await ctx.send(f"@{ctx.author.name} получил бонус {DAILY_BONUS} {POINTS_NAME}! 🎁")

    @commands.command(name="gamble", aliases=["bet"])
    @commands.cooldown(rate=1, per=10, bucket=commands.Bucket.user)
    async def gamble(self, ctx, amount: str = ""):
        channel = ctx.channel.name.lower()
        name = ctx.author.name.lower()
        balance = self.db.get_points(channel, name)
        if amount.lower() == "all":
            bet = balance
        elif amount.isdigit():
            bet = int(amount)
        else:
            await ctx.send(f"@{ctx.author.name}, используй: {PREFIX}gamble <сумма|all>")
            return
        if bet <= 0 or bet > balance:
            await ctx.send(f"@{ctx.author.name}, неверная ставка. Баланс: {balance} {POINTS_NAME}")
            return
        if random.random() < 0.45:
            self.db.add_points(channel, name, bet)
            await ctx.send(f"🎉 @{ctx.author.name} выиграл {bet}! Баланс: {balance + bet} {POINTS_NAME}")
        else:
            self.db.add_points(channel, name, -bet)
            await ctx.send(f"💀 @{ctx.author.name} проиграл {bet}. Баланс: {balance - bet} {POINTS_NAME}")

    @commands.command(name="give", aliases=["pay"])
    async def give(self, ctx, user: str, amount: int):
        channel = ctx.channel.name.lower()
        sender = ctx.author.name.lower()
        receiver = user.lstrip("@").lower()
        if receiver == sender or amount <= 0 or amount > self.db.get_points(channel, sender):
            await ctx.send(f"@{ctx.author.name}, нельзя совершить такой перевод.")
            return
        self.db.add_points(channel, sender, -amount)
        self.db.add_points(channel, receiver, amount)
        await ctx.send(f"@{ctx.author.name} передал {amount} {POINTS_NAME} пользователю @{receiver}")

    @commands.command(name="top", aliases=["топ"])
    @commands.cooldown(rate=1, per=15, bucket=commands.Bucket.channel)
    async def top(self, ctx):
        rows = self.db.top(ctx.channel.name.lower(), 5)
        if not rows:
            await ctx.send("Пока никто ничего не накопил.")
            return
        text = " | ".join(f"{i}. {r['name']} — {r['points']}" for i, r in enumerate(rows, 1))
        await ctx.send(f"🏆 Топ: {text}")

    # ---------- команды для модераторов ----------
    @commands.command(name="addcmd")
    @commands.cooldown(rate=1, per=3, bucket=commands.Bucket.user)
    async def addcmd(self, ctx, name: str, *, response: str):
        if not is_privileged(ctx.author):
            await ctx.send("Эту команду могут использовать только модераторы и владелец канала.")
            return
        channel = ctx.channel.name.lower()
        name = name.lstrip(PREFIX).casefold()
        if not re.fullmatch(r"[\w]+", name, re.UNICODE):
            await ctx.send("Имя команды может содержать буквы, цифры и _.")
            return
        if self.get_command(name) is not None:
            await ctx.send("Это имя занято встроенной командой.")
            return
        if self.db.custom_name_or_alias_exists(channel, name, except_command=name):
            await ctx.send("Это имя уже используется другой пользовательской командой.")
            return
        if not self.db.get_custom(channel, name) and self.db.custom_count(channel) >= 30:
            await ctx.send("Можно создать не более 30 пользовательских команд.")
            return
        parts = response.split(maxsplit=1)
        cooldown = 0
        if parts and parts[0].isdigit():
            cooldown = int(parts[0])
            response = parts[1] if len(parts) > 1 else ""
        if not 0 <= cooldown <= 86400:
            await ctx.send("Кулдаун должен быть от 0 до 86400 секунд.")
            return
        response = response.strip()
        if not response:
            await ctx.send(
                f"Использование: {PREFIX}addcmd <имя> [кд_секунды] <текст ответа>"
            )
            return
        try:
            parse_template(response)
        except TemplateError as error:
            await ctx.send(f"Ошибка переменных: {error}")
            return
        if not self.db.set_custom(channel, name, response, cooldown):
            await ctx.send("Можно создать не более 30 пользовательских команд.")
            return
        cooldown_text = f", кулдаун {cooldown} сек." if cooldown else ", без кулдауна"
        await ctx.send(
            f"✅ Команда {PREFIX}{name} сохранена{cooldown_text}. "
            f"Работает в чате #{channel}. "
            "Переменные: {accountage}, {channel}, {chatter}, {commands}, "
            "{countdown}/{countup}, {emote}, {followage}/{followcount}, "
            "{fromindex}/{index}, {horoscope}, {math}, {pick}, {query}, "
            "{randint}, {sender}, {spam}, {subage}/{subcount}, {uptime}, {weather}."
        )

    @commands.command(name="cmdalias")
    async def custom_aliases(self, ctx, name: str, *, aliases: str = ""):
        if not is_privileged(ctx.author):
            await ctx.send("Настраивать алиасы могут только модераторы и владелец канала.")
            return
        channel = ctx.channel.name.lower()
        name = name.lstrip(PREFIX).casefold()
        if not self.db.get_custom(channel, name):
            await ctx.send("Пользовательская команда не найдена.")
            return
        alias_names = [] if aliases.strip().lower() in ("", "off", "del") else [
            item.strip().lstrip(PREFIX).casefold()
            for item in aliases.replace(",", " ").split()
        ]
        if len(alias_names) > 5:
            await ctx.send("Можно задать не более 5 алиасов на команду.")
            return
        if len(alias_names) != len(set(alias_names)):
            await ctx.send("Алиасы должны быть уникальными.")
            return
        for alias in alias_names:
            if not re.fullmatch(r"[\w]+", alias, re.UNICODE):
                await ctx.send("Алиасы могут содержать буквы, цифры и _.")
                return
            if alias == name:
                await ctx.send("Алиас не должен совпадать с именем команды.")
                return
            if self.get_command(alias) is not None:
                await ctx.send(f"Алиас {PREFIX}{alias} уже занят встроенной командой.")
                return
            if self.db.custom_name_or_alias_exists(channel, alias, except_command=name):
                await ctx.send(f"Алиас {PREFIX}{alias} уже используется.")
                return
        if not self.db.set_custom_aliases(channel, name, alias_names):
            await ctx.send("Можно задать не более 5 алиасов на команду.")
            return
        await ctx.send(
            f"Алиасы команды {PREFIX}{name}: "
            + (", ".join(alias_names) if alias_names else "нет")
        )

    @commands.command(name="delcmd")
    async def delcmd(self, ctx, name: str):
        if not is_privileged(ctx.author):
            await ctx.send("Эту команду могут использовать только модераторы и владелец канала.")
            return
        channel = ctx.channel.name.lower()
        name = name.lstrip(PREFIX).casefold()
        ok = self.db.del_custom(channel, name)
        await ctx.send(f"Команда {PREFIX}{name} удалена." if ok else "Такой команды нет.")

    @commands.command(name="banword")
    async def banword(self, ctx, action: str, *, word: str = ""):
        if not is_privileged(ctx.author):
            await ctx.send("Управлять банвордами могут только модераторы и владелец канала.")
            return
        channel = ctx.channel.name.lower()
        action = action.lower()
        word = re.sub(r"\s+", " ", word.strip().casefold())
        if action in ("list", "список"):
            words = self.db.list_banned_words(channel)
            await ctx.send(
                f"Пользовательские банворды #{channel} ({len(words)}/30): "
                + (", ".join(words) if words else "нет")
            )
            return
        if not word or len(word) > 60:
            await ctx.send(
                f"Использование: {PREFIX}banword add <слово> | "
                f"{PREFIX}banword del <слово> | {PREFIX}banword list"
            )
            return
        if action in ("add", "добавить"):
            if word in BANNED_WORDS:
                await ctx.send("Это слово уже есть в стандартном списке.")
            elif self.db.add_banned_word(channel, word):
                self.custom_banned_words.setdefault(channel, set()).add(word)
                self.banned_words_patterns.pop(channel, None)
                await ctx.send(f"✅ Банворд «{word}» добавлен для канала #{channel}.")
            elif word in self.custom_banned_words.get(channel, ()):
                await ctx.send("Это слово уже добавлено.")
            else:
                await ctx.send("Достигнут лимит: не более 30 пользовательских банвордов.")
            return
        if action in ("del", "delete", "удалить"):
            if self.db.delete_banned_word(channel, word):
                self.custom_banned_words.get(channel, set()).discard(word)
                self.banned_words_patterns.pop(channel, None)
                await ctx.send(f"✅ Банворд «{word}» удалён для канала #{channel}.")
            else:
                await ctx.send("Такого пользовательского банворда нет.")
            return
        await ctx.send(f"Использование: {PREFIX}banword add/del/list <слово>")

    @commands.command(name="gamealias")
    async def game_alias(self, ctx, alias: str = "", *, category: str = ""):
        if not is_privileged(ctx.author):
            await ctx.send("Управлять алиасами категорий могут только модераторы и владелец канала.")
            return
        channel = ctx.channel.name.lower()
        alias = alias.strip().casefold()
        if alias in ("list", "список") and not category:
            aliases = self.db.list_category_aliases(channel)
            summary = ", ".join(
                f"{row['alias']} → {row['category']}" for row in aliases
            )
            await ctx.send(f"Алиасы категорий ({len(aliases)}/20): {summary or 'нет'}")
            return
        if alias in ("del", "delete", "удалить"):
            target = category.strip().casefold()
            if not target:
                await ctx.send(f"Использование: {PREFIX}gamealias del <алиас>")
            elif self.db.delete_category_alias(channel, target):
                await ctx.send(f"Алиас категории «{target}» удалён.")
            else:
                await ctx.send("Такого алиаса категории нет.")
            return
        category = category.strip()
        if not alias or not category or len(alias) > 30 or len(category) > 100:
            await ctx.send(f"Использование: {PREFIX}gamealias <алиас> <категория Twitch>")
            return
        if self.db.set_category_alias(channel, alias, category):
            await ctx.send(f"✅ Алиас «{alias}» ведёт на категорию «{category}».")
        else:
            await ctx.send("Достигнут лимит: не более 20 алиасов категорий.")

    @commands.command(name="permit")
    async def permit(self, ctx, user: str):
        if not is_privileged(ctx.author):
            await ctx.send("Эту команду могут использовать только модераторы и владелец канала.")
            return
        user = user.lstrip("@").lower()
        self.permits[user] = time.time() + PERMIT_SECONDS
        await ctx.send(f"@{user}, можешь отправить ссылку в течение {PERMIT_SECONDS} сек.")

    @commands.command(name="af", aliases=["antiflood"])
    async def antiflood(self, ctx, action: str = "", *, args: str = ""):
        if not is_privileged(ctx.author):
            await ctx.send("Настраивать антифлуд могут только модераторы и владелец канала.")
            return

        channel = ctx.channel.name.lower()
        settings = self.antiflood_settings(channel)
        action = action.lower()
        value = args.strip().lower()

        if not action:
            await ctx.send(
                f"Антифлуд {'включён' if settings['enabled'] else 'выключен'}, "
                f"режим {settings['mode']}, размер {settings['size']}px, "
                f"лимит {settings['spam_messages']} сообщений за {settings['spam_seconds']} сек. "
                f"Настройка: {PREFIX}af on/off | mode 1-3 | size <px|off> | "
                f"spam messages <число> time <сек>. Таймауты: "
                f"{', '.join(map(str, settings['timeouts']))} сек."
            )
            return

        if action in ("on", "off", "вкл", "выкл"):
            if action in ("on", "вкл") and "moderator:manage:banned_users" not in self.scopes:
                await ctx.send(
                    "Для работы антифлуда токену нужно разрешение "
                    "moderator:manage:banned_users."
                )
                return
            settings["enabled"] = action in ("on", "вкл")
        elif action == "mode":
            if value not in ("1", "2", "3"):
                await ctx.send(f"Использование: {PREFIX}af mode <1-3>")
                return
            settings["mode"] = int(value)
        elif action == "size":
            try:
                size = 0 if value == "off" else int(value)
            except ValueError:
                await ctx.send(f"Использование: {PREFIX}af size <пиксели|off>")
                return
            if size != 0 and not 1 <= size <= 10000:
                await ctx.send("Размер должен быть от 1 до 10000 пикселей или off.")
                return
            settings["size"] = size
        elif action == "main":
            parts = value.split()
            if len(parts) != 2 or parts[0] != "size":
                await ctx.send(f"Использование: {PREFIX}af main size <пиксели|off>")
                return
            try:
                size = 0 if parts[1] == "off" else int(parts[1])
            except ValueError:
                await ctx.send(f"Использование: {PREFIX}af main size <пиксели|off>")
                return
            if size != 0 and not 1 <= size <= 10000:
                await ctx.send("Размер должен быть от 1 до 10000 пикселей или off.")
                return
            settings["main_size"] = size
        elif action == "percent":
            try:
                percent = 0 if value == "off" else int(value)
            except ValueError:
                await ctx.send(f"Использование: {PREFIX}af percent <0-100|off>")
                return
            if not 0 <= percent <= 100:
                await ctx.send("Процент должен быть от 0 до 100 или off.")
                return
            settings["percent"] = percent
        elif action in ("video", "lang"):
            if value not in ("on", "off", "вкл", "выкл"):
                await ctx.send(f"Использование: {PREFIX}af {action} <on|off>")
                return
            settings[action] = value in ("on", "вкл")
        elif action == "spam":
            parts = value.split()
            if parts == ["del"]:
                settings["spam_messages"] = 0
            elif (
                len(parts) == 4
                and parts[0] == "messages"
                and parts[2] == "time"
                and parts[1].isdigit()
                and parts[3].isdigit()
            ):
                count, seconds = int(parts[1]), int(parts[3])
                if not 1 <= count <= 50 or not 1 <= seconds <= 60:
                    await ctx.send("Количество сообщений: 1-50, время: 1-60 секунд.")
                    return
                settings["spam_messages"] = count
                settings["spam_seconds"] = seconds
            else:
                await ctx.send(
                    f"Использование: {PREFIX}af spam messages <количество> "
                    f"time <секунды> | {PREFIX}af spam del"
                )
                return
        else:
            await ctx.send(
                f"Неизвестная настройка. Используй {PREFIX}af, чтобы увидеть справку."
            )
            return

        self.db.set_antiflood_settings(channel, settings)
        await ctx.send(f"✅ Настройка антифлуда «{action}» обновлена.")

    @commands.command(name="timeouts")
    async def timeouts(self, ctx, *, durations: str = ""):
        if not is_privileged(ctx.author):
            await ctx.send("Настраивать таймауты могут только модераторы и владелец канала.")
            return
        if "moderator:manage:banned_users" not in self.scopes:
            await ctx.send("Токену бота нужно разрешение moderator:manage:banned_users.")
            return
        try:
            values = [int(value) for value in durations.split()]
        except ValueError:
            values = []
        if not values or len(values) > 5 or any(not 1 <= value <= 1_209_600 for value in values):
            await ctx.send(f"Использование: {PREFIX}timeouts <секунды через пробел, максимум 5 значений>")
            return
        channel = ctx.channel.name.lower()
        settings = self.antiflood_settings(channel)
        settings["timeouts"] = values
        self.db.set_antiflood_settings(channel, settings)
        await ctx.send(f"✅ Таймауты антифлуда: {', '.join(map(str, values))} сек.")

    @commands.command(name="allow")
    async def allow_user(self, ctx, user: str, seconds: int = 180):
        if not is_privileged(ctx.author):
            await ctx.send("Выдавать разрешения могут только модераторы и владелец канала.")
            return
        if not 1 <= seconds <= 86400:
            await ctx.send("Время разрешения должно быть от 1 до 86400 секунд.")
            return
        username = user.lstrip("@").lower()
        self.permits[username] = time.time() + seconds
        await ctx.send(f"@{username} не будет проверяться антифлудом {seconds} сек.")

    @commands.command(name="disallow")
    async def disallow_user(self, ctx, user: str):
        if not is_privileged(ctx.author):
            await ctx.send("Отзывать разрешения могут только модераторы и владелец канала.")
            return
        username = user.lstrip("@").lower()
        if self.permits.pop(username, None) is None:
            await ctx.send(f"Для @{username} нет активного разрешения.")
            return
        await ctx.send(f"Разрешение для @{username} отменено.")

    @commands.command(name="so", aliases=["shoutout"])
    async def shoutout(self, ctx, user: str):
        if not is_privileged(ctx.author):
            return
        user = user.lstrip("@")
        await ctx.send(f"Загляните на канал {user}: https://twitch.tv/{user.lower()} ❤️")

    @commands.command(name="addpoints")
    async def addpoints(self, ctx, user: str, amount: int):
        if not ctx.author.is_broadcaster:
            await ctx.send("Эту команду может использовать только владелец канала.")
            return
        self.db.add_points(ctx.channel.name.lower(), user.lstrip("@").lower(), amount)
        await ctx.send(
            f"✅ Баланс @{user.lstrip('@')} успешно изменён на "
            f"{amount} {POINTS_NAME}."
        )


async def main():
    if not TOKEN:
        raise SystemExit("[ОШИБКА] В .env не заполнен TWITCH_TOKEN")
    if not CHANNELS:
        raise SystemExit("[ОШИБКА] В .env не заполнен TWITCH_CHANNELS")
    if TOKEN.startswith("http"):
        raise SystemExit("[ОШИБКА] В TWITCH_TOKEN записана ссылка, а нужен сам токен (oauth:...)")

    # Проверяем токен ДО запуска, чтобы сразу увидеть, в чём проблема
    info = await validate_token(TOKEN)
    if not info:
        raise SystemExit(
            "[ОШИБКА] Twitch не принял токен. Получи новый токен и впиши его в .env "
            "как TWITCH_TOKEN=oauth:..."
        )
    scopes = info.get("scopes") or []
    log(f"[OK] Токен принадлежит аккаунту: {info.get('login')}")
    log(f"[OK] Права токена: {', '.join(scopes) if scopes else '(нет)'}")
    if "moderator:manage:banned_users" not in scopes:
        log(
            "[ПРЕДУПРЕЖДЕНИЕ] Автоматические баны не сработают: "
            "токену нужен scope moderator:manage:banned_users."
        )
    missing = [s for s in ("chat:read", "chat:edit") if s not in scopes]
    if missing:
        raise SystemExit(
            f"[ОШИБКА] У токена нет прав: {', '.join(missing)}. "
            "Получи токен заново со scope chat:read и chat:edit."
        )

    client_id = CLIENT_ID or info.get("client_id")
    token = TOKEN if TOKEN.startswith("oauth:") else f"oauth:{TOKEN}"

    bot = Bot(token, client_id, info["user_id"], scopes)
    try:
        await bot.start()
    finally:
        await bot.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("Бот остановлен.")
