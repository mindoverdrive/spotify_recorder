import csv
import glob
import json
import os
import shutil
import subprocess
import threading
import time

import numpy as np


SPOTIFY_LOSSLESS_ENUM_CANDIDATE = 5
SPOTIFY_RECOMMENDED_MIN_MBPS = 2.0
QOBUZ_RECOMMENDED_MIN_MBPS = 10.0
NETWORK_TEST_FRESHNESS_SEC = 30 * 60


def _parse_pref_value(raw_value):
    value = raw_value.strip()
    if value == "true":
        return True
    if value == "false":
        return False
    if value.startswith('"') and value.endswith('"'):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value[1:-1]
    try:
        return int(value)
    except ValueError:
        return value


def parse_spotify_prefs(text):
    prefs = {}
    for line in text.splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        prefs[key.strip()] = _parse_pref_value(value)
    return prefs


def read_spotify_quality_settings(spotify_dir=None):
    base_dir = spotify_dir or os.path.expanduser("~/Library/Application Support/Spotify")
    candidates = glob.glob(os.path.join(base_dir, "Users", "*-user", "prefs"))
    if not candidates:
        return {
            "available": False,
            "error": "Spotifyユーザー設定ファイルが見つかりません",
        }

    path = max(candidates, key=os.path.getmtime)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            prefs = parse_spotify_prefs(handle.read())
    except OSError as exc:
        return {"available": False, "error": str(exc)}

    streaming_quality = prefs.get(
        "audio.play_bitrate_non_metered_enumeration",
        prefs.get("audio.play_bitrate_enumeration"),
    )
    return {
        "available": True,
        "streaming_quality_raw": streaming_quality,
        "download_quality_raw": prefs.get("audio.sync_bitrate_enumeration"),
        "auto_downgrade": prefs.get("audio.allow_downgrade"),
        "normalize": prefs.get("audio.normalize_v2"),
        "automix": prefs.get("audio.automix"),
        "source_note": "Spotify非公開prefsの観測値",
    }


def evaluate_spotify_quality_settings(settings):
    warnings = []
    notes = ["Spotifyの公開APIは再生中の実効コーデック・ビット深度を返しません"]
    if not settings.get("available"):
        warnings.append(settings.get("error", "Spotify設定を取得できません"))
        return {
            "conditions_pass": False,
            "lossless_candidate": False,
            "auto_downgrade_disabled": False,
            "warnings": warnings,
            "notes": notes,
            "label": "設定未確認・実効Lossless未証明",
        }

    quality = settings.get("streaming_quality_raw")
    lossless_candidate = quality == SPOTIFY_LOSSLESS_ENUM_CANDIDATE
    auto_downgrade_disabled = settings.get("auto_downgrade") is False
    if not lossless_candidate:
        warnings.append(
            f"Spotify音質設定の観測値がLossless候補(5)ではありません: {quality!r}"
        )
    if not auto_downgrade_disabled:
        warnings.append("Spotifyの音質自動低下OFFを確認できません")
    if settings.get("normalize") is not False:
        warnings.append("Spotifyの音量の均一OFFを確認できません")
    if settings.get("automix") is not False:
        warnings.append("SpotifyのAutomix OFFを確認できません")

    conditions_pass = not warnings
    label = (
        "Lossless設定条件適合・実効品質は未証明"
        if conditions_pass
        else "Lossless設定条件に要確認・実効品質は未証明"
    )
    notes.append("音質値5の意味はSpotifyの非公開実装に基づく候補判定です")
    return {
        "conditions_pass": conditions_pass,
        "lossless_candidate": lossless_candidate,
        "auto_downgrade_disabled": auto_downgrade_disabled,
        "warnings": warnings,
        "notes": notes,
        "label": label,
    }


