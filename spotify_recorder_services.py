import os
import subprocess
import urllib.request
from datetime import datetime

import numpy as np
import pyloudnorm as pyln
import soundfile as sf
from scipy.signal import resample_poly

from mutagen.id3 import APIC, TALB, TIT2, TPE1, TXXX
from mutagen.wave import WAVE

from recording_catalog import record_saved_recording
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
    lines.append("✅ Output: WAV / 32-bit IEEE float")
    settings = read_spotify_quality_settings()
    settings_evaluation = evaluate_spotify_quality_settings(settings)
    if settings_evaluation["conditions_pass"]:
        lines.append(
            "✅ Spotify設定証跡: Lossless候補 / 音質自動低下OFF / 音量均一OFF / Automix OFF"
        )
    else:
        for warning in settings_evaluation["warnings"]:
            lines.append(f"⚠️ Spotify設定証跡: {warning}")
    lines.append("ℹ️ 実効コーデックはSpotify公開APIから取得できないため、Lossless断定はできません")
    lines.append("ℹ️ Spotify推奨: EQ OFF / Crossfade OFF / 可能ならLosslessダウンロード後にOffline再生")
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
            settings = capture_audit.get("spotify_settings") or {}
            network_test = capture_audit.get("network_test") or {}
            network = capture_audit.get("network_observation") or {}
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
                    text="No - Spotify does not expose the effective playback codec",
                )
            )
            audio.tags.add(
                TXXX(
                    encoding=3,
                    desc="Spotify Quality Setting Raw",
                    text=str(settings.get("streaming_quality_raw", "Unknown")),
                )
            )
            audio.tags.add(
                TXXX(
                    encoding=3,
                    desc="Spotify Auto Downgrade",
                    text=(
                        "Disabled"
                        if settings.get("auto_downgrade") is False
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
            if network_test.get("available"):
                audio.tags.add(
                    TXXX(
                        encoding=3,
                        desc="Network Preflight Mbps",
                        text=f'{network_test.get("download_mbps", 0.0):.2f}',
                    )
                )
            if network.get("sample_count"):
                audio.tags.add(
                    TXXX(
                        encoding=3,
                        desc="Spotify Network Observed Bytes",
                        text=str(network.get("inbound_total_bytes", 0)),
                    )
                )
                audio.tags.add(
                    TXXX(
                        encoding=3,
                        desc="Spotify Network Average kbps",
                        text=f'{network.get("inbound_average_kbps", 0.0):.2f}',
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


def analyze_audio(audio, sample_rate):
    samples = np.asarray(audio, dtype=np.float32)
    if samples.ndim == 1:
        samples = samples[:, np.newaxis]
    if samples.ndim != 2 or len(samples) == 0:
        raise ValueError("音声データが空、または不正な形状です")
    if not np.isfinite(samples).all():
        raise ValueError("音声データにNaNまたはInfが含まれています")

    absolute = np.abs(samples)
    sample_peak = float(np.max(absolute))
    sample_peak_flat_index = int(np.argmax(absolute))
    sample_peak_frame = sample_peak_flat_index // samples.shape[1]
    full_scale_mask = absolute >= 1.0
    full_scale_frames = np.any(full_scale_mask, axis=1)

    oversampled = resample_poly(
        samples.astype(np.float64, copy=False),
        TRUE_PEAK_OVERSAMPLE,
        1,
        axis=0,
    )
    true_peak = float(np.max(np.abs(oversampled)))
    true_peak_flat_index = int(np.argmax(np.abs(oversampled)))
    true_peak_frame = true_peak_flat_index // samples.shape[1]

    integrated_lufs = None
    try:
        measured = float(pyln.Meter(int(sample_rate)).integrated_loudness(samples))
        if np.isfinite(measured):
            integrated_lufs = measured
    except (ValueError, ZeroDivisionError):
        pass

    sample_peak_dbfs = dbfs(sample_peak)
    true_peak_dbtp = dbfs(true_peak)
    warnings = []
    if sample_peak > 1.0:
        warnings.append("入力値が0 dBFSを超えています")
    elif np.any(full_scale_mask):
        warnings.append("0 dBFS到達サンプルがあります")
    if true_peak_dbtp > 0.0:
        warnings.append("True Peakが0 dBTPを超えています")

    return {
        "sample_rate": int(sample_rate),
        "channels": int(samples.shape[1]),
        "integrated_lufs": integrated_lufs,
        "sample_peak": sample_peak,
        "sample_peak_dbfs": sample_peak_dbfs,
        "sample_peak_frame": sample_peak_frame,
        "sample_peak_time_sec": sample_peak_frame / sample_rate,
        "true_peak": true_peak,
        "true_peak_dbtp": true_peak_dbtp,
        "true_peak_time_sec": true_peak_frame / (sample_rate * TRUE_PEAK_OVERSAMPLE),
        "headroom_db": -sample_peak_dbfs,
        "full_scale_sample_count": int(np.count_nonzero(full_scale_mask)),
        "full_scale_frame_count": int(np.count_nonzero(full_scale_frames)),
        "longest_full_scale_run": int(longest_true_run(full_scale_frames)),
        "full_scale_ranges": [
            {
                "start_frame": int(start),
                "end_frame": int(end),
                "start_sec": start / sample_rate,
                "end_sec": end / sample_rate,
            }
            for start, end in true_ranges(full_scale_frames)
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
    amplitude = np.max(np.abs(audio), axis=1)
    active = np.where(amplitude > threshold)[0]
    if len(active) == 0:
        return audio, 0, len(audio)

    pad_start = int(pad_start_sec * sample_rate)
    pad_end = int(pad_end_sec * sample_rate)
    start = max(0, active[0] - pad_start)
    end = min(len(audio), active[-1] + pad_end)
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
            trimmed = np.asarray(segment[trim_start:trim_end], dtype=np.float32)
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

                # Unity Gain: float32 samples are written without scaling, limiting, or clipping.
                sf.write(
                    final_path,
                    trimmed,
                    int(sample_rate),
                    format="WAV",
                    subtype=WAV_SUBTYPE,
                )

                artwork_bytes = None
                if track.get("artwork_url"):
                    artwork_bytes = download_url_bytes(track["artwork_url"])
                tag_wav(final_path, track, artwork_bytes, analysis, file_audit)

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

                log_callback(f"保存: {os.path.basename(final_path)} / {format_analysis(analysis)}")
                for warning in analysis["warnings"]:
                    log_callback(f"品質警告: {os.path.basename(final_path)} / {warning}")
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
