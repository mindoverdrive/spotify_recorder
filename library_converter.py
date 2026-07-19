import base64
import hashlib
import os
import shutil
import tempfile
import threading
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

import av
import numpy as np
import soundfile as sf
from mutagen import File as MutagenFile
from mutagen.flac import FLAC, Picture

from recording_catalog import (
    create_library_job,
    find_library_duplicate,
    list_library_assets,
    recover_interrupted_library_jobs,
    update_library_job,
    upsert_library_asset,
)
from spotify_recorder_services import (
    ANALYSIS_CHUNK_FRAMES,
    DITHER_NONE,
    DITHER_TPDF,
    DJ_SAMPLE_RATE,
    SRC_ENGINE,
    SRC_PHASE,
    SRC_QUALITY,
    _analyze_audio_file,
    _resample_to_float64_wav,
    write_pcm24_flac,
)


AUDIO_EXTENSIONS = {
    ".wav",
    ".wave",
    ".aif",
    ".aiff",
    ".flac",
    ".alac",
    ".m4a",
    ".aac",
    ".mp3",
    ".ogg",
    ".oga",
    ".opus",
    ".mp4",
    ".dsf",
    ".dff",
}
DSD_EXTENSIONS = {".dsf", ".dff"}
LOSSY_CODECS = {
    "aac",
    "aac_latm",
    "mp2",
    "mp3",
    "mp3float",
    "opus",
    "vorbis",
}
LOSSLESS_CODECS = {
    "alac",
    "flac",
    "pcm_s16be",
    "pcm_s16le",
    "pcm_s24be",
    "pcm_s24le",
    "pcm_s32be",
    "pcm_s32le",
    "pcm_f32be",
    "pcm_f32le",
    "pcm_f64be",
    "pcm_f64le",
}
METADATA_FIELDS = (
    "title",
    "artist",
    "album",
    "albumartist",
    "date",
    "genre",
    "tracknumber",
    "discnumber",
    "comment",
    "isrc",
    "bpm",
    "initialkey",
    "composer",
    "label",
)


class LibraryConversionRejected(ValueError):
    pass


@dataclass(frozen=True)
class LibraryAudioProbe:
    codec: str
    lossless: bool | None
    sample_rate: int
    bit_depth: int | None
    bit_depth_kind: str
    channels: int
    duration_sec: float
    decoder: str

    def to_dict(self):
        return asdict(self)


def default_library_cache_dir():
    return os.path.expanduser("~/Library/Caches/HiResRecorder/LibraryImports")


def library_codec_diagnostics():
    required_decoders = ("aac", "alac", "flac", "mp3", "vorbis", "opus")
    available = []
    missing = []
    for codec_name in required_decoders:
        try:
            av.codec.Codec(codec_name, "r")
            available.append(codec_name)
        except Exception:
            missing.append(codec_name)
    return {
        "ok": not missing,
        "pyav_version": av.__version__,
        "available_decoders": available,
        "missing_decoders": missing,
        "ffmpeg_libraries": {
            name: ".".join(str(part) for part in version)
            for name, version in av.library_versions.items()
        },
    }


def default_library_destination():
    go_ssd = "/Volumes/Go SSD"
    if os.path.isdir(go_ssd):
        return os.path.join(go_ssd, "DJ Library 24-48")
    return os.path.expanduser("~/Music/DJ Library 24-48")


def _subtype_depth(subtype):
    value = str(subtype or "").upper()
    for depth in (8, 16, 20, 24, 32, 64):
        if str(depth) in value:
            return depth
    return None


