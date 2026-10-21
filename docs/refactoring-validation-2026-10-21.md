# リファクタリング実装・検証記録

日付: 2026-10-21 / 反映先: `main`（実装時の作業ブランチ: `refactor/civitai-trend-windows`）

設計・受け入れ条件: [リファクタリング計画](refactoring-plan-2026-10-21.md)。ローカル実装と生成物の検証を完了した。本書の検証はHugging Faceへの外部書込を伴わず行っており、GitHub Actionsの本番実行結果は含まない。

## 実装した内容

- 既存CLIを維持し、通信・保存と移行・収集管理・日時・分類・集計・品質・描画・一括出力へ責務を分離した。
- MOSS Image / MOSS Image Baseを統合し、注目系列として表示上限8件の枠内に確保した。表示順位は各期間で計算する。
- 全計測期間の週次SVG、直近90日・30日・14日の日次SVGを生成する。既存trends.svgは全期間図と同一バイト列。
- 公開日集計CSVと初観測日集計CSVを区別し、公開日欠損・不正値のIDとraw値、表示有無と理由、取得品質を診断CSVへ出力する。
- DB移行の順序とトランザクション、移行前バックアップ、初観測値の不変性、全期間再集計、在庫スナップショット保全を実装した。
- 実行完了と取得品質を分離した。verifiedには両種別の一日全体を覆う根拠が必要で、ページ上限や既知ID停止は証明にしない。
- 通常復元の失敗時は停止し、空の履歴で継続しない。公開は復元元revisionを期待親とする単一条件付きcommit。完全な送信バッチを保存して再送できる。
- パスの衝突、出力先によるDB・分類設定・無関係なファイルの破壊を防ぐ。生成物はLF固定とし、Gitの正規化後もmanifestのハッシュを維持する。
- 日次・週次workflowを更新し、Windows/Ubuntu、Python 3.10/3.12のテストmatrixを追加した。

## MOSSの実データ診断

公開Datasetの固定revision `bff1be282244f79642ba227f0f74d8a536d0da6b`を読み取った。圧縮snapshotは211,881,319 bytes、展開DBは1,043,398,656 bytes。

- 元DBに115,267 versionが存在し、MOSSはCheckpoint 29 / LORA 209の計238 version。MOSSのpublishedAt欠損は0。
- 元DB内の現在のraw分類は、過去CSVの分類内訳とずれていた。全期間の再集計で現在の保存値と出力を揃えた。
- 検証用DBの移行は約32.5秒。元の115,267 versionを保持し、115,191件の公開日を列へ補完した。公開日のない他系列は欠損として保持した。
- 実APIを各種別2ページ、計4ページだけ読み取った。400 model / 1,055 versionの応答を処理し、両種別の実行がcompletedになった。
- 元DBとの結合比較で、既存IDの欠落0、既存first_seen_at・first_seen_dateの変更0。新規IDはmodel 155 / version 258。検証用DBのversion総数は115,525。
- この限定走査後、MOSSはCheckpoint 30 / LORA 244 version。全計測期間の公開version順位は両種別とも13位で、従来の上位8件の表示から外れることを確認した。
- 新しい4図ではMOSSが各種別・各指標の計16パネルに表示される。

限定走査は完全取得の検証ではない。2026-10-21はpartial、根拠の不足する旧実行日はunknownとして表示した。未知の期間を確定0にしない。Civitai全体のモデル・version捕捉率は未測定。

## 生成物

基準日2026-10-21、生成時刻2026-10-21T10:33:56+00:00。同一DB・分類・表示設定から生成した。

| SVG | 開始日 | 終了日 | 集計単位 |
| --- | --- | --- | --- |
| [全計測期間](../data/exports/trends-all.svg) | 2026-05-18 | 2026-10-21 | ISO週 |
| [90日](../data/exports/trends-90d.svg) | 2026-07-24 | 2026-10-21 | 日 |
| [30日](../data/exports/trends-30d.svg) | 2026-09-22 | 2026-10-21 | 日 |
| [14日](../data/exports/trends-14d.svg) | 2026-10-08 | 2026-10-21 | 日 |

[manifest](../data/exports/export_manifest.json)のsnapshot_id:

```text
5a2ac2ea5ade0ad491631b33c99dffa4304767895217f7bae5a42b2affa1ad2a
```

日次CSVのversion件数合計115,525が検証DBと一致。manifest内の12ファイルのSHA256、GitによるLF正規化後の同じSHA256、2箇所の日次CSVの一致を確認した。

## 検証結果

```bash
python -B -m unittest discover -s tests -q
python -B -m compileall -q scripts tests
git diff --check
```

- 単体・回帰・CLI・一括出力・復元公開のテスト: **102件成功**。
- Python構文検査、差分の空白検査: 成功。
- workflow YAML、4SVGのXML: パース成功。
- 4SVGをPyMuPDFでPNG化して目視確認。専用色、凡例、期間、終端ラベル、欠測表示を確認し、長期図の点の密度と凡例の重なりを修正した。利用可能なブラウザがなかったため、実ブラウザでの描画確認は未実施。
- 旧schema、二度目の移行、失敗時ロールバック、将来schema拒否、破損DB、パス衝突、初観測不変、ページングと再試行、両種別完了、coverage区間、期間境界・年越し・閏日、公開日欠損、注目表示、再生成、原子的置換、競合、再送をテストした。
- 収集系と出力・公開系を別担当がレビュー。指摘を回帰テストとともに修正し、承認を得た。

## 公開前の状態と残る確認

完全な検証用送信バッチをローカルの`.verify/review-batch-lf`に保存し、`publish_prepared(..., dry_run=True)`が成功した。圧縮DB、全出力、manifest、期待親、個々のファイルと展開DBのハッシュを検査した。実Hubへのcreate_commitは呼んでいない。

- **実施済み:** P0〜P4の実装・ローカル検証、P5の公開コードと完全バッチのdry-run、P6のCI設定とローカル検証。
- **別途確認:** HFの本番反映、実際の条件付きcommit競合・Git push失敗の運用検証、push後のCI matrixのリモート実行。
- **検証環境:** ローカルPython 3.10.11 / Windows。Python 3.12とUbuntuは追加したCIで確認する。
- **静的検査の範囲:** compileallとレビューを実施。ruff/mypy等は未導入のため、正式なlint・型検査は未実施。新しい開発用依存は追加していない。
- **API上の制約:** Newestの並びをversion更新日時順とは仮定しない。非公開化・削除済み・取得範囲外を含む完全履歴の復元は保証しない。

公開と再送の手順は[Hugging Face運用手順](huggingface-dataset-setup.md)を参照。外部反映ではこの検証用の限定走査を完全取得として扱わず、通常の走査予算で収集して品質を記録する。
