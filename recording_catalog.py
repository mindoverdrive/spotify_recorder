import json
import os
import sqlite3
from datetime import datetime, timezone


def default_catalog_path():
    return os.path.expanduser(
        "~/Library/Application Support/SpotifyRecorder/recordings.sqlite3"
    )


def _connect(database_path):
    path = os.path.abspath(os.path.expanduser(database_path))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    connection = sqlite3.connect(path, timeout=10.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS recordings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            saved_at TEXT NOT NULL,
            file_path TEXT NOT NULL UNIQUE,
            file_name TEXT NOT NULL,
            title TEXT NOT NULL,
            artist TEXT NOT NULL,
            album TEXT NOT NULL,
            duration_sec REAL NOT NULL,
            sample_rate INTEGER NOT NULL,
            channels INTEGER NOT NULL,
            integrated_lufs REAL,
            sample_peak_dbfs REAL,
            true_peak_dbtp REAL,
            quality_gate_pass INTEGER,
            assurance_label TEXT,
            warnings_json TEXT NOT NULL,
            suspect_events_json TEXT NOT NULL
        )
        """
    )
    return connection


def record_saved_recording(
    file_path,
    track,
    analysis,
    capture_audit=None,
    database_path=None,
):
    path = os.path.abspath(os.path.expanduser(file_path))
    audit = capture_audit or {}
    warnings = list(analysis.get("warnings", [])) + list(audit.get("warnings", []))
    suspect_events = {
        "capture": audit.get("events", []),
        "full_scale_ranges": analysis.get("full_scale_ranges", []),
        "sample_peak_time_sec": analysis.get("sample_peak_time_sec"),
        "true_peak_time_sec": analysis.get("true_peak_time_sec"),
    }
    values = {
        "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "file_path": path,
        "file_name": os.path.basename(path),
        "title": track.get("name", "Unknown"),
        "artist": track.get("artist", "Unknown"),
        "album": track.get("album", "Unknown"),
        "duration_sec": float(analysis.get("duration_sec", 0.0)),
        "sample_rate": int(analysis.get("sample_rate", 0)),
        "channels": int(analysis.get("channels", 0)),
        "integrated_lufs": analysis.get("integrated_lufs"),
        "sample_peak_dbfs": analysis.get("sample_peak_dbfs"),
        "true_peak_dbtp": analysis.get("true_peak_dbtp"),
        "quality_gate_pass": (
            None
            if not audit
            else int(bool(audit.get("quality_gate_pass", False) and not analysis.get("warnings")))
        ),
        "assurance_label": audit.get("assurance_label"),
        "warnings_json": json.dumps(warnings, ensure_ascii=False),
        "suspect_events_json": json.dumps(suspect_events, ensure_ascii=False),
    }
    target = database_path or default_catalog_path()
    with _connect(target) as connection:
        connection.execute(
            """
            INSERT INTO recordings (
                saved_at, file_path, file_name, title, artist, album,
                duration_sec, sample_rate, channels, integrated_lufs,
                sample_peak_dbfs, true_peak_dbtp, quality_gate_pass,
                assurance_label, warnings_json, suspect_events_json
            ) VALUES (
                :saved_at, :file_path, :file_name, :title, :artist, :album,
                :duration_sec, :sample_rate, :channels, :integrated_lufs,
                :sample_peak_dbfs, :true_peak_dbtp, :quality_gate_pass,
                :assurance_label, :warnings_json, :suspect_events_json
            )
            ON CONFLICT(file_path) DO UPDATE SET
                saved_at=excluded.saved_at,
                file_name=excluded.file_name,
                title=excluded.title,
                artist=excluded.artist,
                album=excluded.album,
                duration_sec=excluded.duration_sec,
                sample_rate=excluded.sample_rate,
                channels=excluded.channels,
                integrated_lufs=excluded.integrated_lufs,
                sample_peak_dbfs=excluded.sample_peak_dbfs,
                true_peak_dbtp=excluded.true_peak_dbtp,
                quality_gate_pass=excluded.quality_gate_pass,
                assurance_label=excluded.assurance_label,
                warnings_json=excluded.warnings_json,
                suspect_events_json=excluded.suspect_events_json
            """,
            values,
        )


def list_recordings(query="", limit=500, database_path=None):
    target = database_path or default_catalog_path()
    search = f"%{query.strip()}%"
    with _connect(target) as connection:
        rows = connection.execute(
            """
            SELECT * FROM recordings
            WHERE :query = ''
               OR title LIKE :search COLLATE NOCASE
               OR artist LIKE :search COLLATE NOCASE
               OR album LIKE :search COLLATE NOCASE
               OR file_name LIKE :search COLLATE NOCASE
            ORDER BY saved_at DESC, id DESC
            LIMIT :limit
            """,
            {"query": query.strip(), "search": search, "limit": int(limit)},
        ).fetchall()
    recordings = []
    for row in rows:
        item = dict(row)
        item["warnings"] = json.loads(item.pop("warnings_json"))
        item["suspect_events"] = json.loads(item.pop("suspect_events_json"))
        item["file_exists"] = os.path.isfile(item["file_path"])
        recordings.append(item)
    return recordings