def _soundfile_probe(path):
    info = sf.info(path)
    subtype = str(info.subtype or "")
    format_name = str(info.format or "").lower()
    codec = subtype.lower() or format_name
    extension = Path(path).suffix.lower()
    lossy = (
        extension in {".mp3", ".aac", ".m4a", ".ogg", ".oga", ".opus"}
        or any(token in subtype.upper() for token in ("MPEG", "VORBIS", "OPUS"))
    )
    float_source = subtype.upper() in {"FLOAT", "DOUBLE"}
    bit_depth = _subtype_depth(subtype)
    if float_source:
        kind = "float"
    elif bit_depth:
        kind = "integer"
    else:
        kind = "unknown"
    return LibraryAudioProbe(
        codec=codec,
        lossless=False if lossy else True,
        sample_rate=int(info.samplerate),
        bit_depth=None if lossy else bit_depth,
        bit_depth_kind="lossy" if lossy else kind,
        channels=int(info.channels),
        duration_sec=(float(info.frames) / float(info.samplerate)) if info.samplerate else 0.0,
        decoder="soundfile",
    )


def _av_channels(context):
    channels = int(getattr(context, "channels", 0) or 0)
    layout = getattr(context, "layout", None)
    if not channels and layout is not None:
        channels = int(getattr(layout, "nb_channels", 0) or len(getattr(layout, "channels", ())))
    return channels


def _av_probe(path):
    with av.open(path, mode="r") as container:
        streams = [stream for stream in container.streams if stream.type == "audio"]
        if not streams:
            raise LibraryConversionRejected("音声ストリームがありません")
        if len(streams) != 1:
            raise LibraryConversionRejected("Stem/複数音声ストリームは自動変換しません")
        stream = streams[0]
        context = stream.codec_context
        codec = str(getattr(context, "name", None) or getattr(context.codec, "name", "unknown"))
        rate = int(getattr(context, "sample_rate", 0) or getattr(stream, "rate", 0) or 0)
        channels = _av_channels(context)
        raw_depth = int(getattr(context, "bits_per_raw_sample", 0) or 0)
        coded_depth = int(getattr(context, "bits_per_coded_sample", 0) or 0)
        bit_depth = raw_depth or coded_depth or None
        sample_format = str(getattr(context, "format", "") or "").lower()
        is_float = "flt" in sample_format or "dbl" in sample_format
        lossless = True if codec in LOSSLESS_CODECS else False if codec in LOSSY_CODECS else None
        if bit_depth is None and lossless is True:
            try:
                metadata = MutagenFile(path, easy=False)
                bit_depth = int(getattr(metadata.info, "bits_per_sample", 0) or 0) or None
            except Exception:
                bit_depth = None
        if bit_depth is None and codec.startswith("pcm_"):
            bit_depth = _subtype_depth(codec)
        if stream.duration is not None and stream.time_base is not None:
            duration = float(stream.duration * stream.time_base)
        elif container.duration is not None:
            duration = float(container.duration) / float(av.time_base)
        else:
            duration = 0.0
    return LibraryAudioProbe(
        codec=codec,
        lossless=lossless,
        sample_rate=rate,
        bit_depth=None if lossless is False else bit_depth,
        bit_depth_kind="lossy" if lossless is False else "float" if is_float else "integer" if bit_depth else "unknown",
        channels=channels,
        duration_sec=duration,
        decoder="pyav",
    )


def probe_library_audio(path):
    source = os.path.abspath(os.path.expanduser(path))
    extension = Path(source).suffix.lower()
    if extension not in AUDIO_EXTENSIONS:
        raise LibraryConversionRejected("未対応の拡張子です")
    if extension in DSD_EXTENSIONS:
        raise LibraryConversionRejected("DSDは暗黙PCM変換せず隔離します")
    if source.lower().endswith(".stem.mp4"):
        raise LibraryConversionRejected("NI Stemは構造を保持できないため変換しません")
    try:
        probe = _soundfile_probe(source)
    except Exception:
        probe = _av_probe(source)
    if probe.sample_rate <= 0 or probe.sample_rate > 384000:
        raise LibraryConversionRejected(
            f"未対応サンプルレートです: {probe.sample_rate}Hz"
        )
    if probe.channels not in {1, 2}:
        raise LibraryConversionRejected(
            f"{probe.channels}ch音源は自動ダウンミックスしません"
        )
    return probe


