"""Manage the existing Twitch bot on the web server's asyncio event loop."""

import asyncio
import importlib
import os
import re
import sys
import time
from collections import deque
from pathlib import Path

import bot as bot_module


# Панель живёт долго, а файлы бота могут меняться на диске позже её запуска.
# Перед каждым запуском бота сверяем время изменения исходников и перечитываем
# то, что обновилось, чтобы через сайт работала та же версия кода, что и в IDE.
BOT_SOURCE_FILES = (
    "command_variables.py",
    "variable_services.py",
    "weather_service.py",
    "bot.py",
)
_PANEL_STARTED_AT = time.time()
_APPLIED_AT = {}


def _source_path(module, filename):
    source = Path(getattr(module, "__file__", "") or filename)
    if not source.is_absolute():
        source = Path(__file__).parent / source
    return source


def _pending_sources():
    pending = []
    for filename in BOT_SOURCE_FILES:
        module = sys.modules.get(Path(filename).stem)
        if module is None:
            continue
        source = _source_path(module, filename)
        try:
            modified_at = source.stat().st_mtime
        except OSError:
            continue
        if modified_at > _APPLIED_AT.get(filename, _PANEL_STARTED_AT):
            pending.append((filename, source))
    return pending


def apply_bot_updates():
    """Перечитывает модули бота, чьи файлы изменились после запуска панели."""
    pending = _pending_sources()
    if not pending:
        return []
    for filename, source in pending:
        try:
            compile(source.read_text(encoding="utf-8"), str(source), "exec")
        except SyntaxError as error:
            raise RuntimeError(
                f"Файл {filename} не применён: ошибка синтаксиса, строка {error.lineno}."
            ) from None
        except OSError:
            raise RuntimeError(f"Файл {filename} не применён: не удалось прочитать с диска.") from None
    for filename, _ in pending:
        try:
            importlib.reload(sys.modules[Path(filename).stem])
        except Exception as error:
            raise RuntimeError(f"Файл {filename} не применён: {error}") from None
        _APPLIED_AT[filename] = time.time()
    return [filename for filename, _ in pending]


def server_reload_required():
    for filename in ("web_panel.py", "panel_runtime.py"):
        try:
            if (Path(__file__).with_name(filename)).stat().st_mtime > _PANEL_STARTED_AT:
                return True
        except OSError:
            continue
    return False


