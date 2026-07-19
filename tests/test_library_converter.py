import os
import shutil
import tempfile
import unittest

import numpy as np
import av
import soundfile as sf
from mutagen.flac import FLAC

from library_converter import (
    LibraryConversionQueue,
    check_library_capacity,
    library_codec_diagnostics,
    probe_library_audio,
    scan_library,
)
from recording_catalog import list_library_assets, list_library_jobs


def tone(sample_rate, seconds=0.5, amplitude=0.35):
    timeline = np.arange(int(sample_rate * seconds), dtype=np.float64) / sample_rate
    mono = amplitude * np.sin(2.0 * np.pi * 997.0 * timeline)
    return np.column_stack((mono, mono))


def write_av_audio(path, codec="aac", sample_rate=44100, sample_format="fltp"):
    audio = tone(sample_rate, seconds=0.25)
    if sample_format == "s16p":
        audio = np.rint(audio * 32767.0).astype(np.int16).T
    else:
        audio = audio.astype(np.float32).T
    with av.open(path, mode="w") as container:
        stream = container.add_stream(codec, rate=sample_rate)
        stream.layout = "stereo"
        frame_size = 1024
        for start in range(0, audio.shape[1], frame_size):
            block = audio[:, start : start + frame_size]
            frame = av.AudioFrame.from_ndarray(
                np.ascontiguousarray(block), format=sample_format, layout="stereo"
            )
            frame.sample_rate = sample_rate
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)