def _metadata_value(tags, key):
    if tags is None:
        return None
    value = tags.get(key)
    if value is None:
        value = tags.get(key.upper())
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    if hasattr(value, "text"):
        value = value.text[0] if value.text else None
    return None if value is None else str(value)


def read_library_metadata(path):
    source = os.path.abspath(path)
    result = {
        "name": os.path.splitext(os.path.basename(source))[0],
        "title": os.path.splitext(os.path.basename(source))[0],
        "artist": "Unknown",
        "album": "Unknown",
        "artwork_bytes": None,
    }
    try:
        easy = MutagenFile(source, easy=True)
        tags = getattr(easy, "tags", None)
        for field in METADATA_FIELDS:
            value = _metadata_value(tags, field)
            if value:
                result[field] = value
        if result.get("title"):
            result["name"] = result["title"]
    except Exception:
        pass
    try:
        full = MutagenFile(source, easy=False)
        if isinstance(full, FLAC) and full.pictures:
            result["artwork_bytes"] = bytes(full.pictures[0].data)
        elif getattr(full, "tags", None) is not None:
            tags = full.tags
            if hasattr(tags, "getall"):
                pictures = tags.getall("APIC")
                if pictures:
                    result["artwork_bytes"] = bytes(pictures[0].data)
            if result["artwork_bytes"] is None:
                covers = tags.get("covr") if hasattr(tags, "get") else None
                if covers:
                    result["artwork_bytes"] = bytes(covers[0])
            if result["artwork_bytes"] is None:
                encoded = tags.get("metadata_block_picture") if hasattr(tags, "get") else None
                if encoded:
                    raw = encoded[0] if isinstance(encoded, list) else encoded
                    result["artwork_bytes"] = Picture(base64.b64decode(raw)).data
    except Exception:
        pass
    return result


def _stage_source(source_path, target_path):
    before = os.stat(source_path)
    digest = hashlib.sha256()
    with open(source_path, "rb") as source, open(target_path, "wb") as target:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            target.write(chunk)
        target.flush()
        os.fsync(target.fileno())
    after = os.stat(source_path)
    if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
        raise RuntimeError("コピー中に入力ファイルが変更されました")
    if os.path.getsize(target_path) != before.st_size:
        raise RuntimeError("ローカルステージのサイズが入力と一致しません")
    return digest.hexdigest(), before


def _write_array(output, array, channels):
    data = np.asarray(array)
    if data.ndim == 1:
        data = data.reshape(-1, channels)
    elif data.ndim == 2 and data.shape[0] == channels:
        data = data.T
    if data.ndim != 2 or data.shape[1] != channels:
        raise RuntimeError(f"PyAV復号形状が不正です: {data.shape}")
    data = np.asarray(data, dtype=np.float64)
    if not np.isfinite(data).all():
        raise ValueError("復号音声にNaNまたはInfが含まれています")
    output.write(data)


def _decode_with_pyav(source_path, target_path, probe):
    with av.open(source_path, mode="r") as container:
        streams = [stream for stream in container.streams if stream.type == "audio"]
        if len(streams) != 1:
            raise LibraryConversionRejected("Stem/複数音声ストリームは変換しません")
        stream = streams[0]
        layout = "mono" if probe.channels == 1 else "stereo"
        resampler = av.audio.resampler.AudioResampler(
            format="dblp",
            layout=layout,
            rate=probe.sample_rate,
        )
        with sf.SoundFile(
            target_path,
            mode="w",
            samplerate=probe.sample_rate,
            channels=probe.channels,
            format="WAV",
            subtype="DOUBLE",
        ) as output:
            for frame in container.decode(stream):
                for converted in resampler.resample(frame):
                    _write_array(output, converted.to_ndarray(), probe.channels)
            for converted in resampler.resample(None):
                _write_array(output, converted.to_ndarray(), probe.channels)


