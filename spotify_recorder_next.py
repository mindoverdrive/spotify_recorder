import os
import queue
import base64
import subprocess
import threading
import time
import wave
from datetime import datetime
from tkinter import PhotoImage, filedialog, messagebox

import customtkinter as ctk
import numpy as np
import sounddevice as sd
from mutagen.id3 import TALB, TIT2, TPE1
from mutagen.wave import WAVE

from spotify_recorder_next_services import (
    MODE_PRESETS,
    OUTPUT_FORMATS,
    SpotifyWebClient,
    build_diagnostic_lines,
    convert_image_to_png,
    download_url_bytes,
    normalized_track_key,
    prepare_track_candidates,
    process_and_save_tracks as process_tracks_with_options,
    save_candidates,
)


CHECK_INTERVAL_MS = 800
DEFAULT_SAMPLE_RATE = 44100
DEFAULT_PRE_BUFFER_SEC = 3.0


ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("green")


def run_applescript(script, timeout=1.2):
    try:
        return subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except Exception as exc:
        return exc


def get_spotify_info():
    script = """
    if application "Spotify" is running then
        tell application "Spotify"
            if player state is playing or player state is paused then
                set track_name to name of current track
                set artist_name to artist of current track
                set album_name to album of current track
                set player_state to player state as string
                set player_position to player position
                return track_name & "||" & artist_name & "||" & album_name & "||" & player_state & "||" & player_position
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
        if raw == "CLOSED":
            return {"status": "CLOSED"}
        if raw == "IDLE":
            return {"status": "IDLE"}

        parts = raw.split("||")
        if len(parts) >= 4:
            try:
                position = float(parts[4]) if len(parts) >= 5 else 0.0
            except ValueError:
                position = 0.0
            return {
                "status": "OK",
                "name": parts[0],
                "artist": parts[1],
                "album": parts[2],
                "state": parts[3],
                "position": position,
            }

    err_msg = result.stderr.strip()
    if "not allowed" in err_msg or "許可されていません" in err_msg or "error -1743" in err_msg:
        return {"status": "PERMISSION_DENIED", "error": err_msg}
    if "error -600" in err_msg:
        return {"status": "CLOSED"}

    fallback = (
        'tell application "System Events" to get name of first window '
        'of (first process whose name is "Spotify")'
    )
    fb_result = run_applescript(fallback)
    if not isinstance(fb_result, Exception) and fb_result.returncode == 0:
        title = fb_result.stdout.strip()
        if " - " in title:
            artist, name = title.split(" - ", 1)
            return {
                "status": "OK",
                "name": name,
                "artist": artist,
                "album": "Captured from Window",
                "state": "playing",
                "position": 0.0,
            }

    return {"status": "NOT_LINKED", "error": err_msg}


def track_key(info):
    if not info or info.get("status") != "OK":
        return None
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
        delta = (approx_sample - split) / sample_rate
        log_callback(f"境界補正: {track_name} / {delta:+.2f}s")
        return split

    split = max(0, approx_sample - int(1.2 * sample_rate))
    log_callback(f"無音境界なし: {track_name} / 推定 -1.20s")
    return split


def write_wav(path, audio, sample_rate):
    int_data = (audio * 32767.0).clip(-32768, 32767).astype(np.int16)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(int_data.tobytes())


def add_wave_tags(path, track):
    audio = WAVE(path)
    if audio.tags is None:
        audio.add_tags()
    audio.tags.add(TIT2(encoding=3, text=track.get("name", "")))
    audio.tags.add(TPE1(encoding=3, text=track.get("artist", "")))
    audio.tags.add(TALB(encoding=3, text=track.get("album", "")))
    audio.save()


def build_split_points(audio, history, sample_rate, threshold_db, log_callback):
    split_points = [0]
    for track in history[1:]:
        approx = int(track["start_sample"])
        split_points.append(
            find_silence_split(
                audio,
                approx,
                sample_rate,
                threshold_db,
                log_callback,
                track.get("name", "Unknown"),
            )
        )
    split_points.append(len(audio))

    cleaned = [0]
    for point in split_points[1:]:
        point = min(max(int(point), cleaned[-1]), len(audio))
        cleaned.append(point)
    return cleaned


def process_and_save_tracks(audio, history, options, stop_info, log_callback, on_finish):
    try:
        save_dir = options["save_dir"]
        sample_rate = options["sample_rate"]
        threshold_db = options["threshold_db"]
        pad_start = options["pad_start_sec"]
        pad_end = options["pad_end_sec"]
        min_keep_sec = options["min_keep_sec"]
        discard_tail = options["discard_tail"]
        discard_tail_under_sec = options["discard_tail_under_sec"]

        os.makedirs(save_dir, exist_ok=True)
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
        stop_key = track_key(stop_info)
        saved = 0
        skipped = 0

        for index, track in enumerate(history):
            start = split_points[index]
            end = split_points[index + 1]
            segment = audio[start:end]
            duration = len(segment) / sample_rate
            is_last = index == len(history) - 1
            segment_key = track.get("key")

            if duration < min_keep_sec:
                log_callback(f"スキップ: {track.get('name', 'Unknown')} / {duration:.1f}s < {min_keep_sec:.1f}s")
                skipped += 1
                continue

            if (
                discard_tail
                and is_last
                and stop_key is not None
                and stop_key == segment_key
                and duration <= discard_tail_under_sec
            ):
                log_callback(
                    "スキップ: 停止時に再生中だった最終断片 "
                    f"/ {track.get('name', 'Unknown')} / {duration:.1f}s"
                )
                skipped += 1
                continue

            trimmed, trim_start, trim_end = trim_silence(
                segment,
                sample_rate,
                threshold_db,
                pad_start,
                pad_end,
            )
            if len(trimmed) == 0:
                log_callback(f"スキップ: 無音のみ / {track.get('name', 'Unknown')}")
                skipped += 1
                continue

            filename = safe_filename(f"{track.get('artist', 'Unknown')} - {track.get('name', 'Untitled')}") + ".wav"
            path = unique_path(save_dir, filename)
            write_wav(path, trimmed, sample_rate)

            try:
                add_wave_tags(path, track)
            except Exception as exc:
                log_callback(f"タグ埋め込み失敗: {os.path.basename(path)} / {exc}")

            cut_head = trim_start / sample_rate
            cut_tail = (len(segment) - trim_end) / sample_rate
            log_callback(
                f"保存: {os.path.basename(path)} / {len(trimmed) / sample_rate:.1f}s "
                f"(trim head {cut_head:.2f}s, tail {cut_tail:.2f}s)"
            )
            saved += 1

        log_callback(f"保存完了: {saved}件 / スキップ {skipped}件")
    except Exception as exc:
        log_callback(f"保存処理エラー: {exc}")
    finally:
        on_finish()


class SpotifyRecorderNextApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Spotify Recorder Next Prototype")
        self.geometry("720x820")
        self.minsize(680, 760)

        self.is_recording = False
        self.is_standby = False
        self.stream = None
        self.audio_lock = threading.Lock()
        self.recorded_chunks = []
        self.pre_chunks = []
        self.pre_samples = 0
        self.total_samples_recorded = 0
        self.recording_history = []
        self.current_spotify_info = None
        self.last_track_key = None
        self.spotify_idle_since = None
        self.latest_level = 0.0
        self._stream_start_ch = 1

        self.log_queue = queue.Queue()
        self.save_dir = ctk.StringVar(value=os.path.expanduser("~/Desktop/Spotify_recordings_next"))
        self.record_ch_start = ctk.IntVar(value=5)
        self.silence_threshold_db = ctk.DoubleVar(value=-60.0)
        self.pad_start_sec = ctk.DoubleVar(value=0.10)
        self.pad_end_sec = ctk.DoubleVar(value=0.35)
        self.min_keep_sec = ctk.DoubleVar(value=12.0)
        self.discard_tail_under_sec = ctk.DoubleVar(value=45.0)
        self.discard_tail = ctk.BooleanVar(value=True)
        self.auto_stop_on_idle = ctk.BooleanVar(value=True)
        self.auto_stop_grace_sec = ctk.DoubleVar(value=3.0)

        self.audio_devices = sd.query_devices()
        self.input_devices = [
            (index, device)
            for index, device in enumerate(self.audio_devices)
            if device["max_input_channels"] > 0
        ]
        self.input_device_strings = [f"{index}: {device['name']}" for index, device in self.input_devices]
        self.device_in_id = self.pick_default_input()
        self.sample_rate = DEFAULT_SAMPLE_RATE
        self.update_sample_rate()

        self.build_ui()
        self.after(CHECK_INTERVAL_MS, self.poll_spotify)
        self.after(50, self.process_queues)
        self.protocol("WM_DELETE_WINDOW", self.on_closing)

    def pick_default_input(self):
        for needle in ("loopback", "blackhole"):
            for index, device in self.input_devices:
                if needle in device["name"].lower():
                    return index
        if sd.default.device[0] is not None:
            return sd.default.device[0]
        return self.input_devices[0][0] if self.input_devices else None

    def update_sample_rate(self):
        if self.device_in_id is None:
            self.sample_rate = DEFAULT_SAMPLE_RATE
            return
        try:
            info = sd.query_devices(self.device_in_id, "input")
            self.sample_rate = int(info.get("default_samplerate", DEFAULT_SAMPLE_RATE))
        except Exception:
            self.sample_rate = DEFAULT_SAMPLE_RATE

    def build_ui(self):
        root = ctk.CTkFrame(self, corner_radius=12)
        root.pack(fill="both", expand=True, padx=14, pady=14)

        self.status_label = ctk.CTkLabel(
            root,
            text="Ready",
            font=ctk.CTkFont(size=22, weight="bold"),
            text_color="#1DB954",
        )
        self.status_label.pack(pady=(16, 6))

        info = ctk.CTkFrame(root, fg_color="#1f2328", corner_radius=8)
        info.pack(fill="x", padx=18, pady=8)
        self.track_label = ctk.CTkLabel(info, text="Spotify: checking...", font=ctk.CTkFont(size=16, weight="bold"))
        self.track_label.pack(pady=(14, 2))
        self.artist_label = ctk.CTkLabel(info, text="-", text_color="#b7bec8")
        self.artist_label.pack(pady=(0, 14))

        meter_frame = ctk.CTkFrame(root, fg_color="transparent")
        meter_frame.pack(fill="x", padx=18, pady=4)
        ctk.CTkLabel(meter_frame, text="Input").pack(side="left")
        self.level_meter = ctk.CTkProgressBar(meter_frame, height=12, progress_color="#1DB954")
        self.level_meter.pack(side="left", fill="x", expand=True, padx=10)
        self.level_meter.set(0)

        switches = ctk.CTkFrame(root, fg_color="transparent")
        switches.pack(fill="x", padx=18, pady=8)
        self.standby_switch = ctk.CTkSwitch(
            switches,
            text="Standby: Spotify再生で自動開始",
            progress_color="#1DB954",
            command=self.on_standby_toggle,
        )
        self.standby_switch.pack(anchor="w", pady=3)
        self.auto_stop_switch = ctk.CTkSwitch(
            switches,
            text="Spotify停止/一時停止で自動停止",
            variable=self.auto_stop_on_idle,
            progress_color="#1DB954",
        )
        self.auto_stop_switch.pack(anchor="w", pady=3)
        self.discard_tail_switch = ctk.CTkSwitch(
            switches,
            text="停止時の短い最終断片を保存しない",
            variable=self.discard_tail,
            progress_color="#1DB954",
        )
        self.discard_tail_switch.pack(anchor="w", pady=3)

        buttons = ctk.CTkFrame(root, fg_color="transparent")
        buttons.pack(fill="x", padx=18, pady=8)
        self.start_btn = ctk.CTkButton(
            buttons,
            text="Start Recording",
            height=42,
            fg_color="#1DB954",
            hover_color="#1ed760",
            text_color="#06170c",
            font=ctk.CTkFont(weight="bold"),
            command=self.start_rec,
        )
        self.start_btn.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.stop_btn = ctk.CTkButton(
            buttons,
            text="Stop & Save",
            height=42,
            fg_color="#e91429",
            hover_color="#ff3348",
            state="disabled",
            command=self.stop_rec,
        )
        self.stop_btn.pack(side="left", fill="x", expand=True, padx=6)
        self.abort_btn = ctk.CTkButton(
            buttons,
            text="Abort",
            height=42,
            fg_color="#414852",
            hover_color="#59616d",
            state="disabled",
            command=self.abort_rec,
        )
        self.abort_btn.pack(side="left", fill="x", expand=True, padx=(6, 0))

        settings = ctk.CTkFrame(root, fg_color="#161a1f", corner_radius=8)
        settings.pack(fill="x", padx=18, pady=10)
        settings.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(settings, text="Input Device").grid(row=0, column=0, sticky="w", padx=12, pady=(12, 4))
        values = self.input_device_strings if self.input_device_strings else ["No input devices"]
        self.in_combo = ctk.CTkOptionMenu(settings, values=values, command=self.on_device_change)
        self.in_combo.grid(row=0, column=1, sticky="ew", padx=12, pady=(12, 4))
        if self.input_device_strings:
            current = next(
                (item for item in self.input_device_strings if item.startswith(f"{self.device_in_id}:")),
                self.input_device_strings[0],
            )
            self.in_combo.set(current)

        self.setting_entries = []
        self.add_entry(settings, 1, "開始ch", self.record_ch_start, "Loopback/BlackHoleでSpotifyが入る先頭ch")
        self.add_entry(settings, 2, "無音閾値 dB", self.silence_threshold_db, "小さいほど厳密。例: -60")
        self.add_entry(settings, 3, "開始余白 秒", self.pad_start_sec, "フェードイン保護")
        self.add_entry(settings, 4, "終了余白 秒", self.pad_end_sec, "余韻保護")
        self.add_entry(settings, 5, "通常スキップ 秒", self.min_keep_sec, "これ未満の曲候補は保存しない")
        self.add_entry(settings, 6, "最終断片 秒", self.discard_tail_under_sec, "停止時の最終曲がこれ以下なら破棄")
        self.add_entry(settings, 7, "自動停止猶予 秒", self.auto_stop_grace_sec, "Spotify停止後に待つ秒数")

        ctk.CTkLabel(settings, text="保存先").grid(row=8, column=0, sticky="w", padx=12, pady=8)
        dir_frame = ctk.CTkFrame(settings, fg_color="transparent")
        dir_frame.grid(row=8, column=1, sticky="ew", padx=12, pady=8)
        dir_frame.grid_columnconfigure(0, weight=1)
        self.dir_label = ctk.CTkLabel(dir_frame, textvariable=self.save_dir, anchor="w", fg_color="#252b33", corner_radius=5)
        self.dir_label.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.browse_btn = ctk.CTkButton(dir_frame, text="Browse", width=78, command=self.browse_dir)
        self.browse_btn.grid(row=0, column=1)

        ctk.CTkLabel(root, text="Log", text_color="#b7bec8").pack(anchor="w", padx=20, pady=(4, 2))
        self.log_box = ctk.CTkTextbox(
            root,
            height=210,
            font=ctk.CTkFont(family="monospace", size=11),
            fg_color="#090c10",
            text_color="#63f08f",
            border_width=1,
            border_color="#242b33",
        )
        self.log_box.pack(fill="both", expand=True, padx=18, pady=(0, 16))
        self.log_box.configure(state="disabled")

        if not self.input_devices:
            self.start_btn.configure(state="disabled")
            self.log_message("入力デバイスが見つかりません。Loopback/BlackHole設定を確認してください。")

    def add_entry(self, parent, row, label, variable, hint):
        ctk.CTkLabel(parent, text=label).grid(row=row, column=0, sticky="w", padx=12, pady=4)
        entry = ctk.CTkEntry(parent, textvariable=variable, width=90)
        entry.grid(row=row, column=1, sticky="w", padx=12, pady=4)
        ctk.CTkLabel(parent, text=hint, text_color="#808995").grid(row=row, column=1, sticky="w", padx=(116, 12), pady=4)
        self.setting_entries.append(entry)

    def set_controls_recording(self, recording):
        self.start_btn.configure(state="disabled" if recording else "normal")
        self.stop_btn.configure(state="normal" if recording else "disabled")
        self.abort_btn.configure(state="normal" if recording else "disabled")
        locked_state = "disabled" if recording else "normal"
        self.in_combo.configure(state=locked_state)
        self.browse_btn.configure(state=locked_state)
        self.standby_switch.configure(state=locked_state)
        for entry in self.setting_entries:
            entry.configure(state=locked_state)

    def log_message(self, message):
        self.log_queue.put(message)

    def process_queues(self):
        while not self.log_queue.empty():
            message = self.log_queue.get()
            self.log_box.configure(state="normal")
            self.log_box.insert("end", f"[{datetime.now().strftime('%H:%M:%S')}] {message}\n")
            self.log_box.see("end")
            self.log_box.configure(state="disabled")

        self.level_meter.set(min(1.0, self.latest_level * 8.0))
        self.after(50, self.process_queues)

    def on_device_change(self, _):
        if not self.input_device_strings:
            return
        self.device_in_id = int(self.in_combo.get().split(":", 1)[0])
        self.update_sample_rate()
        self.log_message(f"入力デバイス: {self.device_in_id} / {self.sample_rate}Hz")

    def browse_dir(self):
        folder = filedialog.askdirectory(initialdir=self.save_dir.get())
        if folder:
            self.save_dir.set(folder)

    def on_standby_toggle(self):
        self.is_standby = self.standby_switch.get() == 1
        if self.is_standby:
            if self.start_audio_stream():
                self.status_label.configure(text="Standby", text_color="#f0b429")
                self.pre_chunks = []
                self.pre_samples = 0
                self.log_message("Standby有効: Spotifyの再生を待機します。")
            else:
                self.is_standby = False
                self.standby_switch.deselect()
        else:
            if not self.is_recording:
                self.stop_audio_stream()
                self.status_label.configure(text="Ready", text_color="#1DB954")
            self.log_message("Standby解除")

    def audio_callback(self, indata, frames, time_info, status):
        start = max(0, self._stream_start_ch - 1)
        if indata.shape[1] >= start + 2:
            stereo = indata[:, start : start + 2]
        elif indata.shape[1] >= 2:
            stereo = indata[:, :2]
        else:
            stereo = np.repeat(indata[:, :1], 2, axis=1)

        self.latest_level = float(np.sqrt(np.mean(stereo * stereo))) if stereo.size else 0.0

        with self.audio_lock:
            if self.is_recording:
                self.recorded_chunks.append(stereo.copy())
                self.total_samples_recorded += len(stereo)
            elif self.is_standby:
                self.pre_chunks.append(stereo.copy())
                self.pre_samples += len(stereo)
                target = int(DEFAULT_PRE_BUFFER_SEC * self.sample_rate)
                while self.pre_samples > target and len(self.pre_chunks) > 1:
                    removed = self.pre_chunks.pop(0)
                    self.pre_samples -= len(removed)

    def start_audio_stream(self):
        if self.stream:
            return True
        if self.device_in_id is None:
            self.log_message("入力デバイスがありません。")
            return False

        self.update_sample_rate()
        device_info = sd.query_devices(self.device_in_id, "input")
        max_channels = int(device_info["max_input_channels"])
        start_ch = int(self.record_ch_start.get())
        if start_ch < 1 or start_ch + 1 > max_channels:
            self.log_message(f"開始chエラー: ch {start_ch} は使えません (最大 {max_channels}ch)")
            return False

        try:
            self._stream_start_ch = start_ch
            self.stream = sd.InputStream(
                device=self.device_in_id,
                samplerate=self.sample_rate,
                channels=max_channels,
                dtype="float32",
                blocksize=2048,
                latency="high",
                callback=self.audio_callback,
            )
            self.stream.start()
            self.log_message(f"Audio stream started: {self.sample_rate}Hz / ch {start_ch}-{start_ch + 1}")
            return True
        except Exception as exc:
            self.stream = None
            self.log_message(f"Audio stream error: {exc}")
            return False

    def stop_audio_stream(self):
        if self.stream:
            try:
                self.stream.stop()
                self.stream.close()
            finally:
                self.stream = None

    def add_history_track(self, info, start_sample):
        if not info or info.get("status") != "OK":
            return
        start_sample = max(0, min(int(start_sample), int(self.total_samples_recorded)))
        item = {
            "name": info["name"],
            "artist": info["artist"],
            "album": info["album"],
            "start_sample": start_sample,
            "key": track_key(info),
        }
        self.recording_history.append(item)
        self.last_track_key = item["key"]

    def estimate_track_start_sample(self, info):
        try:
            position = max(0.0, float(info.get("position", 0.0)))
        except (TypeError, ValueError):
            position = 0.0
        estimated = self.total_samples_recorded - int(position * self.sample_rate)
        if self.recording_history:
            estimated = max(estimated, int(self.recording_history[-1]["start_sample"]) + 1)
        return max(0, min(int(estimated), int(self.total_samples_recorded)))

    def start_rec(self):
        if self.is_recording:
            return
        if not self.stream and not self.start_audio_stream():
            return

        with self.audio_lock:
            self.recorded_chunks = []
            if self.is_standby and self.pre_chunks:
                self.recorded_chunks.extend(chunk.copy() for chunk in self.pre_chunks)
                self.total_samples_recorded = self.pre_samples
                self.log_message(f"事前バッファ追加: {self.pre_samples / self.sample_rate:.2f}s")
            else:
                self.total_samples_recorded = 0
            self.pre_chunks = []
            self.pre_samples = 0
            self.is_recording = True

        self.spotify_idle_since = None
        self.recording_history = []
        self.last_track_key = None
        if self.current_spotify_info and self.current_spotify_info.get("status") == "OK":
            self.add_history_track(self.current_spotify_info, 0)
            self.status_label.configure(
                text=f"Recording: {self.current_spotify_info['name']}",
                text_color="#e91429",
            )
            self.log_message(f"録音開始: {self.current_spotify_info['artist']} - {self.current_spotify_info['name']}")
        else:
            self.recording_history.append(
                {
                    "name": datetime.now().strftime("Recording_%H%M%S"),
                    "artist": "Unknown",
                    "album": "Captured",
                    "start_sample": 0,
                    "key": None,
                }
            )
            self.status_label.configure(text="Recording", text_color="#e91429")
            self.log_message("録音開始: Spotifyメタデータ未取得")

        self.set_controls_recording(True)

    def stop_rec(self):
        if not self.is_recording:
            return

        stop_info = get_spotify_info()
        if stop_info.get("status") != "OK":
            stop_info = self.current_spotify_info
        elif track_key(stop_info) != self.last_track_key and self.last_track_key is not None:
            estimated_start = self.estimate_track_start_sample(stop_info)
            self.add_history_track(stop_info, estimated_start)
            self.log_message(
                "停止時に未検知の曲変更を補足: "
                f"{stop_info['artist']} - {stop_info['name']} / start {estimated_start / self.sample_rate:.2f}s"
            )
        self.is_recording = False
        self.is_standby = False
        self.standby_switch.deselect()
        self.stop_audio_stream()
        self.status_label.configure(text="Processing...", text_color="#1DB954")
        self.stop_btn.configure(state="disabled")
        self.abort_btn.configure(state="disabled")

        with self.audio_lock:
            chunks = list(self.recorded_chunks)
            self.recorded_chunks = []

        if not chunks:
            self.log_message("録音データがありません。")
            self.on_processing_finished()
            return

        audio = np.concatenate(chunks, axis=0)
        options = {
            "save_dir": self.save_dir.get(),
            "sample_rate": self.sample_rate,
            "threshold_db": float(self.silence_threshold_db.get()),
            "pad_start_sec": float(self.pad_start_sec.get()),
            "pad_end_sec": float(self.pad_end_sec.get()),
            "min_keep_sec": float(self.min_keep_sec.get()),
            "discard_tail": bool(self.discard_tail.get()),
            "discard_tail_under_sec": float(self.discard_tail_under_sec.get()),
        }
        self.log_message(f"保存処理開始: {len(audio) / self.sample_rate:.1f}s")
        threading.Thread(
            target=process_and_save_tracks,
            args=(audio, list(self.recording_history), options, stop_info, self.log_message, self.on_processing_finished),
            daemon=True,
        ).start()

    def abort_rec(self):
        if not self.is_recording:
            return
        self.is_recording = False
        self.is_standby = False
        self.standby_switch.deselect()
        self.stop_audio_stream()
        with self.audio_lock:
            self.recorded_chunks = []
            self.pre_chunks = []
            self.pre_samples = 0
        self.log_message("録音を破棄しました。")
        self.on_processing_finished()

    def on_processing_finished(self):
        def update():
            self.set_controls_recording(False)
            self.status_label.configure(text="Ready", text_color="#1DB954")

        self.after(0, update)

    def poll_spotify(self):
        info = get_spotify_info()
        self.current_spotify_info = info

        if info.get("status") == "OK":
            self.track_label.configure(text=info["name"], text_color="white")
            self.artist_label.configure(text=f"{info['artist']} - {info['album']} / {info['state']}", text_color="#b7bec8")

            if self.is_standby and not self.is_recording and info.get("state") == "playing":
                self.log_message(f"Spotify再生検知: {info['artist']} - {info['name']}")
                self.start_rec()

            current_key = track_key(info)
            if self.is_recording and info.get("state") == "playing":
                self.spotify_idle_since = None
                if current_key != self.last_track_key:
                    self.add_history_track(info, self.estimate_track_start_sample(info))
                    self.status_label.configure(text=f"Recording: {info['name']}", text_color="#e91429")
                    self.log_message(f"曲変更: {info['artist']} - {info['name']}")
        elif info.get("status") == "PERMISSION_DENIED":
            self.track_label.configure(text="Spotify Automation permission required", text_color="#f0b429")
            self.artist_label.configure(text="System Settings > Privacy & Security > Automation", text_color="#f0b429")
        elif info.get("status") == "CLOSED":
            self.track_label.configure(text="Spotify is not running", text_color="#808995")
            self.artist_label.configure(text="-", text_color="#808995")
        else:
            self.track_label.configure(text="Spotify not linked", text_color="#808995")
            self.artist_label.configure(text=info.get("status", "unknown"), text_color="#808995")

        self.maybe_auto_stop(info)
        self.after(CHECK_INTERVAL_MS, self.poll_spotify)

    def maybe_auto_stop(self, info):
        if not self.is_recording or not self.auto_stop_on_idle.get():
            return
        is_playing = info.get("status") == "OK" and info.get("state") == "playing"
        if is_playing:
            self.spotify_idle_since = None
            return

        now = time.monotonic()
        if self.spotify_idle_since is None:
            self.spotify_idle_since = now
            return

        if now - self.spotify_idle_since >= float(self.auto_stop_grace_sec.get()):
            self.log_message("Spotify停止を検知したため自動停止します。")
            self.stop_rec()

    def on_closing(self):
        if self.is_recording:
            self.stop_rec()
        self.stop_audio_stream()
        self.destroy()


if __name__ == "__main__":
    app = SpotifyRecorderNextApp()
    app.mainloop()
