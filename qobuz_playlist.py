import json
import os
import sqlite3
from dataclasses import asdict, dataclass, field

from qobuz_integration import (
    QOBUZ_FORMATS,
    QOBUZ_SUPPORTED_SAMPLE_RATES,
    default_qobuz_dir,
)


OFFLINE_COMPLETE_STATUSES = {"download", "import"}


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


def _normalize_rate(value):
    rate = _as_float(value)
    if rate is None or rate <= 0:
        return None
    if rate <= 384.0:
        rate *= 1000.0
    return int(round(rate))


def _json(value):
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, (dict, list)) else {}


def _walk(value):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _first(value, keys):
    for node in _walk(value):
        if not isinstance(node, dict):
            continue
        for key in keys:
            candidate = node.get(key)
            if candidate not in (None, ""):
                return candidate
    return None


def _table_columns(connection, table):
    return {
        row[1]
        for row in connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    }


def _required_tables(connection):
    return {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }


def _readonly_connection(database_path):
    path = os.path.abspath(os.path.expanduser(database_path))
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Qobuz DBが見つかりません: {path}")
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
    connection.row_factory = sqlite3.Row
    return connection


def default_qobuz_database():
    return os.path.join(default_qobuz_dir(), "qobuz.db")


def _playlist_names(connection):
    names = {}
    tables = _required_tables(connection)
    if "L_Playlist" in tables:
        columns = _table_columns(connection, "L_Playlist")
        if {"id", "name"} <= columns:
            for row in connection.execute("SELECT id, name FROM L_Playlist"):
                if row["name"]:
                    names[str(row["id"])] = str(row["name"])

    if "Collection_Item" not in tables:
        return names
    try:
        rows = connection.execute("SELECT * FROM Collection_Item").fetchall()
    except sqlite3.Error:
        return names
    for row in rows:
        raw = dict(row)
        payloads = [_json(value) for value in raw.values()]
        payload = next((value for value in payloads if value), {})
        item_id = _first(payload, ("playlist_id", "playlistId", "id"))
        if item_id is None:
            for key in ("playlist_id", "item_id", "id"):
                if raw.get(key) is not None:
                    item_id = raw[key]
                    break
        name = _first(payload, ("name", "title", "playlist_name", "item_name"))
        if name is None:
            for key in ("name", "title", "item_name"):
                if raw.get(key):
                    name = raw[key]
                    break
        if item_id is not None and name:
            names.setdefault(str(item_id), str(name))
    return names


@dataclass(frozen=True)
class QobuzPlaylistSummary:
    playlist_id: str
    name: str
    track_count: int
    synchronized_at: str | None = None

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class QobuzPlaylistTrack:
    playlist_id: str
    original_index: int
    track_id: str
    album_id: str | None
    track_number: int | None
    media_number: int | None
    name: str
    artist: str
    album: str
    duration: float
    format_id: int | None
    format_label: str
    source_sample_rate: int | None
    source_bit_depth: int | None
    source_channels: int | None
    offline_status: str | None
    is_completed: bool
    artwork_url: str | None
    excluded: bool = False
    issues: tuple[str, ...] = field(default_factory=tuple)

    @property
    def eligible(self):
        return not self.excluded and not self.issues

    @property
    def blocking(self):
        return not self.excluded and bool(self.issues)

    def to_dict(self):
        result = asdict(self)
        result["eligible"] = self.eligible
        result["blocking"] = self.blocking
        result["issues"] = list(self.issues)
        result.update(
            provider="qobuz",
            source_mode="offline",
            track_id=self.track_id,
            artwork_url=self.artwork_url,
        )
        return result


@dataclass(frozen=True)
class QobuzPlaylistScan:
    playlist_id: str
    name: str
    tracks: tuple[QobuzPlaylistTrack, ...]
    database_path: str

    @property
    def blocking_tracks(self):
        return tuple(track for track in self.tracks if track.blocking)

    @property
    def eligible_tracks(self):
        return tuple(track for track in self.tracks if track.eligible)

    @property
    def excluded_tracks(self):
        return tuple(track for track in self.tracks if track.excluded)

    @property
    def can_start(self):
        return bool(self.eligible_tracks) and not self.blocking_tracks

    @property
    def rate_groups(self):
        groups = {}
        for track in self.eligible_tracks:
            groups.setdefault(track.source_sample_rate, 0)
            groups[track.source_sample_rate] += 1
        return dict(sorted(groups.items()))

    def execution_tracks(self):
        return tuple(
            sorted(
                self.eligible_tracks,
                key=lambda item: (item.source_sample_rate or 0, item.original_index),
            )
        )

    def to_dict(self):
        return {
            "playlist_id": self.playlist_id,
            "name": self.name,
            "tracks": [track.to_dict() for track in self.tracks],
            "database_path": self.database_path,
            "total_tracks": len(self.tracks),
            "eligible_tracks": len(self.eligible_tracks),
            "blocking_tracks": len(self.blocking_tracks),
            "excluded_tracks": len(self.excluded_tracks),
            "can_start": self.can_start,
            "rate_groups": self.rate_groups,
            "total_duration_sec": sum(track.duration for track in self.eligible_tracks),
        }


