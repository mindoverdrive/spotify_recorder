# Spotify Recorder Native

Spotify Desktopの出力をUnity Gainの32-bit float WAVとして保存し、録音品質と疑わしい箇所を監査するmacOSアプリです。通常利用はNative、Nextは実験版です。

## 録音品質

- `sounddevice.InputStream(dtype="float32")`からWAV保存までゲインは常時`1.0`
- WAV 32-bit IEEE float固定。ノーマライズ、リミッター、クリップ、録音中のリサンプリングなし
- Integrated LUFS、Sample Peak、4倍True Peak、フルスケール到達位置を測定
- Spotify内部設定からLossless候補、音質自動低下OFF、音量均一OFF、Automix OFFの証跡を取得
- PortAudio異常、フレーム不足、0.5秒以上のデジタル無音、同一ブロック反復、ブロック境界不連続を検出
- Spotify再生位置と録音フレームを比較し、停止とタイムライン滑りの疑いを時刻付きで検出
- Spotify通信量を`nettop`で受動記録。通信量はバッファやキャッシュの影響を受けるためコーデック判定には使用しない
- 任意の「回線実測」はApple `networkQuality`を使う。通信量が多いため録音中は実行しない

Spotifyの公開APIは再生中の実効コーデック、ビット深度、Lossless採用結果を返しません。アプリの「合格」は録音経路と設定条件の合格であり、実効Losslessの証明ではありません。

## 推奨手順

1. Spotifyのストリーミング品質とダウンロード品質をLosslessにする。
2. 音質の自動調整、音量の均一、Equalizer、Crossfade、AutomixをOFFにする。
3. 対象アルバムまたはプレイリストを完全にダウンロードする。
4. SpotifyをOffline Modeへ切り替える。未ダウンロード曲が再生不能になるため、回線低下を録音経路から除外できる。
5. Audio MIDI設定でSpotify出力と仮想入力を44.1kHzへ統一する。
6. Nativeの「品質診断」を実行し、Standbyから録音する。
7. レビューで赤い警告と疑い箇所を確認し、問題のある曲だけ録音し直す。

Spotify公式はLosslessを最大24-bit/44.1kHz FLAC、安定回線の目安を1.5-2Mbpsと案内しています。ダウンロード済みコンテンツだけを再生するにはOffline Modeを使えます。

- https://support.spotify.com/uk/article/lossless-audio-quality/
- https://support.spotify.com/us/article/listen-offline/
- https://support.spotify.com/us/article/audio-quality/

実効Losslessをファイル単位で確実に証明する必要がある場合、録音ではなく、正規販売元からDRM-free FLACを購入して元ファイルを保管する方法が最も確実です。

## 録音履歴

保存した曲、ファイルパス、LUFS、Peak、True Peak、品質合否、警告、疑い箇所をSQLiteへ記録します。アプリの「録音履歴」から検索、再生、Finder表示ができます。

```text
~/Library/Application Support/SpotifyRecorder/recordings.sqlite3
```

音声フォルダには解析JSONを作りません。監査結果はWAVのID3 `TXXX`タグにも保存します。

## 開発

```bash
cd /Users/user/Documents/Python_work/spotify_recorder
uv sync
uv run python spotify_recorder.py
uv run python -m unittest discover -s tests -v
```

## Nativeビルド

```bash
uv run python -m PyInstaller --clean --noconfirm SpotifyRecorder_Native.spec
```

出力は`dist/SpotifyRecorder_Native.app`です。
