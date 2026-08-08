import json
import sqlite3
from datetime import datetime

DB_PATH = "polls.db"

_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_conn.row_factory = sqlite3.Row


def init_db():
    _conn.execute(
        """
        CREATE TABLE IF NOT EXISTS polls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vk_poll_id INTEGER NOT NULL,
            owner_id INTEGER NOT NULL,
            peer_id INTEGER NOT NULL,
            question TEXT NOT NULL,
            participant_ids TEXT NOT NULL,
            created_at TEXT NOT NULL,
            reminder_hours REAL NOT NULL,
            reminded INTEGER DEFAULT 0,
            closed INTEGER DEFAULT 0
        )
        """
    )
    _conn.execute(
        """
        CREATE TABLE IF NOT EXISTS children (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            peer_id INTEGER NOT NULL,
            full_name TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    _conn.execute(
        """
        CREATE TABLE IF NOT EXISTS child_parents (
            child_id INTEGER NOT NULL REFERENCES children(id),
            parent_user_id INTEGER NOT NULL,
            PRIMARY KEY (child_id, parent_user_id)
        )
        """
    )
    _conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tournaments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            peer_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            amount REAL NOT NULL,
            created_at TEXT NOT NULL,
            closed INTEGER DEFAULT 0
        )
        """
    )
    _conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tournament_payments (
            tournament_id INTEGER NOT NULL REFERENCES tournaments(id),
            child_id INTEGER NOT NULL REFERENCES children(id),
            paid INTEGER DEFAULT 0,
            paid_at TEXT,
            marked_by INTEGER,
            PRIMARY KEY (tournament_id, child_id)
        )
        """
    )
    _conn.commit()


def save_poll(vk_poll_id, owner_id, peer_id, question, participant_ids, reminder_hours):
    cur = _conn.execute(
        """INSERT INTO polls
           (vk_poll_id, owner_id, peer_id, question, participant_ids, created_at, reminder_hours)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            vk_poll_id,
            owner_id,
            peer_id,
            question,
            json.dumps(participant_ids),
            datetime.utcnow().isoformat(),
            reminder_hours,
        ),
    )
    _conn.commit()
    return cur.lastrowid


def _row_to_dict(row):
    d = dict(row)
    d["participant_ids"] = json.loads(d["participant_ids"])
    return d


def get_poll(local_id):
    row = _conn.execute("SELECT * FROM polls WHERE id = ?", (local_id,)).fetchone()
    return _row_to_dict(row) if row else None


