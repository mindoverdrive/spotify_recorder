# Spotify Recorder Next Prototype

## 目的

既存アプリとは別に、停止時にできる「次曲の頭だけ録音された短いファイル」を保存前に自動スキップする試作用アプリです。

## 追加した挙動

- `Stop & Save`時、最後の曲候補が停止時点でSpotifyに表示されている曲と同じで、かつ`最終断片 秒`以下なら保存しません。
- `通常スキップ 秒`未満の候補は保存しません。
- `Standby`を使うと、Spotifyの再生開始で自動録音します。
- `Spotify停止/一時停止で自動停止`を使うと、Spotify側を止めるだけで録音処理に入れます。
- `Abort`は保存せず録音バッファを破棄します。

## 試し方

```bash
cd /Users/user/Documents/Python_work/spotify_recorder
source .venv/bin/activate
python spotify_recorder_next.py
```

## ビルド候補

```bash
cd /Users/user/Documents/Python_work/spotify_recorder
source .venv/bin/activate
pyinstaller SpotifyRecorder_Next.spec
```

既存アプリとは別名の`SpotifyRecorder_Next.app`として出力されます。
