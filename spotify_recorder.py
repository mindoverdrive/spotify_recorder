import os
import subprocess
import threading
import wave
import queue
import numpy as np
import sounddevice as sd
import customtkinter as ctk
from tkinter import filedialog
from datetime import datetime

# --- 設定 ---
SAMPLE_RATE = 44100            # デフォルトサンプリングレート
CHECK_INTERVAL = 1000          # Spotify監視間隔 (ms)

# UIテーマ設定
ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("green")

def get_spotify_info():
    """Spotifyから現在の再生情報を取得 (AppleScript)"""
    try:
        subprocess.check_call(['pgrep', '-x', 'Spotify'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        return {'status': 'CLOSED'}

    script = 'tell application "Spotify" to get (name of current track) & "||" & (artist of current track) & "||" & (album of current track) & "||" & (player state of current track)'
    try:
        result = subprocess.run(['osascript', '-e', script], capture_output=True, text=True, timeout=1)
        if result.returncode == 0:
            info = result.stdout.strip().split('||')
            if len(info) >= 4:
                return {
                    'status': 'OK',
                    'name': info[0],
                    'artist': info[1],
                    'album': info[2],
                    'state': info[3]
                }
    except:
        pass

    fallback_script = 'tell application "System Events" to get name of first window of (first process whose name is "Spotify")'
    try:
        res_fb = subprocess.run(['osascript', '-e', fallback_script], capture_output=True, text=True, timeout=1)
        if res_fb.returncode == 0:
            title = res_fb.stdout.strip()
            if " - " in title:
                parts = title.split(" - ", 1)
                return {
                    'status': 'OK_WINDOW',
                    'name': parts[0],
                    'artist': parts[1],
                    'album': 'Captured from Window',
                    'state': 'playing'
                }
    except:
        pass

    return {'status': 'PERMISSION_DENIED'}

def save_wav_pure(filename, audio_data, sample_rate, log_callback):
    """保存のみ (ID3タグ・無音トリミング削除し、高音質なピュアデータを保存)"""
    try:
        log_callback(f"Saving to {filename}...")
        with wave.open(filename, 'wb') as wf:
            wf.setnchannels(2) 
            wf.setsampwidth(2) # 16bit format
            wf.setframerate(sample_rate)
            wf.writeframes(audio_data.tobytes())
        log_callback(f"Saved: {os.path.basename(filename)}")
    except Exception as e:
        log_callback(f"Save error: {e}")

class SpotifyRecorderApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("High-Fidelity Spotify Recorder")
        self.geometry("620x650")
        
        self.is_recording = False
        self.current_spotify_info = None
        self.recording_start_info = None
        self.audio_buffer = []
        self.stream = None
        self.save_dir = ctk.StringVar(value=os.path.expanduser("~/Desktop"))
        self.record_ch_start = ctk.IntVar(value=5) # デフォルトを 5 に設定
        self.silent_seconds_count = 0.0
        self.silent_warning_shown = False 
        
        self.log_queue = queue.Queue()
        self.latest_meter_val = 0.0 # ロックフリーでメーターを更新する値
        self.audio_lock = threading.Lock()
        
        self.audio_devices = sd.query_devices()
        self.input_devices_info = [(i, d) for i, d in enumerate(self.audio_devices) if d['max_input_channels'] > 0]
        self.input_device_strings = [f"{i}: {d['name']}" for i, d in self.input_devices_info]

        # 優先デバイス（LoopbackやBlackHole）の自動検出と選択
        preferred_id = None
        for idx, dev in self.input_devices_info:
            name_lower = dev['name'].lower()
            if "loopback" in name_lower:
                preferred_id = idx
                break
        if preferred_id is None:
            for idx, dev in self.input_devices_info:
                name_lower = dev['name'].lower()
                if "blackhole" in name_lower:
                    preferred_id = idx
                    break

        if preferred_id is not None:
            self.device_in_id = preferred_id
        else:
            self.device_in_id = sd.default.device[0] if sd.default.device[0] is not None else 0

        self.current_sample_rate = SAMPLE_RATE
        self.update_sample_rate()
        
        self.build_ui()
        self.after(CHECK_INTERVAL, self.poll_spotify)
        self.after(50, self.process_queues)
        self.protocol("WM_DELETE_WINDOW", self.on_closing)

    def update_sample_rate(self):
        try:
            dev_info = sd.query_devices(self.device_in_id, 'input')
            self.current_sample_rate = int(dev_info.get('default_samplerate', SAMPLE_RATE))
        except Exception as e:
            self.current_sample_rate = SAMPLE_RATE

    def build_ui(self):
        self.frame = ctk.CTkFrame(self, corner_radius=15)
        self.frame.pack(pady=15, padx=15, fill="both", expand=True)

        self.status_label = ctk.CTkLabel(self.frame, text="Ready to Record", font=ctk.CTkFont(size=20, weight="bold"), text_color="#1DB954")
        self.status_label.pack(pady=(15, 5))

        # 無音検知アラート表示用ラベル
        self.warning_label = ctk.CTkLabel(self.frame, text="", font=ctk.CTkFont(size=11, weight="bold"), text_color="#e91429", wraplength=550)
        self.warning_label.pack(pady=(2, 5))

        info_card = ctk.CTkFrame(self.frame, fg_color="#282828", corner_radius=10)
        info_card.pack(pady=10, padx=20, fill="x")
        self.track_title_label = ctk.CTkLabel(info_card, text="Current Track: -", font=ctk.CTkFont(size=16, weight="bold"))
        self.track_title_label.pack(pady=(15, 2))
        self.artist_label = ctk.CTkLabel(info_card, text="Artist: -", font=ctk.CTkFont(size=14), text_color="#b3b3b3")
        self.artist_label.pack(pady=(0, 15))

        # Audio Meter
        meter_frame = ctk.CTkFrame(self.frame, fg_color="transparent")
        meter_frame.pack(fill="x", padx=20, pady=2)
        ctk.CTkLabel(meter_frame, text="入力レベル確認:", font=ctk.CTkFont(size=11)).pack(side="left")
        self.vol_meter = ctk.CTkProgressBar(meter_frame, height=10, fg_color="#333", progress_color="#1DB954")
        self.vol_meter.pack(side="left", fill="x", expand=True, padx=10)
        self.vol_meter.set(0)

        settings_frame = ctk.CTkFrame(self.frame, fg_color="transparent")
        settings_frame.pack(pady=5, padx=20, fill="x")

        # 1. 入力デバイスのみ設定 (出力機能は再生同期のカクつきをなくすため完全削除)
        ctk.CTkLabel(settings_frame, text="Input Device", font=ctk.CTkFont(size=12, weight="bold")).pack(anchor="w", pady=(5, 2))
        self.in_combo = ctk.CTkOptionMenu(settings_frame, values=self.input_device_strings, command=self.on_dev_change, dropdown_font=ctk.CTkFont(size=12))
        self.in_combo.pack(fill="x", pady=2)

        ch_frame = ctk.CTkFrame(settings_frame, fg_color="transparent")
        ch_frame.pack(fill="x", pady=10)
        ctk.CTkLabel(ch_frame, text="録音開始ch (Start at):", font=ctk.CTkFont(size=13)).pack(side="left")
        self.ch_entry = ctk.CTkEntry(ch_frame, textvariable=self.record_ch_start, width=50)
        self.ch_entry.pack(side="left", padx=10)
        ctk.CTkLabel(ch_frame, text="(Loopback等の設定チャンネルを指定。例: 5)", font=ctk.CTkFont(size=11), text_color="gray").pack(side="left")

        dir_frame = ctk.CTkFrame(settings_frame, fg_color="transparent")
        dir_frame.pack(fill="x", pady=5)
        self.dir_label = ctk.CTkLabel(dir_frame, textvariable=self.save_dir, anchor="w", fg_color="#333", corner_radius=5, height=28)
        self.dir_label.pack(side="left", fill="x", expand=True, padx=(0, 5))
        ctk.CTkButton(dir_frame, text="Browse", width=60, command=self.browse_dir).pack(side="right")

        ctrl_frame = ctk.CTkFrame(self.frame, fg_color="transparent")
        ctrl_frame.pack(pady=15, padx=20, fill="x")
        self.start_btn = ctk.CTkButton(ctrl_frame, text="Start Recording", height=50, fg_color="#1DB954", hover_color="#1ed760", font=ctk.CTkFont(weight="bold"), command=self.start_rec)
        self.start_btn.pack(side="left", expand=True, fill="x", padx=(0, 5))
        self.stop_btn = ctk.CTkButton(ctrl_frame, text="Stop", height=50, fg_color="#e91429", hover_color="#f03e3e", state="disabled", command=self.stop_rec)
        self.stop_btn.pack(side="right", expand=True, fill="x", padx=(5, 0))

        ctk.CTkLabel(self.frame, text="Process Log", font=ctk.CTkFont(size=12)).pack(anchor="w", padx=20)
        self.log_box = ctk.CTkTextbox(self.frame, height=100, fg_color="#121212", text_color="#1DB954")
        self.log_box.pack(pady=(0, 15), padx=20, fill="both", expand=True)
        self.log_box.configure(state="disabled")

        if self.input_device_strings:
            n = next((d for d in self.input_device_strings if d.startswith(str(self.device_in_id)+":")), self.input_device_strings[0])
            self.in_combo.set(n)

    def on_dev_change(self, _):
        self.device_in_id = int(self.in_combo.get().split(":")[0])
        self.update_sample_rate()
        self.log_message(f"Input device changed to ID {self.device_in_id}. Detected sample rate: {self.current_sample_rate}Hz")

    def log_message(self, msg):
        self.log_queue.put(msg)

    def process_queues(self):
        # ログキューの処理
        while not self.log_queue.empty():
            m = self.log_queue.get()
            self.log_box.configure(state="normal")
            ts = datetime.now().strftime("%H:%M:%S")
            self.log_box.insert("end", f"[{ts}] {m}\n")
            self.log_box.see("end")
            self.log_box.configure(state="disabled")
        
        # ボリュームの視覚的スケーリング (ロックフリー変数から取得)
        clamped = min(1.0, self.latest_meter_val * 2.0)
        self.vol_meter.set(clamped)

        # 接続されているデバイス情報を検索してデバイス名を取得
        current_dev_name = ""
        for idx, dev in self.input_devices_info:
            if idx == self.device_in_id:
                current_dev_name = dev['name'].lower()
                break

        # 録音中の無音検知
        if self.is_recording:
            if self.latest_meter_val <= 0.0001:  # レベルがほぼ0
                self.silent_seconds_count += 0.05
            else:
                self.silent_seconds_count = 0.0  # リセット
                if self.silent_warning_shown:
                    # 音声が戻ったら警告を解除
                    self.silent_warning_shown = False
                    self.warning_label.configure(text="")
                    self.log_message("Audio signal detected. Warning cleared.")

            if self.silent_seconds_count >= 3.0 and not self.silent_warning_shown:
                self.silent_warning_shown = True
                msg = f"【警告】入力音声レベルがゼロです。以下をご確認ください：\n1. macOSの「システム設定 > プライバシーとセキュリティ > マイク」で実行元（ターミナル等）が許可されているか\n2. Loopbackの設定で、Spotifyの音が選択した録音開始ch（現在: ch {self.record_ch_start.get()}）に正しく配線されているか"
                self.warning_label.configure(text=msg, text_color="#e91429")
                self.log_message("WARNING: No audio input detected. Check mic permissions or Loopback routing.")
        else:
            self.silent_seconds_count = 0.0
            self.silent_warning_shown = False
            
            if self.warning_label.cget("text") != "" and not self.warning_label.cget("text").startswith("【警告】"):
                self.warning_label.configure(text="")

        self.after(50, self.process_queues)

    def browse_dir(self):
        d = filedialog.askdirectory(initialdir=self.save_dir.get())
        if d: self.save_dir.set(d)

    def audio_callback(self, indata, frames, time_info, status):
        # (入力専用 InputStream のコールバック)
        if status:
            pass # エラー状態の無視によるカクつき防止
            
        start_idx = self.record_ch_start.get() - 1
        # 入力から指定チャンネルを抜き出す
        if indata.shape[1] >= start_idx + 2:
            stereo_data = indata[:, start_idx : start_idx + 2]
        else:
            stereo_data = indata[:, :2] if indata.shape[1] >= 2 else indata

        # レベルメーター用（ピーク値で軽量に計算し、ロック回避で代入）
        if stereo_data.size > 0:
            peak = float(np.max(np.abs(stereo_data))) / 32768.0
            self.latest_meter_val = peak

        # 録音中ならバッファに詰める
        if self.is_recording:
            with self.audio_lock:
                self.audio_buffer.append(stereo_data.copy())

    def split_and_save_buffer(self, info_to_save):
        """現在のバッファ内容を非同期で保存し、バッファを空にする"""
        with self.audio_lock:
            data_to_save = self.audio_buffer
            self.audio_buffer = [] 
            
        if not data_to_save:
            return
            
        sample_rate = self.current_sample_rate
        
        def async_save():
            try:
                data = np.concatenate(data_to_save, axis=0)
                if info_to_save and info_to_save['status'].startswith('OK'):
                    name_raw = f"{info_to_save['name']} - {info_to_save['artist']}"
                    name = "".join(x for x in name_raw if x.isalnum() or x in " -_")
                else:
                    name = f"Rec_{datetime.now().strftime('%H%M%S')}"
                    
                path = os.path.join(self.save_dir.get(), f"{name}.wav")
                save_wav_pure(path, data, sample_rate, self.log_message)
            except Exception as e:
                self.log_message(f"Concatenate/Save error: {e}")

        threading.Thread(target=async_save).start()

    def start_rec(self):
        dev_info = sd.query_devices(self.device_in_id, 'input')
        max_ch = dev_info['max_input_channels']
        start_ch = self.record_ch_start.get()
        
        if start_ch + 1 > max_ch:
            self.log_message(f"Error: チャンネル {start_ch} は存在しません (最大 {max_ch}ch)")
            return

        try:
            # 検出されたサンプリングレートを使用し、blocksizeとlatencyを適切に設定して音飛びを防止
            self.stream = sd.InputStream(
                device=self.device_in_id,
                samplerate=self.current_sample_rate,
                channels=max_ch,
                dtype='int16',  # 直接16bitの整数で受け取ることで高速化
                blocksize=2048, # 大きめのブロックサイズでオーバーヘッドを抑制
                latency='high', # レイテンシを高めに設定してバッファ不足（アンダーラン）を防ぐ
                callback=self.audio_callback
            )
            self.stream.start()
            self.log_message(f"Stream started: {self.current_sample_rate}Hz, blocksize=2048, latency='high'")
        except Exception as e:
            self.log_message(f"Steam start error: {e}")
            return

        self.audio_buffer = []
        self.silent_seconds_count = 0.0
        self.silent_warning_shown = False
        self.warning_label.configure(text="")
        self.is_recording = True
        self.recording_start_info = self.current_spotify_info
        
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.in_combo.configure(state="disabled")
        self.ch_entry.configure(state="disabled")
        
        info = self.recording_start_info
        if info and info['status'].startswith('OK'):
            self.status_label.configure(text=f"● RECORDING: {info['name']}", text_color="#e91429")
            self.log_message(f"Recording: {info['name']} ({start_ch}-{start_ch+1}ch)")
        else:
            self.status_label.configure(text="● RECORDING", text_color="#e91429")
            self.log_message(f"Recording started (No metadata detected) on ch {start_ch}-{start_ch+1}")

    def stop_rec(self):
        self.is_recording = False
        if self.stream:
            self.stream.stop()
            self.stream.close()
            self.stream = None
        
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.in_combo.configure(state="normal")
        self.ch_entry.configure(state="normal")
        self.status_label.configure(text="Processing...", text_color="#1DB954")
        
        self.split_and_save_buffer(self.recording_start_info)
        self.recording_start_info = None
        
        self.after(2000, lambda: self.status_label.configure(text="Ready to Record"))

    def poll_spotify(self):
        info = get_spotify_info()
        self.current_spotify_info = info
        
        if info['status'].startswith('OK'):
            current_track = info['name']
            self.track_title_label.configure(text=current_track, text_color="white")
            self.artist_label.configure(text=info['artist'], text_color="#b3b3b3")
            
            if self.is_recording and self.recording_start_info:
                prev_info = self.recording_start_info
                prev_track = prev_info['name'] if prev_info['status'].startswith('OK') else None
                
                if info['state'] == 'playing' and current_track != prev_track:
                    self.log_message(f"Track changed. Splitting file: {prev_track} -> {current_track}")
                    self.split_and_save_buffer(prev_info)
                    self.recording_start_info = info
                    self.status_label.configure(text=f"● RECORDING: {current_track}", text_color="#e91429")
        else:
            self.track_title_label.configure(text="Spotify Not Linked", text_color="gray")
            self.artist_label.configure(text="Check Permissions / Open Spotify", text_color="gray")
            
        self.after(CHECK_INTERVAL, self.poll_spotify)

    def on_closing(self):
        if self.is_recording:
            self.stop_rec()
        self.destroy()

if __name__ == "__main__":
    app = SpotifyRecorderApp()
    app.mainloop()

