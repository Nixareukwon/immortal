import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from stream_info import StreamInfo, StreamInfoError


class StreamInfoTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.service = StreamInfo(SimpleNamespace(access_token="chat-token"))
        self.info = {"user_id": "1", "login": "owner", "client_id": "owner-app",
                     "scopes": ["channel:manage:broadcast", "channel:read:editors"]}
        self.service.request = AsyncMock(return_value={"data": [{"user_id": "2"}]})

    async def test_editor_without_moderator_role(self):
        await self.service.authorize(None, {}, self.info, SimpleNamespace(id="2", is_mod=False))
        self.service.request.assert_awaited_once()

    async def test_moderator_is_not_automatically_editor(self):
        with self.assertRaises(StreamInfoError):
            await self.service.authorize(None, {}, self.info, SimpleNamespace(id="3", is_mod=True))

    async def test_owner_needs_no_editor_scope(self):
        self.info["scopes"] = ["channel:manage:broadcast"]
        await self.service.authorize(None, {}, self.info, SimpleNamespace(id="1"))
        self.service.request.assert_not_awaited()

    async def test_removed_editor_denied_immediately(self):
        await self.service.authorize(None, {}, self.info, SimpleNamespace(id="2"))
        self.service.request.return_value = {"data": []}
        with self.assertRaises(StreamInfoError):
            await self.service.authorize(None, {}, self.info, SimpleNamespace(id="2"))

    async def test_missing_editor_scope_denied(self):
        self.info["scopes"] = ["channel:manage:broadcast"]
        with self.assertRaises(StreamInfoError):
            await self.service.authorize(None, {}, self.info, SimpleNamespace(id="2"))

    async def test_credentials_use_owner_app_and_reject_other_channel(self):
        response = AsyncMock()
        response.status = 200
        response.json.return_value = self.info
        response.__aenter__.return_value = response
        session = SimpleNamespace(get=Mock(return_value=response))
        with patch.dict("os.environ", {"TWITCH_BROADCASTER_TOKEN_OWNER": "oauth:owner-token"}):
            headers, _ = await self.service.credentials(session, "owner")
            self.assertEqual(headers, {"Authorization": "Bearer owner-token", "Client-Id": "owner-app"})
            with self.assertRaises(StreamInfoError):
                await self.service.credentials(session, "another")

    async def test_update_sends_only_requested_field(self):
        self.service.credentials = AsyncMock(return_value=({}, self.info))
        ctx = SimpleNamespace(channel=SimpleNamespace(name="owner"),
                              author=SimpleNamespace(id="2"), send=AsyncMock())
        await self.service.update(ctx, title="New title")
        self.assertEqual(self.service.request.await_args.kwargs["json"], {"title": "New title"})
        self.assertEqual(self.service.request.await_args.kwargs["params"], {"broadcaster_id": "1"})

    async def test_unauthorized_user_never_patches(self):
        self.service.credentials = AsyncMock(return_value=({}, self.info))
        ctx = SimpleNamespace(channel=SimpleNamespace(name="owner"),
                              author=SimpleNamespace(id="3"), send=AsyncMock())
        await self.service.update(ctx, title="New title")
        self.assertEqual(self.service.request.await_count, 1)
        self.assertEqual(self.service.request.await_args.args[2], "GET")

    async def test_category_resolves_id(self):
        self.service.credentials = AsyncMock(return_value=({}, self.info))
        self.service.request.side_effect = [{"data": [{"name": "Just Chatting", "id": "509658"}]}, {}]
        ctx = SimpleNamespace(channel=SimpleNamespace(name="owner"),
                              author=SimpleNamespace(id="1"), send=AsyncMock())
        await self.service.update(ctx, category="Just Chatting")
        self.assertEqual(self.service.request.await_args.kwargs["json"], {"game_id": "509658"})


if __name__ == "__main__":
    unittest.main()
