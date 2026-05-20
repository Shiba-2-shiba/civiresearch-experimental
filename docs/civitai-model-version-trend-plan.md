# Civitai Model Version Trend Collection Plan

## Goal

Civitaiに公開されているモデル更新活動を、測定開始後から継続的に蓄積し、baseModel別・モデル種別別の流行変化を分析できる状態にする。

主指標は新規バージョン数とする。新規モデル数は補助指標として保存するが、新規モデル数と新規バージョン数の合算は二重カウントになりやすいため主グラフには使わない。

## Scope

対象:

- Civitai REST APIで取得できる公開・検索可能なモデル
- モデル種別はまず `Checkpoint` と `LORA`
- 分類軸は `modelVersions[].baseModel`
- 測定開始後の日次推移
- 後から週次・月次・任意baseModelグループで再集計できるID単位データ

対象外:

- 測定開始前の正確な履歴復元
- 削除済み・非公開化済みモデルの完全補完
- 画像・プロンプトデータの収集
- モデルファイル本体のダウンロード

## Primary Metrics

- `new_version_count`: その日に初めて観測された `modelVersion.id` の数
- 集計軸:
  - `observed_date`
  - `model_type`
  - `base_model_raw`

補助指標:

- `new_model_count`: その日に初めて観測された `model.id` の数
- `active_total`: 観測時点でAPI検索対象になっている公開在庫数。Civitai APIが `metadata.totalItems` を返す場合のみ保存するbest-effort指標。

## Data Policy

`base_model_raw` はCivitai APIの値を正規化せず保存する。`Flux.1 D`, `Flux.1 S`, `Illustrious`, `Z-Image-Turbo`, `Anima` などを後から自由にグルーピングできるようにする。

グラフ化時に必要なら別途マッピングを作る。

例:

```yaml
Flux:
  - Flux.1 D
  - Flux.1 S
  - Flux.1 Dev
Illustrious:
  - Illustrious
Z-Image:
  - Z-Image-Turbo
Anima:
  - Anima
```

## Storage Design

最初はGitHubリポジトリ内のSQLiteとCSVで十分とする。

```text
data/
  civitai.sqlite
  daily_counts.csv
  errors.csv
  exports/
    weekly_counts.csv
    monthly_counts.csv
config/
  base_model_groups.yaml
scripts/
  collect_daily.py
  export_counts.py
  plot_trends.py
.github/workflows/
  collect-civitai.yml
```

### SQLite Tables

#### `models`

```sql
model_id integer primary key
first_seen_at text not null
last_seen_at text not null
name text
model_type text not null
creator_username text
nsfw integer
mode text
raw_json text
```

#### `model_versions`

```sql
version_id integer primary key
model_id integer not null
first_seen_at text not null
last_seen_at text not null
created_at text
version_name text
model_type text not null
base_model_raw text
raw_json text
foreign key(model_id) references models(model_id)
```

#### `daily_counts`

```sql
observed_date text not null
model_type text not null
base_model_raw text not null
new_version_count integer not null
new_model_count integer not null
active_total integer
collected_at text not null
primary key(observed_date, model_type, base_model_raw)
```

#### `collection_runs`

```sql
run_id text primary key
started_at text not null
finished_at text
status text not null
pages_fetched integer default 0
models_seen integer default 0
versions_seen integer default 0
error_message text
```

## Collection Flow

Daily collection:

1. GitHub Actionsを毎日同じJST時刻に起動する。
2. `types=Checkpoint` と `types=LORA` を別々に取得する。
3. `sort=Newest` でページングし、モデルとバージョンのID単位データを保存する。
4. 既知モデル・既知バージョンに十分連続して到達したら、その日の差分取得を停止する。
5. 新規 `modelVersion.id` を `first_seen_at` 基準で `base_model_raw` ごとに集計する。
6. 補助的に、新規 `model.id` と `active_total` も保存する。
7. SQLiteとCSVを更新してGitHub Actions botでcommitする。

Weekly reconciliation:

1. 週1回、日次より広いページ数を取得する。
2. 日次クロールの取りこぼし、後から公開された古い `createdAt` のバージョン、分類変更を検出する。
3. `last_seen_at` を更新する。
4. 必要なら `daily_counts` をSQLiteから再計算する。

## GitHub Actions Plan

GitHub Actionsの `schedule` はUTC基準なので、JST 23:55に実行する場合は14:55 UTCを指定する。

```yaml
on:
  schedule:
    - cron: "55 14 * * *"
  workflow_dispatch:
```

Workflow requirements:

- `permissions.contents: write`
- Python 3.12
- 依存関係は最小限にする
- API失敗時は `errors.csv` と `collection_runs` に記録する
- 取得成功時だけ `data/` をcommitする

## Analysis Outputs

最低限作れるグラフ:

- 日次 `new_version_count` by `base_model_raw`
- 日次 `new_version_count` by `base_model_group`
- `Checkpoint` と `LORA` の比較
- `Flux`, `Illustrious`, `Z-Image-Turbo`, `Anima` の比較
- 週次・月次に再集計した `new_version_count`
- 補助グラフとして `new_model_count`
- 補助グラフとして `active_total`

主グラフは `new_version_count` に統一する。

## Acceptance Criteria

