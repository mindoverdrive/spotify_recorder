import os
import subprocess
import tempfile
import urllib.request
from datetime import datetime

import numpy as np
import pyloudnorm as pyln
import soxr
import soundfile as sf
from scipy.signal import lfilter, resample_poly

from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, TALB, TIT2, TPE1, TXXX
from mutagen.wave import WAVE

from recording_catalog import (
    list_audio_exports_for_source,
    record_audio_export,
    record_saved_recording,
    replace_recording_file,
)
from spotify_quality_audit import (
    audit_for_audio_range,
    evaluate_spotify_quality_settings,
    format_timecode,
    read_spotify_quality_settings,
)

MODE_ALBUM = "Album (Auto-split)"
MODE_SINGLE = "Single Track"
MODE_MANUAL = "Manual (No split)"
MODE_PRESETS = [MODE_ALBUM, MODE_SINGLE, MODE_MANUAL]

FORMAT_WAV = "WAV"
OUTPUT_FORMATS = [FORMAT_WAV]
UNITY_GAIN = 1.0
WAV_SUBTYPE = "FLOAT"
TRUE_PEAK_OVERSAMPLE = 4
ANALYSIS_CHUNK_FRAMES = 262144
TRUE_PEAK_OVERLAP_FRAMES = 256
RF64_DATA_THRESHOLD = 3_900_000_000
FLAC_BIT_DEPTH = 24
FLAC_SUBTYPE = "PCM_24"
DITHER_NONE = "NONE"
DITHER_TPDF = "TPDF"
QUANTIZER = "ROUND_TO_NEAREST_EVEN"
DJ_SAMPLE_RATE = 48000
SRC_ENGINE = "libsoxr"
SRC_QUALITY = "SOXR_VHQ"
SRC_PHASE = "LINEAR"
PCM24_SCALE = 1 << 23
PCM24_MIN = -(1 << 23)
PCM24_MAX = (1 << 23) - 1


class FlacExportRejected(ValueError):
    pass


def run_applescript(script, timeout=1.5):
    try:
        return subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except Exception as exc:
        return exc

def build_diagnostic_lines(sample_rate=None):
    lines = []
    # 1. Check Automation
    res = run_applescript('tell application "Spotify" to get player state')
    if isinstance(res, Exception):
        lines.append(f"❌ Automation Error: {res}")
    elif res.returncode != 0:
        if "1743" in res.stderr or "not allowed" in res.stderr.lower() or "許可されていません" in res.stderr:
            lines.append("❌ Automation Permission: Not Allowed (システム設定 > プライバシーとセキュリティ > オートメーション を確認)")
        elif "600" in res.stderr:
            lines.append("⚠️ Spotify is not running (Spotifyアプリを起動してください)")
        else:
            lines.append(f"❌ Spotify Automation Error: {res.stderr.strip()}")
    else:
        lines.append("✅ Spotify Automation Permission: OK")
        
    # 2. Check Audio Devices (Loopback/BlackHole)
    import sounddevice as sd
    try:
        devices = sd.query_devices()
        has_virtual = False
        for d in devices:
            name = d['name'].lower()
            if d['max_input_channels'] > 0 and ('loopback' in name or 'blackhole' in name):
                has_virtual = True
                break
        if has_virtual:
            lines.append("✅ Virtual Audio Device: Found (Loopback / BlackHole)")
        else:
            lines.append("⚠️ Virtual Audio Device: Not Found. (LoopbackやBlackHoleがインストールされていません)")
            
        # 3. Check Mic Permission
        try:
            with sd.InputStream(samplerate=44100, channels=1, blocksize=1024):
                pass
            lines.append("✅ Microphone Permission: OK")
        except Exception as e:
            err_str = str(e).lower()
            if "not found" not in err_str: # Only fail if it's a permission issue, not missing device
                 lines.append(f"❌ Microphone Permission Error: {str(e)}")
            
    except Exception as e:
        lines.append(f"❌ SoundDevice Error: {str(e)}")
        
    lines.append("✅ Capture Gain: Unity Gain (1.0 / DSPなし)")
    lines.append(
        "✅ Output: Archive native-rate + DJ 24-bit/48kHz / SoXR VHQ Linear / conditional dither"
    )
    settings = read_spotify_quality_settings()
    settings_evaluation = evaluate_spotify_quality_settings(settings)
    if settings_evaluation["conditions_pass"]:
        lines.append(
            "✅ Spotify設定証跡: Offline Mode ON / Losslessダウンロード候補 / 音量均一OFF / Automix OFF"
        )
    else:
        for warning in settings_evaluation["warnings"]:
            lines.append(f"⚠️ Spotify設定証跡: {warning}")
    lines.append("ℹ️ 実効コーデックはSpotify公開APIから取得できないため、Lossless断定はできません")
    lines.append("ℹ️ SpotifyはLosslessダウンロード完了後、File > Offline Modeでのみ録音できます")
    if sample_rate is not None:
        if int(sample_rate) == 44100:
            lines.append("✅ Sample Rate: 44100 Hz (Spotifyソースと一致)")
        else:
            lines.append(
                f"⚠️ Sample Rate: {int(sample_rate)} Hz。録音中は変換せず、このレートで保存します"
            )
    return lines

def get_spotify_info_extended():
    script = """
    if application "Spotify" is running then
        tell application "Spotify"
            if player state is playing or player state is paused then
                set t_name to name of current track
                set t_artist to artist of current track
                set t_album to album of current track
                set t_state to player state as string
                set t_pos to player position
                set t_dur to duration of current track
                set t_art to artwork url of current track
                set t_id to id of current track
                return t_name & "||" & t_artist & "||" & t_album & "||" & t_state & "||" & t_pos & "||" & t_dur & "||" & t_art & "||" & t_id
            else
                return "IDLE"
            end if
        end tell
    else
        return "CLOSED"
    end if
    """
    result = run_applescript(script)
    if isinstance(result, Exception):
        return {"status": "ERROR", "error": str(result)}

    if result.returncode == 0:
        raw = result.stdout.strip()
        if raw in ("CLOSED", "IDLE"):
            return {"status": raw}

        parts = raw.split("||")
        if len(parts) >= 8:
            try:
                pos = float(parts[4])
                dur = float(parts[5]) / 1000.0 if parts[5].isdigit() else 0.0
            except ValueError:
                pos, dur = 0.0, 0.0
            return {
                "status": "OK",
                "name": parts[0],
                "artist": parts[1],
                "album": parts[2],
                "state": parts[3],
                "position": pos,
                "duration": dur,
                "artwork_url": parts[6] if parts[6] and not parts[6].startswith("msng") else None,
                "track_id": parts[7],
            }

    err_msg = result.stderr.strip()
    if "1743" in err_msg or "not allowed" in err_msg:
        return {"status": "PERMISSION_DENIED", "error": err_msg}
    return {"status": "NOT_LINKED", "error": err_msg}

def download_url_bytes(url):
    if not url or not url.startswith("http"):
        return None
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.read()
    except Exception:
        return None

def normalized_track_key(info):
    if not info or info.get("status") != "OK":
        return None
    return (
        info.get("name", "").strip().lower(),
        info.get("artist", "").strip().lower(),
        info.get("album", "").strip().lower(),
    )

