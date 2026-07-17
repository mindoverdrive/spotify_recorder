import json
import os
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from spotify_quality_audit import (
    CaptureQualityAudit,
    audit_for_audio_range,
    evaluate_spotify_quality_settings,
    parse_network_quality_output,
    parse_spotify_prefs,
    read_spotify_quality_settings,
    run_network_quality_test,
)
from spotify_recorder import HiResRecorderApp


GOOD_SETTINGS = {
    "available": True,
    "streaming_quality_raw": 5,
    "download_quality_raw": 5,
    "auto_downgrade": False,
    "normalize": False,
    "automix": False,
}


class SpotifySettingsAuditTests(unittest.TestCase):
    def test_parses_and_evaluates_current_lossless_candidate_settings(self):
        prefs = parse_spotify_prefs(
            "\n".join(
                [
                    "audio.play_bitrate_non_metered_enumeration=5",
                    "audio.sync_bitrate_enumeration=5",
                    "audio.allow_downgrade=false",
                    "audio.normalize_v2=false",
                    "audio.automix=false",
                ]
            )
        )
        settings = {
            "available": True,
            "streaming_quality_raw": prefs["audio.play_bitrate_non_metered_enumeration"],
            "download_quality_raw": prefs["audio.sync_bitrate_enumeration"],
            "auto_downgrade": prefs["audio.allow_downgrade"],
            "normalize": prefs["audio.normalize_v2"],
            "automix": prefs["audio.automix"],
        }

        result = evaluate_spotify_quality_settings(settings)

        self.assertTrue(result["conditions_pass"])
        self.assertFalse(result["warnings"])
        self.assertIn("未証明", result["label"])

    def test_reads_latest_spotify_user_prefs(self):
        with tempfile.TemporaryDirectory() as directory:
            user_dir = os.path.join(directory, "Users", "test-user")
            os.makedirs(user_dir)
            with open(os.path.join(user_dir, "prefs"), "w", encoding="utf-8") as handle:
                handle.write(
                    "audio.play_bitrate_non_metered_enumeration=5\n"
                    "audio.sync_bitrate_enumeration=5\n"
                    "audio.allow_downgrade=false\n"
                    "audio.normalize_v2=false\n"
                    "audio.automix=false\n"
                )

            settings = read_spotify_quality_settings(directory)

        self.assertEqual(settings, {**GOOD_SETTINGS, "source_note": "Spotify非公開prefsの観測値"})


class NetworkQualityAuditTests(unittest.TestCase):
    def test_parses_apple_network_quality_json(self):
        result = parse_network_quality_output(
            json.dumps(
                {
                    "dl_throughput": 52_000_000,
                    "base_rtt": 19.2,
                    "interface_name": "en0",
                    "test_endpoint": "example.apple.test",
                }
            ),
            measured_at=1234.0,
        )

        self.assertTrue(result["pass"])
        self.assertEqual(result["download_mbps"], 52.0)
        self.assertEqual(result["measured_at"], 1234.0)

    @patch("spotify_quality_audit.subprocess.run", side_effect=UnicodeDecodeError("utf-8", b"\\xff", 0, 1, "invalid"))
    @patch("spotify_quality_audit.shutil.which", return_value="/usr/bin/networkQuality")
    def test_runner_exception_is_reported_as_a_failed_measurement(self, _which, _run):
        result = run_network_quality_test()

        self.assertFalse(result["available"])
        self.assertIn("networkQuality実行失敗", result["error"])


class NetworkPreflightUiTests(unittest.TestCase):
    def setUp(self):
        self.app = _NetworkPreflightApp()

    def test_timeout_always_reenables_the_measurement_button(self):
        HiResRecorderApp.finish_network_preflight(
            self.app,
            test_id=1,
            adapter_name="Spotify",
            timeout=True,
        )

        self.assertFalse(self.app.network_test_running)
        self.assertEqual(self.app.network_test_btn.options, {"state": "normal", "text": "回線実測"})
        self.assertTrue(any("45秒" in message for message in self.app.logs))

    def test_completed_measurement_is_saved_for_the_active_provider(self):
        result = {
            "available": True,
            "pass": True,
            "warnings": [],
            "download_mbps": 52.0,
            "base_rtt_ms": 19.2,
            "minimum_mbps": 2.0,
        }

        HiResRecorderApp.finish_network_preflight(
            self.app,
            test_id=1,
            adapter_name="Spotify",
            result=result,
        )

        self.assertIs(self.app.last_network_test, result)
        self.assertFalse(self.app.network_test_running)


class _NetworkPreflightApp:
    def __init__(self):
        self.network_test_id = 1
        self.network_test_running = True
        self.network_test_watchdog_id = "watchdog"
        self.network_test_btn = _Button()
        self.provider = _Value("Spotify")
        self.last_network_test = None
        self.logs = []
        self.cancelled_watchdogs = []

    def after_cancel(self, timer_id):
        self.cancelled_watchdogs.append(timer_id)

    def log_message(self, message):
        self.logs.append(message)


class _Button:
    def __init__(self):
        self.options = {}

    def configure(self, **options):
        self.options.update(options)