class Runtime:
    def __init__(self, db_path):
        self.db_path = db_path
        self.bot = None
        self.status = "stopped"
        self.error = None
        self.started_at = None
        self.logs = deque(maxlen=300)
        self._task = None
        self._lock = asyncio.Lock()
        self._stopping = False
        self.channels = None

    def _sanitize(self, message):
        text = str(message)
        secrets = {bot_module.TOKEN, bot_module.TOKEN.removeprefix("oauth:")}
        if self.bot is not None:
            secrets.add(self.bot.access_token)
        for key, value in os.environ.items():
            if any(part in key.upper() for part in ("TOKEN", "SECRET", "PASSWORD", "API_KEY")):
                secrets.add(value)
        for secret in sorted((value for value in secrets if value), key=len, reverse=True):
            text = text.replace(secret, "[скрыто]")
        return re.sub(r"(?i)\b(oauth:|Bearer\s+|OAuth\s+)[a-z0-9._-]+", r"\1[скрыто]", text)

    def _log(self, message, level=None):
        message = self._sanitize(message)
        if level is None:
            lower = message.casefold()
            level = "error" if any(word in lower for word in ("ошибка", "error", "exception")) else "info"
        self.logs.append({"time": time.time(), "level": level, "message": message[:2000]})

    def snapshot(self):
        connected = self.status == "running" and self.bot is not None
        if connected:
            connection = getattr(self.bot, "_connection", None)
            ready = getattr(connection, "is_ready", None)
            if ready is not None:
                connected = ready.is_set()
        return {
            "status": self.status,
            "connected": bool(connected),
            "nick": self.bot.nick if self.bot is not None else None,
            "error": self.error,
            "started_at": self.started_at,
            "logs": list(self.logs),
            "channels": list(bot_module.CHANNELS),
        }

    async def start(self):
        async with self._lock:
            if self._task is not None and not self._task.done():
                return self.snapshot()
            try:
                updated = apply_bot_updates()
            except RuntimeError as error:
                self.status = "error"
                self.error = str(error)
                self._log(str(error), "error")
                return self.snapshot()
            if updated:
                self._log("Подхватил свежий код бота: " + ", ".join(updated) + ".")
            if self.channels is not None:
                # bot.py мог перечитаться из файла и сбросить CHANNELS до значений .env
                self.channels.apply()
            if server_reload_required():
                self._log(
                    "Серверные файлы панели изменены после её запуска — перезапустите "
                    "панель один раз, чтобы применить и их."
                )
            self._stopping = False
            self.error = None
            self.started_at = None
            self.status = "connecting"
            self._log("Подключение бота к Twitch…")
            self._task = asyncio.create_task(self._run(), name="panel-twitch-runtime")
            return self.snapshot()

    async def stop(self):
        async with self._lock:
            self._stopping = True
            task = self._task
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            self._task = None
            self.status = "stopped"
            self.error = None
            self.started_at = None
            self._log("Бот остановлен.")
            return self.snapshot()

    async def _run(self):
        bot = None
        start_task = None
        ready_task = None
        listener = self._log
        bot_module.LOG_LISTENERS.append(listener)
        try:
            if not bot_module.TOKEN:
                raise ValueError("Заполните TWITCH_TOKEN в файле .env и перезапустите панель.")
            if not bot_module.CHANNELS:
                raise ValueError("Заполните TWITCH_CHANNELS в файле .env и перезапустите панель.")
            if bot_module.TOKEN.startswith("http"):
                raise ValueError("В TWITCH_TOKEN нужен токен Twitch, а не ссылка.")
            info = await asyncio.wait_for(bot_module.validate_token(bot_module.TOKEN), timeout=20)
            if not info:
                raise ValueError("Twitch не принял токен. Обновите TWITCH_TOKEN в .env.")
            scopes = info.get("scopes") or []
            missing = [scope for scope in ("chat:read", "chat:edit") if scope not in scopes]
            if missing:
                raise ValueError("Токену не хватает разрешений: " + ", ".join(missing))
            token = bot_module.TOKEN
            if not token.startswith("oauth:"):
                token = "oauth:" + token
            bot = bot_module.Bot(
                token,
                bot_module.CLIENT_ID or info.get("client_id"),
                info["user_id"],
                scopes,
                db_path=self.db_path,
            )
            self.bot = bot
            ready = asyncio.Event()

            async def on_ready():
                if self._stopping:
                    return
                self.status = "running"
                self.started_at = self.started_at or time.time()
                self.error = None
                ready.set()

            bot.add_event(on_ready, "event_ready")
            start_task = asyncio.create_task(bot.start(), name="panel-twitch-connection")
            ready_task = asyncio.create_task(ready.wait(), name="panel-twitch-ready")
            completed, _ = await asyncio.wait(
                (start_task, ready_task), timeout=40, return_when=asyncio.FIRST_COMPLETED
            )
            if not completed:
                raise TimeoutError("Twitch не ответил за 40 секунд. Проверьте подключение и повторите запуск.")
            if start_task in completed:
                await start_task
                if not self._stopping:
                    raise RuntimeError("Соединение с Twitch закрыто.")
            else:
                await start_task
                if not self._stopping:
                    raise RuntimeError("Соединение с Twitch закрыто.")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if not self._stopping:
                self.error = self._sanitize(str(error) or type(error).__name__)
                self.status = "error"
                self._log(self.error, "error")
        finally:
            for task in (ready_task, start_task):
                if task is not None and not task.done():
                    task.cancel()
            tasks = [task for task in (ready_task, start_task) if task is not None]
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if bot is not None:
                try:
                    await bot.close()
                except Exception as error:
                    self._log("Ошибка при отключении: " + self._sanitize(error), "error")
            self.bot = None
            if listener in bot_module.LOG_LISTENERS:
                bot_module.LOG_LISTENERS.remove(listener)
            if self._stopping:
                self.status = "stopped"

    async def send_chat(self, channel, message):
        channel = str(channel).strip().lstrip("#").lower()
        message = str(message).strip()
        if channel not in bot_module.CHANNELS:
            raise ValueError("Канал отсутствует в TWITCH_CHANNELS.")
        if not message or len(message) > 450 or "\n" in message or "\r" in message:
            raise ValueError("Сообщение должно содержать от 1 до 450 символов в одной строке.")
        if not self.snapshot()["connected"]:
            raise ValueError("Сначала запустите бота и дождитесь подключения к Twitch.")
        connected_channel = self.bot.get_channel(channel)
        if connected_channel is None:
            raise ValueError("Бот ещё не вошёл в этот канал.")
        try:
            await asyncio.wait_for(connected_channel.send(message), timeout=10)
        except Exception as error:
            raise ValueError("Не удалось отправить сообщение: " + self._sanitize(error)) from None
        self._log(f"[отправлено] #{channel}: {message}")
        return {"sent": True, "channel": channel}

    async def join_channel(self, channel):
        """Join a channel added from the panel while the bot is already connected."""
        channel = str(channel).strip().lstrip("#").lower()
        if not self.snapshot()["connected"]:
            return False
        try:
            await asyncio.wait_for(self.bot.join_channels([channel]), timeout=15)
        except Exception as error:
            self._log("Не удалось подключиться к #" + channel + ": " + self._sanitize(error), "error")
            return False
        self._log(f"Подключаюсь к чату #{channel} по запросу панели.")
        return True

    async def leave_channel(self, channel):
        """Part a channel removed from the panel while the bot is already connected."""
        channel = str(channel).strip().lstrip("#").lower()
        if not self.snapshot()["connected"]:
            return False
        try:
            await asyncio.wait_for(self.bot.part_channels([channel]), timeout=15)
        except Exception as error:
            self._log("Не удалось выйти из #" + channel + ": " + self._sanitize(error), "error")
            return False
        self._log(f"Покидаю чат #{channel} по запросу панели.")
        return True