def tag_wav(wav_path, track_info, artwork_bytes, analysis, capture_audit=None):
    try:
        title = track_info.get("name", "Unknown")
        artist = track_info.get("artist", "Unknown")
        album = track_info.get("album", "Unknown")
        audio = WAVE(wav_path)
        if audio.tags is None:
            audio.add_tags()
        audio.tags.add(TIT2(encoding=3, text=title))
        audio.tags.add(TPE1(encoding=3, text=artist))
        audio.tags.add(TALB(encoding=3, text=album))
        audio.tags.add(TXXX(encoding=3, desc="Capture Gain", text="1.0 (Unity Gain)"))
        audio.tags.add(TXXX(encoding=3, desc="WAV Encoding", text="32-bit IEEE float"))
        audio.tags.add(TXXX(encoding=3, desc="Sample Rate", text=str(analysis["sample_rate"])))
        if analysis["integrated_lufs"] is not None:
            audio.tags.add(
                TXXX(
                    encoding=3,
                    desc="Integrated LUFS",
                    text=f'{analysis["integrated_lufs"]:.2f}',
                )
            )
        audio.tags.add(
            TXXX(
                encoding=3,
                desc="Sample Peak dBFS",
                text=format_db(analysis["sample_peak_dbfs"]),
            )
        )
        audio.tags.add(
            TXXX(
                encoding=3,
                desc="True Peak dBTP",
                text=format_db(analysis["true_peak_dbtp"]),
            )
        )
        audio.tags.add(
            TXXX(
                encoding=3,
                desc="Full-scale Sample Count",
                text=str(analysis["full_scale_sample_count"]),
            )
        )
        suspect_locations = format_analysis_suspect_locations(analysis)
        if suspect_locations:
            audio.tags.add(
                TXXX(
                    encoding=3,
                    desc="Audio Suspect Locations",
                    text=" / ".join(suspect_locations),
                )
            )
        if capture_audit:
            provider = str(capture_audit.get("provider", "spotify")).lower()
            provider_label = "Qobuz" if provider == "qobuz" else "Spotify"
            settings = capture_audit.get("spotify_settings") or {}
            source = capture_audit.get("source_evaluation") or {}
            evidence = source.get("evidence") or {}
            audio.tags.add(
                TXXX(
                    encoding=3,
                    desc="Capture Assurance",
                    text=capture_audit.get("assurance_label", "Unknown"),
                )
            )
            audio.tags.add(
                TXXX(
                    encoding=3,
                    desc="Lossless Verified",
                    text=f"No - {provider_label} source bits were not available for comparison",
                )
            )
            audio.tags.add(TXXX(encoding=3, desc="Provider", text=provider_label))
            audio.tags.add(
                TXXX(
                    encoding=3,
                    desc="Source Mode",
                    text=str(source.get("mode", "offline")),
                )
            )
            if source.get("source_sample_rate"):
                audio.tags.add(
                    TXXX(
                        encoding=3,
                        desc="Source Sample Rate",
                        text=str(source["source_sample_rate"]),
                    )
                )
            if source.get("source_bit_depth"):
                audio.tags.add(
                    TXXX(
                        encoding=3,
                        desc="Source Bit Depth",
                        text=str(source["source_bit_depth"]),
                    )
                )
            if evidence.get("format_label"):
                audio.tags.add(
                    TXXX(
                        encoding=3,
                        desc="Source Format",
                        text=str(evidence["format_label"]),
                    )
                )
            if provider == "spotify":
                audio.tags.add(
                    TXXX(
                        encoding=3,
                        desc="Spotify Download Quality Setting Raw",
                        text=str(settings.get("download_quality_raw", "Unknown")),
                    )
                )
                audio.tags.add(
                    TXXX(
                        encoding=3,
                        desc="Spotify Offline Mode",
                        text=(
                            "Enabled"
                            if (settings.get("offline_mode") or {}).get("enabled") is True
                            else "Not verified"
                        ),
                    )
                )
            audio.tags.add(
                TXXX(
                    encoding=3,
                    desc="Audio Callback Anomalies",
                    text=str(capture_audit.get("callback_status_count", 0)),
                )
            )
            audio.tags.add(
                TXXX(
                    encoding=3,
                    desc="ADC Timeline Gaps",
                    text=(
                        f'{capture_audit.get("adc_timeline_gap_count", 0)} events / '
                        f'max {capture_audit.get("max_adc_timeline_gap_sec", 0.0):.6f} sec'
                    ),
                )
            )
            audio.tags.add(
                TXXX(
                    encoding=3,
                    desc="Playback Stall Suspicions",
                    text=(
                        f'{capture_audit.get("playback_stall_count", 0)} events / '
                        f'{capture_audit.get("playback_stall_sec", 0.0):.3f} sec'
                    ),
                )
            )
            audio.tags.add(
                TXXX(
                    encoding=3,
                    desc="Timeline Slip Suspicions",
                    text=(
                        f'{capture_audit.get("timeline_slip_count", 0)} events / '
                        f'max {capture_audit.get("max_timeline_slip_sec", 0.0):.3f} sec'
                    ),
                )
            )
            audio.tags.add(
                TXXX(
                    encoding=3,
                    desc="Digital Silence Suspicions",
                    text=(
                        f'{capture_audit.get("digital_zero_run_count", 0)} events / '
                        f'max {capture_audit.get("longest_digital_zero_sec", 0.0):.3f} sec'
                    ),
                )
            )
            audio.tags.add(
                TXXX(
                    encoding=3,
                    desc="Audio Block Repeats",
                    text=str(capture_audit.get("repeated_audio_blocks", 0)),
                )
            )
            audio.tags.add(
                TXXX(
                    encoding=3,
                    desc="Callback Boundary Discontinuities",
                    text=str(capture_audit.get("boundary_discontinuities", 0)),
                )
            )
            if capture_audit.get("warnings"):
                audio.tags.add(
                    TXXX(
                        encoding=3,
                        desc="Capture Audit Warnings",
                        text=" / ".join(capture_audit["warnings"]),
                    )
                )
            capture_locations = [
                f"{format_timecode(event.get('time_sec', 0.0))}: "
                f"{event.get('detail', event.get('type', '異常疑い'))}"
                for event in capture_audit.get("events", [])[:20]
            ]
            if capture_locations:
                audio.tags.add(
                    TXXX(
                        encoding=3,
                        desc="Capture Suspect Locations",
                        text=" / ".join(capture_locations),
                    )
                )
        if artwork_bytes:
            mime = "image/png" if artwork_bytes.startswith(b"\x89PNG") else "image/jpeg"
            audio.tags.add(
                APIC(
                    encoding=3,
                    mime=mime,
                    type=3,
                    desc="Front Cover",
                    data=artwork_bytes,
                )
            )
        audio.save()
    except Exception as e:
        print(f"Tagging error: {e}")


