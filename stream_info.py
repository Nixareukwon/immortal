"""Stream metadata changes with broadcaster OAuth and live editor checks."""

import os

import aiohttp


class StreamInfoError(Exception):
    pass


class StreamInfo:
    def __init__(self, bot):
        self.bot = bot

    async def credentials(self, session, channel):
        token = (os.getenv(f"TWITCH_BROADCASTER_TOKEN_{channel.upper()}")
                 or os.getenv("TWITCH_BROADCASTER_TOKEN")
                 or self.bot.access_token).strip().strip('"\'').removeprefix("oauth:")
        async with session.get(
            "https://id.twitch.tv/oauth2/validate",
            headers={"Authorization": f"OAuth {token}"},
        ) as response:
            if response.status != 200:
                raise StreamInfoError("Токен владельца недействителен или истёк. Обновите TWITCH_BROADCASTER_TOKEN.")
            info = await response.json()
        if str(info.get("login", "")).casefold() != channel.casefold():
            raise StreamInfoError("Нужен TWITCH_BROADCASTER_TOKEN владельца этого канала. Токен редактора не подходит для Twitch API.")
        if "channel:manage:broadcast" not in info.get("scopes", []):
            raise StreamInfoError("Токену владельца нужно разрешение channel:manage:broadcast.")
        return {"Authorization": f"Bearer {token}", "Client-Id": info["client_id"]}, info

    async def request(self, session, headers, method, endpoint, **kwargs):
        async with session.request(
            method, f"https://api.twitch.tv/helix/{endpoint}", headers=headers, **kwargs
        ) as response:
            if response.status == 204:
                return {}
            if response.status != 200:
                raise StreamInfoError(f"Twitch отклонил запрос (HTTP {response.status}). Проверьте срок действия и разрешения токена владельца.")
            return await response.json()

    async def authorize(self, session, headers, info, author):
        author_id = str(getattr(author, "id", "") or "")
        if author_id == str(info["user_id"]):
            return
        if "channel:read:editors" not in info.get("scopes", []):
            raise StreamInfoError("Для проверки редакторов токену владельца нужно разрешение channel:read:editors.")
        editors = await self.request(session, headers, "GET", "channels/editors",
                                     params={"broadcaster_id": info["user_id"]})
        if not author_id or not any(str(item["user_id"]) == author_id for item in editors.get("data", [])):
            raise StreamInfoError("Менять название и категорию могут только владелец и редакторы этого канала.")

    async def update(self, ctx, *, title=None, category=None):
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
                headers, info = await self.credentials(session, ctx.channel.name)
                await self.authorize(session, headers, info, ctx.author)
                payload = {}
                if title is not None:
                    payload["title"] = title
                if category is not None:
                    result = await self.request(session, headers, "GET", "games", params={"name": category})
                    selected = next((g for g in result.get("data", []) if g["name"].casefold() == category.casefold()), None)
                    if selected is None:
                        raise StreamInfoError("Категория не найдена. Укажите её точное название на Twitch.")
                    payload["game_id"] = selected["id"]
                await self.request(session, headers, "PATCH", "channels",
                                   params={"broadcaster_id": info["user_id"]}, json=payload)
        except StreamInfoError as error:
            await ctx.send(str(error))
            return
        except (aiohttp.ClientError, TimeoutError, ValueError, KeyError, TypeError):
            await ctx.send("Не удалось изменить параметры стрима: ошибка связи или ответа Twitch. Попробуйте позже.")
            return
        await ctx.send("✅ Название стрима успешно изменено." if title is not None
                       else f"✅ Категория стрима успешно изменена на «{selected['name']}».")
