# Hugging Face Datasetの運用

対象: [Shiba-2-shiba/civitai-model-version-trends](https://huggingface.co/datasets/Shiba-2-shiba/civitai-model-version-trends)。SQLiteを長期履歴として保持し、GitHubにはコードと比較的小さなCSV・SVGを保存する。

## 設定

GitHub repository secretの`HF_TOKEN`に、対象Datasetのread/write権限を持つトークンを登録する。値をコード・ログ・チャット・コマンド履歴へ書かない。ローカルでも環境変数を使う。

```bash
python -m pip install -r requirements-sync.txt
python scripts/sync_dataset.py restore
python scripts/collect_daily.py --skip-active-totals --fail-on-errors
python scripts/build_exports.py
```

Hub SDKは同期処理だけの依存で、動作対象を`huggingface-hub==2.2.0`へ固定している。収集・出力・単体テストには不要。更新時は[公式API](https://huggingface.co/docs/huggingface_hub/package_reference/hf_api#huggingface_hub.HfApi.create_commit)の`parent_commit`契約と同期テストを確認する。

## 復元と移行

復元時にDatasetの`main`のSHAを取得し、ファイル一覧とダウンロードを同じSHAへ固定する。`.verify/dataset_revision.json`が復元元の記録になる。

圧縮DBは一時ファイルへ展開し、SQLite integrity_checkと必須表を検査してから作業用DBへ置換する。トークン不足、認証・通信エラー、スナップショット不在・破損では通常実行を止める。空のDBに切り替えて続行しない。

旧DBの収集開始時には、移行前の兄弟ファイル`*.pre-migration-v0.bak`等を保存する。列追加、データ補完、インデックス作成はトランザクション内で行う。既存のIDとfirst_seenは維持し、欠損日付の推定補完はmigration_auditへ記録する。バックアップはGit管理しない。

初回だけは次を明示できる。

```bash
python scripts/sync_dataset.py restore --bootstrap
```

bootstrapはDatasetにスナップショット・CSV・exportsなどの既存履歴がなく、ローカルDBも存在しない場合に限る。既存データのリセット用ではない。Dataset自体の作成や削除はこのコマンドでは行わない。

## 同一バッチの公開

公開する構成:

```text
README.md
daily_counts/daily_counts.csv
exports/daily_counts.csv
exports/weekly_counts.csv
exports/monthly_counts.csv
exports/publication_daily_counts.csv
exports/base_model_audit.csv
exports/coverage_daily.csv
exports/series_diagnostics.csv
exports/trends-all.svg
exports/trends-90d.svg
exports/trends-30d.svg
exports/trends-14d.svg
exports/trends.svg
exports/export_manifest.json
snapshots/civitai.sqlite.gz
```

全期間は観測開始以降の週次、90日・30日・14日は日次。DatasetのREADMEにも期間メニューを生成する。観測日CSVと公開日CSVは異なる指標として説明する。

外部書込前に完全な送信バッチを作る。圧縮DB、CSV、SVG、manifest、生成README、期待親を記録した`.batch_receipt.json`を含み、GitHub Actionsはこの全体をartifactへ保存してから送信する。

```bash
python scripts/sync_dataset.py prepare
python scripts/sync_dataset.py retry --dry-run
```

外部書込前の検証:

```bash
python scripts/sync_dataset.py publish --dry-run --stage .verify/hf-dry-run
```

DBハッシュ、必須出力、全ファイルのハッシュ、復元元のDatasetを検査する。生成元DBが変わっていれば公開しない。dry-runはステージを作り、外部へ送信しない。既存ステージは上書きしないため、通常公開とは別の`--stage`を指定する。

```bash
python scripts/sync_dataset.py publish
```

このコマンドは新規バッチの準備とDatasetへの書込を行う。prepare済みのバッチを送る場合は`retry`を使う。復元時のSHAを期待親として`create_commit`を1回だけ呼ぶ。`upload_folder`は大きいアップロードを複数commitへ分割する可能性があるため使わない。根拠: [固定版SDKの実装](https://github.com/huggingface/huggingface_hub/blob/v2.2.0/src/huggingface_hub/hf_api.py)。

```bash
python scripts/sync_dataset.py retry --stage .verify/hf-upload
```

retryは元の作業DBやexportディレクトリを必要としない。保存した全送信物のハッシュ、圧縮DBを展開したバイト列のハッシュ、manifest、Dataset名を検証してから、同じ期待親で送信する。再送に成功したときだけpublication_receipt.jsonを残す。トークン・ローカルのreceiptはDatasetへのcommitに含めない。

他の書込が先に入っていれば条件付きcommitを失敗させる。競合を無条件アップロードで回避しない。新しいrevisionを復元し、その履歴を引き継いで再計算する。Datasetへの定期書込者はこのworkflowに集約する。

## 失敗時の復旧

- **復元・移行失敗:** 既存の公開物は更新されない。診断artifactを確認し、移行前DBのコピーで原因を調べる。元スナップショットは保持する。
- **収集失敗:** 途中ページの観測がDBに残ることはあるが、新しいバッチを公開しない。再実行時もID重複とfirst_seen変更を防ぐ。failed/runningの新方式の実行が最新なら、一括生成も拒否する。
- **HFの期待親競合:** 変更された公開先を上書きしない。先行revisionへ強制的に戻さず、最新snapshotから再開する。
- **HFの一時的な送信失敗:** 完全バッチのartifactを展開し、`retry --stage <展開したhf-upload>`を使う。親revisionが変わっていなければ、元DBや再収集なしで同じバイト列を再送できる。競合時は上記の競合復旧へ進む。
- **HF成功後のGit push失敗:** `civitai-export-batch-<run_id>`と診断artifactのpublication_receiptを取得する。HF上のmanifestとsnapshot_id・基準日・出力ハッシュを照合し、同じCSV/SVG/manifestをGitへ反映する。収集やHFアップロードをやり直して別バッチを混ぜない。

Gitへの再反映では`exports/daily_counts.csv`を`data/daily_counts.csv`にもコピーする。競合するコード変更や他の収集commitがあれば先に確認し、無条件のforce pushは行わない。

GitHubとHFを跨ぐ原子的な更新はできない。manifestとHF publication_receiptのcommit SHAで不一致を識別する。トークンはreceipt・manifestへ含めない。

## 検証

```bash
python -B -m unittest discover -s tests -v
```

単体テストは外部I/Oを置き換え、復元の破損・bootstrap拒否・条件付きcommit競合・出力改変・DB変更・dry-runを確認する。実環境のGitHub ActionsとHF書込は、対象ブランチのコードを公開した後に別途確認する。
