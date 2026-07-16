import base64
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import wave
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

import numpy as np
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, TALB, TIT2, TPE1
from mutagen.mp4 import MP4, MP4Cover
from mutagen.wave import WAVE


AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
CURRENTLY_PLAYING_URL = "https://api.spotify.com/v1/me/player/currently-playing"
SPOTIFY_SCOPE = "user-read-currently-playing user-read-playback-state"
TOKEN_CACHE_PATH = os.path.expanduser("~/.spotify_recorder_next_spotify.json")

OUTPUT_FORMATS = ["wav", "flac", "m4a"]

MODE_PRESETS = {
    "Album": {
        "min_keep_sec": 12.0,
        "discard_tail_under_sec": 45.0,
        "discard_tail": True,
        "auto_stop_on_idle": True,
        "review_before_save": True,
    },
    "Single": {
        "min_keep_sec": 5.0,
        "discard_tail_under_sec": 90.0,
        "discard_tail": True,
        "auto_stop_on_idle": True,
        "review_before_save": True,
    },
    "Manual": {
        "min_keep_sec": 0.0,
        "discard_tail_under_sec": 0.0,
        "discard_tail": False,
        "auto_stop_on_idle": False,
        "review_before_save": True,
    },
}


def normalized_track_key(info):
    if not info or info.get("status") != "OK":
        return None
    spotify_id = info.get("spotify_id")
    if spotify_id:
        return ("spotify", spotify_id)
    return (
        info.get("name", "").strip().lower(),
        info.get("artist", "").strip().lower(),
        info.get("album", "").strip().lower(),
    )


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


def image_mime(image_bytes):
    if not image_bytes:
        return None
    if image_bytes.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    return None