def decode_to_float64_wav(source_path, target_path, probe):
    if probe.decoder == "soundfile":
        with sf.SoundFile(source_path, mode="r") as source, sf.SoundFile(
            target_path,
            mode="w",
            samplerate=probe.sample_rate,
            channels=probe.channels,
            format="WAV",
            subtype="DOUBLE",
        ) as output:
            while True:
                chunk = source.read(
                    frames=ANALYSIS_CHUNK_FRAMES,
                    dtype="float64",
                    always_2d=True,
                )
                if not len(chunk):
                    break
                if not np.isfinite(chunk).all():
                    raise ValueError("復号音声にNaNまたはInfが含まれています")
                output.write(chunk)
    else:
        _decode_with_pyav(source_path, target_path, probe)
    info = sf.info(target_path)
    if int(info.samplerate) != probe.sample_rate or int(info.channels) != probe.channels:
        raise RuntimeError("復号WAVのレートまたはチャンネル数が変化しました")
    return int(info.frames)


def hash_decoded_pcm(wav_path):
    info = sf.info(wav_path)
    digest = hashlib.sha256()
    digest.update(f"f64le:{info.samplerate}:{info.channels}:".encode("ascii"))
    with sf.SoundFile(wav_path, mode="r") as source:
        while True:
            chunk = source.read(
                frames=ANALYSIS_CHUNK_FRAMES,
                dtype="float64",
                always_2d=True,
            )
            if not len(chunk):
                break
            digest.update(np.asarray(chunk, dtype="<f8", order="C").tobytes())
    return digest.hexdigest()


def _destination_for(relative_path, destination_root, source_sha256):
    relative = Path(relative_path)
    output = Path(destination_root) / relative.with_suffix(".flac")
    output.parent.mkdir(parents=True, exist_ok=True)
    existing_names = {item.name.casefold() for item in output.parent.iterdir()}
    if output.name.casefold() in existing_names and not output.is_file():
        output = output.with_name(f"{output.stem}__{source_sha256[:8]}.flac")
    elif output.exists():
        output = output.with_name(f"{output.stem}__{source_sha256[:8]}.flac")
    return str(output)


def _dither_policy(probe, dsp_applied):
    if probe.bit_depth_kind in {"lossy", "unknown"}:
        return DITHER_NONE, "ソースbit深度未定義または未検証: 保守的な無ディザ方針"
    if probe.bit_depth_kind == "float":
        return DITHER_TPDF, "float PCMから最終24-bit量子化するためTPDFを1回適用"
    if probe.bit_depth is not None and probe.bit_depth <= 16:
        return DITHER_NONE, "16-bit以下のソース: SRC・Gain後もディザ禁止"
    if dsp_applied:
        return DITHER_TPDF, "20/24-bitソース: DSP後の最終量子化でTPDFを1回適用"
    return DITHER_NONE, "20/24-bitソース: SRC・Gainなしのためディザ不要"


def _validate_roots(source_root, destination_root):
    source = os.path.realpath(os.path.abspath(os.path.expanduser(source_root)))
    destination = os.path.realpath(os.path.abspath(os.path.expanduser(destination_root)))
    if not os.path.isdir(source):
        raise ValueError("入力フォルダが存在しません")
    if source == destination:
        raise ValueError("入力と出力に同じフォルダは指定できません")
    try:
        common = os.path.commonpath((source, destination))
    except ValueError:
        common = ""
    if common == source:
        raise ValueError("出力フォルダを入力フォルダの内側には配置できません")
    return source, destination