def list_qobuz_playlists(database_path=None):
    path = database_path or default_qobuz_database()
    with _readonly_connection(path) as connection:
        tables = _required_tables(connection)
        if not {"S_Playlist", "S_Playlist_Track"} <= tables:
            raise RuntimeError("QobuzプレイリストDBスキーマが未対応です")
        columns = _table_columns(connection, "S_Playlist")
        names = _playlist_names(connection)
        synchronized = "synchronized_at" if "synchronized_at" in columns else "NULL"
        rows = connection.execute(
            f"""
            SELECT p.id, {synchronized} AS synchronized_at, COUNT(pt.track_id) AS track_count
            FROM S_Playlist p
            LEFT JOIN S_Playlist_Track pt ON CAST(pt.playlist_id AS TEXT) = CAST(p.id AS TEXT)
            GROUP BY p.id
            HAVING COUNT(pt.track_id) > 0
            ORDER BY p.id
            """
        ).fetchall()
    return [
        QobuzPlaylistSummary(
            playlist_id=str(row["id"]),
            name=names.get(str(row["id"]), f"Playlist {row['id']}"),
            track_count=int(row["track_count"]),
            synchronized_at=row["synchronized_at"],
        )
        for row in rows
    ]


def _track_metadata(row):
    stream_data = _json(row.get("stream_data"))
    offline_data = _json(row.get("offline_data"))
    payload = offline_data or stream_data
    title = row.get("stream_title") or _first(payload, ("title", "name")) or "Unknown"
    artist = row.get("stream_artist") or _first(
        payload, ("performer", "artist", "track_artists_names")
    )
    if isinstance(artist, dict):
        artist = artist.get("name") or artist.get("display_name")
    album = row.get("stream_album") or _first(payload, ("release_name", "album"))
    if isinstance(album, dict):
        album = album.get("title") or album.get("name")
    artwork = _first(payload, ("large", "small", "artwork_url", "image_url"))
    return str(title), str(artist or "Unknown"), str(album or "Unknown"), artwork


