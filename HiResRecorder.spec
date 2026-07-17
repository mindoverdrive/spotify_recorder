# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

audio_binaries = collect_dynamic_libs("_soundfile_data")
customtkinter_data = collect_data_files("customtkinter")
hidden_imports = (
    [
        "soundfile",
        "mutagen.flac",
        "pyloudnorm",
        "scipy.signal",
        "qobuz_integration",
        "source_providers",
        "capture_spool",
        "coreaudio_devices",
        "recording_catalog",
    ]
    + collect_submodules("pyloudnorm")
)

a = Analysis(
    ["spotify_recorder.py"],
    pathex=[],
    binaries=audio_binaries,
    datas=customtkinter_data,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="HiResRecorder",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="HiResRecorder",
)
app = BUNDLE(
    coll,
    name="Hi-Res Recorder.app",
    version="4.2.0",
    bundle_identifier="local.hires-recorder.native",
    info_plist={
        "CFBundleDisplayName": "Hi-Res Recorder",
        "NSMicrophoneUsageDescription": "システム音声を高品質FLACとして記録するためにオーディオ入力へのアクセスが必要です。",
        "NSAppleEventsUsageDescription": "Spotifyモードで再生中の曲情報を取得するためにアクセスが必要です。",
    },
)
