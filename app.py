"""
ゼブラフィッシュ水槽管理 Webアプリ
- Streamlit + SQLite の1ファイル構成
- 起動: streamlit run app.py
"""

import sqlite3
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import streamlit as st

DB_PATH = Path(__file__).parent / "zebrafish.db"


def to_csv_bytes(df: pd.DataFrame) -> bytes:
    # Excelで文字化けしないようBOM付きUTF-8
    return df.to_csv(index=False).encode("utf-8-sig")


def csv_filename(prefix: str) -> str:
    return f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"


# ============================================================
# データベース初期化
# ============================================================
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with get_conn() as conn:
        c = conn.cursor()

        # 個体テーブル
        c.execute("""
            CREATE TABLE IF NOT EXISTS individuals (
                individual_id TEXT PRIMARY KEY,
                birth_date    TEXT,
                sex           TEXT CHECK(sex IN ('オス','メス','混合')),
                lineage       TEXT
            )
        """)

        # 水槽テーブル
        c.execute("""
            CREATE TABLE IF NOT EXISTS tanks (
                tank_id               TEXT PRIMARY KEY,
                current_individual_id TEXT,
                health_status         TEXT CHECK(health_status IN ('良好','要観察','隔離中')),
                memo                  TEXT,
                FOREIGN KEY (current_individual_id) REFERENCES individuals(individual_id)
            )
        """)

        # 産卵成績テーブル
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
        conn.commit()


init_db()


# ============================================================
# データ取得ヘルパ
# ============================================================
def fetch_df(query: str, params: tuple = ()) -> pd.DataFrame:
    with get_conn() as conn:
        return pd.read_sql_query(query, conn, params=params)


def execute(query: str, params: tuple = ()):
    with get_conn() as conn:
        conn.execute(query, params)
        conn.commit()


# ============================================================
# 画面
# ============================================================
st.set_page_config(page_title="ゼブラフィッシュ水槽管理", page_icon="🐟", layout="wide")
st.title("🐟 ゼブラフィッシュ水槽管理システム")

tab_dash, tab_tank, tab_ind, tab_spawn = st.tabs(
    ["📊 ダッシュボード", "🪣 水槽管理", "🐠 個体管理", "🥚 産卵成績"]
)


# ---------- ダッシュボード ----------
with tab_dash:
    st.header("ダッシュボード")

    df_tanks = fetch_df("SELECT * FROM tanks")
    df_inds = fetch_df("SELECT * FROM individuals")
    df_spawn = fetch_df("SELECT * FROM spawning_records")

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("水槽数", len(df_tanks))
    col2.metric("個体数", len(df_inds))
    col3.metric("産卵記録数", len(df_spawn))
    alert_count = int((df_tanks["health_status"] == "要観察").sum()) if not df_tanks.empty else 0
    col4.metric("⚠️ 要観察の水槽", alert_count)

    st.divider()

    if df_tanks.empty:
        st.info("水槽がまだ登録されていません。「水槽管理」タブから登録してください。")
    else:
        alert_df = df_tanks[df_tanks["health_status"] == "要観察"]
        if not alert_df.empty:
            st.warning("⚠️ 健康状態が「要観察」の水槽があります")
            st.dataframe(alert_df, use_container_width=True, hide_index=True)

        isolated_df = df_tanks[df_tanks["health_status"] == "隔離中"]
        if not isolated_df.empty:
            st.error("🚨 隔離中の水槽")
            st.dataframe(isolated_df, use_container_width=True, hide_index=True)

        st.subheader("全水槽一覧")
        st.dataframe(df_tanks, use_container_width=True, hide_index=True)

    st.divider()
    st.subheader("📥 CSVダウンロード")
    d1, d2, d3 = st.columns(3)
    with d1:
        st.download_button(
            "水槽データ",
            data=to_csv_bytes(df_tanks),
            file_name=csv_filename("tanks"),
            mime="text/csv",
            disabled=df_tanks.empty,
            use_container_width=True,
        )
    with d2:
        st.download_button(
            "個体データ",
            data=to_csv_bytes(df_inds),
            file_name=csv_filename("individuals"),
            mime="text/csv",
            disabled=df_inds.empty,
            use_container_width=True,
        )
    with d3:
        st.download_button(
            "産卵成績データ",
            data=to_csv_bytes(df_spawn),
            file_name=csv_filename("spawning_records"),
            mime="text/csv",
            disabled=df_spawn.empty,
            use_container_width=True,
        )