def scan_qobuz_playlist(playlist_id, excluded_track_ids=(), database_path=None):
    path = database_path or default_qobuz_database()
    excluded = {str(value) for value in excluded_track_ids}
    with _readonly_connection(path) as connection:
        tables = _required_tables(connection)
        required = {"S_Playlist", "S_Playlist_Track", "S_Track", "L_Track"}
        if not required <= tables:
            missing = ", ".join(sorted(required - tables))
            raise RuntimeError(f"QobuzプレイリストDBスキーマが未対応です: {missing}")
        names = _playlist_names(connection)
        stream_columns = _table_columns(connection, "S_Track")
        offline_columns = _table_columns(connection, "L_Track")

        def stream(column, alias):
            return f's."{column}" AS {alias}' if column in stream_columns else f"NULL AS {alias}"

        def offline(column, alias):
            return f'l."{column}" AS {alias}' if column in offline_columns else f"NULL AS {alias}"

        sort_column = "sort" if "sort" in _table_columns(connection, "S_Playlist_Track") else "rowid"
        query = f"""
            SELECT pt.track_id, pt.{sort_column} AS playlist_sort,
                   {stream('title', 'stream_title')},
                   {stream('track_artists_names', 'stream_artist')},
                   {stream('release_name', 'stream_album')},
                   {stream('duration', 'stream_duration')},
                   {stream('data', 'stream_data')},
                   {offline('data', 'offline_data')},
                   {offline('status', 'offline_status')},
                   {offline('format', 'format_id')},
                   {offline('sampling_rate', 'sampling_rate')},
                   {offline('bit_depth', 'bit_depth')},
                   {offline('duration', 'offline_duration')},
                   {offline('is_completed', 'is_completed')}
            FROM S_Playlist_Track pt
            LEFT JOIN S_Track s ON CAST(s.id AS TEXT) = CAST(pt.track_id AS TEXT)
            LEFT JOIN L_Track l ON CAST(l.track_id AS TEXT) = CAST(pt.track_id AS TEXT)
            WHERE CAST(pt.playlist_id AS TEXT) = ?
            ORDER BY pt.{sort_column}, pt.rowid
        """
        rows = [dict(row) for row in connection.execute(query, (str(playlist_id),))]

    if not rows:
        raise ValueError(f"Qobuzプレイリストが空か見つかりません: {playlist_id}")
    tracks = []
    for index, row in enumerate(rows):
        track_id = str(row["track_id"])
        offline_data = _json(row.get("offline_data"))
        format_id = _as_int(row.get("format_id") or _first(offline_data, ("format_id",)))
        rate = _normalize_rate(
            row.get("sampling_rate")
            or _first(offline_data, ("sampling_rate", "maximum_sampling_rate"))
        )
        depth = _as_int(
            row.get("bit_depth")
            or _first(offline_data, ("bit_depth", "maximum_bit_depth"))
        )
        channels = _as_int(
            _first(offline_data, ("channels", "maximum_channel_count"))
        )
        album_payload = offline_data.get("album")
        if not isinstance(album_payload, dict):
            album_payload = {}
        album_id = album_payload.get("id") or album_payload.get("qobuz_id")
        track_number = _as_int(offline_data.get("track_number"))
        media_number = _as_int(offline_data.get("media_number"))
        status = row.get("offline_status")
        completed = bool(row.get("is_completed"))
        issues = []
        if not offline_data and status is None:
            issues.append("オフライン音源が見つかりません")
        if not completed:
            issues.append("完全ダウンロードされていません")
        if str(status or "").lower() not in OFFLINE_COMPLETE_STATUSES:
            issues.append(f"オフライン状態が未確定です: {status or 'unknown'}")
        spec = QOBUZ_FORMATS.get(format_id)
        if not spec or not spec.get("lossless"):
            issues.append(f"Lossless形式を確認できません: format {format_id}")
        if rate not in QOBUZ_SUPPORTED_SAMPLE_RATES:
            issues.append(f"サンプルレートが未対応です: {rate}")
        if depth not in {16, 24}:
            issues.append(f"bit深度が未対応です: {depth}")
        if channels != 2:
            issues.append(f"ステレオ2chを確認できません: {channels}")
        if not album_id:
            issues.append("公開QobuzアルバムIDを確認できません")
        if track_number is None:
            issues.append("Qobuzアルバム内の曲番号を確認できません")
        name, artist, album, artwork = _track_metadata(row)
        duration = _as_float(row.get("offline_duration") or row.get("stream_duration")) or 0.0
        tracks.append(
            QobuzPlaylistTrack(
                playlist_id=str(playlist_id),
                original_index=index,
                track_id=track_id,
                album_id=None if album_id is None else str(album_id),
                track_number=track_number,
                media_number=media_number,
                name=name,
                artist=artist,
                album=album,
                duration=duration,
                format_id=format_id,
                format_label=QOBUZ_FORMATS.get(format_id, {}).get(
                    "label", f"format {format_id}"
                ),
                source_sample_rate=rate,
                source_bit_depth=depth,
                source_channels=channels,
                offline_status=None if status is None else str(status),
                is_completed=completed,
                artwork_url=None if artwork is None else str(artwork),
                excluded=track_id in excluded,
                issues=tuple(dict.fromkeys(issues)),
            )
        )
    return QobuzPlaylistScan(
        playlist_id=str(playlist_id),
        name=names.get(str(playlist_id), f"Playlist {playlist_id}"),
        tracks=tuple(tracks),
        database_path=os.path.abspath(os.path.expanduser(path)),
    )


def write_playlist_m3u8(path, scan, output_paths):
    target = os.path.abspath(os.path.expanduser(path))
    os.makedirs(os.path.dirname(target), exist_ok=True)
    lines = ["#EXTM3U", f"#PLAYLIST:{scan.name}"]
    for track in sorted(scan.eligible_tracks, key=lambda item: item.original_index):
        output = output_paths.get(track.track_id)
        if not output:
            continue
        lines.append(f"#EXTINF:{int(round(track.duration))},{track.artist} - {track.name}")
        lines.append(os.path.relpath(output, os.path.dirname(target)))
    with open(target, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")
    return target
