# Hi-Res Recorder

Mac用です。HiResRecorder.specでinstallして下さい。
Spotify/Qobuz Desktopの出力をUnity Gainの32-bit float WAVとして保存し、録音品質と疑義箇所を管理するmacOSアプリです。Native版を正式系とし、Nextは既存の実験版として残しています。
*俺の環境ではLoop Back(仮想チャンネルソフト)を使ってるので、デフォルトの入力チャンネルが5,6chステレオになってます。これを1,2chにするとOS通知音とか入っちゃうよ！
通知音をoffにするなり、あんたの環境に合わせてカスタマイズしてくれ。-> CodexにこのURL投げちゃえ！！

## 録音品質

- `sounddevice.InputStream(dtype="float32")`からWAV保存までゲインは常時`1.0`
- WAV 32-bit IEEE float固定。正規化、リミッター、クリップ、録音中リサンプリングなし
- 入力デバイスの実レートを維持し、最大192kHzの長時間録音をディスクスプールへ保存
- 4GiB未満はRIFF WAV、長大な単一曲はRF64 WAVへ自動切替
- Integrated LUFS、Sample Peak、4倍True Peak、0dBFS到達位置をチャンク解析
- PortAudio異常、フレーム不足、0.5秒以上の無音、同一ブロック反復、境界不連続、再生停止、タイムライン滑りを記録
- 音声波形や通信量だけからLosslessを断定せず、常に「bit一致未証明」と表示

## Qobuzモード

Qobuz Desktopのローカル状態、SQLite、ログを読み取り専用で監視します。認証情報、非公開API、Qobuz Connect、暗号化キャッシュ、復号鍵にはアクセスしません。

### Offline

完全ダウンロード、曲ID、配信形式、サンプルレート、bit深度、音量100%、ミュートOFF、Exclusive Mode ONを確認できた場合だけ録音できます。ローカル構造が未対応の場合は開始を拒否します。

### Streaming

ローカル証跡が取れる場合は同じ厳格ゲートを使います。取得不能時は手動でサンプルレート/bit深度と設定確認を入力できますが、履歴には「Qobuzソース品質未検証」と保存されます。QobuzはHi-Resストリーミングに10Mbps超を推奨しているため、録音前の回線実測基準も10Mbpsです。

Qobuz公式資料:

- https://help.qobuz.com/en/articles/10139-what-is-in-the-streaming-catalogue
- https://help.qobuz.com/en/articles/10137-in-what-quality-can-i-listen-to-music-in-offline-playback
- https://help.qobuz.com/en/articles/10149-do-i-need-a-good-internet-connection-to-stream-in-hi-res

## 厳格録音経路

`Spotify/Qobuz -> 単一2ch LoopbackまたはBlackHole -> Hi-Res Recorder`を使用します。QobuzではCoreAudio Device UIDとnominal sample rateを照合し、Aggregate Device、Multi-Output Device、レート不一致、非ステレオ、途中のレート変更を拒否します。

Qobuzは音量100%、Exclusive Mode ON、最高配信品質を使用してください。異なるサンプルレートの曲はセッションを分けて録音します。

## 履歴と復旧

保存曲、サービス、Qobuz曲ID、ソース形式、LUFS、Peak、True Peak、品質合否、疑義時刻、再録要否をSQLiteへ保存します。履歴画面から検索、サービス/再録状態の絞り込み、疑義箇所の試聴ができます。

```text
~/Library/Application Support/HiResRecorder/recordings.sqlite3
```

旧`SpotifyRecorder/recordings.sqlite3`は初回起動時にコピー移行し、元DBを削除しません。録音中のrawスプールはFinderに出さずキャッシュへ置き、異常終了後は次回起動時に復旧レビューできます。

## 開発

```bash
cd /Users/user/Documents/Python_work/spotify_recorder
uv sync
uv run python spotify_recorder.py
uv run python -m unittest discover -s tests -v
```

## ビルド

```bash
uv run python -m PyInstaller --clean --noconfirm HiResRecorder.spec
```

出力は`dist/Hi-Res Recorder.app`です。QobuzアプリがこのMacに未導入のため、fixtureによる連携テストは自動化済みですが、44.1/96/192kHzの実機録音確認はQobuz導入後に行う必要があります。