def _image_mime(artwork_bytes):
    if not artwork_bytes:
        return None
    if artwork_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if artwork_bytes.startswith(b"RIFF") and artwork_bytes[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _quantize_pcm24(samples, dither, rng, gain=1.0):
    source = np.asarray(samples, dtype=np.float64) * float(gain)
    if not np.isfinite(source).all():
        raise ValueError("音声データにNaNまたはInfが含まれています")
    scaled = source * PCM24_SCALE
    if dither == DITHER_TPDF:
        scaled = scaled + rng.random(source.shape) - rng.random(source.shape)
    elif dither != DITHER_NONE:
        raise ValueError(f"未対応のディザ方式です: {dither}")
    quantized = np.rint(scaled)
    if quantized.size and (
        float(np.min(quantized)) < PCM24_MIN
        or float(np.max(quantized)) > PCM24_MAX
    ):
        raise FlacExportRejected(
            "PCM24の表現範囲を超えるため、クリップせずFLAC変換を拒否しました"
        )
    quantized = quantized.astype(np.int32)
    return np.left_shift(quantized, 8)


def _scan_wav_for_flac(wav_path):
    peak = 0.0
    with sf.SoundFile(wav_path, mode="r") as source:
        properties = {
            "sample_rate": int(source.samplerate),
            "channels": int(source.channels),
            "frames": int(source.frames),
        }
        while True:
            chunk = source.read(
                frames=ANALYSIS_CHUNK_FRAMES,
                dtype="float32",
                always_2d=True,
            )
            if not len(chunk):
                break
            if not np.isfinite(chunk).all():
                raise ValueError("WAVにNaNまたはInfが含まれています")
            peak = max(peak, float(np.max(np.abs(chunk))))
    properties["sample_peak"] = peak
    properties["sample_peak_dbfs"] = dbfs(peak)
    return properties


class _AudioFileView:
    def __init__(self, path):
        self.path = os.path.abspath(path)
        info = sf.info(self.path)
        self.shape = (int(info.frames), int(info.channels))

    def __len__(self):
        return self.shape[0]

    def __getitem__(self, item):
        if not isinstance(item, slice) or item.step not in (None, 1):
            raise TypeError("AudioFileViewは連続スライスだけをサポートします")
        start = 0 if item.start is None else max(0, int(item.start))
        stop = self.shape[0] if item.stop is None else min(self.shape[0], int(item.stop))
        if stop <= start:
            return np.empty((0, self.shape[1]), dtype=np.float64)
        with sf.SoundFile(self.path, mode="r") as source:
            source.seek(start)
            return source.read(
                frames=stop - start,
                dtype="float64",
                always_2d=True,
            )


def _analyze_audio_file(path):
    info = sf.info(path)
    return analyze_audio(_AudioFileView(path), int(info.samplerate))


def _resample_to_float64_wav(source_path, target_path, output_rate=DJ_SAMPLE_RATE):
    source_info = sf.info(source_path)
    input_rate = int(source_info.samplerate)
    channels = int(source_info.channels)
    if input_rate == int(output_rate):
        raise ValueError("同一レートではSoXRを呼び出しません")
    stream = soxr.ResampleStream(
        input_rate,
        int(output_rate),
        channels,
        dtype="float64",
        quality="VHQ",
        vr=False,
    )
    written = 0
    with sf.SoundFile(source_path, mode="r") as source, sf.SoundFile(
        target_path,
        mode="w",
        samplerate=int(output_rate),
        channels=channels,
        format="WAV",
        subtype="DOUBLE",
    ) as output:
        current = source.read(
            frames=ANALYSIS_CHUNK_FRAMES,
            dtype="float64",
            always_2d=True,
        )
        if not len(current):
            flushed = stream.resample_chunk(
                np.empty((0, channels), dtype=np.float64),
                last=True,
            )
            if len(flushed):
                output.write(flushed)
                written += len(flushed)
        while len(current):
            following = source.read(
                frames=ANALYSIS_CHUNK_FRAMES,
                dtype="float64",
                always_2d=True,
            )
            converted = stream.resample_chunk(current, last=not len(following))
            if not np.isfinite(converted).all():
                raise ValueError("SoXR出力にNaNまたはInfが含まれています")
            if len(converted):
                output.write(converted)
                written += len(converted)
            current = following
    expected = round(int(source_info.frames) * int(output_rate) / input_rate)
    if abs(written - expected) > 1:
        raise RuntimeError(
            f"SoXR出力フレーム数が不正です: expected {expected}, actual {written}"
        )
    return {
        "input_sample_rate": input_rate,
        "output_sample_rate": int(output_rate),
        "channels": channels,
        "input_frames": int(source_info.frames),
        "output_frames": written,
        "src_engine": SRC_ENGINE,
        "src_quality": SRC_QUALITY,
        "src_phase": SRC_PHASE,
        "soxr_version": getattr(soxr, "__version__", "unknown"),
    }


def _wav_export_metadata(wav_path):
    result = {
        "title": os.path.splitext(os.path.basename(wav_path))[0],
        "artist": "Unknown",
        "album": "Unknown",
        "artwork_bytes": None,
    }
    try:
        audio = WAVE(wav_path)
        tags = audio.tags
        if tags is None:
            return result
        mapping = {"TIT2": "title", "TPE1": "artist", "TALB": "album"}
        for frame_id, field in mapping.items():
            frame = tags.get(frame_id)
            if frame is not None and getattr(frame, "text", None):
                result[field] = str(frame.text[0])
        pictures = tags.getall("APIC")
        if pictures:
            result["artwork_bytes"] = bytes(pictures[0].data)
    except Exception:
        pass
    return result


def _tag_flac(
    flac_path,
    track_info,
    artwork_bytes,
    scan,
    analysis=None,
    capture_audit=None,
    processing=None,
):
    audio = FLAC(flac_path)
    process = processing or {}
    title = track_info.get("name") or track_info.get("title") or "Unknown"
    artist = track_info.get("artist") or "Unknown"
    album = track_info.get("album") or "Unknown"
    audio["TITLE"] = title
    audio["ARTIST"] = artist
    audio["ALBUM"] = album
    for field in (
        "albumartist",
        "date",
        "genre",
        "tracknumber",
        "discnumber",
        "comment",
        "isrc",
        "bpm",
        "initialkey",
        "composer",
        "label",
    ):
        value = track_info.get(field)
        if value not in (None, ""):
            audio[field.upper()] = str(value)
    audio["SOURCE_FORMAT"] = str(
        process.get("source_format") or "WAV/RF64 IEEE float capture"
    )
    if process.get("source_format"):
        audio["SOURCE_CODEC"] = str(process["source_format"])
    if process.get("source_lossless") is not None:
        audio["SOURCE_LOSSLESS"] = "YES" if process["source_lossless"] else "NO"
    if process.get("source_sha256"):
        audio["SOURCE_SHA256"] = str(process["source_sha256"])
    if process.get("pcm_sha256"):
        audio["SOURCE_PCM_SHA256"] = str(process["pcm_sha256"])
    export_role = str(process.get("export_role") or "archive")
    if export_role == "library":
        audio["PROCESSING_GAIN_DB"] = f'{float(process.get("safety_gain_db") or 0.0):.6f}'
    else:
        audio["CAPTURE_GAIN"] = "1.0 (Unity Gain)"
    audio["EXPORT_ROLE"] = export_role
    audio["DITHER"] = str(process.get("dither") or DITHER_NONE)
    audio["DITHER_REASON"] = str(process.get("dither_reason") or "No dither required")
    audio["QUANTIZER"] = str(process.get("quantizer") or QUANTIZER)
    audio["BIT_DEPTH"] = str(FLAC_BIT_DEPTH)
    audio["SAMPLE_RATE"] = str(scan["sample_rate"])
    audio["OUTPUT_SAMPLE_RATE"] = str(scan["sample_rate"])
    if process.get("source_sample_rate") is not None:
        audio["SOURCE_SAMPLE_RATE"] = str(process["source_sample_rate"])
    if process.get("source_bit_depth") is not None:
        audio["SOURCE_BIT_DEPTH"] = str(process["source_bit_depth"])
    audio["SOURCE_VERIFIED"] = "YES" if process.get("source_verified") else "NO"
    if process.get("src_engine"):
        audio["SRC_ENGINE"] = str(process["src_engine"])
        audio["SRC_QUALITY"] = str(process.get("src_quality") or SRC_QUALITY)
        audio["SRC_PHASE"] = str(process.get("src_phase") or SRC_PHASE)
        audio["SOXR_VERSION"] = str(process.get("soxr_version") or "unknown")
    else:
        audio["SRC_ENGINE"] = "BYPASS"
        audio["SRC_QUALITY"] = "BYPASS"
        audio["SRC_PHASE"] = "BYPASS"
    audio["DJ_SAFETY_GAIN_DB"] = f'{float(process.get("safety_gain_db") or 0.0):.6f}'
    input_true_peak = process.get("input_true_peak_dbtp")
    if input_true_peak is not None:
        audio["INPUT_TRUE_PEAK_DBTP"] = format_db(float(input_true_peak))
    if analysis:
        if analysis.get("integrated_lufs") is not None:
            audio["INTEGRATED_LUFS"] = f'{analysis["integrated_lufs"]:.2f}'
        audio["SAMPLE_PEAK_DBFS"] = format_db(analysis.get("sample_peak_dbfs", float("-inf")))
        audio["TRUE_PEAK_DBTP"] = format_db(analysis.get("true_peak_dbtp", float("-inf")))
    else:
        audio["SAMPLE_PEAK_DBFS"] = format_db(scan["sample_peak_dbfs"])
    if capture_audit:
        provider = str(capture_audit.get("provider") or "spotify").title()
        audio["PROVIDER"] = provider
        audio["CAPTURE_ASSURANCE"] = str(
            capture_audit.get("assurance_label") or "Unknown"
        )
        audio["LOSSLESS_VERIFIED"] = "No - source bits were not available for comparison"
    if process.get("export_role") in {"dj", "library"}:
        audio["DERIVATION_NOTICE"] = (
            "Lossless codec; DJ compatibility copy; source bit identity not implied"
        )
    audio.clear_pictures()
    if artwork_bytes:
        picture = Picture()
        picture.type = 3
        picture.mime = _image_mime(artwork_bytes)
        picture.desc = "Front Cover"
        picture.data = artwork_bytes
        audio.add_picture(picture)
    audio.save()


def _verify_flac(
    flac_path,
    expected,
    expect_artwork,
    source_wav_path=None,
    dither=DITHER_NONE,
    reference_gain=1.0,
    export_role="archive",
):
    info = sf.info(flac_path)
    if info.format != "FLAC" or info.subtype != FLAC_SUBTYPE:
        raise RuntimeError(
            f"FLAC形式検証に失敗しました: {info.format}/{info.subtype}"
        )
    if (
        int(info.samplerate) != expected["sample_rate"]
        or int(info.channels) != expected["channels"]
        or int(info.frames) != expected["frames"]
    ):
        raise RuntimeError("FLACのレート、チャンネル数、またはフレーム数がWAVと一致しません")
    source_file = sf.SoundFile(source_wav_path, mode="r") if source_wav_path else None
    try:
        with sf.SoundFile(flac_path, mode="r") as audio_file:
            while True:
                chunk = audio_file.read(
                    frames=ANALYSIS_CHUNK_FRAMES,
                    dtype="float64",
                    always_2d=True,
                )
                if not len(chunk):
                    break
                if not np.isfinite(chunk).all():
                    raise RuntimeError("FLAC再読込時にNaNまたはInfを検出しました")
                if source_file is not None:
                    source_chunk = source_file.read(
                        frames=len(chunk),
                        dtype="float64",
                        always_2d=True,
                    )
                    if len(source_chunk) != len(chunk):
                        raise RuntimeError("FLACとWAVの比較フレーム数が一致しません")
                    reference = source_chunk * float(reference_gain)
                    max_error = float(np.max(np.abs(reference - chunk)))
                    error_limit = (1.500001 if dither == DITHER_TPDF else 0.500001) / PCM24_SCALE
                    if max_error > error_limit:
                        raise RuntimeError(
                            f"FLAC量子化誤差が24-bit許容値を超えています: {max_error:.9g}"
                        )
    finally:
        if source_file is not None:
            source_file.close()
    metadata = FLAC(flac_path)
    if metadata.get("DITHER") != [dither]:
        raise RuntimeError("FLACのディザ証跡を確認できません")
    if metadata.get("EXPORT_ROLE") != [export_role]:
        raise RuntimeError("FLACの出力種別を確認できません")
    if expect_artwork and not metadata.pictures:
        raise RuntimeError("FLACのジャケット画像を確認できません")


def write_pcm24_flac(
    wav_path,
    flac_path,
    track_info=None,
    artwork_bytes=None,
    capture_audit=None,
    processing=None,
    dither=DITHER_NONE,
    gain_db=0.0,
    random_seed=None,
):
    source_path = os.path.abspath(os.path.expanduser(wav_path))
    output_path = os.path.abspath(os.path.expanduser(flac_path))
    scan = _scan_wav_for_flac(source_path)
    metadata = track_info or _wav_export_metadata(source_path)
    cover = artwork_bytes
    if cover is None and metadata.get("artwork_bytes"):
        cover = metadata["artwork_bytes"]
    temporary_path = f"{output_path}.partial"
    rng = np.random.default_rng(random_seed)
    gain = float(10.0 ** (float(gain_db) / 20.0))
    process = dict(processing or {})
    process["dither"] = dither
    process["quantizer"] = QUANTIZER
    process["safety_gain_db"] = float(gain_db)
    try:
        with sf.SoundFile(source_path, mode="r") as source, sf.SoundFile(
            temporary_path,
            mode="w",
            samplerate=scan["sample_rate"],
            channels=scan["channels"],
            format="FLAC",
            subtype=FLAC_SUBTYPE,
        ) as output:
            while True:
                chunk = source.read(
                    frames=ANALYSIS_CHUNK_FRAMES,
                    dtype="float64",
                    always_2d=True,
                )
                if not len(chunk):
                    break
                output.write(_quantize_pcm24(chunk, dither, rng, gain=gain))
        output_analysis = _analyze_audio_file(temporary_path)
        _tag_flac(
            temporary_path,
            metadata,
            cover,
            {**scan, "sample_rate": int(sf.info(temporary_path).samplerate)},
            analysis=output_analysis,
            capture_audit=capture_audit,
            processing=process,
        )
        _verify_flac(
            temporary_path,
            scan,
            bool(cover),
            source_wav_path=source_path,
            dither=dither,
            reference_gain=gain,
            export_role=str(process.get("export_role") or "archive"),
        )
        with open(temporary_path, "rb") as completed_file:
            os.fsync(completed_file.fileno())
        os.replace(temporary_path, output_path)
        final_info = sf.info(output_path)
        if final_info.format != "FLAC" or final_info.subtype != FLAC_SUBTYPE:
            raise RuntimeError("確定後のFLAC形式を確認できません")
    except Exception:
        try:
            os.remove(temporary_path)
        except FileNotFoundError:
            pass
        raise
    return {
        **scan,
        "flac_path": output_path,
        "artwork_embedded": bool(cover),
        "source_bytes": os.path.getsize(source_path),
        "flac_bytes": os.path.getsize(output_path),
        "dither": dither,
        "dither_reason": process.get("dither_reason") or "",
        "safety_gain_db": float(gain_db),
        "output_analysis": output_analysis,
    }


def write_tpdf_flac(
    wav_path,
    flac_path,
    track_info=None,
    artwork_bytes=None,
    analysis=None,
    capture_audit=None,
    random_seed=None,
):
    """Compatibility helper. New exports use write_pcm24_flac and policy metadata."""
    del analysis
    return write_pcm24_flac(
        wav_path,
        flac_path,
        track_info=track_info,
        artwork_bytes=artwork_bytes,
        capture_audit=capture_audit,
        processing={
            "export_role": "archive",
            "dither_reason": "Explicit compatibility TPDF export",
        },
        dither=DITHER_TPDF,
        random_seed=random_seed,
    )


def _resolve_source_policy(
    capture_audit=None,
    source_bit_depth=None,
    source_verified=None,
):
    evaluation = (capture_audit or {}).get("source_evaluation") or {}
    depth = source_bit_depth
    if depth is None:
        depth = evaluation.get("source_bit_depth")
    depth = int(depth) if depth in (16, 24, "16", "24") else None
    verified = source_verified
    if verified is None:
        verified = bool(evaluation.get("source_verified", False))
    return depth, bool(verified and depth in {16, 24})


def _paired_export_paths(source_path, existing=None, archive_target=None):
    rows = existing or {}
    archive_existing = rows.get("archive", {}).get("flac_path")
    dj_existing = rows.get("dj", {}).get("flac_path")
    if archive_target:
        archive_path = os.path.abspath(os.path.expanduser(archive_target))
    elif archive_existing:
        archive_path = archive_existing
    else:
        archive_path = None
    source_dir = os.path.dirname(source_path)
    dj_dir = os.path.join(source_dir, "DJ 24-48")
    os.makedirs(dj_dir, exist_ok=True)
    base = os.path.splitext(os.path.basename(source_path))[0]
    if archive_path is None and dj_existing:
        archive_path = os.path.join(source_dir, os.path.basename(dj_existing))
    if archive_path is None:
        counter = 1
        while True:
            suffix = "" if counter == 1 else f" ({counter})"
            candidate_archive = os.path.join(source_dir, f"{base}{suffix}.flac")
            candidate_dj = os.path.join(dj_dir, f"{base}{suffix}.flac")
            if not os.path.exists(candidate_archive) and not os.path.exists(candidate_dj):
                archive_path = candidate_archive
                break
            counter += 1
    dj_path = dj_existing or os.path.join(dj_dir, os.path.basename(archive_path))
    return {"archive": archive_path, "dj": dj_path}


def auto_export_variants(
    wav_path,
    track_info=None,
    artwork_bytes=None,
    analysis=None,
    capture_audit=None,
    catalog_path=None,
    log_callback=None,
    archive_target_path=None,
    source_bit_depth=None,
    source_verified=None,
    random_seed=None,
):
    source_path = os.path.abspath(os.path.expanduser(wav_path))
    if not os.path.isfile(source_path):
        raise FileNotFoundError(source_path)
    source_info = sf.info(source_path)
    source_rate = int(source_info.samplerate)
    source_depth, depth_verified = _resolve_source_policy(
        capture_audit,
        source_bit_depth=source_bit_depth,
        source_verified=source_verified,
    )
    existing_rows = (
        list_audio_exports_for_source(source_path, database_path=catalog_path)
        if catalog_path
        else []
    )
    existing = {row["export_role"]: row for row in existing_rows}
    output_paths = _paired_export_paths(
        source_path,
        existing=existing,
        archive_target=archive_target_path,
    )
    source_bytes = os.path.getsize(source_path) if os.path.isfile(source_path) else None
    source_analysis = analysis or _analyze_audio_file(source_path)
    catalog_tracking_failed = False

    def persist_status(role, status, process, result=None, reason=""):
        nonlocal catalog_tracking_failed
        if not catalog_path:
            return True
        output_analysis = (result or {}).get("output_analysis") or {}
        try:
            record_audio_export(
                source_path,
                output_paths[role],
                status,
                export_role=role,
                reason=reason,
                source_sample_rate=source_rate,
                source_bit_depth=source_depth,
                source_verified=depth_verified,
                output_sample_rate=int(process["output_sample_rate"]),
                src_engine=process.get("src_engine"),
                src_quality=process.get("src_quality"),
                src_phase=process.get("src_phase"),
                dither=process["dither"],
                dither_reason=process["dither_reason"],
                quantizer=QUANTIZER,
                safety_gain_db=process.get("safety_gain_db", 0.0),
                input_true_peak_dbtp=process.get("input_true_peak_dbtp"),
                output_true_peak_dbtp=output_analysis.get("true_peak_dbtp"),
                sample_peak_dbfs=output_analysis.get(
                    "sample_peak_dbfs", source_analysis.get("sample_peak_dbfs")
                ),
                artwork_embedded=(result or {}).get("artwork_embedded"),
                source_bytes=source_bytes,
                flac_bytes=(result or {}).get("flac_bytes"),
                wav_deleted=(result or {}).get("wav_deleted", False),
                database_path=catalog_path,
            )
            return True
        except Exception as exc:
            catalog_tracking_failed = True
            if log_callback:
                log_callback(
                    f"FLAC履歴更新失敗: {os.path.basename(source_path)} / {role} / {exc}"
                )
            return False

    archive_process = {
        "export_role": "archive",
        "source_sample_rate": source_rate,
        "source_bit_depth": source_depth,
        "source_verified": depth_verified,
        "output_sample_rate": source_rate,
        "src_engine": None,
        "src_quality": None,
        "src_phase": None,
        "dither": DITHER_NONE,
        "dither_reason": "ネイティブレートのアーカイブ: DSPなし・ディザなし",
        "safety_gain_db": 0.0,
        "input_true_peak_dbtp": source_analysis.get("true_peak_dbtp"),
    }
    results = {}

    archive_existing = existing.get("archive")
    if (
        archive_existing
        and str(archive_existing.get("status", "")).startswith("complete")
        and archive_existing.get("flac_path")
        and os.path.isfile(archive_existing["flac_path"])
    ):
        results["archive"] = {
            **archive_existing,
            "status": "complete_wav_retained",
            "flac_path": archive_existing["flac_path"],
            "artwork_embedded": bool(archive_existing.get("artwork_embedded")),
            "output_analysis": {
                "true_peak_dbtp": archive_existing.get("output_true_peak_dbtp"),
                "sample_peak_dbfs": archive_existing.get("sample_peak_dbfs"),
            },
        }
    else:
        persist_status("archive", "converting", archive_process)
        try:
            results["archive"] = write_pcm24_flac(
                source_path,
                output_paths["archive"],
                track_info=track_info,
                artwork_bytes=artwork_bytes,
                capture_audit=capture_audit,
                processing=archive_process,
                dither=DITHER_NONE,
                random_seed=random_seed,
            )
            results["archive"].update(
                status="complete_wav_retained",
                reason="DJ版の検証完了までWAVを保持します",
                flac_path=output_paths["archive"],
            )
            persist_status(
                "archive",
                "complete_wav_retained",
                archive_process,
                results["archive"],
                results["archive"]["reason"],
            )
        except FlacExportRejected as exc:
            results["archive"] = {
                "status": "rejected",
                "reason": str(exc),
                "flac_path": None,
            }
            persist_status("archive", "rejected", archive_process, reason=str(exc))
        except Exception as exc:
            results["archive"] = {
                "status": "failed",
                "reason": str(exc),
                "flac_path": None,
            }
            persist_status("archive", "failed", archive_process, reason=str(exc))

    temporary_resample = None
    dj_process = {
        "export_role": "dj",
        "source_sample_rate": source_rate,
        "source_bit_depth": source_depth,
        "source_verified": depth_verified,
        "output_sample_rate": DJ_SAMPLE_RATE,
        "src_engine": SRC_ENGINE if source_rate != DJ_SAMPLE_RATE else None,
        "src_quality": SRC_QUALITY if source_rate != DJ_SAMPLE_RATE else None,
        "src_phase": SRC_PHASE if source_rate != DJ_SAMPLE_RATE else None,
        "dither": DITHER_NONE,
        "dither_reason": "DJ版の前処理が完了していません",
        "safety_gain_db": 0.0,
        "input_true_peak_dbtp": None,
    }
    try:
        if source_rate == DJ_SAMPLE_RATE:
            dj_reference = source_path
            src_metadata = {}
        else:
            handle = tempfile.NamedTemporaryFile(
                prefix=".hires-dj-",
                suffix=".wav",
                dir=os.path.dirname(source_path),
                delete=False,
            )
            temporary_resample = handle.name
            handle.close()
            src_metadata = _resample_to_float64_wav(
                source_path,
                temporary_resample,
                output_rate=DJ_SAMPLE_RATE,
            )
            dj_reference = temporary_resample
        dj_input_analysis = _analyze_audio_file(dj_reference)
        input_true_peak = dj_input_analysis.get("true_peak_dbtp", float("-inf"))
        safety_gain_db = (
            min(0.0, -1.0 - float(input_true_peak))
            if np.isfinite(input_true_peak)
            else 0.0
        )
        dsp_applied = source_rate != DJ_SAMPLE_RATE or safety_gain_db < 0.0
        use_tpdf = depth_verified and source_depth == 24 and dsp_applied
        dither = DITHER_TPDF if use_tpdf else DITHER_NONE
        if source_depth == 16:
            dither_reason = "検証済み16-bitソース: 拡張・DSP後もディザ禁止"
        elif not depth_verified:
            dither_reason = "ソースbit深度未検証: 保守的な無ディザ方針"
        elif not dsp_applied:
            dither_reason = "検証済み24-bitソース: SRC・Gainなしのためディザ不要"
        else:
            dither_reason = "検証済み24-bitソース: DSP後の最終量子化でTPDFを1回適用"
        dj_process = {
            "export_role": "dj",
            "source_sample_rate": source_rate,
            "source_bit_depth": source_depth,
            "source_verified": depth_verified,
            "output_sample_rate": DJ_SAMPLE_RATE,
            "src_engine": src_metadata.get("src_engine"),
            "src_quality": src_metadata.get("src_quality"),
            "src_phase": src_metadata.get("src_phase"),
            "soxr_version": src_metadata.get("soxr_version"),
            "dither": dither,
            "dither_reason": dither_reason,
            "safety_gain_db": safety_gain_db,
            "input_true_peak_dbtp": input_true_peak,
        }
        dj_existing = existing.get("dj")
        if (
            dj_existing
            and str(dj_existing.get("status", "")).startswith("complete")
            and dj_existing.get("flac_path")
            and os.path.isfile(dj_existing["flac_path"])
        ):
            results["dj"] = {
                **dj_existing,
                "status": "complete_wav_retained",
                "flac_path": dj_existing["flac_path"],
                "artwork_embedded": bool(dj_existing.get("artwork_embedded")),
                "output_analysis": {
                    "true_peak_dbtp": dj_existing.get("output_true_peak_dbtp"),
                    "sample_peak_dbfs": dj_existing.get("sample_peak_dbfs"),
                },
            }
        else:
            persist_status("dj", "converting", dj_process)
            try:
                results["dj"] = write_pcm24_flac(
                    dj_reference,
                    output_paths["dj"],
                    track_info=track_info,
                    artwork_bytes=artwork_bytes,
                    capture_audit=capture_audit,
                    processing=dj_process,
                    dither=dither,
                    gain_db=safety_gain_db,
                    random_seed=random_seed,
                )
                results["dj"].update(
                    status="complete_wav_retained",
                    reason="アーカイブ版の検証完了までWAVを保持します",
                    flac_path=output_paths["dj"],
                )
                persist_status(
                    "dj",
                    "complete_wav_retained",
                    dj_process,
                    results["dj"],
                    results["dj"]["reason"],
                )
            except FlacExportRejected as exc:
                results["dj"] = {
                    "status": "rejected",
                    "reason": str(exc),
                    "flac_path": None,
                }
                persist_status("dj", "rejected", dj_process, reason=str(exc))
            except Exception as exc:
                results["dj"] = {
                    "status": "failed",
                    "reason": str(exc),
                    "flac_path": None,
                }
                persist_status("dj", "failed", dj_process, reason=str(exc))
    except FlacExportRejected as exc:
        results["dj"] = {
            "status": "rejected",
            "reason": str(exc),
            "flac_path": None,
        }
        persist_status("dj", "rejected", dj_process, reason=str(exc))
    except Exception as exc:
        results["dj"] = {
            "status": "failed",
            "reason": str(exc),
            "flac_path": None,
        }
        persist_status("dj", "failed", dj_process, reason=str(exc))
    finally:
        if temporary_resample:
            try:
                os.remove(temporary_resample)
            except FileNotFoundError:
                pass

    completed = all(
        results.get(role, {}).get("status", "").startswith("complete")
        and results[role].get("flac_path")
        and os.path.isfile(results[role]["flac_path"])
        for role in ("archive", "dj")
    )
    tracking_ok = completed and not catalog_tracking_failed
    tracking_error = None
    if completed and catalog_path:
        try:
            replace_recording_file(
                source_path,
                results["archive"]["flac_path"],
                database_path=catalog_path,
            )
        except Exception as exc:
            tracking_ok = False
            tracking_error = exc
    if tracking_ok:
        try:
            os.remove(source_path)
            wav_deleted = True
            final_status = "complete"
            final_reason = ""
        except OSError as exc:
            wav_deleted = False
            final_status = "complete_wav_retained"
            final_reason = f"両FLACは検証済みですがWAVを削除できません: {exc}"
    else:
        wav_deleted = False
        final_status = "complete_wav_retained" if completed else "partial"
        if completed and catalog_tracking_failed:
            final_reason = "両FLACは検証済みですが履歴更新に失敗したためWAVを保持しました"
        else:
            final_reason = "アーカイブ版とDJ版の両方が完了していないためWAVを保持しました"
        if tracking_error is not None:
            final_reason = f"履歴を確定できないためWAVを保持しました: {tracking_error}"

    for role, process in (("archive", archive_process), ("dj", dj_process)):
        result = results.get(role, {})
        if result.get("status", "").startswith("complete"):
            role_status = final_status if completed else "complete_wav_retained"
            result["status"] = role_status
            result["reason"] = final_reason
            result["wav_deleted"] = wav_deleted
            persist_status(role, role_status, process, result, final_reason)
    if log_callback:
        deletion_text = "WAV削除済み" if wav_deleted else "WAV保持"
        for role, label in (("archive", "アーカイブ"), ("dj", "DJ 24/48")):
            item = results.get(role, {})
            if item.get("status", "").startswith("complete"):
                process = archive_process if role == "archive" else dj_process
                log_callback(
                    f"{label} FLAC保存: {os.path.basename(item['flac_path'])} / "
                    f"24-bit / {process['dither']} / {deletion_text}"
                )
            else:
                log_callback(
                    f"{label} FLAC変換{item.get('status', 'failed')}: {item.get('reason', '不明なエラー')}"
                )
        if final_reason:
            log_callback(f"FLAC管理警告: {final_reason}")
    return {
        "archive": results.get("archive"),
        "dj": results.get("dj"),
        "status": final_status,
        "reason": final_reason,
        "wav_deleted": wav_deleted,
    }


def auto_export_flac(
    wav_path,
    track_info=None,
    artwork_bytes=None,
    analysis=None,
    capture_audit=None,
    catalog_path=None,
    log_callback=None,
    target_path=None,
    random_seed=None,
):
    """Compatibility API returning the archive result with variant details."""
    variants = auto_export_variants(
        wav_path,
        track_info=track_info,
        artwork_bytes=artwork_bytes,
        analysis=analysis,
        capture_audit=capture_audit,
        catalog_path=catalog_path,
        log_callback=log_callback,
        archive_target_path=target_path,
        random_seed=random_seed,
    )
    archive = dict(variants.get("archive") or {})
    archive.update(
        status=variants["status"],
        reason=variants["reason"],
        wav_deleted=variants["wav_deleted"],
        variants=variants,
    )
    return archive


def retry_flac_export(export_item, catalog_path, log_callback=None):
    source_path = export_item["source_wav_path"]
    if not os.path.isfile(source_path):
        reason = "変換元WAVが見つかりません"
        record_audio_export(
            source_path,
            export_item.get("flac_path"),
            "failed",
            export_role=export_item.get("export_role") or "archive",
            reason=reason,
            database_path=catalog_path,
        )
        return {"status": "failed", "reason": reason, "flac_path": None}
    metadata = _wav_export_metadata(source_path)
    return auto_export_variants(
        source_path,
        track_info={
            "name": metadata["title"],
            "artist": metadata["artist"],
            "album": metadata["album"],
        },
        artwork_bytes=metadata.get("artwork_bytes"),
        catalog_path=catalog_path,
        log_callback=log_callback,
        source_bit_depth=export_item.get("source_bit_depth"),
        source_verified=export_item.get("source_verified"),
    )


def dbfs(amplitude):
    if amplitude <= 0.0:
        return float("-inf")
    return float(20.0 * np.log10(amplitude))


def format_db(value):
    return "-inf" if not np.isfinite(value) else f"{value:.2f}"


def longest_true_run(mask):
    longest = 0
    current = 0
    for value in mask:
        if value:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def true_ranges(mask, limit=20):
    values = np.asarray(mask, dtype=bool)
    if values.size == 0:
        return []
    changes = np.diff(values.astype(np.int8))
    starts = list(np.where(changes == 1)[0] + 1)
    ends = list(np.where(changes == -1)[0] + 1)
    if values[0]:
        starts.insert(0, 0)
    if values[-1]:
        ends.append(len(values))
    return list(zip(starts, ends))[: int(limit)]


def iter_audio_chunks(audio, start=0, end=None, chunk_frames=ANALYSIS_CHUNK_FRAMES):
    stop = len(audio) if end is None else min(len(audio), int(end))
    position = max(0, int(start))
    while position < stop:
        next_position = min(stop, position + int(chunk_frames))
        yield position, np.asarray(audio[position:next_position], dtype=np.float32)
        position = next_position


def _integrated_loudness_chunked(audio, sample_rate):
    if len(audio) < int(0.4 * sample_rate):
        return None
    meter = pyln.Meter(int(sample_rate))
    channels = int(audio.shape[1])
    states = {}
    for name, stage in meter._filters.items():
        state_length = max(len(stage.a), len(stage.b)) - 1
        states[name] = np.zeros((state_length, channels), dtype=np.float64)

    block_frames = int(meter.block_size * sample_rate)
    hop_frames = int(block_frames * (1.0 - meter.overlap))
    pending = np.empty((0, channels), dtype=np.float64)
    energies = []
    for _position, source in iter_audio_chunks(audio):
        filtered = source.astype(np.float64, copy=True)
        for name, stage in meter._filters.items():
            next_filtered = np.empty_like(filtered)
            for channel in range(channels):
                next_filtered[:, channel], states[name][:, channel] = lfilter(
                    stage.b,
                    stage.a,
                    filtered[:, channel],
                    zi=states[name][:, channel],
                )
            filtered = next_filtered
        pending = np.concatenate((pending, filtered), axis=0)
        while len(pending) >= block_frames:
            block = pending[:block_frames]
            energies.append(np.mean(np.square(block), axis=0))
            pending = pending[hop_frames:]

    if not energies:
        return None
    values = np.asarray(energies, dtype=np.float64)
    gains = np.asarray([1.0, 1.0, 1.0, 1.41, 1.41][:channels])
    weighted_power = values @ gains
    with np.errstate(divide="ignore"):
        loudness = -0.691 + 10.0 * np.log10(weighted_power)
    absolute = loudness >= -70.0
    if not np.any(absolute):
        return None
    absolute_mean = np.mean(values[absolute], axis=0)
    with np.errstate(divide="ignore"):
        relative_gate = -0.691 + 10.0 * np.log10(np.sum(gains * absolute_mean)) - 10.0
    gated = (loudness > -70.0) & (loudness > relative_gate)
    if not np.any(gated):
        return None
    final_power = float(np.sum(gains * np.mean(values[gated], axis=0)))
    measured = dbfs(np.sqrt(final_power)) - 0.691
    return measured if np.isfinite(measured) else None


def _true_peak_chunked(audio, sample_rate):
    best_peak = 0.0
    best_time = 0.0
    total = len(audio)
    for start in range(0, total, ANALYSIS_CHUNK_FRAMES):
        end = min(total, start + ANALYSIS_CHUNK_FRAMES)
        context_start = max(0, start - TRUE_PEAK_OVERLAP_FRAMES)
        context_end = min(total, end + TRUE_PEAK_OVERLAP_FRAMES)
        context = np.asarray(audio[context_start:context_end], dtype=np.float64)
        oversampled = resample_poly(context, TRUE_PEAK_OVERSAMPLE, 1, axis=0)
        local_start = (start - context_start) * TRUE_PEAK_OVERSAMPLE
        local_end = local_start + (end - start) * TRUE_PEAK_OVERSAMPLE
        central = np.abs(oversampled[local_start:local_end])
        if central.size == 0:
            continue
        flat_index = int(np.argmax(central))
        peak = float(central.flat[flat_index])
        if peak > best_peak:
            frame_index = flat_index // central.shape[1]
            best_peak = peak
            best_time = (start * TRUE_PEAK_OVERSAMPLE + frame_index) / (
                sample_rate * TRUE_PEAK_OVERSAMPLE
            )
    return best_peak, best_time


def analyze_audio(audio, sample_rate):
    shape = getattr(audio, "shape", None)
    if shape is None:
        audio = np.asarray(audio, dtype=np.float32)
        shape = audio.shape
    if len(shape) == 1:
        audio = np.asarray(audio, dtype=np.float32)[:, np.newaxis]
        shape = audio.shape
    if len(shape) != 2 or len(audio) == 0:
        raise ValueError("音声データが空、または不正な形状です")
    channels = int(shape[1])
    sample_peak = 0.0
    sample_peak_frame = 0
    full_scale_sample_count = 0
    full_scale_frame_count = 0
    longest_full_scale_run = 0
    current_run = 0
    range_start = None
    full_scale_ranges = []
    for position, chunk in iter_audio_chunks(audio):
        if not np.isfinite(chunk).all():
            raise ValueError("音声データにNaNまたはInfが含まれています")
        absolute = np.abs(chunk)
        local_peak_index = int(np.argmax(absolute))
        local_peak = float(absolute.flat[local_peak_index])
        if local_peak > sample_peak:
            sample_peak = local_peak
            sample_peak_frame = position + local_peak_index // channels
        full_mask = absolute >= 1.0
        frame_mask = np.any(full_mask, axis=1)
        full_scale_sample_count += int(np.count_nonzero(full_mask))
        full_scale_frame_count += int(np.count_nonzero(frame_mask))
        for offset, active in enumerate(frame_mask):
            frame = position + offset
            if active:
                if range_start is None:
                    range_start = frame
                current_run += 1
                longest_full_scale_run = max(longest_full_scale_run, current_run)
            else:
                if range_start is not None and len(full_scale_ranges) < 20:
                    full_scale_ranges.append((range_start, frame))
                range_start = None
                current_run = 0
    if range_start is not None and len(full_scale_ranges) < 20:
        full_scale_ranges.append((range_start, len(audio)))

    true_peak, true_peak_time = _true_peak_chunked(audio, int(sample_rate))
    try:
        integrated_lufs = _integrated_loudness_chunked(audio, int(sample_rate))
    except (ValueError, ZeroDivisionError):
        integrated_lufs = None

    sample_peak_dbfs = dbfs(sample_peak)
    true_peak_dbtp = dbfs(true_peak)
    warnings = []
    if sample_peak > 1.0:
        warnings.append("入力値が0 dBFSを超えています")
    elif full_scale_sample_count:
        warnings.append("0 dBFS到達サンプルがあります")
    if true_peak_dbtp > 0.0:
        warnings.append("True Peakが0 dBTPを超えています")

    return {
        "sample_rate": int(sample_rate),
        "channels": channels,
        "integrated_lufs": integrated_lufs,
        "sample_peak": sample_peak,
        "sample_peak_dbfs": sample_peak_dbfs,
        "sample_peak_frame": sample_peak_frame,
        "sample_peak_time_sec": sample_peak_frame / sample_rate,
        "true_peak": true_peak,
        "true_peak_dbtp": true_peak_dbtp,
        "true_peak_time_sec": true_peak_time,
        "headroom_db": -sample_peak_dbfs,
        "full_scale_sample_count": full_scale_sample_count,
        "full_scale_frame_count": full_scale_frame_count,
        "longest_full_scale_run": longest_full_scale_run,
        "full_scale_ranges": [
            {
                "start_frame": int(start),
                "end_frame": int(end),
                "start_sec": start / sample_rate,
                "end_sec": end / sample_rate,
            }
            for start, end in full_scale_ranges
        ],
        "warnings": warnings,
    }


def format_analysis(analysis):
    lufs = analysis["integrated_lufs"]
    lufs_text = "測定不能" if lufs is None else f"{lufs:.2f} LUFS"
    return (
        f"{lufs_text} / Peak {format_db(analysis['sample_peak_dbfs'])} dBFS / "
        f"True Peak {format_db(analysis['true_peak_dbtp'])} dBTP / "
        f"Full-scale {analysis['full_scale_sample_count']} samples"
    )


def format_analysis_suspect_locations(analysis, limit=8):
    locations = []
    for item in analysis.get("full_scale_ranges", [])[: int(limit)]:
        start = format_timecode(item["start_sec"])
        end = format_timecode(item["end_sec"])
        if item["end_sec"] - item["start_sec"] <= 0.01:
            locations.append(f"{start}: 0 dBFS到達")
        else:
            locations.append(f"{start}-{end}: 連続フルスケール")
    if analysis.get("true_peak_dbtp", float("-inf")) > 0.0:
        locations.append(
            f"{format_timecode(analysis.get('true_peak_time_sec', 0.0))}: "
            f"True Peak {format_db(analysis['true_peak_dbtp'])} dBTP"
        )
    return locations[: int(limit)]

def safe_filename(name):
    cleaned = "".join(ch for ch in name if ch.isalnum() or ch in " -_().[]")
    cleaned = " ".join(cleaned.split())
    return cleaned[:180] if cleaned else datetime.now().strftime("Recording_%H%M%S")

def unique_path(folder, filename):
    base, ext = os.path.splitext(filename)
    path = os.path.join(folder, filename)
    counter = 2
    while os.path.exists(path):
        path = os.path.join(folder, f"{base} ({counter}){ext}")
        counter += 1
    return path

def trim_silence(audio, sample_rate, threshold_db, pad_start_sec, pad_end_sec):
    if len(audio) == 0:
        return audio, 0, 0
    threshold = 10 ** (threshold_db / 20.0)
    first_active = None
    last_active = None
    for position, chunk in iter_audio_chunks(audio):
        active = np.where(np.max(np.abs(chunk), axis=1) > threshold)[0]
        if len(active):
            if first_active is None:
                first_active = position + int(active[0])
            last_active = position + int(active[-1])
    if first_active is None or last_active is None:
        return audio, 0, len(audio)

    pad_start = int(pad_start_sec * sample_rate)
    pad_end = int(pad_end_sec * sample_rate)
    start = max(0, first_active - pad_start)
    end = min(len(audio), last_active + pad_end + 1)
    return audio[start:end], start, end

def find_silence_split(audio, approx_sample, sample_rate, threshold_db, log_callback, track_name):
    threshold = 10 ** (threshold_db / 20.0)
    search_start = max(0, approx_sample - int(4.0 * sample_rate))
    search_end = min(len(audio), approx_sample + int(0.7 * sample_rate))
    window = audio[search_start:search_end]
    if len(window) == 0:
        return max(0, approx_sample - int(1.2 * sample_rate))

    amplitude = np.max(np.abs(window), axis=1)
    silence = amplitude <= threshold
    diff = np.diff(silence.astype(np.int8))
    starts = np.where(diff == 1)[0] + 1
    ends = np.where(diff == -1)[0] + 1

    if silence[0]:
        starts = np.insert(starts, 0, 0)
    if silence[-1]:
        ends = np.append(ends, len(silence))

    min_silence_samples = int(0.05 * sample_rate)
    candidates = [
        (start, end, end - start)
        for start, end in zip(starts, ends)
        if end - start >= min_silence_samples
    ]
    if candidates:
        best_start, best_end, _ = max(candidates, key=lambda item: item[2])
        split = search_start + (best_start + best_end) // 2
        if log_callback:
            delta = (approx_sample - split) / sample_rate
            log_callback(f"境界補正: {track_name} / {delta:+.2f}s")
        return split

    split = max(0, approx_sample - int(1.2 * sample_rate))
    if log_callback:
        log_callback(f"無音境界なし: {track_name} / 推定 -1.20s")
    return split

def build_split_points(audio, history, sample_rate, threshold_db, log_callback):
    split_points = [0]
    for track in history[1:]:
        approx = int(track["start_sample"])
        split_points.append(
            find_silence_split(audio, approx, sample_rate, threshold_db, log_callback, track.get("name", "Unknown"))
        )
    split_points.append(len(audio))

    cleaned = [0]
    for point in split_points[1:]:
        point = min(max(int(point), cleaned[-1]), len(audio))
        cleaned.append(point)
    return cleaned

def prepare_track_candidates(audio, history, options, stop_info, log_callback):
    # Evaluates candidates and returns a list of dictionaries with info ready for Review UI
    sample_rate = options["sample_rate"]
    threshold_db = options["threshold_db"]
    pad_start = options["pad_start_sec"]
    pad_end = options["pad_end_sec"]
    min_keep_sec = options["min_keep_sec"]
    discard_tail = options["discard_tail"]
    discard_tail_under_sec = options["discard_tail_under_sec"]
    record_mode = options.get("record_mode", MODE_ALBUM)

    history = [dict(track) for track in history if int(track.get("start_sample", -1)) < len(audio)]
    if not history:
        history = [
            {
                "name": datetime.now().strftime("Recording_%H%M%S"),
                "artist": "Unknown",
                "album": "Captured",
                "start_sample": 0,
                "key": None,
                "artwork_url": None
            }
        ]
        
    # Mode overrides
    if record_mode == MODE_MANUAL:
        # One big file, no splits based on history
        history = [history[0]]
        min_keep_sec = 0.0
        discard_tail = False
        
    elif record_mode == MODE_SINGLE:
        # Just use the very first track, ignore the rest
        history = [history[0]]

    history.sort(key=lambda item: int(item["start_sample"]))
    split_points = build_split_points(audio, history, sample_rate, threshold_db, log_callback)
    stop_key = normalized_track_key(stop_info)
    
    candidates = []
    
    for index, track in enumerate(history):
        start = split_points[index]
        end = split_points[index + 1]
        segment = audio[start:end]
        duration = len(segment) / sample_rate
        is_last = index == len(history) - 1
        segment_key = track.get("key")

        # Skip logic
        default_checked = True
        reason = ""
        
        if duration < min_keep_sec:
            default_checked = False
            reason = f"Short ({duration:.1f}s < {min_keep_sec}s)"
        elif (
            discard_tail
            and is_last
            and stop_key is not None
            and stop_key == segment_key
            and duration <= discard_tail_under_sec
        ):
            default_checked = False
            reason = "Final tail fragment"

        # Silence trimming pre-check
        trimmed, trim_start, trim_end = trim_silence(
            segment,
            sample_rate,
            threshold_db,
            pad_start,
            pad_end,
        )
        if len(trimmed) == 0:
            default_checked = False
            reason = "All silence"

        analysis = None
        if len(trimmed) > 0:
            analysis = analyze_audio(trimmed, sample_rate)

        candidates.append({
            "track": track,
            "start": start,
            "end": end,
            "trim_start": trim_start,
            "trim_end": trim_end,
            "duration": len(trimmed) / sample_rate if len(trimmed) > 0 else 0,
            "default_checked": default_checked,
            "reason": reason,
            "segment_audio": segment,
            "analysis": analysis,
        })
        
    return candidates

def process_and_save_candidates(candidates, options, log_callback, on_finish):
    save_dir = options["save_dir"]
    sample_rate = options["sample_rate"]
    capture_audit = options.get("capture_audit")
    catalog_path = options.get("catalog_path")
    os.makedirs(save_dir, exist_ok=True)

    saved = 0
    failed = 0
    try:
        for cand in candidates:
            if not cand.get("selected", False):
                continue

            track = cand["track"]
            segment = cand["segment_audio"]
            trim_start = cand["trim_start"]
            trim_end = cand["trim_end"]
            trimmed = segment[trim_start:trim_end]
            if len(trimmed) == 0:
                continue

            try:
                analysis = cand.get("analysis") or analyze_audio(trimmed, sample_rate)
                candidate_start = int(cand.get("start", 0))
                file_audit = audit_for_audio_range(
                    capture_audit,
                    candidate_start + int(trim_start),
                    candidate_start + int(trim_end),
                )
                filename = (
                    safe_filename(
                        f"{track.get('artist', 'Unknown')} - {track.get('name', 'Untitled')}"
                    )
                    + ".wav"
                )
                final_path = unique_path(save_dir, filename)

                data_bytes = len(trimmed) * int(trimmed.shape[1]) * 4
                container = "RF64" if data_bytes >= RF64_DATA_THRESHOLD else "WAV"
                with sf.SoundFile(
                    final_path,
                    mode="w",
                    samplerate=int(sample_rate),
                    channels=int(trimmed.shape[1]),
                    format=container,
                    subtype=WAV_SUBTYPE,
                ) as output:
                    for _position, chunk in iter_audio_chunks(trimmed):
                        output.write(chunk)

                artwork_bytes = None
                if track.get("artwork_url"):
                    artwork_bytes = download_url_bytes(track["artwork_url"])
                if container == "WAV":
                    tag_wav(final_path, track, artwork_bytes, analysis, file_audit)
                else:
                    log_callback(
                        f"RF64タグ制限: {os.path.basename(final_path)} / メタデータは録音履歴DBを正本にします"
                    )

                if catalog_path:
                    try:
                        catalog_analysis = dict(analysis)
                        catalog_analysis["duration_sec"] = len(trimmed) / sample_rate
                        record_saved_recording(
                            final_path,
                            track,
                            catalog_analysis,
                            file_audit,
                            catalog_path,
                        )
                    except Exception as exc:
                        log_callback(
                            f"録音履歴エラー: {os.path.basename(final_path)} / {exc}"
                        )

                log_callback(
                    f"原本WAV作成: {os.path.basename(final_path)} / {container} FLOAT / {format_analysis(analysis)}"
                )
                for warning in analysis["warnings"]:
                    log_callback(f"品質警告: {os.path.basename(final_path)} / {warning}")
                if options.get("auto_flac_export", True):
                    auto_export_variants(
                        final_path,
                        track_info=track,
                        artwork_bytes=artwork_bytes,
                        analysis=analysis,
                        capture_audit=file_audit,
                        catalog_path=catalog_path,
                        log_callback=log_callback,
                    )
                saved += 1
            except Exception as exc:
                failed += 1
                log_callback(
                    f"WAV保存エラー: {track.get('artist', 'Unknown')} - "
                    f"{track.get('name', 'Untitled')} / {exc}"
                )
    finally:
        log_callback(f"処理完了: {saved} 件保存 / {failed} 件失敗")
        if on_finish:
            on_finish()