- GitHub Actionsで手動実行できる。
- 日次実行でSQLiteに `models` と `model_versions` がupsertされる。
- `model_versions.version_id` が重複しない。
- `base_model_raw` がAPI値のまま保存される。
- `daily_counts` が `new_version_count` を主指標として生成される。
- CSV exportから日次・週次・月次グラフを再生成できる。
- API失敗時に失敗理由とHTTPステータスが保存される。
- モデルファイル本体や画像ファイルをダウンロードしない。

## Risks And Mitigations

| Risk | Impact | Mitigation |
| --- | --- | --- |
| Civitai APIの仕様変更 | 収集失敗 | レスポンスmetadataとraw JSONを保存し、エラーを記録する |
| Rate limit | 日次収集失敗 | ページ間sleep、retry、週次reconciliationを入れる |
| `baseModel` 表記揺れ | グラフが分散 | raw保存を維持し、グラフ化時にgroup mappingを使う |
| 日次クロールの取りこぼし | 新規数が過少 | 週次で広く再取得し、ID単位データから再集計する |
| GitHub repo肥大化 | 運用しづらい | SQLiteが大きくなったらGit LFS、Release artifact、Hugging Face Datasetへ移行する |
| scheduled workflow停止 | 欠測 | `workflow_dispatch` を付け、失敗・欠測日を明示する |

## Implementation Tasks

### Phase 0: Repository Setup

- [x] 計画ドキュメントを作成する
- [x] Pythonプロジェクト構成を決める
- [x] `data/`, `scripts/`, `config/`, `.github/workflows/` を作成する
- [x] `.gitignore` 方針を決める

### Phase 1: Minimal Collector

- [x] Civitai API clientを実装する
- [x] `types=Checkpoint` / `types=LORA` のページング取得を実装する
- [x] `models` tableを作成する
- [x] `model_versions` tableを作成する
- [x] upsert処理を実装する
- [x] raw JSON保存を実装する
- [x] 失敗時のエラー記録を実装する

### Phase 2: Daily Aggregation

- [x] `first_seen_at` 基準の `new_version_count` を計算する
- [x] `new_model_count` を補助指標として計算する
- [x] baseModel別 `active_total` をbest-effortで取得・保存する
- [x] `daily_counts` tableを作成する
- [x] `daily_counts.csv` exportを実装する

### Phase 3: Automation

- [x] GitHub Actions workflowを追加する
- [ ] 手動実行 `workflow_dispatch` をGitHub上で確認する
- [x] 日次cronをJST基準で設定する
- [x] commit/push処理をActions内に追加する
- [x] API失敗時にworkflowが原因を残すようにする

### Phase 4: Reaggregation And Plotting

- [x] `base_model_groups.yaml` を追加する
- [x] 日次CSVから週次・月次CSVを生成する
- [x] `Flux`, `Illustrious`, `Z-Image-Turbo`, `Anima` の比較グラフを生成する
- [x] LoRAのみ、Checkpointのみのフィルタを実装する
- [x] READMEに使い方を追加する

### Phase 5: Hardening

- [x] retry/backoffを追加する
- [ ] 週次reconciliation jobを追加する
- [ ] 欠測日を検出する
- [ ] schema migration方針を決める
- [ ] データサイズが増えた場合の退避先を検討する

## Current Progress

| Date | Status | Notes |
| --- | --- | --- |
| 2026-05-18 | Planned | 主指標を新規バージョン数に決定。測定開始後のみを対象にする。 |
| 2026-05-18 | Implemented initial collector | 標準ライブラリのみでSQLite収集、CSV export、SVG plot、GitHub Actions日次workflowを追加。実API 1ページ検証で200モデル・659バージョンを保存できた。`active_total` は現在のAPIレスポンスで `metadata.totalItems` が無い場合は空欄になる。 |

## Open Decisions

- 日次実行時刻を何時JSTにするか。
- 日次クロールの停止条件を何件連続既知IDにするか。
- `active_total` を全baseModel候補で毎日取るか、観測済みbaseModelだけ取るか。
- SQLiteをGit管理するか、CSV exportのみGit管理してSQLiteをartifactにするか。

## Current Defaults

- 日次実行時刻: JST 23:55 (`14:55 UTC`)
- スケジュール実行では `--observed-date` をUTC日付に固定する。JST 23:55時点ではUTC日付とJST日付が一致するため、GitHub Actionsの数分遅延で観測日が翌JST日にずれることを避けられる。
- 日次クロール停止条件: `known_version_stop=200`
- `active_total`: その日の観測済みbaseModelを対象にbest-effortで取得する。現在のAPIが `metadata.totalItems` を返さない場合は空欄。`--skip-active-totals` で無効化できる。
- SQLite: `data/civitai.sqlite` をGit管理対象にする。WAL/SHMだけ `.gitignore` で除外する。
- 初回実行日は観測開始ベースラインとして扱う。初回に見えた既存versionも `first_seen_at` は初回日になるため、初日グラフは「測定開始時に初めて観測されたもの」として解釈する。

## Recommended First Implementation Slice

最初は次の順で小さく作る。

1. `scripts/collect_daily.py` で `LORA` と `Checkpoint` の `sort=Newest` を少数ページだけ取得する。
2. SQLiteに `models` と `model_versions` をupsertする。
3. `first_seen_at` から `daily_counts.csv` を出す。
4. GitHub Actionsで手動実行する。
5. 取得件数とAPI挙動を確認してから日次cronを有効化する。
