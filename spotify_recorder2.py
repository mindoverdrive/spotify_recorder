import os
import subprocess
import threading
import time
import wave
import queue
from datetime import datetime
import numpy as np
import sounddevice as sd
import customtkinter as ctk
from tkinter import filedialog
from mutagen.wave import WAVE
from mutagen.id3 import TIT2, TPE1, TALB

# 定数
SAMPLE_RATE = 44100
CHECK_INTERVAL = 1000  # Spotify監視間隔 (ms)

# --- 無損失（ロスレス）無音トリミング ---
def trim_silence_lossless(audio_data, sample_rate, threshold_db=-60.0, pad_start_sec=0.1, pad_end_sec=0.3):
    threshold = 10 ** (threshold_db / 20.0)
    amplitude = np.max(np.abs(audio_data), axis=1)
    non_silence_indices = np.where(amplitude > threshold)[0]
    
    if len(non_silence_indices) == 0:
        return audio_data, 0, len(audio_data)
        
    start_idx = non_silence_indices[0]
    end_idx = non_silence_indices[-1]
    
    pad_start_samples = int(pad_start_sec * sample_rate)
    pad_end_samples = int(pad_end_sec * sample_rate)
    
    start_trimmed = max(0, start_idx - pad_start_samples)
    end_trimmed = min(len(audio_data), end_idx + pad_end_samples)
    
    trimmed_data = audio_data[start_trimmed:end_trimmed]
    return trimmed_data, start_trimmed, end_trimmed

# --- Spotify情報取得 (AppleScript) ---
def get_spotify_info():
    script = '''
    if application "Spotify" is running then
        tell application "Spotify"
            if player state is playing or player state is paused then
                set track_name to name of current track
                set artist_name to artist of current track
                set album_name to album of current track
                set player_state to player state as string
                return track_name & "||" & artist_name & "||" & album_name & "||" & player_state
            else
                return "IDLE"
            end if
        end tell
    else
        return "CLOSED"
    end if
    '''
    try:
        result = subprocess.run(['osascript', '-e', script], capture_output=True, text=True, timeout=1)
        if result.returncode == 0:
            raw = result.stdout.strip()
            if raw == "CLOSED":
                return {'status': 'CLOSED'}
            if raw == "IDLE":
                return {'status': 'IDLE'}
            
            info = raw.split('||')
            if len(info) >= 4:
                return {
                    'status': 'OK',
                    'name': info[0],
                    'artist': info[1],
                    'album': info[2],
                    'state': info[3]
                }
        else:
            err_msg = result.stderr.strip()
            if "not allowed" in err_msg or "許可されていません" in err_msg or "error -1743" in err_msg:
                return {'status': 'PERMISSION_DENIED', 'error': err_msg}
            if "error -600" in err_msg:
                return {'status': 'CLOSED'}
    except Exception as e:
        return {'status': 'ERROR', 'error': str(e)}

    # フォールバック (ウィンドウタイトル)
    fallback_script = 'tell application "System Events" to get name of first window of (first process whose name is "Spotify")'
    try:
        res_fb = subprocess.run(['osascript', '-e', fallback_script], capture_output=True, text=True, timeout=1)
        if res_fb.returncode == 0:
            title = res_fb.stdout.strip()
            if " - " in title:
                parts = title.split(" - ", 1)
                return {
                    'status': 'OK',
                    'name': parts[1],
                    'artist': parts[0],
                    'album': 'Captured from Window',
                    'state': 'playing'
                }
    except:
        pass

    return {'status': 'NOT_LINKED'}

