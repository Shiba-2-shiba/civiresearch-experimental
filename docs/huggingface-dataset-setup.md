# Hugging Face Dataset の設定

このプロジェクトでは、長期保存するデータ本体を Hugging Face Dataset repo に置く。
GitHub にはコード、workflow、小さめの CSV、SVG グラフを残す。

## 作成済み Dataset repo

```text
https://huggingface.co/datasets/Shiba-2-shiba/civitai-model-version-trends
```

GitHub Actions 用の Hugging Face access token は、GitHub repository secret として次の名前で登録する。

```text
HF_TOKEN
```

## GitHub Actions からアップロードされる内容

workflow は収集と export のあと、次のファイルを Hugging Face Dataset repo にアップロードする。

```text
daily_counts/daily_counts.csv
exports/base_model_audit.csv
exports/monthly_counts.csv
exports/trends.svg
exports/weekly_counts.csv
snapshots/civitai.sqlite.gz
```

`snapshots/civitai.sqlite.gz` は SQLite DB の圧縮スナップショット。
SQLite はローカル処理には便利だが、GitHub の履歴に積み続けるには重すぎるため、将来的には Hugging Face 側を主データ置き場にする。

## token を作り直す場合

1. https://huggingface.co/settings/tokens を開く。
2. `Fine-grained` token を作る。
3. 対象 repo に次を選ぶ。

```text
Shiba-2-shiba/civitai-model-version-trends
```

4. `Read access` と `Write access to contents` を付ける。
5. GitHub の `Settings` -> `Secrets and variables` -> `Actions` で `HF_TOKEN` を更新する。

token 文字列は README、issue、chat、commit に貼らない。

## 手動アップロード確認

ローカルに Hugging Face CLI がある場合は、次で単体アップロードを確認できる。

```bash
hf upload Shiba-2-shiba/civitai-model-version-trends data/exports/trends.svg exports/trends.svg --repo-type dataset
```

GitHub Actions では `HF_TOKEN` secret を使うため、ローカルで token を表示する必要はない。
