import json
import os
import sqlite3
from datetime import datetime, timezone


SCHEMA_VERSION = 6


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
        CREATE TABLE IF NOT EXISTS audio_exports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recording_id INTEGER,
            source_wav_path TEXT NOT NULL,
            flac_path TEXT,
            export_role TEXT NOT NULL,
            status TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            source_sample_rate INTEGER,
            source_bit_depth INTEGER,
            source_verified INTEGER,
            output_sample_rate INTEGER,
            output_bit_depth INTEGER NOT NULL DEFAULT 24,
            src_engine TEXT,
            src_quality TEXT,
            src_phase TEXT,
            dither TEXT NOT NULL DEFAULT 'NONE',
            dither_reason TEXT NOT NULL DEFAULT '',
            quantizer TEXT NOT NULL DEFAULT 'ROUND_TO_NEAREST_EVEN',
            safety_gain_db REAL NOT NULL DEFAULT 0.0,
            input_true_peak_dbtp REAL,
            output_true_peak_dbtp REAL,
            sample_peak_dbfs REAL,
            artwork_embedded INTEGER,
            source_bytes INTEGER,
            flac_bytes INTEGER,
            wav_deleted INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(recording_id) REFERENCES recordings(id) ON DELETE SET NULL,
            UNIQUE(source_wav_path, export_role)
        )
        """
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO audio_exports (
            recording_id, source_wav_path, flac_path, export_role, status,
            reason, output_bit_depth, dither, dither_reason, quantizer,
            sample_peak_dbfs, artwork_embedded, source_bytes, flac_bytes,
            wav_deleted, created_at, updated_at
        )
        SELECT recording_id, source_wav_path, flac_path, 'archive', status,
               reason, bit_depth, dither, 'Legacy FLAC export',
               'LEGACY_TPDF_QUANTIZER', sample_peak_dbfs, artwork_embedded,
               source_bytes, flac_bytes, wav_deleted, created_at, updated_at
        FROM flac_exports
        """
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
        """
        CREATE TABLE IF NOT EXISTS library_jobs (
            job_id TEXT PRIMARY KEY,
            source_root TEXT NOT NULL,
            destination_root TEXT NOT NULL,
            status TEXT NOT NULL,
            total_files INTEGER NOT NULL DEFAULT 0,
            completed_files INTEGER NOT NULL DEFAULT 0,
            duplicate_files INTEGER NOT NULL DEFAULT 0,
            skipped_files INTEGER NOT NULL DEFAULT 0,
            failed_files INTEGER NOT NULL DEFAULT 0,
            projected_bytes INTEGER NOT NULL DEFAULT 0,
            settings_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS library_assets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL,
            source_path TEXT NOT NULL,
            relative_path TEXT NOT NULL,
            output_path TEXT,
            status TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            source_size INTEGER,
            source_mtime_ns INTEGER,
            source_sha256 TEXT,
            pcm_sha256 TEXT,
            source_codec TEXT,
            source_lossless INTEGER,
            source_sample_rate INTEGER,
            source_bit_depth TEXT,
            source_channels INTEGER,
            duration_sec REAL,
            output_bytes INTEGER,
            dither TEXT,
            src_engine TEXT,
            safety_gain_db REAL,
            artwork_embedded INTEGER,
            processing_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(job_id) REFERENCES library_jobs(job_id) ON DELETE CASCADE,
            UNIQUE(job_id, source_path)
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_library_assets_status "
        "ON library_assets(job_id, status)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_library_assets_source_hash "
        "ON library_assets(source_sha256, status)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_library_assets_pcm_hash "
        "ON library_assets(pcm_sha256, source_sample_rate, source_channels, status)"
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS qobuz_playlist_jobs (
            job_id TEXT PRIMARY KEY,
            playlist_id TEXT NOT NULL,
            playlist_name TEXT NOT NULL,
            status TEXT NOT NULL,
            total_tracks INTEGER NOT NULL,
            eligible_tracks INTEGER NOT NULL,
            excluded_tracks INTEGER NOT NULL DEFAULT 0,
            blocking_tracks INTEGER NOT NULL DEFAULT 0,
            completed_tracks INTEGER NOT NULL DEFAULT 0,
            failed_tracks INTEGER NOT NULL DEFAULT 0,
            current_original_index INTEGER,
            execution_json TEXT NOT NULL DEFAULT '[]',
            settings_journal_path TEXT,
            m3u8_path TEXT,
            error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS qobuz_playlist_tracks (
            job_id TEXT NOT NULL,
            original_index INTEGER NOT NULL,
            execution_index INTEGER,
            track_id TEXT NOT NULL,
            title TEXT NOT NULL,
            artist TEXT NOT NULL,
            album TEXT NOT NULL,
            duration_sec REAL NOT NULL,
            source_sample_rate INTEGER,
            source_bit_depth INTEGER,
            source_channels INTEGER,
            format_id INTEGER,
            excluded INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            archive_path TEXT,
            dj_path TEXT,
            requires_rerecord INTEGER NOT NULL DEFAULT 0,
            issues_json TEXT NOT NULL DEFAULT '[]',
            evidence_json TEXT NOT NULL DEFAULT '{}',
            error TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL,
            PRIMARY KEY(job_id, original_index),
            FOREIGN KEY(job_id) REFERENCES qobuz_playlist_jobs(job_id) ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_qobuz_playlist_tracks_status "
        "ON qobuz_playlist_tracks(job_id, status, execution_index)"
    )
    connection.execute(
        "INSERT OR REPLACE INTO schema_metadata(key, value) VALUES('version', ?)",
        (str(SCHEMA_VERSION),),
    )
    connection.commit()
    return connection


def _item_field(item, key):
    if isinstance(item, dict):
        return item.get(key)
    return getattr(item, key)


def create_qobuz_playlist_job(
    job_id,
    scan,
    execution_tracks,
    settings_journal_path=None,
    database_path=None,
):
    target = database_path or default_catalog_path()
    payload = scan.to_dict() if hasattr(scan, "to_dict") else dict(scan)
    tracks = list(getattr(scan, "tracks", payload.get("tracks", [])))
    execution = list(execution_tracks)
    execution_positions = {
        int(_item_field(track, "original_index")): index
        for index, track in enumerate(execution)
    }
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _connect(target) as connection:
        connection.execute(
            """
            INSERT INTO qobuz_playlist_jobs (
                job_id, playlist_id, playlist_name, status, total_tracks,
                eligible_tracks, excluded_tracks, blocking_tracks, execution_json,
                settings_journal_path, created_at, updated_at
            ) VALUES (?, ?, ?, 'ready', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(job_id),
                str(payload["playlist_id"]),
                str(payload["name"]),
                int(payload["total_tracks"]),
                int(payload["eligible_tracks"]),
                int(payload["excluded_tracks"]),
                int(payload["blocking_tracks"]),
                json.dumps(
                    [
                        {
                            "original_index": int(_item_field(item, "original_index")),
                            "track_id": str(_item_field(item, "track_id")),
                            "source_sample_rate": _item_field(
                                item, "source_sample_rate"
                            ),
                        }
                        for item in execution
                    ],
                    ensure_ascii=False,
                ),
                settings_journal_path,
                now,
                now,
            ),
        )
        for item in tracks:
            track = item.to_dict() if hasattr(item, "to_dict") else dict(item)
            original_index = int(track["original_index"])
            status = (
                "excluded"
                if track.get("excluded")
                else "blocked"
                if track.get("blocking")
                else "pending"
            )
            connection.execute(
                """
                INSERT INTO qobuz_playlist_tracks (
                    job_id, original_index, execution_index, track_id, title,
                    artist, album, duration_sec, source_sample_rate,
                    source_bit_depth, source_channels, format_id, excluded,
                    status, issues_json, evidence_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(job_id),
                    original_index,
                    execution_positions.get(original_index),
                    str(track["track_id"]),
                    str(track.get("name") or "Unknown"),
                    str(track.get("artist") or "Unknown"),
                    str(track.get("album") or "Unknown"),
                    float(track.get("duration") or 0.0),
                    track.get("source_sample_rate"),
                    track.get("source_bit_depth"),
                    track.get("source_channels"),
                    track.get("format_id"),
                    int(bool(track.get("excluded"))),
                    status,
                    json.dumps(track.get("issues") or [], ensure_ascii=False),
                    json.dumps(track, ensure_ascii=False),
                    now,
                ),
            )
    return str(job_id)


def update_qobuz_playlist_job(job_id, database_path=None, **fields):
    target = database_path or default_catalog_path()
    allowed = {
        "status",
        "completed_tracks",
        "failed_tracks",
        "current_original_index",
        "settings_journal_path",
        "m3u8_path",
        "error",
    }
    assignments = []
    values = []
    for key, value in fields.items():
        if key not in allowed:
            continue
        assignments.append(f"{key} = ?")
        values.append(value)
    if not assignments:
        return
    assignments.append("updated_at = ?")
    values.append(datetime.now(timezone.utc).isoformat(timespec="seconds"))
    values.append(str(job_id))
    with _connect(target) as connection:
        connection.execute(
            f"UPDATE qobuz_playlist_jobs SET {', '.join(assignments)} WHERE job_id = ?",
            values,
        )


def update_qobuz_playlist_track(
    job_id,
    original_index,
    database_path=None,
    **fields,
):
    target = database_path or default_catalog_path()
    allowed = {
        "status",
        "attempts",
        "archive_path",
        "dj_path",
        "requires_rerecord",
        "evidence_json",
        "error",
    }
    assignments = []
    values = []
    for key, value in fields.items():
        if key not in allowed:
            continue
        if key == "evidence_json" and not isinstance(value, str):
            value = json.dumps(value or {}, ensure_ascii=False)
        assignments.append(f"{key} = ?")
        values.append(value)
    if not assignments:
        return
    assignments.append("updated_at = ?")
    values.append(datetime.now(timezone.utc).isoformat(timespec="seconds"))
    values.extend((str(job_id), int(original_index)))
    with _connect(target) as connection:
        connection.execute(
            f"UPDATE qobuz_playlist_tracks SET {', '.join(assignments)} "
            "WHERE job_id = ? AND original_index = ?",
            values,
        )


def get_qobuz_playlist_job(job_id, database_path=None):
    target = database_path or default_catalog_path()
    with _connect(target) as connection:
        job_row = connection.execute(
            "SELECT * FROM qobuz_playlist_jobs WHERE job_id = ?", (str(job_id),)
        ).fetchone()
        track_rows = connection.execute(
            "SELECT * FROM qobuz_playlist_tracks WHERE job_id = ? "
            "ORDER BY original_index",
            (str(job_id),),
        ).fetchall()
    if job_row is None:
        return None
    job = dict(job_row)
    job["execution"] = json.loads(job.pop("execution_json") or "[]")
    job["tracks"] = []
    for row in track_rows:
        item = dict(row)
        item["issues"] = json.loads(item.pop("issues_json") or "[]")
        item["evidence"] = json.loads(item.pop("evidence_json") or "{}")
        job["tracks"].append(item)
    return job


def list_qobuz_playlist_jobs(limit=100, database_path=None):
    target = database_path or default_catalog_path()
    with _connect(target) as connection:
        rows = connection.execute(
            "SELECT * FROM qobuz_playlist_jobs ORDER BY created_at DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["execution"] = json.loads(item.pop("execution_json") or "[]")
        result.append(item)
    return result


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


def record_audio_export(
    source_wav_path,
    flac_path,
    status,
    export_role="archive",
    reason="",
    source_sample_rate=None,
    source_bit_depth=None,
    source_verified=None,
    output_sample_rate=None,
    output_bit_depth=24,
    src_engine=None,
    src_quality=None,
    src_phase=None,
    dither="NONE",
    dither_reason="",
    quantizer="ROUND_TO_NEAREST_EVEN",
    safety_gain_db=0.0,
    input_true_peak_dbtp=None,
    output_true_peak_dbtp=None,
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
                "SELECT recording_id FROM audio_exports WHERE source_wav_path = ? "
                "AND recording_id IS NOT NULL LIMIT 1",
                (source_path,),
            ).fetchone()
            recording_id = existing[0] if existing else None
        connection.execute(
            """
            INSERT INTO audio_exports (
                recording_id, source_wav_path, flac_path, export_role, status,
                reason, source_sample_rate, source_bit_depth, source_verified,
                output_sample_rate, output_bit_depth, src_engine, src_quality,
                src_phase, dither, dither_reason, quantizer, safety_gain_db,
                input_true_peak_dbtp, output_true_peak_dbtp, sample_peak_dbfs,
                artwork_embedded, source_bytes, flac_bytes, wav_deleted,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_wav_path, export_role) DO UPDATE SET
                recording_id=excluded.recording_id,
                flac_path=excluded.flac_path,
                status=excluded.status,
                reason=excluded.reason,
                source_sample_rate=excluded.source_sample_rate,
                source_bit_depth=excluded.source_bit_depth,
                source_verified=excluded.source_verified,
                output_sample_rate=excluded.output_sample_rate,
                output_bit_depth=excluded.output_bit_depth,
                src_engine=excluded.src_engine,
                src_quality=excluded.src_quality,
                src_phase=excluded.src_phase,
                dither=excluded.dither,
                dither_reason=excluded.dither_reason,
                quantizer=excluded.quantizer,
                safety_gain_db=excluded.safety_gain_db,
                input_true_peak_dbtp=excluded.input_true_peak_dbtp,
                output_true_peak_dbtp=excluded.output_true_peak_dbtp,
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
                str(export_role),
                str(status),
                str(reason or ""),
                source_sample_rate,
                source_bit_depth,
                None if source_verified is None else int(bool(source_verified)),
                output_sample_rate,
                int(output_bit_depth),
                src_engine,
                src_quality,
                src_phase,
                str(dither),
                str(dither_reason or ""),
                str(quantizer),
                float(safety_gain_db or 0.0),
                input_true_peak_dbtp,
                output_true_peak_dbtp,
                sample_peak_dbfs,
                None if artwork_embedded is None else int(bool(artwork_embedded)),
                source_bytes,
                flac_bytes,
                int(bool(wav_deleted)),
                now,
                now,
            ),
        )


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
    """Compatibility wrapper for pre-v4 callers and migrated history."""
    return record_audio_export(
        source_wav_path,
        flac_path,
        status,
        export_role="archive",
        reason=reason,
        dither="TPDF",
        dither_reason="Legacy FLAC export",
        sample_peak_dbfs=sample_peak_dbfs,
        artwork_embedded=artwork_embedded,
        source_bytes=source_bytes,
        flac_bytes=flac_bytes,
        wav_deleted=wav_deleted,
        database_path=database_path,
    )


