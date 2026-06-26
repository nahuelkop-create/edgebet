import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[0].parent / "data" / "edgebet.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS bets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_user_id INTEGER NOT NULL,
    username TEXT,
    league TEXT,
    market TEXT,
    match_name TEXT,
    pick TEXT,
    stake REAL,
    odds REAL,
    result TEXT,
    profit REAL,
    created_at TEXT NOT NULL,
    resolved_at TEXT
);
"""

CREATE_BALANCE_SQL = """
CREATE TABLE IF NOT EXISTS balance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_user_id INTEGER NOT NULL,
    month TEXT NOT NULL,
    total REAL NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
"""


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_column(conn, table: str, column: str, ddl: str):
    """Add a column to an existing table if it is not present yet (lightweight migration)."""
    cols = [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def initialize_db():
    conn = get_connection()
    with conn:
        conn.execute(CREATE_SQL)
        conn.execute(CREATE_BALANCE_SQL)
        # Migrate older DBs that predate the match_name column.
        _ensure_column(conn, "bets", "match_name", "match_name TEXT")
    conn.close()


def add_bet(
    telegram_user_id: int,
    username: str,
    league: str,
    market: str,
    pick: str,
    stake: float,
    odds: float,
    match_name: str = None,
) -> int:
    """Insert a pending bet (result/profit stay NULL until resolved). Returns the new bet id."""
    now = datetime.utcnow().isoformat()
    conn = get_connection()
    with conn:
        cur = conn.execute(
            "INSERT INTO bets (telegram_user_id, username, league, market, match_name, pick, stake, odds, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (telegram_user_id, username, league, market, match_name, pick, stake, odds, now),
        )
        bet_id = cur.lastrowid
    conn.close()
    return bet_id


def get_pending_bets(telegram_user_id: int):
    """All bets that have not been resolved yet (result IS NULL)."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM bets WHERE telegram_user_id = ? AND result IS NULL ORDER BY created_at ASC",
        (telegram_user_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_bet(bet_id: int):
    conn = get_connection()
    row = conn.execute("SELECT * FROM bets WHERE id = ?", (bet_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_stats(telegram_user_id: int) -> dict:
    """Aggregate betting performance for a user: net profit, hit rate, win/loss
    counts, best winning pick and the current win/loss streak."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM bets WHERE telegram_user_id = ?",
        (telegram_user_id,),
    ).fetchall()
    conn.close()
    rows = [dict(r) for r in rows]

    resolved = [r for r in rows if r.get("result") in ("ganada", "perdida")]
    won = [r for r in resolved if r["result"] == "ganada"]
    lost = [r for r in resolved if r["result"] == "perdida"]
    pending = [r for r in rows if r.get("result") is None]

    total_profit = round(sum((r.get("profit") or 0.0) for r in resolved), 2)
    decided = len(won) + len(lost)
    win_rate = round(len(won) / decided * 100, 1) if decided else 0.0

    best = max(won, key=lambda r: (r.get("profit") or 0.0), default=None)

    # Current streak: walk resolved bets from most recent backwards.
    resolved_sorted = sorted(
        resolved, key=lambda r: (r.get("resolved_at") or "", r.get("id")), reverse=True
    )
    streak = 0
    streak_type = None
    for r in resolved_sorted:
        if streak_type is None:
            streak_type = r["result"]
            streak = 1
        elif r["result"] == streak_type:
            streak += 1
        else:
            break

    return {
        "total_profit": total_profit,
        "win_rate": win_rate,
        "won": len(won),
        "lost": len(lost),
        "pending": len(pending),
        "best_pick": best,
        "streak": streak,
        "streak_type": streak_type,
    }


def resolve_bet(bet_id: int, result: str, profit: float):
    now = datetime.utcnow().isoformat()
    conn = get_connection()
    with conn:
        conn.execute(
            "UPDATE bets SET result = ?, profit = ?, resolved_at = ? WHERE id = ?",
            (result, profit, now, bet_id),
        )
    conn.close()


def get_monthly_balance(telegram_user_id: int, month: str):
    conn = get_connection()
    row = conn.execute(
        "SELECT total FROM balance WHERE telegram_user_id = ? AND month = ?",
        (telegram_user_id, month),
    ).fetchone()
    conn.close()
    return row[0] if row else 0.0


def update_monthly_balance(telegram_user_id: int, month: str, amount: float):
    now = datetime.utcnow().isoformat()
    conn = get_connection()
    with conn:
        existing = conn.execute(
            "SELECT id, total FROM balance WHERE telegram_user_id = ? AND month = ?",
            (telegram_user_id, month),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE balance SET total = ?, updated_at = ? WHERE id = ?",
                (existing[1] + amount, now, existing[0]),
            )
        else:
            conn.execute(
                "INSERT INTO balance (telegram_user_id, month, total, updated_at) VALUES (?, ?, ?, ?)",
                (telegram_user_id, month, amount, now),
            )
    conn.close()