def get_pending_reminders():
    rows = _conn.execute(
        "SELECT * FROM polls WHERE reminded = 0 AND closed = 0"
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def mark_reminded(local_id):
    _conn.execute("UPDATE polls SET reminded = 1 WHERE id = ?", (local_id,))
    _conn.commit()


def close_poll(local_id):
    _conn.execute("UPDATE polls SET closed = 1 WHERE id = ?", (local_id,))
    _conn.commit()


def list_active_polls(peer_id):
    rows = _conn.execute(
        "SELECT id, question FROM polls WHERE peer_id = ? AND closed = 0", (peer_id,)
    ).fetchall()
    return [dict(r) for r in rows]


# ---------- Дети и родители ----------

def add_child(peer_id, full_name):
    cur = _conn.execute(
        "INSERT INTO children (peer_id, full_name, created_at) VALUES (?, ?, ?)",
        (peer_id, full_name, datetime.utcnow().isoformat()),
    )
    _conn.commit()
    return cur.lastrowid


def get_child(child_id):
    row = _conn.execute(
        "SELECT * FROM children WHERE id = ?", (child_id,)
    ).fetchone()
    if not row:
        return None
    child = dict(row)
    child["parent_ids"] = get_parent_ids(child_id)
    return child


def remove_child(child_id):
    _conn.execute("DELETE FROM child_parents WHERE child_id = ?", (child_id,))
    _conn.execute("DELETE FROM children WHERE id = ?", (child_id,))
    _conn.commit()


def add_parent(child_id, parent_user_id):
    _conn.execute(
        "INSERT OR IGNORE INTO child_parents (child_id, parent_user_id) VALUES (?, ?)",
        (child_id, parent_user_id),
    )
    _conn.commit()


def remove_parent(child_id, parent_user_id):
    _conn.execute(
        "DELETE FROM child_parents WHERE child_id = ? AND parent_user_id = ?",
        (child_id, parent_user_id),
    )
    _conn.commit()


def get_parent_ids(child_id):
    rows = _conn.execute(
        "SELECT parent_user_id FROM child_parents WHERE child_id = ?", (child_id,)
    ).fetchall()
    return [r["parent_user_id"] for r in rows]


def list_children(peer_id):
    rows = _conn.execute(
        "SELECT id, full_name FROM children WHERE peer_id = ? ORDER BY full_name",
        (peer_id,),
    ).fetchall()
    children = [dict(r) for r in rows]
    for child in children:
        child["parent_ids"] = get_parent_ids(child["id"])
    return children


def count_children(peer_id):
    row = _conn.execute(
        "SELECT COUNT(*) AS c FROM children WHERE peer_id = ?", (peer_id,)
    ).fetchone()
    return row["c"]


def get_children_by_parent(peer_id, parent_user_id):
    rows = _conn.execute(
        """
        SELECT c.id, c.full_name FROM children c
        JOIN child_parents cp ON cp.child_id = c.id
        WHERE c.peer_id = ? AND cp.parent_user_id = ?
        ORDER BY c.full_name
        """,
        (peer_id, parent_user_id),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------- Турниры и оплаты ----------

def create_tournament(peer_id, name, amount, child_ids):
    cur = _conn.execute(
        "INSERT INTO tournaments (peer_id, name, amount, created_at) VALUES (?, ?, ?, ?)",
        (peer_id, name, amount, datetime.utcnow().isoformat()),
    )
    tournament_id = cur.lastrowid
    for child_id in child_ids:
        _conn.execute(
            "INSERT OR IGNORE INTO tournament_payments (tournament_id, child_id) VALUES (?, ?)",
            (tournament_id, child_id),
        )
    _conn.commit()
    return tournament_id


def get_tournament(tournament_id):
    row = _conn.execute(
        "SELECT * FROM tournaments WHERE id = ?", (tournament_id,)
    ).fetchone()
    return dict(row) if row else None


def get_latest_open_tournament(peer_id):
    row = _conn.execute(
        "SELECT * FROM tournaments WHERE peer_id = ? AND closed = 0 ORDER BY id DESC LIMIT 1",
        (peer_id,),
    ).fetchone()
    return dict(row) if row else None


def list_active_tournaments(peer_id):
    rows = _conn.execute(
        "SELECT id, name, amount FROM tournaments WHERE peer_id = ? AND closed = 0 ORDER BY id",
        (peer_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def close_tournament(tournament_id):
    _conn.execute("UPDATE tournaments SET closed = 1 WHERE id = ?", (tournament_id,))
    _conn.commit()


def add_tournament_participant(tournament_id, child_id):
    _conn.execute(
        "INSERT OR IGNORE INTO tournament_payments (tournament_id, child_id) VALUES (?, ?)",
        (tournament_id, child_id),
    )
    _conn.commit()


def mark_paid(tournament_id, child_id, marked_by_user_id):
    _conn.execute(
        """UPDATE tournament_payments
           SET paid = 1, paid_at = ?, marked_by = ?
           WHERE tournament_id = ? AND child_id = ?""",
        (datetime.utcnow().isoformat(), marked_by_user_id, tournament_id, child_id),
    )
    _conn.commit()


def mark_unpaid(tournament_id, child_id):
    _conn.execute(
        """UPDATE tournament_payments
           SET paid = 0, paid_at = NULL, marked_by = NULL
           WHERE tournament_id = ? AND child_id = ?""",
        (tournament_id, child_id),
    )
    _conn.commit()


def is_tournament_participant(tournament_id, child_id):
    row = _conn.execute(
        "SELECT 1 FROM tournament_payments WHERE tournament_id = ? AND child_id = ?",
        (tournament_id, child_id),
    ).fetchone()
    return row is not None


def get_tournament_status(tournament_id):
    rows = _conn.execute(
        """
        SELECT tp.child_id, c.full_name, tp.paid
        FROM tournament_payments tp
        JOIN children c ON c.id = tp.child_id
        WHERE tp.tournament_id = ?
        ORDER BY c.full_name
        """,
        (tournament_id,),
    ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["parent_ids"] = get_parent_ids(d["child_id"])
        result.append(d)
    return result
