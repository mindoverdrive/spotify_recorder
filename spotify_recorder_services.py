import os
import subprocess
import urllib.request
import wave
from datetime import datetime
import numpy as np

from mutagen.id3 import TIT2, TPE1, TALB, APIC
from mutagen.wave import WAVE
from mutagen.flac import FLAC, Picture
from mutagen.mp4 import MP4, MP4Cover

MODE_ALBUM = "Album (Auto-split)"
MODE_SINGLE = "Single Track"
MODE_MANUAL = "Manual (No split)"
MODE_PRESETS = [MODE_ALBUM, MODE_SINGLE, MODE_MANUAL]

FORMAT_WAV = "WAV"
FORMAT_FLAC = "FLAC"
FORMAT_M4A = "M4A"
OUTPUT_FORMATS = [FORMAT_WAV, FORMAT_FLAC, FORMAT_M4A]


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

def build_diagnostic_lines():
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

def export_audio(tmp_wav_path, target_format, track_info, artwork_bytes):
    base_path = os.path.splitext(tmp_wav_path)[0]
    final_path = tmp_wav_path
    
    if target_format == FORMAT_FLAC:
        final_path = base_path + ".flac"
        subprocess.run(["afconvert", "-f", "flac", "-d", "flac", tmp_wav_path, final_path], check=True)
        os.remove(tmp_wav_path)
    elif target_format == FORMAT_M4A:
        final_path = base_path + ".m4a"
        subprocess.run(["afconvert", "-f", "m4af", "-d", "aac", "-b", "320000", tmp_wav_path, final_path], check=True)
        os.remove(tmp_wav_path)
        
    try:
        title = track_info.get("name", "Unknown")
        artist = track_info.get("artist", "Unknown")
        album = track_info.get("album", "Unknown")
        
        if target_format == FORMAT_WAV:
            audio = WAVE(final_path)
            if audio.tags is None:
                audio.add_tags()
            audio.tags.add(TIT2(encoding=3, text=title))
            audio.tags.add(TPE1(encoding=3, text=artist))
            audio.tags.add(TALB(encoding=3, text=album))
            if artwork_bytes:
                audio.tags.add(APIC(encoding=3, mime='image/jpeg', type=3, desc='Front Cover', data=artwork_bytes))
            audio.save()
            
        elif target_format == FORMAT_FLAC:
            audio = FLAC(final_path)
            audio["title"] = title
            audio["artist"] = artist
            audio["album"] = album
            if artwork_bytes:
                pic = Picture()
                pic.type = 3
                pic.mime = "image/jpeg"
                pic.desc = "Front Cover"
                pic.data = artwork_bytes
                audio.add_picture(pic)
            audio.save()
            
        elif target_format == FORMAT_M4A:
            audio = MP4(final_path)
            if audio.tags is None:
                audio.add_tags()
            audio.tags["\xa9nam"] = title
            audio.tags["\xa9ART"] = artist
            audio.tags["\xa9alb"] = album
            if artwork_bytes:
                audio.tags["covr"] = [MP4Cover(artwork_bytes, imageformat=MP4Cover.FORMAT_JPEG)]
            audio.save()
    except Exception as e:
        print(f"Tagging error: {e}")
        
    return final_path

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

        candidates.append({
            "track": track,
            "start": start,
            "end": end,
            "trim_start": trim_start,
            "trim_end": trim_end,
            "duration": len(trimmed) / sample_rate if len(trimmed) > 0 else 0,
            "default_checked": default_checked,
            "reason": reason,
            "segment_audio": segment
        })
        
    return candidates

def process_and_save_candidates(candidates, options, log_callback, on_finish):
    save_dir = options["save_dir"]
    sample_rate = options["sample_rate"]
    target_format = options.get("target_format", FORMAT_WAV)
    os.makedirs(save_dir, exist_ok=True)
    
    saved = 0
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
            
        if options.get("normalize", False):
            max_val = np.max(np.abs(trimmed))
            if max_val > 0:
                target_peak = 10 ** (-1.0 / 20.0)  # -1.0 dBFS
                trimmed = trimmed * (target_peak / max_val)
            
        filename = safe_filename(f"{track.get('artist', 'Unknown')} - {track.get('name', 'Untitled')}") + ".wav"
        tmp_wav_path = unique_path(save_dir, filename)
        
        # Write WAV
        int_data = (trimmed * 32767.0).clip(-32768, 32767).astype(np.int16)
        with wave.open(tmp_wav_path, "wb") as wf:
            wf.setnchannels(2)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(int_data.tobytes())
            
        # Download artwork if requested
        artwork_bytes = None
        if track.get("artwork_url"):
            artwork_bytes = download_url_bytes(track["artwork_url"])
            
        # Convert and tag
        final_path = export_audio(tmp_wav_path, target_format, track, artwork_bytes)
        log_callback(f"保存: {os.path.basename(final_path)}")
        saved += 1
        
    log_callback(f"処理完了: {saved} 件保存しました。")
    if on_finish:
        on_finish()
