# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ["spotify_recorder_next.py"],
    pathex=[],
    binaries=[],
    datas=[
        (
            "/Users/user/Documents/Python_work/spotify_recorder/.venv/lib/python3.12/site-packages/customtkinter",
            "customtkinter/",
        )
    ],
    hiddenimports=[],
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
    name="SpotifyRecorder_Next",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
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
    upx=True,
    upx_exclude=[],
    name="SpotifyRecorder_Next",
)
app = BUNDLE(
    coll,
    name="SpotifyRecorder_Next.app",
    icon=None,
    bundle_identifier="local.spotify-recorder.next",
    info_plist={
        "NSMicrophoneUsageDescription": "システム音声を録音するためにマイクアクセスが必要です。",
        "NSAppleEventsUsageDescription": "Spotifyの再生情報を取得するためにSpotifyへのアクセスが必要です。",
    },
)