def scan_library(source_root, destination_root, database_path=None, job_id=None):
    source, destination = _validate_roots(source_root, destination_root)
    identifier = str(job_id or uuid.uuid4())
    items = []
    projected = 0
    total_duration = 0.0
    formats = {}
    sample_rates = {}
    bit_depths = {}
    for root, directories, files in os.walk(source, followlinks=False):
        directories[:] = [
            name for name in directories if not os.path.islink(os.path.join(root, name))
        ]
        for filename in files:
            path = os.path.join(root, filename)
            extension = Path(filename).suffix.lower()
            if extension not in AUDIO_EXTENSIONS or os.path.islink(path):
                continue
            relative = os.path.relpath(path, source)
            try:
                probe = probe_library_audio(path)
                status = "queued"
                reason = ""
                projected_bytes = int(
                    max(0.0, probe.duration_sec)
                    * DJ_SAMPLE_RATE
                    * probe.channels
                    * 3
                )
                projected += projected_bytes
                total_duration += max(0.0, probe.duration_sec)
                formats[probe.codec] = formats.get(probe.codec, 0) + 1
                rate_label = str(probe.sample_rate)
                sample_rates[rate_label] = sample_rates.get(rate_label, 0) + 1
                depth_label = str(probe.bit_depth or probe.bit_depth_kind)
                bit_depths[depth_label] = bit_depths.get(depth_label, 0) + 1
            except Exception as exc:
                probe = None
                status = "skipped"
                reason = str(exc)
                projected_bytes = 0
            stat = os.stat(path)
            item = {
                "source_path": path,
                "relative_path": relative,
                "status": status,
                "reason": reason,
                "source_size": int(stat.st_size),
                "source_mtime_ns": int(stat.st_mtime_ns),
                "duration_sec": probe.duration_sec if probe else None,
                "source_codec": probe.codec if probe else extension.lstrip("."),
                "source_lossless": probe.lossless if probe else None,
                "source_sample_rate": probe.sample_rate if probe else None,
                "source_bit_depth": (
                    str(probe.bit_depth) if probe and probe.bit_depth else probe.bit_depth_kind if probe else None
                ),
                "source_channels": probe.channels if probe else None,
                "processing_json": {
                    "probe": probe.to_dict() if probe else None,
                    "projected_bytes": projected_bytes,
                },
            }
            items.append(item)
    create_library_job(
        identifier,
        source,
        destination,
        total_files=len(items),
        projected_bytes=projected,
        settings={
            "output": "FLAC PCM_24 48000Hz",
            "src": f"{SRC_ENGINE}/{SRC_QUALITY}/{SRC_PHASE}",
            "archive_originals": False,
            "scan_summary": {
                "duration_sec": total_duration,
                "formats": formats,
                "sample_rates": sample_rates,
                "bit_depths": bit_depths,
            },
        },
        database_path=database_path,
    )
    for item in items:
        upsert_library_asset(
            identifier,
            item.pop("source_path"),
            item.pop("relative_path"),
            database_path=database_path,
            **item,
        )
    skipped = sum(1 for item in items if item["status"] == "skipped")
    update_library_job(
        identifier,
        skipped_files=skipped,
        database_path=database_path,
    )
    return {
        "job_id": identifier,
        "source_root": source,
        "destination_root": destination,
        "total_files": len(items),
        "queued_files": len(items) - skipped,
        "skipped_files": skipped,
        "projected_bytes": projected,
        "duration_sec": total_duration,
        "formats": formats,
        "sample_rates": sample_rates,
        "bit_depths": bit_depths,
    }


def check_library_capacity(destination_root, projected_bytes, reserve_floor=50 * 1024**3):
    target = os.path.abspath(os.path.expanduser(destination_root))
    probe = target
    while not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    usage = shutil.disk_usage(probe)
    reserve = max(int(usage.total * 0.10), int(reserve_floor))
    required = int(projected_bytes) + reserve
    return {
        "ok": int(usage.free) >= required,
        "total": int(usage.total),
        "free": int(usage.free),
        "reserve": reserve,
        "projected": int(projected_bytes),
        "required": required,
    }


def library_destination_available(destination_root):
    destination = os.path.abspath(os.path.expanduser(destination_root))
    if destination.startswith("/Volumes/"):
        parts = Path(destination).parts
        if len(parts) < 3:
            return False
        volume_root = os.path.join("/Volumes", parts[2])
        return os.path.ismount(volume_root)
    probe = destination
    while not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            return False
        probe = parent
    return os.path.isdir(probe)


