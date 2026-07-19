import unittest
import ctypes
from unittest.mock import patch

from coreaudio_devices import (
    CoreAudioDevice,
    sample_rate_is_supported,
    sync_nominal_sample_rate,
)


class CoreAudioRateSyncTests(unittest.TestCase):
    def setUp(self):
        self.device = CoreAudioDevice(
            device_id=42,
            name="BlackHole 2ch",
            uid="BlackHole_UID",
            nominal_sample_rate=44100.0,
            transport="virt",
            max_input_channels=2,
            is_aggregate=False,
        )

    def test_supported_range_accepts_discrete_and_continuous_values(self):
        ranges = ((44100.0, 44100.0), (48000.0, 192000.0))

        self.assertTrue(sample_rate_is_supported(44100, ranges))
        self.assertTrue(sample_rate_is_supported(96000, ranges))
        self.assertFalse(sample_rate_is_supported(32000, ranges))

    @patch(
        "coreaudio_devices.available_nominal_sample_rates",
        return_value=((44100.0, 44100.0), (48000.0, 48000.0)),
    )
    def test_matching_rate_is_verified_without_setting(self, _ranges):
        result = sync_nominal_sample_rate(self.device, 44100)

        self.assertTrue(result.verified)
        self.assertFalse(result.changed)
        self.assertEqual(result.rate_after, 44100)

    def test_physical_and_aggregate_devices_are_never_changed(self):
        physical = CoreAudioDevice(
            **{**self.device.to_dict(), "name": "Built-in Microphone"}
        )
        aggregate = CoreAudioDevice(
            **{**self.device.to_dict(), "is_aggregate": True}
        )

        self.assertFalse(sync_nominal_sample_rate(physical, 48000).verified)
        self.assertFalse(sync_nominal_sample_rate(aggregate, 48000).verified)

    @patch(
        "coreaudio_devices.available_nominal_sample_rates",
        return_value=((44100.0, 44100.0), (48000.0, 48000.0)),
    )
    @patch("coreaudio_devices._property_scalar", return_value=48000.0)
    def test_setter_waits_for_and_verifies_target_rate(self, _scalar, _ranges):
        class FakeCoreAudio:
            def __init__(self):
                self.set_calls = 0

            def AudioObjectIsPropertySettable(self, _device, _address, output):
                ctypes.cast(output, ctypes.POINTER(ctypes.c_bool)).contents.value = True
                return 0

            def AudioObjectSetPropertyData(self, *_args):
                self.set_calls += 1
                return 0

        coreaudio = FakeCoreAudio()

        with patch("coreaudio_devices._libraries", return_value=(coreaudio, object())):
            result = sync_nominal_sample_rate(self.device, 48000)

        self.assertTrue(result.verified)
        self.assertTrue(result.changed)
        self.assertEqual(result.rate_before, 44100)
        self.assertEqual(result.rate_after, 48000)
        self.assertEqual(coreaudio.set_calls, 1)


if __name__ == "__main__":
    unittest.main()
