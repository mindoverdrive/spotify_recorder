import json
import os
import queue
import shutil
import tempfile
import threading
from datetime import datetime, timezone

import numpy as np


DEFAULT_CACHE_DIR = os.path.expanduser("~/Library/Caches/HiResRecorder/Sessions")
DEFAULT_RESERVE_BYTES = 5 * 1024**3


def capture_blocksize(sample_rate):
    rate = int(sample_rate)
    if rate <= 48000:
        return 2048
    if rate <= 96000:
        return 4096
    return 8192


def estimated_capture_bytes(sample_rate, minutes, channels=2):
    return int(float(sample_rate) * float(minutes) * 60.0 * int(channels) * 4)


def check_capture_disk_space(
    cache_dir,
    output_dir,
    sample_rate,
    minutes,
    reserve_bytes=DEFAULT_RESERVE_BYTES,
):
    cache_dir = os.path.abspath(os.path.expanduser(cache_dir))
    output_dir = os.path.abspath(os.path.expanduser(output_dir))
    os.makedirs(cache_dir, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)
    required = estimated_capture_bytes(sample_rate, minutes)
    cache_free = shutil.disk_usage(cache_dir).free
    output_free = shutil.disk_usage(output_dir).free
    same_volume = os.stat(cache_dir).st_dev == os.stat(output_dir).st_dev
    if same_volume:
        required_on_volume = required * 2 + int(reserve_bytes)
        ok = cache_free >= required_on_volume
    else:
        required_on_volume = required + int(reserve_bytes)
        ok = cache_free >= required_on_volume and output_free >= required_on_volume
    return {
        "ok": ok,
        "estimated_audio_bytes": required,
        "required_bytes": required_on_volume,
        "cache_free_bytes": cache_free,
        "output_free_bytes": output_free,
        "same_volume": same_volume,
    }


class SpoolAudio:
    def __init__(self, raw_path, metadata_path, sample_rate, channels, frames):
        self.raw_path = raw_path
        self.metadata_path = metadata_path
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)
        self.frames = int(frames)
        self._array = np.memmap(
            self.raw_path,
            dtype="<f4",
            mode="r",
            shape=(self.frames, self.channels),
        )

    @property
    def shape(self):
        return self._array.shape

    @property
    def dtype(self):
        return self._array.dtype

    def __len__(self):
        return self.frames

    def __getitem__(self, item):
        return self._array[item]

    def close(self, delete=False):
        mmap = getattr(self._array, "_mmap", None)
        if mmap is not None:
            mmap.close()
        if delete:
            for path in (self.raw_path, self.metadata_path):
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass


class CaptureSpool:
    def __init__(
        self,
        sample_rate,
        channels=2,
        blocksize=None,
        queue_seconds=8.0,
        cache_dir=DEFAULT_CACHE_DIR,
    ):
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)
        self.blocksize = int(blocksize or capture_blocksize(sample_rate))
        max_blocks = max(2, int(float(queue_seconds) * self.sample_rate / self.blocksize))
        self._queue = queue.Queue(maxsize=max_blocks)
        self.cache_dir = os.path.abspath(os.path.expanduser(cache_dir))
        self.raw_path = None
        self.metadata_path = None
        self.frames_written = 0
        self.error = None
        self.overflowed = False
        self._thread = None
        self._handle = None
        self._lock = threading.Lock()
        self._started = False
        self._stopped = False

    def _write_metadata(self, state):
        payload = {
            "state": state,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "frames": self.frames_written,
            "raw_path": self.raw_path,
            "error": self.error,
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        temporary = f"{self.metadata_path}.tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(temporary, self.metadata_path)

    def start(self, pre_roll=None):
        if self._started:
            return
        os.makedirs(self.cache_dir, exist_ok=True)
        descriptor, self.raw_path = tempfile.mkstemp(
            prefix="capture-", suffix=".f32", dir=self.cache_dir
        )
        self.metadata_path = f"{self.raw_path}.json"
        self._handle = os.fdopen(descriptor, "wb", buffering=1024 * 1024)
        self._started = True
        self._write_metadata("recording")
        self._thread = threading.Thread(target=self._writer, daemon=True)
        self._thread.start()
        for block in pre_roll or []:
            if not self.try_write(block):
                raise RuntimeError(self.error or "プリロールをスプールへ書き込めません")

    def try_write(self, samples):
        if not self._started or self._stopped or self.error:
            return False
        block = np.asarray(samples, dtype="<f4")
        if block.ndim != 2 or block.shape[1] != self.channels:
            self.error = f"音声ブロック形状が不正です: {block.shape}"
            return False
        try:
            self._queue.put_nowait(np.ascontiguousarray(block))
            return True
        except queue.Full:
            self.overflowed = True
            self.error = "8秒録音キューがオーバーフローしました"
            return False

    def _writer(self):
        try:
            while True:
                block = self._queue.get()
                try:
                    if block is None:
                        break
                    if not np.isfinite(block).all():
                        self.error = "録音データにNaNまたはInfを検出しました"
                        continue
                    self._handle.write(block.astype("<f4", copy=False).tobytes(order="C"))
                    with self._lock:
                        self.frames_written += len(block)
                finally:
                    self._queue.task_done()
        except OSError as exc:
            self.error = f"録音スプール書込エラー: {exc}"
        finally:
            if self._handle:
                self._handle.flush()
                os.fsync(self._handle.fileno())
                self._handle.close()
                self._handle = None

    def stop(self):
        if not self._started:
            raise RuntimeError("録音スプールが開始されていません")
        if self._stopped:
            raise RuntimeError("録音スプールは既に停止しています")
        self._stopped = True
        self._queue.put(None)
        if self._thread:
            self._thread.join()
        state = "failed" if self.error else "complete"
        self._write_metadata(state)
        if self.frames_written <= 0:
            self.discard()
            raise RuntimeError(self.error or "録音データがありません")
        return SpoolAudio(
            self.raw_path,
            self.metadata_path,
            self.sample_rate,
            self.channels,
            self.frames_written,
        )

    def discard(self):
        self._stopped = True
        if self._thread and self._thread.is_alive():
            self._queue.put(None)
            self._thread.join()
        if self._handle:
            self._handle.close()
            self._handle = None
        for path in (self.raw_path, self.metadata_path):
            if path:
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass


def list_recoverable_sessions(cache_dir=DEFAULT_CACHE_DIR):
    base = os.path.abspath(os.path.expanduser(cache_dir))
    sessions = []
    for path in sorted(glob_json(base)):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError):
            continue
        raw_path = payload.get("raw_path")
        if raw_path and os.path.isfile(raw_path):
            channels = max(1, int(payload.get("channels") or 2))
            payload["frames"] = os.path.getsize(raw_path) // (channels * 4)
            payload["metadata_path"] = path
            sessions.append(payload)
    return sessions


def glob_json(base):
    if not os.path.isdir(base):
        return []
    return [os.path.join(base, name) for name in os.listdir(base) if name.endswith(".json")]
