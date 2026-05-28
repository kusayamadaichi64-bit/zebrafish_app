"""
ゼブラフィッシュ水槽管理 Webアプリ v2.0
- Streamlit + SQLite の1ファイル構成
- 機能: 餌やりログ / 交配トライアル管理 / ペア成功率分析 / CSV出力
- 起動: streamlit run app.py
"""

import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
import streamlit as st

DB_PATH = Path(__file__).parent / "zebrafish.db"

FEEDS_PER_DAY = 4
FEED_WARN_HOURS = 2     # この時間を超えたら黄色
FEED_ALERT_HOURS = 6    # この時間を超えたら赤
TRIAL_STATUSES = ["計画中", "前日セット済み", "採卵済み", "戻し済み", "中止"]

# 水槽の物理配置（あとから書き換えれば構造変更可）
RACKS = ["A", "B", "C", "D"]         # ラック
TIERS = [1, 2, 3, 4]                  # 段
COLS = list(range(1, 16))             # 列 1〜15
# → 計 len(RACKS) * len(TIERS) * len(COLS) = 240 水槽


def format_location(rack, tier, col_no):
    """場所コードを 'B-2-05' 形式に整形。値が欠けていれば空文字を返す。"""
    if not rack or tier is None or col_no is None:
        return ""
    return f"{rack}-{int(tier)}-{int(col_no):02d}"


HEALTH_COLOR = {
    "良好":   "#B8DDB6",
    "要観察": "#F5D7A1",
    "隔離中": "#E8B4A8",
}
EMPTY_COLOR = "#F5F2EC"


def render_rack_html(rack_letter, df_sub):
    """1つのラックを 4段×15列 のHTMLテーブルとして組み立てる。"""
    by_loc = {}
    for r in df_sub.itertuples():
        if pd.notna(r.tier) and pd.notna(r.col_no):
            by_loc[(int(r.tier), int(r.col_no))] = r

    parts = [f'<div style="margin:0 0 1.5rem 0">'
             f'<h4 style="margin:0 0 8px 0">🏠 ラック {rack_letter}</h4>'
             f'<table style="border-collapse:separate;border-spacing:3px;font-size:11px;">']
    # ヘッダ列番号
    parts.append('<tr><th style="width:36px"></th>')
    for c in COLS:
        parts.append(f'<th style="padding:2px;color:#888;font-weight:500;width:48px">{c:02d}</th>')
    parts.append('</tr>')

    for t in TIERS:
        parts.append(f'<tr><th style="padding:4px;color:#888;text-align:right">段{t}</th>')
        for c in COLS:
            tank = by_loc.get((t, c))
            if tank is None:
                bg, label, tip = EMPTY_COLOR, "—", "未登録"
            else:
                bg = HEALTH_COLOR.get(tank.health_status, "#E8DDC8")
                tid = str(tank.tank_id)
                label = tid if len(tid) <= 6 else tid[:5] + "…"
                ind = tank.current_individual_id if pd.notna(tank.current_individual_id) else "-"
                tip = f"ID:{tid} / 状態:{tank.health_status or '-'} / 個体:{ind}"
            parts.append(
                f'<td title="{tip}" style="background:{bg};padding:8px 4px;'
                f'text-align:center;border-radius:6px;color:#3C3530;'
                f'font-weight:500;cursor:default">{label}</td>'
            )
        parts.append('</tr>')
    parts.append('</table></div>')
    return "".join(parts)


# ============================================================
# データベース
# ============================================================
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with get_conn() as conn:
        c = conn.cursor()

        c.execute("""
            CREATE TABLE IF NOT EXISTS individuals (
                individual_id TEXT PRIMARY KEY,
                birth_date    TEXT,
                sex           TEXT CHECK(sex IN ('オス','メス','混合')),
                lineage       TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS tanks (
                tank_id               TEXT PRIMARY KEY,
                current_individual_id TEXT,
                health_status         TEXT CHECK(health_status IN ('良好','要観察','隔離中')),
                memo                  TEXT,
                rack                  TEXT,
                tier                  INTEGER,
                col_no                INTEGER,
                FOREIGN KEY (current_individual_id) REFERENCES individuals(individual_id)
            )
        """)
        # マイグレーション: 旧スキーマに rack/tier/col_no が無ければ追加
        tank_cols = [r[1] for r in c.execute("PRAGMA table_info(tanks)").fetchall()]
        for col_def in [("rack", "TEXT"), ("tier", "INTEGER"), ("col_no", "INTEGER")]:
            if col_def[0] not in tank_cols:
                c.execute(f"ALTER TABLE tanks ADD COLUMN {col_def[0]} {col_def[1]}")
        c.execute("""
            CREATE TABLE IF NOT EXISTS spawning_records (
                history_id          INTEGER PRIMARY KEY AUTOINCREMENT,
                spawning_date       TEXT,
                male_parent_id      TEXT,
                female_parent_id    TEXT,
                egg_count           INTEGER,
                fertilization_rate  REAL,
                FOREIGN KEY (male_parent_id)   REFERENCES individuals(individual_id),
                FOREIGN KEY (female_parent_id) REFERENCES individuals(individual_id)
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS feeding_logs (
                log_id  INTEGER PRIMARY KEY AUTOINCREMENT,
                fed_at  TEXT NOT NULL,
                memo    TEXT
            )
        """)
        # マイグレーション: 旧スキーマ（tank_id列あり）から新スキーマへ
        cols = [r[1] for r in c.execute("PRAGMA table_info(feeding_logs)").fetchall()]
        if "tank_id" in cols:
            c.execute("""
                CREATE TABLE feeding_logs_new (
                    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fed_at TEXT NOT NULL,
                    memo   TEXT
                )
            """)
            c.execute("INSERT INTO feeding_logs_new (log_id, fed_at, memo) "
                      "SELECT log_id, fed_at, memo FROM feeding_logs")
            c.execute("DROP TABLE feeding_logs")
            c.execute("ALTER TABLE feeding_logs_new RENAME TO feeding_logs")
        c.execute("""
            CREATE TABLE IF NOT EXISTS mating_trials (
                trial_id            INTEGER PRIMARY KEY AUTOINCREMENT,
                planned_date        TEXT NOT NULL,
                male_id             TEXT NOT NULL,
                female_id           TEXT NOT NULL,
                source_tank_male    TEXT,
                source_tank_female  TEXT,
                breeding_tank_id    TEXT,
                status              TEXT NOT NULL DEFAULT '計画中'
                                    CHECK(status IN ('計画中','前日セット済み','採卵済み','戻し済み','中止')),
                setup_at            TEXT,
                divider_removed_at  TEXT,
                egg_collected_at    TEXT,
                returned_at         TEXT,
                spawning_history_id INTEGER,
                notes               TEXT,
                FOREIGN KEY (male_id)             REFERENCES individuals(individual_id),
                FOREIGN KEY (female_id)           REFERENCES individuals(individual_id),
                FOREIGN KEY (spawning_history_id) REFERENCES spawning_records(history_id)
            )
        """)
        conn.commit()


