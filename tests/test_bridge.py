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


class run_inline:
    """Stand-in for threading.Thread that runs the worker immediately."""

    def __init__(self, target=None, args=(), daemon=None):
        self.target = target
        self.args = args

    def start(self):
        if self.target:
            self.target(*self.args)


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
        bridge.pending_poll = False
        bridge.state_seq = 0
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

    def test_control_press_publishes_status_immediately(self):
        bridge = object.__new__(MODULE.WaydroidAppleMusic)
        bridge.session_active = True
        bridge.playback_status = "Playing"
        bridge.position_us = 2_000_000
        bridge.position_time = MODULE.time.monotonic() - 2.0
        bridge.speed = 1.0
        bridge.title = "Song"
        bridge.artist = "Artist"
        bridge.album = "Album"
        bridge.art_url = ""
        bridge.PropertiesChanged = mock.Mock()

        bridge._preview_status("Paused")

        self.assertEqual(bridge.playback_status, "Paused")
        # The position is frozen where the pause happened, not left running.
        self.assertGreaterEqual(bridge.position_us, 4_000_000)
        self.assertLess(bridge.position_us, 4_500_000)
        changed = bridge.PropertiesChanged.call_args[0][1]
        self.assertEqual(changed["PlaybackStatus"], "Paused")
        # The previewed status is trusted until Android confirms it.
        self.assertEqual(bridge.preview_status_value, "Paused")
        self.assertGreater(bridge.preview_deadline, MODULE.time.monotonic())

    def test_control_press_on_inactive_session_changes_nothing(self):
        bridge = object.__new__(MODULE.WaydroidAppleMusic)
        bridge.session_active = False
        bridge.playback_status = "Stopped"
        bridge.PropertiesChanged = mock.Mock()

        bridge._preview_status("Playing")

        self.assertEqual(bridge.playback_status, "Stopped")
        bridge.PropertiesChanged.assert_not_called()

    def test_stale_read_is_discarded_and_requeued(self):
        bridge = object.__new__(MODULE.WaydroidAppleMusic)
        bridge.poll_running = True
        bridge.pending_poll = False
        bridge.state_seq = 7
        bridge._apply_state = mock.Mock()
        bridge.poll = mock.Mock()

        # Snapshot taken before the control press (state_seq 6) arrives now.
        bridge._apply_state_safely({"status": "Playing"}, state_seq=6)

        bridge._apply_state.assert_not_called()
        self.assertFalse(bridge.poll_running)
        bridge.poll.assert_called_once_with()

    def test_fresh_read_is_applied(self):
        bridge = object.__new__(MODULE.WaydroidAppleMusic)
        bridge.poll_running = True
        bridge.pending_poll = False
        bridge.state_seq = 7
        bridge.preview_status_value = ""
        bridge.preview_deadline = 0.0
        bridge.art_url = ""
        bridge.title = "Song"
        bridge.artist = "Artist"
        bridge.album = "Album"
        bridge._apply_state = mock.Mock(return_value=None)
        bridge.poll = mock.Mock()

        bridge._apply_state_safely({"status": "Paused"}, state_seq=7)

        bridge._apply_state.assert_called_once_with({"status": "Paused"})
        self.assertFalse(bridge.poll_running)
        bridge.poll.assert_not_called()

    def test_read_that_contradicts_preview_is_requeued(self):
        bridge = object.__new__(MODULE.WaydroidAppleMusic)
        bridge.poll_running = True
        bridge.pending_poll = False
        bridge.state_seq = 7
        # A press just published "Playing"; this read still sees "Paused"
        # because Android had not reacted when the snapshot was taken.
        bridge.preview_status_value = "Playing"
        bridge.preview_deadline = MODULE.time.monotonic() + 0.5
        bridge._apply_state = mock.Mock()
        bridge.poll = mock.Mock()

        bridge._apply_state_safely({"status": "Paused"}, state_seq=7)

        bridge._apply_state.assert_not_called()
        self.assertTrue(bridge.pending_poll)
        self.assertFalse(bridge.poll_running)

    def test_preview_expires_so_truth_always_wins(self):
        bridge = object.__new__(MODULE.WaydroidAppleMusic)
        bridge.poll_running = True
        bridge.pending_poll = False
        bridge.state_seq = 7
        # The press's dispatch never took effect; the grace window is over.
        bridge.preview_status_value = "Playing"
        bridge.preview_deadline = MODULE.time.monotonic() - 0.5
        bridge.art_url = ""
        bridge.title = "Song"
        bridge.artist = "Artist"
        bridge.album = "Album"
        bridge._apply_state = mock.Mock(return_value=None)
        bridge.poll = mock.Mock()

        bridge._apply_state_safely({"status": "Paused"}, state_seq=7)

        bridge._apply_state.assert_called_once_with({"status": "Paused"})
        self.assertEqual(bridge.preview_status_value, "")

    def test_connection_reuse_skips_adb_probe(self):
        bridge = object.__new__(MODULE.WaydroidAppleMusic)
        bridge.adb = "/usr/bin/adb"
        bridge.serial = None
        bridge.connection_verified = None
        bridge.connection_retry_after = 0.0
        bridge.connection_backoff = MODULE.ADB_RETRY_BACKOFF
        bridge._waydroid_ip = mock.Mock(return_value="192.168.240.112")
        bridge._waydroid_interface_up = mock.Mock(return_value=True)
        bridge._run = mock.Mock(return_value=mock.Mock(stdout="device\n"))

        with mock.patch.object(MODULE.os.path, "isfile", return_value=True):
            self.assertEqual(bridge._ensure_connection(), (True, False))
            self.assertEqual(bridge._run.call_count, 1)  # one get-state probe
            # Within the TTL the verified connection is trusted: no new probe.
            self.assertEqual(bridge._ensure_connection(), (True, True))
            self.assertEqual(bridge._run.call_count, 1)

    def test_missing_interface_skips_adb_probe(self):
        # A gone bridge means the container has no network, so probing adb
        # would only fork processes that cannot succeed.
        bridge = object.__new__(MODULE.WaydroidAppleMusic)
        bridge.adb = "/usr/bin/adb"
        bridge.serial = None
        bridge.connection_verified = None
        bridge.connection_retry_after = 0.0
        bridge.connection_backoff = MODULE.ADB_RETRY_BACKOFF
        bridge._waydroid_ip = mock.Mock(return_value="192.168.240.112")
        bridge._waydroid_interface_up = mock.Mock(return_value=False)
        bridge._run = mock.Mock()

        with mock.patch.object(MODULE.os.path, "isfile", return_value=True):
            self.assertEqual(bridge._ensure_connection(), (False, False))
            self.assertEqual(bridge._ensure_connection(force=True), (False, False))
            bridge._run.assert_not_called()

    def test_failed_connection_probe_backs_off(self):
        bridge = object.__new__(MODULE.WaydroidAppleMusic)
        bridge.adb = "/usr/bin/adb"
        bridge.serial = None
        bridge.connection_verified = None
        bridge.connection_retry_after = 0.0
        bridge.connection_backoff = MODULE.ADB_RETRY_BACKOFF
        bridge._waydroid_ip = mock.Mock(return_value="192.168.240.112")
        bridge._waydroid_interface_up = mock.Mock(return_value=True)
        bridge._run = mock.Mock(return_value=None)

        with mock.patch.object(MODULE.os.path, "isfile", return_value=True):
            self.assertEqual(bridge._ensure_connection(), (False, False))
            self.assertEqual(bridge._run.call_count, 3)  # get-state, connect, get-state
            bridge._run.reset_mock()
            # Inside the backoff window the poll path does not probe again...
            self.assertEqual(bridge._ensure_connection(), (False, False))
            bridge._run.assert_not_called()
            # ...but a control press forces a fresh attempt.
            self.assertEqual(bridge._ensure_connection(force=True), (False, False))
            self.assertEqual(bridge._run.call_count, 3)

    def test_connection_backoff_escalates_and_recovers(self):
        # Each consecutive failure doubles the wait, capped, and a single
        # success returns it to the base delay so recovery stays prompt.
        bridge = object.__new__(MODULE.WaydroidAppleMusic)
        bridge.adb = "/usr/bin/adb"
        bridge.serial = None
        bridge.connection_verified = None
        bridge.connection_retry_after = 0.0
        bridge.connection_backoff = MODULE.ADB_RETRY_BACKOFF
        bridge._waydroid_ip = mock.Mock(return_value="192.168.240.112")
        bridge._waydroid_interface_up = mock.Mock(return_value=True)
        bridge._run = mock.Mock(return_value=None)

        delays = []
        with mock.patch.object(MODULE.os.path, "isfile", return_value=True):
            for _ in range(4):
                before = MODULE.time.monotonic()
                bridge._ensure_connection(force=True)
                delays.append(round(bridge.connection_retry_after - before))
                # Expire the window so the next forced call probes again.
                bridge.connection_retry_after = 0.0

        self.assertEqual(
            delays,
            [
                MODULE.ADB_RETRY_BACKOFF,
                MODULE.ADB_RETRY_BACKOFF * 2,
                MODULE.ADB_RETRY_BACKOFF * 4,
                MODULE.ADB_RETRY_BACKOFF * 8,
            ],
        )
        self.assertLessEqual(bridge.connection_backoff, MODULE.ADB_RETRY_BACKOFF_MAX)

        # A working connection resets the backoff to the base delay.
        bridge._run = mock.Mock(return_value=mock.Mock(stdout="device\n"))
        bridge.connection_retry_after = 0.0
        bridge.connection_verified = None
        with mock.patch.object(MODULE.os.path, "isfile", return_value=True):
            self.assertEqual(bridge._ensure_connection(force=True), (True, False))
        self.assertEqual(bridge.connection_backoff, MODULE.ADB_RETRY_BACKOFF)

    def test_backoff_is_capped(self):
        bridge = object.__new__(MODULE.WaydroidAppleMusic)
        bridge.adb = "/usr/bin/adb"
        bridge.serial = None
        bridge.connection_verified = None
        bridge.connection_retry_after = 0.0
        bridge.connection_backoff = MODULE.ADB_RETRY_BACKOFF
        bridge._waydroid_ip = mock.Mock(return_value="192.168.240.112")
        bridge._waydroid_interface_up = mock.Mock(return_value=True)
        bridge._run = mock.Mock(return_value=None)

        with mock.patch.object(MODULE.os.path, "isfile", return_value=True):
            for _ in range(10):
                bridge._ensure_connection(force=True)
                bridge.connection_retry_after = 0.0

        self.assertEqual(bridge.connection_backoff, MODULE.ADB_RETRY_BACKOFF_MAX)
        self.assertLessEqual(
            bridge.connection_retry_after - MODULE.time.monotonic(),
            MODULE.ADB_RETRY_BACKOFF_MAX,
        )

    def test_volume_check_records_failure_too(self):
        # A dead adb must not make every poll retry both settings reads.
        bridge = object.__new__(MODULE.WaydroidAppleMusic)
        bridge.last_volume_check = 0.0
        bridge._adb_shell = mock.Mock(return_value=None)
        start = MODULE.time.monotonic()

        with mock.patch.object(MODULE, "FORCE_VOLUME", True):
            bridge._enforce_media_volume()
            self.assertEqual(bridge._adb_shell.call_count, 2)
            self.assertGreater(bridge.last_volume_check, start)
            # Inside the interval the reads are skipped entirely.
            bridge._enforce_media_volume()
            self.assertEqual(bridge._adb_shell.call_count, 2)

    def test_dispatch_ignores_stale_snapshot(self):
        bridge = object.__new__(MODULE.WaydroidAppleMusic)
        bridge.state_seq = 0
        bridge.session_active = True
        bridge.playback_status = "Paused"
        bridge.position_us = 1_000_000
        bridge.position_time = MODULE.time.monotonic()
        bridge.speed = 1.0
        bridge.title = "Song"
        bridge.artist = "Artist"
        bridge.album = ""
        bridge.art_url = ""
        bridge.PropertiesChanged = mock.Mock()
        bridge._adb_shell = mock.Mock()

        with mock.patch.object(MODULE.threading, "Thread"):
            bridge._dispatch("play-pause")

        self.assertEqual(bridge.state_seq, 1)
        self.assertEqual(bridge.playback_status, "Playing")

    def test_play_pause_toggle_is_sent_as_explicit_command(self):
        # Apple Music answers its own play-pause toggle far slower than an
        # explicit play or pause, so the bridge must not send the toggle.
        for current, expected in (("Playing", "pause"), ("Paused", "play"),
                                  ("Stopped", "play")):
            bridge = object.__new__(MODULE.WaydroidAppleMusic)
            bridge.state_seq = 0
            bridge.session_active = True
            bridge.playback_status = current
            bridge.position_us = 0
            bridge.position_time = MODULE.time.monotonic()
            bridge.speed = 1.0
            bridge.title = ""
            bridge.artist = ""
            bridge.album = ""
            bridge.art_url = ""
            bridge.PropertiesChanged = mock.Mock()
            bridge._adb_shell = mock.Mock()

            with mock.patch.object(MODULE.threading, "Thread", side_effect=run_inline):
                bridge._dispatch("play-pause")

            sent = bridge._adb_shell.call_args[0][3]
            self.assertEqual(sent, expected,
                             f"status {current} should send {expected}")

    def test_explicit_commands_pass_through_unchanged(self):
        bridge = object.__new__(MODULE.WaydroidAppleMusic)
        bridge.state_seq = 0
        bridge.session_active = True
        bridge.playback_status = "Playing"
        bridge.position_us = 0
        bridge.position_time = MODULE.time.monotonic()
        bridge.speed = 1.0
        bridge.title = ""
        bridge.artist = ""
        bridge.album = ""
        bridge.art_url = ""
        bridge.PropertiesChanged = mock.Mock()
        bridge._adb_shell = mock.Mock()

        with mock.patch.object(MODULE.threading, "Thread", side_effect=run_inline):
            bridge._dispatch("pause")
            bridge._dispatch("next")

        sent = [call[0][3] for call in bridge._adb_shell.call_args_list]
        self.assertEqual(sent, ["pause", "next"])


if __name__ == "__main__":
    unittest.main()