def download_url_bytes(url, timeout=8):
    if not url:
        return None
    request = urllib.request.Request(url, headers={"User-Agent": "SpotifyRecorderNext/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def convert_image_to_png(image_bytes, max_size=96):
    if not image_bytes:
        return None
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return image_bytes if image_mime(image_bytes) == "image/png" else None

    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        "pipe:0",
        "-vf",
        f"scale={max_size}:{max_size}:force_original_aspect_ratio=decrease",
        "-f",
        "image2pipe",
        "-vcodec",
        "png",
        "pipe:1",
    ]
    result = subprocess.run(command, input=image_bytes, capture_output=True, timeout=10)
    return result.stdout if result.returncode == 0 and result.stdout else None


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

    candidates = [
        (start, end, end - start)
        for start, end in zip(starts, ends)
        if end - start >= int(0.05 * sample_rate)
    ]
    if candidates:
        best_start, best_end, _ = max(candidates, key=lambda item: item[2])
        split = search_start + (best_start + best_end) // 2
        delta = (approx_sample - split) / sample_rate
        log_callback(f"境界補正: {track_name} / {delta:+.2f}s")
        return split

    split = max(0, approx_sample - int(1.2 * sample_rate))
    log_callback(f"無音境界なし: {track_name} / 推定 -1.20s")
    return split


def build_split_points(audio, history, sample_rate, threshold_db, log_callback):
    split_points = [0]
    for track in history[1:]:
        split_points.append(
            find_silence_split(
                audio,
                int(track["start_sample"]),
                sample_rate,
                threshold_db,
                log_callback,
                track.get("name", "Unknown"),
            )
        )
    split_points.append(len(audio))

    cleaned = [0]
    for point in split_points[1:]:
        cleaned.append(min(max(int(point), cleaned[-1]), len(audio)))
    return cleaned


def prepare_track_candidates(audio, history, options, stop_info, log_callback):
    sample_rate = options["sample_rate"]
    threshold_db = options["threshold_db"]
    pad_start = options["pad_start_sec"]
    pad_end = options["pad_end_sec"]
    min_keep_sec = options["min_keep_sec"]
    discard_tail = options["discard_tail"]
    discard_tail_under_sec = options["discard_tail_under_sec"]

    history = [dict(track) for track in history if int(track.get("start_sample", -1)) < len(audio)]
    if not history:
        history = [
            {
                "name": datetime.now().strftime("Recording_%H%M%S"),
                "artist": "Unknown",
                "album": "Captured",
                "start_sample": 0,
                "key": None,
            }
        ]

    history.sort(key=lambda item: int(item["start_sample"]))
    split_points = build_split_points(audio, history, sample_rate, threshold_db, log_callback)
    stop_key = normalized_track_key(stop_info)
    candidates = []

    for index, track in enumerate(history):
        start = split_points[index]
        end = split_points[index + 1]
        segment = audio[start:end]
        duration = len(segment) / sample_rate
        trimmed, trim_start, trim_end = trim_silence(segment, sample_rate, threshold_db, pad_start, pad_end)
        trimmed_duration = len(trimmed) / sample_rate if len(trimmed) else 0.0
        is_last = index == len(history) - 1
        default_save = True
        reason = "保存候補"

        if duration < min_keep_sec:
            default_save = False
            reason = f"短すぎる候補 ({duration:.1f}s < {min_keep_sec:.1f}s)"
        elif len(trimmed) == 0:
            default_save = False
            reason = "無音のみ"
        elif (
            discard_tail
            and len(history) > 1
            and is_last
            and stop_key is not None
            and stop_key == track.get("key")
            and duration <= discard_tail_under_sec
        ):
            default_save = False
            reason = f"停止時の最終断片 ({duration:.1f}s)"

        filename = safe_filename(f"{track.get('artist', 'Unknown')} - {track.get('name', 'Untitled')}")
        candidates.append(
            {
                "track": track,
                "segment": segment,
                "trimmed": trimmed,
                "start_sample": start,
                "end_sample": end,
                "duration": duration,
                "trimmed_duration": trimmed_duration,
                "trim_start": trim_start,
                "trim_end": trim_end,
                "filename_base": filename,
                "save": default_save,
                "default_save": default_save,
                "reason": reason,
            }
        )

    return candidates


def write_wav(path, audio, sample_rate):
    int_data = (audio * 32767.0).clip(-32768, 32767).astype(np.int16)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(int_data.tobytes())


def add_wave_tags(path, track, artwork_bytes=None):
    audio = WAVE(path)
    if audio.tags is None:
        audio.add_tags()
    audio.tags.add(TIT2(encoding=3, text=track.get("name", "")))
    audio.tags.add(TPE1(encoding=3, text=track.get("artist", "")))
    audio.tags.add(TALB(encoding=3, text=track.get("album", "")))
    mime = image_mime(artwork_bytes)
    if mime:
        audio.tags.add(
            APIC(
                encoding=3,
                mime=mime,
                type=3,
                desc="Cover",
                data=artwork_bytes,
            )
        )
    audio.save()


def add_flac_tags(path, track, artwork_bytes=None):
    audio = FLAC(path)
    audio["title"] = track.get("name", "")
    audio["artist"] = track.get("artist", "")
    audio["album"] = track.get("album", "")
    if track.get("spotify_id"):
        audio["spotify_track_id"] = track["spotify_id"]
    mime = image_mime(artwork_bytes)
    if mime:
        picture = Picture()
        picture.type = 3
        picture.mime = mime
        picture.desc = "Cover"
        picture.data = artwork_bytes
        audio.clear_pictures()
        audio.add_picture(picture)
    audio.save()


def add_m4a_tags(path, track, artwork_bytes=None):
    audio = MP4(path)
    audio["\xa9nam"] = [track.get("name", "")]
    audio["\xa9ART"] = [track.get("artist", "")]
    audio["\xa9alb"] = [track.get("album", "")]
    if track.get("spotify_id"):
        audio["----:com.apple.iTunes:SPOTIFY_TRACK_ID"] = [track["spotify_id"].encode("utf-8")]
    mime = image_mime(artwork_bytes)
    if mime == "image/jpeg":
        audio["covr"] = [MP4Cover(artwork_bytes, imageformat=MP4Cover.FORMAT_JPEG)]
    elif mime == "image/png":
        audio["covr"] = [MP4Cover(artwork_bytes, imageformat=MP4Cover.FORMAT_PNG)]
    audio.save()


def encode_with_ffmpeg(audio, sample_rate, output_path, output_format):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpegが見つかりません")

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = tmp.name
        write_wav(tmp_path, audio, sample_rate)
        command = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", tmp_path, "-vn"]
        if output_format == "flac":
            command.extend(["-c:a", "flac", output_path])
        elif output_format == "m4a":
            command.extend(["-c:a", "alac", output_path])
        else:
            raise ValueError(f"Unsupported format: {output_format}")
        result = subprocess.run(command, capture_output=True, text=True, timeout=180)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or f"ffmpeg failed: {result.returncode}")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


def export_audio(path, audio, sample_rate, output_format, track, artwork_bytes, log_callback):
    actual_format = output_format
    actual_path = path
    if output_format == "wav":
        write_wav(actual_path, audio, sample_rate)
        add_wave_tags(actual_path, track, artwork_bytes)
        return actual_path

    try:
        encode_with_ffmpeg(audio, sample_rate, actual_path, output_format)
    except Exception as exc:
        actual_format = "wav"
        actual_path = os.path.splitext(path)[0] + ".wav"
        log_callback(f"{output_format.upper()}保存失敗。WAVへ切替: {exc}")
        write_wav(actual_path, audio, sample_rate)

    if actual_format == "flac":
        add_flac_tags(actual_path, track, artwork_bytes)
    elif actual_format == "m4a":
        add_m4a_tags(actual_path, track, artwork_bytes)
    else:
        add_wave_tags(actual_path, track, artwork_bytes)
    return actual_path


