"""
ゼブラフィッシュ水槽管理 Webアプリ v2.0
- Streamlit + SQLite の1ファイル構成
- 機能: 餌やりログ / 交配トライアル管理 / ペア成功率分析 / CSV出力
- 起動: streamlit run app.py
"""

import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
import streamlit as st

# === タイムゾーン: 日本標準時で固定 ===
# Streamlit Cloud のサーバは UTC なので、必ずこのヘルパを使う
JST = timezone(timedelta(hours=9))

def now_jst() -> datetime:
    """JST タイムゾーン付きの現在時刻"""
    return datetime.now(JST)

def now_naive_jst() -> datetime:
    """DB 保存用のナイーブ JST 時刻（比較を簡単にする）"""
    return now_jst().replace(tzinfo=None)

def today_jst() -> date:
    """今日の日付（JST）"""
    return now_jst().date()

DB_PATH = Path(__file__).parent / "zebrafish.db"

FEEDS_PER_DAY = 4
FEED_WARN_HOURS = 2     # この時間を超えたら黄色
FEED_ALERT_HOURS = 6    # この時間を超えたら赤
TRIAL_STATUSES = ["計画中", "前日セット済み", "採卵済み", "戻し済み", "中止"]

# 水槽の物理配置（UI / app_settings から動的に変更可）
DEFAULT_RACKS = ["wild", "genom1", "genom2", "genom3"]   # ラック名
DEFAULT_TIERS = ["A", "B", "C", "D"]                      # 段（文字）
COLS = list(range(1, 16))                                 # 列 1〜15


def format_location(rack, tier, col_no):
    """場所コードを 'wild-A-01' 形式に整形。値が欠けていれば空文字を返す。"""
    if not rack or tier is None or tier == "" or col_no is None:
        return ""
    try:
        col_str = f"{int(col_no):02d}"
    except (ValueError, TypeError):
        return ""
    return f"{rack}-{tier}-{col_str}"


HEALTH_COLOR = {
    "良好":   "#B8DDB6",
    "要観察": "#F5D7A1",
    "隔離中": "#E8B4A8",
}
EMPTY_COLOR = "#F5F2EC"


