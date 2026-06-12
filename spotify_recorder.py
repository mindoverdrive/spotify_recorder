import os
import subprocess
import threading
import wave
import queue
import numpy as np
import sounddevice as sd
from mutagen.id3 import ID3, TIT2, TPE1, TALB
from mutagen.wave import WAVE
from static_ffmpeg import add_paths
add_paths() # pydubのインポート前にパスを通す

from pydub import AudioSegment
from pydub.silence import detect_nonsilent
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
    """Spotifyから現在の再生情報を取得 (AppleScript) - 診断 & ウィンドウ名フォールバック付き"""
    try:
        subprocess.check_call(['pgrep', '-x', 'Spotify'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        return {'status': 'CLOSED'}

    # Bundle IDを使用して確実にSpotifyをターゲットにする
    script = 'tell application id "com.spotify.client" to get (name of current track) & "||" & (artist of current track) & "||" & (album of current track) & "||" & (player state of current track)'
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
        else:
            # エラー時に権限不足かどうかを確認
            err_msg = result.stderr.strip()
            if "not allowed" in err_msg or "許可されていません" in err_msg or "error -1743" in err_msg:
                return {'status': 'PERMISSION_DENIED', 'error': err_msg}
            if "error -600" in err_msg:
                return {'status': 'CLOSED'} # AppleScript上は起動していない扱い
    except Exception as e:
        return {'status': 'ERROR', 'error': str(e)}

    # Fallback to window title (これもSystem Eventsの許可が必要)
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

    return {'status': 'PERMISSION_DENIED', 'error': 'AppleScript link failed'}

def trim_silence_pydub(filename):
    try:
        audio = AudioSegment.from_wav(filename)
        nonsilent_ranges = detect_nonsilent(audio, min_silence_len=500, silence_thresh=-50)
        if nonsilent_ranges:
            start_trim = nonsilent_ranges[0][0]
            end_trim = nonsilent_ranges[-1][1]
            trimmed_audio = audio[start_trim:end_trim]
            trimmed_audio.export(filename, format="wav")
            return f"Trimmed: {start_trim}ms - {end_trim}ms"
        return "No silence trimmed."
    except Exception as e:
        return f"Trimming skipped or failed: {e}"

def save_wav_with_tags(filename, audio_data, info, log_callback):
    """保存とタグ付け (別スレッド)"""
    try:
        log_callback(f"Saving to {filename}...")
        with wave.open(filename, 'wb') as wf:
            wf.setnchannels(2) # 常にステレオ
            wf.setsampwidth(2) 
            wf.setframerate(SAMPLE_RATE)
            int_data = (audio_data * 32767).clip(-32768, 32767).astype(np.int16)
            wf.writeframes(int_data.tobytes())
        
        msg = trim_silence_pydub(filename)
        log_callback(msg)

        if info and info['status'].startswith('OK'):
            try:
                audio = WAVE(filename)
                if audio.tags is None:
                    audio.add_tags()
                
                audio.tags.add(TIT2(encoding=3, text=info['name']))
                audio.tags.add(TPE1(encoding=3, text=info['artist']))
                audio.tags.add(TALB(encoding=3, text=info['album']))
                audio.save()
                log_callback(f"Tagged: {os.path.basename(filename)}")
            except Exception as e:
                log_callback(f"Tagging failed: {e}")
        else:
            log_callback(f"Saved (No tags): {os.path.basename(filename)}")
    except Exception as e:
        log_callback(f"Save error: {e}")

class SpotifyRecorderApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Premium Spotify Recorder")
        self.geometry("620x750")
        
        self.is_recording = False
        self.is_muted = True
        self.current_spotify_info = None
        self.recording_start_info = None
        self.audio_buffer = []
        self.stream = None
        self.save_dir = ctk.StringVar(value=os.path.expanduser("~/Desktop"))
        self.record_ch_start = ctk.IntVar(value=5) 
        self.log_queue = queue.Queue()
        self.meter_queue = queue.Queue()
        
        self.audio_devices = sd.query_devices()
        self.input_devices_info = [(i, d) for i, d in enumerate(self.audio_devices) if d['max_input_channels'] > 0]
        self.output_devices_info = [(i, d) for i, d in enumerate(self.audio_devices) if d['max_output_channels'] > 0]
        self.input_device_strings = [f"{i}: {d['name']}" for i, d in self.input_devices_info]
        self.output_device_strings = [f"{i}: {d['name']}" for i, d in self.output_devices_info]

        self.device_in_id = sd.default.device[0]
        self.device_out_id = sd.default.device[1]
        self._current_start_ch = 5 # コールバック用のキャッシュ
        
        self.build_ui()
        self.after(CHECK_INTERVAL, self.poll_spotify)
        self.after(100, self.process_queues)
        self.protocol("WM_DELETE_WINDOW", self.on_closing)

    def build_ui(self):
        self.frame = ctk.CTkFrame(self, corner_radius=15)
        self.frame.pack(pady=15, padx=15, fill="both", expand=True)

        self.status_label = ctk.CTkLabel(self.frame, text="Ready to Record", font=ctk.CTkFont(size=20, weight="bold"), text_color="#1DB954")
        self.status_label.pack(pady=(15, 5))

        info_card = ctk.CTkFrame(self.frame, fg_color="#282828", corner_radius=10)
        info_card.pack(pady=10, padx=20, fill="x")
        self.track_title_label = ctk.CTkLabel(info_card, text="Current Track: -", font=ctk.CTkFont(size=16, weight="bold"))
        self.track_title_label.pack(pady=(15, 2))
        self.artist_label = ctk.CTkLabel(info_card, text="Artist: -", font=ctk.CTkFont(size=14), text_color="#b3b3b3")
        self.artist_label.pack(pady=(0, 15))

        # Audio Meter (無音の原因究明用)
        meter_frame = ctk.CTkFrame(self.frame, fg_color="transparent")
        meter_frame.pack(fill="x", padx=20, pady=2)
        ctk.CTkLabel(meter_frame, text="入力レベル確認:", font=ctk.CTkFont(size=11)).pack(side="left")
        self.vol_meter = ctk.CTkProgressBar(meter_frame, height=10, fg_color="#333", progress_color="#1DB954")
        self.vol_meter.pack(side="left", fill="x", expand=True, padx=10)
        self.vol_meter.set(0)

        settings_frame = ctk.CTkFrame(self.frame, fg_color="transparent")
        settings_frame.pack(pady=5, padx=20, fill="x")

        ctk.CTkLabel(settings_frame, text="Audio Devices", font=ctk.CTkFont(size=12, weight="bold")).pack(anchor="w", pady=(5, 2))
        self.in_combo = ctk.CTkOptionMenu(settings_frame, values=self.input_device_strings, command=self.on_dev_change, dropdown_font=ctk.CTkFont(size=12))
        self.in_combo.pack(fill="x", pady=2)
        self.out_combo = ctk.CTkOptionMenu(settings_frame, values=self.output_device_strings, command=self.on_dev_change, dropdown_font=ctk.CTkFont(size=12))
        self.out_combo.pack(fill="x", pady=2)

        ch_frame = ctk.CTkFrame(settings_frame, fg_color="transparent")
        ch_frame.pack(fill="x", pady=10)
        ctk.CTkLabel(ch_frame, text="録音開始ch (Start at):", font=ctk.CTkFont(size=13)).pack(side="left")
        self.ch_entry = ctk.CTkEntry(ch_frame, textvariable=self.record_ch_start, width=50)
        self.ch_entry.pack(side="left", padx=10)
        ctk.CTkLabel(ch_frame, text="(Loopbackなら 5 など)", font=ctk.CTkFont(size=11), text_color="gray").pack(side="left")

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

        self.mon_switch = ctk.CTkSwitch(self.frame, text="モニタリング (音を聴く)", command=self.toggle_mon)
        self.mon_switch.pack(pady=5)
        self.mon_switch.deselect()

        ctk.CTkLabel(self.frame, text="Process Log", font=ctk.CTkFont(size=12)).pack(anchor="w", padx=20)
        self.log_box = ctk.CTkTextbox(self.frame, height=100, fg_color="#121212", text_color="#1DB954")
        self.log_box.pack(pady=(0, 15), padx=20, fill="both", expand=True)
        self.log_box.configure(state="disabled")

        # デフォルトデバイス名をセット
        if self.input_device_strings:
            n = next((d for d in self.input_device_strings if d.startswith(str(self.device_in_id)+":")), self.input_device_strings[0])
            self.in_combo.set(n)
        if self.output_device_strings:
            n = next((d for d in self.output_device_strings if d.startswith(str(self.device_out_id)+":")), self.output_device_strings[0])
            self.out_combo.set(n)

    def on_dev_change(self, _):
        self.device_in_id = int(self.in_combo.get().split(":")[0])
        self.device_out_id = int(self.out_combo.get().split(":")[0])

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
        
        # ボリュームメーターの処理
        latest_rms = 0.0
        while not self.meter_queue.empty():
            latest_rms = self.meter_queue.get()
        # ボリュームの視覚的スケーリング (単純な対数的な表現に近い形)
        clamped = min(1.0, latest_rms * 10.0) 
        self.vol_meter.set(clamped)

        self.after(50, self.process_queues)

    def browse_dir(self):
        d = filedialog.askdirectory(initialdir=self.save_dir.get())
        if d: self.save_dir.set(d)

    def toggle_mon(self):
        self.is_muted = not (self.mon_switch.get() == 1)

    def audio_callback(self, indata, outdata, frames, time, status):
        start_idx = self._current_start_ch - 1
        # 入力から指定チャンネルを抜き出す
        if indata.shape[1] >= start_idx + 2:
            stereo_data = indata[:, start_idx : start_idx + 2]
        else:
            stereo_data = indata[:, :2] if indata.shape[1] >= 2 else indata

        # レベルメーター用にRMS値（音量）を計算してキューに送る
        rms = np.sqrt(np.mean(stereo_data**2))
        self.meter_queue.put(rms)

        # 録音中ならバッファに詰める
        if self.is_recording:
            self.audio_buffer.append(stereo_data.copy())
        
        # パススルー先への出力
        if self.is_muted:
            outdata.fill(0)
        else:
            out_ch = outdata.shape[1]
            if out_ch >= 2:
                outdata[:, :2] = stereo_data
                if out_ch > 2: outdata[:, 2:] = 0
            else:
                outdata[:] = np.mean(stereo_data, axis=1, keepdims=True)

    def split_and_save_buffer(self, info_to_save):
        """現在のバッファ内容を非同期で保存し、バッファを空にするロジック（自動曲分割用）"""
        if not self.audio_buffer:
            return
            
        data = np.concatenate(self.audio_buffer, axis=0)
        self.audio_buffer = [] # 即座にクリアして次の曲の録音に備える
        
        if info_to_save and info_to_save['status'].startswith('OK'):
            name_raw = f"{info_to_save['name']} - {info_to_save['artist']}"
            name = "".join(x for x in name_raw if x.isalnum() or x in " -_")
        else:
            name = f"Rec_{datetime.now().strftime('%H%M%S')}"
            
        path = os.path.join(self.save_dir.get(), f"{name}.wav")
        threading.Thread(target=save_wav_with_tags, args=(path, data, info_to_save, self.log_message)).start()

    def start_rec(self):
        dev_info = sd.query_devices(self.device_in_id, 'input')
        max_ch = dev_info['max_input_channels']
        start_ch = self.record_ch_start.get()
        
        if start_ch + 1 > max_ch:
            self.log_message(f"Error: チャンネル {start_ch} は存在しません (最大 {max_ch}ch)")
            return

        try:
            self.stream = sd.Stream(
                device=(self.device_in_id, self.device_out_id),
                samplerate=SAMPLE_RATE,
                channels=(max_ch, sd.query_devices(self.device_out_id, 'output')['max_output_channels']),
                dtype='float32',
                callback=self.audio_callback
            )
            self.stream.start()
        except Exception as e:
            self.log_message(f"Steam start error: {e}")
            return

        self.audio_buffer = []
        self.is_recording = True
        self._current_start_ch = self.record_ch_start.get() # ここで値を固定
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
        
        # 最後に残っているバッファを保存して終了
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
            
            # --- 曲の切り替わり検知ロジック (自動分割) ---
            if self.is_recording and self.recording_start_info:
                prev_info = self.recording_start_info
                prev_track = prev_info['name'] if prev_info['status'].startswith('OK') else None
                
                # 再生中で、曲名がさっきまでと変わった場合
                if info['state'] == 'playing' and current_track != prev_track:
                    self.log_message(f"Track changed. Splitting file: {prev_track} -> {current_track}")
                    # 前の曲を保存
                    self.split_and_save_buffer(prev_info)
                    # 次の曲の情報に更新して録音継続
                    self.recording_start_info = info
                    self.status_label.configure(text=f"● RECORDING: {current_track}", text_color="#e91429")
        elif info['status'] == 'PERMISSION_DENIED':
            self.track_title_label.configure(text="Permission Required", text_color="#FFA500")
            self.artist_label.configure(text="Check System Settings > Privacy > Automation", text_color="#FFA500")
        elif info['status'] == 'CLOSED':
            self.track_title_label.configure(text="Spotify Not Running", text_color="gray")
            self.artist_label.configure(text="Please open Spotify app", text_color="gray")
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