class _Value:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value


class CaptureAuditTests(unittest.TestCase):
    def test_qobuz_offline_does_not_request_network_measurement(self):
        with patch("spotify_quality_audit.time.time", side_effect=[1000.0, 1001.0]), patch(
            "spotify_quality_audit.time.monotonic", side_effect=[100.0, 101.0]
        ):
            audit = CaptureQualityAudit(
                96000,
                "BlackHole 2ch",
                {},
                provider="qobuz",
                source_evaluation={
                    "conditions_pass": True,
                    "mode": "offline",
                    "assurance_label": "Qobuz Offline条件適合・bit一致未証明",
                },
            )
            result = audit.finish()

        self.assertFalse(any("回線実測" in note for note in result["notes"]))

    def test_quality_gate_pass_does_not_claim_lossless_verification(self):
        with patch("spotify_quality_audit.time.time", side_effect=[1000.0, 1010.0]), patch(
            "spotify_quality_audit.time.monotonic", side_effect=[100.0, 110.0]
        ):
            audit = CaptureQualityAudit(44100, "Loopback", GOOD_SETTINGS)
            audit.record_audio_callback(441000)
            result = audit.finish(
                {
                    "available": True,
                    "sample_count": 5,
                    "inbound_total_bytes": 2_000_000,
                    "inbound_average_kbps": 1600.0,
                    "notes": [],
                }
            )

        self.assertTrue(result["quality_gate_pass"])
        self.assertFalse(result["lossless_verified"])
        self.assertIn("未証明", result["assurance_label"])

    def test_audio_callback_problem_fails_quality_gate(self):
        with patch("spotify_quality_audit.time.time", side_effect=[1000.0, 1001.0]), patch(
            "spotify_quality_audit.time.monotonic", side_effect=[100.0, 101.0]
        ):
            audit = CaptureQualityAudit(44100, "Loopback", GOOD_SETTINGS)
            audit.record_audio_callback(44100, "input overflow")
            result = audit.finish()

        self.assertFalse(result["quality_gate_pass"])
        self.assertEqual(result["callback_status_count"], 1)
        self.assertTrue(any("コールバック異常" in warning for warning in result["warnings"]))

    def test_detects_playback_stall_and_timeline_slip(self):
        audio = np.full((44100, 2), 0.1, dtype=np.float32)
        with patch("spotify_quality_audit.time.time", side_effect=[1000.0, 1002.0]), patch(
            "spotify_quality_audit.time.monotonic", side_effect=[100.0, 102.0]
        ):
            audit = CaptureQualityAudit(44100, "Loopback", GOOD_SETTINGS)
            audit.record_audio_callback(44100, samples=audio)
            audit.observe_spotify_playback(("track",), "playing", 10.0, 100.5)
            audit.record_audio_callback(44100, samples=audio * 0.9)
            audit.observe_spotify_playback(("track",), "playing", 10.0, 101.5)
            result = audit.finish()

        self.assertEqual(result["playback_stall_count"], 1)
        self.assertEqual(result["timeline_slip_count"], 1)
        self.assertFalse(result["quality_gate_pass"])
        self.assertTrue(any(event["type"] == "playback_stall" for event in result["events"]))

        scoped = audit_for_audio_range(result, 44100, 88201)
        self.assertEqual(scoped["playback_stall_count"], 1)
        self.assertEqual(scoped["timeline_slip_count"], 1)

    def test_detects_long_digital_silence_and_boundary_discontinuity(self):
        silence = np.zeros((44100, 2), dtype=np.float32)
        discontinuous = np.ones((44100, 2), dtype=np.float32)
        with patch("spotify_quality_audit.time.time", side_effect=[1000.0, 1002.0]), patch(
            "spotify_quality_audit.time.monotonic", side_effect=[100.0, 102.0]
        ):
            audit = CaptureQualityAudit(44100, "Loopback", GOOD_SETTINGS)
            audit.record_audio_callback(44100, samples=silence)
            audit.record_audio_callback(44100, samples=discontinuous)
            result = audit.finish()

        self.assertEqual(result["digital_zero_run_count"], 1)
        self.assertEqual(result["boundary_discontinuities"], 1)
        self.assertFalse(result["quality_gate_pass"])

    def test_detects_adc_timeline_gap_at_callback_boundary(self):
        audio = np.full((4410, 2), 0.1, dtype=np.float32)
        with patch("spotify_quality_audit.time.time", side_effect=[1000.0, 1001.0]), patch(
            "spotify_quality_audit.time.monotonic", side_effect=[100.0, 101.0]
        ):
            audit = CaptureQualityAudit(44100, "BlackHole", GOOD_SETTINGS)
            audit.record_audio_callback(4410, samples=audio, adc_time=10.0)
            audit.record_audio_callback(4410, samples=audio * 0.9, adc_time=10.12)
            result = audit.finish()

        self.assertEqual(result["adc_timeline_gap_count"], 1)
        self.assertTrue(any(event["type"] == "adc_timeline_gap" for event in result["events"]))
        self.assertFalse(result["quality_gate_pass"])


if __name__ == "__main__":
    unittest.main()