def render_rack_html(rack_name, df_sub):
    """1つのラックを 段×列 のHTMLテーブルとして組み立てる。"""
    by_loc = {}
    for r in df_sub.itertuples():
        if pd.notna(r.tier) and pd.notna(r.col_no):
            try:
                by_loc[(str(r.tier), int(r.col_no))] = r
            except (ValueError, TypeError):
                continue

    parts = [f'<div style="margin:0 0 1.5rem 0">'
             f'<h4 style="margin:0 0 8px 0">🏠 ラック {rack_name}</h4>'
             f'<table style="border-collapse:separate;border-spacing:3px;font-size:11px;">']
    parts.append('<tr><th style="width:48px"></th>')
    for c in COLS:
        parts.append(f'<th style="padding:2px;color:#888;font-weight:500;width:48px">{c:02d}</th>')
    parts.append('</tr>')

    for t in TIERS:
        parts.append(f'<tr><th style="padding:4px;color:#888;text-align:right">段{t}</th>')
        for c in COLS:
            tank = by_loc.get((str(t), c))
            if tank is None:
                bg, label, tip = EMPTY_COLOR, "—", "未登録"
            else:
                bg = HEALTH_COLOR.get(tank.health_status, "#E8DDC8")
                tid = str(tank.tank_id)
                label = tid if len(tid) <= 6 else tid[:5] + "…"
                m = int(getattr(tank, "male_count", 0) or 0)
                f = int(getattr(tank, "female_count", 0) or 0)
                u = int(getattr(tank, "unknown_count", 0) or 0)
                tot = m + f + u
                count_part = "空" if tot == 0 else f"♂{m}/♀{f}/?{u}"
                lin = getattr(tank, "lineage", None) or "-"
                tip = f"ID:{tid} / 状態:{tank.health_status or '-'} / {count_part} / 系統:{lin}"
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
                sex           TEXT,
                lineage       TEXT,
                male_count    INTEGER DEFAULT 0,
                female_count  INTEGER DEFAULT 0,
                unknown_count INTEGER DEFAULT 0
            )
        """)
        # マイグレーション: 既存テーブルに male/female/unknown_count 列を追加
        ind_cols = [r[1] for r in c.execute("PRAGMA table_info(individuals)").fetchall()]
        for col_def in [("male_count", "INTEGER DEFAULT 0"),
                        ("female_count", "INTEGER DEFAULT 0"),
                        ("unknown_count", "INTEGER DEFAULT 0")]:
            if col_def[0] not in ind_cols:
                c.execute(f"ALTER TABLE individuals ADD COLUMN {col_def[0]} {col_def[1]}")
        # 既存レコードでカウントが全て0なら sex から推測して 1 をバックフィル
        c.execute("""
            UPDATE individuals
            SET male_count    = CASE WHEN sex IN ('オス','混合') THEN 1 ELSE 0 END,
                female_count  = CASE WHEN sex IN ('メス','混合') THEN 1 ELSE 0 END,
                unknown_count = 0
            WHERE COALESCE(male_count,0) + COALESCE(female_count,0) + COALESCE(unknown_count,0) = 0
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
                male_count            INTEGER DEFAULT 0,
                female_count          INTEGER DEFAULT 0,
                unknown_count         INTEGER DEFAULT 0,
                lineage               TEXT,
                set_date              TEXT,
                FOREIGN KEY (current_individual_id) REFERENCES individuals(individual_id)
            )
        """)
        # マイグレーション: 既存テーブルに不足列を追加
        tank_cols = [r[1] for r in c.execute("PRAGMA table_info(tanks)").fetchall()]
        for col_def in [
            ("rack", "TEXT"), ("tier", "INTEGER"), ("col_no", "INTEGER"),
            ("male_count", "INTEGER DEFAULT 0"),
            ("female_count", "INTEGER DEFAULT 0"),
            ("unknown_count", "INTEGER DEFAULT 0"),
            ("lineage", "TEXT"),
            ("set_date", "TEXT"),
        ]:
            if col_def[0] not in tank_cols:
                c.execute(f"ALTER TABLE tanks ADD COLUMN {col_def[0]} {col_def[1]}")

        # マイグレーション: 既存の current_individual_id から count/lineage を tanks にコピー
        # （1回だけ。tanks の count が全部 0 のときに発火）
        cur = c.execute(
            "SELECT COUNT(*) FROM tanks "
            "WHERE COALESCE(male_count,0)+COALESCE(female_count,0)+COALESCE(unknown_count,0) > 0"
        )
        if cur.fetchone()[0] == 0:
            c.execute("""
                UPDATE tanks SET
                    male_count = COALESCE((
                        SELECT i.male_count FROM individuals i
                        WHERE i.individual_id = tanks.current_individual_id), 0),
                    female_count = COALESCE((
                        SELECT i.female_count FROM individuals i
                        WHERE i.individual_id = tanks.current_individual_id), 0),
                    unknown_count = COALESCE((
                        SELECT i.unknown_count FROM individuals i
                        WHERE i.individual_id = tanks.current_individual_id), 0),
                    lineage = COALESCE((
                        SELECT i.lineage FROM individuals i
                        WHERE i.individual_id = tanks.current_individual_id), lineage),
                    set_date = COALESCE((
                        SELECT i.birth_date FROM individuals i
                        WHERE i.individual_id = tanks.current_individual_id), set_date)
                WHERE current_individual_id IS NOT NULL
            """)
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
                male_tag            TEXT,
                female_tag          TEXT,
                FOREIGN KEY (male_id)             REFERENCES individuals(individual_id),
                FOREIGN KEY (female_id)           REFERENCES individuals(individual_id),
                FOREIGN KEY (spawning_history_id) REFERENCES spawning_records(history_id)
            )
        """)
        # マイグレーション: 既存テーブルに male_tag/female_tag 追加
        trial_cols = [r[1] for r in c.execute("PRAGMA table_info(mating_trials)").fetchall()]
        for col_def in [("male_tag", "TEXT"), ("female_tag", "TEXT")]:
            if col_def[0] not in trial_cols:
                c.execute(f"ALTER TABLE mating_trials ADD COLUMN {col_def[0]} {col_def[1]}")

        # マイグレーション: mating_trials.male_id/female_id を NOT NULL → nullable に変更
        # （SQLiteは ALTER COLUMN 非対応のため、テーブル再作成）
        info = c.execute("PRAGMA table_info(mating_trials)").fetchall()
        male_is_notnull = any(col[1] == "male_id" and col[3] == 1 for col in info)
        if male_is_notnull:
            c.execute("""
                CREATE TABLE mating_trials_new (
                    trial_id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    planned_date        TEXT NOT NULL,
                    male_id             TEXT,
                    female_id           TEXT,
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
                    male_tag            TEXT,
                    female_tag          TEXT
                )
            """)
            c.execute("""
                INSERT INTO mating_trials_new
                SELECT trial_id, planned_date, male_id, female_id, source_tank_male,
                       source_tank_female, breeding_tank_id, status, setup_at,
                       divider_removed_at, egg_collected_at, returned_at,
                       spawning_history_id, notes, male_tag, female_tag
                FROM mating_trials
            """)
            c.execute("DROP TABLE mating_trials")
            c.execute("ALTER TABLE mating_trials_new RENAME TO mating_trials")

        # 設定テーブル（段の一覧などを保存）
        c.execute("""
            CREATE TABLE IF NOT EXISTS app_settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        # アクティビティログ
        c.execute("""
            CREATE TABLE IF NOT EXISTS activity_logs (
                log_id      INTEGER PRIMARY KEY AUTOINCREMENT,
                occurred_at TEXT NOT NULL,
                category    TEXT NOT NULL,
                actor       TEXT,
                target      TEXT,
                details     TEXT
            )
        """)
        conn.commit()

    # 群IDマイグレーション: 既存の9桁数字IDに 'A' プレフィックスを付ける
    # （A段の登録分という前提。新ID形式は <段文字>NNNYYMMDD = 10文字）
    migrate_conn = sqlite3.connect(DB_PATH)
    try:
        # FK enforcement off（参照列も一括書き換えるため）
        migrate_conn.execute("PRAGMA foreign_keys = OFF")
        cur = migrate_conn.execute(
            "SELECT COUNT(*) FROM individuals "
            "WHERE length(individual_id) = 9 "
            "AND individual_id GLOB '[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]'"
        )
        if cur.fetchone()[0] > 0:
            for tbl, col in [
                ("individuals",       "individual_id"),
                ("tanks",             "current_individual_id"),
                ("spawning_records",  "male_parent_id"),
                ("spawning_records",  "female_parent_id"),
                ("mating_trials",     "male_id"),
                ("mating_trials",     "female_id"),
            ]:
                migrate_conn.execute(
                    f"UPDATE {tbl} SET {col} = 'A' || {col} "
                    f"WHERE {col} IS NOT NULL "
                    f"AND length({col}) = 9 "
                    f"AND {col} GLOB '[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]'"
                )
            migrate_conn.commit()
    finally:
        migrate_conn.close()


init_db()


# === 設定（段の動的管理） =========================================
def get_setting(key, default=None):
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default


def set_setting(key, value):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO app_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        conn.commit()


def load_racks():
    raw = get_setting("racks", ",".join(DEFAULT_RACKS))
    items = [s.strip() for s in raw.split(",") if s.strip()]
    return items or DEFAULT_RACKS[:]


def load_tiers():
    raw = get_setting("tiers", ",".join(DEFAULT_TIERS))
    items = [s.strip() for s in raw.split(",") if s.strip()]
    return items or DEFAULT_TIERS[:]


def add_rack(name):
    name = name.strip()
    if not name or len(name) > 16:
        return False, "ラック名は1〜16文字で入力してください"
    current = load_racks()
    if name in current:
        return False, f"ラック {name} は既に存在します"
    current.append(name)
    set_setting("racks", ",".join(current))
    return True, f"ラック {name} を追加しました"


def remove_rack(name):
    current = load_racks()
    if name not in current:
        return False, "存在しないラックです"
    if len(current) <= 1:
        return False, "ラックは最低1つ必要です"
    current.remove(name)
    set_setting("racks", ",".join(current))
    return True, f"ラック {name} を削除しました"


def add_tier(letter):
    letter = letter.strip().upper()
    if not letter or not letter.isalpha() or len(letter) > 2:
        return False, "段は1〜2文字のアルファベットで入力してください"
    current = load_tiers()
    if letter in current:
        return False, f"段 {letter} は既に存在します"
    current.append(letter)
    set_setting("tiers", ",".join(current))
    return True, f"段 {letter} を追加しました"


def remove_tier(letter):
    current = load_tiers()
    if letter not in current:
        return False, "存在しない段です"
    if len(current) <= 1:
        return False, "段は最低1つ必要です"
    current.remove(letter)
    set_setting("tiers", ",".join(current))
    return True, f"段 {letter} を削除しました"


# === v3 マイグレーション（一度だけ実行） ===
# ラック A → wild、段 1 → A 等にリネーム
if get_setting("migration_v3_done") != "1":
    _migrate_conn = sqlite3.connect(DB_PATH)
    try:
        _migrate_conn.execute("PRAGMA foreign_keys = OFF")
        # 設定を新しいデフォルトに更新
        for k, v in [("racks", ",".join(DEFAULT_RACKS)),
                     ("tiers", ",".join(DEFAULT_TIERS))]:
            _migrate_conn.execute(
                "INSERT INTO app_settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (k, v),
            )
        # 既存の tank データを移行: rack='A' & tier∈{1..4} を持つ行
        _rack_map = {"A": "wild", "B": "genom1", "C": "genom2", "D": "genom3"}
        _tier_map = {1: "A", 2: "B", 3: "C", 4: "D"}
        _rows = _migrate_conn.execute(
            "SELECT tank_id, rack, tier, col_no FROM tanks "
            "WHERE rack IS NOT NULL AND tier IS NOT NULL"
        ).fetchall()
        for _old_id, _r, _t, _c in _rows:
            try:
                _t_int = int(_t)
            except (ValueError, TypeError):
                continue
            _new_rack = _rack_map.get(str(_r).upper())
            _new_tier = _tier_map.get(_t_int)
            if _new_rack is None or _new_tier is None:
                continue
            _new_id = (f"{_new_rack}-{_new_tier}-{int(_c):02d}"
                       if _c is not None else _old_id)
            _migrate_conn.execute(
                "UPDATE tanks SET tank_id=?, rack=?, tier=? WHERE tank_id=?",
                (_new_id, _new_rack, _new_tier, _old_id),
            )
        _migrate_conn.execute(
            "INSERT INTO app_settings (key, value) VALUES ('migration_v3_done','1') "
            "ON CONFLICT(key) DO UPDATE SET value='1'"
        )
        _migrate_conn.commit()
    finally:
        _migrate_conn.close()


RACKS = load_racks()
TIERS = load_tiers()
# 計 len(RACKS) * len(TIERS) * len(COLS) 水槽


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


def humanize_log(row) -> str:
    """ログ行を自然な日本語に整形"""
    cat = row.get("category", "") if hasattr(row, "get") else row["category"]
    actor = row["actor"] if pd.notna(row.get("actor") if hasattr(row, "get") else row["actor"]) else None
    target = row["target"] if pd.notna(row.get("target") if hasattr(row, "get") else row["target"]) else None
    details = row["details"] if pd.notna(row.get("details") if hasattr(row, "get") else row["details"]) else None
    actor_prefix = f"{actor} さんが " if actor else ""
    tgt_str = str(target) if target else ""
    det_str = str(details) if details else ""

    if cat == "餌やり":
        msg = f"{actor_prefix}餌やりしました"
        if det_str and det_str != "全水槽給餌":
            msg += f"（{det_str}）"
    elif cat == "水槽登録/更新":
        msg = f"{actor_prefix}水槽 {tgt_str} を登録/更新しました"
        if det_str: msg += f"（{det_str}）"
    elif cat == "水槽削除":
        msg = f"{actor_prefix}水槽 {tgt_str} を削除しました"
    elif cat == "群登録/更新":
        msg = f"{actor_prefix}群 {tgt_str} を登録/更新しました"
        if det_str: msg += f"（{det_str}）"
    elif cat == "群更新":
        msg = f"{actor_prefix}群 {tgt_str} を編集しました"
        if det_str: msg += f"（{det_str}）"
    elif cat == "群削除":
        msg = f"{actor_prefix}群 {tgt_str} を削除しました"
    elif cat == "トライアル計画":
        msg = f"{actor_prefix}トライアル {tgt_str} を計画しました"
        if det_str: msg += f" — {det_str}"
    elif cat == "トライアル前日セット":
        msg = f"{actor_prefix}トライアル {tgt_str} を前日セット完了にしました"
    elif cat == "仕切り取り出し":
        msg = f"{actor_prefix}トライアル {tgt_str} の仕切りを取り出しました"
    elif cat == "採卵":
        msg = f"{actor_prefix}トライアル {tgt_str} で採卵しました"
        if det_str: msg += f"（{det_str}）"
    elif cat == "トライアル戻し完了":
        msg = f"{actor_prefix}トライアル {tgt_str} の戻しを完了しました"
    elif cat == "トライアル中止":
        msg = f"{actor_prefix}トライアル {tgt_str} を中止しました"
    elif cat == "産卵成績(手入力)":
        msg = f"{actor_prefix}産卵成績を手入力しました"
        if det_str: msg += f" — {det_str}"
    elif cat == "ログ削除":
        msg = f"{actor_prefix}古いログを削除しました"
        if det_str: msg += f"（{det_str}）"
    else:
        msg = f"{actor_prefix}{cat}"
        if tgt_str: msg += f" {tgt_str}"
        if det_str: msg += f" — {det_str}"
    return msg


def render_log_lines_html(df, limit=None):
    """ログ DataFrame を HTML 文字列（テキスト行リスト）に整形"""
    if df.empty:
        return ""
    if limit is not None:
        df = df.head(limit)
    parts = []
    for _, row in df.iterrows():
        ts = str(row["occurred_at"])
        msg = humanize_log(row)
        parts.append(
            f'<div style="font-size:13px;padding:8px 14px;'
            f'border-bottom:1px solid rgba(120,120,120,0.10);'
            f'display:flex;gap:14px;align-items:baseline">'
            f'<span style="color:#6E6E73;font-family:ui-monospace,Menlo,monospace;'
            f'font-size:12px;white-space:nowrap;flex-shrink:0">{ts}</span>'
            f'<span style="color:#1D1D1F">{msg}</span>'
            f'</div>'
        )
    return ('<div style="background:rgba(255,255,255,0.55);'
            'backdrop-filter:blur(20px);border-radius:14px;'
            'border:1px solid rgba(255,255,255,0.6);overflow:hidden">'
            + "".join(parts) + '</div>')


def jump_to_tab(idx: int, label_jp: str = ""):
    """JS でタブをクリックして遷移。session_state.jump_target を経由"""
    import streamlit.components.v1 as components
    components.html(
        f"""
        <script>
        setTimeout(function() {{
            var tabs = window.parent.document.querySelectorAll('button[role="tab"]');
            if (tabs.length > {idx}) {{ tabs[{idx}].click(); }}
        }}, 30);
        </script>
        """,
        height=0,
    )


def log_action(category: str, target=None, details=None):
    """アクション履歴を1件追加。担当者は session_state.actor_name から取得。"""
    actor = None
    try:
        actor = (st.session_state.get("actor_name") or "").strip() or None
    except Exception:
        actor = None
    try:
        execute(
            "INSERT INTO activity_logs (occurred_at, category, actor, target, details) "
            "VALUES (?, ?, ?, ?, ?)",
            (now_iso(), category, actor,
             str(target) if target is not None else None,
             str(details) if details is not None else None),
        )
    except Exception:
        # ログ失敗は本処理を止めない
        pass


def to_csv_bytes(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode("utf-8-sig")


def csv_filename(prefix: str) -> str:
    return f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"


def now_iso() -> str:
    return now_naive_jst().strftime("%Y-%m-%d %H:%M:%S")


def hours_since(iso_str: Optional[str]) -> Optional[float]:
    if not iso_str:
        return None
    try:
        t = datetime.strptime(iso_str, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return (now_naive_jst() - t).total_seconds() / 3600


# ============================================================
# 画面設定
# ============================================================
st.set_page_config(page_title="ゼブラフィッシュ水槽管理", page_icon="🐟", layout="wide")


# --- サイドバー：表示モード切替 ---
if "mobile_mode" not in st.session_state:
    st.session_state.mobile_mode = False

with st.sidebar:
    st.markdown(
        '<div style="padding:8px 4px 16px 4px">'
        '<div style="font-size:10px;letter-spacing:0.28em;color:#AEAEB2;'
        'text-transform:uppercase;margin-bottom:6px;font-weight:600">ZEBRAFISH LAB</div>'
        '<div style="font-size:18px;font-weight:600;color:#1D1D1F;'
        'letter-spacing:-0.015em">水槽管理システム</div>'
        '</div>',
        unsafe_allow_html=True,
    )
    st.markdown("---")
    # 担当者（ログ用、任意）
    if "actor_name" not in st.session_state:
        st.session_state["actor_name"] = ""
    st.markdown("##### 👤 担当者")
    st.text_input(
        "名前（任意）", key="actor_name", label_visibility="collapsed",
        placeholder="例: 草谷",
        help="入力するとログに残ります。空でもOK。",
    )

    st.markdown("---")
    st.markdown("##### ⚙️ 表示設定")
    new_mode = st.checkbox("📱 モバイル表示", value=st.session_state.mobile_mode,
                            help="スマホ/タブレット向けにレイアウトを調整します")
    if new_mode != st.session_state.mobile_mode:
        st.session_state.mobile_mode = new_mode
        st.rerun()

    st.markdown("---")
    with st.expander("[rack] 🗄️ ラックの管理", expanded=False):
        st.caption("現在： " + ", ".join(RACKS))
        new_rack = st.text_input("追加するラック名", max_chars=16, key="add_rack_in")
        rc1, rc2 = st.columns(2)
        if rc1.button("➕ 追加", key="add_rack_btn", use_container_width=True):
            if new_rack:
                ok, msg = add_rack(new_rack)
                (st.success if ok else st.error)(msg)
                if ok:
                    st.rerun()
        if len(RACKS) > 1:
            rm_rack = rc2.selectbox("削除", RACKS, key="rm_rack_sel",
                                     label_visibility="collapsed")
            if rc2.button("🗑️ 削除", key="rm_rack_btn", use_container_width=True):
                ok, msg = remove_rack(rm_rack)
                (st.success if ok else st.error)(msg)
                if ok:
                    st.rerun()

    with st.expander("[tier] 📐 段の管理", expanded=False):
        st.caption("現在： " + ", ".join(TIERS))
        new_tier = st.text_input("追加する段（1〜2文字）", max_chars=2, key="add_tier_in").upper()
        tc1, tc2 = st.columns(2)
        if tc1.button("➕ 追加", key="add_tier_btn", use_container_width=True):
            if new_tier:
                ok, msg = add_tier(new_tier)
                (st.success if ok else st.error)(msg)
                if ok:
                    st.rerun()
        if len(TIERS) > 1:
            rm_tier = tc2.selectbox("削除", TIERS, key="rm_tier_sel",
                                     label_visibility="collapsed")
            if tc2.button("🗑️ 削除", key="rm_tier_btn", use_container_width=True):
                ok, msg = remove_tier(rm_tier)
                (st.success if ok else st.error)(msg)
                if ok:
                    st.rerun()

    st.markdown("---")
    st.caption("🌱 v2.3")
    st.caption(today_jst().strftime("%Y年 %m月 %d日"))


# --- デザインシステム CSS（Liquid Glass / Apple Minimal） ---
# 文字化け対策: ネイティブ日本語フォントを最優先に置く（Web font 失敗時も必ず日本語が出る）
JP_FONT_STACK = ("-apple-system, BlinkMacSystemFont, 'SF Pro Text', 'Inter', "
                 "'Hiragino Sans', 'Hiragino Kaku Gothic ProN', "
                 "'Yu Gothic UI', YuGothic, 'Meiryo', "
                 "'Noto Sans JP', system-ui, sans-serif")
JP_HEAD_STACK = ("-apple-system, BlinkMacSystemFont, 'SF Pro Display', 'Inter', "
                 "'Hiragino Sans', 'Hiragino Kaku Gothic ProN', "
                 "'Yu Gothic UI', YuGothic, 'Meiryo', "
                 "'Noto Sans JP', system-ui, sans-serif")

BASE_CSS = f"""
<style>
:root {{
  --text: #1D1D1F;
  --text-soft: #6E6E73;
  --text-mute: #AEAEB2;
  --primary: #1D1D1F;
  --primary-mid: #2C2C2E;
  --primary-soft: #48484A;
  --accent: #BE8763;            /* 控えめなブロンズ */
  --accent-soft: #E8D5C0;
  --danger: #C44545;
  --success: #4D8D6B;
  --glass-bg: rgba(255, 255, 255, 0.62);
  --glass-bg-strong: rgba(255, 255, 255, 0.82);
  --glass-bg-soft: rgba(255, 255, 255, 0.38);
  --glass-border: rgba(255, 255, 255, 0.70);
  --glass-shadow: 0 4px 24px rgba(60, 60, 67, 0.06);
  --glass-shadow-lg: 0 14px 44px rgba(60, 60, 67, 0.10);
  --glass-highlight: inset 0 1px 0 rgba(255, 255, 255, 0.92);
}}

/* === グローバル：継承ベースで日本語フォントを浸透させる ===
   要点：
   - body と stApp に font-family を設定 → 全要素が継承する
   - span や i は触らない（Streamlit が material-symbols 用に独自 font-family を当てている）
   - 「テキストを必ず持つ」要素だけ明示上書き */
html, body, .stApp {{
  font-family: {JP_FONT_STACK} !important;
  color: var(--text);
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
}}

.stApp p, .stApp li, .stApp td, .stApp th,
.stApp input, .stApp textarea, .stApp select, .stApp label,
.stApp button, .stApp summary,
.stApp h1, .stApp h2, .stApp h3, .stApp h4, .stApp h5, .stApp h6,
.stApp [data-testid="stMarkdownContainer"],
.stApp [data-testid="stMarkdownContainer"] p,
.stApp [data-testid="stMarkdownContainer"] li,
.stApp [data-testid="stMarkdownContainer"] div,
.stApp [data-testid="stCaptionContainer"],
.stApp [data-testid="stMetricLabel"],
.stApp [data-testid="stMetricValue"],
.stApp [data-testid="stMetricDelta"],
.stApp [data-baseweb="tab"],
.stApp [data-baseweb="tab-list"],
[data-testid="stSidebar"],
[data-testid="stSidebar"] p,
[data-testid="stSidebar"] li,
[data-testid="stSidebar"] label,
[data-testid="stSidebar"] button {{
  font-family: {JP_FONT_STACK} !important;
}}

/* === Material Symbols / Icons は絶対に守る ===
   font-family と font-feature-settings を強制復元。
   stApp プレフィックス付きで specificity を高めて、私の他の selector に勝つ */
.stApp .material-icons,
.stApp .material-icons-outlined,
.stApp .material-icons-round,
.stApp .material-symbols-outlined,
.stApp .material-symbols-rounded,
.stApp .material-symbols-sharp,
.stApp [class*="material-symbols"],
.stApp [class*="material-icons"],
.stApp i.material-icons,
.stApp i[class*="material"],
.stApp span.material-icons,
.stApp span[class*="material-symbols"],
.stApp [data-testid="stIconMaterial"],
.stApp [data-testid="stIcon"],
.stApp [data-testid="stExpanderIcon"],
.stApp [data-testid*="Material"],
.stApp [data-testid*="Icon"] {{
  font-family: 'Material Symbols Outlined', 'Material Symbols Rounded',
               'Material Icons', 'Material Icons Outlined' !important;
  font-feature-settings: 'liga' !important;
  -webkit-font-feature-settings: 'liga' !important;
  font-weight: normal !important;
  font-style: normal !important;
  text-transform: none !important;
  letter-spacing: normal !important;
  word-wrap: normal !important;
  white-space: nowrap !important;
  direction: ltr !important;
}}

h1, h2, h3, h4, h5,
[data-testid="stMarkdownContainer"] h1,
[data-testid="stMarkdownContainer"] h2,
[data-testid="stMarkdownContainer"] h3,
[data-testid="stMarkdownContainer"] h4 {{
  font-family: {JP_HEAD_STACK} !important;
  color: var(--text) !important;
  letter-spacing: -0.015em !important;
  font-weight: 700 !important;
}}
h2 {{ font-size: 22px !important; margin-top: 0.5rem !important; }}
h3 {{ font-size: 17px !important; }}
h4 {{ font-size: 14px !important; }}

/* === 背景：極めて控えめなパステルブロブ（クールでミニマル） === */
.stApp {{
  background:
    radial-gradient(circle at  8% 12%, rgba(195, 215, 240, 0.32), transparent 36%),
    radial-gradient(circle at 92% 18%, rgba(220, 210, 240, 0.28), transparent 38%),
    radial-gradient(circle at 18% 90%, rgba(195, 220, 205, 0.26), transparent 38%),
    radial-gradient(circle at 88% 86%, rgba(255, 220, 200, 0.20), transparent 38%),
    linear-gradient(180deg, #F5F5F7 0%, #ECECEF 100%);
  background-attachment: fixed;
}}

.main .block-container {{
  padding-top: 1.5rem !important;
  padding-bottom: 4rem !important;
  max-width: 1400px;
}}

/* === タブ：浮遊ガラスピルバー === */
.stTabs [data-baseweb="tab-list"] {{
  gap: 4px;
  background: var(--glass-bg);
  -webkit-backdrop-filter: blur(28px) saturate(180%);
  backdrop-filter: blur(28px) saturate(180%);
  padding: 6px;
  border-radius: 20px;
  border: 1px solid var(--glass-border);
  box-shadow: var(--glass-shadow), var(--glass-highlight);
  flex-wrap: wrap;
}}

.stTabs [data-baseweb="tab"] {{
  background: transparent;
  border-radius: 14px !important;
  padding: 8px 16px !important;
  color: var(--text-soft) !important;
  font-weight: 500 !important;
  font-size: 13px !important;
  font-family: {JP_FONT_STACK} !important;
  transition: all 0.25s cubic-bezier(0.4, 0, 0.2, 1);
  border: none !important;
}}
.stTabs [data-baseweb="tab"] * {{ font-family: {JP_FONT_STACK} !important; }}
.stTabs [data-baseweb="tab"]:hover {{
  background: rgba(255, 255, 255, 0.55);
  color: var(--text) !important;
}}
.stTabs [aria-selected="true"] {{
  background: linear-gradient(135deg, #1D1D1F, #2C2C2E) !important;
  color: white !important;
  box-shadow: 0 4px 12px rgba(29, 29, 31, 0.22), inset 0 1px 0 rgba(255, 255, 255, 0.16);
}}
.stTabs [aria-selected="true"] * {{ color: white !important; }}
.stTabs [data-baseweb="tab-highlight"], .stTabs [data-baseweb="tab-border"] {{ display: none !important; }}

/* === ボタン：ガラスピル === */
.stButton > button, .stDownloadButton > button {{
  background: var(--glass-bg-strong) !important;
  -webkit-backdrop-filter: blur(18px) saturate(180%);
  backdrop-filter: blur(18px) saturate(180%);
  border: 1px solid var(--glass-border) !important;
  border-radius: 999px !important;
  padding: 9px 20px !important;
  font-weight: 500 !important;
  font-size: 13px !important;
  font-family: {JP_FONT_STACK} !important;
  color: var(--text) !important;
  box-shadow: var(--glass-shadow), var(--glass-highlight);
  transition: all 0.22s cubic-bezier(0.4, 0, 0.2, 1);
}}
.stButton > button:hover, .stDownloadButton > button:hover {{
  transform: translateY(-1px);
  background: rgba(255, 255, 255, 0.95) !important;
  color: var(--text) !important;
  box-shadow: var(--glass-shadow-lg), var(--glass-highlight);
}}
.stButton > button[kind="primary"] {{
  background: linear-gradient(135deg, #1D1D1F, #2C2C2E) !important;
  color: white !important;
  border: 1px solid rgba(29, 29, 31, 0.5) !important;
  box-shadow: 0 4px 16px rgba(29, 29, 31, 0.22), inset 0 1px 0 rgba(255, 255, 255, 0.18);
}}
.stButton > button[kind="primary"]:hover {{
  transform: translateY(-1px);
  box-shadow: 0 10px 28px rgba(29, 29, 31, 0.28), inset 0 1px 0 rgba(255, 255, 255, 0.22);
  color: white !important;
}}

/* === メトリクスカード：ガラス === */
[data-testid="stMetric"] {{
  background: var(--glass-bg);
  -webkit-backdrop-filter: blur(28px) saturate(180%);
  backdrop-filter: blur(28px) saturate(180%);
  padding: 18px 22px !important;
  border-radius: 20px !important;
  border: 1px solid var(--glass-border);
  box-shadow: var(--glass-shadow), var(--glass-highlight);
  transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
}}
[data-testid="stMetric"]:hover {{
  transform: translateY(-2px);
  background: var(--glass-bg-strong);
  box-shadow: var(--glass-shadow-lg), var(--glass-highlight);
}}
[data-testid="stMetricLabel"], [data-testid="stMetricLabel"] * {{
  color: var(--text-mute) !important;
  font-size: 10px !important;
  letter-spacing: 0.16em !important;
  text-transform: uppercase !important;
  font-weight: 600 !important;
  font-family: {JP_FONT_STACK} !important;
}}
[data-testid="stMetricValue"], [data-testid="stMetricValue"] * {{
  color: var(--text) !important;
  font-weight: 700 !important;
  font-family: {JP_HEAD_STACK} !important;
  font-size: 28px !important;
  line-height: 1.1 !important;
  letter-spacing: -0.025em !important;
}}
[data-testid="stMetricDelta"] {{ color: var(--text-soft) !important; }}

/* === 入力フィールド：ガラス === */
.stTextInput input, .stTextArea textarea, .stNumberInput input, .stDateInput input {{
  background: rgba(255, 255, 255, 0.62) !important;
  -webkit-backdrop-filter: blur(10px);
  backdrop-filter: blur(10px);
  border: 1px solid var(--glass-border) !important;
  border-radius: 14px !important;
  padding: 10px 14px !important;
  color: var(--text) !important;
  font-family: {JP_FONT_STACK} !important;
  transition: all 0.2s ease;
}}
.stTextInput input:focus, .stTextArea textarea:focus, .stNumberInput input:focus, .stDateInput input:focus {{
  border-color: rgba(29, 29, 31, 0.45) !important;
  background: rgba(255, 255, 255, 0.95) !important;
  box-shadow: 0 0 0 4px rgba(29, 29, 31, 0.08) !important;
  outline: none !important;
}}
.stSelectbox > div > div, .stMultiSelect > div > div {{
  background: rgba(255, 255, 255, 0.62) !important;
  border: 1px solid var(--glass-border) !important;
  border-radius: 14px !important;
}}

/* === ラベル === */
.stTextInput label, .stTextArea label, .stNumberInput label, .stDateInput label,
.stSelectbox label, .stMultiSelect label, .stRadio label, .stCheckbox label,
.stFileUploader label {{
  color: var(--text-soft) !important;
  font-weight: 500 !important;
  font-size: 13px !important;
  font-family: {JP_FONT_STACK} !important;
}}

/* === データフレーム：ガラス枠 === */
[data-testid="stDataFrame"] {{
  background: var(--glass-bg);
  -webkit-backdrop-filter: blur(20px) saturate(180%);
  backdrop-filter: blur(20px) saturate(180%);
  border-radius: 18px;
  overflow: hidden;
  border: 1px solid var(--glass-border);
  box-shadow: var(--glass-shadow);
}}

/* === アラート === */
.stAlert, div[data-baseweb="notification"] {{
  background: var(--glass-bg-strong) !important;
  -webkit-backdrop-filter: blur(20px) saturate(180%);
  backdrop-filter: blur(20px) saturate(180%);
  border-radius: 16px !important;
  border: 1px solid var(--glass-border) !important;
  padding: 14px 18px !important;
  box-shadow: var(--glass-shadow);
}}

/* === 区切り線 === */
hr {{
  border: none !important;
  height: 1px !important;
  background: linear-gradient(90deg, transparent, rgba(140, 120, 100, 0.20), transparent) !important;
  margin: 32px 0 !important;
}}

/* === expander：ガラス === */
[data-testid="stExpander"] {{
  background: var(--glass-bg);
  -webkit-backdrop-filter: blur(24px) saturate(180%);
  backdrop-filter: blur(24px) saturate(180%);
  border: 1px solid var(--glass-border) !important;
  border-radius: 18px !important;
  box-shadow: var(--glass-shadow);
  overflow: hidden;
}}
[data-testid="stExpander"] summary,
[data-testid="stExpander"] details > summary,
[data-testid="stExpander"] .streamlit-expanderHeader {{
  padding: 14px 18px !important;
  font-weight: 500 !important;
  font-family: {JP_FONT_STACK} !important;
  color: var(--text) !important;
  font-size: 14px !important;
}}

/* Material Symbols（チェブロンなど）のフォントを強制保持。これでリガチャが
   正しく描画され、'keyboard_arrow_right' などのテキストが出なくなる。 */
.stApp .material-symbols-outlined,
.stApp .material-symbols-rounded,
.stApp .material-symbols-sharp,
.stApp .material-icons,
.stApp .material-icons-outlined,
.stApp [class*="material-symbols"],
.stApp [class*="material-icons"] {{
  font-family: 'Material Symbols Outlined', 'Material Symbols Rounded',
               'Material Symbols Sharp', 'Material Icons',
               'Material Icons Outlined' !important;
  font-feature-settings: 'liga' !important;
  -webkit-font-feature-settings: 'liga' !important;
  font-weight: normal !important;
  font-style: normal !important;
  text-transform: none !important;
  letter-spacing: normal !important;
  word-wrap: normal !important;
  white-space: nowrap !important;
  direction: ltr !important;
}}

/* === プログレス === */
.stProgress > div > div > div {{
  background: linear-gradient(90deg, #6E6E73, #1D1D1F) !important;
  border-radius: 10px !important;
  box-shadow: 0 2px 6px rgba(29, 29, 31, 0.20);
}}
.stProgress > div > div {{
  background-color: rgba(255, 255, 255, 0.50) !important;
  -webkit-backdrop-filter: blur(10px);
  backdrop-filter: blur(10px);
  border-radius: 10px !important;
  border: 1px solid rgba(255, 255, 255, 0.55);
}}

/* === サイドバー：ガラスパネル === */
[data-testid="stSidebar"] {{
  background: var(--glass-bg) !important;
  -webkit-backdrop-filter: blur(30px) saturate(180%);
  backdrop-filter: blur(30px) saturate(180%);
  border-right: 1px solid var(--glass-border);
}}
[data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p {{
  color: var(--text-soft);
}}

/* === ヒーロー：シグネチャー・ガラスシャード（チャコール） === */
.zf-hero {{
  position: relative;
  background:
    radial-gradient(circle at 92% 18%, rgba(190, 135, 99, 0.30), transparent 42%),
    radial-gradient(circle at 10% 82%, rgba(120, 140, 175, 0.22), transparent 42%),
    linear-gradient(135deg, #1D1D1F 0%, #2C2C2E 100%);
  border: 1px solid rgba(255, 255, 255, 0.10);
  border-radius: 24px;
  padding: 30px 36px;
  color: white;
  box-shadow:
    0 16px 48px rgba(29, 29, 31, 0.22),
    inset 0 1px 0 rgba(255, 255, 255, 0.10),
    inset 0 0 0 1px rgba(255, 255, 255, 0.04);
  margin: 0 0 28px 0;
  overflow: hidden;
}}
.zf-hero::before {{
  content: '';
  position: absolute;
  top: 0; right: 0;
  width: 380px; height: 380px;
  background:
    radial-gradient(circle at 30% 30%, rgba(190, 135, 99, 0.28), transparent 50%),
    radial-gradient(circle at 70% 70%, rgba(140, 165, 200, 0.18), transparent 55%);
  filter: blur(40px);
  pointer-events: none;
}}
.zf-hero::after {{
  content: '';
  position: absolute;
  top: -50%; left: -20%;
  width: 60%; height: 200%;
  background: linear-gradient(105deg, transparent 38%, rgba(255, 255, 255, 0.05) 50%, transparent 62%);
  transform: rotate(15deg);
  pointer-events: none;
}}
.zf-hero .eyebrow {{
  font-size: 10px;
  letter-spacing: 0.32em;
  text-transform: uppercase;
  color: rgba(255, 255, 255, 0.55);
  font-weight: 600;
  margin: 0 0 10px 0;
  position: relative; z-index: 2;
  font-family: {JP_FONT_STACK} !important;
}}
.zf-hero h1 {{
  color: white !important;
  margin: 0 !important;
  font-size: 30px !important;
  font-weight: 600 !important;
  letter-spacing: -0.025em !important;
  font-family: {JP_HEAD_STACK} !important;
  position: relative; z-index: 2;
}}
.zf-hero .sub {{
  color: rgba(255, 255, 255, 0.62);
  margin: 6px 0 0 0;
  font-size: 13px;
  font-weight: 400;
  position: relative; z-index: 2;
  font-family: {JP_FONT_STACK} !important;
}}

/* === セクションラベル === */
.zf-section-label {{
  font-size: 10px;
  letter-spacing: 0.22em;
  text-transform: uppercase;
  color: var(--text-mute);
  font-weight: 700;
  margin: 8px 0 14px 0;
}}

/* === 餌やりステータス === */
.feed-ok    {{ color: #4D8D6B; font-weight: 600; }}
.feed-warn  {{ color: #BE8763; font-weight: 600; }}
.feed-alert {{ color: #C44545; font-weight: 700; }}

/* === マルチセレクト・ピル === */
[data-baseweb="tag"] {{
  background: rgba(29, 29, 31, 0.06) !important;
  -webkit-backdrop-filter: blur(10px);
  backdrop-filter: blur(10px);
  border: 1px solid rgba(29, 29, 31, 0.12) !important;
  border-radius: 999px !important;
  color: var(--text) !important;
  font-weight: 500;
}}

/* === ファイルアップロード === */
[data-testid="stFileUploadDropzone"] {{
  background: var(--glass-bg) !important;
  -webkit-backdrop-filter: blur(20px);
  backdrop-filter: blur(20px);
  border: 2px dashed rgba(140, 120, 100, 0.40) !important;
  border-radius: 18px !important;
  transition: all 0.2s;
}}
[data-testid="stFileUploadDropzone"]:hover {{
  background: var(--glass-bg-strong) !important;
  border-color: rgba(29, 29, 31, 0.40) !important;
}}

/* === スクロールバー === */
::-webkit-scrollbar {{ width: 10px; height: 10px; }}
::-webkit-scrollbar-track {{ background: transparent; }}
::-webkit-scrollbar-thumb {{
  background: rgba(140, 120, 100, 0.30);
  border-radius: 999px;
  border: 2px solid transparent;
  background-clip: content-box;
}}
::-webkit-scrollbar-thumb:hover {{ background: rgba(140, 120, 100, 0.50); background-clip: content-box; }}

/* === 入場アニメ === */
@keyframes glassIn {{
  from {{ opacity: 0; transform: translateY(8px); }}
  to   {{ opacity: 1; transform: translateY(0); }}
}}
.zf-hero, [data-testid="stMetric"] {{
  animation: glassIn 0.55s cubic-bezier(0.16, 1, 0.3, 1);
}}

/* === Safari/Firefox の backdrop-filter フォールバック === */
@supports not ((backdrop-filter: blur(10px)) or (-webkit-backdrop-filter: blur(10px))) {{
  .stTabs [data-baseweb="tab-list"],
  .stButton > button,
  [data-testid="stMetric"],
  [data-testid="stExpander"],
  [data-testid="stDataFrame"],
  [data-testid="stSidebar"],
  .stAlert {{
    background: rgba(255, 255, 255, 0.92) !important;
  }}
}}
</style>
"""

MOBILE_CSS = f"""
<style>
/* モバイル時のフォント再宣言（テキスト要素のみ・アイコンは除外） */
html, body, .stApp {{
  font-family: {JP_FONT_STACK} !important;
}}
.stApp p, .stApp li, .stApp button, .stApp summary,
.stApp input, .stApp textarea, .stApp select, .stApp label,
.stApp [data-testid="stMarkdownContainer"],
.stApp [data-testid="stMarkdownContainer"] *,
.stApp [data-baseweb="tab"],
.stApp span:not([class*="material"]):not([class*="icon"]):not([class*="Icon"]) {{
  font-family: {JP_FONT_STACK} !important;
}}
h1, h2, h3, h4, h5 {{
  font-family: {JP_HEAD_STACK} !important;
}}

.main .block-container {{
  padding: 0.75rem 1rem 3rem 1rem !important;
  max-width: 100% !important;
}}

.zf-hero {{ padding: 22px 24px; border-radius: 22px; margin-bottom: 22px; }}
.zf-hero h1 {{ font-size: 24px !important; }}
.zf-hero .sub {{ font-size: 12px; }}
.zf-hero::before {{ font-size: 120px; right: -15px; bottom: -35px; }}

.stTabs [data-baseweb="tab-list"] {{ padding: 6px; border-radius: 18px; }}
.stTabs [data-baseweb="tab"] {{
  padding: 8px 13px !important;
  font-size: 12px !important;
}}

.stButton > button, .stDownloadButton > button {{
  padding: 14px 20px !important;
  font-size: 15px !important;
  min-height: 48px;
  width: 100%;
  border-radius: 999px !important;
}}

[data-testid="stMetric"] {{
  padding: 14px 16px !important;
  border-radius: 18px !important;
}}
[data-testid="stMetricValue"] {{ font-size: 24px !important; }}
[data-testid="stMetricLabel"] {{ font-size: 10px !important; }}

h2 {{ font-size: 19px !important; }}
h3 {{ font-size: 16px !important; }}
h4 {{ font-size: 14px !important; }}
</style>
"""

st.markdown(BASE_CSS, unsafe_allow_html=True)
if st.session_state.mobile_mode:
    st.markdown(MOBILE_CSS, unsafe_allow_html=True)


# --- ヒーロー（JST基準） ---
_today_jst = today_jst()
weekday_jp = ["月", "火", "水", "木", "金", "土", "日"][_today_jst.weekday()]
_hour = now_jst().hour
if 5 <= _hour < 11:
    _greeting = "おはようございます"
elif 11 <= _hour < 18:
    _greeting = "こんにちは"
elif 18 <= _hour < 23:
    _greeting = "こんばんは"
else:
    _greeting = "おつかれさまです"

hero_html = f"""
<div class="zf-hero">
  <div class="eyebrow">ZEBRAFISH MANAGEMENT</div>
  <h1>{_greeting} 🐟</h1>
  <p class="sub">{_today_jst.strftime("%Y年 %m月 %d日")}（{weekday_jp}）・餌やり、交配、成績まで1画面で</p>
</div>
"""
st.markdown(hero_html, unsafe_allow_html=True)

(tab_dash, tab_feed, tab_trial, tab_analysis, tab_rack,
 tab_tank, tab_spawn, tab_log) = st.tabs(
    [
        "📊 ダッシュボード",
        "🍚 餌やり",
        "💕 交配トライアル",
        "📈 成績分析",
        "📐 棚ビュー",
        "🪣 水槽管理",
        "🥚 産卵成績",
        "📔 ログ",
    ]
)


# ============================================================
# 📊 ダッシュボード
# ============================================================
# タブインデックス（タブ並び順と一致させる）
TAB_DASH, TAB_FEED, TAB_TRIAL, TAB_ANALYSIS, TAB_RACK, TAB_TANK, TAB_SPAWN, TAB_LOG = range(8)

with tab_dash:
    # === タブジャンプ要求が積まれていたら、JSで該当タブをクリックして遷移 ===
    if st.session_state.get("jump_target") is not None:
        _idx = st.session_state.pop("jump_target")
        jump_to_tab(int(_idx))

    df_tanks = fetch_df("SELECT * FROM tanks")
    df_spawn = fetch_df("SELECT * FROM spawning_records")
    df_trials = fetch_df("SELECT * FROM mating_trials")
    # 総匹数（全水槽合算）
    total_fish = 0
    if not df_tanks.empty:
        total_fish = int(
            df_tanks[["male_count", "female_count", "unknown_count"]].fillna(0).sum().sum()
        )

    today = today_jst().isoformat()
    df_feed_today = fetch_df(
        "SELECT * FROM feeding_logs WHERE substr(fed_at,1,10)=?", (today,)
    )

    # === オーバービュー ===
    st.markdown('<div class="zf-section-label">📊 オーバービュー</div>', unsafe_allow_html=True)
    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("🪣 水槽数", len(df_tanks))
    col2.metric("🐠 総匹数", total_fish)
    col3.metric("🥚 産卵記録", len(df_spawn))
    alert_count = int((df_tanks["health_status"] == "要観察").sum()) if not df_tanks.empty else 0
    col4.metric("⚠️ 要観察", alert_count)
    in_progress = int(df_trials["status"].isin(["計画中", "前日セット済み", "採卵済み"]).sum()) if not df_trials.empty else 0
    col5.metric("💕 進行中トライアル", in_progress)

    st.markdown("<br/>", unsafe_allow_html=True)

    # === 最近のアクティビティ（上部に配置）===
    st.markdown('<div class="zf-section-label">📔 最近のアクティビティ</div>', unsafe_allow_html=True)
    recent_logs = fetch_df(
        "SELECT occurred_at, category, actor, target, details "
        "FROM activity_logs ORDER BY log_id DESC LIMIT 5"
    )
    if recent_logs.empty:
        st.caption("ログがまだありません")
    else:
        st.markdown(render_log_lines_html(recent_logs), unsafe_allow_html=True)
    if st.button("📔 ログタブで全件・絞り込み", key="nav_log", use_container_width=False):
        st.session_state["jump_target"] = TAB_LOG
        st.rerun()

    st.markdown("<br/>", unsafe_allow_html=True)

    # === 今日の状況 ===
    st.markdown('<div class="zf-section-label">🍃 今日の状況</div>', unsafe_allow_html=True)
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

        if st.button("🍚 餌やりタブへ", key="nav_feed", type="primary",
                     use_container_width=True):
            st.session_state["jump_target"] = TAB_FEED
            st.rerun()

    with right:
        st.subheader("⚠️ 注意が必要な水槽")
        has_alerts = False
        if df_tanks.empty:
            st.info("水槽が登録されていません")
        else:
            alert_df = df_tanks[df_tanks["health_status"] == "要観察"]
            isolated_df = df_tanks[df_tanks["health_status"] == "隔離中"]
            if alert_df.empty and isolated_df.empty:
                st.success("すべての水槽が良好です 🎉")
            def _alert_view(d):
                def _fmt(r):
                    m = int(r.get("male_count") or 0)
                    f = int(r.get("female_count") or 0)
                    u = int(r.get("unknown_count") or 0)
                    if m + f + u == 0:
                        return "空"
                    return f"♂{m} / ♀{f} / ？{u}"
                d = d.copy()
                d["匹数"] = d.apply(_fmt, axis=1)
                return d[["tank_id", "匹数", "lineage", "memo"]].rename(
                    columns={"tank_id": "水槽ID", "lineage": "系統", "memo": "メモ"})

            if not alert_df.empty:
                has_alerts = True
                st.warning("【要観察】")
                st.dataframe(_alert_view(alert_df), use_container_width=True, hide_index=True)
            if not isolated_df.empty:
                has_alerts = True
                st.error("【隔離中】")
                st.dataframe(_alert_view(isolated_df), use_container_width=True, hide_index=True)
        if has_alerts and st.button("🪣 水槽管理タブで対応", key="nav_tank",
                                     use_container_width=True):
            st.session_state["jump_target"] = TAB_TANK
            st.rerun()

    st.markdown("<br/>", unsafe_allow_html=True)

    # === アクティブ・トライアル ===
    st.markdown('<div class="zf-section-label">💕 アクティブ・トライアル</div>', unsafe_allow_html=True)
    st.subheader("進行中の交配トライアル")
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
    if st.button("💕 交配トライアルタブへ", key="nav_trial",
                 use_container_width=False):
        st.session_state["jump_target"] = TAB_TRIAL
        st.rerun()

    st.markdown("<br/>", unsafe_allow_html=True)

    # === データエクスポート ===
    st.markdown('<div class="zf-section-label">📥 データエクスポート</div>', unsafe_allow_html=True)
    st.subheader("CSV ダウンロード")
    d1, d2, d3, d4 = st.columns(4)
    with d1:
        st.download_button("水槽", to_csv_bytes(df_tanks), csv_filename("tanks"),
                           "text/csv", disabled=df_tanks.empty, use_container_width=True)
    with d2:
        st.download_button("産卵成績", to_csv_bytes(df_spawn), csv_filename("spawning_records"),
                           "text/csv", disabled=df_spawn.empty, use_container_width=True)
    with d3:
        st.download_button("給餌ログ", to_csv_bytes(fetch_df("SELECT * FROM feeding_logs")),
                           csv_filename("feeding_logs"), "text/csv", use_container_width=True)
    with d4:
        st.download_button("トライアル", to_csv_bytes(df_trials), csv_filename("mating_trials"),
                           "text/csv", disabled=df_trials.empty, use_container_width=True)


# ============================================================
# 🍚 餌やり（全水槽一斉）
# ============================================================
with tab_feed:
    st.header("餌やりログ")
    st.caption(f"目標：1日 {FEEDS_PER_DAY} 回（全水槽に一斉に給餌）")

    today = today_jst().isoformat()
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
        log_action("餌やり", details=(memo or "全水槽給餌"))
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

    with st.expander("[history] 📜 過去の給餌ログ（全件）"):
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
    st.caption("元水槽・交配用水槽を指定して交配を計画。個別タグで特定の魚を追跡したい時は任意で入力。")

    tank_ids = fetch_df("SELECT tank_id FROM tanks ORDER BY tank_id")["tank_id"].tolist()

    # --- 新規計画 ---
    with st.expander("[new] ➕ 新規トライアルを計画する", expanded=False):
        with st.form("new_trial"):
            c1, c2 = st.columns(2)
            with c1:
                planned = st.date_input("採卵予定日", value=today_jst() + timedelta(days=1))
                src_m = st.selectbox("オス側の元水槽（戻し先）", [""] + tank_ids)
                src_f = st.selectbox("メス側の元水槽（戻し先）", [""] + tank_ids)
            with c2:
                breed = st.selectbox("交配用水槽", [""] + tank_ids)
                tc1, tc2 = st.columns(2)
                male_tag = tc1.text_input("♂ 個別タグ（任意）", placeholder="例: M-01")
                female_tag = tc2.text_input("♀ 個別タグ（任意）", placeholder="例: F-01")

            notes = st.text_area("メモ")
            ok = st.form_submit_button("計画を登録", type="primary")
            if ok:
                new_tid = execute(
                    """INSERT INTO mating_trials
                       (planned_date, male_id, female_id, source_tank_male, source_tank_female,
                        breeding_tank_id, notes, male_tag, female_tag)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (planned.isoformat(), None, None, src_m or None, src_f or None,
                     breed or None, notes, male_tag or None, female_tag or None),
                )
                log_detail_parts = []
                if src_m or src_f:
                    log_detail_parts.append(f"戻し♂{src_m or '-'}/♀{src_f or '-'}")
                if breed:
                    log_detail_parts.append(f"交配槽{breed}")
                if male_tag or female_tag:
                    log_detail_parts.append(f"タグ♂{male_tag or '-'}/♀{female_tag or '-'}")
                log_detail_parts.append(f"予定{planned.isoformat()}")
                log_action("トライアル計画", target=f"#{new_tid}",
                           details=" / ".join(log_detail_parts))
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
                m_tag = t["male_tag"] if "male_tag" in t.index else None
                f_tag = t["female_tag"] if "female_tag" in t.index else None
                m_id = t["male_id"] if pd.notna(t.get("male_id")) and t["male_id"] else None
                f_id = t["female_id"] if pd.notna(t.get("female_id")) and t["female_id"] else None
                m_show = m_id or (m_tag if pd.notna(m_tag) and m_tag else None) or "—"
                f_show = f_id or (f_tag if pd.notna(f_tag) and f_tag else None) or "—"
                m_label = f"♂ {m_show}"
                f_label = f"♀ {f_show}"
                top[0].markdown(f"### {badge} Trial #{tid} — {status}")
                top[0].caption(f"予定日: {t['planned_date']}　{m_label}　×　{f_label}")
                top[1].markdown(f"**交配用水槽:** {t['breeding_tank_id'] or '-'}")
                top[2].markdown(f"**戻し先:** ♂{t['source_tank_male'] or '-'} / ♀{t['source_tank_female'] or '-'}")

                # 状態に応じた次アクション
                if status == "計画中":
                    if st.button("✅ 前日セット完了にする", key=f"setup_{tid}", type="primary"):
                        execute(
                            "UPDATE mating_trials SET status='前日セット済み', setup_at=? WHERE trial_id=?",
                            (now_iso(), tid),
                        )
                        log_action("トライアル前日セット", target=f"#{tid}")
                        st.rerun()
                elif status == "前日セット済み":
                    c = st.columns(2)
                    if c[0].button("🔓 仕切り取り出し（交配開始）", key=f"div_{tid}"):
                        execute(
                            "UPDATE mating_trials SET divider_removed_at=? WHERE trial_id=?",
                            (now_iso(), tid),
                        )
                        log_action("仕切り取り出し", target=f"#{tid}")
                        st.rerun()
                    if c[1].button("🥚 採卵完了 → 結果入力", key=f"collect_{tid}", type="primary"):
                        st.session_state[f"collecting_{tid}"] = True
                elif status == "採卵済み":
                    if st.button("🏠 戻し完了にする", key=f"return_{tid}", type="primary"):
                        execute(
                            "UPDATE mating_trials SET status='戻し済み', returned_at=? WHERE trial_id=?",
                            (now_iso(), tid),
                        )
                        log_action("トライアル戻し完了", target=f"#{tid}")
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
                            # 親は元水槽IDを使う（新モデルでは tank_id ベース）
                            parent_m = t.get("source_tank_male") if pd.notna(t.get("source_tank_male")) else None
                            parent_f = t.get("source_tank_female") if pd.notna(t.get("source_tank_female")) else None
                            hist_id = execute(
                                """INSERT INTO spawning_records
                                   (spawning_date, male_parent_id, female_parent_id, egg_count, fertilization_rate)
                                   VALUES (?, ?, ?, ?, ?)""",
                                (t["planned_date"], parent_m, parent_f, int(eggs), float(rate)),
                            )
                            execute(
                                """UPDATE mating_trials
                                   SET status='採卵済み', egg_collected_at=?, spawning_history_id=?
                                   WHERE trial_id=?""",
                                (now_iso(), hist_id, tid),
                            )
                            log_action("採卵", target=f"#{tid}",
                                       details=f"卵 {int(eggs)} 個 / 受精率 {rate}%")
                            st.session_state[f"collecting_{tid}"] = False
                            st.success("採卵結果を登録しました（産卵成績に自動反映）")
                            st.rerun()

                # キャンセル
                with st.expander("[cancel] ⛔ このトライアルを中止"):
                    if st.button("中止する", key=f"cancel_{tid}"):
                        execute("UPDATE mating_trials SET status='中止' WHERE trial_id=?", (tid,))
                        log_action("トライアル中止", target=f"#{tid}")
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
    st.caption("過去の産卵成績から、成功率の高い水槽・ペアを自動で算出します")

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

        st.subheader("♂ オス側水槽ランキング")
        st.dataframe(
            aggregate("male_parent_id", "♂水槽")[
                ["♂水槽", "試行回数", "成功率(%)", "平均採卵数", "平均受精率", "最終試行日", "信頼度"]
            ],
            use_container_width=True, hide_index=True,
        )

        st.subheader("♀ メス側水槽ランキング")
        st.dataframe(
            aggregate("female_parent_id", "♀水槽")[
                ["♀水槽", "試行回数", "成功率(%)", "平均採卵数", "平均受精率", "最終試行日", "信頼度"]
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
               f"最大 {len(RACKS)*len(TIERS)*len(COLS)} 水槽　／　水槽に魚情報を直接記録します。")

    # === 場所セレクタは form の外（水槽IDが入力に追従して即時表示） ===
    st.subheader("新規登録 / 内容更新")
    st.markdown("**場所**（ラック - 段 - 列）")
    lc1, lc2, lc3 = st.columns(3)
    rack = lc1.selectbox("ラック", [""] + RACKS, key="form_rack")
    tier_str = lc2.selectbox("段", [""] + TIERS, key="form_tier")
    col_str = lc3.selectbox("列", [""] + [f"{c:02d}" for c in COLS], key="form_col")

    tank_id_auto = format_location(
        rack or None,
        tier_str or None,
        int(col_str) if col_str else None,
    )
    _existing_tanks_df = fetch_df(
        "SELECT tank_id, male_count, female_count, unknown_count, lineage, "
        "set_date, health_status, memo FROM tanks"
    )
    _existing_tanks = _existing_tanks_df["tank_id"].tolist()
    _tank_exists = tank_id_auto and tank_id_auto in _existing_tanks
    _existing_row = (_existing_tanks_df[_existing_tanks_df["tank_id"] == tank_id_auto].iloc[0]
                     if _tank_exists else None)
    _tank_note = (
        '　<span style="color:#BE8763;font-family:inherit">※ 既存IDのため上書きされます</span>'
        if _tank_exists else ''
    )
    st.markdown(
        "<div style='margin:6px 0 14px 0;padding:12px 16px;"
        "background:rgba(255,255,255,0.7);border:1px solid #E5E5EA;"
        "border-radius:14px;font-family:ui-monospace,Menlo,monospace;"
        f"font-size:16px;color:#1D1D1F'>"
        f"水槽ID（自動生成）：<b style='font-size:18px'>{tank_id_auto or '（場所を選択）'}</b>{_tank_note}</div>",
        unsafe_allow_html=True,
    )

    # 既存タンクが選ばれたら現値を初期値に
    if _tank_exists and st.session_state.get("_tk_loaded_for") != tank_id_auto:
        st.session_state["_tk_loaded_for"] = tank_id_auto
        st.session_state["tk_m"] = int(_existing_row["male_count"] or 0)
        st.session_state["tk_f"] = int(_existing_row["female_count"] or 0)
        st.session_state["tk_u"] = int(_existing_row["unknown_count"] or 0)
        st.session_state["tk_lineage"] = _existing_row["lineage"] or ""
        st.session_state["tk_health"] = _existing_row["health_status"] or "良好"
        st.session_state["tk_memo"] = _existing_row["memo"] or ""

    with st.form("tank_form", clear_on_submit=False):
        c1, c2 = st.columns(2)
        with c1:
            lineage = st.text_input("系統名（例: AB, TU, WIK）", key="tk_lineage")
            health = st.selectbox("健康状態", ["良好", "要観察", "隔離中"], key="tk_health")
        with c2:
            st.markdown("**匹数（性別ごと）**")
            mc1, mc2, mc3 = st.columns(3)
            tk_m = mc1.number_input("♂ オス", min_value=0, step=1, value=0, key="tk_m")
            tk_f = mc2.number_input("♀ メス", min_value=0, step=1, value=0, key="tk_f")
            tk_u = mc3.number_input("？ 不明", min_value=0, step=1, value=0, key="tk_u")
            tk_total = int(tk_m) + int(tk_f) + int(tk_u)
            tk_label = "空" if tk_total == 0 else f"{tk_total} 匹"
            st.markdown(f"<div style='margin-top:8px;font-size:13px;color:#6E6E73'>"
                        f"合計 <b style='color:#1D1D1F;font-size:18px'>{tk_label}</b></div>",
                        unsafe_allow_html=True)
        memo = st.text_area("メモ", height=70, key="tk_memo")

        if st.form_submit_button("登録 / 更新", type="primary"):
            if not tank_id_auto:
                st.error("ラック・段・列をすべて選んでください（水槽IDが生成されません）")
            else:
                set_date_v = (now_iso() if not _tank_exists
                              else (_existing_row["set_date"] or now_iso()))
                execute(
                    """INSERT INTO tanks (tank_id, rack, tier, col_no, health_status, memo,
                                          male_count, female_count, unknown_count, lineage, set_date)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(tank_id) DO UPDATE SET
                         rack=excluded.rack, tier=excluded.tier, col_no=excluded.col_no,
                         health_status=excluded.health_status, memo=excluded.memo,
                         male_count=excluded.male_count, female_count=excluded.female_count,
                         unknown_count=excluded.unknown_count, lineage=excluded.lineage""",
                    (tank_id_auto, rack, tier_str, int(col_str), health, memo,
                     int(tk_m), int(tk_f), int(tk_u), lineage or None, set_date_v),
                )
                log_action("水槽登録/更新", target=tank_id_auto,
                           details=f"♂{int(tk_m)} / ♀{int(tk_f)} / ?{int(tk_u)} / {health}")
                st.success(f"水槽 {tank_id_auto}（{tk_label}）を登録/更新しました")
                st.rerun()

    st.divider()

    # === スワップ機能 ===
    with st.expander("[swap] 🔄 2水槽の中身をスワップする", expanded=False):
        st.caption("選んだ2水槽の **中身（匹数・系統・健康状態・メモ）** を入れ替えます。場所（ラック/段/列/水槽ID）はそのままです。")
        if len(_existing_tanks) < 2:
            st.info("スワップには水槽が2つ以上必要です")
        else:
            sw1, sw2 = st.columns(2)
            tank_a = sw1.selectbox("水槽 A", _existing_tanks, key="swap_a")
            tank_b = sw2.selectbox("水槽 B", _existing_tanks, key="swap_b",
                                    index=min(1, len(_existing_tanks) - 1))
            if st.button("🔄 スワップ実行", type="primary", key="swap_btn"):
                if tank_a == tank_b:
                    st.error("違う水槽を2つ選んでください")
                else:
                    with get_conn() as conn:
                        row_a = conn.execute(
                            "SELECT male_count, female_count, unknown_count, lineage, "
                            "set_date, health_status, memo FROM tanks WHERE tank_id=?",
                            (tank_a,),
                        ).fetchone()
                        row_b = conn.execute(
                            "SELECT male_count, female_count, unknown_count, lineage, "
                            "set_date, health_status, memo FROM tanks WHERE tank_id=?",
                            (tank_b,),
                        ).fetchone()
                        for tid, src in [(tank_a, row_b), (tank_b, row_a)]:
                            conn.execute(
                                "UPDATE tanks SET male_count=?, female_count=?, unknown_count=?, "
                                "lineage=?, set_date=?, health_status=?, memo=? WHERE tank_id=?",
                                (*src, tid),
                            )
                        conn.commit()
                    log_action("水槽スワップ", target=f"{tank_a} ↔ {tank_b}")
                    st.success(f"水槽 {tank_a} と {tank_b} の中身を入れ替えました")
                    st.rerun()

    st.divider()
    st.subheader("登録済み水槽")

    df = fetch_df(
        "SELECT tank_id, rack, tier, col_no, male_count, female_count, unknown_count, "
        "lineage, set_date, health_status, memo "
        "FROM tanks ORDER BY rack, tier, col_no, tank_id"
    )
    if not df.empty:
        df["場所"] = df.apply(
            lambda r: format_location(r["rack"], r["tier"], r["col_no"]), axis=1
        )

        def fmt_counts_row(r):
            m, f, u = int(r["male_count"] or 0), int(r["female_count"] or 0), int(r["unknown_count"] or 0)
            tot = m + f + u
            if tot == 0:
                return "空"
            parts = []
            if m > 0: parts.append(f"♂{m}")
            if f > 0: parts.append(f"♀{f}")
            if u > 0: parts.append(f"？{u}")
            return " / ".join(parts) + f"  計{tot}"
        df["匹数"] = df.apply(fmt_counts_row, axis=1)

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
            view[["tank_id", "場所", "匹数", "lineage", "health_status",
                  "set_date", "memo"]].rename(
                columns={"tank_id": "水槽ID", "lineage": "系統",
                         "health_status": "健康状態", "set_date": "入居日",
                         "memo": "メモ"}
            ),
            use_container_width=True, hide_index=True,
        )
    else:
        st.info("まだ水槽が登録されていません")

    _df_raw = fetch_df("SELECT * FROM tanks ORDER BY rack, tier, col_no, tank_id")
    st.download_button("📥 CSVダウンロード", to_csv_bytes(_df_raw), csv_filename("tanks"),
                       "text/csv", disabled=_df_raw.empty, key="dl_tanks")

    # ----------------------------------------------------------------
    # CSV 一括インポート
    # ----------------------------------------------------------------
    with st.expander("[import] 📤 CSV 一括インポート", expanded=False):
        st.markdown(
            "Excelで水槽リストを作って一気に登録できます。"
            "**同じ tank_id があれば上書き、無ければ新規登録** されます。"
        )

        template_rows = []
        for r in RACKS[:1]:
            for t in TIERS[:2]:
                for c in COLS[:2]:
                    template_rows.append({
                        "tank_id": f"{r}-{t}-{c:02d}",
                        "rack": r, "tier": t, "col_no": c,
                        "male_count": 5, "female_count": 3, "unknown_count": 0,
                        "lineage": "AB",
                        "health_status": "良好",
                        "memo": "",
                    })
        template_df = pd.DataFrame(
            template_rows,
            columns=["tank_id", "rack", "tier", "col_no",
                     "male_count", "female_count", "unknown_count",
                     "lineage", "health_status", "memo"],
        )
        st.download_button(
            "📄 テンプレートCSVをダウンロード",
            to_csv_bytes(template_df),
            "tank_template.csv",
            "text/csv",
            key="dl_template",
        )
        st.caption(
            "必須列：tank_id, rack, tier, col_no　／　任意列：male_count, "
            "female_count, unknown_count, lineage, health_status, memo"
        )

        uploaded = st.file_uploader("CSVファイルを選択", type=["csv"], key="csv_upload")
        if uploaded is not None:
            try:
                up_df = pd.read_csv(uploaded, encoding="utf-8-sig", dtype=str).fillna("")
            except Exception as e:
                st.error(f"CSV読み込みエラー：{e}")
                up_df = None

            if up_df is not None:
                st.success(f"📂 {len(up_df)} 行を読み込みました")
                st.dataframe(up_df.head(20), use_container_width=True, hide_index=True)
                if len(up_df) > 20:
                    st.caption(f"...残り {len(up_df) - 20} 行はインポート時に処理します")

                errors = []
                warnings_list = []

                required_cols = ["tank_id", "rack", "tier", "col_no"]
                missing = [c for c in required_cols if c not in up_df.columns]
                if missing:
                    errors.append(f"必須列が不足しています: {missing}")

                if not errors:
                    bad_rows = []
                    for i, row in up_df.iterrows():
                        rownum = i + 2
                        if not str(row["tank_id"]).strip():
                            bad_rows.append(f"行{rownum}: tank_id が空")
                            continue
                        rack_v = str(row["rack"]).strip()
                        if rack_v and rack_v not in RACKS:
                            bad_rows.append(f"行{rownum}: rack='{rack_v}' は {RACKS} のいずれかにしてください")
                        if str(row["tier"]).strip():
                            tv = str(row["tier"]).strip()
                            if tv not in TIERS:
                                bad_rows.append(f"行{rownum}: tier='{tv}' は {TIERS} のいずれかにしてください")
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
                                bad_rows.append(f"行{rownum}: health_status='{hs}' は [良好/要観察/隔離中]")

                    if bad_rows:
                        errors.extend(bad_rows[:20])
                        if len(bad_rows) > 20:
                            errors.append(f"...他に {len(bad_rows) - 20} 件のエラー")

                    dups = up_df["tank_id"][up_df["tank_id"].duplicated()].unique().tolist()
                    if len(dups):
                        warnings_list.append(f"CSV内で重複する tank_id: {dups[:10]}")

                if errors:
                    st.error("❌ 検証エラーがあります")
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
                                    tier_v = str(row.get("tier", "")).strip() or None
                                    col_v = int(float(row["col_no"])) if str(row.get("col_no", "")).strip() else None
                                    hs_v = str(row.get("health_status", "")).strip() or "良好"
                                    memo_v = str(row.get("memo", "")).strip() or None
                                    lin_v = str(row.get("lineage", "")).strip() or None
                                    mc = int(float(row.get("male_count", "") or 0))
                                    fc = int(float(row.get("female_count", "") or 0))
                                    uc = int(float(row.get("unknown_count", "") or 0))
                                    conn.execute(
                                        """INSERT INTO tanks
                                           (tank_id, rack, tier, col_no, health_status, memo,
                                            male_count, female_count, unknown_count, lineage, set_date)
                                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                           ON CONFLICT(tank_id) DO UPDATE SET
                                             rack=excluded.rack, tier=excluded.tier, col_no=excluded.col_no,
                                             health_status=excluded.health_status, memo=excluded.memo,
                                             male_count=excluded.male_count,
                                             female_count=excluded.female_count,
                                             unknown_count=excluded.unknown_count,
                                             lineage=excluded.lineage""",
                                        (str(row["tank_id"]).strip(), rack_v, tier_v, col_v,
                                         hs_v, memo_v, mc, fc, uc, lin_v, now_iso()),
                                    )
                                    ok += 1
                                except Exception:
                                    ng += 1
                            conn.commit()
                        st.success(f"🎉 {ok} 件を登録/更新しました" + (f"（失敗 {ng} 件）" if ng else ""))
                        st.rerun()

    with st.expander("[delete] 🗑️ 水槽を削除する"):
        if not df.empty:
            del_id = st.selectbox("削除する水槽ID", df["tank_id"].tolist(), key="del_tank")
            if st.button("削除", type="primary", key="del_tank_btn"):
                execute("DELETE FROM tanks WHERE tank_id = ?", (del_id,))
                log_action("水槽削除", target=del_id)
                st.rerun()



# ============================================================
# 🥚 産卵成績（手入力 & 履歴）
# ============================================================
with tab_spawn:
    st.header("産卵成績")
    st.caption("水槽IDをそのまま親として記録します。同じ水槽の中身が変わっても、入居日で世代を区別できます。")

    # 水槽情報
    sp_tanks_df = fetch_df(
        "SELECT tank_id, lineage, male_count, female_count, unknown_count "
        "FROM tanks ORDER BY tank_id"
    )
    sp_tank_ids = sp_tanks_df["tank_id"].tolist()

    def _fmt_tank(tid):
        if not tid:
            return "（未選択）"
        r = sp_tanks_df[sp_tanks_df["tank_id"] == tid]
        if r.empty:
            return tid
        row = r.iloc[0]
        parts = []
        if int(row["male_count"] or 0) > 0:    parts.append(f"♂{int(row['male_count'])}")
        if int(row["female_count"] or 0) > 0:  parts.append(f"♀{int(row['female_count'])}")
        if int(row["unknown_count"] or 0) > 0: parts.append(f"？{int(row['unknown_count'])}")
        suffix = f"  ({' / '.join(parts)})" if parts else "  (空)"
        lin = f"  ・{row['lineage']}" if row["lineage"] else ""
        return f"{tid}{suffix}{lin}"

    st.subheader("手入力で追加")
    sc1, sc2, sc3 = st.columns(3)
    with sc1:
        sdate = st.date_input("産卵日", value=today_jst(), key="sp_date")
    with sc2:
        male_tank = st.selectbox("♂ オス側の水槽", [""] + sp_tank_ids,
                                  format_func=_fmt_tank, key="sp_male_tank")
    with sc3:
        female_tank = st.selectbox("♀ メス側の水槽", [""] + sp_tank_ids,
                                    format_func=_fmt_tank, key="sp_female_tank")

    with st.form("spawn_form", clear_on_submit=False):
        fc1, fc2 = st.columns(2)
        eggs = fc1.number_input("採卵数", min_value=0, step=1, key="sp_eggs")
        rate = fc2.number_input("受精率 (%)", min_value=0.0, max_value=100.0,
                                 step=0.1, key="sp_rate")
        if st.form_submit_button("登録", type="primary"):
            if not male_tank or not female_tank:
                st.error("♂ と ♀ の水槽を選択してください")
            else:
                execute(
                    """INSERT INTO spawning_records
                       (spawning_date, male_parent_id, female_parent_id, egg_count, fertilization_rate)
                       VALUES (?, ?, ?, ?, ?)""",
                    (sdate.isoformat(), male_tank, female_tank, int(eggs), float(rate)),
                )
                log_action("産卵成績(手入力)",
                           details=f"♂{male_tank} × ♀{female_tank} / "
                                   f"卵{int(eggs)} / 受精率{rate}%")
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


# ============================================================
# 📔 ログ
# ============================================================
with tab_log:
    st.header("アクティビティログ")
    st.caption("誰がいつ何をしたかを記録。サイドバーで担当者名を設定すると、以後のログに自動で記録されます。")

    log_df_all = fetch_df(
        "SELECT log_id, occurred_at, category, actor, target, details "
        "FROM activity_logs ORDER BY log_id DESC"
    )

    if log_df_all.empty:
        st.info("ログがまだありません。何か操作するとここに記録されます。")
    else:
        # フィルタ
        st.markdown('<div class="zf-section-label">🔍 絞り込み</div>', unsafe_allow_html=True)
        fc1, fc2, fc3, fc4 = st.columns([2, 2, 2, 3])

        all_cats = sorted(log_df_all["category"].dropna().unique().tolist())
        f_cat = fc1.multiselect("種別", all_cats, default=[], key="log_f_cat")

        all_actors = sorted([a for a in log_df_all["actor"].dropna().unique().tolist() if a])
        f_actor = fc2.multiselect("担当者", all_actors, default=[], key="log_f_actor")

        # 日付範囲：最古〜今日
        min_date = pd.to_datetime(log_df_all["occurred_at"]).min().date()
        max_date = today_jst()
        f_dates = fc4.date_input(
            "日付範囲", value=(min_date, max_date),
            min_value=min_date, max_value=max_date, key="log_f_dates",
        )
        keyword = fc3.text_input("対象/詳細にキーワード", key="log_f_kw")

        view = log_df_all.copy()
        if f_cat:
            view = view[view["category"].isin(f_cat)]
        if f_actor:
            view = view[view["actor"].isin(f_actor)]
        if keyword:
            kw = keyword.lower()
            view = view[
                view["target"].fillna("").str.lower().str.contains(kw, na=False)
                | view["details"].fillna("").str.lower().str.contains(kw, na=False)
            ]
        if isinstance(f_dates, tuple) and len(f_dates) == 2:
            d_from, d_to = f_dates
            view = view[
                (view["occurred_at"] >= f"{d_from.isoformat()} 00:00:00")
                & (view["occurred_at"] <= f"{d_to.isoformat()} 23:59:59")
            ]

        st.caption(f"表示中: {len(view)} / 全 {len(log_df_all)} 件")

        # テキスト形式で表示
        st.markdown(render_log_lines_html(view), unsafe_allow_html=True)

        # CSVは生データで提供
        st.download_button(
            "📥 CSVダウンロード（生データ）", to_csv_bytes(view.rename(columns={
                "occurred_at": "時刻", "category": "種別", "actor": "担当",
                "target": "対象", "details": "詳細",
            })[["時刻", "種別", "担当", "対象", "詳細"]]),
            csv_filename("activity_logs"), "text/csv",
            disabled=view.empty, key="dl_logs",
        )

        with st.expander("[stats] 📊 種別別の件数"):
            cat_counts = view.groupby("category").size().reset_index(name="件数")
            cat_counts = cat_counts.rename(columns={"category": "種別"}).sort_values("件数", ascending=False)
            st.dataframe(cat_counts, use_container_width=True, hide_index=True)

        with st.expander("[purge] 🗑️ 古いログを削除"):
            del_before = st.date_input("これより前のログを全削除", value=today_jst(),
                                        key="log_purge_date")
            if st.button("削除実行", type="primary", key="log_purge_btn"):
                cutoff = f"{del_before.isoformat()} 00:00:00"
                execute("DELETE FROM activity_logs WHERE occurred_at < ?", (cutoff,))
                log_action("ログ削除", details=f"{cutoff} 以前")
                st.success("削除しました")
                st.rerun()
