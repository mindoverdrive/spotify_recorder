import json
import os
import tempfile
import time
from datetime import datetime, timezone

from coreaudio_devices import (
    capture_coreaudio_state,
    restore_coreaudio_state,
    set_default_audio_device,
)


def default_journal_dir():
    return os.path.expanduser(
        "~/Library/Application Support/HiResRecorder/SettingsTransactions"
    )


def _write_json_atomic(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=os.path.dirname(path),
        prefix=".settings-",
        suffix=".json",
        delete=False,
    )
    temporary = handle.name
    try:
        with handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        try:
            os.remove(temporary)
        except FileNotFoundError:
            pass


def read_settings_journal(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


class CaptureSettingsTransaction:
    def __init__(
        self,
        job_id,
        capture_device_id,
        qobuz_automation=None,
        journal_dir=None,
        coreaudio_capture=None,
        coreaudio_restore=None,
    ):
        self.job_id = str(job_id)
        self.capture_device_id = int(capture_device_id)
        self.qobuz_automation = qobuz_automation
        self.journal_dir = journal_dir or default_journal_dir()
        self.coreaudio_capture = coreaudio_capture or capture_coreaudio_state
        self.coreaudio_restore = coreaudio_restore or restore_coreaudio_state
        self.journal_path = os.path.join(self.journal_dir, f"{self.job_id}.json")
        self.payload = None

    def begin(self, qobuz_output_device_name=None):
        if self.payload and self.payload.get("active"):
            return self.journal_path
        coreaudio_state = self.coreaudio_capture(self.capture_device_id)
        qobuz_state = (
            self.qobuz_automation.snapshot_state()
            if self.qobuz_automation is not None
            else None
        )
        self.payload = {
            "version": 1,
            "job_id": self.job_id,
            "parent_pid": os.getpid(),
            "active": True,
            "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "restored_at": None,
            "coreaudio": coreaudio_state,
            "qobuz": qobuz_state,
            "restore_errors": [],
        }
        _write_json_atomic(self.journal_path, self.payload)
        try:
            if self.qobuz_automation is not None:
                self.qobuz_automation.prepare_job(qobuz_output_device_name)
            set_default_audio_device("input", self.capture_device_id)
        except Exception:
            self.restore()
            raise
        return self.journal_path

    def restore(self):
        if self.payload is None and os.path.isfile(self.journal_path):
            self.payload = read_settings_journal(self.journal_path)
        if not self.payload or not self.payload.get("active"):
            return {"restored": True, "errors": []}
        errors = []
        coreaudio_result = self.coreaudio_restore(self.payload.get("coreaudio") or {})
        errors.extend(coreaudio_result.get("errors") or [])
        if self.qobuz_automation is not None and self.payload.get("qobuz"):
            try:
                qobuz_result = self.qobuz_automation.restore_state(
                    self.payload.get("qobuz") or {}
                )
                errors.extend(qobuz_result.get("errors") or [])
            except Exception as exc:
                errors.append(str(exc))
        self.payload["active"] = False
        self.payload["restored_at"] = datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        )
        self.payload["restore_errors"] = errors
        _write_json_atomic(self.journal_path, self.payload)
        return {"restored": not errors, "errors": errors}


def restore_from_journal(path, qobuz_automation=None):
    payload = read_settings_journal(path)
    if not payload.get("active"):
        return {"restored": True, "errors": []}
    errors = []
    result = restore_coreaudio_state(payload.get("coreaudio") or {})
    errors.extend(result.get("errors") or [])
    if qobuz_automation is not None and payload.get("qobuz"):
        try:
            result = qobuz_automation.restore_state(payload["qobuz"])
            errors.extend(result.get("errors") or [])
        except Exception as exc:
            errors.append(str(exc))
    payload["active"] = False
    payload["restored_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    payload["restore_errors"] = errors
    _write_json_atomic(path, payload)
    return {"restored": not errors, "errors": errors}


def run_restore_watchdog(parent_pid, journal_path, poll_interval=1.0):
    parent_pid = int(parent_pid)
    while True:
        if os.path.isfile(journal_path):
            try:
                if not read_settings_journal(journal_path).get("active"):
                    return {"restored": True, "errors": []}
            except (OSError, json.JSONDecodeError):
                pass
        try:
            os.kill(parent_pid, 0)
        except ProcessLookupError:
            break
        except PermissionError:
            break
        time.sleep(max(0.1, float(poll_interval)))
    if os.path.isfile(journal_path):
        return restore_from_journal(journal_path)
    return {"restored": True, "errors": []}
