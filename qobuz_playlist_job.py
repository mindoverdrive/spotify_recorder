import os
import uuid

from qobuz_playlist import write_playlist_m3u8
from recording_catalog import (
    create_qobuz_playlist_job,
    update_qobuz_playlist_job,
    update_qobuz_playlist_track,
)
from spotify_recorder_services import safe_filename


MAX_TRACK_RETRIES = 2


def capture_result_paths(result):
    items = list((result or {}).get("items") or [])
    if len(items) != 1:
        return None, None
    exports = items[0].get("exports") or {}
    archive = (exports.get("archive") or {}).get("flac_path")
    dj = (exports.get("dj") or {}).get("flac_path")
    return archive, dj


def capture_result_is_acceptable(result):
    if (result or {}).get("error"):
        return False, str(result["error"])
    if not result or result.get("failed") or result.get("saved") != 1:
        return False, "録音保存結果が1曲分ではありません"
    item = result["items"][0]
    analysis = item.get("analysis") or {}
    if analysis.get("warnings"):
        return False, "音声解析で重大な疑義を検出しました"
    audit = item.get("capture_audit") or {}
    if audit and not audit.get("quality_gate_pass", False):
        return False, "録音監査が不合格です"
    exports = item.get("exports") or {}
    if exports.get("status") != "complete":
        return False, exports.get("reason") or "Archive/DJ出力が完了していません"
    archive, dj = capture_result_paths(result)
    if not archive or not dj or not os.path.isfile(archive) or not os.path.isfile(dj):
        return False, "検証済みArchive/DJファイルを確認できません"
    return True, ""


class QobuzPlaylistJob:
    def __init__(
        self,
        scan,
        database_path,
        save_dir,
        job_id=None,
        max_retries=MAX_TRACK_RETRIES,
    ):
        if not scan.can_start:
            raise ValueError("未完了または不適合の曲があるためジョブを開始できません")
        self.scan = scan
        self.database_path = database_path
        self.save_dir = os.path.abspath(os.path.expanduser(save_dir))
        self.job_id = str(job_id or uuid.uuid4())
        self.max_retries = max(0, int(max_retries))
        self.execution = list(scan.execution_tracks())
        self.cursor = 0
        self.attempts = {track.original_index: 0 for track in self.execution}
        self.completed = {}
        self.failed = {}
        self.cancelled = False
        self.created = False

    @property
    def current_track(self):
        if self.cursor >= len(self.execution):
            return None
        return self.execution[self.cursor]

    @property
    def done(self):
        return self.cancelled or self.current_track is None

    def create(self, settings_journal_path=None):
        create_qobuz_playlist_job(
            self.job_id,
            self.scan,
            self.execution,
            settings_journal_path=settings_journal_path,
            database_path=self.database_path,
        )
        self.created = True
        return self.job_id

    def start(self, settings_journal_path=None):
        if not self.created:
            self.create(settings_journal_path=settings_journal_path)
        update_qobuz_playlist_job(
            self.job_id,
            status="running",
            settings_journal_path=settings_journal_path,
            database_path=self.database_path,
        )

    def begin_attempt(self):
        track = self.current_track
        if track is None:
            return None
        index = track.original_index
        self.attempts[index] += 1
        update_qobuz_playlist_job(
            self.job_id,
            status="running",
            current_original_index=index,
            database_path=self.database_path,
        )
        update_qobuz_playlist_track(
            self.job_id,
            index,
            status="recording",
            attempts=self.attempts[index],
            error="",
            database_path=self.database_path,
        )
        return track

    def finish_attempt(self, result):
        track = self.current_track
        if track is None:
            raise RuntimeError("処理対象のQobuzトラックがありません")
        accepted, reason = capture_result_is_acceptable(result)
        archive, dj = capture_result_paths(result)
        index = track.original_index
        if accepted:
            self.completed[index] = {"archive": archive, "dj": dj}
            update_qobuz_playlist_track(
                self.job_id,
                index,
                status="complete",
                archive_path=archive,
                dj_path=dj,
                requires_rerecord=0,
                evidence_json=(result["items"][0].get("capture_audit") or {}),
                error="",
                database_path=self.database_path,
            )
            self.cursor += 1
        elif self.attempts[index] <= self.max_retries:
            update_qobuz_playlist_track(
                self.job_id,
                index,
                status="retry_pending",
                requires_rerecord=1,
                error=reason,
                database_path=self.database_path,
            )
        else:
            self.failed[index] = reason
            update_qobuz_playlist_track(
                self.job_id,
                index,
                status="failed",
                requires_rerecord=1,
                error=reason,
                database_path=self.database_path,
            )
            self.cursor += 1
        update_qobuz_playlist_job(
            self.job_id,
            completed_tracks=len(self.completed),
            failed_tracks=len(self.failed),
            database_path=self.database_path,
        )
        return accepted, reason

    def cancel(self, reason="ユーザーにより中止されました"):
        self.cancelled = True
        update_qobuz_playlist_job(
            self.job_id,
            status="cancelled",
            error=reason,
            database_path=self.database_path,
        )

    def fail(self, reason):
        update_qobuz_playlist_job(
            self.job_id,
            status="failed",
            error=str(reason),
            database_path=self.database_path,
        )

    def finalize(self):
        if self.cancelled:
            return None
        output_paths = {
            track.track_id: self.completed.get(track.original_index, {}).get("dj")
            for track in self.execution
        }
        m3u8 = write_playlist_m3u8(
            os.path.join(
                self.save_dir,
                f"{safe_filename(self.scan.name)} - DJ 24-48.m3u8",
            ),
            self.scan,
            output_paths,
        )
        status = "complete" if not self.failed else "complete_with_failures"
        update_qobuz_playlist_job(
            self.job_id,
            status=status,
            current_original_index=None,
            m3u8_path=m3u8,
            error="" if not self.failed else f"{len(self.failed)}曲失敗",
            database_path=self.database_path,
        )
        return m3u8
