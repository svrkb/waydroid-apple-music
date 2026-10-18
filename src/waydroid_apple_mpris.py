#!/usr/bin/python3
"""Expose Apple Music in Waydroid as a desktop MPRIS player."""

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
import unicodedata
from difflib import SequenceMatcher
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import dbus
import dbus.service
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib


BUS_NAME = "org.mpris.MediaPlayer2.WaydroidAppleMusic"
OBJECT_PATH = "/org/mpris/MediaPlayer2"
ROOT_IFACE = "org.mpris.MediaPlayer2"
PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"
PROPERTIES_IFACE = "org.freedesktop.DBus.Properties"
PACKAGE = "com.apple.android.music"
LEASES_FILE = "/var/lib/misc/dnsmasq.waydroid0.leases"


def env_flag(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def env_float(name, default, minimum):
    try:
        value = float(os.environ.get(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, value)


def base_title(value):
    base = (value or "").strip()
    while True:
        stripped = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]\s*$", "", base).strip()
        if not stripped or stripped == base:
            return base
        base = stripped


CATALOG_ARTWORK = env_flag("WAYDROID_APPLE_MPRIS_CATALOG_ARTWORK", False)
FORCE_VOLUME = env_flag("WAYDROID_APPLE_MPRIS_FORCE_VOLUME", False)
AUDIO_WATCHDOG = env_flag("WAYDROID_APPLE_MPRIS_AUDIO_WATCHDOG", False)
WATCHDOG_GRACE_SECONDS = env_float(
    "WAYDROID_APPLE_MPRIS_WATCHDOG_GRACE", 1.0, 0.05
)
WATCHDOG_COOLDOWN_SECONDS = 600
# An ADB connection verified within this window is trusted without probing
# again, so a control press costs a single adb call.
ADB_CONNECTION_TTL = 15.0
# When Waydroid is unreachable, poll at most this often instead of on every
# poll cycle. Control presses always force a fresh connection attempt.
ADB_RETRY_BACKOFF = 5.0
# How long a control press's expected status is trusted before a differing
# read is allowed to override it. Android reacts to a dispatch in well under
# this, so a read that predates the reaction cannot flicker the desktop.
PREVIEW_GRACE_SECONDS = 0.6
CACHE_HOME = Path(
    os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache"))
)
ARTWORK_DIR = CACHE_HOME / "waydroid-apple-music" / "artwork"
DEFAULT_ADB = shutil.which("adb") or "/usr/bin/adb"
CATALOG_USER_AGENT = (
    "WaydroidAppleMPRIS/1 "
    "(https://github.com/willbeez/waydroid-apple-music)"
)


class MprisError(dbus.DBusException):
    _dbus_error_name = "org.mpris.MediaPlayer2.Error.Failed"


class MprisNotSupported(dbus.DBusException):
    _dbus_error_name = "org.mpris.MediaPlayer2.Error.NotSupported"


class WaydroidAppleMusic(dbus.service.Object):
    def __init__(self, bus_name):
        super().__init__(bus_name, OBJECT_PATH)
        self.adb = (
            os.environ.get("WAYDROID_APPLE_MPRIS_ADB", "").strip()
            or DEFAULT_ADB
        )
        self.serial = None
        self.session_active = False
        self.playback_status = "Stopped"
        self.position_us = 0
        self.speed = 0.0
        self.position_time = time.monotonic()
        self.title = ""
        self.artist = ""
        self.album = ""
        self.art_url = ""
        self.description = ""
        self.artwork_cache = {}
        self.catalog_failures = {}
        self.last_catalog_request = 0.0
        self.last_notification_poll = 0.0
        self.last_volume_check = 0.0
        self.audio_path_missing_since = None
        self.last_audio_recovery = 0.0
        self.audio_recovery_running = False
        self.pipewire_playback_nodes = set()
        self.pipewire_monitor_ready = False
        self.pipewire_lock = threading.Lock()
        self.poll_running = False
        # Set when a re-read was requested while one was already in flight.
        self.pending_poll = False
        # Bumped on every control press; a poll snapshot older than this is
        # discarded so it cannot flash a stale status at the desktop.
        self.state_seq = 0
        # Status a control press is expected to produce, trusted until Android
        # has had time to confirm it.
        self.preview_status_value = ""
        self.preview_deadline = 0.0
        self.connection_verified = None
        self.connection_retry_after = 0.0
        self.artwork_fetch_key = None
        if AUDIO_WATCHDOG:
            threading.Thread(
                target=self._pipewire_monitor_worker, daemon=True
            ).start()
        GLib.timeout_add(1000, self._periodic_poll)

    @staticmethod
    def _artwork_key(title, artist, album=""):
        # One cache entry per album; per-track only when the album is
        # unknown.
        if album:
            identity = "\0".join(
                ("album", album.strip().casefold(), artist.strip().casefold())
            )
        else:
            identity = "\0".join(
                ("track", title.strip().casefold(), artist.strip().casefold())
            )
        return hashlib.sha1(identity.encode("utf-8")).hexdigest()

    def _cached_artwork(self, title, artist, album):
        if not title:
            return ""
        key = self._artwork_key(title, artist, album)
        cached = self.artwork_cache.get(key)
        if cached:
            return cached
        for suffix in (".jpg", ".png"):
            path = ARTWORK_DIR / f"{key}{suffix}"
            if path.is_file():
                uri = path.resolve().as_uri()
                self.artwork_cache[key] = uri
                return uri
        return ""

    @staticmethod
    def _normalize(value):
        normalized = unicodedata.normalize("NFKC", value or "").casefold()
        return " ".join(normalized.split())

    def _catalog_match(self, results, title, artist, album):
        wanted_title = self._normalize(title)
        wanted_artist = self._normalize(artist)
        wanted_album = self._normalize(album)
        wanted_titles = {wanted_title, base_title(wanted_title)}
        exact = [
            result
            for result in results
            if self._normalize(result.get("title")) == wanted_title
            and self._normalize(result.get("artist")) == wanted_artist
        ]
        if exact:
            return max(
                exact,
                key=lambda result: SequenceMatcher(
                    None,
                    wanted_album,
                    self._normalize(result.get("album")),
                ).ratio(),
            )

        best_result = None
        best_score = 0.0
        for result in results:
            result_title = self._normalize(result.get("title"))
            result_artist = self._normalize(result.get("artist"))
            result_album = self._normalize(result.get("album"))
            title_score = max(
                SequenceMatcher(None, wanted, candidate).ratio()
                for wanted in wanted_titles
                for candidate in {result_title, base_title(result_title)}
            )
            artist_score = SequenceMatcher(None, wanted_artist, result_artist).ratio()
            album_score = SequenceMatcher(None, wanted_album, result_album).ratio()
            score = title_score * 3 + artist_score * 2 + album_score
            if title_score >= 0.9 and artist_score >= 0.65 and score > best_score:
                best_result = result
                best_score = score
        return best_result

    @staticmethod
    def _musicbrainz_phrase(value):
        escaped = (value or "").replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'

    @staticmethod
    def _musicbrainz_candidates(recordings):
        candidates = []
        for recording in recordings:
            credits = recording.get("artist-credit", [])
            artist = "".join(
                (
                    credit.get("name")
                    or credit.get("artist", {}).get("name", "")
                )
                + credit.get("joinphrase", "")
                for credit in credits
                if isinstance(credit, dict)
            ).strip()
            for release in recording.get("releases", []):
                if not isinstance(release, dict) or not release.get("id"):
                    continue
                candidates.append(
                    {
                        "title": recording.get("title", ""),
                        "artist": artist,
                        "album": release.get("title", ""),
                        "release_id": release["id"],
                    }
                )
        return candidates

    @staticmethod
    def _itunes_candidates(results):
        return [
            {
                "title": result.get("trackName", ""),
                "artist": result.get("artistName", ""),
                "album": result.get("collectionName", ""),
                "artist_id": result.get("artistId", ""),
                "artwork_url": result.get("artworkUrl100", ""),
            }
            for result in results
            if isinstance(result, dict)
        ]

    @staticmethod
    def _artwork_variant(url):
        if not url.startswith("https://"):
            return ""
        return re.sub(
            r"/\d+x\d+[a-z]{0,2}(?=\.[A-Za-z0-9]+$)", "/600x600bb", url
        )

    @staticmethod
    def _read_json(request):
        with urlopen(request, timeout=8) as response:
            payload = response.read(2_000_001)
        if len(payload) > 2_000_000:
            raise ValueError("catalog response too large")
        response_data = json.loads(payload.decode("utf-8"))
        if not isinstance(response_data, dict):
            raise ValueError("catalog response is not an object")
        return response_data

    @staticmethod
    def _read_image(url):
        request = Request(url, headers={"User-Agent": CATALOG_USER_AGENT})
        with urlopen(request, timeout=8) as response:
            image = response.read(8_000_001)
        if not image or len(image) > 8_000_000:
            raise ValueError("artwork response invalid")
        return image

    def _itunes_album_art(self, artist_id, album):
        params = urlencode({"id": artist_id, "entity": "album", "limit": 200})
        lookup_request = Request(
            f"https://itunes.apple.com/lookup?{params}",
            headers={
                "Accept": "application/json",
                "User-Agent": CATALOG_USER_AGENT,
            },
        )
        response_data = self._read_json(lookup_request)
        wanted_album = self._normalize(album)
        best_rating = 0.0
        best_url = ""
        for result in response_data.get("results", []):
            if (
                not isinstance(result, dict)
                or result.get("wrapperType") != "collection"
            ):
                continue
            rating = SequenceMatcher(
                None,
                wanted_album,
                self._normalize(result.get("collectionName")),
            ).ratio()
            if rating > best_rating:
                best_rating = rating
                best_url = result.get("artworkUrl100", "")
        if best_rating < 0.75:
            return ""
        return self._artwork_variant(best_url)

    def _itunes_artist_id(self, artist):
        params = urlencode(
            {
                "term": artist,
                "media": "music",
                "entity": "musicArtist",
                "limit": 5,
            }
        )
        search_request = Request(
            f"https://itunes.apple.com/search?{params}",
            headers={
                "Accept": "application/json",
                "User-Agent": CATALOG_USER_AGENT,
            },
        )
        response_data = self._read_json(search_request)
        wanted_artist = self._normalize(artist)
        for result in response_data.get("results", []):
            if not isinstance(result, dict):
                continue
            if self._normalize(result.get("artistName")) == wanted_artist:
                return result.get("artistId", "")
        return ""

    def _itunes_image(self, title, artist, album):
        params = urlencode(
            {
                "term": f"{base_title(title)} {artist}",
                "media": "music",
                "entity": "song",
                "limit": 25,
            }
        )
        search_request = Request(
            f"https://itunes.apple.com/search?{params}",
            headers={
                "Accept": "application/json",
                "User-Agent": CATALOG_USER_AGENT,
            },
        )
        response_data = self._read_json(search_request)
        candidates = self._itunes_candidates(response_data.get("results", []))
        match = self._catalog_match(candidates, title, artist, album)
        artwork_url = ""
        if match:
            album_similarity = SequenceMatcher(
                None,
                self._normalize(album),
                self._normalize(match.get("album", "")),
            ).ratio()
            if album and album_similarity < 0.75 and match.get("artist_id"):
                artwork_url = self._itunes_album_art(
                    match.get("artist_id", ""), album
                )
            if not artwork_url:
                artwork_url = self._artwork_variant(
                    match.get("artwork_url", "")
                )
        if not artwork_url and album:
            artist_id = self._itunes_artist_id(artist)
            if artist_id:
                artwork_url = self._itunes_album_art(artist_id, album)
        if not artwork_url:
            return None
        return self._read_image(artwork_url)

    def _musicbrainz_image(self, title, artist, album):
        terms = [
            f"recording:{self._musicbrainz_phrase(title)}",
            f"artist:{self._musicbrainz_phrase(artist)}",
        ]
        params = urlencode({"query": " AND ".join(terms), "fmt": "json", "limit": 10})
        search_request = Request(
            f"https://musicbrainz.org/ws/2/recording/?{params}",
            headers={
                "Accept": "application/json",
                "User-Agent": CATALOG_USER_AGENT,
            },
        )
        response_data = self._read_json(search_request)
        candidates = self._musicbrainz_candidates(
            response_data.get("recordings", [])
        )
        match = self._catalog_match(candidates, title, artist, album)
        release_id = match.get("release_id", "") if match else ""
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", release_id):
            return None
        return self._read_image(
            f"https://coverartarchive.org/release/{release_id}/front-500"
        )

    def _catalog_artwork(self, title, artist, album):
        if not CATALOG_ARTWORK or not title or not artist:
            return ""
        key = self._artwork_key(title, artist, album)
        now = time.monotonic()
        if now - self.catalog_failures.get(key, 0.0) < 600:
            return ""
        if now - self.last_catalog_request < 3:
            return ""
        self.last_catalog_request = now

        image = None
        for lookup in (self._itunes_image, self._musicbrainz_image):
            try:
                image = lookup(title, artist, album)
            except (OSError, ValueError, json.JSONDecodeError):
                image = None
            if image:
                break
        if not image:
            self.catalog_failures[key] = now
            return ""

        temporary = None
        try:
            if image.startswith(b"\xff\xd8\xff"):
                suffix = ".jpg"
            elif image.startswith(b"\x89PNG\r\n\x1a\n"):
                suffix = ".png"
            else:
                raise ValueError("artwork format is not JPEG or PNG")
            ARTWORK_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
            ARTWORK_DIR.chmod(0o700)
            path = ARTWORK_DIR / f"{key}{suffix}"
            temporary = ARTWORK_DIR / f".{key}.tmp{suffix}"
            descriptor = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
            )
            with os.fdopen(descriptor, "wb") as artwork_file:
                artwork_file.write(image)
            os.replace(temporary, path)
            path.chmod(0o600)
        except (OSError, ValueError, json.JSONDecodeError):
            self.catalog_failures[key] = now
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
            return ""
        uri = path.resolve().as_uri()
        self.artwork_cache[key] = uri
        return uri

    def _start_artwork_fetch(self, title, artist, album):
        key = self._artwork_key(title, artist, album)
        now = time.monotonic()
        if self.artwork_fetch_key == key:
            return
        if now - self.catalog_failures.get(key, 0.0) < 600:
            return
        self.artwork_fetch_key = key
        threading.Thread(
            target=self._artwork_worker,
            args=(key, title, artist, album),
            daemon=True,
        ).start()

    def _artwork_worker(self, key, title, artist, album):
        uri = ""
        try:
            uri = self._cached_artwork(title, artist, album)
            if not uri:
                uri = self._catalog_artwork(title, artist, album)
        except Exception as error:
            print(f"Could not fetch artwork: {error}", flush=True)
        finally:
            if self.artwork_fetch_key == key:
                self.artwork_fetch_key = None
        if uri:
            GLib.idle_add(self._apply_artwork, key, uri)

    def _apply_artwork(self, key, uri):
        if self._artwork_key(self.title, self.artist, self.album) != key:
            return False
        if self.art_url != uri:
            self.art_url = uri
            self.PropertiesChanged(
                PLAYER_IFACE, {"Metadata": self._metadata()}, []
            )
        return False

    def _run(self, args, timeout=5):
        try:
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return result if result.returncode == 0 else None

    def _waydroid_ip(self):
        try:
            with open(LEASES_FILE, "r", encoding="utf-8") as leases:
                rows = [line.split() for line in leases if line.strip()]
        except OSError:
            return None
        for fields in reversed(rows):
            if len(fields) >= 3 and fields[2].startswith("192.168."):
                return fields[2]
        return rows[-1][2] if rows and len(rows[-1]) >= 3 else None

    def _ensure_connection(self, force=False):
        """Return (connected, reused).

        A connection verified within ADB_CONNECTION_TTL is trusted without
        probing adb again, which keeps a control press down to one adb call.
        A failed probe is not retried for ADB_RETRY_BACKOFF seconds unless the
        caller passes force=True, as control presses do.
        """
        ip = self._waydroid_ip()
        if not ip or not os.path.isfile(self.adb):
            self.serial = None
            self.connection_verified = None
            return False, False
        serial = f"{ip}:5555"
        if serial != self.serial:
            self.serial = serial
            self.connection_verified = None
        now = time.monotonic()
        if (
            self.connection_verified is not None
            and now - self.connection_verified < ADB_CONNECTION_TTL
        ):
            return True, True
        if not force and now < self.connection_retry_after:
            return False, False
        state = self._run([self.adb, "-s", serial, "get-state"], timeout=2)
        if state and state.stdout.strip() == "device":
            self.connection_verified = now
            return True, False
        self._run([self.adb, "connect", serial], timeout=5)
        state = self._run([self.adb, "-s", serial, "get-state"], timeout=2)
        connected = bool(state and state.stdout.strip() == "device")
        self.connection_verified = now if connected else None
        self.connection_retry_after = 0.0 if connected else now + ADB_RETRY_BACKOFF
        return connected, False

    def _adb_shell(self, *args, timeout=5, force_connect=False):
        connected, reused = self._ensure_connection(force=force_connect)
        if not connected:
            return None
        result = self._run([self.adb, "-s", self.serial, "shell", *args], timeout)
        if result is None and reused:
            # The cached connection dropped since it was verified; reconnect
            # once and try again.
            self.connection_verified = None
            if self._ensure_connection(force=True)[0]:
                result = self._run(
                    [self.adb, "-s", self.serial, "shell", *args], timeout
                )
        return result.stdout if result else None

    def _handle_pipewire_event(self, block):
        id_match = re.search(r"(?m)^[ \t]*id:[ \t]*(\d+)[ \t]*$", block)
        if not id_match:
            return
        node_id = int(id_match.group(1))
        is_node = "type: PipeWire:Interface:Node" in block
        is_waydroid_playback = (
            is_node
            and 'application.name = "Waydroid"' in block
            and 'media.class = "Stream/Output/Audio"' in block
        )
        with self.pipewire_lock:
            if is_node:
                self.pipewire_monitor_ready = True
            if block.lstrip().startswith("added:") and is_waydroid_playback:
                self.pipewire_playback_nodes.add(node_id)
                self.audio_path_missing_since = None
            elif block.lstrip().startswith("removed:"):
                self.pipewire_playback_nodes.discard(node_id)
                if (
                    not self.pipewire_playback_nodes
                    and self.playback_status == "Playing"
                ):
                    self.audio_path_missing_since = time.monotonic()

    def _pipewire_monitor_worker(self):
        while True:
            with self.pipewire_lock:
                self.pipewire_monitor_ready = False
                self.pipewire_playback_nodes.clear()
            try:
                process = subprocess.Popen(
                    ["pw-mon", "-N", "-a", "-p"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    errors="replace",
                    bufsize=1,
                )
                block = []
                for line in process.stdout:
                    if line.strip():
                        block.append(line)
                    elif block:
                        self._handle_pipewire_event("".join(block))
                        block = []
            except OSError:
                pass
            time.sleep(1)

    def _recover_audio_path(self):
        print(
            "Apple Music reports playback but Waydroid has no Linux output; "
            "restarting the Waydroid session",
            flush=True,
        )
        try:
            self._run(["waydroid", "session", "stop"], timeout=25)
            time.sleep(3)
            subprocess.Popen(
                ["waydroid", "session", "start"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )

            ready = False
            for _ in range(35):
                time.sleep(2)
                booted = self._adb_shell("getprop", "sys.boot_completed", timeout=3)
                if booted and booted.strip() == "1":
                    ready = True
                    break
            if not ready:
                print("Waydroid audio recovery timed out", flush=True)
                return

            launched = False
            for _ in range(5):
                result = self._run(
                    ["waydroid", "app", "launch", PACKAGE], timeout=12
                )
                if result is not None:
                    launched = True
                    break
                time.sleep(2)
            if not launched:
                print("Waydroid restarted but Apple Music did not relaunch", flush=True)
                return
            time.sleep(5)
            self._adb_shell(
                "cmd", "media_session", "dispatch", "play", timeout=4
            )
            print("Waydroid audio recovery completed", flush=True)
        finally:
            self.audio_recovery_running = False

    def _check_audio_watchdog(self, active, status):
        if not AUDIO_WATCHDOG:
            return
        now = time.monotonic()
        if self.audio_recovery_running:
            return
        if not active or status != "Playing":
            self.audio_path_missing_since = None
            return
        with self.pipewire_lock:
            monitor_ready = self.pipewire_monitor_ready
            present = bool(self.pipewire_playback_nodes)
        if not monitor_ready:
            return
        if present:
            self.audio_path_missing_since = None
            return
        if self.audio_path_missing_since is None:
            self.audio_path_missing_since = now
            return
        if now - self.audio_path_missing_since < WATCHDOG_GRACE_SECONDS:
            return
        if now - self.last_audio_recovery < WATCHDOG_COOLDOWN_SECONDS:
            return
        self.last_audio_recovery = now
        self.audio_path_missing_since = None
        self.audio_recovery_running = True
        threading.Thread(target=self._recover_audio_path, daemon=True).start()

    def _enforce_media_volume(self):
        if not FORCE_VOLUME:
            return
        now = time.monotonic()
        if now - self.last_volume_check < 20:
            return
        safe_state = self._adb_shell(
            "settings", "get", "global", "audio_safe_volume_state", timeout=3
        )
        volume = self._adb_shell(
            "settings", "get", "system", "volume_music_speaker", timeout=3
        )
        if safe_state is None or volume is None:
            return
        self.last_volume_check = now
        if safe_state.strip() != "2":
            self._adb_shell(
                "settings", "put", "global", "audio_safe_volume_state", "2", timeout=3
            )
            time.sleep(0.5)
        try:
            current_volume = int(volume.strip())
        except ValueError:
            current_volume = 0
        if current_volume < 15:
            for _ in range(15 - current_volume):
                self._adb_shell("input", "keyevent", "24", timeout=3)
                time.sleep(0.15)

    @staticmethod
    def _apple_session_block(dump):
        blocks = re.split(r"(?m)(?=^\s*Session #\d+:)", dump)
        for block in blocks:
            if f"package={PACKAGE}" in block:
                return block
        package_at = dump.find(f"package={PACKAGE}")
        if package_at < 0:
            return ""
        return dump[max(0, package_at - 1500) : package_at + 5000]

    @staticmethod
    def _parse_notification(dump):
        candidates = dump.split("NotificationRecord(")
        for block in candidates:
            if PACKAGE not in block or "category=transport" not in block:
                continue
            title_match = re.search(
                r"(?m)^\s*android\.title=String \((.*)\)\s*$", block
            )
            artist_match = re.search(
                r"(?m)^\s*android\.text=String \((.*)\)\s*$", block
            )
            if title_match or artist_match:
                return (
                    title_match.group(1) if title_match else "",
                    artist_match.group(1) if artist_match else "",
                )
        return "", ""

    @staticmethod
    def _metadata_from_description(description, title, artist):
        album = ""
        if title and artist:
            prefix = f"{title}, {artist}, "
            if description.startswith(prefix):
                album = description[len(prefix) :]
                return title, artist, album
        if description:
            parts = description.rsplit(", ", 2)
            if len(parts) == 3:
                fallback_title, fallback_artist, fallback_album = parts
                return fallback_title, fallback_artist, fallback_album
        return title or description, artist, album

    def _read_state(self):
        self._enforce_media_volume()
        media = self._adb_shell("dumpsys", "media_session", timeout=5)
        if not media:
            self._check_audio_watchdog(False, "Stopped")
            return {
                "active": False,
                "status": "Stopped",
                "position_us": 0,
                "speed": 0.0,
                "description": "",
                "title": "",
                "artist": "",
                "album": "",
                "art_url": "",
            }

        block = self._apple_session_block(media)
        active = bool(re.search(r"(?m)^\s*active=true\s*$", block))
        state_match = re.search(
            r"state=PlaybackState \{state=(\d+), position=(-?\d+),.*?speed=([-\d.]+)",
            block,
        )
        state_code = int(state_match.group(1)) if state_match else 0
        position_us = max(0, int(state_match.group(2))) * 1000 if state_match else 0
        speed = float(state_match.group(3)) if state_match else 0.0
        if state_code == 2:
            status = "Paused"
        elif state_code in (3, 4, 5, 6, 8, 10, 11):
            status = "Playing"
        else:
            status = "Stopped"

        description_match = re.search(
            r"(?m)^\s*metadata:.*?description=(.*)\s*$", block
        )
        description = description_match.group(1).strip() if description_match else ""

        title, artist = self.title, self.artist
        now = time.monotonic()
        if description != self.description or now - self.last_notification_poll >= 5:
            notification = self._adb_shell(
                "dumpsys", "notification", "--noredact", timeout=5
            )
            if notification:
                new_title, new_artist = self._parse_notification(notification)
                title = new_title or title
                artist = new_artist or artist
            self.last_notification_poll = now
        title, artist, album = self._metadata_from_description(
            description, title, artist
        )
        art_url = ""
        if CATALOG_ARTWORK:
            art_url = self._cached_artwork(title, artist, album)
        self._check_audio_watchdog(active, status)
        return {
            "active": active,
            "status": status,
            "position_us": position_us,
            "speed": speed,
            "description": description,
            "title": title,
            "artist": artist,
            "album": album,
            "art_url": art_url,
        }

    def _apply_state(self, state):
        old_player = self._player_properties()
        self.session_active = state["active"]
        self.playback_status = state["status"]
        self.position_us = state["position_us"]
        self.speed = state["speed"]
        self.position_time = time.monotonic()
        self.description = state["description"]
        self.title = state["title"]
        self.artist = state["artist"]
        self.album = state["album"]
        art_url = state["art_url"]
        if not art_url and CATALOG_ARTWORK:
            art_url = self._cached_artwork(
                state["title"], state["artist"], state["album"]
            )
        self.art_url = art_url
        self._publish_player_changes(old_player)
        return False

    PLAYER_CHANGE_KEYS = (
        "PlaybackStatus",
        "Metadata",
        "CanGoNext",
        "CanGoPrevious",
        "CanPlay",
        "CanPause",
        "CanControl",
    )

    def _publish_player_changes(self, old_player):
        new_player = self._player_properties()
        changed = {}
        for key in self.PLAYER_CHANGE_KEYS:
            if old_player[key] != new_player[key]:
                changed[key] = new_player[key]
        if changed:
            self.PropertiesChanged(PLAYER_IFACE, changed, [])
        return changed

    PREVIEW_STATUS = {"play": "Playing", "pause": "Paused", "stop": "Stopped"}

    # Apple Music handles an explicit play or pause command in well under
    # 100 ms, but its play-pause toggle takes over 300 ms. Since the current
    # status is always known here, the toggle is translated to the explicit
    # command it actually means.
    STATUS_ADB_COMMAND = {"Playing": "play", "Paused": "pause", "Stopped": "stop"}

    def _target_status(self, command):
        """The playback status a control command is meant to produce."""
        if command == "play-pause":
            return "Paused" if self.playback_status == "Playing" else "Playing"
        return self.PREVIEW_STATUS.get(command)

    def _preview_status(self, target):
        """Publish the status a control press is expected to produce.

        The desktop reacts to the click immediately instead of waiting for the
        next poll; the next read then confirms or corrects it.
        """
        if not self.session_active or target is None:
            return
        if target == self.playback_status:
            return
        old_player = self._player_properties()
        # Freeze the position when pausing, keep it running when playing.
        self.position_us = self._position()
        self.position_time = time.monotonic()
        self.playback_status = target
        if target == "Stopped":
            self.position_us = 0
        self.preview_status_value = target
        self.preview_deadline = time.monotonic() + PREVIEW_GRACE_SECONDS
        self._publish_player_changes(old_player)

    def _apply_state_safely(self, state, state_seq=None):
        try:
            if state_seq is not None and state_seq != self.state_seq:
                # A control press landed while this snapshot was being read, so
                # it may predate the press. Drop it and read again rather than
                # flash a stale status at the desktop.
                self.pending_poll = True
                return False
            if (
                self.preview_status_value
                and time.monotonic() < self.preview_deadline
                and state["status"] != self.preview_status_value
            ):
                # The read still sees the pre-press state, so Android has not
                # caught up with the dispatch yet. Read again instead of
                # reverting what the press already published.
                self.pending_poll = True
                return False
            self.preview_status_value = ""
            result = self._apply_state(state)
            if CATALOG_ARTWORK and not self.art_url and self.title and self.artist:
                self._start_artwork_fetch(self.title, self.artist, self.album)
            return result
        except Exception as error:
            print(f"Could not publish Apple Music state: {error}", flush=True)
            return False
        finally:
            self._finish_poll()

    def _poll_failed(self, error, state_seq=None):
        print(f"Could not read Apple Music state: {error}", flush=True)
        self._finish_poll()
        return False

    def _finish_poll(self):
        self.poll_running = False
        if self.pending_poll:
            self.poll()

    def _poll_worker(self, state_seq=None):
        try:
            state = self._read_state()
        except Exception as error:
            GLib.idle_add(self._poll_failed, str(error), state_seq)
            return
        GLib.idle_add(self._apply_state_safely, state, state_seq)

    def _periodic_poll(self):
        self.poll()
        return True

    def poll(self, queued=False):
        if self.poll_running:
            # A read is already in flight. Remember that another one is wanted
            # instead of dropping the request, so a requested re-read always
            # happens right after the current one finishes.
            if queued:
                self.pending_poll = True
            return
        self.pending_poll = False
        self.poll_running = True
        state_seq = self.state_seq
        threading.Thread(
            target=self._poll_worker, args=(state_seq,), daemon=True
        ).start()

    def _dispatch(self, command):
        # A control press should feel immediate: bump the state sequence so
        # in-flight snapshots are discarded, then publish the expected status
        # before the adb command even runs. The target status is resolved
        # first, because publishing it changes playback_status.
        self.state_seq += 1
        target = self._target_status(command) if self.session_active else None
        self._preview_status(target)
        # Prefer the explicit command over Apple Music's slow toggle, but only
        # when the current status is actually known.
        adb_command = command
        if command == "play-pause" and target is not None:
            adb_command = self.STATUS_ADB_COMMAND.get(target, command)

        def worker():
            self._adb_shell(
                "cmd",
                "media_session",
                "dispatch",
                adb_command,
                timeout=3,
                force_connect=True,
            )
            GLib.timeout_add(150, self._poll_soon)
            GLib.timeout_add(700, self._poll_soon)

        threading.Thread(target=worker, daemon=True).start()

    def _poll_soon(self):
        self.poll(queued=True)
        return False

    def _launch(self):
        def worker():
            self._run(["waydroid", "app", "launch", PACKAGE], timeout=15)

        threading.Thread(target=worker, daemon=True).start()

    def _metadata(self):
        if not (self.title or self.artist or self.album):
            return dbus.Dictionary({}, signature="sv")
        identity = "\0".join((self.title, self.artist, self.album))
        digest = hashlib.sha1(identity.encode("utf-8")).hexdigest()
        metadata = {
            "mpris:trackid": dbus.ObjectPath(f"/org/mpris/MediaPlayer2/Track/{digest}"),
            "xesam:title": dbus.String(self.title or "Apple Music"),
        }
        if self.artist:
            metadata["xesam:artist"] = dbus.Array(
                [dbus.String(self.artist)], signature="s"
            )
        if self.album:
            metadata["xesam:album"] = dbus.String(self.album)
        if self.art_url:
            metadata["mpris:artUrl"] = dbus.String(self.art_url)
        return dbus.Dictionary(metadata, signature="sv")

    def _position(self):
        position = self.position_us
        if self.playback_status == "Playing" and self.speed > 0:
            position += int((time.monotonic() - self.position_time) * self.speed * 1_000_000)
        return dbus.Int64(max(0, position))

    def _root_properties(self):
        return {
            "CanQuit": dbus.Boolean(False),
            "CanRaise": dbus.Boolean(True),
            "HasTrackList": dbus.Boolean(False),
            "Identity": dbus.String("Apple Music (Waydroid)"),
            "DesktopEntry": dbus.String("waydroid.com.apple.android.music"),
            "SupportedUriSchemes": dbus.Array([], signature="s"),
            "SupportedMimeTypes": dbus.Array([], signature="s"),
        }

    def _player_properties(self):
        controllable = self.session_active
        return {
            "PlaybackStatus": dbus.String(self.playback_status),
            "LoopStatus": dbus.String("None"),
            "Rate": dbus.Double(1.0),
            "Shuffle": dbus.Boolean(False),
            "Metadata": self._metadata(),
            "Volume": dbus.Double(1.0),
            "Position": self._position(),
            "MinimumRate": dbus.Double(1.0),
            "MaximumRate": dbus.Double(1.0),
            "CanGoNext": dbus.Boolean(controllable),
            "CanGoPrevious": dbus.Boolean(controllable),
            "CanPlay": dbus.Boolean(controllable),
            "CanPause": dbus.Boolean(controllable),
            "CanSeek": dbus.Boolean(False),
            "CanControl": dbus.Boolean(controllable),
        }

    @dbus.service.method(PROPERTIES_IFACE, in_signature="ss", out_signature="v")
    def Get(self, interface_name, property_name):
        properties = self.GetAll(interface_name)
        if property_name not in properties:
            raise MprisError(f"Unknown property {property_name}")
        return properties[property_name]

    @dbus.service.method(PROPERTIES_IFACE, in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface_name):
        if interface_name == ROOT_IFACE:
            return dbus.Dictionary(self._root_properties(), signature="sv")
        if interface_name == PLAYER_IFACE:
            return dbus.Dictionary(self._player_properties(), signature="sv")
        raise MprisError(f"Unknown interface {interface_name}")

    @dbus.service.method(PROPERTIES_IFACE, in_signature="ssv")
    def Set(self, interface_name, property_name, value):
        raise MprisNotSupported(f"{interface_name}.{property_name} is read-only")

    @dbus.service.signal(PROPERTIES_IFACE, signature="sa{sv}as")
    def PropertiesChanged(self, interface_name, changed_properties, invalidated):
        pass

    @dbus.service.method(ROOT_IFACE)
    def Raise(self):
        self._launch()

    @dbus.service.method(ROOT_IFACE)
    def Quit(self):
        pass

    @dbus.service.method(PLAYER_IFACE)
    def Next(self):
        self._dispatch("next")

    @dbus.service.method(PLAYER_IFACE)
    def Previous(self):
        self._dispatch("previous")

    @dbus.service.method(PLAYER_IFACE)
    def Pause(self):
        self._dispatch("pause")

    @dbus.service.method(PLAYER_IFACE)
    def PlayPause(self):
        self._dispatch("play-pause")

    @dbus.service.method(PLAYER_IFACE)
    def Stop(self):
        self._dispatch("stop")

    @dbus.service.method(PLAYER_IFACE)
    def Play(self):
        self._dispatch("play")

    @dbus.service.method(PLAYER_IFACE, in_signature="x")
    def Seek(self, offset):
        raise MprisNotSupported("Apple Music seeking is not exposed by this bridge")

    @dbus.service.method(PLAYER_IFACE, in_signature="ox")
    def SetPosition(self, track_id, position):
        raise MprisNotSupported("Apple Music seeking is not exposed by this bridge")

    @dbus.service.method(PLAYER_IFACE, in_signature="s")
    def OpenUri(self, uri):
        raise MprisNotSupported("Opening URIs is not exposed by this bridge")

    @dbus.service.signal(PLAYER_IFACE, signature="x")
    def Seeked(self, position):
        pass


def main():
    DBusGMainLoop(set_as_default=True)
    bus = dbus.SessionBus()
    bus_name = dbus.service.BusName(
        BUS_NAME, bus=bus, allow_replacement=True, replace_existing=True
    )
    WaydroidAppleMusic(bus_name)
    GLib.MainLoop().run()


if __name__ == "__main__":
    main()