class LibraryConverterTests(unittest.TestCase):
    def test_required_pyav_decoders_are_available(self):
        diagnostics = library_codec_diagnostics()

        self.assertTrue(diagnostics["ok"], diagnostics)
        self.assertEqual(diagnostics["missing_decoders"], [])

    def run_job(self, source_dir, destination_dir, database_path, cache_dir):
        summary = scan_library(
            source_dir,
            destination_dir,
            database_path=database_path,
        )
        queue = LibraryConversionQueue(database_path, cache_dir=cache_dir)
        queue.start(summary["job_id"])
        queue.wait(20)
        self.assertFalse(queue.running)
        return summary, list_library_assets(
            summary["job_id"], database_path=database_path
        )

    def test_16_bit_44100_becomes_no_dither_pcm24_48000(self):
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "source")
            destination = os.path.join(directory, "destination")
            cache = os.path.join(directory, "cache")
            database = os.path.join(directory, "catalog.sqlite3")
            os.makedirs(source)
            input_path = os.path.join(source, "Artist - Track.wav")
            sf.write(input_path, tone(44100), 44100, subtype="PCM_16")

            _, assets = self.run_job(source, destination, database, cache)

            self.assertEqual(assets[0]["status"], "complete", assets[0]["reason"])
            output = assets[0]["output_path"]
            info = sf.info(output)
            tags = FLAC(output)
            self.assertEqual(info.samplerate, 48000)
            self.assertEqual(info.subtype, "PCM_24")
            self.assertEqual(tags["DITHER"], ["NONE"])
            self.assertEqual(tags["SRC_QUALITY"], ["SOXR_VHQ"])
            self.assertTrue(os.path.isfile(input_path))
            self.assertFalse(os.path.exists(output + ".partial"))

    def test_24_bit_src_uses_deterministic_tpdf(self):
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "source")
            os.makedirs(source)
            input_path = os.path.join(source, "HiRes.flac")
            sf.write(input_path, tone(44100), 44100, format="FLAC", subtype="PCM_24")
            outputs = []
            for index in (1, 2):
                destination = os.path.join(directory, f"destination-{index}")
                database = os.path.join(directory, f"catalog-{index}.sqlite3")
                cache = os.path.join(directory, f"cache-{index}")
                _, assets = self.run_job(source, destination, database, cache)
                self.assertEqual(assets[0]["status"], "complete", assets[0]["reason"])
                outputs.append(
                    sf.read(assets[0]["output_path"], dtype="int32", always_2d=True)[0]
                )
                self.assertEqual(FLAC(assets[0]["output_path"])["DITHER"], ["TPDF"])

            np.testing.assert_array_equal(outputs[0], outputs[1])

    def test_exact_duplicate_is_not_written_twice(self):
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "source")
            destination = os.path.join(directory, "destination")
            cache = os.path.join(directory, "cache")
            database = os.path.join(directory, "catalog.sqlite3")
            os.makedirs(os.path.join(source, "A"))
            os.makedirs(os.path.join(source, "B"))
            first = os.path.join(source, "A", "Track.wav")
            second = os.path.join(source, "B", "Track.wav")
            sf.write(first, tone(48000), 48000, subtype="PCM_24")
            shutil.copyfile(first, second)

            summary, assets = self.run_job(source, destination, database, cache)
            statuses = {item["status"] for item in assets}
            jobs = list_library_jobs(database_path=database)

            self.assertEqual(statuses, {"complete", "duplicate"})
            self.assertEqual(jobs[0]["duplicate_files"], 1)
            self.assertEqual(summary["total_files"], 2)

    def test_scan_skips_stem_and_dsd_with_reasons(self):
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "source")
            destination = os.path.join(directory, "destination")
            database = os.path.join(directory, "catalog.sqlite3")
            os.makedirs(source)
            for filename in ("Deck.stem.mp4", "Archive.dsf"):
                with open(os.path.join(source, filename), "wb") as handle:
                    handle.write(b"fixture")

            summary = scan_library(source, destination, database_path=database)
            assets = list_library_assets(
                summary["job_id"], database_path=database
            )

            self.assertEqual(summary["skipped_files"], 2)
            self.assertTrue(all(item["status"] == "skipped" for item in assets))
            self.assertTrue(any("Stem" in item["reason"] for item in assets))
            self.assertTrue(any("DSD" in item["reason"] for item in assets))

    def test_capacity_keeps_ten_percent_or_fifty_gib_free(self):
        with tempfile.TemporaryDirectory() as directory:
            result = check_library_capacity(directory, 0)

        self.assertEqual(result["reserve"], max(result["total"] // 10, 50 * 1024**3))
        self.assertEqual(result["required"], result["reserve"])

    def test_probe_preserves_mono_without_fake_stereo(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "mono.wav")
            sf.write(path, tone(48000)[:, 0], 48000, subtype="PCM_24")

            probe = probe_library_audio(path)

        self.assertEqual(probe.channels, 1)
        self.assertEqual(probe.sample_rate, 48000)

    def test_aac_uses_pyav_decode_and_remains_marked_lossy(self):
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "source")
            destination = os.path.join(directory, "destination")
            cache = os.path.join(directory, "cache")
            database = os.path.join(directory, "catalog.sqlite3")
            os.makedirs(source)
            input_path = os.path.join(source, "Compressed.m4a")
            write_av_audio(input_path)

            probe = probe_library_audio(input_path)
            _, assets = self.run_job(source, destination, database, cache)

            self.assertEqual(probe.decoder, "pyav")
            self.assertFalse(probe.lossless)
            self.assertEqual(assets[0]["status"], "complete", assets[0]["reason"])
            tags = FLAC(assets[0]["output_path"])
            self.assertEqual(tags["SOURCE_LOSSLESS"], ["NO"])
            self.assertEqual(tags["DITHER"], ["NONE"])

    def test_alac_uses_container_bit_depth_for_dither_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "source")
            destination = os.path.join(directory, "destination")
            cache = os.path.join(directory, "cache")
            database = os.path.join(directory, "catalog.sqlite3")
            os.makedirs(source)
            input_path = os.path.join(source, "Lossless.m4a")
            write_av_audio(input_path, codec="alac", sample_format="s16p")

            probe = probe_library_audio(input_path)
            summary, assets = self.run_job(source, destination, database, cache)

            self.assertTrue(probe.lossless)
            self.assertEqual(probe.bit_depth, 24)
            self.assertEqual(summary["sample_rates"], {"44100": 1})
            self.assertEqual(summary["bit_depths"], {"24": 1})
            self.assertEqual(assets[0]["status"], "complete", assets[0]["reason"])
            tags = FLAC(assets[0]["output_path"])
            self.assertEqual(tags["SOURCE_CODEC"], ["alac"])
            self.assertEqual(tags["SOURCE_BIT_DEPTH"], ["24"])
            self.assertEqual(tags["DITHER"], ["TPDF"])

    def test_24_bit_48khz_bypass_preserves_decoded_pcm_exactly(self):
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "source")
            destination = os.path.join(directory, "destination")
            cache = os.path.join(directory, "cache")
            database = os.path.join(directory, "catalog.sqlite3")
            os.makedirs(source)
            input_path = os.path.join(source, "Native.flac")
            sf.write(input_path, tone(48000), 48000, format="FLAC", subtype="PCM_24")
            expected = sf.read(input_path, dtype="int32", always_2d=True)[0]

            _, assets = self.run_job(source, destination, database, cache)

            self.assertEqual(assets[0]["status"], "complete", assets[0]["reason"])
            actual = sf.read(
                assets[0]["output_path"], dtype="int32", always_2d=True
            )[0]
            np.testing.assert_array_equal(actual, expected)
            tags = FLAC(assets[0]["output_path"])
            self.assertEqual(tags["SRC_ENGINE"], ["BYPASS"])
            self.assertEqual(tags["DITHER"], ["NONE"])

    def test_conversion_matrix_preserves_strict_dither_and_src_policy(self):
        cases = (
            ("16-48.wav", 48000, "PCM_16", "WAV", "NONE", "BYPASS"),
            ("24-48.flac", 48000, "PCM_24", "FLAC", "NONE", "BYPASS"),
            ("24-96.flac", 96000, "PCM_24", "FLAC", "TPDF", "libsoxr"),
            ("24-192.flac", 192000, "PCM_24", "FLAC", "TPDF", "libsoxr"),
            ("float-44.wav", 44100, "DOUBLE", "WAV", "TPDF", "libsoxr"),
            ("lossy-44.ogg", 44100, "VORBIS", "OGG", "NONE", "libsoxr"),
        )
        for filename, rate, subtype, format_name, dither, src in cases:
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as directory:
                source = os.path.join(directory, "source")
                destination = os.path.join(directory, "destination")
                cache = os.path.join(directory, "cache")
                database = os.path.join(directory, "catalog.sqlite3")
                os.makedirs(source)
                sf.write(
                    os.path.join(source, filename),
                    tone(rate, seconds=0.08),
                    rate,
                    subtype=subtype,
                    format=format_name,
                )

                _, assets = self.run_job(source, destination, database, cache)

                self.assertEqual(assets[0]["status"], "complete", assets[0]["reason"])
                tags = FLAC(assets[0]["output_path"])
                self.assertEqual(tags["DITHER"], [dither])
                self.assertEqual(tags["SRC_ENGINE"], [src])
                self.assertEqual(sf.info(assets[0]["output_path"]).samplerate, 48000)


if __name__ == "__main__":
    unittest.main()
