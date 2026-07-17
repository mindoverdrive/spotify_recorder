import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from spotify_recorder import HiResRecorderApp
from spotify_recorder_services import MODE_ALBUM


class Value:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value


def qobuz_snapshot(state):
    return {
        "status": "OK",
        "state": state,
        "provider": "qobuz",
        "name": "Fixture Track",
        "artist": "Fixture Artist",
        "album": "Fixture Album",
        "position": 3.0,
        "duration": 120.0,
    }


def make_app(snapshot, *, standby=False, recording=False):
    adapter = SimpleNamespace(
        name="Qobuz",
        snapshot=Mock(return_value=snapshot),
        poll_events=Mock(return_value=[]),
    )
    return SimpleNamespace(
        current_adapter=Mock(return_value=adapter),
        current_source_info=None,
        is_recording=recording,
        is_standby=standby,
        capture_quality_audit=None,
        track_label=Mock(),
        artist_label=Mock(),
        log_message=Mock(),
        start_rec=Mock(),
        stop_rec=Mock(),
        record_mode=Value(MODE_ALBUM),
        last_track_key=None,
        auto_stop_on_idle=Value(True),
        source_idle_since=None,
        auto_stop_grace_sec=Value(3.0),
        provider=Value("Qobuz"),
        sample_rate=96000,
        poll_source=Mock(),
        after=Mock(),
    )


class SourceAutomationTests(unittest.TestCase):
    def test_qobuz_playback_starts_recording_from_standby(self):
        app = make_app(qobuz_snapshot("playing"), standby=True)

        HiResRecorderApp.poll_source(app)

        app.start_rec.assert_called_once_with()
        self.assertEqual(app.current_source_info["provider"], "qobuz")
        app.log_message.assert_any_call(
            "Qobuz再生検知: Fixture Artist - Fixture Track"
        )

    def test_qobuz_pause_stops_after_configured_grace_period(self):
        app = make_app(qobuz_snapshot("paused"), recording=True)
        app.source_idle_since = time.time() - 4.0

        HiResRecorderApp.poll_source(app)

        app.stop_rec.assert_called_once_with()
        app.log_message.assert_any_call("Qobuz自動停止検知")

    def test_qobuz_pause_does_not_stop_before_grace_period(self):
        app = make_app(qobuz_snapshot("paused"), recording=True)

        HiResRecorderApp.poll_source(app)

        app.stop_rec.assert_not_called()
        self.assertIsNotNone(app.source_idle_since)


if __name__ == "__main__":
    unittest.main()