# ---------- 水槽管理 ----------
with tab_tank:
    st.header("水槽管理")

    ind_ids = fetch_df("SELECT individual_id FROM individuals")["individual_id"].tolist()

    with st.form("tank_form", clear_on_submit=True):
        st.subheader("水槽の登録 / 更新")
        c1, c2 = st.columns(2)
        with c1:
            tank_id = st.text_input("水槽ID（例: T-001）")
            current_ind = st.selectbox("現在の個体ID", [""] + ind_ids)
        with c2:
            health = st.selectbox("健康状態", ["良好", "要観察", "隔離中"])
            memo = st.text_area("メモ", height=80)
        submitted = st.form_submit_button("登録 / 更新")
        if submitted:
            if not tank_id.strip():
                st.error("水槽IDを入力してください")
            else:
                execute(
                    """INSERT INTO tanks (tank_id, current_individual_id, health_status, memo)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(tank_id) DO UPDATE SET
                         current_individual_id=excluded.current_individual_id,
                         health_status=excluded.health_status,
                         memo=excluded.memo""",
                    (tank_id.strip(), current_ind or None, health, memo),
                )
                st.success(f"水槽 {tank_id} を登録 / 更新しました")

    st.divider()
    st.subheader("登録済み水槽")
    df = fetch_df("SELECT * FROM tanks ORDER BY tank_id")
    st.dataframe(df, use_container_width=True, hide_index=True)
    st.download_button(
        "📥 CSVダウンロード",
        data=to_csv_bytes(df),
        file_name=csv_filename("tanks"),
        mime="text/csv",
        disabled=df.empty,
        key="dl_tanks",
    )

    with st.expander("水槽を削除する"):
        if not df.empty:
            del_id = st.selectbox("削除する水槽ID", df["tank_id"].tolist(), key="del_tank")
            if st.button("削除", type="primary"):
                execute("DELETE FROM tanks WHERE tank_id = ?", (del_id,))
                st.success(f"水槽 {del_id} を削除しました")
                st.rerun()


# ---------- 個体管理 ----------
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
        submitted = st.form_submit_button("登録 / 更新")
        if submitted:
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
                st.success(f"個体 {ind_id} を登録 / 更新しました")

    st.divider()
    st.subheader("登録済み個体")
    df = fetch_df("SELECT * FROM individuals ORDER BY individual_id")
    st.dataframe(df, use_container_width=True, hide_index=True)
    st.download_button(
        "📥 CSVダウンロード",
        data=to_csv_bytes(df),
        file_name=csv_filename("individuals"),
        mime="text/csv",
        disabled=df.empty,
        key="dl_individuals",
    )

    with st.expander("個体を削除する"):
        if not df.empty:
            del_id = st.selectbox("削除する個体ID", df["individual_id"].tolist(), key="del_ind")
            if st.button("削除", type="primary", key="del_ind_btn"):
                execute("DELETE FROM individuals WHERE individual_id = ?", (del_id,))
                st.success(f"個体 {del_id} を削除しました")
                st.rerun()


# ---------- 産卵成績 ----------
with tab_spawn:
    st.header("産卵成績")

    males = fetch_df(
        "SELECT individual_id FROM individuals WHERE sex IN ('オス','混合') ORDER BY individual_id"
    )["individual_id"].tolist()
    females = fetch_df(
        "SELECT individual_id FROM individuals WHERE sex IN ('メス','混合') ORDER BY individual_id"
    )["individual_id"].tolist()

    with st.form("spawn_form", clear_on_submit=True):
        st.subheader("産卵記録の追加")
        c1, c2, c3 = st.columns(3)
        with c1:
            sdate = st.date_input("産卵日", value=date.today())
            male = st.selectbox("オス親ID", [""] + males)
        with c2:
            female = st.selectbox("メス親ID", [""] + females)
            eggs = st.number_input("採卵数", min_value=0, step=1)
        with c3:
            rate = st.number_input("受精率 (%)", min_value=0.0, max_value=100.0, step=0.1)
        submitted = st.form_submit_button("登録")
        if submitted:
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
    st.download_button(
        "📥 CSVダウンロード",
        data=to_csv_bytes(df),
        file_name=csv_filename("spawning_records"),
        mime="text/csv",
        disabled=df.empty,
        key="dl_spawning",
    )

    if not df.empty:
        st.subheader("受精率の推移")
        chart_df = df.copy()
        chart_df["spawning_date"] = pd.to_datetime(chart_df["spawning_date"])
        chart_df = chart_df.sort_values("spawning_date").set_index("spawning_date")
        st.line_chart(chart_df["fertilization_rate"])
