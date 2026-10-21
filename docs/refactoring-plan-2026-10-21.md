# Civitai収集・集計・期間別SVG公開のリファクタリング計画

作成日: 2026-10-21 / 調査対象: `88d651d` / 状態: 実装・検証中（計画本文は受け入れ条件として保持）

## 1. 目的と完了条件

MOSS Imageなど新しいモデル系列の動向が埋もれず、長期の全体像と直近の変化を別々の図で確認できるようにする。同時に、履歴保全、指標の一貫性、取得状態の説明、変更時の検証を改善する。

本書は実装の順序・仕様・検証条件を定める。計画作成時の作業範囲は本書の作成まで。続くユーザー指示で実装が承認されたため、実装結果と検証状況は別の検証記録へ残す。外部公開はコードの反映後に確認する。

### ユーザー要求

- MOSS Imageが現在の出力で確認できない問題を解消する。
- 全期間の図を維持し、直近3か月・1か月・2週間の図を別ファイルとして継続公開する。
- 先行調査で指摘した収集・保存・集計・構造・テストの問題を含めて段階的に改修する。

### 計画上の既定値

- 「3か月」「1か月」は固定長の90日・30日とする。図・READMEには「直近90日（約3か月）」「直近30日（約1か月）」と表示し、暦月と混同させない。
- 期間の基準は壁時計の今日ではなく、同一DBスナップショット内で必須の両モデル種別を処理した`completed`バッチの最大`observed_date`。実行完了と取得範囲の品質を区別し、全図で同じ基準日を使う。
- 主図は既存SVGと同じ公開日基準のバージョン数。既存の日次・週次・月次CSVは初観測日基準を維持し、名前・説明・別CSVで両者を区別する。
- 収集対象は引き続き`Checkpoint`と`LORA`。新しいモデル種別、Webアプリ、対話型ダッシュボード、モデル本体の取得は追加しない。
- Python標準ライブラリを中心とする。実行時の新規依存は追加しない。CIの静的検査ツール追加は開発用依存として実装時に別途扱う。

## 2. 現状と根拠

リンクは調査時点のソースを指す。行番号は`88d651d`時点。確定事項と未検証事項を分ける。

