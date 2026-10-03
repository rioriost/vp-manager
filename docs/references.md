# 聴取済み音声を再利用する

同じ原稿・設定で同じ音声ができた場合に、記録済みの聴取結果を再利用します。音声、読み、文脈、生成条件のどれかが変われば照合は `unknown` となり、改めて確認が必要です。新しい音声の自然さを推定する機能はありません。

CLIでジョブを生成・検査した利用者向けの手順です。通常の利用は次の「保存と再利用」で足ります。手動の評価データを扱う場合だけ、後半のJSON形式を参照してください。

## 聴取結果を保存し、次の検査で照合する

実際に聴いて確認した区間に限り、確認者と根拠を記録します。次の `--note` は書式例なので、実際の聴取結果に置き換えてください。

```sh
uv run vp-manager verify JOB --json
uv run vp-manager accept-review JOB --chunk c0001 \
  --reviewer '確認者名' --note '実際に確認した読み・アクセントと判断理由' --json
uv run vp-manager export-references JOB --corpus .local/references --json
uv run vp-manager reference-list --corpus .local/references --json
uv run vp-manager verify NEXT_JOB --corpus .local/references --json
```

`export-references` は、ジョブに明示的な聴取記録があり、現在の音声についてQAが `pass`、聴取結果が `accepted` の区間だけを保存します。未確認・不合格の区間は保存対象から外します。保存件数と除外理由は `JOB/job.json` の `reference_export` に記録されます。

`verify --corpus` は、音声ファイルのSHA-256、入力文、辞書から求めた想定読み、話者、速度、ピッチ、感情、エンジン版、音声資産、辞書、分割仕様、原稿内の文脈を照合します。すべて一致した `accepted` の記録だけを再利用し、現在の音響検査も実施します。結果は `job.json` の `reference_matches` で確認できます。手動の聴取記録を上書きしません。

指定したコーパスの場所はジョブに保存され、その後の検査でも使われます。コーパスとは、音声のコピーと、それを誰がどう評価したかを保存するローカルの記録集です。

## ラベル付きの音声を取り込み、照合結果を評価する

```sh
uv run vp-manager reference-import --file examples.json --corpus .local/references --json
uv run vp-manager evaluate-references --file evaluation.json --corpus .local/references --json
```

`reference-import` は確認者が付けたラベルを登録します。`evaluate-references` は評価用JSONと既存コーパスを照合するだけで、ラベルを登録しません。音声WAVはJSONと同じディレクトリに置き、`audio` にはファイル名だけを書きます。

JSONは次の形式です。山括弧の値は説明用で、そのままでは入力できません。`provenance` はPython APIの `reference_provenance(job, chunk)` が返す値を使い、推測で埋めないでください。

```json
{
  "schema_version": 1,
  "examples": [
    {
      "id": "sample-001",
      "audio": "sample-001.wav",
      "sha256": "<WAVのSHA-256、64桁の小文字16進数>",
      "expected_text": "桜餅を確認します。",
      "provenance": {
        "engine": "1.2.23",
        "assets": "<音声資産の識別ハッシュ>",
        "dictionary": "<使用辞書の識別ハッシュ>",
        "split": 1,
        "narrator": "Japanese Female 1",
        "speed": 100,
        "pitch": 0,
        "emotion": {},
        "context": "<原稿と区間の文脈ハッシュ>",
        "content_expected": "桜餅を確認します。"
      },
      "label": "uncertain",
      "reviewer": "<確認者名>",
      "note": "<聴取結果と判断理由>"
    }
  ]
}
```

`label` は `accepted`（聴取で採用）、`rejected`（聴取で不採用）、`uncertain`（判断保留）のいずれかです。同じ音声・文・生成条件・文脈に異なるラベルを登録すると停止します。`uncertain` は自動採用されません。

評価結果の `correct_accepts`、`correct_rejects`、`false_accepts`、`false_rejects` は、登録済みの判断と評価用ラベルの対応件数です。`unknown` は判断を保留した件数、`exact_in_corpus_matches` は同一の記録が見つかった件数です。登録した音声そのものを評価すれば一致するため、その結果から未知音声の判定精度は分かりません。出力は常に `calibrated: false` と `naturalness_inference: false` を含みます。

## JSONの状態と終了コードを別々に確認する

| 終了コード | 意味 |
|---|---|
| `0` | コマンドが完了。評価結果が合格という意味ではない |
| `2` | 入力スキーマ・引数などの不備 |
| `3` | 環境不備またはロック競合 |
| `4` | 判断や再検査が必要。未確認の音声が残る `verify` も含む |
| `6` | 音声・メタデータの変更などを検出し、復旧が必要 |

JSONの `status`、`issues` と、ジョブ内のQA・照合結果を確認してください。`evaluate-references` は不採用例との不一致や `unknown` があっても、評価処理が完了すれば終了コード `0` を返します。

## コーパスは署名のないローカル記録として扱う

コーパスには原稿、音声、確認者、聴取メモが含まれます。共有を意図しない限りGitや公開ストレージに入れず、専用ディレクトリに保存してください。本体はディレクトリを所有者だけが利用できる権限にし、ファイルロックと原子的な保存を使います。

索引、メタデータ、音声のハッシュを確認し、破損や食い違いを検出すると停止します。ただし、記録は暗号署名されていません。確認者の身元や、実際に聴いた事実を本体が証明するものではありません。この前提を `trust_model: local_review_records_not_signed` として出力します。

復旧が必要な場合はコーパスを上書きせず退避し、`reference-list --json` のエラーと該当ジョブの記録を確認してください。原因を確認できない記録を、手でハッシュだけ書き換えて採用し直さないでください。