# --- 分割・トリミング・保存処理 (別スレッド) ---
def process_and_save_tracks(full_audio, history, save_dir, threshold_db, pad_start, pad_end, log_callback, on_finish_callback):
    log_callback(f"保存処理を開始します。総サンプル数: {len(full_audio)}")
    os.makedirs(save_dir, exist_ok=True)
    
    for i, track in enumerate(history):
        start_sample = track["start_sample"]
        end_sample = history[i+1]["start_sample"] if i + 1 < len(history) else len(full_audio)
        
        track_audio = full_audio[start_sample:end_sample]
        
        if len(track_audio) < SAMPLE_RATE * 5:  # 5秒未満はスキップ
            log_callback(f"短すぎるためスキップ: {track.get('name', 'Unknown')}")
            continue
            
        log_callback(f"トリミング処理中: {track['name']} - {track['artist']}")
        trimmed, s_idx, e_idx = trim_silence_lossless(
            track_audio, SAMPLE_RATE, threshold_db, pad_start, pad_end
        )
        
        log_callback(f"トリム完了: 開始前 -{s_idx} サンプル, 終了後 -{len(track_audio) - e_idx} サンプルをカット")
        
        raw_name = f"{track['artist']} - {track['name']}"
        safe_name = "".join(x for x in raw_name if x.isalnum() or x in " -_")
        if not safe_name.strip():
            safe_name = f"Track_{i+1}_{datetime.now().strftime('%H%M%S')}"
            
        file_path = os.path.join(save_dir, f"{safe_name}.wav")
        
        try:
            log_callback(f"保存中: {file_path}")
            with wave.open(file_path, 'wb') as wf:
                wf.setnchannels(2)
                wf.setsampwidth(2)
                wf.setframerate(SAMPLE_RATE)
                # float32 [-1.0, 1.0] から int16 へのロスレス変換
                int_data = (trimmed * 32767).clip(-32768, 32767).astype(np.int16)
                wf.writeframes(int_data.tobytes())
                
            # ID3タグ埋め込み
            try:
                audio = WAVE(file_path)
                if audio.tags is None:
                    audio.add_tags()
                audio.tags.add(TIT2(encoding=3, text=track['name']))
                audio.tags.add(TPE1(encoding=3, text=track['artist']))
                audio.tags.add(TALB(encoding=3, text=track['album']))
                audio.save()
                log_callback(f"タグ埋め込み完了: {safe_name}.wav")
            except Exception as tag_err:
                log_callback(f"タグ埋め込み失敗: {tag_err}")
        except Exception as save_err:
            log_callback(f"WAV保存エラー: {save_err}")
            
    log_callback("すべての保存とトリミング処理が完了しました。")
    on_finish_callback()

