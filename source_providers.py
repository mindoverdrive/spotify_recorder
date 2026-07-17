import time
from dataclasses import dataclass

from qobuz_integration import (
    QobuzLogTailer,
    diagnose_qobuz_integration,
    evaluate_qobuz_capture_gate,
    get_qobuz_snapshot,
)
from spotify_quality_audit import (
    evaluate_spotify_quality_settings,
    format_spotify_quality_settings,
    read_spotify_quality_settings,
)
from spotify_recorder_services import (
    build_diagnostic_lines,
    get_spotify_info_extended,
)


PROVIDER_SPOTIFY = "Spotify"
PROVIDER_QOBUZ = "Qobuz"
PROVIDERS = [PROVIDER_SPOTIFY, PROVIDER_QOBUZ]
QOBUZ_OFFLINE = "Offline"


@dataclass
class ProviderStatus:
    provider: str
    label: str
    conditions_pass: bool
    settings: dict
    warnings: list


class SpotifySourceAdapter:
    name = PROVIDER_SPOTIFY

    def snapshot(self):
        result = get_spotify_info_extended()
        result["provider"] = "spotify"
        result["source_mode"] = "offline"
        result.setdefault("source_verified", False)
        return result

    def quality_status(self):
        settings = read_spotify_quality_settings()
        evaluation = evaluate_spotify_quality_settings(settings)
        return ProviderStatus(
            provider="spotify",
            label=evaluation["label"],
            conditions_pass=evaluation["conditions_pass"],
            settings=settings,
            warnings=list(evaluation["warnings"]),
        )

    def diagnostics(self, sample_rate, **_kwargs):
        return build_diagnostic_lines(sample_rate)

    def format_quality(self, status):
        return format_spotify_quality_settings(status.settings)

    def preflight(self, _device, **_kwargs):
        settings = read_spotify_quality_settings()
        evaluation = evaluate_spotify_quality_settings(settings)
        return {
            "conditions_pass": evaluation["conditions_pass"],
            "warnings": list(evaluation["warnings"]),
            "notes": list(evaluation["notes"]),
            "source_verified": False,
            "source_sample_rate": 44100,
            "source_bit_depth": None,
            "source_channels": 2,
            "assurance_label": evaluation["label"],
            "mode": "offline",
            "evidence": settings,
        }

    def poll_events(self):
        return []


class QobuzSourceAdapter:
    name = PROVIDER_QOBUZ

    def __init__(self, qobuz_dir=None):
        self.qobuz_dir = qobuz_dir
        self.log_tailer = QobuzLogTailer(qobuz_dir) if qobuz_dir else QobuzLogTailer()
        self.last_snapshot = None
        self._buffer_started = None
        self._buffer_started_epoch = None

    def snapshot(self):
        self.last_snapshot = get_qobuz_snapshot(self.qobuz_dir)
        self.last_snapshot["source_mode"] = QOBUZ_OFFLINE.lower()
        return self.last_snapshot

    def quality_status(self):
        diagnostic = diagnose_qobuz_integration(self.qobuz_dir)
        snapshot = self.last_snapshot or self.snapshot()
        if diagnostic["available"] and snapshot.get("source_verified"):
            label = (
                f"{snapshot.get('format_label', 'Qobuz')} / "
                f"{snapshot.get('source_bit_depth', '?')}-bit / "
                f"{snapshot.get('source_sample_rate', '?')}Hz / bit一致未証明"
            )
            passed = True
            warnings = []
        else:
            label = "Qobuzローカル証跡を取得できません"
            passed = False
            warnings = list(diagnostic["warnings"])
            if snapshot.get("source_error"):
                warnings.append(snapshot["source_error"])
        settings = {
            "diagnostic": diagnostic,
            "snapshot": snapshot,
            "available": diagnostic["available"],
        }
        return ProviderStatus("qobuz", label, passed, settings, list(dict.fromkeys(warnings)))

    def diagnostics(self, sample_rate, device=None):
        diagnostic = diagnose_qobuz_integration(self.qobuz_dir)
        lines = [
            "OK: Capture Gain = Unity Gain 1.0 / DSPなし",
            "OK: Output = WAV 32-bit IEEE float",
        ]
        if diagnostic["available"]:
            lines.append(
                f"OK: Qobuzローカル証跡 / app {diagnostic.get('app_version') or 'unknown'}"
            )
        else:
            lines.extend(f"警告: {warning}" for warning in diagnostic["warnings"])
        if device:
            gate = self.preflight(device)
            lines.extend(
                ("OK: " if gate["conditions_pass"] else "拒否: ") + gate["assurance_label"]
                for _ in [0]
            )
            lines.extend(f"拒否: {warning}" for warning in gate["warnings"])
        if int(sample_rate) > 192000:
            lines.append(f"警告: Qobuz上限192kHzを超える入力レートです: {sample_rate}Hz")
        lines.append("情報: 音声波形のみからLossless/Hi-Res採用を断定しません")
        return lines

    def format_quality(self, status):
        return status.label

    def preflight(self, device):
        diagnostic = diagnose_qobuz_integration(self.qobuz_dir)
        snapshot = dict(self.last_snapshot or self.snapshot())
        result = evaluate_qobuz_capture_gate(snapshot, device)
        if not diagnostic["available"]:
            result["warnings"] = list(
                dict.fromkeys(result["warnings"] + diagnostic["warnings"])
            )
            result["conditions_pass"] = False
            result["assurance_label"] = "要確認: Qobuz Offline証跡を検証できません"
        result["evidence"]["app_version"] = diagnostic.get("app_version")
        result["evidence"]["app_running"] = diagnostic.get("app_running")
        return result

    def poll_events(self):
        normalized = []
        for event in self.log_tailer.poll():
            message = event.get("message", "")
            if event.get("type") == "buffer" and "init buffer" in message.lower():
                self._buffer_started = time.monotonic()
                self._buffer_started_epoch = event.get("timestamp_epoch")
                continue
            if (
                event.get("type") == "buffer"
                and "entirely buffered" in message.lower()
                and self._buffer_started is not None
            ):
                ended_epoch = event.get("timestamp_epoch")
                if self._buffer_started_epoch is not None and ended_epoch is not None:
                    duration = max(0.0, ended_epoch - self._buffer_started_epoch)
                else:
                    duration = max(0.0, time.monotonic() - self._buffer_started)
                normalized.append(
                    {
                        "type": "source_buffering",
                        "duration_sec": duration,
                        "message": f"Qobuzバッファリング疑い: {duration:.2f}秒",
                        "timestamp_epoch": self._buffer_started_epoch,
                    }
                )
                self._buffer_started = None
                self._buffer_started_epoch = None
            elif event.get("type") == "error":
                normalized.append(
                    {
                        "type": "source_error",
                        "duration_sec": 0.0,
                        "message": f"Qobuz再生エラー: {message}",
                        "timestamp_epoch": event.get("timestamp_epoch"),
                    }
                )
            else:
                normalized.append(event)
        return normalized


def create_provider_adapters(qobuz_dir=None):
    return {
        PROVIDER_SPOTIFY: SpotifySourceAdapter(),
        PROVIDER_QOBUZ: QobuzSourceAdapter(qobuz_dir),
    }