def save_candidates(candidates, options, log_callback):
    save_dir = options["save_dir"]
    sample_rate = options["sample_rate"]
    output_format = options.get("output_format", "wav")
    os.makedirs(save_dir, exist_ok=True)
    saved = 0
    skipped = 0

    for candidate in candidates:
        track = candidate["track"]
        if not candidate.get("save"):
            log_callback(f"スキップ: {track.get('name', 'Unknown')} / {candidate.get('reason', '')}")
            skipped += 1
            continue

        audio = candidate["trimmed"]
        if len(audio) == 0:
            log_callback(f"スキップ: {track.get('name', 'Unknown')} / 無音のみ")
            skipped += 1
            continue

        artwork_bytes = None
        if track.get("album_art_url"):
            try:
                artwork_bytes = download_url_bytes(track["album_art_url"])
            except Exception as exc:
                log_callback(f"アートワーク取得失敗: {track.get('name', 'Unknown')} / {exc}")

        filename = f"{candidate['filename_base']}.{output_format}"
        path = unique_path(save_dir, filename)
        actual_path = export_audio(path, audio, sample_rate, output_format, track, artwork_bytes, log_callback)
        log_callback(
            f"保存: {os.path.basename(actual_path)} / {candidate['trimmed_duration']:.1f}s "
            f"(元 {candidate['duration']:.1f}s)"
        )
        saved += 1

    log_callback(f"保存完了: {saved}件 / スキップ {skipped}件")
    return saved, skipped


def process_and_save_tracks(audio, history, options, stop_info, log_callback, on_finish):
    try:
        candidates = prepare_track_candidates(audio, history, options, stop_info, log_callback)
        save_candidates(candidates, options, log_callback)
    except Exception as exc:
        log_callback(f"保存処理エラー: {exc}")
    finally:
        on_finish()


def make_code_verifier():
    return base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode("ascii")


