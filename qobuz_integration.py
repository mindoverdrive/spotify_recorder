import glob
import json
import os
import plistlib
import re
import sqlite3
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone


QOBUZ_FORMATS = {
    5: {"label": "MP3", "lossless": False},
    6: {"label": "CD Lossless", "lossless": True, "max_rate": 44100, "bit_depth": 16},
    7: {"label": "Hi-Res 96", "lossless": True, "max_rate": 96000, "bit_depth": 24},
    27: {"label": "Hi-Res 192", "lossless": True, "max_rate": 192000, "bit_depth": 24},
}
QOBUZ_SUPPORTED_SAMPLE_RATES = {44100, 48000, 88200, 96000, 176400, 192000}
SUPPORTED_SCHEMA_COLUMNS = {
    "track_id",
    "data",
    "status",
    "format",
    "sampling_rate",
    "bit_depth",
    "duration",
    "is_completed",
}


def default_qobuz_dir():
    return os.path.expanduser("~/Library/Application Support/Qobuz")


def qobuz_app_version(app_path=None):
    if app_path is None:
        candidates = [
            "/Applications/Qobuz.app",
            os.path.expanduser("~/Applications/Qobuz.app"),
        ]
        app_path = next((path for path in candidates if os.path.isdir(path)), candidates[0])
    plist_path = os.path.join(app_path, "Contents", "Info.plist")
    try:
        with open(plist_path, "rb") as handle:
            payload = plistlib.load(handle)
    except OSError:
        return None
    return payload.get("CFBundleShortVersionString") or payload.get("CFBundleVersion")