# --- GUI アプリケーションクラス ---
class SpotifyRecorderV2App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Premium Spotify Recorder V2 (Lightweight)")
        self.geometry("640x780")
        
        # 録音制御変数
        self.is_recording = False
        self.is_standby = False
        self.audio_buffer = []
        self.recording_history = []
        self.total_samples_recorded = 0
        self.stream = None
        self.current_spotify_info = None
        self.last_track_name = None
        
        # キュー
        self.log_queue = queue.Queue()
        self.meter_queue = queue.Queue()
        
        # 設定変数
        self.save_dir = ctk.StringVar(value=os.path.expanduser("~/Desktop"))
        self.record_ch_start = ctk.IntVar(value=5)
        self.silence_threshold_db = ctk.DoubleVar(value=-60.0)
        self.pad_start_sec = ctk.DoubleVar(value=0.1)
        self.pad_end_sec = ctk.DoubleVar(value=0.3)
        self.is_muted = ctk.BooleanVar(value=True)
        
        # デバイス一覧の取得
        self.audio_devices = sd.query_devices()
        self.input_devices_info = [(i, d) for i, d in enumerate(self.audio_devices) if d['max_input_channels'] > 0]
        self.output_devices_info = [(i, d) for i, d in enumerate(self.audio_devices) if d['max_output_channels'] > 0]
        self.input_device_strings = [f"{i}: {d['name']}" for i, d in self.input_devices_info]
        self.output_device_strings = [f"{i}: {d['name']}" for i, d in self.output_devices_info]
        
        self.device_in_id = sd.default.device[0] if sd.default.device[0] is not None else 0
        self.device_out_id = sd.default.device[1] if sd.default.device[1] is not None else 0
        self._current_start_ch = 5
        
        # UI構築
        self.build_ui()
        
        # 定期実行タスク
        self.after(CHECK_INTERVAL, self.poll_spotify)
        self.after(50, self.process_queues)
        self.protocol("WM_DELETE_WINDOW", self.on_closing)
        
    def build_ui(self):
        # メインフレーム
        self.frame = ctk.CTkFrame(self, corner_radius=15)
        self.frame.pack(pady=15, padx=15, fill="both", expand=True)
        
        # ステータスラベル
        self.status_label = ctk.CTkLabel(self.frame, text="Ready to Record", font=ctk.CTkFont(size=22, weight="bold"), text_color="#1DB954")
        self.status_label.pack(pady=(20, 10))
        
        # 曲情報表示カード
        info_card = ctk.CTkFrame(self.frame, fg_color="#1e1e24", corner_radius=10, border_width=1, border_color="#2a2a32")
        info_card.pack(pady=10, padx=20, fill="x")
        self.track_title_label = ctk.CTkLabel(info_card, text="Spotify: Not Playing", font=ctk.CTkFont(size=16, weight="bold"))
        self.track_title_label.pack(pady=(15, 2))
        self.artist_label = ctk.CTkLabel(info_card, text="Artist: -", font=ctk.CTkFont(size=13), text_color="#b3b3b3")
        self.artist_label.pack(pady=(0, 15))
        
        # 音量レベルメーター (無負荷)
        meter_frame = ctk.CTkFrame(self.frame, fg_color="transparent")
        meter_frame.pack(fill="x", padx=20, pady=5)
        ctk.CTkLabel(meter_frame, text="Level Meter:", font=ctk.CTkFont(size=12)).pack(side="left")
        self.vol_meter = ctk.CTkProgressBar(meter_frame, height=12, fg_color="#333", progress_color="#1DB954")
        self.vol_meter.pack(side="left", fill="x", expand=True, padx=10)
        self.vol_meter.set(0)
        
        # コントロールスイッチ
        ctrl_switch_frame = ctk.CTkFrame(self.frame, fg_color="transparent")
        ctrl_switch_frame.pack(pady=10)
        
        self.standby_switch = ctk.CTkSwitch(ctrl_switch_frame, text="録音待機 (Standby Mode)", font=ctk.CTkFont(size=13, weight="bold"), progress_color="#1DB954", command=self.on_standby_toggle)
        self.standby_switch.pack(side="left", padx=10)
        
        self.mute_switch = ctk.CTkSwitch(ctrl_switch_frame, text="スピーカーミュート", font=ctk.CTkFont(size=13), variable=self.is_muted)
        self.mute_switch.pack(side="left", padx=10)
        
        # アクションボタン
        btn_frame = ctk.CTkFrame(self.frame, fg_color="transparent")
        btn_frame.pack(pady=10)
        
        self.start_btn = ctk.CTkButton(btn_frame, text="● Manual Start", width=140, fg_color="#1DB954", hover_color="#1ED760", text_color="#000", font=ctk.CTkFont(weight="bold"), command=self.start_rec)
        self.start_btn.pack(side="left", padx=10)
        
        self.stop_btn = ctk.CTkButton(btn_frame, text="■ Stop Recording", width=140, fg_color="#e91429", hover_color="#ff2d3d", text_color="#fff", font=ctk.CTkFont(weight="bold"), command=self.stop_rec, state="disabled")
        self.stop_btn.pack(side="left", padx=10)
        
        # 設定セクション (アコーディオン風)
        settings_frame = ctk.CTkFrame(self.frame, fg_color="transparent")
        settings_frame.pack(pady=10, padx=20, fill="x")
        
        ctk.CTkLabel(settings_frame, text="--- 設定項目 (Settings) ---", font=ctk.CTkFont(size=12, weight="bold"), text_color="#727272").pack(anchor="center", pady=(5, 5))
        
        # デバイス選択
        dev_frame = ctk.CTkFrame(settings_frame, fg_color="transparent")
        dev_frame.pack(fill="x", pady=2)
        ctk.CTkLabel(dev_frame, text="入力デバイス:", width=100, anchor="w").pack(side="left")
        self.in_combo = ctk.CTkOptionMenu(dev_frame, values=self.input_device_strings, command=self.on_dev_change)
        self.in_combo.pack(side="left", fill="x", expand=True)
        if self.input_device_strings:
            self.in_combo.set(next((d for d in self.input_device_strings if d.startswith(str(self.device_in_id)+":")), self.input_device_strings[0]))
            
        dev_out_frame = ctk.CTkFrame(settings_frame, fg_color="transparent")
        dev_out_frame.pack(fill="x", pady=2)
        ctk.CTkLabel(dev_out_frame, text="パススルー出力:", width=100, anchor="w").pack(side="left")
        self.out_combo = ctk.CTkOptionMenu(dev_out_frame, values=self.output_device_strings, command=self.on_dev_change)
        self.out_combo.pack(side="left", fill="x", expand=True)
        if self.output_device_strings:
            self.out_combo.set(next((d for d in self.output_device_strings if d.startswith(str(self.device_out_id)+":")), self.output_device_strings[0]))
            
        # チャンネル＆保存ディレクトリ
        ch_dir_frame = ctk.CTkFrame(settings_frame, fg_color="transparent")
        ch_dir_frame.pack(fill="x", pady=4)
        
        ctk.CTkLabel(ch_dir_frame, text="開始ch (Start ch):").pack(side="left")
        self.ch_entry = ctk.CTkEntry(ch_dir_frame, textvariable=self.record_ch_start, width=45)
        self.ch_entry.pack(side="left", padx=5)
        
        ctk.CTkButton(ch_dir_frame, text="保存先選択 (Browse)", width=130, command=self.browse_dir).pack(side="right")
        self.dir_label = ctk.CTkLabel(ch_dir_frame, textvariable=self.save_dir, anchor="w", fg_color="#2a2a32", corner_radius=5, height=28)
        self.dir_label.pack(side="right", fill="x", expand=True, padx=5)
        
        # トリミング設定項目
        trim_set_frame = ctk.CTkFrame(settings_frame, fg_color="transparent")
        trim_set_frame.pack(fill="x", pady=5)
        
        # 閾値dB
        ctk.CTkLabel(trim_set_frame, text="無音閾値 (dB):").grid(row=0, column=0, sticky="w", pady=2)
        self.thresh_entry = ctk.CTkEntry(trim_set_frame, textvariable=self.silence_threshold_db, width=60)
        self.thresh_entry.grid(row=0, column=1, sticky="w", padx=5)
        ctk.CTkLabel(trim_set_frame, text="(例: -60.0)", text_color="gray").grid(row=0, column=2, sticky="w")
        
        # 開始マージン
        ctk.CTkLabel(trim_set_frame, text="開始マージン (秒):").grid(row=1, column=0, sticky="w", pady=2)
        self.margin_start_entry = ctk.CTkEntry(trim_set_frame, textvariable=self.pad_start_sec, width=60)
        self.margin_start_entry.grid(row=1, column=1, sticky="w", padx=5)
        ctk.CTkLabel(trim_set_frame, text="(フェードイン保護)", text_color="gray").grid(row=1, column=2, sticky="w")
        
        # 終了余韻マージン
        ctk.CTkLabel(trim_set_frame, text="終了マージン (秒):").grid(row=2, column=0, sticky="w", pady=2)
        self.margin_end_entry = ctk.CTkEntry(trim_set_frame, textvariable=self.pad_end_sec, width=60)
        self.margin_end_entry.grid(row=2, column=1, sticky="w", padx=5)
        ctk.CTkLabel(trim_set_frame, text="(余韻・フェードアウト保護)", text_color="gray").grid(row=2, column=2, sticky="w")
        
        # 実行ログ表示エリア
        self.log_box = ctk.CTkTextbox(self.frame, font=ctk.CTkFont(family="monospace", size=11), fg_color="#0a0a0c", text_color="#00FF66", border_width=1, border_color="#2a2a32")
        self.log_box.pack(fill="both", expand=True, padx=20, pady=(10, 15))
        self.log_box.configure(state="disabled")
        
    def log_message(self, msg):
        self.log_queue.put(msg)
        
    def process_queues(self):
        # ログ表示の更新
        while not self.log_queue.empty():
            m = self.log_queue.get()
            self.log_box.configure(state="normal")
            ts = datetime.now().strftime("%H:%M:%S")
            self.log_box.insert("end", f"[{ts}] {m}\n")
            self.log_box.see("end")
            self.log_box.configure(state="disabled")
            
        # レベルメーターの更新
        latest_rms = 0.0
        while not self.meter_queue.empty():
            latest_rms = self.meter_queue.get()
        # ボリュームの視覚表現スケーリング
        clamped = min(1.0, latest_rms * 10.0)
        self.vol_meter.set(clamped)
        
        self.after(50, self.process_queues)
        
    def on_dev_change(self, _):
        self.device_in_id = int(self.in_combo.get().split(":")[0])
        self.device_out_id = int(self.out_combo.get().split(":")[0])
        
    def browse_dir(self):
        d = filedialog.askdirectory(initialdir=self.save_dir.get())
        if d:
            self.save_dir.set(d)
            
    def on_standby_toggle(self):
        self.is_standby = (self.standby_switch.get() == 1)
        if self.is_standby:
            self.status_label.configure(text="Standby (Waiting for Spotify)", text_color="#FFA500")
            self.log_message("待機モード (Standby) を有効にしました。Spotifyの再生開始を待ちます。")
        else:
            if not self.is_recording:
                self.status_label.configure(text="Ready to Record", text_color="#1DB954")
            self.log_message("待機モードを解除しました。")
            
    def audio_callback(self, indata, outdata, frames, time, status):
        start_idx = self._current_start_ch - 1
        if indata.shape[1] >= start_idx + 2:
            stereo_data = indata[:, start_idx : start_idx + 2]
        else:
            stereo_data = indata[:, :2] if indata.shape[1] >= 2 else indata
            if stereo_data.shape[1] < 2:
                stereo_data = np.hstack([stereo_data, stereo_data])
                
        # RMSレベルメーター
        rms = np.sqrt(np.mean(stereo_data**2))
        self.meter_queue.put(rms)
        
        if self.is_recording:
            self.audio_buffer.append(stereo_data.copy())
            self.total_samples_recorded += frames
            
        # パススルー出力
        if self.is_muted.get():
            outdata.fill(0)
        else:
            out_ch = outdata.shape[1]
            if out_ch >= 2:
                outdata[:, :2] = stereo_data
                if out_ch > 2:
                    outdata[:, 2:] = 0
            else:
                outdata[:] = np.mean(stereo_data, axis=1, keepdims=True)
                
    def start_rec(self):
        if self.is_recording:
            return
            
        dev_info = sd.query_devices(self.device_in_id, 'input')
        max_ch = dev_info['max_input_channels']
        start_ch = self.record_ch_start.get()
        
        if start_ch + 1 > max_ch:
            self.log_message(f"Error: チャンネル {start_ch} は存在しません (最大 {max_ch}ch)")
            return
            
        try:
            out_dev_info = sd.query_devices(self.device_out_id, 'output')
            self.stream = sd.Stream(
                device=(self.device_in_id, self.device_out_id),
                samplerate=SAMPLE_RATE,
                channels=(max_ch, out_dev_info['max_output_channels']),
                dtype='float32',
                callback=self.audio_callback
            )
            self.stream.start()
        except Exception as e:
            self.log_message(f"録音開始エラー: {e}")
            return
            
        self.audio_buffer = []
        self.recording_history = []
        self.total_samples_recorded = 0
        self.is_recording = True
        self._current_start_ch = start_ch
        self.last_track_name = None
        
        # GUI状態切り替え
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.in_combo.configure(state="disabled")
        self.out_combo.configure(state="disabled")
        self.ch_entry.configure(state="disabled")
        self.thresh_entry.configure(state="disabled")
        self.margin_start_entry.configure(state="disabled")
        self.margin_end_entry.configure(state="disabled")
        
        info = self.current_spotify_info
        if info and info['status'] == 'OK':
            self.status_label.configure(text=f"● RECORDING: {info['name']}", text_color="#e91429")
            self.recording_history.append({
                "name": info["name"],
                "artist": info["artist"],
                "album": info["album"],
                "start_sample": 0
            })
            self.last_track_name = info["name"]
            self.log_message(f"録音を開始しました: {info['name']} ({start_ch}-{start_ch+1}ch)")
        else:
            self.status_label.configure(text="● RECORDING", text_color="#e91429")
            self.log_message(f"録音を開始しました (メタデータ未検出) on ch {start_ch}-{start_ch+1}")
            
    def stop_rec(self):
        if not self.is_recording:
            return
            
        self.is_recording = False
        self.is_standby = False
        self.standby_switch.deselect()
        
        if self.stream:
            self.stream.stop()
            self.stream.close()
            self.stream = None
            
        self.status_label.configure(text="Processing...", text_color="#1DB954")
        
        # スレッドを起動してロスレス分割とトリミングを実行
        if self.audio_buffer:
            full_audio = np.concatenate(self.audio_buffer, axis=0)
            
            # 録音開始時にトラックが記録されていなかった場合、フォールバックのトラック情報を付与
            if not self.recording_history:
                info = self.current_spotify_info
                name = info['name'] if info and info['status'] == 'OK' else f"Rec_{datetime.now().strftime('%H%M%S')}"
                artist = info['artist'] if info and info['status'] == 'OK' else "Unknown"
                album = info['album'] if info and info['status'] == 'OK' else "Captured"
                self.recording_history.append({
                    "name": name,
                    "artist": artist,
                    "album": album,
                    "start_sample": 0
                })
                
            save_dir = self.save_dir.get()
            threshold = self.silence_threshold_db.get()
            pad_start = self.pad_start_sec.get()
            pad_end = self.pad_end_sec.get()
            
            threading.Thread(
                target=process_and_save_tracks,
                args=(
                    full_audio,
                    list(self.recording_history),
                    save_dir,
                    threshold,
                    pad_start,
                    pad_end,
                    self.log_message,
                    self.on_processing_finished
                ),
                daemon=True
            ).start()
        else:
            self.log_message("録音データがありませんでした。")
            self.on_processing_finished()
            
    def on_processing_finished(self):
        # メインスレッドでのGUIコントロール復帰
        def gui_update():
            self.start_btn.configure(state="normal")
            self.stop_btn.configure(state="disabled")
            self.in_combo.configure(state="normal")
            self.out_combo.configure(state="normal")
            self.ch_entry.configure(state="normal")
            self.thresh_entry.configure(state="normal")
            self.margin_start_entry.configure(state="normal")
            self.margin_end_entry.configure(state="normal")
            self.status_label.configure(text="Ready to Record", text_color="#1DB954")
        self.after(0, gui_update)
        
    def poll_spotify(self):
        info = get_spotify_info()
        self.current_spotify_info = info
        
        if info['status'] == 'OK':
            current_track = info['name']
            self.track_title_label.configure(text=current_track, text_color="white")
            self.artist_label.configure(text=f"{info['artist']} • {info['album']}", text_color="#b3b3b3")
            
            # --- 自動録音待機 (Standby) ロジック ---
            if self.is_standby and not self.is_recording:
                if info['state'] == 'playing':
                    self.log_message(f"Spotifyの再生を検知しました。録音を開始します: {current_track}")
                    self.start_rec()
            
            # --- 曲の切り替わり検知ロジック (自動分割) ---
            if self.is_recording and self.last_track_name:
                if info['state'] == 'playing' and current_track != self.last_track_name:
                    self.log_message(f"曲の切り替わりを検知しました: {self.last_track_name} -> {current_track}")
                    # 新しい曲の開始位置（現在の録音サンプル数）を履歴に追加
                    self.recording_history.append({
                        "name": info["name"],
                        "artist": info["artist"],
                        "album": info["album"],
                        "start_sample": self.total_samples_recorded
                    })
                    self.last_track_name = current_track
                    self.status_label.configure(text=f"● RECORDING: {current_track}", text_color="#e91429")
                    
        elif info['status'] == 'PERMISSION_DENIED':
            self.track_title_label.configure(text="Permission Required", text_color="#FFA500")
            self.artist_label.configure(text="Check Settings > Privacy > Automation", text_color="#FFA500")
        elif info['status'] == 'CLOSED':
            self.track_title_label.configure(text="Spotify Not Running", text_color="gray")
            self.artist_label.configure(text="Please open Spotify app", text_color="gray")
        else:
            self.track_title_label.configure(text="Spotify Not Linked", text_color="gray")
            self.artist_label.configure(text="Check Permissions / Open Spotify", text_color="gray")
            
        self.after(CHECK_INTERVAL, self.poll_spotify)
        
    def on_closing(self):
        if self.is_recording:
            # 録音中の場合は停止と保存を走らせる
            self.stop_rec()
        self.destroy()

if __name__ == "__main__":
    app = SpotifyRecorderV2App()
    app.mainloop()