def make_code_challenge(verifier):
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class SpotifyWebClient:
    def __init__(self, log_callback=None, redirect_port=8765):
        self.log_callback = log_callback or (lambda message: None)
        self.redirect_port = redirect_port
        self.client_id = ""
        self.token = {}
        self.load_cache()

    @property
    def redirect_uri(self):
        return f"http://127.0.0.1:{self.redirect_port}/callback"

    def load_cache(self):
        try:
            with open(TOKEN_CACHE_PATH, "r", encoding="utf-8") as f:
                payload = json.load(f)
            self.client_id = payload.get("client_id", "")
            self.token = payload.get("token", {})
        except Exception:
            self.client_id = ""
            self.token = {}

    def save_cache(self):
        payload = {"client_id": self.client_id, "token": self.token}
        with open(TOKEN_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    def is_configured(self):
        return bool(self.client_id)

    def is_connected(self):
        return bool(self.token.get("access_token") or self.token.get("refresh_token"))

    def authenticate(self, client_id):
        self.client_id = client_id.strip()
        if not self.client_id:
            raise RuntimeError("Spotify Client IDが未設定です")

        verifier = make_code_verifier()
        challenge = make_code_challenge(verifier)
        state = secrets.token_urlsafe(24)
        query = urllib.parse.urlencode(
            {
                "client_id": self.client_id,
                "response_type": "code",
                "redirect_uri": self.redirect_uri,
                "scope": SPOTIFY_SCOPE,
                "code_challenge_method": "S256",
                "code_challenge": challenge,
                "state": state,
            }
        )
        result = {}

        class CallbackHandler(BaseHTTPRequestHandler):
            def do_GET(handler_self):
                parsed = urllib.parse.urlparse(handler_self.path)
                params = urllib.parse.parse_qs(parsed.query)
                result["code"] = params.get("code", [""])[0]
                result["state"] = params.get("state", [""])[0]
                result["error"] = params.get("error", [""])[0]
                body = (
                    "<html><body><h2>Spotify Recorder Next</h2>"
                    "<p>Authentication finished. You can close this tab.</p></body></html>"
                ).encode("utf-8")
                handler_self.send_response(200)
                handler_self.send_header("Content-Type", "text/html; charset=utf-8")
                handler_self.send_header("Content-Length", str(len(body)))
                handler_self.end_headers()
                handler_self.wfile.write(body)

            def log_message(self, format, *args):
                return

        server = HTTPServer(("127.0.0.1", self.redirect_port), CallbackHandler)
        server.timeout = 180
        webbrowser.open(f"{AUTH_URL}?{query}")
        self.log_callback("ブラウザでSpotify認証を開きました。")
        server.handle_request()
        server.server_close()

        if result.get("error"):
            raise RuntimeError(f"Spotify認証エラー: {result['error']}")
        if not result.get("code") or result.get("state") != state:
            raise RuntimeError("Spotify認証が完了しませんでした")

        token = self.exchange_token(
            {
                "client_id": self.client_id,
                "grant_type": "authorization_code",
                "code": result["code"],
                "redirect_uri": self.redirect_uri,
                "code_verifier": verifier,
            }
        )
        self.set_token(token)
        self.save_cache()
        return True

    def exchange_token(self, fields):
        data = urllib.parse.urlencode(fields).encode("utf-8")
        request = urllib.request.Request(
            TOKEN_URL,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))

    def set_token(self, token):
        if "expires_in" in token:
            token["expires_at"] = time.time() + int(token["expires_in"]) - 30
        if "refresh_token" not in token and self.token.get("refresh_token"):
            token["refresh_token"] = self.token["refresh_token"]
        self.token = token

    def ensure_token(self):
        if not self.token.get("access_token") and not self.token.get("refresh_token"):
            raise RuntimeError("Spotify Web API未接続")
        if self.token.get("access_token") and time.time() < float(self.token.get("expires_at", 0)):
            return self.token["access_token"]
        refresh_token = self.token.get("refresh_token")
        if not refresh_token:
            raise RuntimeError("Spotify refresh tokenがありません")
        token = self.exchange_token(
            {
                "client_id": self.client_id,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            }
        )
        self.set_token(token)
        self.save_cache()
        return self.token["access_token"]

    def currently_playing(self):
        access_token = self.ensure_token()
        request = urllib.request.Request(
            CURRENTLY_PLAYING_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                if response.status == 204:
                    return {"status": "IDLE", "source": "web"}
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 204:
                return {"status": "IDLE", "source": "web"}
            if exc.code == 401 and self.token.get("refresh_token"):
                self.token["expires_at"] = 0
                access_token = self.ensure_token()
                request = urllib.request.Request(
                    CURRENTLY_PLAYING_URL,
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    if response.status == 204:
                        return {"status": "IDLE", "source": "web"}
                    payload = json.loads(response.read().decode("utf-8"))
            else:
                raise

        item = payload.get("item")
        if payload.get("currently_playing_type") != "track" or not item:
            return {"status": "IDLE", "source": "web"}

        album = item.get("album", {})
        images = album.get("images", [])
        image_url = images[0]["url"] if images else ""
        artists = ", ".join(artist.get("name", "") for artist in item.get("artists", []))
        return {
            "status": "OK",
            "source": "web",
            "name": item.get("name", ""),
            "artist": artists,
            "album": album.get("name", ""),
            "state": "playing" if payload.get("is_playing") else "paused",
            "position": float(payload.get("progress_ms") or 0) / 1000.0,
            "duration": float(item.get("duration_ms") or 0) / 1000.0,
            "spotify_id": item.get("id", ""),
            "album_art_url": image_url,
        }


def build_diagnostic_lines(input_devices, device_id, start_ch, latest_level, save_dir, web_client, output_format):
    lines = []
    if input_devices:
        lines.append(f"OK input devices: {len(input_devices)}")
    else:
        lines.append("NG input devices: 入力デバイスなし")

    selected = next((device for index, device in input_devices if index == device_id), None)
    if selected:
        max_channels = int(selected["max_input_channels"])
        if start_ch >= 1 and start_ch + 1 <= max_channels:
            lines.append(f"OK channel: ch {start_ch}-{start_ch + 1} / max {max_channels}")
        else:
            lines.append(f"NG channel: ch {start_ch}-{start_ch + 1} / max {max_channels}")
        lines.append(f"Device: {selected['name']}")
    else:
        lines.append("NG selected device: 見つかりません")

    try:
        os.makedirs(save_dir, exist_ok=True)
        writable = os.access(save_dir, os.W_OK)
        lines.append(f"{'OK' if writable else 'NG'} save dir: {save_dir}")
    except Exception as exc:
        lines.append(f"NG save dir: {exc}")

    ffmpeg_path = shutil.which("ffmpeg")
    if output_format in ("flac", "m4a"):
        lines.append(f"{'OK' if ffmpeg_path else 'NG'} ffmpeg: {ffmpeg_path or 'not found'}")
    else:
        lines.append(f"{'OK' if ffmpeg_path else 'INFO'} ffmpeg: {ffmpeg_path or 'not required for WAV'}")

    if web_client and web_client.is_connected():
        lines.append("OK Spotify Web API: connected")
    elif web_client and web_client.is_configured():
        lines.append("WARN Spotify Web API: Client IDあり / 未接続")
    else:
        lines.append("INFO Spotify Web API: 未設定。AppleScriptへフォールバック")

    if latest_level <= 0.0001:
        lines.append("WARN level: 現在の入力レベルがほぼ0")
    else:
        lines.append(f"OK level: {latest_level:.5f}")

    return lines