def qobuz_is_running():
    try:
        result = subprocess.run(
            ["pgrep", "-x", "Qobuz"],
            capture_output=True,
            timeout=1.0,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _walk(value):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _first_key(payload, keys, expected_type=None):
    for node in _walk(payload):
        if not isinstance(node, dict):
            continue
        for key in keys:
            if key not in node:
                continue
            value = node[key]
            if expected_type is None or isinstance(value, expected_type):
                return value
    return None


def _as_int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _as_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _track_candidate(payload):
    candidates = []
    for node in _walk(payload):
        if not isinstance(node, dict):
            continue
        keys = {str(key).lower() for key in node}
        if keys.intersection({"track_id", "trackid"}) or (
            "title" in keys and keys.intersection({"album", "album_id", "albumid"})
        ):
            candidates.append(node)
    if not candidates:
        return {}
    return max(candidates, key=lambda item: len(item))


def parse_qobuz_player_state(payload):
    track = _track_candidate(payload)
    track_id = (
        track.get("track_id")
        or track.get("trackId")
        or track.get("id")
        or _first_key(payload, ("currentTrackId", "trackId"))
    )
    title = track.get("title") or track.get("name") or "Unknown"
    artist = track.get("artist") or track.get("performer") or "Unknown"
    if isinstance(artist, dict):
        artist = artist.get("name") or artist.get("display_name") or "Unknown"
    album = track.get("album") or "Unknown"
    if isinstance(album, dict):
        album = album.get("title") or album.get("name") or "Unknown"

    state = _first_key(payload, ("playerState", "playbackState", "state", "status"))
    if isinstance(state, dict):
        state = state.get("name") or state.get("state")
    state_text = str(state or "unknown").lower()
    if state_text in {"2", "playing", "play"}:
        state_text = "playing"
    elif state_text in {"1", "paused", "pause"}:
        state_text = "paused"
    elif state_text in {"0", "stopped", "stop", "idle"}:
        state_text = "stopped"

    volume = _as_float(_first_key(payload, ("volume", "playerVolume")))
    if volume is not None and 0.0 <= volume <= 1.0:
        volume *= 100.0
    muted = _first_key(payload, ("muted", "isMuted"), (bool, int))
    exclusive = _first_key(payload, ("exclusiveMode", "isExclusiveMode"), (bool, int))
    return {
        "status": "OK" if track_id is not None else "IDLE",
        "track_id": None if track_id is None else str(track_id),
        "name": str(title),
        "artist": str(artist),
        "album": str(album),
        "state": state_text,
        "position": _as_float(
            _first_key(payload, ("position", "currentPosition", "positionSec"))
        )
        or 0.0,
        "duration": _as_float(track.get("duration")) or 0.0,
        "volume_percent": volume,
        "muted": None if muted is None else bool(muted),
        "exclusive_mode": None if exclusive is None else bool(exclusive),
        "output_device_name": _first_key(
            payload, ("deviceName", "outputDeviceName", "currentDeviceName")
        ),
    }


def read_qobuz_player_state(qobuz_dir=None):
    base = qobuz_dir or default_qobuz_dir()
    paths = glob.glob(os.path.join(base, "player-*.json"))
    if not paths:
        return {"status": "UNAVAILABLE", "error": "Qobuz player状態が見つかりません"}
    path = max(paths, key=os.path.getmtime)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        return {"status": "UNAVAILABLE", "error": str(exc), "player_path": path}
    result = parse_qobuz_player_state(payload)
    result["player_path"] = path
    return result


def _table_columns(connection, table):
    return {
        row[1]
        for row in connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    }


def read_qobuz_track_record(track_id, database_path=None):
    path = database_path or os.path.join(default_qobuz_dir(), "qobuz.db")
    if not track_id or not os.path.isfile(path):
        return {"available": False, "error": "QobuzトラックDBを利用できません"}
    uri = f"file:{os.path.abspath(path)}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True, timeout=1.0) as connection:
            connection.row_factory = sqlite3.Row
            columns = _table_columns(connection, "L_Track")
            missing = SUPPORTED_SCHEMA_COLUMNS - columns
            if missing:
                return {
                    "available": False,
                    "error": f"Qobuz DBスキーマ非対応: {', '.join(sorted(missing))}",
                }
            row = connection.execute(
                """
                SELECT track_id, data, status, format, sampling_rate, bit_depth,
                       duration, is_completed
                FROM L_Track WHERE CAST(track_id AS TEXT) = ? LIMIT 1
                """,
                (str(track_id),),
            ).fetchone()
    except sqlite3.Error as exc:
        return {"available": False, "error": str(exc)}
    if row is None:
        return {"available": False, "error": "再生曲がQobuz DBに見つかりません"}
    result = dict(row)
    try:
        data = json.loads(result.get("data") or "{}")
    except json.JSONDecodeError:
        data = {}
    format_id = _as_int(result.get("format") or data.get("format_id"))
    result.update(
        {
            "available": True,
            "track_id": str(result["track_id"]),
            "format_id": format_id,
            "format_label": QOBUZ_FORMATS.get(format_id, {}).get(
                "label", f"format {format_id}"
            ),
            "source_sample_rate": _as_int(
                result.get("sampling_rate") or data.get("sampling_rate")
            ),
            "source_bit_depth": _as_int(
                result.get("bit_depth") or data.get("bit_depth")
            ),
            "source_channels": _as_int(data.get("channels")),
            "duration": _as_float(result.get("duration") or data.get("duration")) or 0.0,
            "is_completed": bool(result.get("is_completed")),
            "raw_data": data,
        }
    )
    return result


@dataclass
class QobuzLogTailer:
    qobuz_dir: str = field(default_factory=default_qobuz_dir)
    offsets: dict = field(default_factory=dict)
    initialized: bool = False

    _event_pattern = re.compile(
        r"(?P<timestamp>\d{4}-\d\d-\d\d[T ][^ ]+).*?"
        r"(?P<message>Start loading track (?P<load>\d+)|Play track (?P<play>\d+)|"
        r"Pause track (?P<pause>\d+)|Status has changed to (?P<state>\w+)|"
        r"Init buffer|entirely buffered|(?:network|playback).*error)",
        re.IGNORECASE,
    )

    def poll(self):
        events = []
        paths = sorted(
            glob.glob(os.path.join(self.qobuz_dir, "logs", "rapport_qobuz*.txt")),
            key=os.path.getmtime,
        )[-4:]
        for path in paths:
            try:
                size = os.path.getsize(path)
                if path not in self.offsets:
                    self.offsets[path] = size if not self.initialized else 0
                    if not self.initialized:
                        continue
                offset = self.offsets[path]
                if size < offset:
                    offset = 0
                with open(path, "r", encoding="utf-8", errors="replace") as handle:
                    handle.seek(offset)
                    lines = handle.readlines()
                    self.offsets[path] = handle.tell()
            except OSError:
                continue
            for line in lines:
                match = self._event_pattern.search(line)
                if not match:
                    continue
                message = match.group("message")
                lowered = message.lower()
                if match.group("play"):
                    event_type = "play"
                    track_id = match.group("play")
                elif match.group("pause"):
                    event_type = "pause"
                    track_id = match.group("pause")
                elif "buffer" in lowered:
                    event_type = "buffer"
                    track_id = None
                elif "error" in lowered:
                    event_type = "error"
                    track_id = None
                elif match.group("state"):
                    event_type = "state"
                    track_id = None
                else:
                    event_type = "load"
                    track_id = match.group("load")
                events.append(
                    {
                        "type": event_type,
                        "track_id": track_id,
                        "message": message,
                        "timestamp": match.group("timestamp"),
                        "timestamp_epoch": _timestamp_epoch(match.group("timestamp")),
                    }
                )
        self.initialized = True
        return events


def _timestamp_epoch(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def diagnose_qobuz_integration(qobuz_dir=None, app_path=None):
    base = qobuz_dir or default_qobuz_dir()
    version = qobuz_app_version(app_path)
    running = qobuz_is_running()
    player = read_qobuz_player_state(base)
    db_path = os.path.join(base, "qobuz.db")
    warnings = []
    if version is None:
        warnings.append("Qobuzアプリが見つかりません")
    elif not running:
        warnings.append("Qobuzアプリが実行されていません")
    if player.get("status") == "UNAVAILABLE":
        warnings.append(player.get("error", "Qobuz player状態を取得できません"))
    schema_ok = False
    if os.path.isfile(db_path):
        try:
            with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as connection:
                schema_ok = SUPPORTED_SCHEMA_COLUMNS <= _table_columns(connection, "L_Track")
        except sqlite3.Error as exc:
            warnings.append(f"Qobuz DBを確認できません: {exc}")
    else:
        warnings.append("Qobuz DBが見つかりません")
    if not schema_ok and os.path.isfile(db_path):
        warnings.append("Qobuz DBスキーマが未対応です")
    return {
        "available": bool(
            version and running and player.get("status") != "UNAVAILABLE" and schema_ok
        ),
        "app_version": version,
        "app_running": running,
        "qobuz_dir": base,
        "player_available": player.get("status") != "UNAVAILABLE",
        "database_available": os.path.isfile(db_path),
        "schema_ok": schema_ok,
        "warnings": warnings,
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def get_qobuz_snapshot(qobuz_dir=None):
    base = qobuz_dir or default_qobuz_dir()
    player = read_qobuz_player_state(base)
    if player.get("status") != "OK":
        player["provider"] = "qobuz"
        return player
    track = read_qobuz_track_record(
        player.get("track_id"), os.path.join(base, "qobuz.db")
    )
    snapshot = {
        **player,
        "provider": "qobuz",
        "app_version": qobuz_app_version(),
    }
    if track.get("available"):
        snapshot.update(track)
        snapshot["status"] = "OK"
        snapshot["source_verified"] = True
    else:
        snapshot["source_verified"] = False
        snapshot["source_error"] = track.get("error")
    return snapshot


def evaluate_qobuz_capture_gate(snapshot, device):
    source_verified = bool(snapshot.get("source_verified"))
    source_rate = snapshot.get("source_sample_rate")
    source_depth = snapshot.get("source_bit_depth")
    source_channels = snapshot.get("source_channels")
    warnings = []

    if not source_verified:
        warnings.append(snapshot.get("source_error") or "Qobuz Offlineソースを検証できません")
    if not snapshot.get("is_completed"):
        warnings.append("Qobuz Offlineトラックの完全ダウンロードを確認できません")
    if not source_rate or not source_depth:
        warnings.append("Qobuzソースのサンプルレート/bit深度が不明です")
    if source_rate and _as_int(source_rate) not in QOBUZ_SUPPORTED_SAMPLE_RATES:
        warnings.append(f"Qobuz標準外のサンプルレートです: {source_rate}Hz")
    if source_depth and _as_int(source_depth) not in {16, 24}:
        warnings.append(f"Qobuz標準外のbit深度です: {source_depth}-bit")
    if _as_int(source_channels) != 2:
        warnings.append(f"Qobuzソースがステレオではありません: {source_channels}ch")
    if source_verified:
        format_spec = QOBUZ_FORMATS.get(_as_int(snapshot.get("format_id")))
        if not format_spec or not format_spec.get("lossless"):
            warnings.append(
                f"Qobuz Lossless形式を確認できません: format {snapshot.get('format_id')}"
            )
    if snapshot.get("volume_percent") is not None and abs(snapshot["volume_percent"] - 100.0) > 0.01:
        warnings.append(f"Qobuz音量が100%ではありません: {snapshot['volume_percent']:.1f}%")
    if snapshot.get("muted") is True:
        warnings.append("Qobuzがミュートされています")
    if snapshot.get("exclusive_mode") is False:
        warnings.append("Qobuz Exclusive ModeがOFFです")
    if source_verified and snapshot.get("volume_percent") is None:
        warnings.append("Qobuz音量100%を検証できません")
    if source_verified and snapshot.get("exclusive_mode") is None:
        warnings.append("Qobuz Exclusive Modeを検証できません")
    if source_verified and snapshot.get("muted") is None:
        warnings.append("QobuzミュートOFFを検証できません")

    device_rate = _as_int(device.get("nominal_sample_rate"))
    if source_rate and device_rate != _as_int(source_rate):
        warnings.append(f"レート不一致: Qobuz {source_rate}Hz / CoreAudio {device_rate}Hz")
    if device.get("is_aggregate"):
        warnings.append("Aggregate/Multi-Output Deviceは使用できません")
    if not device.get("uid"):
        warnings.append("CoreAudio Device UIDを取得できません")
    if int(device.get("max_input_channels", 0)) < 2:
        warnings.append("入力デバイスにステレオ入力がありません")
    name = str(device.get("name", "")).lower()
    if not any(token in name for token in ("loopback", "blackhole")):
        warnings.append("単一のLoopback/BlackHole入力を確認できません")
    output_name = str(snapshot.get("output_device_name") or "")
    if source_verified and not output_name:
        warnings.append("Qobuz出力デバイスを検証できません")
    elif output_name and output_name.strip().lower() != str(device.get("name", "")).strip().lower():
        warnings.append(
            f"Qobuz出力先と録音入力が一致しません: {output_name} / {device.get('name')}"
        )

    verified_label = "Qobuz Offline条件適合・bit一致未証明"
    if not source_verified:
        verified_label = "Qobuz Offlineソース品質未検証"
    return {
        "conditions_pass": not warnings,
        "warnings": warnings,
        "source_verified": source_verified,
        "source_sample_rate": _as_int(source_rate),
        "source_bit_depth": _as_int(source_depth),
        "source_channels": _as_int(source_channels),
        "assurance_label": verified_label if not warnings else f"要確認: {verified_label}",
        "mode": "offline",
        "evidence": {
            "track_id": snapshot.get("track_id"),
            "format_id": snapshot.get("format_id"),
            "format_label": snapshot.get("format_label"),
            "is_completed": snapshot.get("is_completed"),
            "volume_percent": snapshot.get("volume_percent"),
            "exclusive_mode": snapshot.get("exclusive_mode"),
            "output_device_name": snapshot.get("output_device_name"),
            "device_uid": device.get("uid"),
            "device_transport": device.get("transport"),
            "app_version": snapshot.get("app_version"),
        },
    }
