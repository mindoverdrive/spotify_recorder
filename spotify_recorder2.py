import os
import queue
import threading
import time
from datetime import datetime
from tkinter import filedialog

import customtkinter as ctk
import numpy as np
import sounddevice as sd

from spotify_recorder_services import (
    MODE_PRESETS,
    MODE_ALBUM,
    MODE_SINGLE,
    MODE_MANUAL,
    build_diagnostic_lines,
    format_analysis,
    get_spotify_info_extended,
    normalized_track_key,
    prepare_track_candidates,
    process_and_save_candidates,
)

CHECK_INTERVAL_MS = 800
DEFAULT_SAMPLE_RATE = 44100
DEFAULT_PRE_BUFFER_SEC = 3.0

ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("green")


class SpotifyRecorderApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Spotify Recorder V2")
        self.geometry("780x860")
        self.minsize(700, 800)

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
        self.save_dir = ctk.StringVar(value=os.path.expanduser("~/Desktop/Spotify_recodings"))
        self.record_ch_start = ctk.IntVar(value=5)
        self.silence_threshold_db = ctk.DoubleVar(value=-60.0)
        self.pad_start_sec = ctk.DoubleVar(value=0.10)
        self.pad_end_sec = ctk.DoubleVar(value=0.35)
        self.min_keep_sec = ctk.DoubleVar(value=12.0)
        self.discard_tail_under_sec = ctk.DoubleVar(value=45.0)
        self.discard_tail = ctk.BooleanVar(value=True)
        self.auto_stop_on_idle = ctk.BooleanVar(value=True)
        self.auto_stop_grace_sec = ctk.DoubleVar(value=3.0)
        self.record_mode = ctk.StringVar(value=MODE_ALBUM)

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
        self.load_past_logs()
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
        self.status_label.pack(pady=(8, 4))

        info = ctk.CTkFrame(root, fg_color="#1f2328", corner_radius=8)
        info.pack(fill="x", padx=18, pady=4)
        self.track_label = ctk.CTkLabel(info, text="Spotify: checking...", font=ctk.CTkFont(size=16, weight="bold"))
        self.track_label.pack(pady=(8, 2))
        self.artist_label = ctk.CTkLabel(info, text="-", text_color="#b7bec8")
        self.artist_label.pack(pady=(0, 8))

        meter_frame = ctk.CTkFrame(root, fg_color="transparent")
        meter_frame.pack(fill="x", padx=18, pady=2)
        ctk.CTkLabel(meter_frame, text="Input").pack(side="left")
        self.level_meter = ctk.CTkProgressBar(meter_frame, height=12, progress_color="#1DB954")
        self.level_meter.pack(side="left", fill="x", expand=True, padx=10)
        self.level_meter.set(0)

        # Recording mode and fixed quality profile
        controls_frame = ctk.CTkFrame(root, fg_color="transparent")
        controls_frame.pack(fill="x", padx=18, pady=2)

        ctk.CTkLabel(controls_frame, text="モード:").pack(side="left")
        self.mode_menu = ctk.CTkSegmentedButton(controls_frame, values=MODE_PRESETS, variable=self.record_mode)
        self.mode_menu.pack(side="left", padx=10)

        self.quality_label = ctk.CTkLabel(
            controls_frame,
            text="WAV 32-bit float / Unity Gain / LUFS解析のみ",
            fg_color="#252b33",
            corner_radius=5,
        )
        self.quality_label.pack(side="left", padx=(20, 0))

        switches = ctk.CTkFrame(root, fg_color="transparent")
        switches.pack(fill="x", padx=18, pady=4)
        self.standby_switch = ctk.CTkSwitch(
            switches,
            text="Standby: Spotify再生で自動開始",
            progress_color="#1DB954",
            command=self.on_standby_toggle,
        )
        self.standby_switch.pack(anchor="w", pady=2)
        self.auto_stop_switch = ctk.CTkSwitch(
            switches,
            text="Spotify停止/一時停止で自動停止",
            variable=self.auto_stop_on_idle,
            progress_color="#1DB954",
        )
        self.auto_stop_switch.pack(anchor="w", pady=2)
        self.discard_tail_switch = ctk.CTkSwitch(
            switches,
            text="停止時の短い最終断片を保存しない",
            variable=self.discard_tail,
            progress_color="#1DB954",
        )
        self.discard_tail_switch.pack(anchor="w", pady=2)

        buttons = ctk.CTkFrame(root, fg_color="transparent")
        buttons.pack(fill="x", padx=18, pady=6)
        self.start_btn = ctk.CTkButton(
            buttons,
            text="Start Recording",
            height=40,
            fg_color="#1DB954",
            hover_color="#1ed760",
            text_color="#06170c",
            font=ctk.CTkFont(weight="bold"),
            command=self.start_rec,
        )
        self.start_btn.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.stop_btn = ctk.CTkButton(
            buttons,
            text="Stop & Review",
            height=40,
            fg_color="#e91429",
            hover_color="#ff3348",
            state="disabled",
            command=self.stop_rec,
        )
        self.stop_btn.pack(side="left", fill="x", expand=True, padx=6)
        self.abort_btn = ctk.CTkButton(
            buttons,
            text="Abort",
            height=40,
            fg_color="#414852",
            hover_color="#59616d",
            state="disabled",
            command=self.abort_rec,
        )
        self.abort_btn.pack(side="left", fill="x", expand=True, padx=(6, 0))

        settings = ctk.CTkScrollableFrame(root, fg_color="#161a1f", corner_radius=8, height=220)
        settings.pack(fill="x", padx=18, pady=6)
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
        self.add_entry(settings, 1, "開始ch", self.record_ch_start, "Loopback等でのSpotify出力先ch")
        self.add_entry(settings, 2, "無音閾値 dB", self.silence_threshold_db, "例: -60")
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

        log_header = ctk.CTkFrame(root, fg_color="transparent")
        log_header.pack(fill="x", padx=18, pady=(4, 2))
        ctk.CTkLabel(log_header, text="Log", text_color="#b7bec8").pack(side="left")

        diag_btn = ctk.CTkButton(log_header, text="診断(Diagnostics)を実行", height=24, width=140, command=self.run_diagnostics, fg_color="#30363d", hover_color="#484f58")
        diag_btn.pack(side="right")
        self.log_box = ctk.CTkTextbox(
            root,
            height=160,
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
        self.mode_menu.configure(state=locked_state)
        for entry in self.setting_entries:
            entry.configure(state=locked_state)

    def log_message(self, message):
        self.log_queue.put(message)

    def process_queues(self):
        while not self.log_queue.empty():
            message = self.log_queue.get()
            self.log_box.configure(state="normal")
            formatted_msg = f"[{datetime.now().strftime('%H:%M:%S')}] {message}\n"
            self.log_box.insert("end", formatted_msg)
            self.log_box.see("end")
            self.log_box.configure(state="disabled")
            self.write_log_to_file(formatted_msg)

        self.level_meter.set(min(1.0, self.latest_level * 8.0))
        self.after(50, self.process_queues)

    def load_past_logs(self):
        log_dir = os.path.expanduser("~/Desktop/Spotify_recodings")
        os.makedirs(log_dir, exist_ok=True)
        self.log_file_path = os.path.join(log_dir, "spotify_recorder.log")

        past_log = ""
        if os.path.exists(self.log_file_path):
            try:
                with open(self.log_file_path, "r", encoding="utf-8") as f:
                    # 最後の1000行程度に制限するなどして肥大化対策（今回はシンプルに全部読み込み）
                    past_log = f.read()
            except Exception as e:
                past_log = f"[System Error] 過去のログ読み込み失敗: {e}\n"

        self.log_box.configure(state="normal")
        self.log_box.insert("end", past_log)
        self.log_box.insert("end", f"--- アプリ起動: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ---\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")
        self.write_log_to_file(f"--- アプリ起動: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ---\n")

    def write_log_to_file(self, formatted_msg):
        try:
            with open(self.log_file_path, "a", encoding="utf-8") as f:
                f.write(formatted_msg)
        except Exception:
            pass

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

    def run_diagnostics(self):
        self.log_message("=== 診断を実行中 ===")
        def diag_thread():
            lines = build_diagnostic_lines(self.sample_rate)
            for line in lines:
                self.log_message(line)
            self.log_message("=== 診断完了 ===")
        threading.Thread(target=diag_thread, daemon=True).start()

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
            self.log_message(
                f"Audio stream started: {self.sample_rate}Hz / ch {start_ch}-{start_ch + 1} / "
                "Unity Gain 1.0 / WAV 32-bit float"
            )
            if self.sample_rate != DEFAULT_SAMPLE_RATE:
                self.log_message(
                    f"品質警告: 入力は{self.sample_rate}Hzです。リサンプリングせず同じレートで保存します"
                )
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
            "key": normalized_track_key(info),
            "artwork_url": info.get("artwork_url")
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
                    "artwork_url": None
                }
            )
            self.status_label.configure(text="Recording", text_color="#e91429")
            self.log_message("録音開始: Spotifyメタデータ未取得")

        self.set_controls_recording(True)

    def stop_rec(self):
        if not self.is_recording:
            return

        stop_info = get_spotify_info_extended()
        if stop_info.get("status") != "OK":
            stop_info = self.current_spotify_info
        elif normalized_track_key(stop_info) != self.last_track_key and self.last_track_key is not None:
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
        self.status_label.configure(text="Reviewing Candidates...", text_color="#1DB954")
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
            "record_mode": self.record_mode.get(),
        }

        self.log_message(f"録音解析開始: {len(audio) / self.sample_rate:.1f}s")

        # 解析は別スレッドで行う
        def evaluate_and_show():
            try:
                candidates = prepare_track_candidates(audio, list(self.recording_history), options, stop_info, self.log_message)
                self.after(0, lambda: self.show_review_ui(candidates, options))
            except Exception as e:
                self.log_message(f"解析中にエラーが発生しました: {e}")
                self.after(0, self.on_processing_finished)

        threading.Thread(target=evaluate_and_show, daemon=True).start()

    def show_review_ui(self, candidates, options):
        review_win = ctk.CTkToplevel(self)
        review_win.title("Review Track Candidates")
        review_win.geometry("820x560")
        review_win.transient(self)
        review_win.grab_set()

        ctk.CTkLabel(review_win, text="保存候補レビュー", font=ctk.CTkFont(size=18, weight="bold")).pack(pady=10)

        scroll = ctk.CTkScrollableFrame(review_win)
        scroll.pack(fill="both", expand=True, padx=20, pady=10)

        check_vars = []
        for cand in candidates:
            frame = ctk.CTkFrame(scroll)
            frame.pack(fill="x", pady=4, padx=4)

            chk_var = ctk.BooleanVar(value=cand["default_checked"])
            check_vars.append(chk_var)

            chk = ctk.CTkCheckBox(frame, text="", variable=chk_var, width=30)
            chk.pack(side="left", padx=10)

            title = cand["track"].get("name", "Unknown")
            artist = cand["track"].get("artist", "Unknown")
            dur = cand["duration"]
            reason = cand["reason"]
            analysis = cand.get("analysis")

            text = f"{artist} - {title} ({dur:.1f}s)"
            if reason:
                text += f"  [⚠ {reason}]"
            if analysis:
                text += f"\n{format_analysis(analysis)}"
                if analysis["warnings"]:
                    text += "\n⚠ " + " / ".join(analysis["warnings"])

            if analysis and analysis["warnings"]:
                text_color = "#ff5964"
            else:
                text_color = "white" if cand["default_checked"] else "#808995"
            lbl = ctk.CTkLabel(frame, text=text, text_color=text_color, anchor="w", justify="left")
            lbl.pack(side="left", padx=5, pady=8)

        def on_confirm():
            for cand, var in zip(candidates, check_vars):
                cand["selected"] = var.get()

            review_win.destroy()
            self.status_label.configure(text="Processing & Saving...", text_color="#1DB954")

            threading.Thread(
                target=process_and_save_candidates,
                args=(candidates, options, self.log_message, self.on_processing_finished),
                daemon=True,
            ).start()

        def on_cancel():
            review_win.destroy()
            self.abort_rec()

        btn_frame = ctk.CTkFrame(review_win, fg_color="transparent")
        btn_frame.pack(fill="x", padx=20, pady=10)

        ctk.CTkButton(btn_frame, text="Confirm & Save", fg_color="#1DB954", hover_color="#1ed760", text_color="#000", command=on_confirm).pack(side="right", padx=10)
        ctk.CTkButton(btn_frame, text="Cancel & Discard", fg_color="#e91429", hover_color="#ff3348", command=on_cancel).pack(side="right")

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
        info = get_spotify_info_extended()
        self.current_spotify_info = info

        if info.get("status") == "OK":
            self.track_label.configure(text=info["name"], text_color="white")
            self.artist_label.configure(text=f"{info['artist']} - {info['album']} / {info['state']}", text_color="#b7bec8")

            if self.is_standby and not self.is_recording and info.get("state") == "playing":
                self.log_message(f"Spotify再生検知: {info['artist']} - {info['name']}")
                self.start_rec()

            if self.is_recording:
                # Track change auto-stop logic for SINGLE mode
                if self.record_mode.get() == MODE_SINGLE:
                    if self.last_track_key and normalized_track_key(info) != self.last_track_key:
                        self.log_message("単曲モード: 曲の切り替わりを検知し、録音を自動停止します。")
                        self.stop_rec()

            if self.is_recording and self.auto_stop_on_idle.get():
                if info.get("state") != "playing":
                    if self.spotify_idle_since is None:
                        self.spotify_idle_since = time.time()
                    elif time.time() - self.spotify_idle_since >= float(self.auto_stop_grace_sec.get()):
                        self.log_message("Spotify自動停止検知")
                        self.stop_rec()
                else:
                    self.spotify_idle_since = None

            if self.is_recording and info.get("state") == "playing":
                new_key = normalized_track_key(info)
                if new_key and new_key != self.last_track_key:
                    estimated_start = self.estimate_track_start_sample(info)
                    self.add_history_track(info, estimated_start)
                    self.log_message(f"曲変更: {info['artist']} - {info['name']}")

        elif info.get("status") in ("IDLE", "CLOSED"):
            if info.get("status") == "CLOSED":
                self.track_label.configure(text="Spotify is closed", text_color="#b7bec8")
            else:
                self.track_label.configure(text="Spotify is idle", text_color="#b7bec8")
            self.artist_label.configure(text="-", text_color="#b7bec8")

            if self.is_recording and self.auto_stop_on_idle.get():
                if self.spotify_idle_since is None:
                    self.spotify_idle_since = time.time()
                elif time.time() - self.spotify_idle_since >= float(self.auto_stop_grace_sec.get()):
                    self.log_message("Spotify終了検知、録音停止")
                    self.stop_rec()

        self.after(CHECK_INTERVAL_MS, self.poll_spotify)

    def on_closing(self):
        self.stop_audio_stream()
        self.destroy()


if __name__ == "__main__":
    app = SpotifyRecorderApp()
    app.mainloop()
