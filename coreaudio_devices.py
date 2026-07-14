import ctypes
import ctypes.util
from dataclasses import asdict, dataclass


def _fourcc(value):
    return int.from_bytes(value.encode("ascii"), "big")


class AudioObjectPropertyAddress(ctypes.Structure):
    _fields_ = [
        ("mSelector", ctypes.c_uint32),
        ("mScope", ctypes.c_uint32),
        ("mElement", ctypes.c_uint32),
    ]


SYSTEM_OBJECT = 1
GLOBAL_SCOPE = _fourcc("glob")
INPUT_SCOPE = _fourcc("inpt")
MASTER_ELEMENT = 0
PROPERTY_DEVICES = _fourcc("dev#")
PROPERTY_NAME = _fourcc("lnam")
PROPERTY_UID = _fourcc("uid ")
PROPERTY_NOMINAL_RATE = _fourcc("nsrt")
PROPERTY_TRANSPORT = _fourcc("tran")
TRANSPORT_AGGREGATE = _fourcc("grup")


@dataclass(frozen=True)
class CoreAudioDevice:
    device_id: int
    name: str
    uid: str | None
    nominal_sample_rate: float
    transport: str
    max_input_channels: int
    is_aggregate: bool

    def to_dict(self):
        return asdict(self)


def _libraries():
    coreaudio_path = ctypes.util.find_library("CoreAudio")
    corefoundation_path = ctypes.util.find_library("CoreFoundation")
    if not coreaudio_path or not corefoundation_path:
        raise RuntimeError("CoreAudioフレームワークを読み込めません")
    coreaudio = ctypes.cdll.LoadLibrary(coreaudio_path)
    corefoundation = ctypes.cdll.LoadLibrary(corefoundation_path)
    coreaudio.AudioObjectGetPropertyDataSize.argtypes = [
        ctypes.c_uint32,
        ctypes.POINTER(AudioObjectPropertyAddress),
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    coreaudio.AudioObjectGetPropertyDataSize.restype = ctypes.c_int32
    coreaudio.AudioObjectGetPropertyData.argtypes = [
        ctypes.c_uint32,
        ctypes.POINTER(AudioObjectPropertyAddress),
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_void_p,
    ]
    coreaudio.AudioObjectGetPropertyData.restype = ctypes.c_int32
    corefoundation.CFStringGetCString.argtypes = [
        ctypes.c_void_p,
        ctypes.c_char_p,
        ctypes.c_long,
        ctypes.c_uint32,
    ]
    corefoundation.CFStringGetCString.restype = ctypes.c_bool
    return coreaudio, corefoundation


def _address(selector, scope=GLOBAL_SCOPE):
    return AudioObjectPropertyAddress(selector, scope, MASTER_ELEMENT)


def _property_bytes(coreaudio, object_id, selector, scope=GLOBAL_SCOPE):
    address = _address(selector, scope)
    size = ctypes.c_uint32()
    status = coreaudio.AudioObjectGetPropertyDataSize(
        int(object_id), ctypes.byref(address), 0, None, ctypes.byref(size)
    )
    if status != 0 or size.value == 0:
        return None
    buffer = ctypes.create_string_buffer(size.value)
    status = coreaudio.AudioObjectGetPropertyData(
        int(object_id),
        ctypes.byref(address),
        0,
        None,
        ctypes.byref(size),
        buffer,
    )
    return None if status != 0 else buffer.raw[: size.value]


def _property_scalar(coreaudio, object_id, selector, ctype, scope=GLOBAL_SCOPE):
    raw = _property_bytes(coreaudio, object_id, selector, scope)
    if raw is None or len(raw) < ctypes.sizeof(ctype):
        return None
    return ctype.from_buffer_copy(raw).value


def _property_cfstring(coreaudio, corefoundation, object_id, selector):
    pointer = _property_scalar(coreaudio, object_id, selector, ctypes.c_void_p)
    if not pointer:
        return None
    buffer = ctypes.create_string_buffer(4096)
    if not corefoundation.CFStringGetCString(pointer, buffer, len(buffer), 0x08000100):
        return None
    return buffer.value.decode("utf-8", errors="replace")


def list_coreaudio_devices(sounddevice_devices=None):
    coreaudio, corefoundation = _libraries()
    raw_ids = _property_bytes(coreaudio, SYSTEM_OBJECT, PROPERTY_DEVICES)
    if not raw_ids:
        return []
    count = len(raw_ids) // ctypes.sizeof(ctypes.c_uint32)
    ids = (ctypes.c_uint32 * count).from_buffer_copy(raw_ids)
    sd_by_name = {}
    for item in sounddevice_devices or []:
        sd_by_name.setdefault(str(item.get("name", "")), []).append(item)

    devices = []
    for device_id in ids:
        name = _property_cfstring(coreaudio, corefoundation, device_id, PROPERTY_NAME)
        if not name:
            continue
        uid = _property_cfstring(coreaudio, corefoundation, device_id, PROPERTY_UID)
        rate = _property_scalar(
            coreaudio, device_id, PROPERTY_NOMINAL_RATE, ctypes.c_double
        )
        transport_value = _property_scalar(
            coreaudio, device_id, PROPERTY_TRANSPORT, ctypes.c_uint32
        )
        transport = (
            int(transport_value).to_bytes(4, "big").decode("ascii", errors="replace")
            if transport_value is not None
            else "unknown"
        )
        matches = sd_by_name.get(name, [])
        max_inputs = max(
            (int(item.get("max_input_channels", 0)) for item in matches),
            default=0,
        )
        normalized = name.lower().replace(" ", "")
        aggregate = transport_value == TRANSPORT_AGGREGATE or any(
            token in normalized
            for token in ("aggregate", "multi-output", "multioutput", "複数出力")
        )
        devices.append(
            CoreAudioDevice(
                device_id=int(device_id),
                name=name,
                uid=uid,
                nominal_sample_rate=float(rate or 0.0),
                transport=transport,
                max_input_channels=max_inputs,
                is_aggregate=aggregate,
            )
        )
    return devices


def resolve_coreaudio_device(device_name, sounddevice_devices=None):
    name = str(device_name)
    matches = [
        item
        for item in list_coreaudio_devices(sounddevice_devices)
        if item.name == name
    ]
    if len(matches) != 1:
        reason = "見つかりません" if not matches else "同名デバイスが複数あります"
        raise RuntimeError(f"CoreAudioデバイスを一意に特定できません: {name} ({reason})")
    return matches[0]
