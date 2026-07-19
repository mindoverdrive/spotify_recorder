import json
import os
import queue
import subprocess
import sys
import threading
import time
from datetime import datetime
from tkinter import TclError, filedialog

import customtkinter as ctk
import numpy as np
import sounddevice as sd
import soundfile as sf

from capture_spool import (
    CaptureSpool,
    SpoolAudio,
    capture_blocksize,
    check_capture_disk_space,
    list_recoverable_sessions,
)
from coreaudio_devices import (
    SampleRateSyncResult,
    resolve_coreaudio_device,
    sync_nominal_sample_rate,
)
from library_converter import (
    LibraryConversionQueue,
    default_library_destination,
    library_codec_diagnostics,
    scan_library,
)
from recording_catalog import (
    default_catalog_path,
    list_audio_exports,
    list_library_assets,
    list_library_jobs,
    list_recordings,
)
from spotify_recorder_services import (
    MODE_PRESETS,
    MODE_ALBUM,
    MODE_SINGLE,
    MODE_MANUAL,
    build_diagnostic_lines,
    format_analysis,
    format_analysis_suspect_locations,
    normalized_track_key,
    prepare_track_candidates,
    process_and_save_candidates,
    retry_flac_export,
)
from spotify_quality_audit import (
    CaptureQualityAudit,
    format_audit_events,
    format_capture_audit,
    format_timecode,
)
from source_providers import (
    PROVIDER_QOBUZ,
    PROVIDER_SPOTIFY,
    PROVIDERS,
    create_provider_adapters,
    source_is_playing,
)

CHECK_INTERVAL_MS = 800
DEFAULT_SAMPLE_RATE = 44100
DEFAULT_PRE_BUFFER_SEC = 5.0

ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("green")


class HiResRecorderApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Hi-Res Recorder")
        self.geometry("860x940")
        self.minsize(760, 860)

        self.is_recording = False
        self.is_standby = False
        self.stream = None
        self.audio_lock = threading.Lock()
        self.capture_spool = None
        self.pending_spool_audio = None
        self.spool_failure_handled = False
        self.pre_chunks = []
        self.pre_samples = 0
        self.total_samples_recorded = 0
        self.recording_history = []
        self.current_source_info = None
        self.last_track_key = None
        self.source_idle_since = None
        self.latest_level = 0.0
        self._stream_start_ch = 1
        self.spotify_quality_settings = {"available": False}
        self.capture_quality_audit = None
        self.rate_sync_lock = threading.Lock()
        self.rate_sync_in_progress = False
        self.pending_record_start = False
        self.last_rate_sync = None
        self.device_state_unavailable_reported = False
        self.provider_adapters = create_provider_adapters()
        self.catalog_path = default_catalog_path()
        self.library_queue = LibraryConversionQueue(
            self.catalog_path,
            event_callback=self.on_library_conversion_event,
        )

        self.log_queue = queue.Queue()
        self.ui_queue = queue.Queue()
        self.save_dir = ctk.StringVar(value=os.path.expanduser("~/Music/Hi-Res Recorder"))
        self.provider = ctk.StringVar(value=PROVIDER_SPOTIFY)
        self.maximum_recording_minutes = ctk.IntVar(value=120)
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
        self.library_source_dir = ctk.StringVar(value="")
        self.library_destination_dir = ctk.StringVar(
            value=default_library_destination()
        )
        self.current_library_job_id = None

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
        self.refresh_source_quality_status(log_result=True)
        self.recoverable_sessions = list_recoverable_sessions()
        if self.recoverable_sessions:
            self.log_message(
                f"前回の録音スプールが{len(self.recoverable_sessions)}件あります。復旧確認を表示します。"
            )
            self.after(300, self.show_recovery_prompt)
        self.after(CHECK_INTERVAL_MS, self.poll_source)
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
            coreaudio = resolve_coreaudio_device(info["name"], self.audio_devices)
            self.sample_rate = int(
                round(coreaudio.nominal_sample_rate)
                or info.get("default_samplerate", DEFAULT_SAMPLE_RATE)
            )
        except Exception:
            try:
                info = sd.query_devices(self.device_in_id, "input")
                self.sample_rate = int(
                    info.get("default_samplerate", DEFAULT_SAMPLE_RATE)
                )
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

        source_controls = ctk.CTkFrame(root, fg_color="transparent")
        source_controls.pack(fill="x", padx=18, pady=2)
        ctk.CTkLabel(source_controls, text="サービス").pack(side="left")
        self.provider_menu = ctk.CTkSegmentedButton(
            source_controls,
            values=PROVIDERS,
            variable=self.provider,
            command=self.on_provider_change,
        )
        self.provider_menu.pack(side="left", padx=(8, 18))

        info = ctk.CTkFrame(root, fg_color="#1f2328", corner_radius=8)
        info.pack(fill="x", padx=18, pady=4)
        self.track_label = ctk.CTkLabel(info, text="ソース確認中...", font=ctk.CTkFont(size=16, weight="bold"))
        self.track_label.pack(pady=(8, 2))
        self.artist_label = ctk.CTkLabel(info, text="-", text_color="#b7bec8")
        self.artist_label.pack(pady=(0, 2))
        self.source_quality_label = ctk.CTkLabel(
            info,
            text="ソース品質: 未監査",
            text_color="#f0b429",
            wraplength=720,
        )
        self.source_quality_label.pack(pady=(0, 8), padx=10)
        self.rate_sync_label = ctk.CTkLabel(
            info,
            text="入力レート: 確認中",
            text_color="#f0b429",
            wraplength=720,
        )
        self.rate_sync_label.pack(pady=(0, 8), padx=10)

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
            text="Archive: native-rate 24-bit / DJ: 24-bit 48kHz / conditional dither",
            fg_color="#252b33",
            corner_radius=5,
        )
        self.quality_label.pack(side="left", padx=(20, 0))

        switches = ctk.CTkFrame(root, fg_color="transparent")
        switches.pack(fill="x", padx=18, pady=4)
        self.automation_provider_label = ctk.CTkLabel(
            switches,
            text="監視対象: Spotify Offline",
            text_color="#63f08f",
        )
        self.automation_provider_label.pack(anchor="w", pady=(0, 2))
        self.standby_switch = ctk.CTkSwitch(
            switches,
            text="Standby (Spotify / Qobuz): 選択中サービスの再生で自動開始",
            progress_color="#1DB954",
            command=self.on_standby_toggle,
        )
        self.standby_switch.pack(anchor="w", pady=2)
        self.auto_stop_switch = ctk.CTkSwitch(
            switches,
            text="自動停止 (Spotify / Qobuz): 選択中サービスの停止・一時停止を検知",
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
        self.add_entry(settings, 1, "開始ch", self.record_ch_start, "単一Loopback/BlackHoleの出力先ch")
        self.add_entry(settings, 2, "無音閾値 dB", self.silence_threshold_db, "例: -60")
        self.add_entry(settings, 3, "開始余白 秒", self.pad_start_sec, "フェードイン保護")
        self.add_entry(settings, 4, "終了余白 秒", self.pad_end_sec, "余韻保護")
        self.add_entry(settings, 5, "通常スキップ 秒", self.min_keep_sec, "これ未満の曲候補は保存しない")
        self.add_entry(settings, 6, "最終断片 秒", self.discard_tail_under_sec, "停止時の最終曲がこれ以下なら破棄")
        self.add_entry(settings, 7, "自動停止猶予 秒", self.auto_stop_grace_sec, "ソース停止後に待つ秒数")
        self.add_entry(settings, 8, "最大録音 分", self.maximum_recording_minutes, "空き容量の事前検査に使用")
        ctk.CTkLabel(settings, text="保存先").grid(row=9, column=0, sticky="w", padx=12, pady=8)
        dir_frame = ctk.CTkFrame(settings, fg_color="transparent")
        dir_frame.grid(row=9, column=1, sticky="ew", padx=12, pady=8)
        dir_frame.grid_columnconfigure(0, weight=1)
        self.dir_label = ctk.CTkLabel(dir_frame, textvariable=self.save_dir, anchor="w", fg_color="#252b33", corner_radius=5)
        self.dir_label.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.browse_btn = ctk.CTkButton(dir_frame, text="Browse", width=78, command=self.browse_dir)
        self.browse_btn.grid(row=0, column=1)
        
        log_header = ctk.CTkFrame(root, fg_color="transparent")
        log_header.pack(fill="x", padx=18, pady=(4, 2))
        ctk.CTkLabel(log_header, text="Log", text_color="#b7bec8").pack(side="left")
        self.history_btn = ctk.CTkButton(
            log_header,
            text="録音/出力",
            height=24,
            width=82,
            command=self.show_recording_history,
            fg_color="#30363d",
            hover_color="#484f58",
        )
        self.history_btn.pack(side="left", padx=(8, 0))
        
        self.diag_btn = ctk.CTkButton(
            log_header,
            text="品質診断",
            height=24,
            width=92,
            command=self.run_diagnostics,
            fg_color="#30363d",
            hover_color="#484f58",
        )
        self.diag_btn.pack(side="right", padx=(0, 6))
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
        self.on_provider_change(self.provider.get(), refresh=False)

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
        self.provider_menu.configure(state=locked_state)
        self.diag_btn.configure(state=locked_state)
        for entry in self.setting_entries:
            entry.configure(state=locked_state)

    def log_message(self, message):
        self.log_queue.put(message)

    def process_queues(self):
        while not self.ui_queue.empty():
            callback = self.ui_queue.get()
            try:
                callback()
            except TclError:
                # 終了処理中に残ったUI更新は無視する。
                pass
            except Exception as exc:
                self.log_message(f"UI更新エラー: {exc}")

        while not self.log_queue.empty():
            message = self.log_queue.get()
            self.log_box.configure(state="normal")
            formatted_msg = f"[{datetime.now().strftime('%H:%M:%S')}] {message}\n"
            self.log_box.insert("end", formatted_msg)
            self.log_box.see("end")
            self.log_box.configure(state="disabled")
            self.write_log_to_file(formatted_msg)

        self.level_meter.set(min(1.0, self.latest_level * 8.0))
        if (
            self.is_recording
            and self.capture_spool is not None
            and self.capture_spool.error
            and not self.spool_failure_handled
        ):
            self.spool_failure_handled = True
            self.log_message(f"重大録音エラー: {self.capture_spool.error}")
            if self.capture_quality_audit is not None:
                self.capture_quality_audit.add_external_event(
                    "spool_overflow",
                    self.capture_spool.error,
                )
            self.stop_rec()
        self.after(50, self.process_queues)

    def load_past_logs(self):
        log_dir = os.path.expanduser("~/Library/Application Support/HiResRecorder/Logs")
        os.makedirs(log_dir, exist_ok=True)
        self.log_file_path = os.path.join(log_dir, "hires_recorder.log")
        
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

    def show_recovery_prompt(self):
        if not self.recoverable_sessions:
            return
        window = ctk.CTkToplevel(self)
        window.title("録音スプールの復旧")
        window.geometry("520x230")
        window.transient(self)
        window.grab_set()
        latest = self.recoverable_sessions[-1]
        duration = (
            int(latest.get("frames", 0)) / int(latest.get("sample_rate") or 1)
        )
        ctk.CTkLabel(
            window,
            text="前回終了時に未処理の録音があります",
            font=ctk.CTkFont(size=17, weight="bold"),
        ).pack(pady=(22, 8))
        ctk.CTkLabel(
            window,
            text=(
                f"{len(self.recoverable_sessions)}件 / 最新 {duration:.1f}秒 / "
                f"{latest.get('sample_rate')}Hz\n元データを変更せず、全体WAV候補としてレビューします。"
            ),
            text_color="#b7bec8",
        ).pack(padx=20)
        buttons = ctk.CTkFrame(window, fg_color="transparent")
        buttons.pack(fill="x", padx=22, pady=22)

        def recover():
            window.destroy()
            self.recover_spool(latest)

        def discard_all():
            for payload in self.recoverable_sessions:
                for path in (payload.get("raw_path"), payload.get("metadata_path")):
                    if path:
                        try:
                            os.remove(path)
                        except FileNotFoundError:
                            pass
            self.recoverable_sessions = []
            window.destroy()
            self.log_message("残存録音スプールを破棄しました。")

        ctk.CTkButton(buttons, text="最新を復旧", command=recover).pack(side="right", padx=5)
        ctk.CTkButton(
            buttons,
            text="すべて破棄",
            fg_color="#e91429",
            hover_color="#ff3348",
            command=discard_all,
        ).pack(side="right", padx=5)
        ctk.CTkButton(
            buttons,
            text="後で",
            fg_color="#414852",
            command=window.destroy,
        ).pack(side="right", padx=5)

    def recover_spool(self, payload):
        if self.is_recording or self.pending_spool_audio is not None:
            self.log_message("録音中またはレビュー中のため復旧できません。")
            return
        frames = int(payload.get("frames") or 0)
        if frames <= 0:
            self.log_message("復旧対象に音声フレームがありません。")
            return
        audio = SpoolAudio(
            payload["raw_path"],
            payload["metadata_path"],
            int(payload["sample_rate"]),
            int(payload.get("channels") or 2),
            frames,
        )
        self.pending_spool_audio = audio
        sample_rate = int(payload["sample_rate"])
        history = [
            {
                "name": datetime.now().strftime("Recovered_%Y%m%d_%H%M%S"),
                "artist": "Unknown",
                "album": "Recovered Capture",
                "start_sample": 0,
                "key": None,
                "artwork_url": None,
                "provider": "unknown",
                "source_mode": "recovered",
            }
        ]
        options = {
            "save_dir": self.save_dir.get(),
            "sample_rate": sample_rate,
            "threshold_db": float(self.silence_threshold_db.get()),
            "pad_start_sec": float(self.pad_start_sec.get()),
            "pad_end_sec": float(self.pad_end_sec.get()),
            "min_keep_sec": 0.0,
            "discard_tail": False,
            "discard_tail_under_sec": 0.0,
            "record_mode": MODE_MANUAL,
            "capture_audit": None,
            "catalog_path": self.catalog_path,
            "audio_source": audio,
        }
        self.status_label.configure(text="Recovering Capture...", text_color="#f0b429")

        def evaluate():
            try:
                candidates = prepare_track_candidates(
                    audio, history, options, None, self.log_message
                )
                self.after(0, lambda: self.show_review_ui(candidates, options))
            except Exception as exc:
                self.log_message(f"録音スプール復旧エラー: {exc}")
                self.after(0, self.on_processing_finished)

        threading.Thread(target=evaluate, daemon=True).start()

    def on_device_change(self, _):
        if not self.input_device_strings:
            return
        self.device_in_id = int(self.in_combo.get().split(":", 1)[0])
        self.update_sample_rate()
        self.log_message(f"入力デバイス: {self.device_in_id} / {self.sample_rate}Hz")
        self.refresh_source_quality_status()
        snapshot = self.current_source_info or {"state": "stopped"}
        self.consider_idle_sample_rate_sync(snapshot)

    def current_adapter(self):
        return self.provider_adapters[self.provider.get()]

    def on_provider_change(self, value, refresh=True):
        self.automation_provider_label.configure(text=f"監視対象: {value} Offline")
        self.current_source_info = None
        self.source_idle_since = None
        self.last_track_key = None
        if self.is_standby:
            with self.audio_lock:
                self.pre_chunks = []
                self.pre_samples = 0
            self.log_message(f"Standby監視対象を{value} Offlineへ変更しました。")
        if refresh:
            self.refresh_source_quality_status(log_result=True)
        snapshot = self.current_source_info or {"state": "stopped"}
        self.after(0, lambda: self.consider_idle_sample_rate_sync(snapshot))

    def current_device_profile(self):
        if self.device_in_id is None:
            raise RuntimeError("入力デバイスがありません")
        device_info = sd.query_devices(self.device_in_id, "input")
        coreaudio = resolve_coreaudio_device(device_info["name"], self.audio_devices)
        profile = coreaudio.to_dict()
        profile["max_input_channels"] = int(device_info["max_input_channels"])
        return profile

    def desired_capture_sample_rate(self, snapshot=None):
        try:
            return self.current_adapter().desired_sample_rate(snapshot)
        except Exception:
            return None

    def _set_rate_sync_status(self, text, color="#f0b429"):
        if hasattr(self, "rate_sync_label"):
            self.rate_sync_label.configure(text=f"入力レート: {text}", text_color=color)

    def request_sample_rate_sync(
        self,
        snapshot=None,
        reopen_standby=False,
        start_recording_after=False,
    ):
        if self.is_recording or self.rate_sync_in_progress:
            return False
        target = self.desired_capture_sample_rate(snapshot)
        if not target:
            self._set_rate_sync_status("ソースレート未検証")
            return False
        try:
            profile = self.current_device_profile()
        except Exception as exc:
            self._set_rate_sync_status(f"デバイス確認失敗: {exc}", "#ff5964")
            return False
        before = int(round(float(profile.get("nominal_sample_rate") or 0)))
        if before == int(target):
            try:
                result = sync_nominal_sample_rate(profile, int(target))
            except Exception as exc:
                result = SampleRateSyncResult(
                    device_id=int(profile["device_id"]),
                    device_uid=profile.get("uid"),
                    device_name=profile["name"],
                    target_rate=int(target),
                    rate_before=before,
                    rate_after=before,
                    supported=False,
                    changed=False,
                    verified=False,
                    reason=str(exc),
                )
            self.last_rate_sync = result.to_dict()
            if result.verified:
                self.sample_rate = before
                self._set_rate_sync_status(
                    f"同期済み {self.provider.get()} {target}Hz = {profile['name']} {before}Hz",
                    "#63f08f",
                )
                return True
            self._set_rate_sync_status(f"検証失敗: {result.reason}", "#ff5964")
            self.log_message(f"CoreAudioレート検証失敗: {result.reason}")
            return False
        if source_is_playing(snapshot or {}):
            self._set_rate_sync_status(
                f"待機 {self.provider.get()} {target}Hz / CoreAudio {before}Hz",
                "#ff5964",
            )
            self.log_message(
                f"レート不一致: 再生中は変更しません ({target}Hz / {before}Hz)。"
                "停止または一時停止後に自動同期し、曲頭から再生してください。"
            )
            return False
        with self.rate_sync_lock:
            if self.rate_sync_in_progress:
                return False
            self.rate_sync_in_progress = True
        self.pending_record_start = bool(start_recording_after)
        had_stream = bool(self.stream)
        if had_stream:
            self.stop_audio_stream()
        with self.audio_lock:
            self.pre_chunks = []
            self.pre_samples = 0
        self._set_rate_sync_status(f"同期中 {before}Hz -> {target}Hz", "#58a6ff")
        self.log_message(
            f"CoreAudioレート自動同期開始: {profile['name']} {before}Hz -> {target}Hz"
        )

        def worker():
            try:
                result = sync_nominal_sample_rate(profile, int(target))
                if result.verified:
                    sd.check_input_settings(
                        device=self.device_in_id,
                        channels=2,
                        dtype="float32",
                        samplerate=int(target),
                    )
            except Exception as exc:
                result = SampleRateSyncResult(
                    device_id=int(profile["device_id"]),
                    device_uid=profile.get("uid"),
                    device_name=profile["name"],
                    target_rate=int(target),
                    rate_before=before,
                    rate_after=before,
                    supported=False,
                    changed=False,
                    verified=False,
                    reason=str(exc),
                )

            def finish():
                self.last_rate_sync = result.to_dict()
                self.rate_sync_in_progress = False
                if result.verified:
                    self.sample_rate = int(result.rate_after)
                    self._set_rate_sync_status(
                        f"同期済み {result.device_name} {result.rate_after}Hz",
                        "#63f08f",
                    )
                    self.log_message(
                        f"CoreAudioレート自動同期完了: {result.rate_before}Hz -> "
                        f"{result.rate_after}Hz / {result.reason}"
                    )
                else:
                    self._set_rate_sync_status(
                        f"同期失敗: {result.reason}", "#ff5964"
                    )
                    self.log_message(f"CoreAudioレート自動同期失敗: {result.reason}")
                should_reopen = (
                    result.verified
                    and self.is_standby
                    and not self.is_recording
                    and (reopen_standby or had_stream)
                )
                if should_reopen:
                    self.start_audio_stream()
                pending = self.pending_record_start
                self.pending_record_start = False
                if pending and result.verified:
                    self.start_rec()

            self.after(0, finish)

        threading.Thread(target=worker, daemon=True).start()
        return False

    def ensure_sample_rate_before_recording(self):
        snapshot = self.current_adapter().snapshot()
        self.current_source_info = snapshot
        target = self.desired_capture_sample_rate(snapshot)
        if not target:
            return True
        try:
            current = int(
                round(float(self.current_device_profile()["nominal_sample_rate"]))
            )
        except Exception as exc:
            self._set_rate_sync_status(f"デバイス確認失敗: {exc}", "#ff5964")
            self.log_message(f"録音開始拒否: CoreAudio入力を検証できません: {exc}")
            return False
        if current == int(target):
            return self.request_sample_rate_sync(snapshot)
        if source_is_playing(snapshot):
            self.request_sample_rate_sync(snapshot)
            return False
        self.request_sample_rate_sync(
            snapshot,
            reopen_standby=self.is_standby,
            start_recording_after=True,
        )
        return False

    def consider_idle_sample_rate_sync(self, snapshot):
        if self.is_recording or self.rate_sync_in_progress or source_is_playing(snapshot):
            return
        target = self.desired_capture_sample_rate(snapshot)
        if not target:
            return
        try:
            current = int(
                round(float(self.current_device_profile()["nominal_sample_rate"]))
            )
        except Exception:
            return
        if current != int(target):
            self.request_sample_rate_sync(
                snapshot,
                reopen_standby=self.is_standby,
            )
        else:
            self.request_sample_rate_sync(snapshot)

    def source_preflight(self):
        adapter = self.current_adapter()
        try:
            device = self.current_device_profile()
        except Exception as exc:
            return {
                "conditions_pass": False,
                "warnings": [str(exc)],
                "assurance_label": f"{self.provider.get()}録音経路を検証できません",
                "source_sample_rate": None,
            }
        result = adapter.preflight(device)
        evidence = result.setdefault("evidence", {})
        if self.last_rate_sync:
            evidence["rate_sync"] = dict(self.last_rate_sync)
        return result

    def browse_dir(self):
        folder = filedialog.askdirectory(initialdir=self.save_dir.get())
        if folder:
            self.save_dir.set(folder)
            
    def run_diagnostics(self):
        self.log_message("=== 診断を実行中 ===")
        adapter = self.current_adapter()
        sample_rate = self.sample_rate

        def diag_thread():
            device = None
            try:
                device = self.current_device_profile()
            except Exception as exc:
                self.log_message(f"CoreAudio診断: {exc}")
            lines = adapter.diagnostics(
                sample_rate,
                device=device,
            )
            for line in lines:
                self.log_message(line)
            self.after(0, self.refresh_source_quality_status)
            self.log_message("=== 診断完了 ===")
        threading.Thread(target=diag_thread, daemon=True).start()

    def refresh_source_quality_status(self, log_result=False):
        status = self.current_adapter().quality_status()
        self.spotify_quality_settings = status.settings
        self.source_quality_label.configure(
            text=f"ソース品質: {status.label}",
            text_color="#63f08f" if status.conditions_pass else "#f0b429",
        )
        if log_result:
            self.log_message(f"{self.provider.get()}設定監査: {self.current_adapter().format_quality(status)}")
            for warning in status.warnings:
                self.log_message(f"設定警告: {warning}")
        return self.spotify_quality_settings

    def on_standby_toggle(self):
        self.is_standby = self.standby_switch.get() == 1
        if self.is_standby:
            snapshot = self.current_adapter().snapshot()
            self.current_source_info = snapshot
            target = self.desired_capture_sample_rate(snapshot)
            try:
                current = int(
                    round(float(self.current_device_profile()["nominal_sample_rate"]))
                )
            except Exception:
                current = self.sample_rate
            if target and current != int(target):
                if source_is_playing(snapshot):
                    self.request_sample_rate_sync(snapshot, reopen_standby=True)
                    self.status_label.configure(
                        text="Rate Sync Waiting", text_color="#f0b429"
                    )
                else:
                    self.request_sample_rate_sync(snapshot, reopen_standby=True)
                    self.status_label.configure(
                        text="Syncing Input Rate", text_color="#58a6ff"
                    )
                self.log_message(
                    f"Standbyは入力レート{target}Hzへの同期完了後に開始します。"
                )
                return
            if self.start_audio_stream():
                self.status_label.configure(text="Standby", text_color="#f0b429")
                self.pre_chunks = []
                self.pre_samples = 0
                self.log_message(f"Standby有効: {self.provider.get()}の再生を待機します。")
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

        if self.is_recording and self.capture_quality_audit is not None:
            self.capture_quality_audit.record_audio_callback(
                frames,
                status,
                stereo,
                adc_time=getattr(time_info, "inputBufferAdcTime", None),
            )

        with self.audio_lock:
            if self.is_recording:
                if self.capture_spool is not None and self.capture_spool.try_write(stereo):
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
                blocksize=capture_blocksize(self.sample_rate),
                latency="high",
                callback=self.audio_callback,
            )
            self.stream.start()
            self.log_message(
                f"Audio stream started: {self.sample_rate}Hz / ch {start_ch}-{start_ch + 1} / "
                "Unity Gain 1.0 / Archive native-rate + DJ 24-bit 48kHz"
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
            "artwork_url": info.get("artwork_url"),
            "provider": str(info.get("provider") or self.provider.get()).lower(),
            "source_mode": "offline",
            "track_id": info.get("track_id"),
            "source_sample_rate": info.get("source_sample_rate"),
            "source_bit_depth": info.get("source_bit_depth"),
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
        if self.rate_sync_in_progress:
            self.log_message("入力レート同期中のため録音開始を待機します。")
            return
        if not self.ensure_sample_rate_before_recording():
            return
        self.update_sample_rate()
        try:
            maximum_minutes = int(self.maximum_recording_minutes.get())
            if maximum_minutes <= 0:
                raise ValueError("最大録音時間は1分以上にしてください")
            source_evaluation = self.source_preflight()
        except (TclError, TypeError, ValueError) as exc:
            self.log_message(f"録音設定エラー: {exc}")
            return
        if not source_evaluation["conditions_pass"]:
            self.log_message(f"{self.provider.get()} Offline品質ゲートにより録音開始を拒否しました。")
            for warning in source_evaluation.get("warnings", []):
                self.log_message(f"開始拒否: {warning}")
            return
        disk = check_capture_disk_space(
            os.path.expanduser("~/Library/Caches/HiResRecorder/Sessions"),
            self.save_dir.get(),
            self.sample_rate,
            maximum_minutes,
        )
        if not disk["ok"]:
            self.log_message(
                "録音開始拒否: スプール、SRC一時領域、Archive/DJ出力用の空き容量が不足しています "
                f"(必要 {disk['required_bytes'] / 1024**3:.1f} GiB)"
            )
            return
        if not self.stream and not self.start_audio_stream():
            return

        with self.audio_lock:
            pre_roll = []
            if self.is_standby and self.pre_chunks:
                pre_roll = [chunk.copy() for chunk in self.pre_chunks]
                self.total_samples_recorded = self.pre_samples
                self.log_message(f"事前バッファ追加: {self.pre_samples / self.sample_rate:.2f}s")
            else:
                self.total_samples_recorded = 0
            self.pre_chunks = []
            self.pre_samples = 0
            self.capture_spool = CaptureSpool(
                self.sample_rate,
                channels=2,
                blocksize=capture_blocksize(self.sample_rate),
            )
            try:
                self.capture_spool.start(pre_roll)
            except Exception as exc:
                self.capture_spool.discard()
                self.capture_spool = None
                self.log_message(f"録音スプール開始エラー: {exc}")
                return
            self.spool_failure_handled = False
            self.is_recording = True

        settings = self.refresh_source_quality_status()
        try:
            device_name = sd.query_devices(self.device_in_id, "input")["name"]
        except Exception:
            device_name = f"Device {self.device_in_id}"
        self.capture_quality_audit = CaptureQualityAudit(
            self.sample_rate,
            device_name,
            settings,
            recording_frame_offset=self.total_samples_recorded,
            provider=self.provider.get().lower(),
            source_evaluation=source_evaluation,
        )
        self.device_state_unavailable_reported = False
        self.log_message(f"録音監査開始: {source_evaluation['assurance_label']}")

        self.source_idle_since = None
        self.recording_history = []
        self.last_track_key = None
        if self.current_source_info and self.current_source_info.get("status") == "OK":
            self.add_history_track(self.current_source_info, 0)
            self.status_label.configure(
                text=f"Recording: {self.current_source_info['name']}",
                text_color="#e91429",
            )
            self.log_message(f"録音開始: {self.current_source_info['artist']} - {self.current_source_info['name']}")
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
            self.log_message(f"録音開始: {self.provider.get()}メタデータ未取得")

        self.set_controls_recording(True)

    def stop_rec(self):
        if not self.is_recording:
            return

        stop_info = self.current_adapter().snapshot()
        if stop_info.get("status") != "OK":
            stop_info = self.current_source_info
        elif normalized_track_key(stop_info) != self.last_track_key and self.last_track_key is not None:
            estimated_start = self.estimate_track_start_sample(stop_info)
            self.add_history_track(stop_info, estimated_start)
            self.log_message(
                "停止時に未検知の曲変更を補足: "
                f"{stop_info['artist']} - {stop_info['name']} / start {estimated_start / self.sample_rate:.2f}s"
            )
        self.was_standby = self.is_standby  # スタンドバイ状態を記憶
        self.is_recording = False
        self.is_standby = False
        self.standby_switch.deselect()
        self.stop_audio_stream()
        capture_ended_at = time.time()
        capture_ended_monotonic = time.monotonic()
        spool = self.capture_spool
        self.capture_spool = None
        spool_audio = None
        spool_error = None
        if spool is not None:
            try:
                spool_audio = spool.stop()
                spool_error = spool.error
            except Exception as exc:
                spool_error = str(exc)
        if spool_error and self.capture_quality_audit is not None:
            self.capture_quality_audit.add_external_event("spool_overflow", spool_error)
        capture_audit = (
            self.capture_quality_audit.finish(
                ended_at=capture_ended_at,
                ended_monotonic=capture_ended_monotonic,
            )
            if self.capture_quality_audit is not None
            else None
        )
        self.capture_quality_audit = None
        if capture_audit:
            self.log_message(f"録音監査結果: {format_capture_audit(capture_audit)}")
            for warning in capture_audit["warnings"]:
                self.log_message(f"録音監査警告: {warning}")
        self.status_label.configure(text="Reviewing Candidates...", text_color="#1DB954")
        self.stop_btn.configure(state="disabled")
        self.abort_btn.configure(state="disabled")

        if spool_audio is None:
            self.log_message(f"録音データを確定できません: {spool_error or 'データなし'}")
            self.on_processing_finished()
            return

        audio = spool_audio
        self.pending_spool_audio = spool_audio
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
            "capture_audit": capture_audit,
            "catalog_path": self.catalog_path,
            "audio_source": spool_audio,
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

        ctk.CTkLabel(review_win, text="保存候補レビュー", font=ctk.CTkFont(size=18, weight="bold")).pack(pady=(10, 4))
        capture_audit = options.get("capture_audit")
        if capture_audit:
            ctk.CTkLabel(
                review_win,
                text=format_capture_audit(capture_audit),
                text_color="#63f08f" if capture_audit["quality_gate_pass"] else "#ff5964",
                wraplength=780,
            ).pack(padx=20, pady=(0, 6))
        
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
                    audio_locations = format_analysis_suspect_locations(analysis)
                    if audio_locations:
                        text += "\n音声疑い箇所:\n" + "\n".join(audio_locations)
            audit_events = format_audit_events(
                capture_audit,
                cand["start"] + cand["trim_start"],
                cand["start"] + cand["trim_end"],
            )
            if audit_events:
                text += "\n疑い箇所:\n" + "\n".join(audit_events)
            
            if (analysis and analysis["warnings"]) or (
                capture_audit and not capture_audit.get("quality_gate_pass")
            ):
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

        review_win.protocol("WM_DELETE_WINDOW", on_cancel)

        btn_frame = ctk.CTkFrame(review_win, fg_color="transparent")
        btn_frame.pack(fill="x", padx=20, pady=10)
        
        ctk.CTkButton(btn_frame, text="Confirm & Save", fg_color="#1DB954", hover_color="#1ed760", text_color="#000", command=on_confirm).pack(side="right", padx=10)
        ctk.CTkButton(btn_frame, text="Cancel & Discard", fg_color="#e91429", hover_color="#ff3348", command=on_cancel).pack(side="right")

    def abort_rec(self):
        if not self.is_recording and self.pending_spool_audio is None:
            return
        self.was_standby = self.is_standby  # スタンドバイ状態を記憶
        self.is_recording = False
        self.is_standby = False
        self.standby_switch.deselect()
        self.stop_audio_stream()
        self.capture_quality_audit = None
        if self.capture_spool is not None:
            self.capture_spool.discard()
            self.capture_spool = None
        if self.pending_spool_audio is not None:
            self.pending_spool_audio.close(delete=True)
            self.pending_spool_audio = None
        with self.audio_lock:
            self.pre_chunks = []
            self.pre_samples = 0
        self.log_message("録音を破棄しました。")
        self.on_processing_finished()

    def show_recording_history(self):
        history_win = ctk.CTkToplevel(self)
        history_win.title("録音・出力管理")
        history_win.geometry("980x700")
        history_win.minsize(820, 560)
        history_win.transient(self)

        tabs = ctk.CTkTabview(history_win)
        tabs.pack(fill="both", expand=True, padx=12, pady=12)
        recordings_tab = tabs.add("録音履歴")
        flac_tab = tabs.add("FLAC出力")
        library_tab = tabs.add("ライブラリ変換")
        self.build_library_conversion_tab(library_tab)

        toolbar = ctk.CTkFrame(recordings_tab, fg_color="transparent")
        toolbar.pack(fill="x", padx=10, pady=(8, 8))
        query_var = ctk.StringVar()
        provider_filter = ctk.StringVar(value="すべて")
        verdict_filter = ctk.StringVar(value="すべて")
        search_entry = ctk.CTkEntry(
            toolbar,
            textvariable=query_var,
            placeholder_text="曲名・アーティスト・アルバム・ファイル名",
        )
        search_entry.pack(side="left", fill="x", expand=True)
        count_label = ctk.CTkLabel(toolbar, text="")
        count_label.pack(side="right", padx=(10, 0))

        filter_bar = ctk.CTkFrame(recordings_tab, fg_color="transparent")
        filter_bar.pack(fill="x", padx=10, pady=(0, 8))
        ctk.CTkLabel(filter_bar, text="サービス").pack(side="left")
        ctk.CTkSegmentedButton(
            filter_bar,
            values=["すべて", PROVIDER_SPOTIFY, PROVIDER_QOBUZ],
            variable=provider_filter,
            command=lambda _value: render(),
        ).pack(side="left", padx=8)
        ctk.CTkLabel(filter_bar, text="判定").pack(side="left", padx=(14, 0))
        ctk.CTkSegmentedButton(
            filter_bar,
            values=["すべて", "合格", "要再録"],
            variable=verdict_filter,
            command=lambda _value: render(),
        ).pack(side="left", padx=8)

        scroll = ctk.CTkScrollableFrame(recordings_tab, fg_color="#11151a")
        scroll.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        def open_recording(path):
            if os.path.isfile(path):
                subprocess.Popen(["open", path])

        def reveal_recording(path):
            if os.path.isfile(path):
                subprocess.Popen(["open", "-R", path])

        def preview_suspect(item):
            if self.is_recording or not item.get("file_exists"):
                return
            events = (item.get("suspect_events") or {}).get("capture", [])
            if not events:
                return
            start_sec = max(0.0, float(events[0].get("time_sec", 0.0)) - 2.0)
            try:
                with sf.SoundFile(item["file_path"]) as audio_file:
                    audio_file.seek(int(start_sec * audio_file.samplerate))
                    audio = audio_file.read(
                        frames=int(8.0 * audio_file.samplerate),
                        dtype="float32",
                        always_2d=True,
                    )
                    sd.play(audio, audio_file.samplerate)
            except Exception as exc:
                self.log_message(f"疑義箇所の試聴エラー: {exc}")

        def suspect_lines(item):
            lines = []
            suspect = item.get("suspect_events") or {}
            for event in suspect.get("capture", [])[:4]:
                lines.append(
                    f"{format_timecode(event.get('time_sec', 0.0))}: "
                    f"{event.get('detail', event.get('type', '異常疑い'))}"
                )
            for event in suspect.get("full_scale_ranges", [])[:4]:
                lines.append(f"{format_timecode(event.get('start_sec', 0.0))}: 0 dBFS到達")
            return lines[:6]

        def render(_event=None):
            for child in scroll.winfo_children():
                child.destroy()
            try:
                selected_provider = provider_filter.get()
                selected_verdict = verdict_filter.get()
                recordings = list_recordings(
                    query_var.get(),
                    database_path=self.catalog_path,
                    provider=(
                        selected_provider.lower()
                        if selected_provider != "すべて"
                        else None
                    ),
                    requires_rerecord=(
                        True
                        if selected_verdict == "要再録"
                        else False if selected_verdict == "合格" else None
                    ),
                )
            except Exception as exc:
                count_label.configure(text="読込エラー")
                ctk.CTkLabel(scroll, text=str(exc), text_color="#ff5964").pack(pady=20)
                return
            count_label.configure(text=f"{len(recordings)}件")
            if not recordings:
                ctk.CTkLabel(scroll, text="該当する録音はありません", text_color="#808995").pack(pady=24)
                return

            for item in recordings:
                row = ctk.CTkFrame(scroll, corner_radius=6)
                row.pack(fill="x", padx=4, pady=4)
                row.grid_columnconfigure(0, weight=1)

                if not item["file_exists"]:
                    verdict = "ファイルなし"
                    verdict_color = "#808995"
                elif item["quality_gate_pass"] == 1:
                    verdict = "合格（bit一致未証明）"
                    verdict_color = "#63f08f"
                elif item["quality_gate_pass"] == 0:
                    verdict = "要確認"
                    verdict_color = "#ff5964"
                else:
                    verdict = "未監査"
                    verdict_color = "#f0b429"

                title = f"{item['artist']} - {item['title']}"
                try:
                    saved_text = datetime.fromisoformat(item["saved_at"]).astimezone().strftime(
                        "%Y-%m-%d %H:%M"
                    )
                except (TypeError, ValueError):
                    saved_text = item["saved_at"]
                details = (
                    f"{item['provider'].title()} {item.get('source_mode') or '-'} / "
                    f"{saved_text} / {item['album']} / {item['duration_sec']:.1f}秒 / "
                    f"{item['sample_rate']}Hz / {item['integrated_lufs']:.2f} LUFS"
                    if item["integrated_lufs"] is not None
                    else (
                        f"{item['provider'].title()} {item.get('source_mode') or '-'} / "
                        f"{saved_text} / {item['album']} / {item['duration_sec']:.1f}秒 / "
                        f"{item['sample_rate']}Hz"
                    )
                )
                ctk.CTkLabel(
                    row,
                    text=title,
                    anchor="w",
                    font=ctk.CTkFont(size=14, weight="bold"),
                ).grid(row=0, column=0, sticky="ew", padx=12, pady=(9, 0))
                ctk.CTkLabel(row, text=verdict, text_color=verdict_color).grid(
                    row=0, column=1, sticky="e", padx=10, pady=(9, 0)
                )
                ctk.CTkLabel(row, text=details, anchor="w", text_color="#aab2bd").grid(
                    row=1, column=0, sticky="ew", padx=12, pady=(2, 3)
                )
                problems = suspect_lines(item)
                if item["warnings"] or problems:
                    warning_text = "\n".join((item["warnings"] + problems)[:6])
                    ctk.CTkLabel(
                        row,
                        text=warning_text,
                        anchor="w",
                        justify="left",
                        text_color="#ff9b9b",
                        wraplength=650,
                    ).grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 8))

                button_frame = ctk.CTkFrame(row, fg_color="transparent")
                button_frame.grid(row=1, column=1, rowspan=2, sticky="e", padx=8, pady=6)
                state = "normal" if item["file_exists"] else "disabled"
                ctk.CTkButton(
                    button_frame,
                    text="再生",
                    width=58,
                    height=26,
                    state=state,
                    command=lambda path=item["file_path"]: open_recording(path),
                ).pack(side="left", padx=3)
                preview_state = (
                    "normal"
                    if state == "normal" and suspect_lines(item) and not self.is_recording
                    else "disabled"
                )
                ctk.CTkButton(
                    button_frame,
                    text="疑義試聴",
                    width=70,
                    height=26,
                    state=preview_state,
                    command=lambda target=item: preview_suspect(target),
                ).pack(side="left", padx=3)
                ctk.CTkButton(
                    button_frame,
                    text="Finder",
                    width=64,
                    height=26,
                    state=state,
                    command=lambda path=item["file_path"]: reveal_recording(path),
                ).pack(side="left", padx=3)

        ctk.CTkButton(toolbar, text="検索", width=70, command=render).pack(side="left", padx=(8, 0))
        search_entry.bind("<Return>", render)
        render()

        flac_toolbar = ctk.CTkFrame(flac_tab, fg_color="transparent")
        flac_toolbar.pack(fill="x", padx=10, pady=(8, 8))
        flac_query_var = ctk.StringVar()
        flac_status_filter = ctk.StringVar(value="すべて")
        flac_role_filter = ctk.StringVar(value="すべて")
        flac_search_entry = ctk.CTkEntry(
            flac_toolbar,
            textvariable=flac_query_var,
            placeholder_text="曲名・アーティスト・WAV/FLACファイル名",
        )
        flac_search_entry.pack(side="left", fill="x", expand=True)
        flac_count_label = ctk.CTkLabel(flac_toolbar, text="")
        flac_count_label.pack(side="right", padx=(10, 0))

        flac_filter_bar = ctk.CTkFrame(flac_tab, fg_color="transparent")
        flac_filter_bar.pack(fill="x", padx=10, pady=(0, 8))
        ctk.CTkLabel(flac_filter_bar, text="状態").pack(side="left")
        ctk.CTkSegmentedButton(
            flac_filter_bar,
            values=["すべて", "完了", "拒否", "失敗"],
            variable=flac_status_filter,
            command=lambda _value: render_flac(),
        ).pack(side="left", padx=8)
        ctk.CTkLabel(
            flac_filter_bar,
            text="ArchiveとDJ版の両方を検証後にWAVを削除",
            text_color="#aab2bd",
        ).pack(side="right")

        flac_role_bar = ctk.CTkFrame(flac_tab, fg_color="transparent")
        flac_role_bar.pack(fill="x", padx=10, pady=(0, 8))
        ctk.CTkLabel(flac_role_bar, text="出力").pack(side="left")
        ctk.CTkSegmentedButton(
            flac_role_bar,
            values=["すべて", "Archive", "DJ 24/48"],
            variable=flac_role_filter,
            command=lambda _value: render_flac(),
        ).pack(side="left", padx=8)

        flac_scroll = ctk.CTkScrollableFrame(flac_tab, fg_color="#11151a")
        flac_scroll.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        def retry_export(item):
            if self.is_recording or not item.get("source_exists"):
                return
            self.log_message(
                f"FLAC再変換開始: {os.path.basename(item['source_wav_path'])}"
            )

            def worker():
                retry_flac_export(item, self.catalog_path, self.log_message)
                self.after(0, render_flac)
                self.after(0, render)

            threading.Thread(target=worker, daemon=True).start()

        def render_flac(_event=None):
            for child in flac_scroll.winfo_children():
                child.destroy()
            try:
                selected_role = flac_role_filter.get()
                export_role = {
                    "Archive": "archive",
                    "DJ 24/48": "dj",
                }.get(selected_role)
                exports = list_audio_exports(
                    flac_query_var.get(),
                    export_role=export_role,
                    database_path=self.catalog_path,
                )
                selected_status = flac_status_filter.get()
                if selected_status == "完了":
                    exports = [item for item in exports if item["status"].startswith("complete")]
                elif selected_status == "拒否":
                    exports = [item for item in exports if item["status"] == "rejected"]
                elif selected_status == "失敗":
                    exports = [item for item in exports if item["status"] == "failed"]
            except Exception as exc:
                flac_count_label.configure(text="読込エラー")
                ctk.CTkLabel(flac_scroll, text=str(exc), text_color="#ff5964").pack(
                    pady=20
                )
                return
            flac_count_label.configure(text=f"{len(exports)}件")
            if not exports:
                ctk.CTkLabel(
                    flac_scroll,
                    text="該当するFLAC変換はありません",
                    text_color="#808995",
                ).pack(pady=24)
                return

            status_labels = {
                "complete": ("完了・WAV削除済み", "#63f08f"),
                "complete_wav_retained": ("完了・WAV残存", "#f0b429"),
                "partial": ("一部完了", "#f0b429"),
                "converting": ("変換中", "#58a6ff"),
                "rejected": ("拒否", "#ff5964"),
                "failed": ("失敗", "#ff5964"),
            }
            for item in exports:
                row = ctk.CTkFrame(flac_scroll, corner_radius=6)
                row.pack(fill="x", padx=4, pady=4)
                row.grid_columnconfigure(0, weight=1)
                status_text, status_color = status_labels.get(
                    item["status"], (item["status"], "#f0b429")
                )
                title = (
                    f"{item.get('artist') or 'Unknown'} - "
                    f"{item.get('title') or os.path.basename(item['source_wav_path'])}"
                )
                source_bytes = int(item.get("source_bytes") or 0)
                flac_bytes = int(item.get("flac_bytes") or 0)
                size_text = ""
                if source_bytes and flac_bytes:
                    ratio = 100.0 * flac_bytes / source_bytes
                    size_text = (
                        f" / {flac_bytes / 1024**2:.1f} MiB "
                        f"({ratio:.1f}% of WAV)"
                    )
                artwork_text = (
                    "ジャケット埋込済み"
                    if item.get("artwork_embedded")
                    else "ジャケットなし"
                )
                role_text = "Archive" if item.get("export_role") == "archive" else "DJ 24/48"
                source_rate = item.get("source_sample_rate") or "?"
                output_rate = item.get("output_sample_rate") or "?"
                src_text = (
                    "SRC Bypass"
                    if not item.get("src_engine")
                    else f"{item.get('src_quality') or item['src_engine']} {item.get('src_phase') or '?'}"
                )
                gain_text = f"Gain {float(item.get('safety_gain_db') or 0.0):.3f} dB"
                details = (
                    f"{role_text} / 24-bit FLAC / {source_rate} -> {output_rate} Hz / "
                    f"Dither {item.get('dither') or 'NONE'} / {src_text} / {gain_text}"
                )
                ctk.CTkLabel(
                    row,
                    text=title,
                    anchor="w",
                    font=ctk.CTkFont(size=14, weight="bold"),
                ).grid(row=0, column=0, sticky="ew", padx=12, pady=(9, 0))
                ctk.CTkLabel(row, text=status_text, text_color=status_color).grid(
                    row=0, column=1, sticky="e", padx=10, pady=(9, 0)
                )
                ctk.CTkLabel(
                    row,
                    text=details,
                    anchor="w",
                    justify="left",
                    text_color="#aab2bd",
                    wraplength=620,
                ).grid(row=1, column=0, sticky="ew", padx=12, pady=(2, 2))
                ctk.CTkLabel(
                    row,
                    text=f"{item.get('dither_reason') or '処理理由なし'}{size_text} / {artwork_text}",
                    anchor="w",
                    justify="left",
                    text_color="#808995",
                    wraplength=620,
                ).grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 7))
                if item.get("reason"):
                    ctk.CTkLabel(
                        row,
                        text=item["reason"],
                        anchor="w",
                        justify="left",
                        text_color="#ff9b9b",
                        wraplength=650,
                    ).grid(row=3, column=0, sticky="ew", padx=12, pady=(0, 8))

                button_frame = ctk.CTkFrame(row, fg_color="transparent")
                button_frame.grid(row=1, column=1, rowspan=3, sticky="e", padx=8, pady=6)
                flac_state = "normal" if item.get("flac_exists") else "disabled"
                ctk.CTkButton(
                    button_frame,
                    text="再生",
                    width=56,
                    height=26,
                    state=flac_state,
                    command=lambda path=item.get("flac_path"): open_recording(path),
                ).pack(side="left", padx=3)
                ctk.CTkButton(
                    button_frame,
                    text="Finder",
                    width=62,
                    height=26,
                    state=flac_state,
                    command=lambda path=item.get("flac_path"): reveal_recording(path),
                ).pack(side="left", padx=3)
                retry_state = (
                    "normal"
                    if item["status"] in {"failed", "rejected"}
                    and item.get("source_exists")
                    and not self.is_recording
                    else "disabled"
                )
                ctk.CTkButton(
                    button_frame,
                    text="再実行",
                    width=62,
                    height=26,
                    state=retry_state,
                    command=lambda target=item: retry_export(target),
                ).pack(side="left", padx=3)

        ctk.CTkButton(
            flac_toolbar,
            text="検索",
            width=70,
            command=render_flac,
        ).pack(side="left", padx=(8, 0))
        flac_search_entry.bind("<Return>", render_flac)
        render_flac()

    def on_library_conversion_event(self, event, payload):
        messages = {
            "started": "ライブラリ変換を開始しました",
            "paused": "ライブラリ変換を一時停止しました",
            "resumed": "ライブラリ変換を再開しました",
            "cancelled": "ライブラリ変換を停止しました。未処理項目は再開できます",
            "complete": "ライブラリ変換が完了しました",
            "capacity_error": "ライブラリ変換を開始できません: SSD空き容量不足",
            "destination_missing": "ライブラリ変換を一時停止しました: 出力SSDが見つかりません",
        }
        if event in messages:
            self.log_message(messages[event])
        elif event == "asset_failed":
            self.log_message(
                f"ライブラリ変換失敗: {os.path.basename(payload.get('source_path', ''))} / "
                f"{payload.get('reason', '不明なエラー')}"
            )
        refresh = getattr(self, "library_ui_refresh", None)
        if refresh is not None:
            self.ui_queue.put(refresh)

    def build_library_conversion_tab(self, parent):
        parent.grid_columnconfigure(0, weight=1)
        path_frame = ctk.CTkFrame(parent, fg_color="#161a1f", corner_radius=6)
        path_frame.pack(fill="x", padx=10, pady=(8, 6))
        path_frame.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(path_frame, text="入力フォルダ").grid(
            row=0, column=0, sticky="w", padx=10, pady=(10, 4)
        )
        ctk.CTkLabel(
            path_frame,
            textvariable=self.library_source_dir,
            anchor="w",
            fg_color="#252b33",
            corner_radius=5,
        ).grid(row=0, column=1, sticky="ew", padx=8, pady=(10, 4))

        def choose_source():
            selected = filedialog.askdirectory(
                initialdir=self.library_source_dir.get() or os.path.expanduser("~/Downloads")
            )
            if selected:
                self.library_source_dir.set(selected)

        ctk.CTkButton(
            path_frame, text="選択", width=64, command=choose_source
        ).grid(row=0, column=2, padx=(0, 10), pady=(10, 4))
        ctk.CTkLabel(path_frame, text="24/48出力").grid(
            row=1, column=0, sticky="w", padx=10, pady=(4, 10)
        )
        ctk.CTkLabel(
            path_frame,
            textvariable=self.library_destination_dir,
            anchor="w",
            fg_color="#252b33",
            corner_radius=5,
        ).grid(row=1, column=1, sticky="ew", padx=8, pady=(4, 10))

        def choose_destination():
            selected = filedialog.askdirectory(
                initialdir=self.library_destination_dir.get()
                if os.path.isdir(self.library_destination_dir.get())
                else "/Volumes"
            )
            if selected:
                if os.path.basename(selected) == "Go SSD":
                    selected = os.path.join(selected, "DJ Library 24-48")
                self.library_destination_dir.set(selected)

        ctk.CTkButton(
            path_frame, text="選択", width=64, command=choose_destination
        ).grid(row=1, column=2, padx=(0, 10), pady=(4, 10))

        toolbar = ctk.CTkFrame(parent, fg_color="transparent")
        toolbar.pack(fill="x", padx=10, pady=4)
        scan_button = ctk.CTkButton(toolbar, text="事前走査", width=88)
        scan_button.pack(side="left", padx=(0, 5))
        start_button = ctk.CTkButton(
            toolbar,
            text="変換開始",
            width=88,
            fg_color="#1DB954",
            hover_color="#1ed760",
            text_color="#06170c",
            state="disabled",
        )
        start_button.pack(side="left", padx=5)
        pause_button = ctk.CTkButton(
            toolbar, text="一時停止", width=88, state="disabled"
        )
        pause_button.pack(side="left", padx=5)
        cancel_button = ctk.CTkButton(
            toolbar,
            text="停止",
            width=70,
            state="disabled",
            fg_color="#414852",
        )
        cancel_button.pack(side="left", padx=5)
        status_label = ctk.CTkLabel(toolbar, text="未走査", text_color="#aab2bd")
        status_label.pack(side="right")

        progress = ctk.CTkProgressBar(parent, height=10, progress_color="#1DB954")
        progress.pack(fill="x", padx=10, pady=(2, 6))
        progress.set(0)
        scan_summary_label = ctk.CTkLabel(
            parent,
            text="走査情報なし",
            anchor="w",
            justify="left",
            text_color="#808995",
            wraplength=900,
        )
        scan_summary_label.pack(fill="x", padx=12, pady=(0, 6))

        filter_bar = ctk.CTkFrame(parent, fg_color="transparent")
        filter_bar.pack(fill="x", padx=10, pady=(0, 6))
        search_var = ctk.StringVar()
        status_filter = ctk.StringVar(value="すべて")
        job_var = ctk.StringVar(value="")
        job_labels = {}
        job_selector = ctk.CTkOptionMenu(
            filter_bar,
            variable=job_var,
            values=["履歴なし"],
            width=210,
        )
        job_selector.pack(side="left", padx=(0, 8))
        search_entry = ctk.CTkEntry(
            filter_bar,
            textvariable=search_var,
            placeholder_text="ファイル名・形式・理由",
        )
        search_entry.pack(side="left", fill="x", expand=True)
        ctk.CTkSegmentedButton(
            filter_bar,
            values=["すべて", "完了", "重複", "スキップ", "失敗", "待機"],
            variable=status_filter,
        ).pack(side="left", padx=(8, 0))

        scroll = ctk.CTkScrollableFrame(parent, fg_color="#11151a")
        scroll.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        def select_job(label):
            selected_id = job_labels.get(label)
            if selected_id:
                self.current_library_job_id = selected_id
                render()

        def current_job():
            jobs = list_library_jobs(limit=200, database_path=self.catalog_path)
            known_ids = {item["job_id"] for item in jobs}
            if self.current_library_job_id not in known_ids and jobs:
                self.current_library_job_id = jobs[0]["job_id"]
            return self.current_library_job_id

        def format_counts(values, suffix=""):
            return ", ".join(
                f"{key}{suffix} x{count}"
                for key, count in sorted(
                    (values or {}).items(), key=lambda item: str(item[0])
                )
            ) or "なし"

        def render():
            if not scroll.winfo_exists():
                return
            for child in scroll.winfo_children():
                child.destroy()
            job_id = current_job()
            if not job_id:
                ctk.CTkLabel(
                    scroll,
                    text="入力フォルダを選択して事前走査を実行してください",
                    text_color="#808995",
                ).pack(pady=28)
                return
            jobs = list_library_jobs(limit=200, database_path=self.catalog_path)
            job_labels.clear()
            selected_label = None
            labels = []
            for candidate in jobs:
                label = (
                    f"{candidate['created_at'][:16]} | "
                    f"{os.path.basename(candidate['source_root']) or candidate['source_root']} | "
                    f"{candidate['status']}"
                )
                if label in job_labels:
                    label = f"{label} | {candidate['job_id'][:8]}"
                labels.append(label)
                job_labels[label] = candidate["job_id"]
                if candidate["job_id"] == job_id:
                    selected_label = label
            job_selector.configure(values=labels or ["履歴なし"])
            if selected_label and job_var.get() != selected_label:
                job_var.set(selected_label)
            job = next((item for item in jobs if item["job_id"] == job_id), None)
            if job:
                total = max(1, int(job["total_files"]))
                finished = sum(
                    int(job[key])
                    for key in (
                        "completed_files",
                        "duplicate_files",
                        "skipped_files",
                        "failed_files",
                    )
                )
                progress.set(min(1.0, finished / total))
                status_label.configure(
                    text=(
                        f"{job['status']} / 完了 {job['completed_files']} / "
                        f"重複 {job['duplicate_files']} / スキップ {job['skipped_files']} / "
                        f"失敗 {job['failed_files']} / 全{job['total_files']}"
                    )
                )
                start_button.configure(
                    state="disabled" if self.library_queue.running else "normal",
                    text=(
                        "失敗分を再試行"
                        if int(job["failed_files"]) > 0
                        else "変換開始"
                    ),
                )
                pause_button.configure(
                    state="normal" if self.library_queue.running else "disabled",
                    text="再開" if self.library_queue.paused else "一時停止",
                )
                cancel_button.configure(
                    state="normal" if self.library_queue.running else "disabled"
                )
                summary = (job.get("settings") or {}).get("scan_summary") or {}
                duration = float(summary.get("duration_sec") or 0.0)
                scan_summary_label.configure(
                    text=(
                        f"再生時間 {duration / 3600:.2f}時間 / "
                        f"予測上限 {int(job['projected_bytes']) / 1024**3:.2f}GiB\n"
                        f"形式: {format_counts(summary.get('formats'))} / "
                        f"レート: {format_counts(summary.get('sample_rates'), 'Hz')} / "
                        f"深度: {format_counts(summary.get('bit_depths'), '-bit')}"
                    )
                )
            mapping = {
                "完了": {"complete"},
                "重複": {"duplicate"},
                "スキップ": {"skipped"},
                "失敗": {"failed"},
                "待機": {"queued", "converting"},
            }
            selected = mapping.get(status_filter.get())
            assets = list_library_assets(
                job_id,
                statuses=selected,
                limit=5000,
                database_path=self.catalog_path,
            )
            query = search_var.get().strip().casefold()
            if query:
                assets = [
                    item
                    for item in assets
                    if query
                    in " ".join(
                        str(item.get(key) or "")
                        for key in (
                            "relative_path",
                            "source_codec",
                            "reason",
                            "status",
                        )
                    ).casefold()
                ]
            if not assets:
                ctk.CTkLabel(
                    scroll, text="該当項目はありません", text_color="#808995"
                ).pack(pady=24)
                return
            colors = {
                "complete": "#63f08f",
                "duplicate": "#58a6ff",
                "skipped": "#f0b429",
                "failed": "#ff5964",
                "converting": "#58a6ff",
                "queued": "#aab2bd",
            }
            for item in assets[:1000]:
                row = ctk.CTkFrame(scroll, corner_radius=6)
                row.pack(fill="x", padx=4, pady=3)
                row.grid_columnconfigure(0, weight=1)
                ctk.CTkLabel(
                    row,
                    text=item["relative_path"],
                    anchor="w",
                    font=ctk.CTkFont(size=13, weight="bold"),
                ).grid(row=0, column=0, sticky="ew", padx=10, pady=(7, 1))
                ctk.CTkLabel(
                    row,
                    text=item["status"],
                    text_color=colors.get(item["status"], "#aab2bd"),
                ).grid(row=0, column=1, sticky="e", padx=10, pady=(7, 1))
                details = (
                    f"{item.get('source_codec') or '?'} / "
                    f"{item.get('source_bit_depth') or '?'}-bit / "
                    f"{item.get('source_sample_rate') or '?'}Hz / "
                    f"{item.get('source_channels') or '?'}ch / "
                    f"Dither {item.get('dither') or '-'} / "
                    f"SRC {item.get('src_engine') or '-'}"
                )
                ctk.CTkLabel(
                    row, text=details, anchor="w", text_color="#aab2bd"
                ).grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 2))
                if item.get("reason"):
                    ctk.CTkLabel(
                        row,
                        text=item["reason"],
                        anchor="w",
                        text_color="#ff9b9b",
                        wraplength=720,
                    ).grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 7))
                if item.get("output_exists"):
                    ctk.CTkButton(
                        row,
                        text="Finder",
                        width=62,
                        height=25,
                        command=lambda path=item["output_path"]: subprocess.run(
                            ["open", "-R", path], check=False
                        ),
                    ).grid(row=1, column=1, rowspan=2, padx=8, pady=5)

        self.library_ui_refresh = render

        def scan_action():
            if self.library_queue.running:
                return
            source = self.library_source_dir.get()
            destination = self.library_destination_dir.get()
            scan_button.configure(state="disabled")
            status_label.configure(text="走査中...")

            def worker():
                try:
                    summary = scan_library(
                        source,
                        destination,
                        database_path=self.catalog_path,
                    )

                    def finish():
                        self.current_library_job_id = summary["job_id"]
                        projected = summary["projected_bytes"] / 1024**3
                        status_label.configure(
                            text=(
                                f"走査完了: 対象 {summary['queued_files']} / "
                                f"スキップ {summary['skipped_files']} / "
                                f"最大見積 {projected:.1f} GiB"
                            )
                        )
                        scan_summary_label.configure(
                            text=(
                                f"再生時間 {summary['duration_sec'] / 3600:.2f}時間 / "
                                f"予測上限 {projected:.2f}GiB\n"
                                f"形式: {format_counts(summary['formats'])} / "
                                f"レート: {format_counts(summary['sample_rates'], 'Hz')} / "
                                f"深度: {format_counts(summary['bit_depths'], '-bit')}"
                            )
                        )
                        scan_button.configure(state="normal")
                        start_button.configure(state="normal")
                        render()

                    self.after(0, finish)
                except Exception as exc:
                    error = str(exc)
                    self.log_message(f"ライブラリ走査失敗: {error}")
                    self.after(
                        0,
                        lambda error=error: (
                            status_label.configure(text=f"走査失敗: {error}"),
                            scan_button.configure(state="normal"),
                        ),
                    )

            threading.Thread(target=worker, daemon=True).start()

        def start_action():
            job_id = current_job()
            if not job_id or self.library_queue.running:
                return
            self.library_queue.start(job_id)
            render()

        def pause_action():
            if not self.library_queue.running:
                return
            if self.library_queue.paused:
                self.library_queue.resume()
            else:
                self.library_queue.pause()
            render()

        scan_button.configure(command=scan_action)
        start_button.configure(command=start_action)
        pause_button.configure(command=pause_action)
        cancel_button.configure(command=self.library_queue.cancel)
        job_selector.configure(command=select_job)
        status_filter.trace_add("write", lambda *_args: render())
        search_entry.bind("<Return>", lambda _event: render())
        render()

    def on_processing_finished(self):
        if self.pending_spool_audio is not None:
            self.pending_spool_audio.close(delete=True)
            self.pending_spool_audio = None
        def update():
            self.set_controls_recording(False)
            if getattr(self, "was_standby", False):
                self.is_standby = True
                self.standby_switch.select()
                if self.start_audio_stream():
                    self.status_label.configure(text="Standby", text_color="#f0b429")
                    self.pre_chunks = []
                    self.pre_samples = 0
                    self.log_message("Standby待機状態に自動復帰しました。")
                else:
                    self.is_standby = False
                    self.standby_switch.deselect()
                    self.status_label.configure(text="Ready", text_color="#1DB954")
            else:
                self.status_label.configure(text="Ready", text_color="#1DB954")
            self.was_standby = False

        self.after(0, update)

    def poll_source(self):
        adapter = self.current_adapter()
        info = adapter.snapshot()
        self.current_source_info = info

        if self.is_recording and self.capture_quality_audit is not None:
            try:
                observed_device_rate = int(
                    round(float(self.current_device_profile()["nominal_sample_rate"]))
                )
            except Exception as exc:
                if not self.device_state_unavailable_reported:
                    self.capture_quality_audit.add_external_event(
                        "device_state_unavailable",
                        f"録音中のCoreAudio状態を確認できません: {exc}",
                    )
                    self.device_state_unavailable_reported = True
            else:
                self.device_state_unavailable_reported = False
                if observed_device_rate != int(self.sample_rate):
                    detail = (
                        f"録音中にCoreAudio入力レートが変更されました: "
                        f"{self.sample_rate}Hz -> {observed_device_rate}Hz。"
                        "異なるレートを同一セッションへ混在させないため停止します"
                    )
                    self.capture_quality_audit.add_external_event(
                        "device_sample_rate_change", detail
                    )
                    self.log_message(f"品質重大エラー: {detail}")
                    self.stop_rec()
                    self.after(CHECK_INTERVAL_MS, self.poll_source)
                    return

        if self.is_recording and self.capture_quality_audit is not None:
            for event in adapter.poll_events():
                event_type = event.get("type")
                if event_type in {"source_buffering", "source_error"}:
                    detail = event.get("message", f"{adapter.name}再生イベント")
                    timestamp_epoch = event.get("timestamp_epoch")
                    event_sample = None
                    if timestamp_epoch is not None:
                        event_sample = max(
                            0,
                            int(
                                (float(timestamp_epoch) - self.capture_quality_audit.started_at)
                                * self.sample_rate
                            ),
                        )
                    self.capture_quality_audit.add_external_event(
                        event_type,
                        detail,
                        sample=event_sample,
                        duration_sec=event.get("duration_sec", 0.0),
                    )
                    self.log_message(f"品質疑義: {detail}")

        if info.get("status") == "OK":
            self.track_label.configure(text=info["name"], text_color="white")
            self.artist_label.configure(text=f"{info['artist']} - {info['album']} / {info['state']}", text_color="#b7bec8")

            if self.is_recording and self.capture_quality_audit is not None:
                self.capture_quality_audit.observe_playback(
                    normalized_track_key(info),
                    info.get("state"),
                    info.get("position"),
                    duration=info.get("duration"),
                )

            if self.is_standby and not self.is_recording and source_is_playing(info):
                self.log_message(f"{adapter.name}再生検知: {info['artist']} - {info['name']}")
                self.start_rec()
                
            if self.is_recording:
                # Track change auto-stop logic for SINGLE mode
                if self.record_mode.get() == MODE_SINGLE:
                    if self.last_track_key and normalized_track_key(info) != self.last_track_key:
                        self.log_message("単曲モード: 曲の切り替わりを検知し、録音を自動停止します。")
                        self.stop_rec()

            if self.is_recording and self.auto_stop_on_idle.get():
                if not source_is_playing(info):
                    if self.source_idle_since is None:
                        self.source_idle_since = time.time()
                    elif time.time() - self.source_idle_since >= float(self.auto_stop_grace_sec.get()):
                        self.log_message(f"{adapter.name}自動停止検知")
                        self.stop_rec()
                else:
                    self.source_idle_since = None

            if self.is_recording and source_is_playing(info):
                if self.provider.get() == PROVIDER_QOBUZ:
                    expected_rate = self.capture_quality_audit.source_evaluation.get(
                        "source_sample_rate"
                    )
                    observed_rate = info.get("source_sample_rate")
                    if expected_rate and observed_rate and int(expected_rate) != int(observed_rate):
                        detail = (
                            f"Qobuzソースレート変更: {expected_rate}Hz -> {observed_rate}Hz。"
                            "1セッション1レート制約により停止します"
                        )
                        self.capture_quality_audit.add_external_event(
                            "sample_rate_change", detail
                        )
                        self.log_message(f"品質重大エラー: {detail}")
                        self.stop_rec()
                        self.after(CHECK_INTERVAL_MS, self.poll_source)
                        return
                new_key = normalized_track_key(info)
                if new_key and new_key != self.last_track_key:
                    estimated_start = self.estimate_track_start_sample(info)
                    self.add_history_track(info, estimated_start)
                    self.log_message(f"曲変更: {info['artist']} - {info['name']}")

            if not self.is_recording and not source_is_playing(info):
                self.consider_idle_sample_rate_sync(info)

        elif info.get("status") in ("IDLE", "CLOSED", "UNAVAILABLE"):
            if self.is_recording and self.capture_quality_audit is not None:
                self.capture_quality_audit.observe_playback(None, "idle", None)
            if info.get("status") == "CLOSED":
                self.track_label.configure(text=f"{adapter.name} is closed", text_color="#b7bec8")
            elif info.get("status") == "UNAVAILABLE":
                self.track_label.configure(text=f"{adapter.name}: 証跡取得不能", text_color="#f0b429")
            else:
                self.track_label.configure(text=f"{adapter.name} is idle", text_color="#b7bec8")
            self.artist_label.configure(text="-", text_color="#b7bec8")

            if self.is_recording and self.auto_stop_on_idle.get():
                if self.source_idle_since is None:
                    self.source_idle_since = time.time()
                elif time.time() - self.source_idle_since >= float(self.auto_stop_grace_sec.get()):
                    self.log_message(f"{adapter.name}終了検知、録音停止")
                    self.stop_rec()

            if not self.is_recording:
                self.consider_idle_sample_rate_sync(info)

        self.after(CHECK_INTERVAL_MS, self.poll_source)

    def on_closing(self):
        self.library_queue.cancel()
        self.stop_audio_stream()
        if self.capture_spool is not None:
            self.capture_spool.discard()
        if self.pending_spool_audio is not None:
            self.pending_spool_audio.close(delete=False)
        self.destroy()


if __name__ == "__main__":
    if "--self-test-library-codecs" in sys.argv:
        diagnostics = library_codec_diagnostics()
        print(json.dumps(diagnostics, ensure_ascii=False, sort_keys=True))
        raise SystemExit(0 if diagnostics["ok"] else 1)
    else:
        app = HiResRecorderApp()
        app.mainloop()


SpotifyRecorderApp = HiResRecorderApp
