# リリース手順

Homebrew Formulaは、Pythonの配布wheelと`uv.lock`で固定した依存wheelを導入する。Skill、参照文書、プロジェクトのライセンスを本体wheelへ含める。VOICEPEAK、ユーザー辞書、実原稿、音声、ASRモデルは配布しない。

1. `pyproject.toml`のバージョンとCHANGELOGを更新し、公開PyPIで`uv lock`する。
2. `python scripts/package_skill_plugin.py`でSkill資産を更新する。
3. `uv run pytest -q`、`uv run ruff check src tests scripts/package_skill_plugin.py scripts/prepare_homebrew_resources.py`とSkillの検証を行う。
4. `uv build --wheel`でwheelを作り、`python scripts/prepare_homebrew_resources.py --wheel dist/vp_manager-VERSION-py3-none-any.whl`でFormulaと依存マニフェストを生成する。
5. 別環境でwheelを導入し、同梱Skill、`--version`、`install-skill --dry-run`、Homebrewの機能試験、`pip check`、ローカルモデルによるASRを確認する。
6. Formulaを含むソースアーカイブを`uv build --sdist`で生成する。wheel・ソースの内容を検査し、SHA-256を記録する。
7. Gitのコミットとタグを作り、GitHub Releaseへwheel、ソースアーカイブ、`SHA256SUMS`を公開する。公開後に取り直して照合する。
8. `rioriost/homebrew-tap`の`Formula/vp-manager.rb`へ検証済みFormulaを反映し、公開する。公開URLからの導入と`brew test vp-manager`を確認する。

Formulaは取得後のpipへ`--no-deps --no-index`を渡し、追加の依存解決を行わない。対象はPython 3.12、macOS 14以降のarm64。更新時には依存wheelの対応プラットフォーム、実METADATA、第三者ライセンスも確認する。SoundFileは汎用wheelを使い、libsndfileはHomebrewの依存として導入する。

`brew install`中に利用者領域を変更する処理は置かない。Skillを登録・更新する利用者は、導入後に`vp-manager install-skill`を実行する。登録先の既存ファイルや別プラグインの設定を保持し、編集済み資産の上書きは停止する。
