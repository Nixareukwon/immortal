"""Вход через Twitch по коду устройства (Device Code Flow) и привязка аккаунтов."""
import asyncio
import os
import secrets
import time

import aiohttp
from aiohttp import web

DEVICE_URL = "https://id.twitch.tv/oauth2/device"
TOKEN_URL = "https://id.twitch.tv/oauth2/token"
VALIDATE_URL = "https://id.twitch.tv/oauth2/validate"
GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"
VERIFY_PREFIX = "https://www.twitch.tv/"
OWNER_TOKEN_ENV = "TWITCH_BROADCASTER_TOKEN"
REFRESH_INTERVAL = 900
NETWORK_ERRORS = (aiohttp.ClientError, asyncio.TimeoutError, ValueError, RuntimeError)

# Полный список прав Twitch из https://dev.twitch.tv/docs/authentication/scopes/.
# Запрашивается только у владельца при привязке: токен сохраняется, подставляется
# боту (TWITCH_BROADCASTER_TOKEN) и обновляется автоматически.
OWNER_SCOPES = (
    "analytics:read:extensions", "analytics:read:games", "bits:read", "channel:bot",
    "channel:manage:ads", "channel:read:ads", "channel:manage:broadcast", "channel:read:charity",
    "channel:manage:clips", "channel:edit:commercial", "channel:read:editors",
    "channel:manage:extensions", "channel:read:goals", "channel:read:guest_star",
    "channel:manage:guest_star", "channel:read:hype_train", "channel:manage:moderators",
    "channel:read:polls", "channel:manage:polls", "channel:read:predictions",
    "channel:manage:predictions", "channel:manage:raids", "channel:read:redemptions",
    "channel:manage:redemptions", "channel:manage:schedule", "channel:read:stream_key",
    "channel:read:subscriptions", "channel:manage:videos", "channel:read:vips",
    "channel:manage:vips", "channel:moderate", "clips:edit", "editor:manage:clips",
    "moderation:read", "moderator:manage:announcements", "moderator:manage:automod",
    "moderator:read:automod_settings", "moderator:manage:automod_settings",
    "moderator:read:banned_users", "moderator:manage:banned_users",
    "moderator:read:blocked_terms", "moderator:manage:blocked_terms",
    "moderator:read:chat_messages", "moderator:manage:chat_messages",
    "moderator:read:chat_settings", "moderator:manage:chat_settings",
    "moderator:read:chatters", "moderator:read:followers", "moderator:read:guest_star",
    "moderator:manage:guest_star", "moderator:read:moderators", "moderator:read:shield_mode",
    "moderator:manage:shield_mode", "moderator:read:shoutouts", "moderator:manage:shoutouts",
    "moderator:read:suspicious_users", "moderator:manage:suspicious_users",
    "moderator:read:unban_requests", "moderator:manage:unban_requests", "moderator:read:vips",
    "moderator:read:warnings", "moderator:manage:warnings", "user:bot", "user:edit",
    "user:edit:broadcast", "user:read:blocked_users", "user:manage:blocked_users",
    "user:read:broadcast", "user:read:chat", "user:manage:chat_color", "user:read:email",
    "user:read:emotes", "user:read:follows", "user:read:moderated_channels",
    "user:read:subscriptions", "user:read:whispers", "user:manage:whispers",
    "user:write:chat", "chat:edit", "chat:read", "whispers:read",
)


