"""Bot channels: the .env base plus channels connected right from the panel.

The owner manages every channel. An invited account may connect the bot only
to the channel of its own linked Twitch login and manages only that channel;
channels from TWITCH_CHANNELS (.env) always belong to the owner.
"""
import os
import re
import time

from aiohttp import web

import bot as bot_module


CHANNEL_RE = re.compile(r"[a-z0-9_]{1,25}\Z")
MAX_EXTRA_CHANNELS = 25


def env_channels():
    return [
        value.strip().lstrip("#").lower()
        for value in os.getenv("TWITCH_CHANNELS", "").split(",")
        if value.strip()
    ]


def normalize_channel(raw):
    return str(raw or "").strip().lstrip("#").lower()


class Channels:
    def __init__(self, panel):
        self.panel = panel
        conn = panel.db.conn
        conn.execute("CREATE TABLE IF NOT EXISTS panel_channels ("
                     "channel TEXT PRIMARY KEY, username TEXT NOT NULL, created REAL NOT NULL)")
        conn.commit()
        self.apply()

    def registry(self):
        return {row["channel"]: row["username"] for row in
                self.panel.db.conn.execute("SELECT channel, username FROM panel_channels")}

    def owned_by(self, username):
        return [row["channel"] for row in self.panel.db.conn.execute(
            "SELECT channel FROM panel_channels WHERE username=? ORDER BY created", (username,))]

    def owners_map(self, channels):
        registry = self.registry()
        return {channel: registry.get(channel) for channel in channels}

    def visible(self, username):
        if username == "owner":
            return list(bot_module.CHANNELS)
        return self.owned_by(username)

    def may_manage(self, username, channel):
        if username == "owner":
            return channel in bot_module.CHANNELS
        return self.registry().get(channel) == username

    def add_status(self, username):
        """What the panel UI should offer the current account on the overview page."""
        if username == "owner":
            if len(self.registry()) >= MAX_EXTRA_CHANNELS:
                return {"allowed": False, "channel": None, "needs_link": False,
                        "hint": "Достигнут лимит подключённых каналов."}
            return {"allowed": True, "channel": None, "needs_link": False,
                    "hint": "Подключение проходит через вход в Twitch: вы войдёте "
                            "в аккаунт канала и подтвердите подключение."}
        login = self.panel.twitch.links().get(username, "")
        if not login:
            return {"allowed": False, "channel": None, "needs_link": True,
                    "hint": "Подключить можно только свой канал. Сначала привяжите Twitch в разделе «Доступ к панели»."}
        if login in env_channels() or login in self.registry():
            return {"allowed": False, "channel": login, "needs_link": False,
                    "hint": f"Ваш канал #{login} уже подключён."}
        if len(self.registry()) >= MAX_EXTRA_CHANNELS:
            return {"allowed": False, "channel": login, "needs_link": False,
                    "hint": "Достигнут лимит подключённых каналов."}
        return {"allowed": True, "channel": login, "needs_link": False,
                "hint": f"Вы войдёте в Twitch как #{login} и подключите бота к своему каналу."}

    def apply(self):
        """Rebuild bot_module.CHANNELS: .env channels first, then panel ones."""
        base = env_channels()
        extra = [row["channel"] for row in self.panel.db.conn.execute(
            "SELECT channel FROM panel_channels ORDER BY created") if row["channel"] not in base]
        channels = base + extra
        if list(bot_module.CHANNELS) != channels:
            bot_module.CHANNELS[:] = channels
        return list(bot_module.CHANNELS)

    async def connect(self, username, channel):
        """Подключить канал после входа в Twitch-аккаунт этого канала."""
        from web_panel import InputError
        channel = normalize_channel(channel)
        if not CHANNEL_RE.fullmatch(channel):
            raise InputError("Не удалось определить канал Twitch-аккаунта.")
        if channel in env_channels():
            raise InputError("Этот канал уже подключён через TWITCH_CHANNELS в .env.", 409)
        if channel in self.registry():
            raise InputError("Этот канал уже подключён.", 409)
        if username != "owner":
            login = self.panel.twitch.links().get(username, "")
            if not login:
                raise InputError("Сначала привяжите Twitch в разделе «Доступ к панели»: "
                                 "подключить бота можно только к своему каналу.", 403)
            if login.casefold() != channel:
                raise InputError(f"Приглашённый может подключить только свой канал — #{login}.", 403)
        if len(self.registry()) >= MAX_EXTRA_CHANNELS:
            raise InputError("Достигнут лимит подключённых каналов.", 409)
        with self.panel.transaction() as conn:
            if conn.execute("SELECT 1 FROM panel_channels WHERE channel=?", (channel,)).fetchone():
                raise InputError("Этот канал уже подключён.", 409)
            conn.execute("INSERT INTO panel_channels(channel, username, created) VALUES(?,?,?)",
                         (channel, username, time.time()))
        self.apply()
        joined = await self._join(channel)
        return {"ok": True, "channel": channel, "joined": joined}

    async def remove(self, request):
        from web_panel import InputError
        username = self.panel.accounts.identity(request)
        channel = normalize_channel(request.match_info["channel"])
        owner_name = self.registry().get(channel)
        if owner_name is None:
            if channel in env_channels():
                raise InputError("Этот канал задан в TWITCH_CHANNELS (.env) и отключается правкой файла.")
            raise InputError("Канал не подключён из панели.", 404)
        if username != "owner" and owner_name != username:
            raise InputError("Этот канал подключён другим аккаунтом панели.", 403)
        with self.panel.transaction() as conn:
            conn.execute("DELETE FROM panel_channels WHERE channel=?", (channel,))
        self.apply()
        await self._leave(channel)
        return web.json_response({"ok": True, "channel": channel})

    async def _join(self, channel):
        join = getattr(self.panel.runtime, "join_channel", None)
        if join is None:
            return False
        return bool(await join(channel))

    async def _leave(self, channel):
        leave = getattr(self.panel.runtime, "leave_channel", None)
        if leave is None:
            return False
        return bool(await leave(channel))