def format_spotify_quality_settings(settings):
    evaluation = evaluate_spotify_quality_settings(settings)
    if not settings.get("available"):
        return evaluation["label"]
    return (
        f"{evaluation['label']} / 音質値 {settings.get('streaming_quality_raw')} / "
        f"自動低下 {'OFF' if settings.get('auto_downgrade') is False else '未確認'} / "
        f"音量均一 {'OFF' if settings.get('normalize') is False else '未確認'}"
    )


def parse_network_quality_output(
    output,
    measured_at=None,
    minimum_mbps=SPOTIFY_RECOMMENDED_MIN_MBPS,
    provider_label="Spotify",
):
    payload = json.loads(output)
    throughput_bps = float(payload.get("dl_throughput", 0.0))
    download_mbps = throughput_bps / 1_000_000.0
    base_rtt = payload.get("base_rtt")
    interface_name = payload.get("interface_name")
    warnings = []
    if download_mbps < float(minimum_mbps):
        warnings.append(
            f"下り実測が{provider_label}推奨下限{float(minimum_mbps):.1f} Mbps未満です"
        )
    if interface_name and str(interface_name).startswith("utun"):
        warnings.append("VPN/トンネル経由の測定です")
    return {
        "available": True,
        "measured_at": float(measured_at if measured_at is not None else time.time()),
        "download_mbps": download_mbps,
        "base_rtt_ms": None if base_rtt is None else float(base_rtt),
        "interface_name": interface_name,
        "endpoint": payload.get("test_endpoint"),
        "pass": download_mbps >= float(minimum_mbps),
        "minimum_mbps": float(minimum_mbps),
        "provider": str(provider_label).lower(),
        "warnings": warnings,
    }


def run_network_quality_test(
    max_runtime=15,
    minimum_mbps=SPOTIFY_RECOMMENDED_MIN_MBPS,
    provider_label="Spotify",
):
    executable = shutil.which("networkQuality")
    if not executable:
        return {"available": False, "error": "networkQualityコマンドがありません"}
    try:
        result = subprocess.run(
            [executable, "-c", "-u", "-M", str(int(max_runtime))],
            capture_output=True,
            text=True,
            timeout=max_runtime + 15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"available": False, "error": str(exc)}
    if result.returncode != 0:
        return {
            "available": False,
            "error": result.stderr.strip() or f"networkQuality終了コード: {result.returncode}",
        }
    try:
        return parse_network_quality_output(
            result.stdout,
            minimum_mbps=minimum_mbps,
            provider_label=provider_label,
        )
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        return {"available": False, "error": f"回線実測結果を解析できません: {exc}"}


def network_test_is_fresh(result, now=None):
    if not result or not result.get("available") or "measured_at" not in result:
        return False
    current = time.time() if now is None else float(now)
    return 0.0 <= current - float(result["measured_at"]) <= NETWORK_TEST_FRESHNESS_SEC


def format_network_quality(result):
    if not result or not result.get("available"):
        return f"回線実測なし: {(result or {}).get('error', '未実行')}"
    rtt = result.get("base_rtt_ms")
    rtt_text = "不明" if rtt is None else f"{rtt:.1f} ms"
    verdict = "基準通過" if result.get("pass") else "基準未達"
    return (
        f"下り {result['download_mbps']:.1f} Mbps / RTT {rtt_text} / "
        f"{result.get('interface_name') or 'interface不明'} / {verdict}"
    )


