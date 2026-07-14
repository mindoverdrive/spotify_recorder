# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_dynamic_libs

audio_binaries = collect_dynamic_libs('_soundfile_data')

a = Analysis(
    ['spotify_recorder.py'],
    pathex=[],
    binaries=audio_binaries,
    datas=[('/Users/user/Documents/Python_work/spotify_recorder/.venv/lib/python3.12/site-packages/customtkinter', 'customtkinter/')],
    hiddenimports=['soundfile', 'pyloudnorm', 'scipy.signal'],
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
    name='SpotifyRecorder_Native',
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
    name='SpotifyRecorder_Native',
)
app = BUNDLE(
    coll,
    name='SpotifyRecorder_Native.app',
    version='3.0.0',
    icon='app_icon.icns',
    bundle_identifier='local.spotify-recorder.native',
    info_plist={
        'NSMicrophoneUsageDescription': 'システム音声を録音するためにマイクアクセスが必要です。',
        'NSAppleEventsUsageDescription': 'Spotifyの再生情報を取得するためにアクセスが必要です。',
    },
)
