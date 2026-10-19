import asyncio
import json
import os
import random
import re
import sqlite3
import sys
import time
import unicodedata
from collections import deque

import aiohttp
from dotenv import load_dotenv
from twitchio.ext import commands

from stream_info import StreamInfo

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
TIMER_INTERVAL = 500           # автосообщения раз в 15 минут
TIMER_MESSAGES = [
    "Зарабатывай монеты, просто находясь в чате. Проверить баланс: !points",
]
DEFAULT_BANNED_WORDS = {
    "ниггер", "нига", "нага", "nigger", "nigga", "naga",
    "пидор", "пидорас", "педик", "гомик", "петух", "faggot",
    "хиджаб", "белый", "натурал", "гетеросексуал",
    "хохол", "хач", "жид", "москаль", "ватник", "сионист",
    "куколд", "конча", "даун", "аутист", "дебил", "retard",
    "virgin", "девственник", "simp", "симп", "incel", "инцел",
    "cunt", "пизда", "вагина",
}
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


def log(*args):
    print(*args, flush=True)


class Database:
    def __init__(self, path="bot.db"):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                name TEXT PRIMARY KEY,
                points INTEGER NOT NULL DEFAULT 0,
                last_daily REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS custom_commands (
                name TEXT PRIMARY KEY,
                response TEXT NOT NULL,
                cooldown_seconds INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS quotes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL,
                author TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS timers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message TEXT NOT NULL,
                interval_minutes INTEGER NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                last_sent REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS antiflood_settings (
                channel TEXT PRIMARY KEY,
                settings TEXT NOT NULL
            );
            """
        )
        custom_command_columns = {
            row["name"] for row in self.conn.execute("PRAGMA table_info(custom_commands)")
        }
        if "cooldown_seconds" not in custom_command_columns:
            self.conn.execute(
                "ALTER TABLE custom_commands "
                "ADD COLUMN cooldown_seconds INTEGER NOT NULL DEFAULT 0"
            )
        self.conn.commit()
        self.compact_timer_ids()

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
    def get_points(self, name):
        row = self.conn.execute("SELECT points FROM users WHERE name=?", (name,)).fetchone()
        return row["points"] if row else 0

    def add_points(self, name, amount):
        self.conn.execute(
            "INSERT INTO users(name, points) VALUES(?, ?) "
            "ON CONFLICT(name) DO UPDATE SET points = MAX(0, points + ?)",
            (name, max(0, amount), amount),
        )
        self.conn.commit()

    def get_last_daily(self, name):
        row = self.conn.execute("SELECT last_daily FROM users WHERE name=?", (name,)).fetchone()
        return row["last_daily"] if row else 0

    def set_last_daily(self, name, ts):
        self.conn.execute(
            "INSERT INTO users(name, last_daily) VALUES(?, ?) "
            "ON CONFLICT(name) DO UPDATE SET last_daily=?",
            (name, ts, ts),
        )
        self.conn.commit()

    def top(self, limit=5):
        return self.conn.execute(
            "SELECT name, points FROM users ORDER BY points DESC LIMIT ?", (limit,)
        ).fetchall()

    # --- кастомные команды ---
    def get_custom(self, name):
        return self.conn.execute(
            "SELECT response, cooldown_seconds FROM custom_commands WHERE name=?",
            (name,),
        ).fetchone()

    def set_custom(self, name, response, cooldown_seconds=0):
        self.conn.execute(
            "INSERT INTO custom_commands(name, response, cooldown_seconds) VALUES(?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET response=?, cooldown_seconds=?",
            (name, response, cooldown_seconds, response, cooldown_seconds),
        )
        self.conn.commit()

    def del_custom(self, name):
        cur = self.conn.execute("DELETE FROM custom_commands WHERE name=?", (name,))
        self.conn.commit()
        return cur.rowcount > 0

    def list_custom(self):
        return [r["name"] for r in self.conn.execute("SELECT name FROM custom_commands ORDER BY name")]

    # --- цитаты ---
    def add_quote(self, text, author):
        cur = self.conn.execute("INSERT INTO quotes(text, author) VALUES(?, ?)", (text, author))
        self.conn.commit()
        return cur.lastrowid

    def get_quote(self, quote_id=None):
        if quote_id is not None:
            return self.conn.execute("SELECT * FROM quotes WHERE id=?", (quote_id,)).fetchone()
        return self.conn.execute("SELECT * FROM quotes ORDER BY RANDOM() LIMIT 1").fetchone()

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

    def add_timer(self, message, interval_minutes):
        cur = self.conn.execute(
            "INSERT INTO timers(message, interval_minutes, last_sent) VALUES(?, ?, ?)",
            (message, interval_minutes, time.time()),
        )
        self.conn.commit()
        return cur.lastrowid

    def list_timers(self):
        return self.conn.execute(
            "SELECT id, message, interval_minutes, enabled FROM timers ORDER BY id"
        ).fetchall()

    def delete_timer(self, timer_id):
        cur = self.conn.execute("DELETE FROM timers WHERE id=?", (timer_id,))
        self.conn.commit()
        if cur.rowcount > 0:
            self.compact_timer_ids()
        return cur.rowcount > 0

    def set_timer_enabled(self, timer_id, enabled):
        if enabled:
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

    def due_timers(self, now):
        rows = self.conn.execute(
            "SELECT id, message FROM timers "
            "WHERE enabled=1 AND last_sent + interval_minutes * 60 <= ?",
            (now,),
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
    def __init__(self, token, client_id, moderator_id, scopes):
        super().__init__(
            token=token,
            client_id=client_id,
            prefix=PREFIX,
            initial_channels=CHANNELS,
        )
        self.access_token = token.removeprefix("oauth:")
        self.client_id = client_id
        self.moderator_id = str(moderator_id)
        self.scopes = set(scopes)
        self.twitch_id_cache = {}
        self.db = Database()
        self.active = {}      # имя -> время последнего сообщения
        self.permits = {}     # имя -> до какого времени разрешены ссылки
        self.tasks_started = False
        self.greeted = set()
        self.recent_messages = deque(maxlen=500)
        self.mass_actions = {}
        self.custom_command_uses = {}
        self.antiflood_history = {}
        self.antiflood_violations = {}

    async def twitch_api(self, method, endpoint, *, params=None, payload=None):
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Client-Id": self.client_id,
        }
        if payload is not None:
            headers["Content-Type"] = "application/json"
        async with aiohttp.ClientSession() as session:
            async with session.request(
                method,
                f"https://api.twitch.tv/helix/{endpoint}",
                headers=headers,
                params=params,
                json=payload,
            ) as response:
                if response.status == 204:
                    return {}
                data = await response.json(content_type=None)
                if response.status != 200:
                    message = data.get("message", f"HTTP {response.status}")
                    raise RuntimeError(message)
                return data

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
            asyncio.create_task(self.points_loop())
            asyncio.create_task(self.timer_loop())

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
            self.active[name] = time.time()
            channel_name = message.channel.name.lower()
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
            ):
                action["users"].add(name)
                action["queue"].put_nowait(name)

            # Модераторов и стримера не трогаем.
            matched_word = next(
                (
                    word for word in BANNED_WORDS
                    if re.search(
                        rf"(?<!\w){re.escape(word)}(?!\w)",
                        message.content,
                        re.IGNORECASE,
                    )
                ),
                None,
            )
            if matched_word:
                if privileged:
                    log(
                        f"[BANWORD] @{name}: совпало слово из фильтра, "
                        "но сообщение пропущено (автор отмечен модератором/владельцем)."
                    )
                    return
                log(f"[BANWORD] Совпадение: @{name}, слово {matched_word!r}, канал #{channel_name}")
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
                if len(repeat_match.group(1)) > 2:
                    await message.channel.send("Количество повторов должно быть от 1 до 10.")
                    return
                repeat_count = int(repeat_match.group(1))
                if not 1 <= repeat_count <= 10:
                    await message.channel.send("Количество повторов должно быть от 1 до 10.")
                    return
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
        if content.startswith(PREFIX):
            command_parts = content[len(PREFIX):].split(maxsplit=1)
            cmd_name = command_parts[0].lower() if command_parts else ""
            if cmd_name and self.get_command(cmd_name) is None:
                custom_command = self.db.get_custom(cmd_name)
                if custom_command:
                    if repeat_count == 1 and len(command_parts) > 1:
                        count_match = re.search(r"\s+(\d+)\s*$", content)
                        if count_match:
                            raw_count = count_match.group(1)
                            if len(raw_count) > 2:
                                await message.channel.send(
                                    "Количество повторов должно быть от 1 до 10."
                                )
                                return
                            repeat_count = int(raw_count)
                            if not 1 <= repeat_count <= 10:
                                await message.channel.send(
                                    "Количество повторов должно быть от 1 до 10."
                                )
                                return
                            if repeat_count > 1 and not is_privileged(message.author):
                                await message.channel.send(
                                    "Повторять команды могут только модераторы и владелец канала."
                                )
                                return
                    cooldown = custom_command["cooldown_seconds"]
                    cooldown_key = (channel_name, name, cmd_name)
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
                    response = custom_command["response"].replace(
                        "{user}", message.author.name
                    )
                    for _ in range(repeat_count):
                        await message.channel.send(response)
                    if cooldown:
                        self.custom_command_uses[cooldown_key] = time.time()
                    return

        context = await self.get_context(message)
        if (
            context.command
            and context.command.name == "tg"
            and repeat_count > 1
            and context.view.words
        ):
            await message.channel.send(
                f"Для команды {PREFIX}tg используй либо количество в конце команды, "
                "либо формат xN, но не оба сразу."
            )
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

        action["task"] = asyncio.create_task(
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
            for user, ts in list(self.active.items()):
                if now - ts <= ACTIVE_WINDOW:
                    self.db.add_points(user, POINTS_PER_TICK)
                else:
                    del self.active[user]

    async def timer_loop(self):
        i = 0
        last_legacy_timer = time.time()
        while True:
            await asyncio.sleep(10)
            now = time.time()
            messages = [row["message"] for row in self.db.due_timers(now)]

            if TIMER_MESSAGES and now - last_legacy_timer >= TIMER_INTERVAL:
                messages.append(TIMER_MESSAGES[i % len(TIMER_MESSAGES)])
                i += 1
                last_legacy_timer = now

            for message in messages:
                for ch_name in CHANNELS:
                    channel = self.get_channel(ch_name)
                    if channel:
                        try:
                            await channel.send(message)
                        except Exception as e:
                            log(f"[ОШИБКА таймера] {e!r}")

    # ---------- общие команды ----------

    @commands.command(name="commands", aliases=["help", "команды"])
    async def команды(self, ctx):
        builtin = "dice, 8ball, tg, points, daily, gamble, top, give, quote, uptime, timer, af"
        custom = ", ".join(self.db.list_custom())
        text = f"Команды: {builtin}"
        if custom:
            text += f" | Кастомные: {custom}"
        text += (
            f" | Модераторам: добавьте xN к команде "
            f"(например, {PREFIX}tg x5); у кастомной команды можно указать "
            f"число повторов без x ({PREFIX}donat 5)"
        )
        text += f" | Антифлуд: {PREFIX}af on/off, {PREFIX}timeouts <секунды>"
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

        await StreamInfo(self).update(ctx, category=category)

    @commands.command(name="timer", aliases=["timers"])
    async def timer(self, ctx, action: str = "", *, details: str = ""):
        if not is_privileged(ctx.author):
            await ctx.send("Управлять таймерами могут только модераторы и владелец канала.")
            return

        action = action.lower()
        if action in ("", "list", "список"):
            timers = self.db.list_timers()
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
            if self.db.delete_timer(timer_id):
                await ctx.send(f"✅ Таймер #{timer_id} успешно удалён.")
            else:
                await ctx.send(f"Таймер #{timer_id} не найден.")
            return

        if action in ("on", "off", "вкл", "выкл"):
            try:
                timer_id = int(details.strip())
            except ValueError:
                await ctx.send(f"Использование: {PREFIX}timer {action} <номер>")
                return
            enabled = action in ("on", "вкл")
            if self.db.set_timer_enabled(timer_id, enabled):
                await ctx.send(
                    f"✅ Таймер #{timer_id} успешно "
                    f"{'включён' if enabled else 'выключен'}."
                )
            else:
                await ctx.send(f"Таймер #{timer_id} не найден.")
            return

        try:
            interval = int(action)
        except ValueError:
            await ctx.send(
                f"Использование: {PREFIX}timer <минуты> <сообщение> | "
                f"{PREFIX}timer list | {PREFIX}timer on/off/del <номер>"
            )
            return

        message = details.strip()
        if not 1 <= interval <= 1440:
            await ctx.send("Интервал должен быть от 1 до 1440 минут.")
            return
        if not message or len(message) > 450:
            await ctx.send("Укажите сообщение длиной от 1 до 450 символов.")
            return

        timer_id = self.db.add_timer(message, interval)
        await ctx.send(
            f"✅ Таймер #{timer_id} успешно создан. "
            f"Сообщение будет отправляться каждые {interval} мин."
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

    @commands.command(name="tg", aliases=["тг", "тгшка", "тгк"])
    async def tg(self, ctx, count: int = 1):
        if not is_privileged(ctx.author):
            await ctx.send("Эту команду могут использовать только модераторы и владелец канала.")
            return
        if not 1 <= count <= 10:
            await ctx.send(f"Использование: {PREFIX}tg <количество от 1 до 10>")
            return

        for _ in range(count):
            await ctx.send("Тгк tg Тгк канал https://t.me/smailrofltg tg ")


    # ---------- экономика ----------
    @commands.command(name="points", aliases=["баланс", "coins"])
    async def points(self, ctx, user: str = None):
        target = (user or ctx.author.name).lstrip("@").lower()
        await ctx.send(f"У @{target}: {self.db.get_points(target)} {POINTS_NAME}")

    @commands.command(name="daily")
    async def daily(self, ctx):
        name = ctx.author.name.lower()
        last = self.db.get_last_daily(name)
        left = 86400 - (time.time() - last)
        if left > 0:
            h, m = int(left // 3600), int(left % 3600 // 60)
            await ctx.send(f"@{ctx.author.name}, бонус будет доступен через {h} ч {m} мин.")
            return
        self.db.add_points(name, DAILY_BONUS)
        self.db.set_last_daily(name, time.time())
        await ctx.send(f"@{ctx.author.name} получил ежедневный бонус {DAILY_BONUS} {POINTS_NAME}! 🎁")

    @commands.command(name="gamble", aliases=["bet"])
    @commands.cooldown(rate=1, per=10, bucket=commands.Bucket.user)
    async def gamble(self, ctx, amount: str = ""):
        name = ctx.author.name.lower()
        balance = self.db.get_points(name)
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
            self.db.add_points(name, bet)
            await ctx.send(f"🎉 @{ctx.author.name} выиграл {bet}! Баланс: {balance + bet} {POINTS_NAME}")
        else:
            self.db.add_points(name, -bet)
            await ctx.send(f"💀 @{ctx.author.name} проиграл {bet}. Баланс: {balance - bet} {POINTS_NAME}")

    @commands.command(name="give", aliases=["pay"])
    async def give(self, ctx, user: str, amount: int):
        sender = ctx.author.name.lower()
        receiver = user.lstrip("@").lower()
        if receiver == sender or amount <= 0 or amount > self.db.get_points(sender):
            await ctx.send(f"@{ctx.author.name}, нельзя совершить такой перевод.")
            return
        self.db.add_points(sender, -amount)
        self.db.add_points(receiver, amount)
        await ctx.send(f"@{ctx.author.name} передал {amount} {POINTS_NAME} пользователю @{receiver}")

    @commands.command(name="top", aliases=["топ"])
    @commands.cooldown(rate=1, per=15, bucket=commands.Bucket.channel)
    async def top(self, ctx):
        rows = self.db.top(5)
        if not rows:
            await ctx.send("Пока никто ничего не накопил.")
            return
        text = " | ".join(f"{i}. {r['name']} — {r['points']}" for i, r in enumerate(rows, 1))
        await ctx.send(f"🏆 Топ: {text}")

    # ---------- цитаты ----------
    @commands.command(name="quote", aliases=["цитата"])
    @commands.cooldown(rate=1, per=10, bucket=commands.Bucket.channel)
    async def quote(self, ctx, number: int = None):
        q = self.db.get_quote(number)
        if not q:
            await ctx.send("Цитата не найдена.")
            return
        await ctx.send(f'#{q["id"]}: "{q["text"]}" — добавил {q["author"]}')

    @commands.command(name="addquote")
    async def addquote(self, ctx, *, text: str):
        if not is_privileged(ctx.author):
            await ctx.send("Эту команду могут использовать только модераторы и владелец канала.")
            return
        qid = self.db.add_quote(text, ctx.author.name)
        await ctx.send(f"Цитата #{qid} добавлена.")

    # ---------- команды для модераторов ----------
    @commands.command(name="addcmd")
    @commands.cooldown(rate=1, per=3, bucket=commands.Bucket.user)
    async def addcmd(self, ctx, name: str, *, response: str):
        if not is_privileged(ctx.author):
            await ctx.send("Эту команду могут использовать только модераторы и владелец канала.")
            return
        name = name.lstrip(PREFIX).lower()
        if self.get_command(name) is not None:
            await ctx.send("Это имя занято встроенной командой.")
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
        self.db.set_custom(name, response, cooldown)
        cooldown_text = f", кулдаун {cooldown} сек." if cooldown else ", без кулдауна"
        await ctx.send(
            f"✅ Команда {PREFIX}{name} сохранена{cooldown_text}. "
            "Можно использовать {user}."
        )

    @commands.command(name="delcmd")
    async def delcmd(self, ctx, name: str):
        if not is_privileged(ctx.author):
            await ctx.send("Эту команду могут использовать только модераторы и владелец канала.")
            return
        name = name.lstrip(PREFIX).lower()
        ok = self.db.del_custom(name)
        await ctx.send(f"Команда {PREFIX}{name} удалена." if ok else "Такой команды нет.")

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
        self.db.add_points(user.lstrip("@").lower(), amount)
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
        from bot import main as current_main

        asyncio.run(current_main())
    except KeyboardInterrupt:
        log("Бот остановлен.")