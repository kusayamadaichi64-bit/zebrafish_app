# ゼブラフィッシュ水槽管理アプリ

Streamlit + SQLite で作ったゼブラフィッシュの水槽管理 Web アプリ。

## 機能

- 📊 ダッシュボード：全水槽の状態一覧、要観察/隔離中のアラート
- 🪣 水槽管理：水槽の登録・更新・削除
- 🐠 個体管理：個体の登録・更新・削除
- 🥚 産卵成績：産卵記録の登録、受精率の推移グラフ
- 📥 CSV ダウンロード対応

## セットアップ

```bash
pip3 install streamlit pandas
streamlit run app.py
```

初回起動時に `zebrafish.db` が自動生成されます。
