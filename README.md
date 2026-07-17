# Hi-Res Recorder

Spotify/Qobuz DesktopのOffline再生出力をアプリ内で音量を変えずに32-bit floatで記録し、TPDFディザ付き24-bit FLACへ自動変換して、録音品質と疑義箇所を管理するmacOSアプリです。

録音には、音楽アプリ専用の仮想オーディオ経路を使います。LoopbackはRogue Amoeba製の有料ルーティングアプリ、BlackHoleは無料で使える仮想オーディオドライバです。Macの通常の入力1/2chを直接録音すると、通知音やブラウザなど他アプリの音が混ざる可能性があります。専用の2ch経路を用意し、SpotifyまたはQobuzだけをそこへ出力してください。

## 録音品質

- `sounddevice.InputStream(dtype="float32")`からWAV保存までゲインは常時`1.0`
- キャプチャと解析はWAV 32-bit IEEE float。正規化、リミッター、クリップ、録音中リサンプリングなし
- 入力デバイスの実レートを維持し、最大192kHzの長時間録音をディスクスプールへ保存
- WAVから24-bit FLACへ変換する際はピーク±1 LSB（2 LSB peak-to-peak）のTPDFディザを使用
- 変換後のFLACを再読込し、形式、レート、チャンネル数、フレーム数、有限値、タグ、ジャケット画像を検証
- 検証成功時だけ一時WAVを自動削除。Sample Peakが0dBFSを超える場合や変換・検証失敗時はFLAC化を拒否してWAVを保持
- Integrated LUFS、Sample Peak、4倍True Peak、0dBFS到達位置をチャンク解析
- PortAudio異常、フレーム不足、0.5秒以上の無音、同一ブロック反復、境界不連続、再生停止、タイムライン滑りを記録
- 音声波形だけからLosslessを断定せず、常に「bit一致未証明」と表示

## 初めて使う人へ

このアプリは開発途中です。配布済みアプリをダブルクリックするだけで完結する段階ではなく、Macのオーディオ経路、Python環境、録音アプリのビルドを設定する必要があります。音楽ファイルの権利と各サービスの利用規約を守れるコンテンツだけを対象にしてください。

### いちばん現実的な方法: Codexなどのコーディングエージェントに任せる

このリポジトリのURLと次の依頼文をコーディングエージェントに渡してください。Macの構成は人によって異なるため、手順を丸写しするより、実際のデバイス名・チャンネル構成・アプリの場所を確認させた方が失敗が少なくなります。

```text
このHi-Res RecorderをMacで使えるようにセットアップして。
READMEの手順に従い、uv環境、BlackHoleまたはLoopbackの録音専用2ch経路、
マイク権限、アプリのビルドと起動まで確認して。ほかのアプリ音が録音に混ざらない
設定にし、Spotify/Qobuz用の最適なサンプルレートも確認して。
```

### 自分でセットアップする場合

