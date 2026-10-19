"""Invitation-only accounts for the shared bot panel."""
import asyncio
import hashlib
import hmac
import re
import secrets
import time

from aiohttp import web


def digest(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 600_000).hex()


class Accounts:
    def __init__(self, panel):
        self.panel = panel
        self.identities = {}
        conn = panel.db.conn
        conn.execute("CREATE TABLE IF NOT EXISTS panel_accounts (username TEXT PRIMARY KEY, salt TEXT NOT NULL, hash TEXT NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS panel_invites (hash TEXT PRIMARY KEY, expires REAL NOT NULL)")
        conn.commit()
        self.hash_lock = asyncio.Semaphore(2)

    def identity(self, request):
        return self.identities.get(request.cookies.get("immortal_session", ""), "owner")

    async def hash(self, password, salt):
        async with self.hash_lock:
            return await asyncio.to_thread(digest, password, salt)

    async def verify(self, username, password):
        if not isinstance(password, str) or len(password) > 128:
            return False
        row = self.panel.db.conn.execute("SELECT salt, hash FROM panel_accounts WHERE username=?", (username,)).fetchone()
        salt = row["salt"] if row else "00" * 16
        actual = await self.hash(password, salt)
        current = self.panel.db.conn.execute("SELECT hash FROM panel_accounts WHERE username=?", (username,)).fetchone()
        return bool(row and current and current[0] == row["hash"] and hmac.compare_digest(actual, row["hash"]))

    def session(self, request, username, payload=None):
        panel = self.panel
        now = time.time()
        panel.sessions = {key: expiry for key, expiry in panel.sessions.items() if expiry > now}
        self.identities = {key: name for key, name in self.identities.items() if key in panel.sessions}
        sid = secrets.token_urlsafe(32)
        panel.sessions[sid] = now + 12 * 3600
        self.identities[sid] = username
        response = web.json_response({"ok": True, **(payload or {})})
        response.set_cookie("immortal_session", sid, httponly=True, samesite="Strict",
                            secure=request.secure, max_age=12 * 3600, path="/")
        return response

    def limit(self, request):
        from web_panel import InputError
        now = time.time()
        attempts = self.panel.login_attempts
        key = request.remote or "local"
        recent = [stamp for stamp in attempts.get(key, []) if stamp > now - 60]
        if len(recent) >= 10:
            raise InputError("Слишком много попыток. Подождите минуту.", 429)
        attempts[key] = [*recent, now]

    async def register(self, request):
        from web_panel import InputError, ROOT
        if request.method == "GET":
            html = (ROOT / "templates/register.html").read_text(encoding="utf-8")
            return web.Response(text=html.replace("{{csrf_token}}", self.panel.csrf_token), content_type="text/html")
        self.limit(request)
        data = await self.panel.body(request)
        token = data.get("invite", "")
        if not isinstance(token, str) or len(token) > 100:
            raise InputError("Неверное приглашение.")
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        row = self.panel.db.conn.execute("SELECT expires FROM panel_invites WHERE hash=?", (token_hash,)).fetchone()
        if not row or row[0] <= time.time():
            raise InputError("Приглашение истекло или уже использовано. Попросите новую ссылку.")
        username = data.get("username", "")
        if not isinstance(username, str):
            raise InputError("Укажите логин.")
        username = username.strip().lower()
        if not re.fullmatch(r"[a-z0-9_]{3,32}", username) or username == "owner":
            raise InputError("Логин: 3–32 латинские буквы, цифры или _. Имя owner занято.")
        password = data.get("password")
        if not isinstance(password, str) or not 12 <= len(password) <= 128:
            raise InputError("Пароль должен содержать от 12 до 128 символов.")
        if password != data.get("confirm"):
            raise InputError("Пароли не совпадают.")
        salt = secrets.token_hex(16)
        hashed = await self.hash(password, salt)
        with self.panel.transaction() as conn:
            if conn.execute("SELECT 1 FROM panel_accounts WHERE username=?", (username,)).fetchone():
                raise InputError("Этот логин уже занят.", 409)
            deleted = conn.execute("DELETE FROM panel_invites WHERE hash=? AND expires>?", (token_hash, time.time()))
            if deleted.rowcount != 1:
                raise InputError("Приглашение истекло или уже использовано.")
            conn.execute("INSERT INTO panel_accounts VALUES (?, ?, ?)", (username, salt, hashed))
        return self.session(request, username)

    async def access(self, request):
        from web_panel import InputError, ROOT
        owner = self.identity(request) == "owner"
        if request.path == "/access":
            html = (ROOT / "templates/access.html").read_text(encoding="utf-8")
            return web.Response(text=html.replace("{{csrf_token}}", self.panel.csrf_token), content_type="text/html")
        if not owner:
            raise InputError("Управлять доступом может только владелец.", 403)
        if request.method == "GET":
            users = [row[0] for row in self.panel.db.conn.execute("SELECT username FROM panel_accounts ORDER BY username")]
            return web.json_response({"users": users, "twitch": self.panel.twitch.links()})
        if request.method == "DELETE":
            username = request.match_info["username"]
            with self.panel.transaction() as conn:
                conn.execute("DELETE FROM panel_accounts WHERE username=?", (username,))
                conn.execute("DELETE FROM panel_twitch_links WHERE username=?", (username,))
            for sid, name in list(self.identities.items()):
                if name == username:
                    self.panel.sessions.pop(sid, None)
                    self.identities.pop(sid, None)
            return web.json_response({"ok": True})
        if not self.panel.password:
            raise InputError("Сначала установите пароль владельца панели.")
        token = secrets.token_urlsafe(32)
        with self.panel.transaction() as conn:
            conn.execute("DELETE FROM panel_invites WHERE expires<=?", (time.time(),))
            conn.execute("INSERT INTO panel_invites VALUES (?, ?)", (hashlib.sha256(token.encode()).hexdigest(), time.time() + 86400))
        origin = self.panel.public_origin or f"{request.scheme}://{request.host}"
        return web.json_response({"url": origin + "/register?invite=" + token})
