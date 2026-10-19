import os
import sqlite3
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer
from music_widget import MusicWidget, cover_data_url, read_cover, MAX_COVER_BYTES, is_music_app

with patch("dotenv.load_dotenv"), patch.dict(os.environ, {}, clear=True):
    from web_panel import create_app, PANEL_KEY


def track(source="Spotify", playing=True, current=True):
    return dict(source=source, title="Track", artist="Artist", playing=playing,
                current=current, position=10, duration=180)


class MusicTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.reader = AsyncMock(return_value=[track()])
        self.widget = MusicWidget(self.conn, self.reader)

    async def test_disabled_does_not_read_media(self):
        await self.widget.refresh()
        self.reader.assert_not_awaited()
        self.assertEqual(self.widget.overlay_state(), {"visible": False})

    async def test_enabled_reads_and_caches_without_exposing_credentials(self):
        self.widget.configure(True, "", True)
        await self.widget.refresh()
        await self.widget.refresh()
        self.reader.assert_awaited_once()
        result = self.widget.overlay_state()
        self.assertTrue(result["visible"])
        self.assertEqual(result["title"], "Track")
        self.assertNotIn("token", result)
        self.assertNotIn("source", result)

    async def test_pause_hides_but_can_be_shown(self):
        self.reader.return_value = [track(playing=False)]
        self.widget.configure(True, "", True)
        await self.widget.refresh()
        self.assertFalse(self.widget.overlay_state()["visible"])
        self.widget.configure(True, "", False)
        await self.widget.refresh()
        self.assertTrue(self.widget.overlay_state()["visible"])

    async def test_selected_missing_player_never_falls_back_to_other_app(self):
        self.widget.configure(True, "Chrome", True)
        await self.widget.refresh()
        self.assertFalse(self.widget.overlay_state()["visible"])

    async def test_playing_player_preferred_over_current_paused_player(self):
        self.reader.return_value = [track("YandexMusic", False, True), track("Spotify", True, False)]
        self.widget.configure(True, "", True)
        await self.widget.refresh()
        self.assertEqual(self.widget.track["source"], "Spotify")

    async def test_failure_hides_stale_track(self):
        self.widget.configure(True, "", True)
        await self.widget.refresh()
        self.reader.side_effect = OSError("API unavailable")
        self.widget.checked = float("-inf")
        await self.widget.refresh()
        self.assertFalse(self.widget.overlay_state()["visible"])
        self.assertTrue(self.widget.error)

    async def test_config_and_link_survive_restart(self):
        self.widget.configure(True, "Spotify", False)
        restarted = MusicWidget(self.conn, self.reader)
        self.assertEqual(restarted.settings, self.widget.settings)
        self.assertTrue(restarted.valid_token(self.widget.settings["token"]))
        self.assertFalse(restarted.valid_token("wrong"))

    async def test_cover_changes_with_track_and_clears_when_missing(self):
        first = track()
        first["cover"] = cover_data_url(b"\x89PNG\r\n\x1a\nexample")
        self.reader.return_value = [first]
        self.widget.configure(True, "", True)
        await self.widget.refresh()
        self.assertEqual(self.widget.overlay_state()["cover"], first["cover"])
        self.assertEqual(self.widget.panel_state()["track"]["cover"], first["cover"])
        self.reader.return_value = [track()]
        self.widget.checked = float("-inf")
        await self.widget.refresh()
        self.assertIsNone(self.widget.overlay_state()["cover"])

    async def test_missing_or_failed_thumbnail_is_optional(self):
        self.assertIsNone(await read_cover(None))
        thumbnail = SimpleNamespace(open_read_async=AsyncMock(side_effect=OSError("closed")))
        self.assertIsNone(await read_cover(thumbnail))

    def test_only_bounded_raster_images_embedded(self):
        for payload, mime in ((b"\x89PNG\r\n\x1a\nabc", "png"), (b"\xff\xd8\xffabc", "jpeg"),
                              (b"GIF89aabc", "gif"), (b"RIFF0000WEBPabc", "webp")):
            self.assertTrue(cover_data_url(payload).startswith(f"data:image/{mime};base64,"))
        for payload in (b"", b"<svg/>", b"https://example.com/a.png", b"\x89PNG\r\n\x1a\n" + b"x" * MAX_COVER_BYTES):
            self.assertIsNone(cover_data_url(payload))

    async def test_generic_browser_and_video_apps_never_reach_overlay(self):
        self.reader.return_value = [track("Chrome"), track("msedge.exe"), track("Twitch"), track("VLC.exe")]
        self.widget.configure(True, "", True)
        await self.widget.refresh()
        self.assertFalse(self.widget.overlay_state()["visible"])
        self.assertEqual(self.widget.sources, [])

    async def test_browser_music_works_without_windows_and_expires(self):
        self.reader.side_effect = OSError("Windows unavailable")
        self.widget.configure(True, "", True)
        self.widget.browser_report("tab1", track("youtube-music"))
        await self.widget.refresh()
        self.assertTrue(self.widget.overlay_state()["visible"])
        self.assertEqual(self.widget.error, "")
        timestamp, value = self.widget.browser_tracks["tab1"]
        self.widget.browser_tracks["tab1"] = (timestamp - 20, value)
        self.widget.checked = float("-inf")
        await self.widget.refresh()
        self.assertFalse(self.widget.overlay_state()["visible"])

    def test_only_dedicated_apps_allowed(self):
        for source in ("Spotify.exe", "SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify", "YandexMusic.exe"):
            self.assertTrue(is_music_app(source))
        for source in ("Chrome", "YandexBrowser.exe", "YouTube", "msedge.exe"):
            self.assertFalse(is_music_app(source))


class MusicRoutesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runtime = SimpleNamespace(stop=AsyncMock())
        self.app = create_app(db_path=Path(self.temp.name) / "test.db", runtime=self.runtime, password="test-password")
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()
        self.panel = self.app[PANEL_KEY]
        self.panel.music.reader = AsyncMock(return_value=[track()])

    async def asyncTearDown(self):
        await self.client.close()
        self.temp.cleanup()

    async def test_overlay_rejects_missing_token(self):
        for path in ("/overlay/music", "/overlay/music/state"):
            response = await self.client.get(path)
            self.assertEqual(response.status, 403)

    async def test_overlay_token_is_not_admin_session(self):
        token = self.panel.music.settings["token"]
        response = await self.client.get("/api/music?token=" + token)
        self.assertEqual(response.status, 401)
        response = await self.client.get("/overlay/music?token=" + token)
        self.assertEqual(response.status, 200)
        self.assertIn("music_overlay.js", await response.text())
        response = await self.client.get("/overlay/music/state?token=" + token)
        self.assertEqual(await response.json(), {"visible": False})

    async def test_settings_require_login_and_csrf(self):
        payload = dict(enabled=True, source="", hide_paused=True)
        response = await self.client.put("/api/music", json=payload)
        self.assertEqual(response.status, 403)
        headers = {"X-CSRF-Token": self.panel.csrf_token}
        response = await self.client.put("/api/music", json=payload, headers=headers)
        self.assertEqual(response.status, 401)
        response = await self.client.post("/login", json={"password": "test-password"}, headers=headers)
        self.assertEqual(response.status, 200)
        cookie = response.cookies["immortal_session"].value
        headers["Cookie"] = "immortal_session=" + cookie
        response = await self.client.put("/api/music", json=payload, headers=headers)
        self.assertEqual(response.status, 200)
        self.assertTrue((await response.json())["settings"]["enabled"])
        response = await self.client.get("/overlay/music/state?token=" + self.panel.music.settings["token"])
        self.assertTrue((await response.json())["visible"])

    async def test_browser_ingest_requires_own_key_and_rejects_video_providers(self):
        data = dict(provider="youtube-music", client="tab1", title="Song", artist="Artist", playing=True)
        for token in ("wrong", self.panel.music.settings["token"]):
            response = await self.client.post("/api/music/browser?token=" + token, json=data)
            self.assertEqual(response.status, 403)
        url = "/api/music/browser?token=" + self.panel.music.settings["browser_token"]
        for provider in ("youtube", "twitch", "chrome"):
            response = await self.client.post(url, json={**data, "provider": provider})
            self.assertEqual(response.status, 400)
        self.panel.music.configure(True, "youtube-music", True)
        response = await self.client.post(url, json=data)
        self.assertEqual(response.status, 200)
        response = await self.client.get("/overlay/music/state?token=" + self.panel.music.settings["token"])
        self.assertEqual((await response.json())["title"], "Song")


if __name__ == "__main__":
    unittest.main()