class TwitchAuth:
    """Одноразовые коды Twitch: вход в панель и привязка к личному аккаунту.

    Обычный редирект не используется, поэтому меняющийся адрес временного
    Cloudflare-туннеля не мешает: код всегда вводится на twitch.tv/activate.
    При привязке владельца запрашиваются все права, а полученный токен
    сохраняется и обновляется, чтобы бот мог работать с Twitch API сам.
    """

    def __init__(self, panel):
        self.panel = panel
        self.client_id = (os.getenv("TWITCH_CLIENT_ID") or "").strip()
        self.client_secret = (os.getenv("TWITCH_CLIENT_SECRET") or "").strip()
        self.flows = {}
        self.starts = {}
        self.http = None
        self.refresh_error = ""
        self.env_original = os.environ.get(OWNER_TOKEN_ENV)
        self.env_managed = False
        conn = panel.db.conn
        conn.execute("CREATE TABLE IF NOT EXISTS panel_twitch_links ("
                     "twitch_id TEXT PRIMARY KEY, twitch_login TEXT NOT NULL, username TEXT NOT NULL UNIQUE)")
        conn.execute("CREATE TABLE IF NOT EXISTS panel_twitch_tokens ("
                     "id INTEGER PRIMARY KEY CHECK (id = 1), twitch_id TEXT NOT NULL, twitch_login TEXT NOT NULL, "
                     "access_token TEXT NOT NULL, refresh_token TEXT, expires REAL NOT NULL, scope TEXT NOT NULL, "
                     "updated REAL NOT NULL)")
        conn.commit()

    @property
    def configured(self):
        return bool(self.client_id)

    def link_for(self, username):
        return self.panel.db.conn.execute(
            "SELECT twitch_id, twitch_login FROM panel_twitch_links WHERE username=?", (username,)).fetchone()

    def links(self):
        return {row["username"]: row["twitch_login"] for row in
                self.panel.db.conn.execute("SELECT username, twitch_login FROM panel_twitch_links")}

    def token_row(self):
        return self.panel.db.conn.execute("SELECT * FROM panel_twitch_tokens WHERE id=1").fetchone()

    def save_token(self, conn, twitch_id, login, data):
        lifetime = data.get("expires_in")
        lifetime = lifetime if type(lifetime) in (int, float) and lifetime > 0 else 0
        refresh = data.get("refresh_token")
        scope = data.get("scope")
        if isinstance(scope, (list, tuple)):
            scope = " ".join(str(item) for item in scope)
        conn.execute("DELETE FROM panel_twitch_tokens")
        conn.execute("INSERT INTO panel_twitch_tokens"
                     "(id,twitch_id,twitch_login,access_token,refresh_token,expires,scope,updated) "
                     "VALUES(1,?,?,?,?,?,?,?)",
                     (twitch_id, login, str(data.get("access_token") or ""),
                      refresh if isinstance(refresh, str) else None, time.time() + lifetime,
                      str(scope or ""), time.time()))

    def restore_env(self):
        if not self.env_managed:
            return
        self.env_managed = False
        if self.env_original is None:
            os.environ.pop(OWNER_TOKEN_ENV, None)
        else:
            os.environ[OWNER_TOKEN_ENV] = self.env_original

    async def close(self):
        if self.http is not None:
            http, self.http = self.http, None
            await http.close()

    async def _http(self):
        if self.http is None or self.http.closed:
            self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        return self.http

    async def device_start(self, scopes):
        """Запросить у Twitch код активации. Переопределяется в тестах."""
        http = await self._http()
        async with http.post(DEVICE_URL, data={"client_id": self.client_id, "scopes": scopes}) as response:
            data = await response.json(content_type=None)
            status = response.status
        if status != 200 or not isinstance(data, dict):
            raise RuntimeError("Twitch не выдал код активации.")
        return data

    async def device_token(self, device_code):
        """Обменять код активации на токен. Переопределяется в тестах."""
        http = await self._http()
        payload = {"client_id": self.client_id, "scopes": "", "device_code": device_code, "grant_type": GRANT_TYPE}
        if self.client_secret:
            payload["client_secret"] = self.client_secret
        async with http.post(TOKEN_URL, data=payload) as response:
            data = await response.json(content_type=None)
        if not isinstance(data, dict):
            raise RuntimeError("Twitch вернул неожиданный ответ.")
        return data

    async def device_refresh(self, refresh_token):
        """Обновить токен владельца. Переопределяется в тестах."""
        http = await self._http()
        payload = {"client_id": self.client_id, "grant_type": "refresh_token", "refresh_token": refresh_token}
        if self.client_secret:
            payload["client_secret"] = self.client_secret
        async with http.post(TOKEN_URL, data=payload) as response:
            data = await response.json(content_type=None)
            status = response.status
        if status != 200 or not isinstance(data, dict) or not data.get("access_token"):
            message = data.get("message") if isinstance(data, dict) else ""
            raise RuntimeError(str(message or "Twitch отклонил обновление токена"))
        return data

    async def device_identity(self, access_token):
        """Определить аккаунт Twitch по токену. Сам токен нигде не сохраняется."""
        http = await self._http()
        async with http.get(VALIDATE_URL, headers={"Authorization": f"Bearer {access_token}"}) as response:
            data = await response.json(content_type=None)
            status = response.status
        if status != 200 or not isinstance(data, dict) or not data.get("user_id"):
            raise RuntimeError("Не удалось определить аккаунт Twitch.")
        return str(data["user_id"]), str(data.get("login") or "").lower()

    async def owner_token(self):
        """Действующий токен владельца, при необходимости обновлённый."""
        row = self.token_row()
        if row is None:
            return None
        if row["expires"] > time.time() + 300:
            return row["access_token"]
        if not row["refresh_token"]:
            raise RuntimeError("Срок действия токена владельца истёк. Привяжите Twitch заново в разделе «Доступ к панели».")
        try:
            data = await self.device_refresh(row["refresh_token"])
        except NETWORK_ERRORS as error:
            raise RuntimeError("Twitch не обновил токен владельца: " + (str(error) or "нет связи") +
                               ". Если повторится, добавьте TWITCH_CLIENT_SECRET в .env.") from error
        with self.panel.transaction() as conn:
            self.save_token(conn, row["twitch_id"], row["twitch_login"], data)
        return data["access_token"]

    async def apply_owner_token(self):
        """Подставить боту действующий токен владельца (или вернуть ручной из .env)."""
        try:
            token = await self.owner_token()
        except NETWORK_ERRORS as error:
            self.refresh_error = str(error)
            return None
        if token:
            os.environ[OWNER_TOKEN_ENV] = token
            self.env_managed = True
            self.refresh_error = ""
        elif self.env_managed:
            self.restore_env()
        return token

    async def refresh_loop(self):
        while True:
            try:
                await self.apply_owner_token()
            except Exception as error:
                # Фоновая задача не должна падать: ошибку покажет раздел «Доступ к панели».
                self.refresh_error = str(error)
            await asyncio.sleep(REFRESH_INTERVAL)

    def limit(self, request):
        from web_panel import InputError
        now = time.time()
        remote = request.remote or "local"
        recent = [stamp for stamp in self.starts.get(remote, []) if stamp > now - 60]
        if len(recent) >= 10:
            raise InputError("Слишком много попыток. Подождите минуту.", 429)
        self.starts[remote] = [*recent, now]

    async def start(self, request):
        from web_panel import InputError
        if not self.configured:
            raise InputError("Вход через Twitch не настроен: добавьте TWITCH_CLIENT_ID в .env.", 503)
        self.limit(request)
        data = await self.panel.body(request)
        purpose = data.get("purpose")
        if purpose not in {"login", "link", "channel"}:
            raise InputError("Неизвестное действие.")
        username = None
        if purpose in {"link", "channel"}:
            if not self.panel.authenticated(request):
                action = "привязать Twitch" if purpose == "link" else "подключить канал"
                raise InputError(f"Чтобы {action}, войдите в панель.", 401)
            username = self.panel.accounts.identity(request)
        if purpose == "channel":
            status = self.panel.channels.add_status(username)
            if not status["allowed"]:
                raise InputError(status["hint"], 403 if status["needs_link"] else 409)
        full = purpose == "link" and username == "owner"
        now = time.time()
        self.flows = {key: flow for key, flow in self.flows.items() if flow["expires"] > now}
        try:
            code = await self.device_start(" ".join(OWNER_SCOPES) if full else "")
        except NETWORK_ERRORS as error:
            raise InputError("Не удалось получить код у Twitch. Повторите через минуту.", 502) from error
        device_code = str(code.get("device_code") or "")
        if not device_code:
            raise InputError("Twitch не выдал код активации. Повторите через минуту.", 502)
        uri = str(code.get("verification_uri") or "")
        if not uri.startswith(VERIFY_PREFIX):
            uri = VERIFY_PREFIX + "activate"
        interval = code.get("interval")
        expires = code.get("expires_in")
        interval = interval if type(interval) is int and 1 <= interval <= 60 else 5
        expires = expires if type(expires) is int and 60 <= expires <= 3600 else 1800
        flow_id = secrets.token_urlsafe(24)
        self.flows[flow_id] = {"device_code": device_code, "purpose": purpose, "username": username,
                               "interval": interval, "expires": now + expires, "next_poll": 0.0}
        return web.json_response({"flow": flow_id, "code": str(code.get("user_code") or ""),
                                  "uri": uri, "interval": interval, "expires": expires, "full": full})

    async def poll(self, request):
        from web_panel import InputError
        data = await self.panel.body(request)
        flow_id = data.get("flow")
        flow = self.flows.get(flow_id) if isinstance(flow_id, str) else None
        if flow is None or flow["expires"] <= time.time():
            if flow is not None:
                self.flows.pop(flow_id, None)
            return web.json_response({"status": "expired"})
        if time.time() < flow["next_poll"]:
            return web.json_response({"status": "pending"})
        flow["next_poll"] = time.time() + flow["interval"]
        try:
            token = await self.device_token(flow["device_code"])
        except NETWORK_ERRORS as error:
            raise InputError("Twitch временно недоступен. Повторите попытку.", 502) from error
        if not token.get("access_token"):
            message = str(token.get("message") or "").strip().lower()
            if "pending" in message:
                return web.json_response({"status": "pending"})
            if "slow" in message:
                flow["interval"] = min(flow["interval"] + 5, 60)
                return web.json_response({"status": "pending"})
            self.flows.pop(flow_id, None)
            if "denied" in message or "cancel" in message:
                return web.json_response({"status": "denied"})
            if "client" in message or "secret" in message:
                return web.json_response({"status": "config"})
            if "expired" in message or "device code" in message:
                return web.json_response({"status": "expired"})
            return web.json_response({"status": "failed"})
        try:
            twitch_id, login = await self.device_identity(token["access_token"])
        except NETWORK_ERRORS:
            self.flows.pop(flow_id, None)
            return web.json_response({"status": "failed"})
        if flow["purpose"] == "login":
            row = self.panel.db.conn.execute(
                "SELECT username FROM panel_twitch_links WHERE twitch_id=?", (twitch_id,)).fetchone()
            self.flows.pop(flow_id, None)
            if row is None:
                return web.json_response({"status": "unlinked", "login": login})
            return self.panel.accounts.session(request, row["username"], {"status": "ok", "login": login})
        username = flow["username"]
        self.flows.pop(flow_id, None)
        if not username or not self.panel.authenticated(request) or self.panel.accounts.identity(request) != username:
            raise InputError("Сеанс истёк. Войдите в панель и повторите действие.", 401)
        if flow["purpose"] == "channel":
            if username != "owner":
                expected = self.links().get(username, "")
                if expected.casefold() != login:
                    return web.json_response({"status": "foreign", "login": login, "expected": expected})
            try:
                result = await self.panel.channels.connect(username, login)
            except InputError as error:
                return web.json_response({"status": "channel-error", "error": str(error), "login": login})
            return web.json_response({"status": "channel", "login": login, "joined": result["joined"]})
        stored = username == "owner" and isinstance(token.get("refresh_token"), str)
        with self.panel.transaction() as conn:
            taken = conn.execute("SELECT username FROM panel_twitch_links WHERE twitch_id=?", (twitch_id,)).fetchone()
            if taken and taken["username"] != username:
                raise InputError("Этот Twitch уже привязан к другому аккаунту панели.", 409)
            conn.execute("DELETE FROM panel_twitch_links WHERE username=?", (username,))
            conn.execute("INSERT INTO panel_twitch_links(twitch_id, twitch_login, username) VALUES(?,?,?)",
                         (twitch_id, login, username))
            if stored:
                self.save_token(conn, twitch_id, login, token)
        if stored:
            await self.apply_owner_token()
        return web.json_response({"status": "linked", "login": login, "full": stored})

    async def state(self, request):
        username = self.panel.accounts.identity(request)
        row = self.link_for(username)
        payload = {"configured": self.configured, "owner": username == "owner",
                   "link": {"login": row["twitch_login"]} if row else None}
        if username == "owner":
            token = self.token_row()
            payload["enhanced"] = bool(token and token["refresh_token"])
            payload["scopes"] = len((token["scope"] or "").split()) if token else 0
            payload["refresh_error"] = self.refresh_error
        return web.json_response(payload)

    async def unlink(self, request):
        from web_panel import InputError
        data = await self.panel.body(request)
        username = self.panel.accounts.identity(request)
        requested = data.get("username")
        if isinstance(requested, str) and requested and requested != username:
            if username != "owner":
                raise InputError("Отвязывать чужой Twitch может только владелец.", 403)
            username = requested
        with self.panel.transaction() as conn:
            conn.execute("DELETE FROM panel_twitch_links WHERE username=?", (username,))
            if username == "owner":
                conn.execute("DELETE FROM panel_twitch_tokens")
        if username == "owner":
            self.restore_env()
        return web.json_response({"ok": True})
