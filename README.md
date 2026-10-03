# vp-manager

テキストやPowerPointのノートから、VOICEPEAKのナレーションと静止スライド動画を作るローカルPythonプログラムです。Skillが読み方を判断し、本体が原稿の保全、辞書の試用と確定保存、音声生成、検査と再開を担当します。

2026-10-03にVOICEPEAK 1.2.23で検証しました。**確認用WAV・MP4を生成でき、聴取済みの新規語を共通辞書へ保存できます。未知の音声の自然さを無人で合格判定する機能と、アニメーション再現は未実装です。**

- [実装と実機検証の結果](docs/implementation-validation.md)
- [追加実装とPowerPoint実機検証](docs/continuation-validation.md) / [独立レビュー](docs/continuation-review.md)
- [聴取済み音声の参照・評価](docs/references.md)
- [制作Skill](.agents/skills/voicepeak-production/SKILL.md)
- [読み・品詞・アクセントの判断JSON](docs/decisions.md)
- [技術検証](docs/technical-validation.md) / [実装計画](docs/implementation-plan.md)

## Homebrewでインストール

v0.1.0のHomebrew配布はmacOS 14以降のApple Silicon Macを対象にしています。アクティベーション済みのVOICEPEAKを別途インストールしてください。VOICEPEAK本体、ボイスのライセンス、ASRのモデルは同梱しません。

```sh
brew tap rioriost/tap
brew install vp-manager
vp-manager --version
vp-manager install-skill
```

Python本体とChatGPT用Skillは同じパッケージに含まれます。`install-skill`が、このMacのアプリへSkillを登録します。Homebrewのインストールだけではユーザーのアプリ設定を変更しません。別途`rioriost/skills`から取得する必要はありません。

ローカルのプラグインに対応したCLIがあれば正規のプラグイン登録を使い、なければローカルSkillとして配置します。結果のJSONに登録方式と次の操作が表示されます。次のメッセージまたは新しいチャットで使い、一覧へ反映されない場合はアプリを再起動してください。実行にはこのMacへアクセスできるChatGPTのローカル作業環境が必要です。

導入後はVOICEPEAKのGUIを閉じて、利用可能な話者と依存関係を確認します。

```sh
vp-manager doctor --json
```

Python 3.12、音声・ASR用ライブラリ、FFmpeg、PopplerはHomebrew側で導入します。ASRはApple Silicon上のMLX Whisperとローカルに取得済みのモデルを使い、`verify --asr-model /absolute/local-model`で明示します。実行時にモデルを自動ダウンロードしません。

更新時は本体の更新後にSkillも登録し直します。管理外の既存ファイルや、利用者が編集したSkillを上書きする場合は停止します。

```sh
brew update
brew upgrade vp-manager
vp-manager install-skill
```

開発用にソースから導入する場合は、Python 3.12とuvを用意します。

```sh
git clone https://github.com/rioriost/vp-manager.git
cd vp-manager
brew install ffmpeg poppler libsndfile
uv sync --locked --all-extras
uv run vp-manager install-skill
```

GUIとの競合を検出した場合、本体は停止し、VOICEPEAKを強制終了しません。`doctor`は聴取品質の合格判定ではありません。

## Skillから使う

登録した「VOICEPEAK音声制作」を利用するローカルエージェントに、例えば「このPPTXのノートから読みを調整して確認用動画を作って」と依頼します。Skillは本体CLIを呼び、判断をJSONで渡します。通常のクラウドチャットからMacのCLIを直接動かす機能は提供しません。

原本のスライドとノートは変更せず、読み上げ用コピーに補正を適用します。AIへ原稿を渡す際は、そのホストがクラウドで処理する範囲に注意してください。合成とASR自体はローカルです。別のAI APIキーは必要ありません。

## CLIで使う

新しいジョブディレクトリに原文、判断、各候補音声、検査結果を保存します。原文のファイルとテキスト直接入力を選べます。

```sh
vp-manager analyze input.pptx --job artifacts/job-001 --json
vp-manager analyze --text '桜餅を確認します。' --job artifacts/text-001 --json
```

候補がある場合は終了コード4、`needs_decision`を返します。`job.json`の原文・候補・`source_revision`を読み、[判断JSON](docs/decisions.md)を作ります。判断は毎回全件を渡します。

```sh
vp-manager apply-decisions artifacts/job-001 --file decisions.json
vp-manager render artifacts/job-001
vp-manager verify artifacts/job-001 --asr-model /absolute/local-whisper-model
vp-manager assemble artifacts/job-001 --allow-draft
```

`verify`は音響、ASR、聴取受入れを分けて記録します。ASRが一致していても聴取未確認なら`needs_revision`と`draft`です。これは合成失敗ではありません。未確認音声の結合には`--allow-draft`が必要です。実際に聴いた人の受入れは`accept-review`で音声、表記、辞書適用後の読み、生成条件に紐づけます。架空の受入れを記録しないでください。