| ID | 確認した事実 | 根拠 |
|---|---|---|
| E01 | 初観測日で日次件数を計算し、公開日でSVGを計算している | [collect_daily.py](https://github.com/Shiba-2-shiba/civiresearch-experimental/blob/88d651d/scripts/collect_daily.py#L537)、[plot_trends.py](https://github.com/Shiba-2-shiba/civiresearch-experimental/blob/88d651d/scripts/plot_trends.py#L149)、[README](https://github.com/Shiba-2-shiba/civiresearch-experimental/blob/88d651d/README.md#L5) |
| E02 | 表示系列は各パネルの件数上位8件。期間指定引数は存在するが定期実行は既定の1図のみ生成する | [描画](https://github.com/Shiba-2-shiba/civiresearch-experimental/blob/88d651d/scripts/plot_trends.py#L243)、[CLI](https://github.com/Shiba-2-shiba/civiresearch-experimental/blob/88d651d/scripts/plot_trends.py#L303)、[workflow](https://github.com/Shiba-2-shiba/civiresearch-experimental/blob/88d651d/.github/workflows/collect-civitai.yml#L127) |
| E03 | MOSSの2表記は保存済みで、分類設定に登録されていない | [監査CSV](https://github.com/Shiba-2-shiba/civiresearch-experimental/blob/88d651d/data/exports/base_model_audit.csv#L36)、[分類設定](https://github.com/Shiba-2-shiba/civiresearch-experimental/blob/88d651d/config/base_model_groups.yaml) |
| E04 | スナップショット取得失敗時も新規DBで継続し、後段に同じパスへの公開処理がある | [復元](https://github.com/Shiba-2-shiba/civiresearch-experimental/blob/88d651d/.github/workflows/collect-civitai.yml#L51)、[アップロード](https://github.com/Shiba-2-shiba/civiresearch-experimental/blob/88d651d/.github/workflows/collect-civitai.yml#L133) |
| E05 | ページ上限の停止理由を保存せず、エラー数0を成功とし、指定期間を図の完了判定に使用する | [収集](https://github.com/Shiba-2-shiba/civiresearch-experimental/blob/88d651d/scripts/collect_daily.py#L422)、[成功判定](https://github.com/Shiba-2-shiba/civiresearch-experimental/blob/88d651d/scripts/collect_daily.py#L817)、[完了判定](https://github.com/Shiba-2-shiba/civiresearch-experimental/blob/88d651d/scripts/plot_trends.py#L91) |
| E06 | 旧列の追加より先にその列を参照するインデックスを作成する | [DB初期化](https://github.com/Shiba-2-shiba/civiresearch-experimental/blob/88d651d/scripts/collect_daily.py#L136) |
| E07 | 861行の収集ファイルに通信・DB・移行・集計・CSV・CLIがあり、日時処理とCSV処理に重複がある | [collect_daily.py](https://github.com/Shiba-2-shiba/civiresearch-experimental/blob/88d651d/scripts/collect_daily.py)、[日時処理](https://github.com/Shiba-2-shiba/civiresearch-experimental/blob/88d651d/scripts/plot_trends.py#L40)、[CSV出力](https://github.com/Shiba-2-shiba/civiresearch-experimental/blob/88d651d/scripts/export_counts.py#L58) |
| E08 | 分類パーサーは重複を上書きし、不正な行を無視する。active_total_lastは最後の非NULL値で上書きされる | [設定読込](https://github.com/Shiba-2-shiba/civiresearch-experimental/blob/88d651d/scripts/export_counts.py#L14)、[集計](https://github.com/Shiba-2-shiba/civiresearch-experimental/blob/88d651d/scripts/export_counts.py#L76) |
| E09 | 描画は全versionのraw JSONを読み、publishedAtのないversionを集計しない | [データ読込](https://github.com/Shiba-2-shiba/civiresearch-experimental/blob/88d651d/scripts/plot_trends.py#L149) |
| E10 | 11件の既存テストがある。定期workflowにはテスト実行、実行の直列化、失敗時artifact保存がない | [収集テスト](https://github.com/Shiba-2-shiba/civiresearch-experimental/blob/88d651d/tests/test_collect_daily.py)、[出力テスト](https://github.com/Shiba-2-shiba/civiresearch-experimental/blob/88d651d/tests/test_trend_scripts.py)、[workflow全体](https://github.com/Shiba-2-shiba/civiresearch-experimental/blob/88d651d/.github/workflows/collect-civitai.yml) |

先行調査の検証結果: Python 3.10.11で11テスト成功。日次CSV4,027行に重複キーなし。週次763行・月次294行は現行設定からの再計算と一致した。一時DBでは、ページ上限でも指定期間が完了扱いになること、旧スキーマ初期化の`no such column: first_seen_date`、初観測日と公開日の集計差を再現した。CIのPython 3.12と実環境の動作確認は実装フェーズで行う。

### MOSS Imageの切り分け

2026-10-20を最終観測日とする保存済みCSVの値。いずれも初観測日基準であり、Civitai全体の件数や公開日基準の件数ではない。

| 種別 | raw値 | 初観測されたバージョン数 | 初観測されたモデル数 |
|---|---|---:|---:|
| Checkpoint | MOSS Image | 24 | 15 |
| Checkpoint | MOSS Image Base | 5 | 2 |
| LORA | MOSS Image | 182 | 80 |
| LORA | MOSS Image Base | 27 | 10 |

根拠: [監査CSV](https://github.com/Shiba-2-shiba/civiresearch-experimental/blob/88d651d/data/exports/base_model_audit.csv#L36)の36、63、111、131行。[日次CSV](https://github.com/Shiba-2-shiba/civiresearch-experimental/blob/88d651d/data/daily_counts.csv#L3923)では10月17〜20日に記録されているため、この基準日での14日・30日・90日の各窓にも同じ合計が入る。

- **確認済み:** Checkpoint計29、LORA計209バージョンが観測済み。2表記とも未分類。現在のSVGにMOSSの凡例はない。
- **有力な説明:** 全期間の上位8系列制限と分類の分散が、最近登場した系列を見えにくくしている。
- **未確認:** 公開日欠損、公開日での実順位、API取得漏れの量。SQLiteはこのチェックアウトに存在せず、CSVからは確定できない。
- **実装前の診断:** 正常な復元DBでMOSSのID、raw値、publishedAtの有無、各窓の公開件数・順位・非表示理由を出す。少数の現在のAPI応答と照合し、取得漏れがあればAPIの並び順とページング仕様を公式資料で確認する。分類追加だけで全件取得できたとは結論しない。

## 3. 採用する進め方

| 選択肢 | 効果と制約 | 判断 |
|---|---|---|
| 分類追加と既存CLIの4回実行だけ | 早く図を増やせるが、指標差・履歴保全・完了判定・重複読込が残る | 一時対処に限定 |
| 既存CLIを維持し、責務分離とデータ契約の修正を段階的に行う | 変更範囲を区切って検証でき、現在の運用を引き継げる | 採用 |
| 新しい基盤やWebダッシュボードへ全面移行 | 運用・依存・互換性の変更が大きい | 今回の対象外 |

改修順は「履歴保全 → 挙動を固定した分割 → 指標・取得状態の修正 → MOSS診断と期間別図 → 公開の統合」。既存の正しい振る舞いは回帰テストで固定し、不具合修正は構造変更と別の差分にする。各段階を独立にレビューし、巨大な一括置換にしない。

## 4. データと表示の仕様

### 4.1 指標を区別する

| 指標 | 日付の意味 | 出力・表示 |
|---|---|---|
| 初観測バージョン数 | first_seen_date | 既存daily_counts / weekly_counts / monthly_counts。既存列名と意味を維持 |
| 公開バージョン数 | publishedAtをJSTへ変換した日 | 全期間・90日・30日・14日の主図と、新規publication_daily_counts.csv |
| モデルの最初の公開 | 観測済みversion内の最も早いpublishedAt | 補助パネル。「観測済みバージョン内の初公開」と明記し、完全なモデル履歴とは表示しない |

- `first_seen_at`と既存の`first_seen_date`は再収集で変更しない。古い公開日のversionを今日初取得した場合、初観測は今日、公開集計は過去日になる。
- `published_at`をDBの明示列に追加し、既存JSONからバックフィルする。JSONは検証用に残す。公開日がない場合、createdAtやfirst_seenへ黙って置換しない。
- 公開日欠損・不正日時は件数と対象IDを診断出力に残し、公開日図から除外する。MOSSの公開日が全件欠損なら「公開日不足」と表示する。
- CSVと描画は同じ公開日集計関数を使う。新CSVの主キーは`publication_date,model_type,base_model_group`、件数列は`published_version_count,first_known_model_publication_count`。raw分類の内訳は診断出力で保持する。
- 初観測集計の再構築は全期間を対象にできるようにする。baseModel更新後に過去のキャッシュ集計だけが古く残らないようにし、現在の分類で再集計したことと設定のハッシュを記録する。過去のactive_totalは再構築で上書きしない。

### 4.2 期間別SVG

同一スナップショット・同一分類設定・同一基準日から、次の4図を一括生成する。

| 出力 | 対象期間（両端を含む） | 時間単位 | 主な用途 |
|---|---|---|---|
| trends-all.svg | 計測開始日〜基準日 | ISO週の件数合計 | 全計測期間（観測開始以降）の全体像 |
| trends-90d.svg | 基準日−89日〜基準日 | 日次 | 約3か月の変化 |
| trends-30d.svg | 基準日−29日〜基準日 | 日次 | 約1か月の変化 |
| trends-14d.svg | 基準日−13日〜基準日 | 日次 | 直近2週間の変化 |

- 計測開始前の日は作らず、窓の開始を計測開始日まで切り詰める。計測開始日はcollection_runsの最初の観測日から求め、旧DBで記録不足ならdaily_countsの最初の日を使い、その代替をメタデータに残す。
- 基準日は、必須検索を両モデル種別について処理した`execution_status=completed`バッチの`observed_date`の最大値とする。過去日を再処理しても基準日は後退しない。全図に基準日、生成時刻、日付基準、時間単位を表示する。該当バッチのないDBは通常公開を止め、初回設定の診断を出す。旧DBは少なくとも1回の新方式の収集完了を通常公開の前提とし、旧runを根拠なくcompletedへ昇格させない。
- 全期間図の表題は「全計測期間の公開動向（観測開始以降）」とする。計測開始前のpublishedAtは図と表示範囲の公開CSVから除外し、DBには保持する。Civitaiの全歴史や取得済み全公開日の図とはしない。モデル初公開の判定では開始前のversionも参照し、既存モデルの期間内更新を初公開と誤認しない。
- 例: 基準日2026-10-20なら、90日は7月23日、30日は9月21日、14日は10月7日から。末尾日は全図で10月20日。
- 全期間の週次集計は月曜始まり。最初・最後の途中週は対象日数を注記し、週全体を取得したように見せない。日次図と週次図では縦軸の単位を明記する。
- 通常公開時、既存URLの`trends.svg`は`trends-all.svg`と同じ内容を出力する互換ファイルとして残す。日次→週次への表示変更はREADMEに明記する。`plot_trends.py`は既存の単独日次図生成・`--start-date/--end-date`を維持し、4図と互換ファイルの一括生成は`build_exports.py`だけが担当する。
- 基準日、実際の期間、集計単位、選択系列、データ件数、品質状態を`export_manifest.json`へ記録する。異なる窓を生成する途中でDBを更新しない。

### 4.3 MOSSと新しい系列の見せ方

- 分類設定に`MOSS Image`グループを追加し、確認済みの`MOSS Image`と`MOSS Image Base`を対応させる。元のraw値は保持する。未確認の別名を推測でまとめない。
- 各パネルの表示順位を、その窓の公開件数で計算する。全期間の順位を短期間へ流用しない。同数時はグループ名で順序を固定する。
- `config/trend_views.json`に期間、時間単位、表示上限8、注目グループ、色を定義する。初期の注目グループはMOSS Image。注目グループも上限8の枠に含め、上位外なら末尾の通常系列と入れ替える。注目設定が表示上限を超える場合は設定エラーとし、黙って切り捨てない。
- 注目グループの期間内公開件数が0なら空の線を追加せず、図に「期間内公開0件／公開日不足／観測なし」の該当状態を表示する。注目設定だけで検出成功としない。
- `series_diagnostics.csv`に窓・種別・指標・系列・初観測件数・公開件数・欠損公開日数・順位・表示有無・非表示理由を記録する。表示されない理由を追えるようにする。
- 未分類でもraw名の系列として集計に残す。監査はdaily_countsだけでなくmodel_versionsの実データも確認する。未分類件数を可視化するが、通常運用で未知の値が現れただけでは全公開を止めない。
- 設定の重複割当、グループ名空欄、不正な構文、存在しない設定ファイルは明示的に失敗させる。既存の小さなYAMLサブセットを検証付きで維持する。
- グループの色は順位に依存させず、4図・各種別・再生成で一定とする。未知のグループにも決定的な色を割り当てる。

設定形式の最小例。`label_interval=null`は最大8個を目安とする自動計算、整数は該当するbucket単位の間隔を表す。

```json
{
  "schema_version": 1,
  "timezone": "+09:00",
  "max_series": 8,
  "pinned_groups": ["MOSS Image"],
  "colors": {"MOSS Image": "#0891b2"},
  "views": [
    {"name": "all", "days": null, "bucket": "week", "label_interval": null},
    {"name": "90d", "days": 90, "bucket": "day", "label_interval": 14},
    {"name": "30d", "days": 30, "bucket": "day", "label_interval": 5},
    {"name": "14d", "days": 14, "bucket": "day", "label_interval": 2}
  ]
}
```

### 4.4 読みやすさと品質表示

- 初期は既存の4パネルを維持し、バージョン数を主、モデル初公開を補助と明示する。14日図は日次点を描き、30日・90日図と全期間図は全点への丸印を省略する。
- x軸ラベル間隔は14日=2日、30日=5日、90日=14日、全期間=最大8個程度を目安に計算し、開始・終了を表示する。長い凡例は折返しまたは余白調整を行う。
- 欠測日一覧をSVG上部の長い1行に並べない。日数の要約と背景帯で示し、日別の詳細は`coverage_daily.csv`に出す。
- 2026-10-22の視認性修正: 記録済みの公開件数を丸印なしの連続した実線で示す。取得状況がpartial・unknownでも線を切らず、0は「記録上0件」であって実際の公開がなかった証拠ではないことを注記する。取得範囲の不確かさは背景色（薄黄・薄灰）で示す。経緯と検証は[SVG視認性修正記録](svg-readability-2026-10-22.md)を参照。
- 週内にunknown/partialの日があれば週もpartialとする。対象範囲を満たした日だけ0件を確定値として補完する。APIで観測可能な範囲の品質であり、Civitai全体の完全性を保証する表現は使わない。

## 5. 収集・保存・運用の改善

### 5.1 履歴保全

- 通常実行ではHF_TOKEN不足、認証失敗、通信失敗、復元ファイル欠損・破損を失敗として終了し、既存の公開物を更新しない。初回DB作成は明示的なbootstrapオプションだけで許可し、公開先に既存履歴がないことを確認する。既存履歴がある／確認不能な場合はbootstrapでも上書きしない。
- 一時パスへダウンロード・展開し、SQLite integrity_check、schema、必須表を検証してから作業用DBへ切り替える。
- 移行前スナップショットを保存し、schema_versionごとのトランザクションで「列追加→既存値補完→インデックス作成」を行う。再実行しても結果を変えない。
- 旧first_seen_dateが欠損している場合は、対応する実行記録のobserved_dateで補完する。対応記録がなくUTC時刻からJST日付を導く場合は推定補完として件数を記録する。既存の日付を再計算で上書きしない。
- DB更新、集計生成、公開は段階を分け、APIエラー・DB不正・必須出力不足の状態を公開しない。生成は作業用ディレクトリで完結させてから置換する。

### 5.2 取得状態と取りこぼしの診断

- collection_runsに加え、モデル種別・検索条件単位の結果を記録する。run_id、停止理由、ページ数、新規ID数、要求範囲、実際の取得時刻、エラー数を持たせる。
- バッチの`execution_status`は`running / completed / failed`。必須検索はCheckpointとLORAの通常走査各1件とし、両者がAPI・応答検証・DBエラーなしで終了した場合にcompletedとする。補完走査で必須走査の欠落を埋めたことにはしない。いずれかにエラーがある、または必須結果が欠ける場合はfailedで公開せず、従来の公開バッチと基準日を保つ。
- 停止理由は少なくとも`exhausted / known_threshold / page_budget / error`を区別する。page_budgetとknown_thresholdによる終了はエラーがなければcompletedになり、partial/unknownの品質注記を付けて公開できる。API要求の完了を完全取得と同一視しない。
- 検索条件・種別・日付ごとの`coverage_status`は`verified / partial / unknown`。次の表で判定する。旧success記録は根拠不足ならunknownへ読み替える。API仕様をまだ検証できない段階ではpartial/unknownを使い、verifiedを推測しない。

| 停止・根拠 | coverage_status | 追加条件 |
|---|---|---|
| exhausted、ページング契約と範囲到達の証拠あり | verified | 対象の種別・検索条件の取得可能な範囲を尽くし、対象日の終了時刻以降まで確認。件数0でも可 |
| exhaustedだが範囲・API契約の証拠不足 | partial / unknown | その日に利用可能な観測値があればpartial、なければunknown |
| page_budget / known_threshold | partial / unknown | 同上。指定期間だけを根拠としてverifiedにしない |
| error / 途中で不正応答 | partial / unknown | 利用可能な観測値がある日だけpartial。バッチ自体はfailedで公開しない |
| 未実行、必須種別欠落、根拠のない旧run | unknown | 図の品質判定から除外せずunknownとして残す |

日付全体の品質は、その日について全必須種別・検索条件がJSTの一日全体を証明できたときだけverifiedとする。一部だけ確認・取得できていればpartial、利用可能な根拠がなければunknown。分割された取得範囲を統合する場合も区間の空白がないことを検査する。

- 実際の取得終了時刻より未来の時間帯はverifiedにしない。23:55の日次実行で当日末尾の5分間を取得済み扱いにしない。
- ページ応答の型、不正ID、欠落metadata、ページループを検査する。API再試行の対象、上限、待機をテストし、エラーは時刻文字列だけでなくrun_idへ結び付ける。
- MOSSがDBに存在しないケースと、存在しても図に出ないケースを診断で区別する。既存モデルへの新versionを現在の並び順で拾えるかは、公式仕様と実サンプルで確認する。
- 取得漏れが確認された場合だけ、週次再取得の条件・停止条件を調整する。特定baseModelによる補完走査はAPIが対応し必要性を確認できた場合の追加策とし、無制限の全件再取得を既定にしない。

### 5.3 active_total

- 現行データでは全件空欄で、定期実行も取得を省略している。主図とMOSS対応の前提にしない。
- 将来の取得値は初観測集計とは別の日時付きスナップショットとして扱い、日次件数の再構築で消さない。
- グループ内の最後のraw値をグループ合計として出さない。モデルIDの重複を除いたグループ合計を証明できない場合は、グループのactive_total_lastを空欄にし理由を記録する。単一raw分類では最新の有効スナップショットを利用できる。

### 5.4 公開と失敗時の挙動

- workflowのconcurrencyでスケジュール・手動実行を同じ書込グループに直列化し、実行中ジョブを新着ジョブでキャンセルしない。
- HFの復元元revisionを記録し、そのrevisionを期待する親とした条件付きcommitを必須にする。直前のrevision読込だけでは競合防止としない。具体的なSDK呼出しと失敗条件は実装時に公式資料で確認し、条件付き書込が利用できなければ自動公開を止める。無条件アップロードへフォールバックしない。
- 復元後に別の書込が入った場合は条件付きcommitを失敗させ、HFを上書きせずGitHubの公開もしない。新revisionから再取得して別バッチとして再計算する。bootstrapも公開先の履歴・期待親を検証する。
- 全SVG・CSV・manifestを同じ生成バッチで検証し、HFは1回のcommitで公開する。GitHub側も同じmanifestを含む1commitで更新する。
- HFとGitHubを跨ぐ完全な原子的更新はできない。HF公開後のGit push失敗時はバッチIDをログに残し、再収集せず同じ生成物でGit公開を再試行できるようにする。異なるバッチを混ぜない。
- 失敗時は`if: always()`等で実行結果・収集エラー・診断・公開段階をartifactへ残す。トークンと認証付きURLをログに含めない。
- `git commit ... || exit 0`を差分なしの判定に置き換え、本当のcommit失敗を隠さない。HF依存は動作確認したバージョンに固定し、更新手順を明記する。

## 6. ファイル構成と責務

既存`python scripts/*.py`の入口を維持する。まずscripts配下の共通パッケージへ移し、パッケージ配布基盤は今回追加しない。

```text
scripts/
  collect_daily.py         # CLI引数と収集サービス呼出し
  export_counts.py         # 既存CSVのCLI
  audit_base_models.py     # 分類監査CLI
  plot_trends.py           # 既存の単独日次図生成CLI
  build_exports.py         # 同一snapshotから全成果物を作る入口
  civitrends/
    __init__.py
    api.py                 # HTTP、応答検証、再試行、ページング
    storage.py             # 接続、schema移行、upsert、検索、snapshot検証
    collector.py           # APIとstorageの調整、run・停止理由記録
    timeutils.py           # JST変換、期間境界、ISO週
    grouping.py            # 分類設定の検証、監査
    metrics.py             # 初観測・公開集計、在庫スナップショット集計
    coverage.py            # 種別・日付単位の取得状態
    rendering.py           # 集計済みデータからSVG生成
    exports.py             # CSV、4図、diagnostics、manifest生成
config/
  base_model_groups.yaml
  trend_views.json
tests/
  test_collect_daily.py / test_trend_scripts.py  # 既存の保護を引継ぐ
  test_migrations.py / test_metrics.py / test_coverage.py
  test_grouping.py / test_view_windows.py / test_exports.py
  test_pipeline.py
  fixtures/                                  # 小さい匿名化API応答と旧schema
.github/workflows/
  collect-civitai.yml
  tests.yml
```

境界: renderingはDB・通信へアクセスせず、metricsはHTTPへアクセスしない。collectorはCSVやSVGを生成しない。timeutils/groupingはCLIモジュールをimportしない。DB接続はトランザクションのwithだけに頼らず明示的にcloseし、Windowsでも一時DBを削除できるようにする。

主要な受渡し契約:

- `CollectionResult`: run_id、model_type、query条件、stop_reason、counts、error_count、requested_bounds、fetch_finished_at、coverage_statusとその証拠。バッチのexecution_statusは必須結果を検査してcollectorが決定する。
- `ViewSpec`: name、days（全期間はNone）、bucket（day/week）、max_series、pinned_groups、label_interval。
- `ExportContext`: snapshot_id、grouping_hash、view_config_hash、anchor_date、measurement_start、timezone、生成時刻。
- `build_exports.py`は`--db / --output-dir / --views / --generated-at`を受け付ける。views既定はconfig/trend_views.json。generated-atはタイムゾーン付きISO日時で、未指定時は開始時のUTCを1回だけ取得して全成果物へ渡す。テストでは固定値を注入する。
- metricsの結果は日付・種別・系列・指標・件数の行に統一し、coverageは別の品質情報として合流する。観測値と欠測状態を同じ0で表さない。

## 7. 実装タスクと受け入れ条件

各タスクは「対象の失敗を再現するテスト／既存挙動の固定→最小変更→対象テスト→全体テスト→差分レビュー」の順で行う。計画の作成者とレビュー担当は分ける。

### P0: 保護する契約とMOSS診断用fixture（前提なし）

対象: tests/test_collect_daily.py、tests/test_trend_scripts.py、tests/fixtures/、READMEの指標説明。

- [ ] 再収集してもversion IDが増殖せず、first_seenが変わらないテストを追加する。
- [ ] 2 raw分類・既存モデルの追加version・公開日と初観測日のずれ・公開日欠損を含む小さいMOSS fixtureを用意する。保存済みCSVの238は「観測済み総数」として資料に残し、将来も変わらない本番総数のassertにはしない。
- [ ] 正常なDBのコピーでMOSS診断を実施し、取得漏れ／分類漏れ／公開日不足／順位非表示の各件数を記録する。DB未入手ならfixture検証と実データ未確認を分けて報告する。

完了条件: 既存11テスト成功、fixtureのID重複0、両日時の期待日付が明示され、原因を「未収集」と一括りにしない診断表がある。

### P1: 復元の安全性とDB移行（P0後）

対象: collect_daily.pyのinit_db、storage.py、tests/test_migrations.py、collect-civitai.yml、HF設定文書。

- [ ] 旧schema→現schema、二重移行、移行途中失敗、破損DBのテストを追加する。
- [ ] migration順序・schema_version・バックアップ・published_at列の補完を実装する。
- [ ] 通常復元の失敗と明示bootstrapを分け、公開前検証と直列化を導入する。

完了条件: 元のID数・初回観測日を保持し、二度目の移行で差分0。復元失敗ケースで収集・公開を呼ばず、既存CSV/SVGが不変。旧版へ戻す時は移行前DBを使える。

### P2: 責務を分割し共通処理を一本化（P1後）

対象: scripts/civitrends/のapi、storage、collector、timeutils、grouping、metrics、renderingと既存CLI。

- [ ] 現行の正常系CSV・集計・CLI終了コードをfixtureで固定する。
- [ ] 通信、保存、集計、描画を順に移動し、日時、分類読込、日次CSVの重複を除く。
- [ ] main(argv)の形を揃え、入出力パス・数値引数・日付範囲を入口で検証する。

完了条件: 既存4コマンドの呼出しを維持し、仕様変更対象以外のfixture結果が一致。CLI間importとsys.path変更への依存を共通パッケージ内へ持ち込まない。新規実行時依存0。

### P3: 指標・coverage・エラーの意味を修正（P2後）

対象: collector.py、storage.py、metrics.py、coverage.py、tests/test_metrics.py、tests/test_coverage.py。

- [ ] 1ページ上限かつnextPageあり、既知閾値、片方の種別だけ成功、日付境界、日次実行後の未到来時間帯をテストする。
- [ ] run単位・種別単位の結果とstop_reason、execution_status、coverage_statusを保存し、上の判定表をテストで固定する。旧successを完全性の証拠として扱わない。
- [ ] 公開日集計と初観測集計を分離し、全期間再集計とactive_total保全を実装する。
- [ ] 不正API構造、再試行後失敗、公開日欠損・不正値を診断で確認できるようにする。

完了条件: ページ上限と既知閾値でverifiedにならないが、両種別にエラーがなければcompletedとして公開対象にできる。必須種別の失敗・欠落で新バッチを公開しない。過去日の再実行で基準日が後退しない。未検証日を確定0にしない。公開日が昨日・初観測が今日のfixtureは別日へ計上。再集計前後でIDとfirst_seenは不変、在庫履歴は保持される。

### P4: MOSS分類・4期間出力・可読性（P3後）

対象: base_model_groups.yaml、trend_views.json、metrics.py、rendering.py、exports.py、plot_trends.py、tests/test_grouping.py、tests/test_view_windows.py、tests/test_exports.py。

- [ ] MOSSの2表記、設定重複、未知raw、注目系列が9位、公開日全欠損をテストする。
- [ ] 90/30/14日の包含境界、計測期間不足、閏年・年越し、ISO週端、0件・1日だけのDBをテストする。計測開始前の公開を全期間図から除外し、モデル初公開の判定では参照するケースも固定する。
- [ ] 4期間の集計と独立した順位選択、安定色、MOSSの状態表示、diagnosticsを実装する。
- [ ] 軸・点・凡例・品質表示を調整し、既存trends.svg互換出力を作る。

完了条件: 各窓でグラフ件数と公開日CSVが一致。MOSSに期間内公開があれば順位にかかわらず表示され、なければ理由が表示される。全図のMOSSの色が一致し、通常の系列数は8以下。図の文字が切れず、欠測が0件に見えない。

### P5: 一括生成と公開導線（P4後）

対象: build_exports.py、exports.py、collect-civitai.yml、README.md、docs/huggingface-dataset-setup.md、tests/test_pipeline.py。

- [ ] 一時DBからCSV・4SVG・互換SVG・coverage・diagnostics・manifestを一括生成する。
- [ ] READMEに4図へのリンクを設置し、全期間と14日をプレビュー、90日と30日はリンクで選べるようにする。HF DatasetのREADMEにも同じ期間メニューを設ける。
- [ ] HFへのコピー対象にJSONを含め、期待する出力一覧をmanifestと照合する。欠けたファイルがあるバッチを公開しない。
- [ ] 外部送信を置き換えたdry-run、アップロード失敗、Git push失敗、再公開時の同じバッチ使用をテストする。復元後・送信直前の別書込を注入し、条件付きcommitの競合でHF上書きとGitHub公開の両方が0回になることを確認する。

完了条件: GitHub/HFそれぞれから4期間を開ける。全ファイルのsnapshot_id・基準日・設定ハッシュが一致。失敗時の旧公開物保全と診断artifactを確認できる。公開リトライで再収集を実行しない。

### P6: CI・文書・移行後検証（P5後、CIの基本追加はP0から可）

対象: .github/workflows/tests.yml、README.md、既存の計画文書、HF設定文書。

- [ ] pull_request/pushでunittestを実行する。最初はPython 3.10/3.12とWindows/Ubuntuで接続・日時・一時ファイルの違いを確認する。
- [ ] lint・型検査は設定とツールの導入を別差分として扱う。導入した場合はCIで実行し、未導入なら未実施を明記する。
- [ ] 古い計画文書に本書への参照を追加し、実装済みの週次処理など古いチェックリストと矛盾する記述を更新する。
- [ ] 本番DBのコピーで旧出力との差分を確認し、変更を意図した指標・分類・期間以外に差がないことを確認する。

完了条件: CI成功、4図の目視確認、移行/復旧手順の実行証拠、残る取得範囲の制約の説明が揃う。fixtureだけで本番MOSSの完全取得を宣言しない。

## 8. 検証方法

既存の基準コマンド:

```bash
python -B -m unittest discover -s tests -v
```

実装後のCLI契約として、以下を追加する。現時点では未実装。

```bash
python scripts/build_exports.py --db <検証用DB> --output-dir <一時出力先> --generated-at 2026-10-21T00:00:00+00:00
python scripts/plot_trends.py --db <検証用DB> --start-date 2026-10-07 --end-date 2026-10-20 --output <一時SVG>
python scripts/audit_base_models.py --db <検証用DB> --all --output <一時監査CSV>
```

| 検証 | 方法 | 合格条件 |
|---|---|---|
| MOSS分類 | 2 raw値のfixtureで観測・公開集計 | 1グループに統合、IDの重複計上0 |
| 指標の区別 | 初観測日と公開日が違うfixture | 既存CSVと新CSVの期待日がそれぞれ正しい |
| 上位外の表示 | MOSSを9位にしたfixture | 注目指定で表示、上限8維持、非表示理由も記録 |
| 日付窓 | 2026-10-20基準・端日の前後にデータ | 90日=7/23、30日=9/21、14日=10/7、端日含有 |
| coverage | 上限停止、片側失敗、旧run、未来の時刻 | 完全取得の誤判定0、欠測の確定0表示0 |
| migration | 旧DB・二度実行・失敗注入 | データ保全、再実行可能、失敗時ロールバック |
| 復元・公開 | 401/403、404、通信失敗、破損、成果物不足、競合 | 通常実行では公開を呼ばず旧成果物維持 |
| 集計一致 | SVG用系列・CSV・manifestの照合 | 窓内総数、選択系列、日付境界が一致 |
| 表示 | 4SVGを実サイズでブラウザ表示 | 凡例・軸・注記の切れや重なりなし、品質状態を判別可能 |
| 再生成 | 同一snapshot・設定・指定生成時刻で2回 | 決定的な並び・色・件数、同じ成果物ハッシュ |
| リソース | Windowsで読込後に一時DBを削除 | 未解放接続によるPermissionErrorなし |

性能は現物DBで変更前後を計測する。公開日を列に保持し必要な範囲へSQLを絞るが、モデル初公開の算出は全観測履歴で行ってから期間を絞る。短期窓内の最初のversionをモデルの初公開と誤認しない。根拠のない速度目標や依存追加は置かない。

## 9. リスクと復旧

| リスク | 対策・復旧 |
|---|---|
| MOSSの取得不足が描画修正だけでは解決しない | P0診断でIDと公開日を照合。未確認範囲を残し、P3で収集条件を検証する |
| 新しい分類・公開日補完で過去の図が変わる | snapshotと設定ハッシュを保存し、初観測CSVと公開日CSVを比較。生ID・first_seenを保全する |
| 週次の全期間図と日次図を同じ単位として読む | タイトル・軸・manifestに単位を表示し、途中週を注記する |
| 旧DBが想定外のschemaや日時を含む | コピー上で移行し、補完不可を診断。移行前snapshotへ復旧する |
| 複数実行・外部書込で履歴が上書きされる | workflow直列化とHF期待親revisionに対する条件付きcommit。競合時はGit公開も停止し再取得する |
| HFとGitHubの公開バッチがずれる | manifestのバッチIDで判別し、同じバッチを再送する。旧revisionと旧Git commitを保持する |
| unknownを多く表示して図が変わる | 取得成功と完全性を区別した結果として説明し、既存successを遡及的に完全取得と見なさない |
| 改修が大きくなる | P0〜P6を分割し、段階ごとの契約テスト・レビューを通す。可視化基盤の全面移行はしない |

停止条件は、テスト・移行検証・4期間の生成と表示・公開整合性・MOSS診断の証拠が揃い、未確認の取得範囲が文書化された時点とする。DBやAPIへアクセスできない場合はその検証だけを未完了として扱い、成功したように報告しない。
