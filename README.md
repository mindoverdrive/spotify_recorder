# Hi-Res Recorder

> **重要: 現在の開発版はエラーで本番利用できません。**
>
> Qobuzプレイリスト自動録音の実機検証中に、macOS Accessibility経由のQobuz操作・権限認識まわりでジョブ開始が停止する状態を確認しています。README以下の仕様は目標設計と実装中の内容を含みますが、現時点では「Qobuzのプレイリストをまとめて録音し、DJライブラリを作る」用途で完走保証できません。修正と実機再検証が終わるまで、本番音源の作成には使わないでください。

Qobuz DesktopのOfflineプレイリストを一括処理し、各曲をネイティブレート・Unity Gainで記録して、アーカイブFLACとTraktor向け24-bit/48kHz FLACを自動生成します。Spotify Offlineの手動録音と、手動入手したローカル音源の24-bit/48kHz一括変換も別系統で利用できます。

録音には、音楽アプリ専用の仮想オーディオ経路を使います。LoopbackはRogue Amoeba製の有料ルーティングアプリ、BlackHoleは無料で使える仮想オーディオドライバです。Macの通常の入力1/2chを直接録音すると、通知音やブラウザなど他アプリの音が混ざる可能性があります。専用の2ch経路を用意し、SpotifyまたはQobuzだけをそこへ出力してください。

## 録音品質

