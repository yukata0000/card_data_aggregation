# Data Aggregation（Streamlit用リポジトリ）

このディレクトリは、元リポジトリから **Streamlitで動かすために必要な最小構成**だけを切り出したものです。

## Streamlit Community Cloud 設定

- **Main file path**: `streamlit_app/streamlit_app.py`
- **Python dependencies**: `requirements.txt`

## ローカル起動

```powershell
python -m pip install -r requirements.txt
python -m streamlit run .\streamlit_app\streamlit_app.py
```

## データ永続化について

- このリポジトリには **`db.sqlite3` は含めません**（誤コミット防止のため `.gitignore` 済み）。
- SQLite（`db.sqlite3`）は手軽ですが、Streamlit Cloud上では揮発する可能性があります。
- アプリ内の「バックアップ/復元」から **ZIPバックアップ**をダウンロードできます。
- 永続化したい場合は **PostgreSQL** を別サービス（Neon/Supabase等）で用意し、`env.example` の環境変数を Secrets に設定してください。


