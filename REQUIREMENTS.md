# ゼブラフィッシュ水槽管理アプリ 要件定義書

**Version**: 2.0
**Date**: 2026-05-28
**Author**: Da1（Tamable）

---

## 1. 背景と目的

ゼブラフィッシュ研究室の日常業務（給餌・交配・採卵）を、無料ツールのみでデジタル化する。
過去データを蓄積して**交配成功率の高い個体・ペア**を可視化し、研究効率を上げることをゴールとする。

## 2. スコープ

| # | 機能 | 本リリース |
|---|---|---|
| F1 | 餌やりログ | ✅ |
| F2 | ペア成功率分析 | ✅ |
| F3 | 交配トライアル管理 | ✅ |
| F4 | 個体写真記録 | ⏸️ 次回 |
| F5 | 系統樹可視化 | ⏸️ 次回 |
| F6 | UIの温かみアップ | ✅ |

## 3. ユーザーと利用環境

- **ユーザー**：研究室メンバー（1〜数名）
- **デバイス**：PC（管理）／スマホ（現場記録）
- **接続**：研究室内・Streamlit Community Cloud（無料枠）

## 4. 機能要件

### F1. 餌やりログ

| 項目 | 内容 |
|---|---|
| 目的 | 1日4回の給餌を確実に記録、やり忘れ防止 |
| 主操作 | 「今あげた」ボタンを水槽ごとにタップ → 自動で現在時刻を記録 |
| 表示 | 水槽ごとに「最終給餌から◯時間◯分」「本日の給餌回数 n/4」 |
| データ | `feeding_logs` テーブル（log_id, tank_id, fed_at, memo） |
| 受入基準 | ・スマホでもボタンが押しやすい大きさ<br>・最後の給餌から2時間以上経った水槽は黄色／6時間超は赤で警告 |

### F2. ペア成功率分析

| 項目 | 内容 |
|---|---|
| 目的 | 過去の産卵成績から、成績の良い個体／ペアを発見する |
| 算出指標 | **オス別**：試行回数、成功回数（採卵>0）、成功率、平均採卵数、平均受精率<br>**メス別**：同上<br>**ペア別**：同上 + 最終試行日 |
| 表示 | ・個体別ランキング（オス／メス）<br>・ペア別ランキング<br>・「次回おすすめペア」（成功率×直近未試行）スコア順 |
| データソース | 既存の `spawning_records` を集計（追加スキーマなし） |
| 受入基準 | ・成績は試行3回以上の個体のみ「信頼度マーク」付き<br>・データが0件でも画面エラーにならない |

### F3. 交配トライアル管理

| 項目 | 内容 |
|---|---|
| 目的 | 「前日セット→当日採卵→戻し」の一連を可視化し、戻し忘れ・採卵忘れを防ぐ |
| 状態遷移 | `計画中 → 前日セット済み → 採卵済み → 戻し済み`（中止も可） |
| 入力項目 | 予定日、オスID、メスID、元水槽（オス／メス）、交配用水槽、メモ |
| 自動化 | 「採卵済み」マーク時に採卵数・受精率を入力 → `spawning_records` へ自動連携 |
| 表示 | ・進行中トライアル一覧（ステータス別色分け）<br>・各トライアルに次のアクションボタンを表示 |
| データ | `mating_trials` テーブル（後述） |
| 受入基準 | ・「戻し済み」になるまで個体は使用中扱い、別トライアルで重複選択不可<br>・各ステップのタイムスタンプが記録される |

### F6. UIの温かみアップ

| 項目 | 内容 |
|---|---|
| 目的 | 毎日触るツールとして気持ちよく使える見た目に |
| 手段 | `.streamlit/config.toml` でカラーテーマ定義<br>・Primary：落ち着いた緑（水草） `#5C8D5A`<br>・Background：温かいオフホワイト `#FBF7F0`<br>・Secondary background：ベージュ `#F0E8D8`<br>・Text：濃いめのブラウン `#3C3530` |
| アイコン | 既存の絵文字を継続。タブ・ボタンで統一感を持たせる |
| 受入基準 | ・コントラスト比 4.5:1 以上（可読性）<br>・各画面の余白・見出しサイズが揃っている |