1. Xcode Command Line Tools、Git、[uv](https://docs.astral.sh/uv/)をインストールします。
2. [BlackHole](https://existential.audio/blackhole/)（無料）または[Loopback](https://rogueamoeba.com/loopback/)（有料）を導入し、Spotify/Qobuz専用のステレオ出力を作ります。通常のMacスピーカー出力や、通知音と共用する入力を録音先にしないでください。
3. SpotifyまたはQobuzの出力先をその専用経路に設定します。モニター再生が必要なら、録音経路とは別にスピーカーへ送ります。
4. ターミナルで以下を実行します。`<repository-url>`は、このGitHubリポジトリのURLに置き換えます。

```bash
git clone <repository-url>
cd spotify_recorder
uv sync
uv run python spotify_recorder.py
```

5. macOSからマイク（オーディオ入力）権限を求められたら許可します。SpotifyのOffline Mode確認には「システム設定 > プライバシーとセキュリティ > アクセシビリティ」でHi-Res Recorderまたは実行中のターミナルも許可します。
6. アプリのInput Deviceで専用経路を選び、左右の開始チャンネルがその経路のステレオ2chと一致することを確認します。
7. 音楽サービス側の音量は100%にし、EQ、Crossfade、Automix、音量の均一化はOFFにします。Spotifyは対象をLossless設定で完全ダウンロードして`File > Offline Mode`をON、QobuzはOffline再生とExclusive ModeをONにします。
8. まず短いテスト録音を行います。レビューで入力レート、LUFS、Peak、True Peak、疑義イベントを確認し、通知音や別アプリの音が混ざっていないことを再生して確かめます。問題があれば録音せず、ルーティングを修正します。
9. 「録音/FLAC」からFLAC管理タブを開き、変換結果、WAV削除状態、ジャケット埋め込み、容量を確認します。拒否・失敗時はWAVが残るため、原因を解消してから再実行できます。

SpotifyとQobuzのどちらもStandbyと自動停止に対応します。サービスを選ぶと「監視対象」に現在のOfflineサービスが表示されます。StandbyをONにすると選択中サービスの再生開始で録音を始め、自動停止をONにすると停止・一時停止が設定した猶予時間を超えた時点で録音を止めます。

### 録音時の判断

- Spotify: Offline専用です。ダウンロード品質がLossless候補で、Spotifyの`File > Offline Mode`がONと確認できた場合だけ開始できます。ただし配信版は販売用・CD・DJ向けファイルとマスタリングが異なることがあります。
- Qobuz: Offline専用です。完全ダウンロードとアプリ側の品質条件を確認できた場合だけ開始できます。ローカル証跡を取得できない場合、手動入力での代替やStreaming録音は行いません。
- DJ用途: 曲間のLUFS差やマスタリング差は、録音不良ではありません。本番用に音量を揃えたい場合は、原音記録とは別に書き出したコピーを使ってください。原音記録ファイルへ正規化をかけると、このアプリのUnity Gain方針と両立しません。

## Spotify Offlineモード

Spotifyのメニューバーにある`File > Offline Mode`のチェック状態をmacOSアクセシビリティ経由で確認します。OFF、Spotify未起動、確認権限なし、メニュー構造未対応のいずれでも録音開始を拒否します。回線実測、通信量監視、Streaming品質設定は使用しません。

Spotifyの非公開prefsからダウンロード品質、音量の均一化、Automixを読み取ります。これらは設定条件の証跡であり、現在の曲のデコード済みサンプルと配信原本のbit一致を証明するものではありません。

## Qobuz Offlineモード

Qobuz Desktopのローカル状態、SQLite、ログを読み取り専用で監視します。Qobuzの録音経路はOffline固定で、Streamingへの切り替えや手動証跡による品質ゲートの迂回はできません。認証情報、非公開API、Qobuz Connect、暗号化キャッシュ、復号鍵にはアクセスしません。

完全ダウンロード、曲ID、配信形式、サンプルレート、bit深度、音量100%、ミュートOFF、Exclusive Mode ONを確認できた場合だけ録音できます。ローカル構造が未対応の場合は開始を拒否します。

Qobuz公式資料:

- https://help.qobuz.com/en/articles/10139-what-is-in-the-streaming-catalogue
- https://help.qobuz.com/en/articles/10137-in-what-quality-can-i-listen-to-music-in-offline-playback

## 厳格録音経路

`Spotify/Qobuz Offline -> 単一2ch LoopbackまたはBlackHole -> Hi-Res Recorder`を使用します。Spotifyは44.1kHz入力を要求します。QobuzではCoreAudio Device UIDとnominal sample rateを照合し、Aggregate Device、Multi-Output Device、レート不一致、非ステレオ、途中のレート変更を拒否します。

Qobuzは音量100%、Exclusive Mode ON、最高配信品質を使用してください。異なるサンプルレートの曲はセッションを分けて録音します。

## 履歴と復旧

保存曲、サービス、Qobuz曲ID、ソース形式、LUFS、Peak、True Peak、品質合否、疑義時刻、再録要否をSQLiteへ保存します。管理画面は録音履歴とFLAC管理の2タブで、変換状態、拒否理由、ジャケット埋め込み、WAV削除結果、容量も確認できます。

```text
~/Library/Application Support/HiResRecorder/recordings.sqlite3
```

旧`SpotifyRecorder/recordings.sqlite3`は初回起動時にコピー移行し、元DBを削除しません。録音中のrawスプールはFinderに出さずキャッシュへ置き、異常終了後は次回起動時に復旧レビューできます。

## 開発

```bash
uv sync
uv run python spotify_recorder.py
uv run python -m unittest discover -s tests -v
```

## ビルド

```bash
uv run python -m PyInstaller --clean --noconfirm HiResRecorder.spec
```

出力は`dist/Hi-Res Recorder.app`です。Qobuz連携はfixtureによる自動テストを備えていますが、実機のアプリ構造やオーディオデバイスはバージョン・環境で変わります。44.1/96/192kHzの実機録音、再生、品質レビューを行ってから本番利用してください。