def replace_recording_file(source_wav_path, flac_path, database_path=None):
    source_path = os.path.abspath(os.path.expanduser(source_wav_path))
    output_path = os.path.abspath(os.path.expanduser(flac_path))
    target = database_path or default_catalog_path()
    with _connect(target) as connection:
        row = connection.execute(
            "SELECT recording_id FROM audio_exports WHERE source_wav_path = ? "
            "AND export_role = 'archive'",
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
                "UPDATE audio_exports SET recording_id = ? WHERE source_wav_path = ?",
                (recording_id, source_path),
            )


def list_audio_exports(
    query="",
    status=None,
    export_role=None,
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
    if export_role:
        conditions.append("e.export_role = :export_role")
        parameters["export_role"] = str(export_role)
    sql = (
        "SELECT e.*, r.title, r.artist, r.album, r.provider, r.source_mode "
        "FROM audio_exports e LEFT JOIN recordings r ON r.id = e.recording_id WHERE "
        + " AND ".join(conditions)
        + " ORDER BY e.updated_at DESC, e.id DESC LIMIT :limit"
    )
    with _connect(target) as connection:
        rows = connection.execute(sql, parameters).fetchall()
    exports = []
    for row in rows:
        item = dict(row)
        item.setdefault("bit_depth", item.get("output_bit_depth"))
        item["source_exists"] = os.path.isfile(item["source_wav_path"])
        item["flac_exists"] = bool(
            item.get("flac_path") and os.path.isfile(item["flac_path"])
        )
        exports.append(item)
    return exports


def list_audio_exports_for_source(source_wav_path, database_path=None):
    source_path = os.path.abspath(os.path.expanduser(source_wav_path))
    target = database_path or default_catalog_path()
    with _connect(target) as connection:
        rows = connection.execute(
            "SELECT * FROM audio_exports WHERE source_wav_path = ? ORDER BY id",
            (source_path,),
        ).fetchall()
    return [dict(row) for row in rows]


def list_flac_exports(query="", status=None, limit=500, database_path=None):
    """Compatibility alias used by older UI and integrations."""
    return list_audio_exports(
        query=query,
        status=status,
        limit=limit,
        database_path=database_path,
    )


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


def create_library_job(
    job_id,
    source_root,
    destination_root,
    total_files=0,
    projected_bytes=0,
    settings=None,
    database_path=None,
):
    target = database_path or default_catalog_path()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _connect(target) as connection:
        connection.execute(
            """
            INSERT INTO library_jobs (
                job_id, source_root, destination_root, status, total_files,
                projected_bytes, settings_json, created_at, updated_at
            ) VALUES (?, ?, ?, 'scanned', ?, ?, ?, ?, ?)
            """,
            (
                str(job_id),
                os.path.abspath(os.path.expanduser(source_root)),
                os.path.abspath(os.path.expanduser(destination_root)),
                int(total_files),
                int(projected_bytes),
                json.dumps(settings or {}, ensure_ascii=False),
                now,
                now,
            ),
        )
    return str(job_id)


def update_library_job(job_id, status=None, database_path=None, **counts):
    target = database_path or default_catalog_path()
    allowed = {
        "total_files",
        "completed_files",
        "duplicate_files",
        "skipped_files",
        "failed_files",
        "projected_bytes",
    }
    assignments = []
    values = []
    if status is not None:
        assignments.append("status = ?")
        values.append(str(status))
    for key, value in counts.items():
        if key in allowed:
            assignments.append(f"{key} = ?")
            values.append(int(value))
    assignments.append("updated_at = ?")
    values.append(datetime.now(timezone.utc).isoformat(timespec="seconds"))
    values.append(str(job_id))
    with _connect(target) as connection:
        connection.execute(
            f"UPDATE library_jobs SET {', '.join(assignments)} WHERE job_id = ?",
            values,
        )


def upsert_library_asset(job_id, source_path, relative_path, database_path=None, **fields):
    target = database_path or default_catalog_path()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    allowed = {
        "output_path",
        "status",
        "reason",
        "source_size",
        "source_mtime_ns",
        "source_sha256",
        "pcm_sha256",
        "source_codec",
        "source_lossless",
        "source_sample_rate",
        "source_bit_depth",
        "source_channels",
        "duration_sec",
        "output_bytes",
        "dither",
        "src_engine",
        "safety_gain_db",
        "artwork_embedded",
        "processing_json",
    }
    payload = {key: value for key, value in fields.items() if key in allowed}
    payload.setdefault("status", "queued")
    if isinstance(payload.get("processing_json"), (dict, list)):
        payload["processing_json"] = json.dumps(
            payload["processing_json"], ensure_ascii=False
        )
    if payload.get("source_lossless") is not None:
        payload["source_lossless"] = int(bool(payload["source_lossless"]))
    if payload.get("artwork_embedded") is not None:
        payload["artwork_embedded"] = int(bool(payload["artwork_embedded"]))
    columns = ["job_id", "source_path", "relative_path", *payload, "created_at", "updated_at"]
    values = [
        str(job_id),
        os.path.abspath(os.path.expanduser(source_path)),
        str(relative_path),
        *payload.values(),
        now,
        now,
    ]
    updates = [
        f"{column}=excluded.{column}"
        for column in ["relative_path", *payload, "updated_at"]
    ]
    placeholders = ", ".join("?" for _ in columns)
    with _connect(target) as connection:
        connection.execute(
            f"""
            INSERT INTO library_assets ({', '.join(columns)})
            VALUES ({placeholders})
            ON CONFLICT(job_id, source_path) DO UPDATE SET {', '.join(updates)}
            """,
            values,
        )


def find_library_duplicate(
    source_sha256=None,
    pcm_sha256=None,
    source_sample_rate=None,
    source_channels=None,
    exclude_source_path=None,
    database_path=None,
):
    target = database_path or default_catalog_path()
    conditions = ["status = 'complete'", "output_path IS NOT NULL"]
    parameters = []
    hashes = []
    if source_sha256:
        hashes.append("source_sha256 = ?")
        parameters.append(str(source_sha256))
    if pcm_sha256 and source_sample_rate and source_channels:
        hashes.append(
            "(pcm_sha256 = ? AND source_sample_rate = ? AND source_channels = ?)"
        )
        parameters.extend(
            [str(pcm_sha256), int(source_sample_rate), int(source_channels)]
        )
    if not hashes:
        return None
    conditions.append("(" + " OR ".join(hashes) + ")")
    if exclude_source_path:
        conditions.append("source_path != ?")
        parameters.append(os.path.abspath(os.path.expanduser(exclude_source_path)))
    with _connect(target) as connection:
        row = connection.execute(
            "SELECT * FROM library_assets WHERE "
            + " AND ".join(conditions)
            + " ORDER BY id LIMIT 1",
            parameters,
        ).fetchone()
    return dict(row) if row else None


def list_library_jobs(limit=50, database_path=None):
    target = database_path or default_catalog_path()
    with _connect(target) as connection:
        rows = connection.execute(
            "SELECT * FROM library_jobs ORDER BY updated_at DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["settings"] = json.loads(item.pop("settings_json") or "{}")
        result.append(item)
    return result


def list_library_assets(job_id, statuses=None, limit=5000, database_path=None):
    target = database_path or default_catalog_path()
    parameters = [str(job_id)]
    condition = "job_id = ?"
    if statuses:
        values = [str(item) for item in statuses]
        condition += " AND status IN (" + ",".join("?" for _ in values) + ")"
        parameters.extend(values)
    parameters.append(int(limit))
    with _connect(target) as connection:
        rows = connection.execute(
            f"SELECT * FROM library_assets WHERE {condition} ORDER BY id LIMIT ?",
            parameters,
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["processing"] = json.loads(item.pop("processing_json") or "{}")
        item["output_exists"] = bool(
            item.get("output_path") and os.path.isfile(item["output_path"])
        )
        result.append(item)
    return result


def recover_interrupted_library_jobs(database_path=None):
    target = database_path or default_catalog_path()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _connect(target) as connection:
        connection.execute(
            "UPDATE library_assets SET status='queued', reason='前回の変換中断から再開', "
            "updated_at=? WHERE status='converting'",
            (now,),
        )
        connection.execute(
            "UPDATE library_jobs SET status='paused', updated_at=? "
            "WHERE status='running'",
            (now,),
        )
