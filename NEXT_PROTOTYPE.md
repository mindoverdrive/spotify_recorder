# Spotify Recorder Next Prototype

> 状態: 実験版です。通常利用と高音質監査には`SpotifyRecorder_Native.app`を使用します。

## 目的

既存アプリとは別に、停止時にできる「次曲の頭だけ録音された短いファイル」を保存前に自動スキップする試作用アプリです。

## 追加した挙動

- `Stop & Save`時、最後の曲候補が停止時点でSpotifyに表示されている曲と同じで、かつ`最終断片 秒`以下なら保存しません。
- `通常スキップ 秒`未満の候補は保存しません。
- `Standby`を使うと、Spotifyの再生開始で自動録音します。
- `Spotify停止/一時停止で自動停止`を使うと、Spotify側を止めるだけで録音処理に入れます。
- `Abort`は保存せず録音バッファを破棄します。

## 録音品質

- キャプチャから保存までUnity Gain (`1.0`) 固定で、ノーマライズ、リミッター、クリップ処理を行いません。
- 保存形式はWAV 32-bit IEEE float固定です。入力デバイスのサンプルレートを維持し、録音中はリサンプリングしません。
- 保存候補ごとにIntegrated LUFS、Sample Peak、4倍オーバーサンプリングTrue Peak、フルスケール到達数を測定します。
- LUFSは測定と品質警告にのみ使い、音声データの音量は変更しません。
- 測定値はWAVのID3タグに埋め込み、副ファイルは作成しません。

## Spotify推奨設定

- 音量の均一: OFF
- 音質: Lossless（利用できない場合はVery High）
- 音質の自動調整、Equalizer、Crossfade、Automix: OFF
- Spotify音量: 100%
- Gapless playback: ON
- Audio MIDI設定: 仮想入力・出力を可能なら44.1kHzへ統一

## 試し方

```bash
cd /Users/user/Documents/Python_work/spotify_recorder
uv sync
uv run python spotify_recorder_next.py
```

## ビルド候補

```bash
cd /Users/user/Documents/Python_work/spotify_recorder
uv run python -m PyInstaller SpotifyRecorder_Next.spec
```

既存アプリとは別名の`SpotifyRecorder_Next.app`として出力されます。