```sh
vp-manager accept-review artifacts/job-001 --chunk c0001 \
  --reviewer '確認者' --note '実際に聴取して確認した内容'
vp-manager status artifacts/job-001 --json
vp-manager resume artifacts/job-001
```

`resume`は入力・音声・エンジン・辞書の整合性を確認し、必要な音声生成を再開します。変更されていない完了区間は再生成しません。異常終了・強制中断した同じ入力は自動反復せず、調査か修正を要求します。QA・結合・出力は状態に応じて続けます。常駐デーモンではありません。

速度・全体ピッチ・休止時間などは、JSONを`configure JOB --file options.json`へ渡して変更できます。

```json
{"speed": 110, "pitch": 0, "paragraph_pause": 0.4, "slide_lead": 0.25, "slide_tail": 0.5}
```

速度は50〜200、ピッチは-300〜300。休止は元の音声へ追加する秒数です。息や子音を削る無音除去は行いません。

## 静止スライド動画

PowerPoint本体の描画を使う場合は、`prepare-slides`で原本を保持した描画用コピーを作ります。コピーだけ非表示スライドを一時表示にし、ノートや見た目は変更しません。

```sh
vp-manager prepare-slides artifacts/job-001
```

返された`render_copy`をPowerPointで開き、PDFへエクスポートします。macOS版でローカル処理にする場合は「印刷に最適」を選び、Microsoftオンラインサービスを使う選択肢を避けます。通常の原本からの書き出しは非表示スライドを除外するため、このコピーを使ってください。PDF書き出し自体はPython内に組み込まず、ホストの画面操作または利用者が担当します。

表示・非表示を含む全スライドを原順序で収めたPDFがあれば、次のように渡せます。音声は非表示スライド分も生成します。動画への収録は表示スライドが既定で、`--include-hidden`で変更します。空ノートは既定で3秒の無音表示です。

```sh
vp-manager export-video artifacts/job-001 \
  --pdf /absolute/all-slides.pdf --allow-draft
```

Codex同梱のLibreOfficeから直接描画する経路もあります。実行パスは`load_workspace_dependencies`で確認し、`--soffice`へ絶対パスを渡します。日本語が欠ける環境では`--font-dir`を繰り返して、ローカルのフォント探索先を指定します。

```sh
vp-manager export-video artifacts/job-001 \
  --soffice /absolute/bundled/soffice \
  --font-dir /System/Library/Fonts \
  --font-dir /Library/Fonts \
  --font-dir '/Applications/Microsoft PowerPoint.app/Contents/Resources/DFonts' \
  --allow-draft
```

フォントは変換プロセス内で参照し、コピーやインストールはしません。検証資料ではPowerPoint本体と文字の太さや間隔に差がありました。元の描画を重視する場合はPowerPointのPDFを使います。動画には`visual_review: required`を残し、音声の受入れ済みでも動画全体を自動で`verified`にしません。アニメーション・埋め込み音声や動画は再現せず警告します。

## 辞書と中断時の回復

生成時は新規語を一時追加します。既存語を上書きせず、バッチ完了後に`dic.json`、`user.dic`、`user.csv`の内容・属性を復元します。バックアップと復元記録は`~/.local/state/vp-manager/dictionary-transactions/`へ保存します。原稿・辞書を含むため外部へ公開しないでください。

新規語が実際に使われた全区間を聴取し、`accept-review`で受け入れた後、共通辞書へ確定保存できます。

```sh
vp-manager promote-dictionary artifacts/job-001
```

音声・読み・生成条件・元辞書を再照合して、新規語だけを`dic.json`へ保存します。未使用語や未承認区間がある場合は保存しません。派生辞書は次のVOICEPEAK実行時の再生成に任せます。保存完了の記録を残し、完了後の再実行で後から追加された語を消しません。従来のジョブは`resume`で不足する証跡を補い、`verify`と聴取確認をやり直します。

強制終了後の復元は次で確認できます。

```sh
vp-manager recover-dictionary --json
```

外部変更や所有権不明の変更がある場合は`needs_recovery`で停止し、現状とバックアップを保持します。古い辞書を機械的に上書きしません。確定保存の成功経路と中断復旧は一時辞書で検証済みです。実辞書への初回保存は、実音声の聴取記録を得てから行います。

## 検証

```sh
uv run pytest -q
uv run ruff check src/vp_manager tests
uv build
```

テストは模擬エンジンと一時辞書を使い、実VOICEPEAKや利用者の辞書を変更しません。FFmpegが利用できる環境では動画の実エンコードも検査します。実機検証の音声・原稿・ログはGit対象外の`artifacts/`にあります。

終了コードは、0が工程成功、2が入力不備、3が環境・競合、4が判断待ち、5が合成失敗、6が回復待ち、130が中断です。0と品質合格は同じではありません。JSONの`quality`、`issues`、`next_action`も確認してください。

## ライセンス

Python本体とSkillは[MITライセンス](LICENSE)です。依存ライブラリ、VOICEPEAK、ボイス、フォント、ASRモデルには各提供元の条件が適用されます。[第三者コンポーネント](THIRD_PARTY_NOTICES.md)を参照してください。
