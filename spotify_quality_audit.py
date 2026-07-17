import glob
import json
import os
import subprocess
import threading
import time

import numpy as np


SPOTIFY_LOSSLESS_ENUM_CANDIDATE = 5

SPOTIFY_OFFLINE_MODE_SCRIPT = r'''
tell application "System Events"
    if not (exists process "Spotify") then return "CLOSED"
    tell process "Spotify"
        repeat with barItem in menu bar items of menu bar 1
            try
                repeat with candidate in menu items of menu 1 of barItem
                    set itemName to name of candidate as text
                    if itemName is "Offline Mode" or itemName is "オフラインモード" or itemName is "オフライン モード" then
                        set markValue to ""
                        try
                            set markValue to value of attribute "AXMenuItemMarkChar" of candidate
                        end try
                        if markValue is missing value or markValue is "" then return "OFF"
                        return "ON"
                    end if
                end repeat
            end try
        end repeat
    end tell
end tell
return "UNAVAILABLE"
'''


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


def read_spotify_offline_mode():
    try:
        result = subprocess.run(
            ["/usr/bin/osascript", "-e", SPOTIFY_OFFLINE_MODE_SCRIPT],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        return {
            "available": False,
            "enabled": False,
            "error": f"Spotify Offline Mode確認に失敗しました: {exc}",
        }
    if result.returncode != 0:
        detail = result.stderr.strip() or f"osascript終了コード {result.returncode}"
        return {
            "available": False,
            "enabled": False,
            "error": (
                "Spotify Offline Modeを確認できません。macOSのアクセシビリティで"
                f"Hi-Res Recorderを許可してください: {detail}"
            ),
        }

    state = result.stdout.strip().upper()
    if state == "ON":
        return {"available": True, "enabled": True, "evidence": "Spotify menu mark"}
    if state == "OFF":
        return {"available": True, "enabled": False, "evidence": "Spotify menu mark"}
    error = (
        "Spotifyが起動していません"
        if state == "CLOSED"
        else "SpotifyのOffline Modeメニューを確認できません"
    )
    return {"available": False, "enabled": False, "error": error}


def read_spotify_quality_settings(spotify_dir=None, offline_mode=None):
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

    offline = read_spotify_offline_mode() if offline_mode is None else dict(offline_mode)
    return {
        "available": True,
        "download_quality_raw": prefs.get("audio.sync_bitrate_enumeration"),
        "normalize": prefs.get("audio.normalize_v2"),
        "automix": prefs.get("audio.automix"),
        "offline_mode": offline,
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
            "offline_mode_enabled": False,
            "warnings": warnings,
            "notes": notes,
            "label": "設定未確認・実効Lossless未証明",
        }

    quality = settings.get("download_quality_raw")
    lossless_candidate = quality == SPOTIFY_LOSSLESS_ENUM_CANDIDATE
    offline_mode = dict(settings.get("offline_mode") or {})
    offline_mode_enabled = bool(
        offline_mode.get("available") and offline_mode.get("enabled")
    )
    if not lossless_candidate:
        warnings.append(
            f"Spotifyダウンロード音質の観測値がLossless候補(5)ではありません: {quality!r}"
        )
    if not offline_mode.get("available"):
        warnings.append(offline_mode.get("error", "Spotify Offline Modeを確認できません"))
    elif not offline_mode.get("enabled"):
        warnings.append("SpotifyのFile > Offline ModeがOFFです")
    if settings.get("normalize") is not False:
        warnings.append("Spotifyの音量の均一OFFを確認できません")
    if settings.get("automix") is not False:
        warnings.append("SpotifyのAutomix OFFを確認できません")

    conditions_pass = not warnings
    label = (
        "Offline/Lossless設定条件適合・実効品質は未証明"
        if conditions_pass
        else "Offline/Lossless設定条件に要確認・実効品質は未証明"
    )
    notes.append("音質値5の意味はSpotifyの非公開実装に基づく候補判定です")
    return {
        "conditions_pass": conditions_pass,
        "lossless_candidate": lossless_candidate,
        "offline_mode_enabled": offline_mode_enabled,
        "warnings": warnings,
        "notes": notes,
        "label": label,
    }


def format_spotify_quality_settings(settings):
    evaluation = evaluate_spotify_quality_settings(settings)
    if not settings.get("available"):
        return evaluation["label"]
    return (
        f"{evaluation['label']} / Offline "
        f"{'ON' if evaluation['offline_mode_enabled'] else '未確認'} / "
        f"DL音質値 {settings.get('download_quality_raw')} / "
        f"音量均一 {'OFF' if settings.get('normalize') is False else '未確認'}"
    )


class CaptureQualityAudit:
    def __init__(
        self,
        sample_rate,
        device_name,
        spotify_settings,
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

    def finish(self, ended_at=None, ended_monotonic=None):
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
            if self.source_evaluation
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
    return (
        f"{audit.get('assurance_label', '監査不明')} / "
        f"音声異常 {audit.get('callback_status_count', 0) + audit.get('adc_timeline_gap_count', 0)} / "
        f"停止疑い {audit.get('playback_stall_count', 0)} / "
        f"滑り疑い {audit.get('timeline_slip_count', 0)}"
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
