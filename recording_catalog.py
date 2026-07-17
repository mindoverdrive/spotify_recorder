import json
import os
import sqlite3
from datetime import datetime, timezone


SCHEMA_VERSION = 3


def legacy_catalog_path():
    return os.path.expanduser(
        "~/Library/Application Support/SpotifyRecorder/recordings.sqlite3"
    )


def default_catalog_path():
    target = os.path.expanduser(
        "~/Library/Application Support/HiResRecorder/recordings.sqlite3"
    )
    migrate_legacy_catalog(legacy_catalog_path(), target)
    return target


def migrate_legacy_catalog(source_path, target_path):
    source = os.path.abspath(os.path.expanduser(source_path))
    target = os.path.abspath(os.path.expanduser(target_path))
    if os.path.exists(target) or not os.path.isfile(source):
        return False
    os.makedirs(os.path.dirname(target), exist_ok=True)
    source_uri = f"file:{source}?mode=ro"
    with sqlite3.connect(source_uri, uri=True) as old, sqlite3.connect(target) as new:
        old.backup(new)
    return True


RECORDING_COLUMNS = {
    "provider": "TEXT NOT NULL DEFAULT 'spotify'",
    "source_mode": "TEXT",
    "source_track_id": "TEXT",
    "source_format_id": "INTEGER",
    "source_sample_rate": "INTEGER",
    "source_bit_depth": "INTEGER",
    "source_channels": "INTEGER",
    "source_verified": "INTEGER",
    "qobuz_app_version": "TEXT",
    "capture_session_id": "TEXT",
    "requires_rerecord": "INTEGER NOT NULL DEFAULT 0",
    "evidence_json": "TEXT NOT NULL DEFAULT '{}'",
}


