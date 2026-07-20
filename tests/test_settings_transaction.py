import json
import os
import tempfile
import unittest
from unittest.mock import patch

from settings_transaction import CaptureSettingsTransaction


class FakeQobuzAutomation:
    def __init__(self):
        self.prepared = []
        self.restored = []

    def snapshot_state(self):
        return {"track_id": "123", "state": "paused", "output_device_name": "Old"}

    def prepare_job(self, output):
        self.prepared.append(output)

    def restore_state(self, state):
        self.restored.append(state)
        return {"restored": True, "errors": []}


class SettingsTransactionTests(unittest.TestCase):
    def test_journals_before_change_and_restores(self):
        restored = []
        qobuz = FakeQobuzAutomation()
        with tempfile.TemporaryDirectory() as directory:
            transaction = CaptureSettingsTransaction(
                "job",
                42,
                qobuz_automation=qobuz,
                journal_dir=directory,
                coreaudio_capture=lambda device_id: {
                    "capture_device": {"device_id": device_id, "nominal_sample_rate": 44100},
                    "defaults": {},
                },
                coreaudio_restore=lambda state: restored.append(state)
                or {"restored": True, "errors": []},
            )
            with patch("settings_transaction.set_default_audio_device") as set_default:
                path = transaction.begin("Qobuz Capture")
                result = transaction.restore()
            with open(path, "r", encoding="utf-8") as handle:
                journal = json.load(handle)

        self.assertEqual(qobuz.prepared, ["Qobuz Capture"])
        self.assertEqual(qobuz.restored[0]["output_device_name"], "Old")
        set_default.assert_called_once_with("input", 42)
        self.assertTrue(result["restored"])
        self.assertFalse(journal["active"])
        self.assertEqual(restored[0]["capture_device"]["device_id"], 42)


if __name__ == "__main__":
    unittest.main()
