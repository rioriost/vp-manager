# Changelog

## 0.1.0 — 2026-10-03

- テキスト・テキストファイル・PPTXノートから制作ジョブを作成するCLI。
- 原文を保持した読み補正、140文字以内の分割、段落ごとの休止、VOICEPEAKの逐次実行と再開。
- 読み・品詞・アクセントの判断JSON、辞書の一時適用、聴取確認を条件とした共通辞書への保存。
- 音響検査、ローカルMLX WhisperによるASR、音声と生成条件に結び付けた聴取記録。
- WAV結合と、全スライドPDFからの静止スライド動画出力。
- Apple Silicon Mac向けHomebrew Formulaと、同梱Skillを登録する`install-skill`。

自然さの無人合格判定とPowerPointアニメーションの再現は対象外です。VOICEPEAK、ボイスのライセンス、ASRモデルは別途必要です。
