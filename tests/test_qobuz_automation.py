import subprocess
import unittest
from unittest.mock import patch

import ApplicationServices as AS
import Quartz

from qobuz_automation import (
    MacOSQobuzAccessibility,
    QobuzAutomation,
    QobuzAutomationError,
)


class FakeRunner:
    def __init__(self):
        self.commands = []

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")


class FakeAccessibility:
    def __init__(self):
        self.checked = 0
        self.activated = 0
        self.track_requests = []
        self.output_requests = []

    def check(self):
        self.checked += 1
        return True

    def activate(self):
        self.activated += 1

    def track_play_point(self, title, track_number):
        self.track_requests.append((title, track_number))
        return 81.0, 467.0

    def output_button_point(self):
        return 1100.0, 845.0

    def output_device_point(self, device_name):
        self.output_requests.append(device_name)
        return 1021.0, 376.0


class QobuzAutomationTests(unittest.TestCase):
    def test_pyobjc_axvalue_success_tuple_is_unwrapped(self):
        accessibility = MacOSQobuzAccessibility()
        value = AS.AXValueCreate(
            AS.kAXValueCGPointType, Quartz.CGPoint(12.5, 34.5)
        )
        with patch.object(accessibility, "_attribute", return_value=value):
            point = accessibility._point_value(
                object(), AS.kAXPositionAttribute, AS.kAXValueCGPointType
            )

        self.assertEqual(point, (12.5, 34.5))

    def test_open_track_uses_album_route_and_verifies_track_head(self):
        runner = FakeRunner()
        accessibility = FakeAccessibility()
        clicks = []
        snapshots = iter(
            [
                {"track_id": "old", "state": "playing", "position": 42.0},
                {"track_id": "123", "state": "playing", "position": 0.4},
            ]
        )
        automation = QobuzAutomation(
            runner=runner,
            snapshot_reader=lambda: next(snapshots),
            sleep=lambda _seconds: None,
            point_clicker=lambda x, y: clicks.append((x, y)),
            accessibility=accessibility,
        )
        with patch("qobuz_automation.qobuz_is_running", return_value=True):
            result = automation.open_track(
                "123", album_id="album-1", track_number=2, title="Target"
            )

        self.assertEqual(result["track_id"], "123")
        self.assertIn(["open", "qobuzapp://album/album-1"], runner.commands)
        self.assertEqual(accessibility.track_requests, [("Target", 2)])
        self.assertEqual(clicks, [(81.0, 467.0)])

    def test_open_track_refuses_unverifiable_public_route(self):
        automation = QobuzAutomation(
            runner=FakeRunner(),
            snapshot_reader=lambda: {},
            sleep=lambda _seconds: None,
            point_clicker=lambda _x, _y: None,
            accessibility=FakeAccessibility(),
        )
        with patch("qobuz_automation.qobuz_is_running", return_value=True):
            with self.assertRaises(QobuzAutomationError):
                automation.open_track("123")

    def test_output_selection_uses_two_verified_accessibility_points(self):
        runner = FakeRunner()
        accessibility = FakeAccessibility()
        clicks = []
        snapshots = iter(
            [
                {"output_device_name": "Old Device"},
                {"output_device_name": "Hi-Res Recorder Qobuz Loopback"},
            ]
        )
        automation = QobuzAutomation(
            runner=runner,
            snapshot_reader=lambda: next(snapshots),
            sleep=lambda _seconds: None,
            point_clicker=lambda x, y: clicks.append((x, y)),
            accessibility=accessibility,
        )
        automation.select_output_device("Hi-Res Recorder Qobuz Loopback")

        self.assertEqual(clicks, [(1100.0, 845.0), (1021.0, 376.0)])
        self.assertEqual(
            accessibility.output_requests, ["Hi-Res Recorder Qobuz Loopback"]
        )

    def test_accessibility_check_is_in_process_and_never_runs_osascript(self):
        runner = FakeRunner()
        accessibility = FakeAccessibility()
        automation = QobuzAutomation(
            runner=runner,
            sleep=lambda _seconds: None,
            accessibility=accessibility,
        )
        with patch("qobuz_automation.qobuz_is_running", return_value=True):
            self.assertTrue(automation.check_accessibility())

        self.assertEqual(accessibility.checked, 1)
        self.assertFalse(
            any(command and command[0] == "osascript" for command in runner.commands)
        )

    def test_pause_uses_in_process_core_graphics_key_event(self):
        keys = []
        accessibility = FakeAccessibility()
        automation = QobuzAutomation(
            snapshot_reader=lambda: {"state": "playing"},
            sleep=lambda _seconds: None,
            key_presser=keys.append,
            accessibility=accessibility,
        )

        automation.pause()

        self.assertEqual(keys, [49])
        self.assertEqual(accessibility.activated, 1)


if __name__ == "__main__":
    unittest.main()
