import importlib.util
import os
from pathlib import Path
from unittest import mock
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "waydroid_apple_mpris", ROOT / "src" / "waydroid_apple_mpris.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class BridgeTests(unittest.TestCase):
    def test_env_flag(self):
        with mock.patch.dict(os.environ, {"TEST_FLAG": "yes"}):
            self.assertTrue(MODULE.env_flag("TEST_FLAG"))
        with mock.patch.dict(os.environ, {"TEST_FLAG": "off"}):
            self.assertFalse(MODULE.env_flag("TEST_FLAG", True))

    def test_env_float_uses_safe_minimum(self):
        with mock.patch.dict(os.environ, {"TEST_FLOAT": "not-a-number"}):
            self.assertEqual(MODULE.env_float("TEST_FLOAT", 1.0, 0.05), 1.0)
        with mock.patch.dict(os.environ, {"TEST_FLOAT": "0"}):
            self.assertEqual(MODULE.env_float("TEST_FLOAT", 1.0, 0.05), 0.05)

    def test_notification_parser(self):
        dump = """
NotificationRecord(pkg=com.apple.android.music category=transport)
  android.title=String (Bene Gesserit)
  android.text=String (Hans Zimmer)
"""
        title, artist = MODULE.WaydroidAppleMusic._parse_notification(dump)
        self.assertEqual(title, "Bene Gesserit")
        self.assertEqual(artist, "Hans Zimmer")

    def test_description_supplies_album(self):
        values = MODULE.WaydroidAppleMusic._metadata_from_description(
            "Bene Gesserit, Hans Zimmer, Dune: Part Two", "Bene Gesserit", "Hans Zimmer"
        )
        self.assertEqual(values, ("Bene Gesserit", "Hans Zimmer", "Dune: Part Two"))

    def test_description_replaces_stale_notification_values(self):
        values = MODULE.WaydroidAppleMusic._metadata_from_description(
            "New Song, New Artist, New Album", "Old Song", "Old Artist"
        )
        self.assertEqual(values, ("New Song", "New Artist", "New Album"))

    def test_artwork_key_includes_album(self):
        first = MODULE.WaydroidAppleMusic._artwork_key("Song", "Artist", "Album")
        second = MODULE.WaydroidAppleMusic._artwork_key(
            "Song", "Artist", "Deluxe Album"
        )
        self.assertNotEqual(first, second)

    def test_catalog_prefers_exact_album(self):
        bridge = object.__new__(MODULE.WaydroidAppleMusic)
        results = [
            {"title": "Song", "artist": "Artist", "album": "Other"},
            {"title": "Song", "artist": "Artist", "album": "Album"},
        ]
        match = bridge._catalog_match(results, "Song", "Artist", "Album")
        self.assertEqual(match["album"], "Album")

    def test_catalog_prefers_closest_album_name(self):
        bridge = object.__new__(MODULE.WaydroidAppleMusic)
        results = [
            {"title": "Song", "artist": "Artist", "album": "Greatest Hits"},
            {
                "title": "Song",
                "artist": "Artist",
                "album": "Album (Deluxe Edition)",
            },
        ]
        match = bridge._catalog_match(results, "Song", "Artist", "Album")
        self.assertEqual(match["album"], "Album (Deluxe Edition)")

    def test_musicbrainz_candidates(self):
        recordings = [
            {
                "title": "Song",
                "artist-credit": [{"name": "Artist"}],
                "releases": [{"id": "123", "title": "Album"}],
            }
        ]
        self.assertEqual(
            MODULE.WaydroidAppleMusic._musicbrainz_candidates(recordings),
            [
                {
                    "title": "Song",
                    "artist": "Artist",
                    "album": "Album",
                    "release_id": "123",
                }
            ],
        )

    def test_poll_error_can_retry(self):
        bridge = object.__new__(MODULE.WaydroidAppleMusic)
        bridge.poll_running = True
        bridge._read_state = mock.Mock(side_effect=RuntimeError("test failure"))
        callbacks = []

        def remember(callback, *args):
            callbacks.append((callback, args))

        with mock.patch.object(MODULE.GLib, "idle_add", side_effect=remember):
            bridge._poll_worker()
        self.assertTrue(bridge.poll_running)
        callback, args = callbacks.pop()
        callback(*args)
        self.assertFalse(bridge.poll_running)


if __name__ == "__main__":
    unittest.main()