class LibraryConversionQueue:
    def __init__(self, database_path, cache_dir=None, event_callback=None):
        self.database_path = database_path
        self.cache_dir = os.path.abspath(
            os.path.expanduser(cache_dir or default_library_cache_dir())
        )
        self.event_callback = event_callback or (lambda _event, _payload: None)
        self._run_event = threading.Event()
        self._run_event.set()
        self._cancel_event = threading.Event()
        self._thread = None
        self.job_id = None
        recover_interrupted_library_jobs(database_path=self.database_path)

    @property
    def running(self):
        return bool(self._thread and self._thread.is_alive())

    @property
    def paused(self):
        return not self._run_event.is_set()

    def start(self, job_id):
        if self.running:
            raise RuntimeError("別のライブラリ変換が実行中です")
        self.job_id = str(job_id)
        self._cancel_event.clear()
        self._run_event.set()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def pause(self):
        self._run_event.clear()
        if self.job_id:
            update_library_job(
                self.job_id, status="paused", database_path=self.database_path
            )
        self._emit("paused", {"job_id": self.job_id})

    def resume(self):
        self._run_event.set()
        if self.job_id:
            update_library_job(
                self.job_id, status="running", database_path=self.database_path
            )
        self._emit("resumed", {"job_id": self.job_id})

    def cancel(self):
        self._cancel_event.set()
        self._run_event.set()

    def wait(self, timeout=None):
        if self._thread:
            self._thread.join(timeout)

    def _emit(self, event, payload):
        try:
            self.event_callback(event, dict(payload or {}))
        except Exception:
            pass

    def _job_counts(self):
        assets = list_library_assets(
            self.job_id, limit=1_000_000, database_path=self.database_path
        )
        return {
            "total_files": len(assets),
            "completed_files": sum(item["status"] == "complete" for item in assets),
            "duplicate_files": sum(item["status"] == "duplicate" for item in assets),
            "skipped_files": sum(item["status"] == "skipped" for item in assets),
            "failed_files": sum(item["status"] == "failed" for item in assets),
        }

    def _run(self):
        assets = list_library_assets(
            self.job_id,
            statuses=("queued", "failed"),
            limit=1_000_000,
            database_path=self.database_path,
        )
        if not assets:
            update_library_job(
                self.job_id, status="complete", database_path=self.database_path
            )
            self._emit("complete", self._job_counts())
            return
        from recording_catalog import list_library_jobs

        job = next(
            item
            for item in list_library_jobs(
                limit=1000, database_path=self.database_path
            )
            if item["job_id"] == self.job_id
        )
        if not library_destination_available(job["destination_root"]):
            update_library_job(
                self.job_id, status="paused", database_path=self.database_path
            )
            self._emit(
                "destination_missing",
                {"destination_root": job["destination_root"]},
            )
            return
        pending_bytes = sum(
            int(asset.get("processing", {}).get("projected_bytes") or 0)
            for asset in assets
        )
        capacity = check_library_capacity(job["destination_root"], pending_bytes)
        if not capacity["ok"]:
            update_library_job(
                self.job_id, status="failed", database_path=self.database_path
            )
            self._emit("capacity_error", capacity)
            return
        os.makedirs(job["destination_root"], exist_ok=True)
        os.makedirs(self.cache_dir, exist_ok=True)
        update_library_job(
            self.job_id, status="running", database_path=self.database_path
        )
        self._emit("started", {"job_id": self.job_id, "total": len(assets)})
        for asset in assets:
            if self._cancel_event.is_set():
                update_library_job(
                    self.job_id, status="paused", database_path=self.database_path
                )
                self._emit("cancelled", self._job_counts())
                return
            self._run_event.wait()
            if not library_destination_available(job["destination_root"]):
                update_library_job(
                    self.job_id, status="paused", database_path=self.database_path
                )
                self._emit(
                    "destination_missing",
                    {"destination_root": job["destination_root"]},
                )
                return
            try:
                self._convert_asset(asset, job)
            except Exception as exc:
                upsert_library_asset(
                    self.job_id,
                    asset["source_path"],
                    asset["relative_path"],
                    status="failed",
                    reason=str(exc),
                    database_path=self.database_path,
                )
                self._emit(
                    "asset_failed",
                    {"source_path": asset["source_path"], "reason": str(exc)},
                )
            counts = self._job_counts()
            update_library_job(
                self.job_id,
                status="paused" if self.paused else "running",
                database_path=self.database_path,
                **counts,
            )
            self._emit("progress", counts)
        counts = self._job_counts()
        final_status = "complete_with_errors" if counts["failed_files"] else "complete"
        update_library_job(
            self.job_id,
            status=final_status,
            database_path=self.database_path,
            **counts,
        )
        self._emit("complete", {**counts, "status": final_status})

    def _convert_asset(self, asset, job):
        source_path = asset["source_path"]
        relative_path = asset["relative_path"]
        if not os.path.isfile(source_path):
            raise FileNotFoundError("入力ファイルが見つかりません")
        upsert_library_asset(
            self.job_id,
            source_path,
            relative_path,
            status="converting",
            reason="",
            database_path=self.database_path,
        )
        suffix = Path(source_path).suffix
        with tempfile.TemporaryDirectory(
            prefix="library-", dir=self.cache_dir
        ) as working:
            staged_source = os.path.join(working, f"source{suffix}")
            source_sha, source_stat = _stage_source(source_path, staged_source)
            duplicate = find_library_duplicate(
                source_sha256=source_sha,
                exclude_source_path=source_path,
                database_path=self.database_path,
            )
            if duplicate and duplicate.get("output_path") and os.path.isfile(duplicate["output_path"]):
                upsert_library_asset(
                    self.job_id,
                    source_path,
                    relative_path,
                    status="duplicate",
                    reason=f"完全一致: {duplicate['output_path']}",
                    output_path=duplicate["output_path"],
                    source_sha256=source_sha,
                    source_size=source_stat.st_size,
                    source_mtime_ns=source_stat.st_mtime_ns,
                    database_path=self.database_path,
                )
                self._emit("asset_duplicate", {"source_path": source_path})
                return
            probe = probe_library_audio(staged_source)
            decoded_path = os.path.join(working, "decoded.wav")
            decode_to_float64_wav(staged_source, decoded_path, probe)
            pcm_sha = hash_decoded_pcm(decoded_path)
            duplicate = find_library_duplicate(
                pcm_sha256=pcm_sha,
                source_sample_rate=probe.sample_rate,
                source_channels=probe.channels,
                exclude_source_path=source_path,
                database_path=self.database_path,
            )
            if duplicate and duplicate.get("output_path") and os.path.isfile(duplicate["output_path"]):
                upsert_library_asset(
                    self.job_id,
                    source_path,
                    relative_path,
                    status="duplicate",
                    reason=f"復号PCM完全一致: {duplicate['output_path']}",
                    output_path=duplicate["output_path"],
                    source_sha256=source_sha,
                    pcm_sha256=pcm_sha,
                    source_size=source_stat.st_size,
                    source_mtime_ns=source_stat.st_mtime_ns,
                    source_codec=probe.codec,
                    source_lossless=probe.lossless,
                    source_sample_rate=probe.sample_rate,
                    source_bit_depth=str(probe.bit_depth or probe.bit_depth_kind),
                    source_channels=probe.channels,
                    duration_sec=probe.duration_sec,
                    database_path=self.database_path,
                )
                self._emit("asset_duplicate", {"source_path": source_path})
                return
            if probe.sample_rate == DJ_SAMPLE_RATE:
                dj_reference = decoded_path
                src_metadata = {}
            else:
                dj_reference = os.path.join(working, "resampled.wav")
                src_metadata = _resample_to_float64_wav(
                    decoded_path, dj_reference, output_rate=DJ_SAMPLE_RATE
                )
            analysis = _analyze_audio_file(dj_reference)
            input_true_peak = analysis.get("true_peak_dbtp", float("-inf"))
            safety_gain_db = (
                min(0.0, -1.0 - float(input_true_peak))
                if np.isfinite(input_true_peak)
                else 0.0
            )
            dsp_applied = probe.sample_rate != DJ_SAMPLE_RATE or safety_gain_db < 0.0
            dither, dither_reason = _dither_policy(probe, dsp_applied)
            metadata = read_library_metadata(staged_source)
            output_path = asset.get("output_path") or _destination_for(
                relative_path, job["destination_root"], source_sha
            )
            process = {
                "export_role": "library",
                "source_format": probe.codec,
                "source_lossless": probe.lossless,
                "source_sample_rate": probe.sample_rate,
                "source_bit_depth": probe.bit_depth or probe.bit_depth_kind,
                "source_verified": probe.lossless is True,
                "output_sample_rate": DJ_SAMPLE_RATE,
                "src_engine": src_metadata.get("src_engine"),
                "src_quality": src_metadata.get("src_quality"),
                "src_phase": src_metadata.get("src_phase"),
                "soxr_version": src_metadata.get("soxr_version"),
                "dither": dither,
                "dither_reason": dither_reason,
                "safety_gain_db": safety_gain_db,
                "input_true_peak_dbtp": input_true_peak,
                "source_sha256": source_sha,
                "pcm_sha256": pcm_sha,
                "projected_bytes": int(
                    asset.get("processing", {}).get("projected_bytes") or 0
                ),
            }
            upsert_library_asset(
                self.job_id,
                source_path,
                relative_path,
                status="converting",
                reason="",
                output_path=output_path,
                source_size=source_stat.st_size,
                source_mtime_ns=source_stat.st_mtime_ns,
                source_sha256=source_sha,
                pcm_sha256=pcm_sha,
                source_codec=probe.codec,
                source_lossless=probe.lossless,
                source_sample_rate=probe.sample_rate,
                source_bit_depth=str(probe.bit_depth or probe.bit_depth_kind),
                source_channels=probe.channels,
                duration_sec=probe.duration_sec,
                dither=dither,
                src_engine=process.get("src_engine") or "BYPASS",
                safety_gain_db=safety_gain_db,
                processing_json=process,
                database_path=self.database_path,
            )
            result = write_pcm24_flac(
                dj_reference,
                output_path,
                track_info=metadata,
                artwork_bytes=metadata.get("artwork_bytes"),
                processing=process,
                dither=dither,
                gain_db=safety_gain_db,
                random_seed=int(source_sha[:16], 16),
            )
            final_info = sf.info(output_path)
            if (
                final_info.format != "FLAC"
                or final_info.subtype != "PCM_24"
                or int(final_info.samplerate) != DJ_SAMPLE_RATE
                or int(final_info.channels) != probe.channels
            ):
                raise RuntimeError("確定FLACが24-bit/48kHz条件を満たしません")
            tags = FLAC(output_path)
            if tags.get("SOURCE_SHA256") != [source_sha]:
                raise RuntimeError("FLACのソースハッシュ証跡を確認できません")
            upsert_library_asset(
                self.job_id,
                source_path,
                relative_path,
                status="complete",
                reason="",
                output_path=output_path,
                source_size=source_stat.st_size,
                source_mtime_ns=source_stat.st_mtime_ns,
                source_sha256=source_sha,
                pcm_sha256=pcm_sha,
                source_codec=probe.codec,
                source_lossless=probe.lossless,
                source_sample_rate=probe.sample_rate,
                source_bit_depth=str(probe.bit_depth or probe.bit_depth_kind),
                source_channels=probe.channels,
                duration_sec=float(final_info.frames) / DJ_SAMPLE_RATE,
                output_bytes=os.path.getsize(output_path),
                dither=dither,
                src_engine=process.get("src_engine") or "BYPASS",
                safety_gain_db=safety_gain_db,
                artwork_embedded=result.get("artwork_embedded"),
                processing_json=process,
                database_path=self.database_path,
            )
            self._emit(
                "asset_complete",
                {"source_path": source_path, "output_path": output_path},
            )