def _ensure_columns(connection, table, columns):
    existing = {
        row[1] for row in connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    }
    for name, declaration in columns.items():
        if name not in existing:
            connection.execute(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {declaration}')


def _connect(database_path):
    path = os.path.abspath(os.path.expanduser(database_path))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    connection = sqlite3.connect(path, timeout=10.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
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
            suspect_events_json TEXT NOT NULL,
            provider TEXT NOT NULL DEFAULT 'spotify',
            source_mode TEXT,
            source_track_id TEXT,
            source_format_id INTEGER,
            source_sample_rate INTEGER,
            source_bit_depth INTEGER,
            source_channels INTEGER,
            source_verified INTEGER,
            qobuz_app_version TEXT,
            capture_session_id TEXT,
            requires_rerecord INTEGER NOT NULL DEFAULT 0,
            evidence_json TEXT NOT NULL DEFAULT '{}'
        )
        """
    )
    _ensure_columns(connection, "recordings", RECORDING_COLUMNS)
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS capture_sessions (
            session_id TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            source_mode TEXT,
            started_at TEXT,
            ended_at TEXT,
            sample_rate INTEGER NOT NULL,
            device_name TEXT,
            assurance_label TEXT,
            quality_gate_pass INTEGER,
            network_json TEXT NOT NULL,
            evidence_json TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS quality_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recording_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            severity TEXT NOT NULL,
            start_sample INTEGER NOT NULL,
            end_sample INTEGER,
            time_sec REAL NOT NULL,
            duration_sec REAL NOT NULL,
            detail TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            FOREIGN KEY(recording_id) REFERENCES recordings(id) ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS flac_exports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recording_id INTEGER,
            source_wav_path TEXT NOT NULL UNIQUE,
            flac_path TEXT,
            status TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            bit_depth INTEGER NOT NULL DEFAULT 24,
            dither TEXT NOT NULL DEFAULT 'TPDF',
            sample_peak_dbfs REAL,
            artwork_embedded INTEGER,
            source_bytes INTEGER,
            flac_bytes INTEGER,
            wav_deleted INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(recording_id) REFERENCES recordings(id) ON DELETE SET NULL
        )
        """
    )
    _ensure_columns(
        connection,
        "flac_exports",
        {"wav_deleted": "INTEGER NOT NULL DEFAULT 0"},
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    connection.execute(
        "INSERT OR REPLACE INTO schema_metadata(key, value) VALUES('version', ?)",
        (str(SCHEMA_VERSION),),
    )
    connection.commit()
    return connection


def _event_severity(event):
    if event.get("type") in {
        "audio_callback",
        "adc_timeline_gap",
        "source_error",
        "sample_rate_change",
        "spool_overflow",
    }:
        return "error"
    return "warning"


def record_saved_recording(
    file_path,
    track,
    analysis,
    capture_audit=None,
    database_path=None,
):
    path = os.path.abspath(os.path.expanduser(file_path))
    audit = capture_audit or {}
    source = audit.get("source_evaluation") or {}
    evidence = source.get("evidence") or {}
    provider = str(audit.get("provider") or track.get("provider") or "spotify").lower()
    warnings = list(analysis.get("warnings", [])) + list(audit.get("warnings", []))
    events = list(audit.get("events", []))
    suspect_events = {
        "capture": events,
        "full_scale_ranges": analysis.get("full_scale_ranges", []),
        "sample_peak_time_sec": analysis.get("sample_peak_time_sec"),
        "true_peak_time_sec": analysis.get("true_peak_time_sec"),
    }
    session_id = str(
        audit.get("capture_session_id")
        or audit.get("started_at")
        or datetime.now(timezone.utc).isoformat(timespec="microseconds")
    )
    quality_gate_pass = (
        None
        if not audit
        else int(bool(audit.get("quality_gate_pass", False) and not analysis.get("warnings")))
    )
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
        "quality_gate_pass": quality_gate_pass,
        "assurance_label": audit.get("assurance_label"),
        "warnings_json": json.dumps(warnings, ensure_ascii=False),
        "suspect_events_json": json.dumps(suspect_events, ensure_ascii=False),
        "provider": provider,
        "source_mode": source.get("mode") or track.get("source_mode"),
        "source_track_id": evidence.get("track_id") or track.get("track_id"),
        "source_format_id": evidence.get("format_id"),
        "source_sample_rate": source.get("source_sample_rate"),
        "source_bit_depth": source.get("source_bit_depth"),
        "source_channels": source.get("source_channels"),
        "source_verified": None if not source else int(bool(source.get("source_verified"))),
        "qobuz_app_version": evidence.get("app_version"),
        "capture_session_id": session_id,
        "requires_rerecord": int(quality_gate_pass == 0 or bool(analysis.get("warnings"))),
        "evidence_json": json.dumps(evidence, ensure_ascii=False),
    }
    target = database_path or default_catalog_path()
    with _connect(target) as connection:
        connection.execute(
            """
            INSERT INTO capture_sessions (
                session_id, provider, source_mode, started_at, ended_at,
                sample_rate, device_name, assurance_label, quality_gate_pass,
                network_json, evidence_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                ended_at=excluded.ended_at,
                assurance_label=excluded.assurance_label,
                quality_gate_pass=excluded.quality_gate_pass,
                network_json=excluded.network_json,
                evidence_json=excluded.evidence_json
            """,
            (
                session_id,
                provider,
                values["source_mode"],
                audit.get("started_at"),
                audit.get("ended_at"),
                values["sample_rate"],
                audit.get("device_name"),
                values["assurance_label"],
                quality_gate_pass,
                json.dumps(audit.get("network_observation") or {}, ensure_ascii=False),
                values["evidence_json"],
            ),
        )
        connection.execute(
            """
            INSERT INTO recordings (
                saved_at, file_path, file_name, title, artist, album,
                duration_sec, sample_rate, channels, integrated_lufs,
                sample_peak_dbfs, true_peak_dbtp, quality_gate_pass,
                assurance_label, warnings_json, suspect_events_json,
                provider, source_mode, source_track_id, source_format_id,
                source_sample_rate, source_bit_depth, source_channels,
                source_verified, qobuz_app_version, capture_session_id,
                requires_rerecord, evidence_json
            ) VALUES (
                :saved_at, :file_path, :file_name, :title, :artist, :album,
                :duration_sec, :sample_rate, :channels, :integrated_lufs,
                :sample_peak_dbfs, :true_peak_dbtp, :quality_gate_pass,
                :assurance_label, :warnings_json, :suspect_events_json,
                :provider, :source_mode, :source_track_id, :source_format_id,
                :source_sample_rate, :source_bit_depth, :source_channels,
                :source_verified, :qobuz_app_version, :capture_session_id,
                :requires_rerecord, :evidence_json
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
                suspect_events_json=excluded.suspect_events_json,
                provider=excluded.provider,
                source_mode=excluded.source_mode,
                source_track_id=excluded.source_track_id,
                source_format_id=excluded.source_format_id,
                source_sample_rate=excluded.source_sample_rate,
                source_bit_depth=excluded.source_bit_depth,
                source_channels=excluded.source_channels,
                source_verified=excluded.source_verified,
                qobuz_app_version=excluded.qobuz_app_version,
                capture_session_id=excluded.capture_session_id,
                requires_rerecord=excluded.requires_rerecord,
                evidence_json=excluded.evidence_json
            """,
            values,
        )
        recording_id = connection.execute(
            "SELECT id FROM recordings WHERE file_path = ?", (path,)
        ).fetchone()[0]
        connection.execute("DELETE FROM quality_events WHERE recording_id = ?", (recording_id,))
        for event in events:
            start_sample = int(event.get("sample", 0))
            duration = float(event.get("duration_sec", 0.0))
            end_sample = start_sample + int(duration * values["sample_rate"]) if duration else None
            connection.execute(
                """
                INSERT INTO quality_events (
                    recording_id, event_type, severity, start_sample, end_sample,
                    time_sec, duration_sec, detail, evidence_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    recording_id,
                    event.get("type", "unknown"),
                    _event_severity(event),
                    start_sample,
                    end_sample,
                    float(event.get("time_sec", 0.0)),
                    duration,
                    event.get("detail", event.get("type", "異常疑い")),
                    json.dumps(event, ensure_ascii=False),
                ),
            )
    return recording_id


def record_flac_export(
    source_wav_path,
    flac_path,
    status,
    reason="",
    sample_peak_dbfs=None,
    artwork_embedded=None,
    source_bytes=None,
    flac_bytes=None,
    wav_deleted=False,
    database_path=None,
):
    source_path = os.path.abspath(os.path.expanduser(source_wav_path))
    output_path = (
        os.path.abspath(os.path.expanduser(flac_path)) if flac_path else None
    )
    target = database_path or default_catalog_path()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _connect(target) as connection:
        row = connection.execute(
            "SELECT id FROM recordings WHERE file_path = ?", (source_path,)
        ).fetchone()
        if row:
            recording_id = row[0]
        else:
            existing = connection.execute(
                "SELECT recording_id FROM flac_exports WHERE source_wav_path = ?",
                (source_path,),
            ).fetchone()
            recording_id = existing[0] if existing else None
        connection.execute(
            """
            INSERT INTO flac_exports (
                recording_id, source_wav_path, flac_path, status, reason,
                bit_depth, dither, sample_peak_dbfs, artwork_embedded,
                source_bytes, flac_bytes, wav_deleted, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 24, 'TPDF', ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_wav_path) DO UPDATE SET
                recording_id=excluded.recording_id,
                flac_path=excluded.flac_path,
                status=excluded.status,
                reason=excluded.reason,
                bit_depth=excluded.bit_depth,
                dither=excluded.dither,
                sample_peak_dbfs=excluded.sample_peak_dbfs,
                artwork_embedded=excluded.artwork_embedded,
                source_bytes=excluded.source_bytes,
                flac_bytes=excluded.flac_bytes,
                wav_deleted=excluded.wav_deleted,
                updated_at=excluded.updated_at
            """,
            (
                recording_id,
                source_path,
                output_path,
                str(status),
                str(reason or ""),
                sample_peak_dbfs,
                None if artwork_embedded is None else int(bool(artwork_embedded)),
                source_bytes,
                flac_bytes,
                int(bool(wav_deleted)),
                now,
                now,
            ),
        )


def replace_recording_file(source_wav_path, flac_path, database_path=None):
    source_path = os.path.abspath(os.path.expanduser(source_wav_path))
    output_path = os.path.abspath(os.path.expanduser(flac_path))
    target = database_path or default_catalog_path()
    with _connect(target) as connection:
        row = connection.execute(
            "SELECT recording_id FROM flac_exports WHERE source_wav_path = ?",
            (source_path,),
        ).fetchone()
        recording_id = row[0] if row else None
        if recording_id is None:
            row = connection.execute(
                "SELECT id FROM recordings WHERE file_path = ?", (source_path,)
            ).fetchone()
            recording_id = row[0] if row else None
        if recording_id is not None:
            connection.execute(
                "UPDATE recordings SET file_path = ?, file_name = ? WHERE id = ?",
                (output_path, os.path.basename(output_path), recording_id),
            )
            connection.execute(
                "UPDATE flac_exports SET recording_id = ? WHERE source_wav_path = ?",
                (recording_id, source_path),
            )


def list_flac_exports(
    query="",
    status=None,
    limit=500,
    database_path=None,
):
    target = database_path or default_catalog_path()
    search = f"%{query.strip()}%"
    conditions = [
        "(:query = '' OR r.title LIKE :search COLLATE NOCASE "
        "OR r.artist LIKE :search COLLATE NOCASE "
        "OR r.album LIKE :search COLLATE NOCASE "
        "OR e.source_wav_path LIKE :search COLLATE NOCASE "
        "OR e.flac_path LIKE :search COLLATE NOCASE)"
    ]
    parameters = {"query": query.strip(), "search": search, "limit": int(limit)}
    if status:
        conditions.append("e.status = :status")
        parameters["status"] = str(status)
    sql = (
        "SELECT e.*, r.title, r.artist, r.album, r.provider, r.source_mode "
        "FROM flac_exports e LEFT JOIN recordings r ON r.id = e.recording_id WHERE "
        + " AND ".join(conditions)
        + " ORDER BY e.updated_at DESC, e.id DESC LIMIT :limit"
    )
    with _connect(target) as connection:
        rows = connection.execute(sql, parameters).fetchall()
    exports = []
    for row in rows:
        item = dict(row)
        item["source_exists"] = os.path.isfile(item["source_wav_path"])
        item["flac_exists"] = bool(
            item.get("flac_path") and os.path.isfile(item["flac_path"])
        )
        exports.append(item)
    return exports


def list_recordings(
    query="",
    limit=500,
    database_path=None,
    provider=None,
    assurance=None,
    requires_rerecord=None,
):
    target = database_path or default_catalog_path()
    search = f"%{query.strip()}%"
    conditions = [
        "(:query = '' OR title LIKE :search COLLATE NOCASE OR artist LIKE :search COLLATE NOCASE "
        "OR album LIKE :search COLLATE NOCASE OR file_name LIKE :search COLLATE NOCASE)"
    ]
    parameters = {"query": query.strip(), "search": search, "limit": int(limit)}
    if provider:
        conditions.append("provider = :provider")
        parameters["provider"] = str(provider).lower()
    if assurance:
        conditions.append("assurance_label LIKE :assurance COLLATE NOCASE")
        parameters["assurance"] = f"%{assurance}%"
    if requires_rerecord is not None:
        conditions.append("requires_rerecord = :requires_rerecord")
        parameters["requires_rerecord"] = int(bool(requires_rerecord))
    sql = (
        "SELECT * FROM recordings WHERE "
        + " AND ".join(conditions)
        + " ORDER BY saved_at DESC, id DESC LIMIT :limit"
    )
    with _connect(target) as connection:
        rows = connection.execute(sql, parameters).fetchall()
    recordings = []
    for row in rows:
        item = dict(row)
        item["warnings"] = json.loads(item.pop("warnings_json") or "[]")
        item["suspect_events"] = json.loads(item.pop("suspect_events_json") or "{}")
        item["evidence"] = json.loads(item.pop("evidence_json") or "{}")
        item["file_exists"] = os.path.isfile(item["file_path"])
        recordings.append(item)
    return recordings
