# Streamlit 版（DjangoのDB/モデルをそのまま使用）

このリポジトリは元々 **Djangoアプリ** ですが、`streamlit_app/streamlit_app.py` に **Streamlit版UI** を追加しています。

## ローカル起動（Windows例）

```powershell
python -m pip install -r requirements.txt
python -m streamlit run .\streamlit_app\streamlit_app.py
```

初回起動時は `migrate` が自動で走ります（`db.sqlite3` を使う場合）。

## Streamlit Community Cloud へのデプロイ

- **Main file path**: `streamlit_app/streamlit_app.py`
- **Python dependencies**: `requirements.txt`
- **DB**:
  - 既定はSQLite（`db.sqlite3`）です。Streamlit Cloudのファイルシステムは**揮発**するため、再起動/再デプロイ等でSQLiteが消える可能性があります。
  - 対策として、アプリの「**バックアップ/復元**」ページから **ZIPバックアップをダウンロード**しておいてください。
  - 永続化したい場合は **PostgreSQL推奨**（Streamlit Cloud自体はPostgreSQLをホストしません）。

### 環境変数（PostgreSQLを使う場合）

`env.example` と同様に、以下を Streamlit Cloud の Settings → Secrets（または環境変数）に設定します:

- `USE_POSTGRES=1`
- `POSTGRES_DB=...`
- `POSTGRES_USER=...`
- `POSTGRES_PASSWORD=...`
- `POSTGRES_HOST=...`
- `POSTGRES_PORT=...`

#### PostgreSQLは「別サーバ」が必要？

はい。Streamlit Community CloudはDBサーバを提供しないため、PostgreSQLは以下いずれかで用意します。

- **マネージドPostgreSQL**（おすすめ）: Neon / Supabase / Railway / Render など
- **自前サーバ**: VPS等にPostgreSQLを立てる

どちらでも、上記の接続情報（HOST/USER/PASSWORD等）を Secrets に設定すれば接続できます。

## 使い方

- サイドバーの **ログイン** からDjangoユーザーでログインします（未作成なら「新規ユーザ作成」）。
- **入力**: 対戦結果を保存
- **結果一覧**: フィルタ/ソート、複数削除、1件編集
- **分析**: 勝率やデッキ別集計
- **マスタ管理**: 使用デッキ/対面デッキの追加・有効/無効切替