## 5. データモデル

### 既存テーブル（変更なし）

```sql
individuals (individual_id PK, birth_date, sex, lineage)
tanks       (tank_id PK, current_individual_id, health_status, memo)
spawning_records (history_id PK, spawning_date, male_parent_id, female_parent_id,
                  egg_count, fertilization_rate)
```

### 新規テーブル

```sql
-- F1: 給餌ログ
feeding_logs (
    log_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    tank_id TEXT NOT NULL,
    fed_at  TEXT NOT NULL,          -- ISO datetime
    memo    TEXT,
    FOREIGN KEY (tank_id) REFERENCES tanks(tank_id)
)

-- F3: 交配トライアル
mating_trials (
    trial_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    planned_date        TEXT NOT NULL,           -- 採卵予定日
    male_id             TEXT NOT NULL,
    female_id           TEXT NOT NULL,
    source_tank_male    TEXT,                    -- 戻し先（オス）
    source_tank_female  TEXT,                    -- 戻し先（メス）
    breeding_tank_id    TEXT,                    -- 交配用水槽
    status              TEXT NOT NULL DEFAULT '計画中'
                        CHECK(status IN ('計画中','前日セット済み','採卵済み','戻し済み','中止')),
    setup_at            TEXT,
    divider_removed_at  TEXT,
    egg_collected_at    TEXT,
    returned_at         TEXT,
    spawning_history_id INTEGER,                 -- 採卵時にリンク
    notes               TEXT,
    FOREIGN KEY (male_id)             REFERENCES individuals(individual_id),
    FOREIGN KEY (female_id)           REFERENCES individuals(individual_id),
    FOREIGN KEY (spawning_history_id) REFERENCES spawning_records(history_id)
)
```

## 6. 画面構成（タブ）

| # | タブ名 | 内容 |
|---|---|---|
| 1 | 📊 ダッシュボード | 全体状況サマリ（要観察水槽／本日給餌回数／進行中トライアル） |
| 2 | 🍚 餌やり | 水槽ごとの給餌ボタン、本日の集計 |
| 3 | 💕 交配トライアル | 進行中トライアル管理、新規計画 |
| 4 | 📈 成績分析 | 個体別／ペア別ランキング、おすすめペア |
| 5 | 🪣 水槽管理 | 既存 |
| 6 | 🐠 個体管理 | 既存 |
| 7 | 🥚 産卵成績 | 既存（手入力＆履歴） |

## 7. 非機能要件

- **動作環境**：Streamlit 1.x / Python 3.7+ / SQLite
- **データ保護**：CSV出力可（既存）、Streamlit Cloud再起動でDBリセットの可能性あり → 重要データは適宜CSVダウンロード推奨
- **国際化**：UIは日本語のみ
- **コスト**：完全無料

## 8. 制約事項・既知の課題

1. **Streamlit Cloud のファイルシステムは揮発性** → 永続化したい場合は Google Sheets 連携 / Supabase 等への移行が必要（次フェーズ）
2. **写真機能は本リリース対象外**（容量制約のため）
3. **マルチユーザー同時編集の競合制御は未実装**（1〜数名利用前提）

## 9. リリース後の運用フロー（推奨）

1. **朝**：ダッシュボードで「要観察」水槽と「本日の給餌タスク」を確認
2. **給餌時**：餌やりタブで該当水槽の「あげた」ボタンをタップ
3. **実験前日**：交配トライアルタブで新規計画を作成 → 個体をセット → 「前日セット済み」に
4. **実験当日**：採卵 → 「採卵済み」マーク（採卵数・受精率を入力）→ 個体を戻し「戻し済み」に
5. **週次**：成績分析タブで次週のペア候補を検討