init_db()


# ============================================================
# 共通ヘルパ
# ============================================================
def fetch_df(query: str, params: tuple = ()) -> pd.DataFrame:
    with get_conn() as conn:
        return pd.read_sql_query(query, conn, params=params)


def execute(query: str, params: tuple = ()) -> int:
    with get_conn() as conn:
        cur = conn.execute(query, params)
        conn.commit()
        return cur.lastrowid


def to_csv_bytes(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode("utf-8-sig")


def csv_filename(prefix: str) -> str:
    return f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def hours_since(iso_str: Optional[str]) -> Optional[float]:
    if not iso_str:
        return None
    try:
        t = datetime.strptime(iso_str, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return (datetime.now() - t).total_seconds() / 3600


# ============================================================
# 画面設定
# ============================================================
st.set_page_config(page_title="ゼブラフィッシュ水槽管理", page_icon="🐟", layout="wide")

st.markdown(
    """
    <style>
    /* 温かみのある見出し */
    h1, h2, h3 { letter-spacing: 0.02em; }
    /* ボタンを少し丸く */
    .stButton>button { border-radius: 12px; padding: 0.5rem 1rem; }
    /* メトリクスカードの背景 */
    [data-testid="stMetric"] {
        background: #FFFDF8;
        padding: 12px 16px;
        border-radius: 14px;
        border: 1px solid #E8DDC8;
    }
    /* 警告色 */
    .feed-ok    { color: #5C8D5A; font-weight: 600; }
    .feed-warn  { color: #C48A2A; font-weight: 600; }
    .feed-alert { color: #B94A3A; font-weight: 700; }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("🐟 ゼブラフィッシュ水槽管理")
st.caption(f"今日は {date.today().strftime('%Y年%m月%d日')}")

(tab_dash, tab_feed, tab_trial, tab_analysis, tab_rack,
 tab_tank, tab_ind, tab_spawn) = st.tabs(
    [
        "📊 ダッシュボード",
        "🍚 餌やり",
        "💕 交配トライアル",
        "📈 成績分析",
        "📐 棚ビュー",
        "🪣 水槽管理",
        "🐠 個体管理",
        "🥚 産卵成績",
    ]
)


# ============================================================
# 📊 ダッシュボード
# ============================================================
with tab_dash:
    st.header("ダッシュボード")

    df_tanks = fetch_df("SELECT * FROM tanks")
    df_inds = fetch_df("SELECT * FROM individuals")
    df_spawn = fetch_df("SELECT * FROM spawning_records")
    df_trials = fetch_df("SELECT * FROM mating_trials")

    today = date.today().isoformat()
    df_feed_today = fetch_df(
        "SELECT * FROM feeding_logs WHERE substr(fed_at,1,10)=?", (today,)
    )

    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("🪣 水槽数", len(df_tanks))
    col2.metric("🐠 個体数", len(df_inds))
    col3.metric("🥚 産卵記録", len(df_spawn))
    alert_count = int((df_tanks["health_status"] == "要観察").sum()) if not df_tanks.empty else 0
    col4.metric("⚠️ 要観察", alert_count)
    in_progress = int(df_trials["status"].isin(["計画中", "前日セット済み", "採卵済み"]).sum()) if not df_trials.empty else 0
    col5.metric("💕 進行中トライアル", in_progress)

    st.divider()

    # 給餌サマリ
    left, right = st.columns(2)
    with left:
        st.subheader("🍚 本日の給餌状況")
        n_today = len(df_feed_today)
        last_log = fetch_df("SELECT fed_at FROM feeding_logs ORDER BY fed_at DESC LIMIT 1")
        last_fed_dash = last_log.iloc[0]["fed_at"] if not last_log.empty else None
        hrs_dash = hours_since(last_fed_dash)

        st.progress(min(n_today / FEEDS_PER_DAY, 1.0),
                    text=f"本日 {n_today} / {FEEDS_PER_DAY} 回")

        c_a, c_b = st.columns(2)
        c_a.metric("給餌回数", f"{n_today} / {FEEDS_PER_DAY}")
        c_b.metric("前回給餌",
                   last_fed_dash.split(" ")[1][:5] if last_fed_dash else "—",
                   f"{hrs_dash:.1f}時間前" if hrs_dash is not None else None)

        if hrs_dash is not None and hrs_dash >= FEED_ALERT_HOURS:
            st.error(f"⚠️ 前回給餌から {hrs_dash:.1f} 時間経過しています")
        elif hrs_dash is not None and hrs_dash >= FEED_WARN_HOURS:
            st.warning(f"前回給餌から {hrs_dash:.1f} 時間経過")

    with right:
        st.subheader("⚠️ 注意が必要な水槽")
        if df_tanks.empty:
            st.info("水槽が登録されていません")
        else:
            alert_df = df_tanks[df_tanks["health_status"] == "要観察"]
            isolated_df = df_tanks[df_tanks["health_status"] == "隔離中"]
            if alert_df.empty and isolated_df.empty:
                st.success("すべての水槽が良好です 🎉")
            if not alert_df.empty:
                st.warning("【要観察】")
                st.dataframe(alert_df[["tank_id", "current_individual_id", "memo"]],
                             use_container_width=True, hide_index=True)
            if not isolated_df.empty:
                st.error("【隔離中】")
                st.dataframe(isolated_df[["tank_id", "current_individual_id", "memo"]],
                             use_container_width=True, hide_index=True)

    st.divider()

    # 進行中トライアル
    st.subheader("💕 進行中の交配トライアル")
    if df_trials.empty:
        st.info("トライアルがまだ登録されていません")
    else:
        active = df_trials[df_trials["status"].isin(["計画中", "前日セット済み", "採卵済み"])]
        if active.empty:
            st.success("進行中のトライアルはありません")
        else:
            st.dataframe(
                active[["trial_id", "planned_date", "status", "male_id", "female_id", "breeding_tank_id"]],
                use_container_width=True, hide_index=True,
            )

    st.divider()
    st.subheader("📥 CSV ダウンロード")
    d1, d2, d3, d4, d5 = st.columns(5)
    with d1:
        st.download_button("水槽", to_csv_bytes(df_tanks), csv_filename("tanks"),
                           "text/csv", disabled=df_tanks.empty, use_container_width=True)
    with d2:
        st.download_button("個体", to_csv_bytes(df_inds), csv_filename("individuals"),
                           "text/csv", disabled=df_inds.empty, use_container_width=True)
    with d3:
        st.download_button("産卵成績", to_csv_bytes(df_spawn), csv_filename("spawning_records"),
                           "text/csv", disabled=df_spawn.empty, use_container_width=True)
    with d4:
        st.download_button("給餌ログ", to_csv_bytes(fetch_df("SELECT * FROM feeding_logs")),
                           csv_filename("feeding_logs"), "text/csv", use_container_width=True)
    with d5:
        st.download_button("トライアル", to_csv_bytes(df_trials), csv_filename("mating_trials"),
                           "text/csv", disabled=df_trials.empty, use_container_width=True)


# ============================================================
# 🍚 餌やり（全水槽一斉）
# ============================================================
with tab_feed:
    st.header("餌やりログ")
    st.caption(f"目標：1日 {FEEDS_PER_DAY} 回（全水槽に一斉に給餌）")

    today = date.today().isoformat()
    today_logs = fetch_df(
        "SELECT log_id, fed_at, memo FROM feeding_logs "
        "WHERE substr(fed_at,1,10)=? ORDER BY fed_at DESC", (today,),
    )
    last_log = fetch_df("SELECT fed_at FROM feeding_logs ORDER BY fed_at DESC LIMIT 1")

    count_today = len(today_logs)
    last_fed = last_log.iloc[0]["fed_at"] if not last_log.empty else None
    hrs = hours_since(last_fed)

    # メイン表示
    m1, m2, m3 = st.columns([2, 2, 3])
    m1.metric("🍚 本日の給餌回数", f"{count_today} / {FEEDS_PER_DAY}")
    m2.metric("🕒 前回給餌", last_fed.split(" ")[1][:5] if last_fed else "—")
    with m3:
        if hrs is None:
            st.markdown('<h4 class="feed-warn">まだ給餌記録がありません</h4>', unsafe_allow_html=True)
        elif hrs >= FEED_ALERT_HOURS:
            st.markdown(f'<h4 class="feed-alert">前回から {hrs:.1f} 時間（要給餌）</h4>', unsafe_allow_html=True)
        elif hrs >= FEED_WARN_HOURS:
            st.markdown(f'<h4 class="feed-warn">前回から {hrs:.1f} 時間</h4>', unsafe_allow_html=True)
        else:
            st.markdown(f'<h4 class="feed-ok">前回から {hrs:.1f} 時間</h4>', unsafe_allow_html=True)

    st.progress(min(count_today / FEEDS_PER_DAY, 1.0),
                text=f"本日の進捗 {count_today}/{FEEDS_PER_DAY}")

    st.divider()

    # 巨大ボタン
    if count_today >= FEEDS_PER_DAY:
        st.success(f"✅ 本日の目標 {FEEDS_PER_DAY} 回を達成しました！")
    btn_label = "🍚 全水槽に餌をあげた" if count_today < FEEDS_PER_DAY else "🍚 追加で記録する"
    memo = st.text_input("メモ（任意）", placeholder="例: 朝の分 / 担当者名 など")
    if st.button(btn_label, type="primary", use_container_width=True):
        execute(
            "INSERT INTO feeding_logs (fed_at, memo) VALUES (?, ?)",
            (now_iso(), memo or None),
        )
        st.rerun()

    st.divider()

    # 本日のログ
    st.subheader("本日の給餌ログ")
    if today_logs.empty:
        st.info("本日の記録はまだありません")
    else:
        st.dataframe(today_logs, use_container_width=True, hide_index=True)
        if st.button("⬅️ 直近の1件を取り消す"):
            execute("DELETE FROM feeding_logs WHERE log_id=?",
                    (int(today_logs.iloc[0]["log_id"]),))
            st.rerun()

    with st.expander("📜 過去の給餌ログ（全件）"):
        all_logs = fetch_df("SELECT * FROM feeding_logs ORDER BY fed_at DESC")
        st.dataframe(all_logs, use_container_width=True, hide_index=True)
        st.download_button(
            "📥 CSVダウンロード",
            to_csv_bytes(all_logs),
            csv_filename("feeding_logs"),
            "text/csv",
            disabled=all_logs.empty,
            key="dl_feeds",
        )


# ============================================================
# 💕 交配トライアル
# ============================================================
with tab_trial:
    st.header("交配トライアル管理")

    inds = fetch_df("SELECT individual_id, sex FROM individuals")
    males = inds[inds["sex"].isin(["オス", "混合"])]["individual_id"].tolist()
    females = inds[inds["sex"].isin(["メス", "混合"])]["individual_id"].tolist()
    tank_ids = fetch_df("SELECT tank_id FROM tanks ORDER BY tank_id")["tank_id"].tolist()

    # --- 新規計画 ---
    with st.expander("➕ 新規トライアルを計画する", expanded=False):
        with st.form("new_trial"):
            c1, c2 = st.columns(2)
            with c1:
                planned = st.date_input("採卵予定日", value=date.today() + timedelta(days=1))
                male = st.selectbox("オス", [""] + males)
                female = st.selectbox("メス", [""] + females)
            with c2:
                src_m = st.selectbox("オスの元水槽（戻し先）", [""] + tank_ids)
                src_f = st.selectbox("メスの元水槽（戻し先）", [""] + tank_ids)
                breed = st.selectbox("交配用水槽", [""] + tank_ids)
            notes = st.text_area("メモ")
            ok = st.form_submit_button("計画を登録", type="primary")
            if ok:
                if not male or not female:
                    st.error("オスとメスを選択してください")
                else:
                    execute(
                        """INSERT INTO mating_trials
                           (planned_date, male_id, female_id, source_tank_male, source_tank_female,
                            breeding_tank_id, notes)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (planned.isoformat(), male, female, src_m or None, src_f or None,
                         breed or None, notes),
                    )
                    st.success("計画を登録しました")
                    st.rerun()

    st.divider()

    # --- 進行中トライアル ---
    st.subheader("🔄 進行中のトライアル")
    active = fetch_df(
        "SELECT * FROM mating_trials WHERE status IN ('計画中','前日セット済み','採卵済み') "
        "ORDER BY planned_date"
    )
    if active.empty:
        st.info("進行中のトライアルはありません")
    else:
        for _, t in active.iterrows():
            tid = int(t["trial_id"])
            status = t["status"]
            badge = {"計画中": "🟦", "前日セット済み": "🟨", "採卵済み": "🟧"}.get(status, "⬜")
            with st.container():
                top = st.columns([3, 2, 2])
                top[0].markdown(f"### {badge} Trial #{tid} — {status}")
                top[0].caption(f"予定日: {t['planned_date']}　♂ {t['male_id']}　×　♀ {t['female_id']}")
                top[1].markdown(f"**交配用水槽:** {t['breeding_tank_id'] or '-'}")
                top[2].markdown(f"**戻し先:** ♂{t['source_tank_male'] or '-'} / ♀{t['source_tank_female'] or '-'}")

                # 状態に応じた次アクション
                if status == "計画中":
                    if st.button("✅ 前日セット完了にする", key=f"setup_{tid}", type="primary"):
                        execute(
                            "UPDATE mating_trials SET status='前日セット済み', setup_at=? WHERE trial_id=?",
                            (now_iso(), tid),
                        )
                        st.rerun()
                elif status == "前日セット済み":
                    c = st.columns(2)
                    if c[0].button("🔓 仕切り取り出し（交配開始）", key=f"div_{tid}"):
                        execute(
                            "UPDATE mating_trials SET divider_removed_at=? WHERE trial_id=?",
                            (now_iso(), tid),
                        )
                        st.rerun()
                    if c[1].button("🥚 採卵完了 → 結果入力", key=f"collect_{tid}", type="primary"):
                        st.session_state[f"collecting_{tid}"] = True
                elif status == "採卵済み":
                    if st.button("🏠 戻し完了にする", key=f"return_{tid}", type="primary"):
                        execute(
                            "UPDATE mating_trials SET status='戻し済み', returned_at=? WHERE trial_id=?",
                            (now_iso(), tid),
                        )
                        st.rerun()

                # 採卵結果入力フォーム
                if st.session_state.get(f"collecting_{tid}"):
                    with st.form(f"collect_form_{tid}"):
                        st.markdown("**採卵結果を入力**")
                        cc = st.columns(2)
                        eggs = cc[0].number_input("採卵数", min_value=0, step=1, key=f"eggs_{tid}")
                        rate = cc[1].number_input("受精率(%)", min_value=0.0, max_value=100.0,
                                                  step=0.1, key=f"rate_{tid}")
                        sub = st.form_submit_button("登録", type="primary")
                        if sub:
                            hist_id = execute(
                                """INSERT INTO spawning_records
                                   (spawning_date, male_parent_id, female_parent_id, egg_count, fertilization_rate)
                                   VALUES (?, ?, ?, ?, ?)""",
                                (t["planned_date"], t["male_id"], t["female_id"], int(eggs), float(rate)),
                            )
                            execute(
                                """UPDATE mating_trials
                                   SET status='採卵済み', egg_collected_at=?, spawning_history_id=?
                                   WHERE trial_id=?""",
                                (now_iso(), hist_id, tid),
                            )
                            st.session_state[f"collecting_{tid}"] = False
                            st.success("採卵結果を登録しました（産卵成績に自動反映）")
                            st.rerun()

                # キャンセル
                with st.expander("⛔ このトライアルを中止"):
                    if st.button("中止する", key=f"cancel_{tid}"):
                        execute("UPDATE mating_trials SET status='中止' WHERE trial_id=?", (tid,))
                        st.rerun()
            st.divider()

    st.divider()

    # --- 完了済みトライアル ---
    st.subheader("📚 完了・中止トライアル")
    done = fetch_df(
        "SELECT trial_id, planned_date, male_id, female_id, status, "
        "spawning_history_id, breeding_tank_id, notes "
        "FROM mating_trials WHERE status IN ('戻し済み','中止') ORDER BY planned_date DESC"
    )
    st.dataframe(done, use_container_width=True, hide_index=True)
    st.download_button("📥 CSVダウンロード", to_csv_bytes(done),
                       csv_filename("mating_trials_done"), "text/csv",
                       disabled=done.empty, key="dl_trials_done")


# ============================================================
# 📈 成績分析
# ============================================================
with tab_analysis:
    st.header("成績分析")
    st.caption("過去の産卵成績から、成功率の高い個体・ペアを自動で算出します")

    df_spawn = fetch_df("SELECT * FROM spawning_records")
    if df_spawn.empty:
        st.info("まだ産卵成績が登録されていません。記録が溜まると分析できるようになります。")
    else:
        df = df_spawn.copy()
        df["success"] = (df["egg_count"].fillna(0) > 0).astype(int)

        def aggregate(by_col: str, label: str) -> pd.DataFrame:
            g = df.groupby(by_col).agg(
                試行回数=("history_id", "count"),
                成功回数=("success", "sum"),
                平均採卵数=("egg_count", "mean"),
                平均受精率=("fertilization_rate", "mean"),
                最終試行日=("spawning_date", "max"),
            ).reset_index().rename(columns={by_col: label})
            g["成功率(%)"] = (g["成功回数"] / g["試行回数"] * 100).round(1)
            g["平均採卵数"] = g["平均採卵数"].round(1)
            g["平均受精率"] = g["平均受精率"].round(1)
            g["信頼度"] = g["試行回数"].apply(lambda n: "⭐" * min(int(n // 3) + 1, 3) if n >= 1 else "")
            return g.sort_values(["成功率(%)", "試行回数"], ascending=[False, False])

        st.subheader("♂ オス別ランキング")
        st.dataframe(
            aggregate("male_parent_id", "オスID")[
                ["オスID", "試行回数", "成功率(%)", "平均採卵数", "平均受精率", "最終試行日", "信頼度"]
            ],
            use_container_width=True, hide_index=True,
        )

        st.subheader("♀ メス別ランキング")
        st.dataframe(
            aggregate("female_parent_id", "メスID")[
                ["メスID", "試行回数", "成功率(%)", "平均採卵数", "平均受精率", "最終試行日", "信頼度"]
            ],
            use_container_width=True, hide_index=True,
        )

        st.subheader("💞 ペア別ランキング")
        pair = df.copy()
        pair["pair"] = pair["male_parent_id"].astype(str) + " × " + pair["female_parent_id"].astype(str)
        g = pair.groupby("pair").agg(
            試行回数=("history_id", "count"),
            成功回数=("success", "sum"),
            平均採卵数=("egg_count", "mean"),
            平均受精率=("fertilization_rate", "mean"),
            最終試行日=("spawning_date", "max"),
        ).reset_index().rename(columns={"pair": "ペア"})
        g["成功率(%)"] = (g["成功回数"] / g["試行回数"] * 100).round(1)
        g["平均採卵数"] = g["平均採卵数"].round(1)
        g["平均受精率"] = g["平均受精率"].round(1)
        g["信頼度"] = g["試行回数"].apply(lambda n: "⭐" * min(int(n // 2) + 1, 3) if n >= 1 else "")
        g = g.sort_values(["成功率(%)", "平均採卵数"], ascending=[False, False])
        st.dataframe(
            g[["ペア", "試行回数", "成功率(%)", "平均採卵数", "平均受精率", "最終試行日", "信頼度"]],
            use_container_width=True, hide_index=True,
        )

        st.divider()
        st.subheader("✨ 次回おすすめペア Top 5")
        st.caption("成功率 × 平均採卵数 × 受精率 を重み付けしたスコア（試行3回以上）")
        reliable = g[g["試行回数"] >= 3].copy()
        if reliable.empty:
            st.info("試行3回以上のペアがまだありません（信頼度を出すには各ペア3回以上のデータが必要）")
        else:
            reliable["スコア"] = (
                reliable["成功率(%)"].fillna(0) * 0.5
                + reliable["平均採卵数"].fillna(0) * 0.3
                + reliable["平均受精率"].fillna(0) * 0.2
            ).round(1)
            top = reliable.sort_values("スコア", ascending=False).head(5)
            st.dataframe(
                top[["ペア", "スコア", "試行回数", "成功率(%)", "平均採卵数", "平均受精率", "最終試行日"]],
                use_container_width=True, hide_index=True,
            )


# ============================================================
# 📐 棚ビュー
# ============================================================
with tab_rack:
    st.header("棚ビュー")
    st.caption(
        f"{len(RACKS)} ラック × {len(TIERS)} 段 × {len(COLS)} 列 ＝ 最大 "
        f"{len(RACKS)*len(TIERS)*len(COLS)} 水槽。色は健康状態を示します。"
    )

    # 凡例
    legend = (
        '<div style="display:flex;gap:14px;align-items:center;margin:8px 0 16px 0;font-size:13px">'
        f'<span style="background:{HEALTH_COLOR["良好"]};padding:3px 12px;border-radius:6px">良好</span>'
        f'<span style="background:{HEALTH_COLOR["要観察"]};padding:3px 12px;border-radius:6px">要観察</span>'
        f'<span style="background:{HEALTH_COLOR["隔離中"]};padding:3px 12px;border-radius:6px">隔離中</span>'
        f'<span style="background:{EMPTY_COLOR};padding:3px 12px;border-radius:6px;color:#888">未登録</span>'
        '</div>'
    )
    st.markdown(legend, unsafe_allow_html=True)

    df_all = fetch_df("SELECT * FROM tanks")

    # サマリ
    sc1, sc2, sc3, sc4 = st.columns(4)
    sc1.metric("登録済み水槽", len(df_all))
    sc2.metric("良好", int((df_all["health_status"] == "良好").sum()) if not df_all.empty else 0)
    sc3.metric("要観察", int((df_all["health_status"] == "要観察").sum()) if not df_all.empty else 0)
    sc4.metric("隔離中", int((df_all["health_status"] == "隔離中").sum()) if not df_all.empty else 0)

    st.divider()

    # 4ラックを縦並び表示
    if df_all.empty:
        st.info("水槽がまだ登録されていません。「水槽管理」タブから登録するか、CSV一括インポートを使ってください。")
    else:
        for rack in RACKS:
            sub = df_all[df_all["rack"] == rack]
            st.markdown(render_rack_html(rack, sub), unsafe_allow_html=True)


# ============================================================
# 🪣 水槽管理
# ============================================================
with tab_tank:
    st.header("水槽管理")
    st.caption(f"配置：{len(RACKS)}ラック × {len(TIERS)}段 × {len(COLS)}列 = "
               f"最大 {len(RACKS)*len(TIERS)*len(COLS)} 水槽")

    ind_ids = fetch_df("SELECT individual_id FROM individuals")["individual_id"].tolist()

    with st.form("tank_form", clear_on_submit=True):
        st.subheader("水槽の登録 / 更新")
        c1, c2 = st.columns(2)
        with c1:
            tank_id = st.text_input("水槽ID（例: T-001 / または場所コードと同じ B-2-05 でもOK）")
            current_ind = st.selectbox("現在の個体ID", [""] + ind_ids)
            health = st.selectbox("健康状態", ["良好", "要観察", "隔離中"])
        with c2:
            st.markdown("**場所**（ラック - 段 - 列）")
            lc1, lc2, lc3 = st.columns(3)
            rack = lc1.selectbox("ラック", [""] + RACKS, key="form_rack")
            tier_str = lc2.selectbox("段", [""] + [str(t) for t in TIERS], key="form_tier")
            col_str = lc3.selectbox("列", [""] + [f"{c:02d}" for c in COLS], key="form_col")
            preview = format_location(
                rack or None,
                int(tier_str) if tier_str else None,
                int(col_str) if col_str else None,
            )
            st.caption(f"場所コード：**{preview or '（未設定）'}**")
            memo = st.text_area("メモ", height=80)

        if st.form_submit_button("登録 / 更新", type="primary"):
            if not tank_id.strip():
                st.error("水槽IDを入力してください")
            else:
                execute(
                    """INSERT INTO tanks (tank_id, current_individual_id, health_status, memo, rack, tier, col_no)
                       VALUES (?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(tank_id) DO UPDATE SET
                         current_individual_id=excluded.current_individual_id,
                         health_status=excluded.health_status,
                         memo=excluded.memo,
                         rack=excluded.rack,
                         tier=excluded.tier,
                         col_no=excluded.col_no""",
                    (tank_id.strip(), current_ind or None, health, memo,
                     rack or None,
                     int(tier_str) if tier_str else None,
                     int(col_str) if col_str else None),
                )
                st.success(f"水槽 {tank_id} を登録/更新しました")

    st.divider()
    st.subheader("登録済み水槽")

    df = fetch_df("SELECT * FROM tanks ORDER BY rack, tier, col_no, tank_id")
    if not df.empty:
        df["場所"] = df.apply(
            lambda r: format_location(r["rack"], r["tier"], r["col_no"]), axis=1
        )

        # フィルタ
        fc1, fc2, fc3 = st.columns(3)
        f_rack = fc1.multiselect("ラックで絞り込み", RACKS, default=[])
        f_tier = fc2.multiselect("段で絞り込み", TIERS, default=[])
        f_health = fc3.multiselect("健康状態で絞り込み", ["良好", "要観察", "隔離中"], default=[])

        view = df.copy()
        if f_rack:
            view = view[view["rack"].isin(f_rack)]
        if f_tier:
            view = view[view["tier"].isin(f_tier)]
        if f_health:
            view = view[view["health_status"].isin(f_health)]

        st.caption(f"表示中: {len(view)} / 全 {len(df)} 水槽")
        st.dataframe(
            view[["tank_id", "場所", "rack", "tier", "col_no",
                  "current_individual_id", "health_status", "memo"]],
            use_container_width=True, hide_index=True,
        )
    else:
        st.info("まだ水槽が登録されていません")

    st.download_button("📥 CSVダウンロード", to_csv_bytes(df), csv_filename("tanks"),
                       "text/csv", disabled=df.empty, key="dl_tanks")

    # ----------------------------------------------------------------
    # CSV 一括インポート
    # ----------------------------------------------------------------
    with st.expander("📤 CSV 一括インポート（240水槽まとめて登録）", expanded=False):
        st.markdown(
            "Excelで水槽リストを作って一気に登録できます。"
            "**同じ tank_id があれば上書き、無ければ新規登録** されます。"
        )

        # テンプレート
        template_rows = []
        for r in RACKS[:1]:
            for t in TIERS[:2]:
                for c in COLS[:2]:
                    template_rows.append({
                        "tank_id": f"{r}-{t}-{c:02d}",
                        "rack": r, "tier": t, "col_no": c,
                        "health_status": "良好",
                        "current_individual_id": "",
                        "memo": "",
                    })
        template_df = pd.DataFrame(
            template_rows,
            columns=["tank_id", "rack", "tier", "col_no",
                     "health_status", "current_individual_id", "memo"],
        )
        st.download_button(
            "📄 テンプレートCSVをダウンロード",
            to_csv_bytes(template_df),
            "tank_template.csv",
            "text/csv",
            key="dl_template",
        )
        st.caption(
            "必須列：tank_id, rack, tier, col_no　／　任意列：health_status, current_individual_id, memo"
        )

        uploaded = st.file_uploader("CSVファイルを選択", type=["csv"], key="csv_upload")
        if uploaded is not None:
            try:
                # BOM付きでも普通でも読めるよう utf-8-sig
                up_df = pd.read_csv(uploaded, encoding="utf-8-sig", dtype=str).fillna("")
            except Exception as e:
                st.error(f"CSV読み込みエラー：{e}")
                up_df = None

            if up_df is not None:
                st.success(f"📂 {len(up_df)} 行を読み込みました")
                st.dataframe(up_df.head(20), use_container_width=True, hide_index=True)
                if len(up_df) > 20:
                    st.caption(f"...残り {len(up_df) - 20} 行はインポート時に処理します")

                # 検証
                errors = []
                warnings_list = []

                required_cols = ["tank_id", "rack", "tier", "col_no"]
                missing = [c for c in required_cols if c not in up_df.columns]
                if missing:
                    errors.append(f"必須列が不足しています: {missing}")

                if not errors:
                    bad_rows = []
                    for i, row in up_df.iterrows():
                        rownum = i + 2  # 1行目はヘッダなので+2
                        if not str(row["tank_id"]).strip():
                            bad_rows.append(f"行{rownum}: tank_id が空")
                            continue
                        rack_v = str(row["rack"]).strip()
                        if rack_v and rack_v not in RACKS:
                            bad_rows.append(f"行{rownum}: rack='{rack_v}' は {RACKS} のいずれかにしてください")
                        if str(row["tier"]).strip():
                            try:
                                tv = int(float(row["tier"]))
                                if tv not in TIERS:
                                    bad_rows.append(f"行{rownum}: tier={tv} は {TIERS} のいずれかにしてください")
                            except ValueError:
                                bad_rows.append(f"行{rownum}: tier='{row['tier']}' が数値ではありません")
                        if str(row["col_no"]).strip():
                            try:
                                cv = int(float(row["col_no"]))
                                if cv not in COLS:
                                    bad_rows.append(f"行{rownum}: col_no={cv} は 1〜{len(COLS)} の範囲外")
                            except ValueError:
                                bad_rows.append(f"行{rownum}: col_no='{row['col_no']}' が数値ではありません")
                        if "health_status" in up_df.columns:
                            hs = str(row["health_status"]).strip()
                            if hs and hs not in ["良好", "要観察", "隔離中"]:
                                bad_rows.append(f"行{rownum}: health_status='{hs}' は [良好/要観察/隔離中] のいずれか")

                    if bad_rows:
                        errors.extend(bad_rows[:20])
                        if len(bad_rows) > 20:
                            errors.append(f"...他に {len(bad_rows) - 20} 件のエラー")

                    # 重複 tank_id
                    dups = up_df["tank_id"][up_df["tank_id"].duplicated()].unique().tolist()
                    if len(dups):
                        warnings_list.append(f"CSV内で重複する tank_id: {dups[:10]}")

                if errors:
                    st.error("❌ 検証エラーがあります（修正してから再アップロードしてください）")
                    for e in errors:
                        st.write(f"- {e}")
                else:
                    for w in warnings_list:
                        st.warning(w)
                    st.info(f"✅ 検証OK：{len(up_df)} 行を登録/更新します")
                    if st.button("⬆️ インポート実行", type="primary", key="csv_import_btn"):
                        ok, ng = 0, 0
                        with get_conn() as conn:
                            for _, row in up_df.iterrows():
                                try:
                                    rack_v = str(row.get("rack", "")).strip() or None
                                    tier_v = int(float(row["tier"])) if str(row.get("tier", "")).strip() else None
                                    col_v = int(float(row["col_no"])) if str(row.get("col_no", "")).strip() else None
                                    hs_v = str(row.get("health_status", "")).strip() or "良好"
                                    ind_v = str(row.get("current_individual_id", "")).strip() or None
                                    memo_v = str(row.get("memo", "")).strip() or None
                                    conn.execute(
                                        """INSERT INTO tanks
                                           (tank_id, current_individual_id, health_status, memo,
                                            rack, tier, col_no)
                                           VALUES (?, ?, ?, ?, ?, ?, ?)
                                           ON CONFLICT(tank_id) DO UPDATE SET
                                             current_individual_id=excluded.current_individual_id,
                                             health_status=excluded.health_status,
                                             memo=excluded.memo,
                                             rack=excluded.rack,
                                             tier=excluded.tier,
                                             col_no=excluded.col_no""",
                                        (str(row["tank_id"]).strip(), ind_v, hs_v, memo_v,
                                         rack_v, tier_v, col_v),
                                    )
                                    ok += 1
                                except Exception:
                                    ng += 1
                            conn.commit()
                        st.success(f"🎉 {ok} 件を登録/更新しました" + (f"（失敗 {ng} 件）" if ng else ""))
                        st.rerun()

    with st.expander("水槽を削除する"):
        if not df.empty:
            del_id = st.selectbox("削除する水槽ID", df["tank_id"].tolist(), key="del_tank")
            if st.button("削除", type="primary", key="del_tank_btn"):
                execute("DELETE FROM tanks WHERE tank_id = ?", (del_id,))
                st.rerun()


# ============================================================
# 🐠 個体管理
# ============================================================
with tab_ind:
    st.header("個体管理")
    with st.form("ind_form", clear_on_submit=True):
        st.subheader("個体の登録 / 更新")
        c1, c2 = st.columns(2)
        with c1:
            ind_id = st.text_input("個体ID（例: F-001）")
            birth = st.date_input("生まれた日付", value=date.today())
        with c2:
            sex = st.selectbox("性別", ["オス", "メス", "混合"])
            lineage = st.text_input("系統名（例: AB, TU, WIK）")
        if st.form_submit_button("登録 / 更新", type="primary"):
            if not ind_id.strip():
                st.error("個体IDを入力してください")
            else:
                execute(
                    """INSERT INTO individuals (individual_id, birth_date, sex, lineage)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(individual_id) DO UPDATE SET
                         birth_date=excluded.birth_date,
                         sex=excluded.sex,
                         lineage=excluded.lineage""",
                    (ind_id.strip(), birth.isoformat(), sex, lineage),
                )
                st.success(f"個体 {ind_id} を登録/更新しました")

    st.divider()
    st.subheader("登録済み個体")
    df = fetch_df("SELECT * FROM individuals ORDER BY individual_id")
    st.dataframe(df, use_container_width=True, hide_index=True)
    st.download_button("📥 CSVダウンロード", to_csv_bytes(df), csv_filename("individuals"),
                       "text/csv", disabled=df.empty, key="dl_individuals")

    with st.expander("個体を削除する"):
        if not df.empty:
            del_id = st.selectbox("削除する個体ID", df["individual_id"].tolist(), key="del_ind")
            if st.button("削除", type="primary", key="del_ind_btn"):
                execute("DELETE FROM individuals WHERE individual_id = ?", (del_id,))
                st.rerun()


# ============================================================
# 🥚 産卵成績（手入力 & 履歴）
# ============================================================
with tab_spawn:
    st.header("産卵成績")
    st.caption("交配トライアル経由で自動登録されるほか、ここから手入力もできます")

    inds = fetch_df("SELECT individual_id, sex FROM individuals")
    males = inds[inds["sex"].isin(["オス", "混合"])]["individual_id"].tolist()
    females = inds[inds["sex"].isin(["メス", "混合"])]["individual_id"].tolist()

    with st.form("spawn_form", clear_on_submit=True):
        st.subheader("手入力で追加")
        c1, c2, c3 = st.columns(3)
        with c1:
            sdate = st.date_input("産卵日", value=date.today())
            male = st.selectbox("オス親ID", [""] + males)
        with c2:
            female = st.selectbox("メス親ID", [""] + females)
            eggs = st.number_input("採卵数", min_value=0, step=1)
        with c3:
            rate = st.number_input("受精率 (%)", min_value=0.0, max_value=100.0, step=0.1)
        if st.form_submit_button("登録", type="primary"):
            if not male or not female:
                st.error("オス親IDとメス親IDを選択してください")
            else:
                execute(
                    """INSERT INTO spawning_records
                       (spawning_date, male_parent_id, female_parent_id, egg_count, fertilization_rate)
                       VALUES (?, ?, ?, ?, ?)""",
                    (sdate.isoformat(), male, female, int(eggs), float(rate)),
                )
                st.success("産卵成績を登録しました")

    st.divider()
    st.subheader("産卵成績履歴")
    df = fetch_df("SELECT * FROM spawning_records ORDER BY spawning_date DESC, history_id DESC")
    st.dataframe(df, use_container_width=True, hide_index=True)
    st.download_button("📥 CSVダウンロード", to_csv_bytes(df), csv_filename("spawning_records"),
                       "text/csv", disabled=df.empty, key="dl_spawning")

    if not df.empty:
        st.subheader("受精率の推移")
        chart_df = df.copy()
        chart_df["spawning_date"] = pd.to_datetime(chart_df["spawning_date"])
        chart_df = chart_df.sort_values("spawning_date").set_index("spawning_date")
        st.line_chart(chart_df["fertilization_rate"])
