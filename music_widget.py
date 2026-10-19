"""Read-only Windows media metadata for an OBS browser overlay."""
import asyncio
import base64
import json
import secrets
import sys
import time


MAX_COVER_BYTES = 1024 * 1024
BROWSER_PROVIDERS = {"spotify-web": "Spotify Web", "yandex-music": "Яндекс Музыка", "youtube-music": "YouTube Music"}


def is_music_app(source):
    """Generic browsers cannot prove which site owns a Windows media session."""
    name = source.casefold().replace("\\", "/").rsplit("/", 1)[-1]
    return (name in {"spotify", "spotify.exe", "yandexmusic", "yandexmusic.exe", "yandex music.exe"}
            or name.startswith("spotifyab.spotifymusic_"))


def cover_data_url(data):
    """Only embed bounded raster images; never arbitrary URLs or SVG documents."""
    if not data or len(data) > MAX_COVER_BYTES:
        return None
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    elif data.startswith(b"\xff\xd8\xff"):
        mime = "image/jpeg"
    elif data[:6] in (b"GIF87a", b"GIF89a"):
        mime = "image/gif"
    elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        mime = "image/webp"
    else:
        return None
    return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")


async def read_cover(thumbnail):
    if thumbnail is None:
        return None
    stream = reader = None
    try:
        from winrt.windows.storage.streams import DataReader
        stream = await thumbnail.open_read_async()
        size = stream.size
        if not 0 < size <= MAX_COVER_BYTES:
            return None
        reader = DataReader(stream)
        loaded = await reader.load_async(size)
        if loaded != size:
            return None
        data = bytearray(loaded)
        reader.read_bytes(data)
        return cover_data_url(data)
    except Exception:
        # Missing or broken artwork must not hide the track itself.
        return None
    finally:
        for resource in (reader, stream):
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    pass


async def windows_sessions():
    if sys.platform != "win32":
        raise RuntimeError("Музыкальный виджет требует Windows 10/11.")
    try:
        from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as Manager
    except ImportError:
        raise RuntimeError("Установите зависимости из requirements.txt и перезапустите панель.") from None
    manager = await Manager.request_async()
    current = manager.get_current_session()
    current_id = current.source_app_user_model_id if current else None
    tracks = []
    for session in manager.get_sessions():
        try:
            if not is_music_app(session.source_app_user_model_id):
                continue
            props = await session.try_get_media_properties_async()
            playback = session.get_playback_info()
            timeline = session.get_timeline_properties()
            source = session.source_app_user_model_id
            duration = max(0, (timeline.end_time - timeline.start_time).total_seconds())
            position = max(0, (timeline.position - timeline.start_time).total_seconds())
            playing = int(playback.playback_status) == 4
            try:
                cover = await asyncio.wait_for(read_cover(props.thumbnail), timeout=.75)
            except (TimeoutError, AttributeError, OSError):
                cover = None
            if playing and duration:
                # Browser timeline updates are sparse; extrapolate from Windows' timestamp.
                elapsed = max(0, min(86400, time.time() - timeline.last_updated_time.timestamp()))
                position += elapsed
            tracks.append({
                "source": source, "title": (props.title or "")[:500],
                "artist": (props.artist or props.album_artist or "")[:300],
                "playing": playing, "duration": duration,
                "position": min(position, duration) if duration else 0,
                "current": source == current_id,
                "cover": cover,
            })
        except Exception:
            # A closing player must not prevent other sessions from being read.
            continue
    return tracks


class MusicWidget:
    def __init__(self, conn, reader=windows_sessions):
        self.conn = conn
        self.reader = reader
        conn.execute("CREATE TABLE IF NOT EXISTS music_widget (id INTEGER PRIMARY KEY CHECK(id=1), settings TEXT NOT NULL)")
        row = conn.execute("SELECT settings FROM music_widget WHERE id=1").fetchone()
        self.settings = json.loads(row[0]) if row else {
            "enabled": False, "source": "", "hide_paused": True, "token": secrets.token_urlsafe(32),
        }
        if "browser_token" not in self.settings:
            self.settings["browser_token"] = secrets.token_urlsafe(32)
            self.save()
        if self.settings["source"] and not is_music_app(self.settings["source"]) and self.settings["source"] not in BROWSER_PROVIDERS:
            self.settings["source"] = ""
            self.save()
        self.lock = asyncio.Lock()
        self.checked = float("-inf")
        self.sources = []
        self.track = None
        self.error = ""
        self.browser_tracks = {}

    def save(self):
        self.conn.execute("INSERT OR REPLACE INTO music_widget(id,settings) VALUES(1,?)", (json.dumps(self.settings),))
        self.conn.commit()

    def configure(self, enabled, source, hide_paused):
        self.settings.update(enabled=enabled, source=source, hide_paused=hide_paused)
        self.save()
        self.track = None
        self.checked = float("-inf")

    def valid_token(self, token):
        return bool(token) and secrets.compare_digest(token, self.settings["token"])

    def valid_browser_token(self, token):
        return bool(token) and secrets.compare_digest(token, self.settings["browser_token"])

    def browser_report(self, key, data):
        now = time.monotonic()
        self.browser_tracks = {k: v for k, v in self.browser_tracks.items() if now - v[0] < 10}
        if len(self.browser_tracks) >= 32 and key not in self.browser_tracks:
            self.browser_tracks.pop(next(iter(self.browser_tracks)))
        self.browser_tracks[key] = (now, data)
        self.checked = float("-inf")

    async def refresh(self):
        async with self.lock:
            if time.monotonic() - self.checked < 2:
                return
            self.error = ""
            self.track = None
            self.sources = []
            try:
                if self.settings["enabled"]:
                    try:
                        tracks = await asyncio.wait_for(self.reader(), timeout=4)
                    except Exception:
                        tracks = []
                        self.error = "Windows не передала данные плеера. Проверьте приложение и зависимости."
                    tracks = [t for t in tracks if is_music_app(t["source"])]
                    now = time.monotonic()
                    tracks += [t for timestamp, t in self.browser_tracks.values() if now - timestamp < 10]
                    self.sources = list(dict.fromkeys(t["source"] for t in tracks))
                    selected = self.settings["source"]
                    candidates = [t for t in tracks if t["title"] and (not selected or t["source"] == selected)]
                    self.track = max(candidates, key=lambda t: (t["playing"], t["current"]), default=None)
                    if self.track:
                        self.error = ""
            except RuntimeError as error:
                self.error = str(error)
            except Exception:
                self.error = "Windows не передала данные плеера. Запустите музыку в том же сеансе Windows, что и панель."
            self.checked = time.monotonic()

    def overlay_state(self):
        track = self.track
        visible = bool(self.settings["enabled"] and track and (track["playing"] or not self.settings["hide_paused"]))
        if not visible:
            return {"visible": False}
        return {"visible": True, "cover": track.get("cover"),
                **{key: track[key] for key in ("title", "artist", "playing", "position", "duration")}}

    def panel_state(self):
        return {"settings": {k: self.settings[k] for k in ("enabled", "source", "hide_paused")},
                "sources": self.sources, "track": self.track, "error": self.error,
                "browser_path": "/api/music/browser?token=" + self.settings["browser_token"],
                "overlay_path": "/overlay/music?token=" + self.settings["token"]}