class ProcessNetworkMonitor:
    def __init__(self, process_prefixes=("spotify",), provider_label="Spotify", interval_sec=2.0):
        self.process_prefixes = tuple(str(value).lower() for value in process_prefixes)
        self.provider_label = str(provider_label)
        self.interval_sec = float(interval_sec)
        self._lock = threading.Lock()
        self._process = None
        self._thread = None
        self._current_snapshot = {}
        self._last_snapshot = None
        self._last_snapshot_time = None
        self._samples = []
        self._error = None
        self._started = False
        self._saw_header = False

    def start(self):
        executable = shutil.which("nettop")
        if not executable:
            self._error = "nettopコマンドがありません"
            return False
        try:
            self._process = subprocess.Popen(
                [
                    executable,
                    "-P",
                    "-L",
                    "0",
                    "-s",
                    str(self.interval_sec),
                    "-x",
                    "-J",
                    "bytes_in,bytes_out",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            self._error = str(exc)
            return False
        self._started = True
        self._thread = threading.Thread(target=self._read_output, daemon=True)
        self._thread.start()
        return True

    def _is_target_process(self, process_name):
        base_name, separator, pid = process_name.rpartition(".")
        if separator and pid.isdigit():
            process_name = base_name
        return process_name.lower().startswith(self.process_prefixes)

    def _read_output(self):
        assert self._process is not None
        assert self._process.stdout is not None
        try:
            for raw_line in self._process.stdout:
                try:
                    row = next(csv.reader([raw_line]))
                except csv.Error:
                    continue
                if not row:
                    continue
                if row[0] == "" and "bytes_in" in row:
                    if self._saw_header:
                        self._commit_snapshot()
                    self._saw_header = True
                    self._current_snapshot = {}
                    continue
                if not self._saw_header or len(row) < 3:
                    continue
                process_name = row[0].strip()
                if not self._is_target_process(process_name):
                    continue
                try:
                    self._current_snapshot[process_name] = int(row[1])
                except ValueError:
                    continue
        except OSError as exc:
            self._error = str(exc)

    def _commit_snapshot(self):
        now = time.monotonic()
        snapshot = dict(self._current_snapshot)
        with self._lock:
            if self._last_snapshot is not None and self._last_snapshot_time is not None:
                elapsed = max(now - self._last_snapshot_time, 0.001)
                delta_bytes = sum(
                    max(0, current - self._last_snapshot.get(process_name, current))
                    for process_name, current in snapshot.items()
                )
                self._samples.append(
                    {
                        "elapsed_sec": elapsed,
                        "bytes_in": delta_bytes,
                        "target_process_seen": bool(snapshot),
                        "spotify_process_seen": bool(snapshot),
                    }
                )
            self._last_snapshot = snapshot
            self._last_snapshot_time = now

    def stop(self):
        process = self._process
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                process.kill()
        if self._thread:
            self._thread.join(timeout=1.0)
        if self._saw_header and self._current_snapshot:
            self._commit_snapshot()
        return self.summary()

    def summary(self):
        with self._lock:
            samples = list(self._samples)
        duration = sum(sample["elapsed_sec"] for sample in samples)
        total_bytes = sum(sample["bytes_in"] for sample in samples)
        rates_kbps = [
            sample["bytes_in"] * 8.0 / sample["elapsed_sec"] / 1000.0
            for sample in samples
        ]
        observed = sum(1 for sample in samples if sample["target_process_seen"])
        notes = [
            f"{self.provider_label}通信量はバッファ/キャッシュの影響を受け、実効コーデックの証明には使えません"
        ]
        if self._started and observed == 0:
            notes.append(
                f"{self.provider_label}通信を観測できませんでした（オフライン/キャッシュ再生の可能性を含む）"
            )
        return {
            "available": self._started and self._error is None,
            "error": self._error,
            "sample_count": len(samples),
            "observed_intervals": observed,
            "duration_sec": duration,
            "inbound_total_bytes": total_bytes,
            "inbound_average_kbps": (
                total_bytes * 8.0 / duration / 1000.0 if duration > 0.0 else 0.0
            ),
            "inbound_peak_kbps": max(rates_kbps, default=0.0),
            "zero_transfer_intervals": sum(
                1
                for sample in samples
                if sample["target_process_seen"] and sample["bytes_in"] == 0
            ),
            "notes": notes,
            "provider": self.provider_label.lower(),
        }


class SpotifyNetworkMonitor(ProcessNetworkMonitor):
    def __init__(self, interval_sec=2.0):
        super().__init__(("spotify",), "Spotify", interval_sec)


class CaptureQualityAudit:
    def __init__(
        self,
        sample_rate,
        device_name,
        spotify_settings,
        network_test=None,
        recording_frame_offset=0,
        provider="spotify",
        source_evaluation=None,
    ):
        self.sample_rate = int(sample_rate)
        self.device_name = str(device_name)
        self.spotify_settings = dict(spotify_settings)
        self.provider = str(provider).lower()
        self.provider_label = "Qobuz" if self.provider == "qobuz" else "Spotify"
        self.source_evaluation = dict(source_evaluation or {})
        self.network_test = dict(network_test) if network_test else None
        self.recording_frame_offset = max(0, int(recording_frame_offset))
        self.started_at = time.time()
        self._started_monotonic = time.monotonic()
        self._lock = threading.Lock()
        self._callback_frames = 0
        self._callback_status_count = 0
        self._callback_status_examples = []
        self._last_adc_time = None
        self._last_adc_frames = None
        self._adc_timeline_gaps = []
        self._last_audio_probe = None
        self._last_audio_frame = None
        self._current_zero_frames = 0
        self._current_zero_start_frame = None
        self._digital_zero_ranges = []
        self._repeated_audio_blocks = 0
        self._boundary_discontinuities = 0
        self._max_boundary_jump = 0.0
        self._last_playback_observation = None
        self._source_playing = None
        self._playback_stall_durations = []
        self._timeline_slips = []
        self._events = []

    def _add_event_locked(
        self,
        event_type,
        sample,
        detail,
        duration_sec=0.0,
        value_sec=None,
    ):
        event = {
            "type": event_type,
            "sample": self.recording_frame_offset + max(0, int(sample)),
            "duration_sec": max(0.0, float(duration_sec)),
            "detail": str(detail),
        }
        if value_sec is not None:
            event["value_sec"] = float(value_sec)
        self._events.append(event)

    def record_audio_callback(self, frames, status=None, samples=None, adc_time=None):
        with self._lock:
            self._callback_frames += int(frames)
            block_start_frame = self._callback_frames - int(frames)
            try:
                adc_value = float(adc_time) if adc_time is not None else None
            except (TypeError, ValueError):
                adc_value = None
            if adc_value and self._last_adc_time is not None and self._last_adc_frames:
                expected_delta = self._last_adc_frames / self.sample_rate
                timeline_gap = adc_value - self._last_adc_time - expected_delta
                if abs(timeline_gap) > max(0.0005, 2.0 / self.sample_rate):
                    self._adc_timeline_gaps.append(timeline_gap)
                    self._add_event_locked(
                        "adc_timeline_gap",
                        block_start_frame,
                        f"ADCタイムラインのgap/overlap疑い: {timeline_gap:+.6f}秒",
                        value_sec=timeline_gap,
                    )
            if adc_value:
                self._last_adc_time = adc_value
                self._last_adc_frames = int(frames)
            if status:
                self._callback_status_count += 1
                text = str(status)
                if text not in self._callback_status_examples and len(self._callback_status_examples) < 5:
                    self._callback_status_examples.append(text)
                self._add_event_locked(
                    "audio_callback",
                    block_start_frame,
                    f"音声コールバック異常: {text}",
                )
            if samples is None:
                return
            block = np.asarray(samples)
            if block.ndim != 2 or len(block) == 0:
                return
            if self._source_playing is False:
                self._current_zero_frames = 0
                self._last_audio_probe = None
                self._last_audio_frame = None
                return

            block_peak = float(np.max(np.abs(block)))
            if block_peak <= 1e-12:
                if self._current_zero_start_frame is None:
                    self._current_zero_start_frame = block_start_frame
                self._current_zero_frames += len(block)
            elif self._current_zero_frames:
                self._digital_zero_ranges.append(
                    (
                        self._current_zero_start_frame,
                        self._current_zero_start_frame + self._current_zero_frames,
                    )
                )
                self._current_zero_frames = 0
                self._current_zero_start_frame = None

            stride = max(1, len(block) // 64)
            probe = np.ascontiguousarray(block[::stride][:64])
            if (
                block_peak > 1e-6
                and self._last_audio_probe is not None
                and probe.shape == self._last_audio_probe.shape
                and np.array_equal(probe, self._last_audio_probe)
            ):
                self._repeated_audio_blocks += 1
                self._add_event_locked(
                    "repeated_audio_block",
                    block_start_frame,
                    "同一音声ブロックの反復疑い",
                    len(block) / self.sample_rate,
                )
            self._last_audio_probe = probe.copy()

            first_frame = np.asarray(block[0], dtype=np.float64)
            if self._last_audio_frame is not None:
                boundary_jump = float(np.max(np.abs(first_frame - self._last_audio_frame)))
                local = np.asarray(block[: min(len(block), 128)], dtype=np.float64)
                local_steps = np.abs(np.diff(local, axis=0))
                typical_step = float(np.median(local_steps)) if local_steps.size else 0.0
                if boundary_jump >= 0.75 and boundary_jump > max(0.1, typical_step * 20.0):
                    self._boundary_discontinuities += 1
                    self._max_boundary_jump = max(self._max_boundary_jump, boundary_jump)
                    self._add_event_locked(
                        "boundary_discontinuity",
                        block_start_frame,
                        f"コールバック境界の不連続疑い: jump {boundary_jump:.3f}",
                    )
            self._last_audio_frame = np.asarray(block[-1], dtype=np.float64).copy()

    def observe_playback(
        self,
        track_key,
        state,
        position,
        observed_monotonic=None,
        duration=None,
    ):
        now = time.monotonic() if observed_monotonic is None else float(observed_monotonic)
        try:
            position = float(position)
        except (TypeError, ValueError):
            position = None
        with self._lock:
            if state != "playing" or position is None or track_key is None:
                self._source_playing = False
                self._last_playback_observation = None
                self._current_zero_frames = 0
                self._current_zero_start_frame = None
                self._last_audio_probe = None
                self._last_audio_frame = None
                return
            self._source_playing = True
            current = {
                "track_key": track_key,
                "position": position,
                "callback_frames": self._callback_frames,
                "observed_monotonic": now,
            }
            previous = self._last_playback_observation
            self._last_playback_observation = current
            if not previous or previous["track_key"] != track_key:
                return

            capture_delta = (
                current["callback_frames"] - previous["callback_frames"]
            ) / self.sample_rate
            position_delta = current["position"] - previous["position"]
            if capture_delta < 0.5:
                return
            if position_delta < max(0.10, capture_delta * 0.20):
                stall_duration = max(0.0, capture_delta - position_delta)
                self._playback_stall_durations.append(stall_duration)
                self._add_event_locked(
                    "playback_stall",
                    current["callback_frames"],
                    f"{self.provider_label}再生位置の停止疑い: {stall_duration:.2f}秒",
                    stall_duration,
                )
            timeline_slip = capture_delta - position_delta
            try:
                duration_value = max(0.0, float(duration or 0.0))
            except (TypeError, ValueError):
                duration_value = 0.0
            slip_threshold = max(0.25, duration_value * 0.001)
            if abs(timeline_slip) >= slip_threshold:
                self._timeline_slips.append(timeline_slip)
                self._add_event_locked(
                    "timeline_slip",
                    current["callback_frames"],
                    f"録音と{self.provider_label}タイムラインの滑り疑い: {timeline_slip:+.2f}秒",
                    value_sec=timeline_slip,
                )

    def observe_spotify_playback(self, track_key, state, position, observed_monotonic=None):
        self.observe_playback(track_key, state, position, observed_monotonic)

    def add_external_event(self, event_type, detail, sample=None, duration_sec=0.0):
        with self._lock:
            target = self._callback_frames if sample is None else int(sample)
            self._add_event_locked(event_type, target, detail, duration_sec)

    def finish(self, network_summary=None, ended_at=None, ended_monotonic=None):
        ended_at = time.time() if ended_at is None else float(ended_at)
        stopped_monotonic = time.monotonic() if ended_monotonic is None else float(ended_monotonic)
        elapsed = max(0.0, stopped_monotonic - self._started_monotonic)
        with self._lock:
            callback_frames = self._callback_frames
            callback_status_count = self._callback_status_count
            callback_status_examples = list(self._callback_status_examples)
            adc_timeline_gaps = list(self._adc_timeline_gaps)
            zero_ranges = list(self._digital_zero_ranges)
            if self._current_zero_frames and self._current_zero_start_frame is not None:
                zero_ranges.append(
                    (
                        self._current_zero_start_frame,
                        self._current_zero_start_frame + self._current_zero_frames,
                    )
                )
            repeated_audio_blocks = self._repeated_audio_blocks
            boundary_discontinuities = self._boundary_discontinuities
            max_boundary_jump = self._max_boundary_jump
            playback_stall_durations = list(self._playback_stall_durations)
            timeline_slips = list(self._timeline_slips)
            events = list(self._events)
        captured_sec = callback_frames / self.sample_rate if self.sample_rate else 0.0
        deficit_sec = max(0.0, elapsed - captured_sec)
        deficit_limit = max(0.75, elapsed * 0.02)
        settings_evaluation = (
            dict(self.source_evaluation)
            if self.provider == "qobuz"
            else evaluate_spotify_quality_settings(self.spotify_settings)
        )
        settings_evaluation.setdefault("warnings", [])
        settings_evaluation.setdefault("notes", [])
        settings_evaluation.setdefault("conditions_pass", False)
        warnings = list(settings_evaluation["warnings"])
        notes = list(settings_evaluation["notes"])

        expected_rate = settings_evaluation.get("source_sample_rate")
        if expected_rate and self.sample_rate != int(expected_rate):
            warnings.append(
                f"入力サンプルレートがソースと一致しません: "
                f"入力{self.sample_rate}Hz / ソース{int(expected_rate)}Hz"
            )
        elif self.provider == "spotify" and self.sample_rate != 44100:
            warnings.append(f"入力サンプルレートが44.1kHzではありません: {self.sample_rate}Hz")
        if callback_status_count:
            warnings.append(f"音声コールバック異常を{callback_status_count}回検出しました")
        if adc_timeline_gaps:
            warnings.append(
                f"ADCタイムラインのgap/overlapを{len(adc_timeline_gaps)}回検出しました"
            )
        if deficit_sec > deficit_limit:
            warnings.append(f"録音時間に対して約{deficit_sec:.2f}秒のフレーム不足があります")

        zero_run_frames = [end - start for start, end in zero_ranges]
        longest_zero_sec = max(zero_run_frames, default=0) / self.sample_rate
        suspicious_zero_runs = sum(
            1 for frames in zero_run_frames if frames / self.sample_rate >= 0.50
        )
        for start, end in zero_ranges:
            duration_sec = (end - start) / self.sample_rate
            if duration_sec >= 0.50:
                events.append(
                    {
                        "type": "digital_silence",
                        "sample": self.recording_frame_offset + start,
                        "duration_sec": duration_sec,
                        "detail": f"デジタル無音の継続疑い: {duration_sec:.2f}秒",
                    }
                )
        if suspicious_zero_runs:
            warnings.append(
                f"0.50秒以上のデジタル無音を{suspicious_zero_runs}区間検出しました"
            )
        if repeated_audio_blocks:
            warnings.append(f"同一音声ブロックの反復を{repeated_audio_blocks}回検出しました")
        if boundary_discontinuities:
            warnings.append(
                f"コールバック境界の不連続を{boundary_discontinuities}回検出しました"
            )
        playback_stall_sec = sum(playback_stall_durations)
        if playback_stall_sec >= 0.75:
            warnings.append(
                f"{self.provider_label}再生位置の停止疑いを{len(playback_stall_durations)}回 / "
                f"約{playback_stall_sec:.2f}秒検出しました"
            )
        if timeline_slips:
            warnings.append(
                f"録音と{self.provider_label}タイムラインの滑り疑いを{len(timeline_slips)}回検出しました"
            )
        source_problem_events = [
            event
            for event in events
            if event.get("type") in {"source_buffering", "source_error", "sample_rate_change", "spool_overflow"}
        ]
        if source_problem_events:
            warnings.append(
                f"{self.provider_label}再生/録音経路の重大イベントを{len(source_problem_events)}件検出しました"
            )

        if not self.network_test:
            notes.append("録音前の回線実測は未実行です")
        elif not network_test_is_fresh(self.network_test, ended_at):
            notes.append("回線実測は30分より古いため参考値です")
        elif not self.network_test.get("pass"):
            warnings.extend(self.network_test.get("warnings", []))

        network = dict(network_summary or {})
        if network.get("error"):
            notes.append(f"{self.provider_label}通信監視エラー: {network['error']}")
        notes.extend(network.get("notes", []))

        quality_gate_pass = not warnings
        for event in events:
            event["time_sec"] = event["sample"] / self.sample_rate
        events.sort(key=lambda item: item["sample"])
        return {
            "started_at": self.started_at,
            "ended_at": ended_at,
            "sample_rate": self.sample_rate,
            "device_name": self.device_name,
            "provider": self.provider,
            "spotify_settings": self.spotify_settings,
            "source_evaluation": settings_evaluation,
            "settings_evaluation": settings_evaluation,
            "network_test": self.network_test,
            "network_observation": network,
            "callback_frames": callback_frames,
            "callback_status_count": callback_status_count,
            "callback_status_examples": callback_status_examples,
            "adc_timeline_gap_count": len(adc_timeline_gaps),
            "max_adc_timeline_gap_sec": max(
                (abs(value) for value in adc_timeline_gaps), default=0.0
            ),
            "capture_elapsed_sec": elapsed,
            "captured_callback_sec": captured_sec,
            "frame_deficit_sec": deficit_sec,
            "digital_zero_run_count": suspicious_zero_runs,
            "longest_digital_zero_sec": longest_zero_sec,
            "repeated_audio_blocks": repeated_audio_blocks,
            "boundary_discontinuities": boundary_discontinuities,
            "max_boundary_jump": max_boundary_jump,
            "playback_stall_count": len(playback_stall_durations),
            "playback_stall_sec": playback_stall_sec,
            "timeline_slip_count": len(timeline_slips),
            "max_timeline_slip_sec": max((abs(value) for value in timeline_slips), default=0.0),
            "events": events,
            "quality_gate_pass": quality_gate_pass,
            "lossless_verified": False,
            "assurance_label": (
                settings_evaluation.get("assurance_label")
                if quality_gate_pass and settings_evaluation.get("assurance_label")
                else (
                    f"{self.provider_label}品質条件適合・bit一致未証明"
                    if quality_gate_pass
                    else f"品質条件に要確認・{self.provider_label} bit一致未証明"
                )
            ),
            "warnings": warnings,
            "notes": list(dict.fromkeys(notes)),
        }


def format_capture_audit(audit):
    if not audit:
        return "ソース監査なし"
    network = audit.get("network_observation") or {}
    provider_label = "Qobuz" if audit.get("provider") == "qobuz" else "Spotify"
    network_text = f"{provider_label}通信未観測"
    if network.get("sample_count"):
        total_mb = network.get("inbound_total_bytes", 0) / 1_000_000.0
        network_text = (
            f"{provider_label}受信 {total_mb:.1f} MB / 平均 {network.get('inbound_average_kbps', 0):.0f} kbps"
        )
    return (
        f"{audit.get('assurance_label', '監査不明')} / "
        f"音声異常 {audit.get('callback_status_count', 0) + audit.get('adc_timeline_gap_count', 0)} / "
        f"停止疑い {audit.get('playback_stall_count', 0)} / "
        f"滑り疑い {audit.get('timeline_slip_count', 0)} / {network_text}"
    )


def format_timecode(seconds):
    total = max(0.0, float(seconds))
    hours = int(total // 3600)
    minutes = int((total % 3600) // 60)
    remaining = total % 60
    if hours:
        return f"{hours:02d}:{minutes:02d}:{remaining:05.2f}"
    return f"{minutes:02d}:{remaining:05.2f}"


def format_audit_events(audit, start_sample=None, end_sample=None, limit=12):
    if not audit:
        return []
    sample_rate = int(audit.get("sample_rate") or 0)
    if sample_rate <= 0:
        return []
    start = 0 if start_sample is None else int(start_sample)
    end = None if end_sample is None else int(end_sample)
    lines = []
    for event in audit.get("events", []):
        sample = int(event.get("sample", 0))
        if sample < start or (end is not None and sample >= end):
            continue
        recording_time = sample / sample_rate
        local_time = (sample - start) / sample_rate
        lines.append(
            f"録音 {format_timecode(recording_time)} / 曲内 {format_timecode(local_time)}: "
            f"{event.get('detail', event.get('type', '異常疑い'))}"
        )
        if len(lines) >= int(limit):
            remaining = sum(
                1
                for item in audit.get("events", [])
                if int(item.get("sample", 0)) >= sample
                and (end is None or int(item.get("sample", 0)) < end)
            ) - 1
            if remaining > 0:
                lines.append(f"ほか {remaining} 件")
            break
    return lines


def audit_for_audio_range(audit, start_sample, end_sample):
    if not audit:
        return None
    start = int(start_sample)
    end = int(end_sample)
    scoped = dict(audit)
    if "events" not in audit:
        return scoped
    scoped_events = []
    for source in audit.get("events", []):
        sample = int(source.get("sample", 0))
        if sample < start or sample >= end:
            continue
        event = dict(source)
        event["sample"] = sample - start
        event["time_sec"] = event["sample"] / int(audit["sample_rate"])
        scoped_events.append(event)
    scoped["events"] = scoped_events
    scoped["playback_stall_count"] = sum(
        1 for event in scoped_events if event.get("type") == "playback_stall"
    )
    scoped["playback_stall_sec"] = sum(
        event.get("duration_sec", 0.0)
        for event in scoped_events
        if event.get("type") == "playback_stall"
    )
    scoped["timeline_slip_count"] = sum(
        1 for event in scoped_events if event.get("type") == "timeline_slip"
    )
    scoped["max_timeline_slip_sec"] = max(
        (
            abs(float(event.get("value_sec", 0.0)))
            for event in scoped_events
            if event.get("type") == "timeline_slip"
        ),
        default=0.0,
    )
    scoped["digital_zero_run_count"] = sum(
        1 for event in scoped_events if event.get("type") == "digital_silence"
    )
    scoped["longest_digital_zero_sec"] = max(
        (
            event.get("duration_sec", 0.0)
            for event in scoped_events
            if event.get("type") == "digital_silence"
        ),
        default=0.0,
    )
    scoped["repeated_audio_blocks"] = sum(
        1 for event in scoped_events if event.get("type") == "repeated_audio_block"
    )
    scoped["boundary_discontinuities"] = sum(
        1 for event in scoped_events if event.get("type") == "boundary_discontinuity"
    )
    scoped["callback_status_count"] = sum(
        1 for event in scoped_events if event.get("type") == "audio_callback"
    )
    scoped["adc_timeline_gap_count"] = sum(
        1 for event in scoped_events if event.get("type") == "adc_timeline_gap"
    )
    return scoped