- `sounddevice.InputStream(dtype="float32")`からWAV保存までゲインは常時`1.0`
- キャプチャと解析はWAV 32-bit IEEE float。正規化、リミッター、クリップ、録音中リサンプリングなし
- 入力デバイスの実レートを維持し、最大192kHzの長時間録音をディスクスプールへ保存
- アーカイブ版は録音レートを維持した24-bit FLAC、DJ版は24-bit/48kHz FLACとして`DJ 24-48/`へ保存
- 48kHz以外のDJ版だけlibsoxrのVHQ、float64、固定レート、Linear PhaseでオフラインSRC。48kHz入力はSRCを完全バイパス
- 16-bitソースから24-bitへの拡張は、SRCや安全減衰の有無にかかわらずディザなし。bit深度不明のSpotifyもディザなし
- 検証済み24-bitソースにSRCまたは安全減衰を行った場合だけ、最終PCM24量子化時にTPDFディザを1回使用
- 無ディザ変換はround-to-nearest-evenを使用し、範囲外値をクリップせず変換拒否
- DJ版はSRC後の4倍True Peakが-1dBTPを超える場合だけ、リミッターを使わず必要量の線形減衰を適用
- 両FLACを再読込し、形式、レート、チャンネル数、フレーム数、量子化誤差、有限値、タグ、ジャケット画像を検証
- ArchiveとDJ版の両方の検証成功時だけ一時WAVを自動削除。一方でも失敗した場合はWAVを保持
- Integrated LUFS、Sample Peak、4倍True Peak、0dBFS到達位置をチャンク解析
- PortAudio異常、フレーム不足、0.5秒以上の無音、同一ブロック反復、境界不連続、再生停止、タイムライン滑りを記録
- 音声波形だけからLosslessを断定せず、常に「bit一致未証明」と表示
- Spotifyは44.1kHz、Qobuzは検証済みソースレートへ、停止・一時停止中だけLoopback/BlackHoleを自動同期
- 再生中のレート不一致は変更せず録音を拒否し、同期後に曲頭からの再生を要求

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
2. [BlackHole](https://existential.audio/blackhole/)（無料）または[Loopback](https://rogueamoeba.com/loopback/)（有料）を導入します。Qobuzには2ch Pass-Thruだけを持つ専用デバイスを作ります。Loopbackでは`Hi-Res Recorder Qobuz Loopback`のように、名前へ`Qobuz`と`Loopback`の両方を含めてください。Spotifyにも別の2chアプリソース専用デバイスを用意します。通常のMacスピーカー出力や、通知音と共用する16chデバイスを録音先にしないでください。
3. SpotifyまたはQobuzの出力先をその専用経路に設定します。モニター再生が必要なら、録音経路とは別にスピーカーへ送ります。
4. ターミナルで以下を実行します。`<repository-url>`は、このGitHubリポジトリのURLに置き換えます。

```bash
git clone <repository-url>
cd spotify_recorder
uv sync
uv run python spotify_recorder.py
```

5. macOSからマイク（オーディオ入力）権限を求められたら許可します。「システム設定 > プライバシーとセキュリティ > アクセシビリティ」でHi-Res Recorderを許可します。ソースから`uv run`で起動する開発時だけ、実行元のターミナルにも許可が必要です。これはSpotifyのOffline Mode確認と、Qobuzを曲頭から自動再生する操作に使います。
6. アプリは起動時、名前に`Qobuz`と`Loopback`（または`BlackHole`）を含む入力を優先して自動選択します。見つからない場合だけInput Deviceで専用経路を選びます。Qobuzプレイリスト録音は専用2chデバイスの1–2ch固定で、共有16ch構成は開始拒否されます。
7. 音楽サービス側の音量は100%にし、EQ、Crossfade、Automix、音量の均一化はOFFにします。Spotifyは対象をLossless設定で完全ダウンロードして`File > Offline Mode`をON、Qobuzは対象プレイリストを最高品質で完全ダウンロードしてから使います。
8. Qobuzでは対象曲を1つのプレイリストへまとめ、全曲を最高品質で完全ダウンロードします。本アプリでプレイリストを更新して「事前検査」を実行し、未完了曲が0件になってから開始します。不要な未完了曲だけは確認画面で今回のジョブから明示的に除外できます。
9. 「録音/出力」からFLAC出力タブを開き、Archive/DJ種別、入出力レート、SRC、ディザ、安全減衰量、WAV削除状態、ジャケットを確認します。拒否・失敗時はWAVが残るため、原因を解消してから再実行できます。
10. 手動ダウンロード音源を変換する場合は「ライブラリ変換」タブで入力フォルダとSSD上の出力先を選び、事前走査後に変換します。入力ファイルは削除・上書きされません。

Spotifyは従来の手動録音、Standby、自動停止を利用できます。Qobuzではこれらの手動UIを使用せず、プレイリストジョブが曲ID、曲頭、停止、次曲遷移を監視します。ジョブはソースレートごとにまとめて実行しますが、SQLite、FLACタグ、M3U8には元プレイリスト順を保持します。

### 録音時の判断

- Spotify: Offline専用です。ダウンロード品質がLossless候補で、Spotifyの`File > Offline Mode`がONと確認できた場合だけ開始できます。ただし配信版は販売用・CD・DJ向けファイルとマスタリングが異なることがあります。
- Qobuz: Offline専用です。完全ダウンロードとアプリ側の品質条件を確認できた場合だけ開始できます。ローカル証跡を取得できない場合、手動入力での代替やStreaming録音は行いません。
- DJ用途: 曲間のLUFS差やマスタリング差は、録音不良ではありません。DJ版もLUFS正規化、EQ、コンプレッサーは行わず、True Peak安全減衰だけを適用します。音量差はTraktor Auto Gainとミキサーのヘッドルームで非破壊管理してください。

## Soundiizを使った他サービスとの連携

[Soundiiz](https://soundiiz.com/)は、異なる音楽サービス間でプレイリストや対応するお気に入り情報を移すサービスです。本アプリが直接監視・録音できるのはSpotify DesktopとQobuz Desktopだけですが、Soundiizを入口にすると、Apple Music、YouTube Music、Amazon Music、TIDAL、Deezer、SoundCloud、Beatport、Beatsource、Bandcamp、Plex、Jellyfin、Navidromeなどの選曲情報をSpotifyまたはQobuzへ移してから、本アプリのOffline録音フローへ渡せます。対応先は随時変わるため、全サービスとPlaylists/Albums/Artists/Tracksごとの読み書き可否は[Soundiiz公式の互換表](https://soundiiz.com/features)を確認してください。

```text
各音楽サービスのプレイリスト／お気に入り
                  ↓ Soundiizでカタログ照合・コピー
       SpotifyまたはQobuzのプレイリスト
                  ↓ 移行結果を目視確認して完全ダウンロード
       Spotify/Qobuz DesktopのOffline再生
                  ↓ 専用2ch仮想オーディオ経路
              Hi-Res Recorder
```

Soundiizが移すのは曲名、アーティスト、アルバムなどのメタデータです。MP3、FLACなどの音声ファイル、配信ビット深度、サンプルレート、ラウドネス、TraktorのCue/Gridは転送しません。転送元がLosslessやHi-Resでも、その音質や同じ音源が移行先へ引き継がれるわけではありません。実際に録音される音は、移行先のSpotify/Qobuzで選ばれた版と、そのサービス側の配信品質・契約・地域カタログで決まります。[Soundiiz公式も音声ファイルではなくメタデータを照合すると説明しています](https://support.soundiiz.com/hc/en-us/articles/360009509574-How-to-download-export-audio-files-to-my-device)。

### サービス別の使い分け

| 転送元 | Soundiizで行うこと | 本アプリで行うこと |
|---|---|---|
| Spotify | Qobuzへ移す場合だけ使用。Spotifyで録音するなら転送不要 | Spotify側で対象を完全ダウンロードし、Offline ModeをONにして録音 |
| Qobuz | Spotifyへ移す場合だけ使用。Qobuzで録音するなら転送不要 | Qobuz側で最高品質を完全ダウンロードし、プレイリスト事前検査後に録音 |
| Apple Music / TIDAL / Deezer / Amazon Music | プレイリスト等をSpotifyまたはQobuzへコピー | 移行先で各曲を再確認・完全ダウンロードしてから録音 |
| YouTube Music / YouTube / SoundCloud / Audiomack | 動画題名や不完全なメタデータを含みやすいため、転送後の照合を特に厳格に確認 | 誤ったカバー、ライブ版、リミックスを修正してから録音 |
| Beatport / Beatsource / Bandcamp / Discogs等 | 対応している項目だけを移行。購入ファイルそのものは転送されない | 購入済み原本がある場合は、再録音より原本利用を優先 |
| iTunes / Plex / Jellyfin / Navidrome / Subsonic / Emby等 | ローカル／自己管理ライブラリの選曲情報を対応範囲内で移行 | 元のローカル原本がある場合は、本アプリで再録音しない |

音質と配信形式の証跡を優先する移行先はQobuzです。Qobuzでは完全ダウンロード、曲ID、サンプルレート、bit深度を本アプリが確認できます。Spotifyはカタログ上の代替候補として有用ですが、実効bit深度と配信原本とのbit一致を確認できないため、品質保証段階はQobuzより低くなります。

転送後は必ずSoundiizの結果と移行先プレイリストを照合してください。サービス間ではカタログ、地域ライセンス、メタデータが異なり、同じ曲名でもライブ版、Remaster、Radio Edit、Clean/Explicit、カバー、別リミックスへ置換されたり、曲が見つからなかったりします。[Soundiizの誤マッチ対策](https://support.soundiiz.com/hc/en-us/articles/32567703886098-Wrong-Track-Matches-Causes-How-to-Fix-Them)も参照し、可能ならアルバム名、バージョン表記、収録時間、ISRCを確認してください。正しい曲でも移行先では別マスターの可能性があり、24-bit/48kHzへ統一しても音圧、EQ、ダイナミクスは一致しません。

### Soundiizプランによる違い

| Soundiizプラン | Soundiiz側の主な範囲 | 本アプリへの影響 |
|---|---|---|
| Free | プレイリストを1件ずつ、1プレイリスト最大200曲。Syncは1枠。Soundiiz上へ保存できるコレクション情報はプレイリスト中心 | 録音機能、音質、品質ゲートは有料プランと同じ。大量移行では分割と手動確認・手動ダウンロードが増える |
| Premium | Soundiiz側の件数制限なしの一括転送、Batch操作、お気に入りのAlbums/Artists/Tracks管理、20 Sync枠 | 録音処理はFreeと同じ。大規模ライブラリを移しやすいが、同期後の誤マッチ確認とOfflineダウンロードは必要 |
| Creator | Premiumの全機能、50 Sync枠、追加Sync枠、Smartlinksの高度な機能 | 録音品質の向上や対応録音サービスの追加はない。多数のプレイリストを継続同期する運用以外では本アプリ上の利点はない |

プラン情報は変更される可能性があるため、契約前に[Soundiiz公式料金表](https://soundiiz.com/pricing)を確認してください。Soundiizのプランは本アプリへログイン連携されず、本アプリはプラン種別を検出しません。どのプランでも、入力デバイス、Unity Gain、WAV/FLAC形式、LUFS/True Peak解析、異常検出、Spotify/QobuzのOffline品質ゲートは変わりません。

Soundiizの有料契約はSpotify/Qobuzの再生契約を含みません。移行先でOffline再生と必要な音質を使える契約を別途用意してください。Auto Syncも音源をダウンロードしないため、同期で追加・置換された曲は移行先アプリで内容を確認し、完全ダウンロードが終わってから録音します。Sync実行中や転送結果未確認のまま録音を始めないでください。

## Spotify Offlineモード

Spotifyのメニューバーにある`File > Offline Mode`のチェック状態をmacOSアクセシビリティ経由で確認します。OFF、Spotify未起動、確認権限なし、メニュー構造未対応のいずれでも録音開始を拒否します。回線実測、通信量監視、Streaming品質設定は使用しません。

Spotifyの非公開prefsからダウンロード品質、音量の均一化、Automixを読み取ります。これらは設定条件の証跡であり、現在の曲のデコード済みサンプルと配信原本のbit一致を証明するものではありません。

## Qobuz Offlineモード

Qobuz Desktopのローカル状態、SQLite、ログを読み取り専用で監視します。Qobuzの録音経路はOfflineプレイリスト固定で、Streamingへの切り替えや手動証跡による品質ゲートの迂回はできません。認証情報、非公開API、Qobuz Connect、暗号化キャッシュ、復号鍵にはアクセスしません。

Qobuz Desktop 8.2系では`playqueue.data.currentIndex`から現在曲を特定し、DB内の`44.1`、`96.0`等のkHz表記を`44100Hz`、`96000Hz`へ変換して扱います。キュー履歴や次曲を現在曲として誤認しないよう、現在曲ID、完全DLレコード、再生ログの対応を確認します。

Qobuzのformat IDは「最大96kHz」等の配信品質枠であり、その曲の実サンプルレートそのものではありません。アプリでは品質枠とDBで確認した実レートを分けて表示し、例えば24-bit/44.1kHz曲を96kHz音源とは扱いません。

録音前にプレイリスト全曲の完全ダウンロード、曲ID、配信形式、実サンプルレート、bit深度、2chを走査します。さらに録音時に音量100%、ミュートOFF、Qobuz出力先と録音入力の一致を確認します。16/24-bitおよび44.1/48/88.2/96/176.4/192kHzを受け入れ、1曲でも未完了なら開始を拒否します。

macOS版では排他制御の有無を品質条件や証跡にしません。代わりに、専用2ch仮想デバイス、実レート一致、他アプリを通さない経路を検証します。ジョブ開始時にCoreAudioの既定入力、対象デバイスのレート、Qobuzの出力先と再生状態を保存し、正常終了・中止・異常終了後に復元します。Qobuzの非公開設定ファイルは変更せず、公開URLスキームとmacOS Accessibilityだけで操作します。Accessibility要素の探索、スクロール、クリックはHi-Res Recorder本体から直接実行し、別途`osascript`へ操作権限を与える設計にはしていません。

実行時は曲を実レート昇順にまとめ、各曲の再生前に専用Loopback/BlackHoleをソースレートへ同期します。再生開始後にレートが変わった場合は録音を中止します。24/44.1を48kHzで直接録音するのではなく、44.1kHzのまま記録してからDJ版だけSoXR VHQで24/48へ変換します。各曲は最大2回再試行し、重大な疑義が残る曲は削除せず隔離状態にします。

Qobuz公式資料:

- https://help.qobuz.com/en/articles/10139-what-is-in-the-streaming-catalogue
- https://help.qobuz.com/en/articles/10137-in-what-quality-can-i-listen-to-music-in-offline-playback

## 厳格録音経路

`Spotify/Qobuz Offline -> 単一2ch LoopbackまたはBlackHole -> Hi-Res Recorder`を使用します。Spotifyは44.1kHz入力を要求します。QobuzではCoreAudio Device UIDとnominal sample rateを照合し、Aggregate Device、Multi-Output Device、レート不一致、非ステレオ、途中のレート変更を拒否します。

Qobuzは音量100%、最高配信品質を使用してください。異なるサンプルレートの曲は曲ごとに入力レートを同期して録音します。24/96や24/192も録音中は元レートを維持し、録音完了後にDJ版だけ24/48へ変換します。

## ライブラリ一括変換

「録音/出力 > ライブラリ変換」は、サーバー等から手動ダウンロード済みのローカルフォルダを、新しいTraktor用24-bit/48kHz FLACコレクションへ変換します。入力ツリーの相対フォルダ構造を維持し、入力ファイルを削除・上書きしません。接続中なら`/Volumes/Go SSD/DJ Library 24-48`を初期出力候補にします。

- WAV、AIFF、FLAC、ALAC、AAC/M4A、MP3、Ogg Vorbis、Opusを通常音源として走査
- 48kHz以外はfloat64・SoXR VHQ・Linear Phaseで48kHz化し、48kHz入力はSRCをバイパス
- 16-bit以下はSRCや安全減衰後もディザなし。20/24-bitのDSP経路とfloat PCMの最終量子化だけTPDFを1回適用
- 変換後True Peakが-1dBTPを超える場合だけ線形減衰し、LUFS正規化、リミッター、EQは使用しない
- ソース形式、Lossless判定、レート、深度、SRC、ディザ、True Peak、SHA-256をFLACタグとSQLiteへ保存
- 完全一致するファイルまたは復号PCMだけ重複扱いにし、別マスターや編集版は自動統合しない
- NI Stem、DSD、DRM、破損音源、3ch以上は暗黙変換せず理由付きでスキップ
- SSDへ10%または50GiBの大きい方を空き容量として残し、`.partial`を検証してから確定
- 事前走査で総曲数、変換不能数、総再生時間、形式、入力レート、bit深度、保守的な出力容量上限を集計

MP3、AAC、Ogg等をFLACへ変換しても失われた情報は復元しません。新しいコレクションの形式と実行時サンプルレートを統一するための処理であり、タグには`SOURCE_LOSSLESS=NO`を残します。変換キューは一時停止、再開、失敗項目の再実行に対応します。過去ジョブはタブ上部から選び直せます。履歴DBは外付けexFATではなくMac内蔵ディスクへ保存します。

## Traktor Pro 4

TraktorのAudio Setupは48kHzに設定します。バッファは256 samplesを基準とし、LOADメーター上昇やドロップアウトがあれば512へ上げてください。録音DJ版とライブラリ変換出力はすべて24-bit/48kHzなので、Traktor内のリアルタイムSRCを避けられます。新SSDの出力フォルダをMusic Folderとして新規登録し、BPM、Grid、Key、Gainは最初から解析してください。旧`collection.nml`は本アプリから変更しません。

Auto GainはON、Mixer Headroomは-6dBを基準にします。Spotify版とQobuz版は配信マスターが異なる可能性があり、24/48へ形式を統一してもLUFS、EQ、ダイナミクスの差は消えません。

## 履歴と復旧

保存曲、サービス、Qobuzプレイリスト/曲ID、元順序、レート別実行順、ソース形式、LUFS、Peak、True Peak、品質合否、疑義時刻、再録要否をSQLiteへ保存します。管理画面は録音履歴、FLAC出力、Qobuzジョブ、ライブラリ変換の4タブで、Archive/DJ、入出力レート、SoXR品質、Linear Phase、ディザ方式と理由、安全減衰量、拒否理由、ジャケット、WAV削除結果、一括変換状態を確認できます。

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
dist/Hi-Res\ Recorder.app/Contents/MacOS/HiResRecorder --self-test-library-codecs
```

出力は`dist/Hi-Res Recorder.app`です。自己診断は配布アプリ内のPyAV/FFmpegがAAC、ALAC、FLAC、MP3、Vorbis、Opusを復号可能か確認します。

このアプリは開発途上です。Qobuz DBとAccessibility UIは公式の外部連携APIではなく、アプリ更新で構造が変わる可能性があります。fixtureによる自動テストだけで本番品質を保証せず、使用中のQobuz版と専用デバイスで44.1/48/96/192kHzの短い実機試験、設定復元、再生、品質レビューを確認してからDJライブラリの本番作成に使用してください。
