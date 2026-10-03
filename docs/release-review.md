**v0.1.0 配布レビュー：公開前のローカル検証完了**

2026-10-03。HomebrewによるPython本体の導入、同梱Skillの登録、MITライセンス表記を独立に確認した。対象はApple Silicon、macOS 14以降、CPython 3.12。公開・タグ作成・GitHub Release・実際の利用者設定への登録は統合担当が行う。この文書だけで公開完了を示すものではない。

**指摘と修正**

| 確認事項 | 修正・検証 |
|---|---|
| 依存ロックが組織用ミラーを参照していた | 公開PyPIのメタデータで再解決し、公開URLとSHA-256を固定した |
| 開発端末の絶対パスが実装・検証手順に残っていた | 実行時のホーム・明示パスと一般化した手順へ置き換え、公開対象を再検索した |
| 従来のwheelにSkill資産がなく、Skillはソース配置を前提にしていた | Skill、参照文書、ライセンスを同梱し、PATH上のCLIを優先する方式へ変更した |
| PyMuPDFのライセンスと配布範囲が曖昧だった | PDF処理を外部Popplerへ移し、PyMuPDFは開発用のみに限定した |
| Skill更新中の外部編集や別プラグイン登録が失われる可能性があった | 置換直前に所有記録と全ファイルを再確認し、登録一覧も再読込して併合する |
| MLX Whisper 0.4.3のwheelにMIT本文がなかった | 公式Apple/OpenAIのMIT通知全文を`THIRD_PARTY_NOTICES.md`へ補完した |
| HomebrewのMach-O書換えがヘッダー領域不足で中断し、MLXの署名が不整合になった | `preserve_rpath`で既存の短いIDを保持し、新規導入・署名検証・ASRの成功を確認した |

本体とSkillのMIT表記は自作部分に適用する。[PyMuPDFの公式説明](https://pymupdf.readthedocs.io/en/latest/about.html#license-and-copyright)はAGPLと商用ライセンスを区別している。配布するSoundFileはpure wheelを選び、別のHomebrew依存としてlibsndfileを導入する。SoundFileとlibsndfileのライセンスを同一視しない。[SoundFileの説明](https://python-soundfile.readthedocs.io/en/latest/#installation)

依存wheelは改変せず、NumPy、PyTorch、llvmlite、MLXなどのライセンス資産も削除しない。MLX Whisperの不足分には、[MLX ExamplesのMITライセンス](https://github.com/ml-explore/mlx-examples/blob/main/LICENSE)と[OpenAI WhisperのMITライセンス](https://github.com/openai/whisper/blob/main/LICENSE)をそのまま追加した。VOICEPEAK、音声資産、フォント、ASRモデルは配布物へ含めない。

**依存と配布物の照合**

公開ロックの生成では、PyPI APIから得た各版の依存情報を変更せず、一時的なresolver入力として使用した。統合担当は一時入力の記録だけを除去し、パッケージグラフが変わっていないことと、通常の`uv lock --check --offline`の成功を確認した。

独立検査では、本体と依存39件、計40個の実wheelについて、固定SHA-256、パッケージ名、版、Python要件を照合した。wheel内部のMETADATAから、Darwin・arm64・Python 3.12で有効な依存とextrasを再帰的にたどった。53本の依存関係がすべて選択済みの版で満たされ、未取得の依存も不要な選択もなかった。この結果はロックの記載だけに依存しない。

この環境ではPyPIのファイル配信先への直接接続に失敗したため、検査用wheelの取得に組織ミラーを利用した。取得したバイト列は公開PyPIが示すSHA-256と一致する。公開Formulaには公開URLだけを記載するが、今回の検査を公開配信先からの直接取得成功とは扱わない。

Formulaは固定URL・SHA-256のresourceを使い、pipへ実wheelを指定する。`PIP_NO_INDEX`、`PIP_NO_DEPS`、ビルド分離の無効化により、取得済み資産からの導入時に依存解決や追加取得を行わない。Homebrew管理領域に仮想環境を作り、公開コマンドだけをリンクする。これは[Homebrewの言語別配布方針](https://docs.brew.sh/Language-Specific-Formulae)に沿う構成である。

検査時のwheelは29項目で、同梱Skill資産7件と自作ライセンス・第三者通知を含む。各Pythonファイルは作業ツリーと一致した。wheelと公開予定のソースを検索し、実機成果物、仮想環境、端末固有パス、組織ミラーURL、代表的な秘密鍵・トークン形式の混入は見つからなかった。MIT通知を追加した最終wheelも再検査し、ソース・通知本文・Formulaのハッシュ一致を確認した。ソースアーカイブは生成後に別途照合する。

**Skill登録と復旧**

Homebrewの導入中には利用者の設定を変更しない。`vp-manager install-skill`を明示的に実行したときだけ、管理対象のコピーを利用者領域へ置く。dry-runは書込みもCLIプローブも行わない。Codexの対応CLIを使える場合はその登録処理へ委ね、未対応の場合は自己完結したstandalone Skillを置く。CLIの有効化に失敗した場合は、成功とせず`pending_activation`を返す。

同名の既存ファイルを、名前だけで所有物とみなさない。所有記録、内容ハッシュ、追加ディレクトリ、シンボリックリンクを検査し、不明・編集済みの資産を保持して停止する。段階的な書込みとバックアップからの復旧を行い、更新で消えるCellarの版別パスは埋め込まない。

登録実装とFormula生成のテスト33件を独立実行し、すべて成功した。別途、一時ディレクトリで更新中の外部編集、バックアップへの移動直後の中断、コピー中の別プラグイン追加、dry-runの無書込みを検査した。編集内容と別登録を保持し、中断後の再実行も成功した。実装担当はソース外の新規仮想環境でもwheelの資産参照とstandalone登録を確認している。PDF処理変更後のmediaテスト15件も独立に成功した。

**統合担当の実機確認**

初回のHomebrew導入では、tiktokenの再配置処理がMach-Oの予約領域を超えて失敗した。中断後のMLXには無効な署名が残り、ASRプロセスが終了した。FormulaにHomebrewの`preserve_rpath`を指定し、既存の`@rpath`から始まるIDを保持するよう修正した。他のリンク処理は有効なままである。

修正後の新規導入は正常終了し、`brew test`も成功した。導入先の`pip check`は依存不整合なし、MLXライブラリのコード署名検証も成功した。Homebrewで導入したCLIと既存のローカルモデルを使うASRは正常終了し、検証音声の「音声の検証です。桜餅を確認します。」を認識した。レビュー担当も導入ログ、機能試験ログ、ASR結果を確認した。

実際のホームへの`install-skill`は、公式Codex CLIを通じて`status=installed`、`method=codex_plugin`となった。同じ作業中に作ったソース参照型の登録を、登録一覧を保持したうえで所有記録のあるコピーへ移した。全242テストと公開スクリプトを含むRuff検査も統合担当が実行し、成功した。

公開前のローカル検証に未修正の阻害事項はない。最終ソースアーカイブと公開Releaseの資産照合は、この記録とは別の公開工程で行う。一般的な韻律・自然さの自動合格や、未聴取の辞書項目の永続登録を、この配布検証から保証しない。
