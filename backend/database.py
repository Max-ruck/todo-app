"""
database.py  –  SQLite-Zugriffsschicht
Todos werden als relationale Zeilen gespeichert (statt JSON-Blobs).
GET/PUT /api/data bleiben kompatibel – data_load/data_save übersetzen.
"""
import json
import os
import random
import sqlite3
from datetime import datetime
from typing import Any, Optional

DB_PATH = os.path.join(os.path.dirname(__file__), "todo.db")

# Felder die direkt als Spalten gespeichert werden
_TODO_COLS = ("id", "title", "kunde", "sub", "subsub", "prio", "umgebung",
              "desc", "link", "recur", "blocked_by", "created")
# Felder die in extra_data JSON landen (Frontend-interne Felder)
_EXTRA_KEYS = {"blocked", "blockedBy", "parentTitle", "zeitGebucht",
               "recurLast", "lastCheckin", "checkinHistory", "esk"}


# ── Verbindung ─────────────────────────────────────────────────────────────────
def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _col_exists(conn: sqlite3.Connection, table: str, col: str) -> bool:
    return any(r["name"] == col
               for r in conn.execute(f"PRAGMA table_info({table})"))


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _add_col_if_missing(conn: sqlite3.Connection, table: str,
                        col: str, definition: str) -> None:
    if not _col_exists(conn, table, col):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {definition}")


# ── Schema ─────────────────────────────────────────────────────────────────────
def init_db() -> None:
    conn = get_conn()

    # ── Alte JSON-Blob-Tabellen VOR dem Schema-Create umbenennen ──────────────
    # CREATE TABLE IF NOT EXISTS tut nichts wenn Tabelle schon existiert,
    # daher erst umbenennen damit die neue Struktur erstellt werden kann.
    _needs_todo_migration = (
        _table_exists(conn, "todos")
        and _col_exists(conn, "todos", "data")
    )
    _needs_archiv_migration = _table_exists(conn, "archiv")

    if _needs_todo_migration:
        conn.execute("ALTER TABLE todos RENAME TO todos_blob_old")
        conn.commit()
    if _needs_archiv_migration:
        conn.execute("ALTER TABLE archiv RENAME TO archiv_blob_old")
        conn.commit()

    conn.executescript("""
        -- ── Benutzer ──────────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT    NOT NULL,
            discriminator INTEGER NOT NULL DEFAULT 0,
            user_tag      TEXT    UNIQUE,
            pw_hash       TEXT    NOT NULL,
            role          TEXT    NOT NULL DEFAULT 'member',
            status        TEXT    NOT NULL DEFAULT 'active',
            created       TEXT    DEFAULT (datetime('now'))
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_user_tag ON users(user_tag);

        -- ── Tagesberichte (JSON-Blob, unverändert) ─────────────────────────
        CREATE TABLE IF NOT EXISTS berichte (
            id      TEXT    PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            data    TEXT    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_berichte_user ON berichte(user_id);

        -- ── Einstellungen / kundenNotizen ─────────────────────────────────
        CREATE TABLE IF NOT EXISTS settings (
            user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            data    TEXT    NOT NULL DEFAULT '{}'
        );

        -- ── Posteingang ────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS inbox (
            id               TEXT    PRIMARY KEY,
            from_user_id     INTEGER NOT NULL REFERENCES users(id),
            to_user_id       INTEGER NOT NULL REFERENCES users(id),
            original_todo_id TEXT,
            todo_data        TEXT    NOT NULL,
            assign_comment   TEXT,
            status           TEXT    NOT NULL DEFAULT 'pending',
            response_comment TEXT,
            created          TEXT    DEFAULT (datetime('now'))
        );

        -- ── Gruppen ────────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS groups (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT    UNIQUE NOT NULL,
            created_by INTEGER REFERENCES users(id),
            created    TEXT    DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS group_members (
            group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
            user_id  INTEGER NOT NULL REFERENCES users(id)  ON DELETE CASCADE,
            role     TEXT    NOT NULL DEFAULT 'member',  -- 'member' | 'admin'
            PRIMARY KEY (group_id, user_id)
        );

        -- ── Freigaben ─────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS shares (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            viewer_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            permission TEXT    NOT NULL DEFAULT 'read',
            status     TEXT    NOT NULL DEFAULT 'accepted',  -- 'pending' | 'accepted'
            created    TEXT    DEFAULT (datetime('now')),
            UNIQUE (owner_id, viewer_id)
        );

        -- ── Einladungs-Tokens ─────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS invites (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            token      TEXT    UNIQUE NOT NULL,
            created_by INTEGER REFERENCES users(id),
            group_id   INTEGER REFERENCES groups(id) ON DELETE SET NULL,
            created_at TEXT    DEFAULT (datetime('now')),
            used       INTEGER NOT NULL DEFAULT 0
        );

        -- ── Todos (relational) ────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS todos (
            id               TEXT    PRIMARY KEY,
            owner_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            title            TEXT    NOT NULL DEFAULT '',
            kunde            TEXT    NOT NULL DEFAULT '',
            sub              TEXT    NOT NULL DEFAULT '',
            subsub           TEXT    NOT NULL DEFAULT '',
            prio             TEXT    NOT NULL DEFAULT 'mittel',
            umgebung         TEXT    NOT NULL DEFAULT 'intern',
            desc             TEXT    NOT NULL DEFAULT '',
            link             TEXT    NOT NULL DEFAULT '',
            recur            TEXT    NOT NULL DEFAULT '',
            delegated_to     INTEGER REFERENCES users(id),
            delegated_status TEXT,
            blocked_by       TEXT    NOT NULL DEFAULT '',
            created          TEXT    DEFAULT (datetime('now')),
            archived_at      TEXT,
            is_archived      INTEGER NOT NULL DEFAULT 0,
            sort_order       INTEGER NOT NULL DEFAULT 0,
            extra_data       TEXT    NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_todos_owner    ON todos(owner_id, is_archived);
        CREATE INDEX IF NOT EXISTS idx_todos_delegated ON todos(delegated_to);

        -- ── Checkin-Log ───────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS checkin_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            todo_id    TEXT    NOT NULL REFERENCES todos(id) ON DELETE CASCADE,
            user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            checked_at TEXT    NOT NULL,
            val        TEXT    NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_checkin_todo ON checkin_log(todo_id);

        -- ── Todo-Kommentare ───────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS todo_comments (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            todo_id  TEXT    NOT NULL REFERENCES todos(id) ON DELETE CASCADE,
            user_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            text     TEXT    NOT NULL,
            created  TEXT    DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_comments_todo ON todo_comments(todo_id);

        -- ── Todo-Zuweisungen ──────────────────────────────────────────────
        -- Ersetzt langfristig die inbox-Tabelle; inbox bleibt für Rückwärts-
        -- kompatibilität bis Frontend vollständig migriert ist.
        CREATE TABLE IF NOT EXISTS todo_assignments (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            todo_id      TEXT    NOT NULL REFERENCES todos(id) ON DELETE CASCADE,
            from_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            to_user_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            status       TEXT    NOT NULL DEFAULT 'pending',  -- pending/accepted/rejected/completed
            comment      TEXT,
            response     TEXT,
            created      TEXT    DEFAULT (datetime('now')),
            updated      TEXT    DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_assign_todo   ON todo_assignments(todo_id);
        CREATE INDEX IF NOT EXISTS idx_assign_to     ON todo_assignments(to_user_id, status);
        CREATE INDEX IF NOT EXISTS idx_assign_from   ON todo_assignments(from_user_id, status);

        -- ── Gruppen-Archiv-Log ───────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS group_archive_log (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id      INTEGER REFERENCES groups(id) ON DELETE CASCADE,
            todo_id       TEXT,
            todo_title    TEXT,
            action        TEXT,        -- 'created','status_changed','completed','deleted'
            actor_user_tag TEXT,
            old_value     TEXT,
            new_value     TEXT,
            created       TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_glog_group ON group_archive_log(group_id, created);

        -- ── Verbindungen (materialisierte Sicht: Gruppe + Freigabe) ─────
        -- Canonical: user_a < user_b (immer nur eine Zeile pro Paar)
        CREATE TABLE IF NOT EXISTS user_connections (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            user_a  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            user_b  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created TEXT    DEFAULT (datetime('now')),
            UNIQUE (user_a, user_b)
        );
        CREATE INDEX IF NOT EXISTS idx_conn_a ON user_connections(user_a);
        CREATE INDEX IF NOT EXISTS idx_conn_b ON user_connections(user_b);

        -- ── Indices für andere Tabellen ───────────────────────────────────
        CREATE INDEX IF NOT EXISTS idx_inbox_to      ON inbox(to_user_id, status);
        CREATE INDEX IF NOT EXISTS idx_inbox_from    ON inbox(from_user_id, status);
        CREATE INDEX IF NOT EXISTS idx_shares_viewer ON shares(viewer_id);
        CREATE INDEX IF NOT EXISTS idx_grp_members   ON group_members(user_id);
    """)

    # ── User-Migrations ────────────────────────────────────────────────────────
    _add_col_if_missing(conn, "users", "discriminator",    "INTEGER NOT NULL DEFAULT 0")
    _add_col_if_missing(conn, "users", "user_tag",         "TEXT")
    _add_col_if_missing(conn, "users", "role",             "TEXT NOT NULL DEFAULT 'member'")
    _add_col_if_missing(conn, "inbox", "original_todo_id", "TEXT")
    _add_col_if_missing(conn, "todos", "archived_by",      "INTEGER REFERENCES users(id)")
    # shares.status: bestehende Zeilen galten als akzeptiert
    _add_col_if_missing(conn, "shares", "status", "TEXT NOT NULL DEFAULT 'accepted'")
    _add_col_if_missing(conn, "group_members", "role",     "TEXT NOT NULL DEFAULT 'member'")
    _add_col_if_missing(conn, "invites",       "group_id", "INTEGER REFERENCES groups(id) ON DELETE SET NULL")
    _add_col_if_missing(conn, "users", "status",         "TEXT NOT NULL DEFAULT 'active'")
    _add_col_if_missing(conn, "users", "deactivated_at", "TEXT")
    _add_col_if_missing(conn, "todos",         "pool_group_id",   "INTEGER REFERENCES groups(id)")
    _add_col_if_missing(conn, "todos",         "pool_open",       "INTEGER NOT NULL DEFAULT 0")
    _add_col_if_missing(conn, "group_members", "can_post_to_pool","INTEGER NOT NULL DEFAULT 0")
    # Rename pool_group_id → group_id (add new column, copy data)
    _add_col_if_missing(conn, "todos", "group_id", "INTEGER REFERENCES groups(id)")
    conn.execute("UPDATE todos SET group_id = pool_group_id WHERE pool_group_id IS NOT NULL AND group_id IS NULL")
    _add_col_if_missing(conn, "group_archive_log", "todo_group_id", "INTEGER REFERENCES groups(id)")
    conn.commit()

    for row in conn.execute("SELECT id, username, discriminator FROM users WHERE user_tag IS NULL"):
        conn.execute("UPDATE users SET user_tag = ? WHERE id = ?",
                     (f"{row['username']}#{row['discriminator']:04d}", row["id"]))

    no_admin  = conn.execute("SELECT COUNT(*) FROM users WHERE role='admin'").fetchone()[0] == 0
    has_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] > 0
    if no_admin and has_users:
        oldest = conn.execute("SELECT id FROM users ORDER BY id ASC LIMIT 1").fetchone()["id"]
        conn.execute("UPDATE users SET role='admin' WHERE id=?", (oldest,))

    # ── Todo-Migration: JSON-Blob → relationale Tabelle ────────────────────────
    if _needs_todo_migration:
        _migrate_blobs_to_relational(conn, src_table="todos_blob_old", archived=False)
        conn.execute("DROP TABLE todos_blob_old")
    if _needs_archiv_migration:
        _migrate_blobs_to_relational(conn, src_table="archiv_blob_old", archived=True)
        conn.execute("DROP TABLE archiv_blob_old")

    # ── user_connections backfill (einmalig: bestehende Verbindungen eintragen) ─
    _backfill_connections(conn)

    conn.commit()
    conn.close()


def _backfill_connections(conn: sqlite3.Connection) -> None:
    """Trägt alle bestehenden Verbindungen (Freigaben + gemeinsame Gruppen) nach."""
    # Aus Freigaben (beide Richtungen → ein kanonisches Paar)
    for row in conn.execute("SELECT owner_id, viewer_id FROM shares"):
        a, b = sorted([row["owner_id"], row["viewer_id"]])
        conn.execute(
            "INSERT OR IGNORE INTO user_connections (user_a, user_b) VALUES (?,?)", (a, b)
        )
    # Aus gemeinsamen Gruppen
    for row in conn.execute("""
        SELECT DISTINCT
            CASE WHEN a.user_id < b.user_id THEN a.user_id ELSE b.user_id END AS ua,
            CASE WHEN a.user_id < b.user_id THEN b.user_id ELSE a.user_id END AS ub
        FROM group_members a
        JOIN group_members b ON a.group_id = b.group_id AND a.user_id != b.user_id
    """):
        conn.execute(
            "INSERT OR IGNORE INTO user_connections (user_a, user_b) VALUES (?,?)",
            (row["ua"], row["ub"])
        )


def _migrate_blobs_to_relational(conn: sqlite3.Connection,
                                  src_table: str, archived: bool) -> None:
    """Liest JSON-Blobs aus der umbenannten Blob-Tabelle und schreibt sie
    in die neue relationale todos-Tabelle."""
    rows = conn.execute(f"SELECT user_id, data FROM {src_table}").fetchall()
    for i, row in enumerate(rows):
        try:
            t = json.loads(row["data"])
        except Exception:
            continue
        conn.execute("""
            INSERT OR IGNORE INTO todos
              (id, owner_id, title, kunde, sub, subsub, prio, umgebung,
               desc, link, recur, blocked_by, created, archived_at,
               is_archived, sort_order, extra_data)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, _todo_row(t, row["user_id"], 1 if archived else 0, i))
        _sync_checkins(conn, t, row["user_id"])




def _todo_row(t: dict, owner_id: int, is_archived: int, sort_order: int) -> tuple:
    # Felder die niemals in extra_data landen sollen (DB-Spalten + Frontend-interne Flags)
    _SKIP = set(_TODO_COLS) | {
        "id", "blocked_by", "blockedBy", "checkinHistory",
        # Delegierungs-Flags: werden bei jedem data_load neu berechnet
        "_delegated_from_tag", "_rejected_by_tag", "_rejection_comment",
        "_erledigt_von_tag", "_archived_by_tag",
    }
    extra = {k: v for k, v in t.items() if k not in _SKIP}
    s = lambda key, default="": t.get(key) or default  # None → default
    def _cap(v: str) -> str:
        """Ersten Buchstaben groß, Rest unverändert, Leerzeichen trimmen."""
        v = (v or "").strip()
        return (v[0].upper() + v[1:]) if v else ""
    return (
        s("id"),
        owner_id,
        s("title"),
        _cap(s("kunde")),
        s("sub"),
        s("subsub"),
        s("prio", "mittel"),
        s("umgebung", "intern"),
        s("desc"),
        s("link"),
        s("recur"),
        s("blocked_by") or s("blockedBy"),
        s("created"),
        t.get("archived_at"),   # darf NULL sein
        is_archived,
        sort_order,
        json.dumps(extra),
    )


def _row_to_todo(row: sqlite3.Row, checkins: list) -> dict:
    """Baut aus einer todos-Zeile + Checkin-Liste das Frontend-Todo-Dict."""
    extra = json.loads(row["extra_data"] or "{}")
    t = {
        "id":        row["id"],
        "title":     row["title"],
        "kunde":     row["kunde"],
        "sub":       row["sub"],
        "subsub":    row["subsub"],
        "prio":      row["prio"],
        "umgebung":  row["umgebung"],
        "desc":      row["desc"],
        "link":      row["link"],
        "recur":     row["recur"],
        "blockedBy": row["blocked_by"],
        "blocked":   bool(row["blocked_by"]),
        "created":   row["created"],
    }
    if row["archived_at"]:
        t["archived_at"] = row["archived_at"]
    # Checkin-Daten aus checkin_log rekonstruieren
    if checkins:
        t["checkinHistory"] = checkins
        last = checkins[-1]
        t["lastCheckin"] = last.get("date", "") + " " + last.get("time", "")
    # Extra-Felder (zeitGebucht, esk, parentTitle, etc.) zurückführen
    t.update(extra)
    return t


# ── Bulk-Daten ─────────────────────────────────────────────────────────────────
def data_load(user_id: int) -> dict:
    conn = get_conn()

    # ── Eigene aktive Todos ────────────────────────────────────────────────────
    own_todo_rows = conn.execute(
        "SELECT * FROM todos WHERE owner_id=? AND is_archived=0 AND (pool_open IS NULL OR pool_open=0) ORDER BY sort_order, created",
        (user_id,)
    ).fetchall()

    # ── Delegierte Todos (an mich, accepted, noch nicht archiviert) ────────────
    # owner_id != user_id: eigene Todos die "zurückdelegiert" wurden erscheinen
    # im normalen own_todo_rows-Block, nicht doppelt hier.
    delegated_to_me_rows = conn.execute("""
        SELECT t.*, u.user_tag AS _from_tag
        FROM   todos t
        JOIN   users u ON u.id = t.owner_id
        WHERE  t.delegated_to=? AND t.owner_id!=? AND t.delegated_status='accepted' AND t.is_archived=0
        ORDER  BY t.created
    """, (user_id, user_id)).fetchall()

    # ── Delegierte Todos die ICH abgeschlossen habe (für mein Archiv) ─────────
    delegated_done_rows = conn.execute("""
        SELECT t.*, u.user_tag AS _from_tag
        FROM   todos t
        JOIN   users u ON u.id = t.owner_id
        WHERE  t.delegated_to=? AND t.archived_by=? AND t.is_archived=1
        ORDER  BY t.archived_at DESC
    """, (user_id, user_id)).fetchall()

    # ── Eigenes Archiv (inkl. vom Delegierten erledigter Todos) ───────────────
    archiv_rows = conn.execute("""
        SELECT t.*, u.user_tag AS _archived_by_tag
        FROM   todos t
        LEFT JOIN users u ON u.id = t.archived_by
        WHERE  t.owner_id=? AND t.is_archived=1
        ORDER  BY t.archived_at DESC
    """, (user_id,)).fetchall()

    # ── Aktuelle Ablehnungen: abgelehnte Zuweisungen ohne Folge-Delegation ────
    # (todo_assignments.status='rejected', aber kein späteres pending/accepted)
    rejection_rows = conn.execute("""
        SELECT a.todo_id, u.user_tag AS by_tag, a.response AS comment
        FROM   todo_assignments a
        JOIN   users u ON u.id = a.to_user_id
        WHERE  a.from_user_id=? AND a.status='rejected'
          AND  a.id = (
              SELECT MAX(id) FROM todo_assignments
              WHERE  todo_id=a.todo_id AND from_user_id=? AND status='rejected'
          )
          AND  NOT EXISTS (
              SELECT 1 FROM todo_assignments a2
              WHERE  a2.todo_id=a.todo_id AND a2.from_user_id=?
                AND  a2.id > a.id AND a2.status IN ('pending','accepted')
          )
    """, (user_id, user_id, user_id)).fetchall()
    rejections: dict[str, dict] = {
        r["todo_id"]: {"by_tag": r["by_tag"], "comment": r["comment"] or ""}
        for r in rejection_rows
    }

    # ── Checkin-Log für alle relevanten Todos ─────────────────────────────────
    todo_ids_for_checkins = (
        [r["id"] for r in own_todo_rows] +
        [r["id"] for r in delegated_to_me_rows] +
        [r["id"] for r in delegated_done_rows] +
        [r["id"] for r in archiv_rows]
    )
    checkins_by_todo: dict[str, list] = {}
    if todo_ids_for_checkins:
        placeholders = ",".join("?" * len(todo_ids_for_checkins))
        checkin_rows = conn.execute(f"""
            SELECT c.todo_id,
                   substr(c.checked_at, 1, 10) AS date,
                   substr(c.checked_at, 12, 5) AS time,
                   c.val
            FROM   checkin_log c
            WHERE  c.todo_id IN ({placeholders})
            ORDER  BY c.todo_id, c.checked_at
        """, todo_ids_for_checkins).fetchall()
        for c in checkin_rows:
            checkins_by_todo.setdefault(c["todo_id"], []).append(
                {"date": c["date"], "time": c["time"], "val": c["val"]}
            )

    # ── Todos zusammenbauen ────────────────────────────────────────────────────
    todos = []
    for r in own_todo_rows:
        t = _row_to_todo(r, checkins_by_todo.get(r["id"], []))
        # Ablehnungs-Badge: wenn dieses Todo zuletzt abgelehnt wurde
        if t["id"] in rejections:
            rej = rejections[t["id"]]
            t["_rejected_by_tag"]    = rej["by_tag"]
            t["_rejection_comment"]  = rej["comment"]
        todos.append(t)

    # Delegierte-an-mich: als normale Todos mit Extra-Flag (voll editierbar)
    for r in delegated_to_me_rows:
        t = _row_to_todo(r, checkins_by_todo.get(r["id"], []))
        t["_delegated_from_tag"] = r["_from_tag"]
        todos.append(t)

    # ── Archiv zusammenbauen ───────────────────────────────────────────────────
    archiv = []
    for r in archiv_rows:
        t = _row_to_todo(r, checkins_by_todo.get(r["id"], []))
        archived_by_id = r["archived_by"] if "archived_by" in r.keys() else None
        if r["_archived_by_tag"] and archived_by_id != user_id:
            t["_erledigt_von_tag"] = r["_archived_by_tag"]   # anderer Nutzer hat es erledigt
        else:
            t["_erledigt_von_tag"] = None                     # selbst erledigt → None
        archiv.append(t)

    # Von mir abgeschlossene delegierte Todos: erscheinen auch in meinem Archiv
    for r in delegated_done_rows:
        t = _row_to_todo(r, checkins_by_todo.get(r["id"], []))
        t["_erledigt_von_tag"]   = None          # ich habe es erledigt
        t["_delegated_from_tag"] = r["_from_tag"]  # ursprünglich von wem
        archiv.append(t)

    berichte = [json.loads(r["data"]) for r in conn.execute(
        "SELECT data FROM berichte WHERE user_id=?", (user_id,)
    )]

    s_row    = conn.execute("SELECT data FROM settings WHERE user_id=?", (user_id,)).fetchone()
    settings = json.loads(s_row["data"]) if s_row else {}

    # ── Delegierungen: aktiv (delegated_to IS NOT NULL) + zurückgegeben (returned) ──
    # Aktive erste Hops
    active_hops = conn.execute("""
        SELECT DISTINCT a.todo_id, u.user_tag, a.status
        FROM   todo_assignments a
        JOIN   users u ON u.id = a.to_user_id
        JOIN   todos  t ON t.id = a.todo_id
        WHERE  a.from_user_id=? AND a.status IN ('pending','accepted')
          AND  t.delegated_to IS NOT NULL
    """, (user_id,)).fetchall()

    # Zurückgegebene: eigene initiierte Delegierungen die jetzt alle 'returned' sind
    returned_hops = conn.execute("""
        SELECT DISTINCT a.todo_id
        FROM   todo_assignments a
        JOIN   todos t ON t.id = a.todo_id
        WHERE  a.from_user_id=? AND a.status='returned'
          AND  t.delegated_to IS NULL
          AND  NOT EXISTS (
              SELECT 1 FROM todo_assignments a2
              WHERE  a2.todo_id=a.todo_id AND a2.status IN ('pending','accepted')
          )
    """, (user_id,)).fetchall()

    delegations = []
    seen_todo_ids = set()

    for r in active_hops:
        tid = r["todo_id"]
        seen_todo_ids.add(tid)
        chain_rows = conn.execute("""
            SELECT u.user_tag
            FROM   todo_assignments a
            JOIN   users u ON u.id = a.to_user_id
            WHERE  a.todo_id=? AND a.status IN ('pending','accepted')
            ORDER  BY a.id ASC
        """, (tid,)).fetchall()
        delegations.append({
            "todo_id":     tid,
            "to_user_tag": r["user_tag"],
            "status":      r["status"],
            "chain":       [c["user_tag"] for c in chain_rows],
        })

    for r in returned_hops:
        tid = r["todo_id"]
        if tid in seen_todo_ids:
            continue
        seen_todo_ids.add(tid)
        chain_rows = conn.execute("""
            SELECT u.user_tag
            FROM   todo_assignments a
            JOIN   users u ON u.id = a.to_user_id
            WHERE  a.todo_id=? AND a.status='returned'
            ORDER  BY a.id ASC
        """, (tid,)).fetchall()
        delegations.append({
            "todo_id":     tid,
            "to_user_tag": None,
            "status":      "returned",
            "chain":       [c["user_tag"] for c in chain_rows],
        })

    # ── Freigaben ──────────────────────────────────────────────────────────────
    shares_with_me = [
        {"owner_id": r["owner_id"], "owner_tag": r["user_tag"],
         "permission": r["permission"]}
        for r in conn.execute("""
            SELECT s.owner_id, u.user_tag, s.permission
            FROM   shares s JOIN users u ON u.id = s.owner_id
            WHERE  s.viewer_id=? AND s.status='accepted'
            ORDER  BY u.user_tag
        """, (user_id,))
    ]

    try:
        return {
            "todos":         todos,
            "archiv":        archiv,
            "berichte":      berichte,
            "kundenNotizen": settings.get("kundenNotizen", {}),
            "delegations":   delegations,
            "sharesWithMe":  shares_with_me,
        }
    finally:
        conn.close()


def _group_archive_log_add(conn: sqlite3.Connection, group_id: int, todo_id: str,
                           todo_title: str, action: str, actor_tag: str,
                           old_value: str = None, new_value: str = None,
                           todo_group_id: int = None) -> None:
    conn.execute(
        "INSERT INTO group_archive_log "
        "(group_id, todo_id, todo_title, action, actor_user_tag, old_value, new_value, todo_group_id) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (group_id, todo_id, todo_title or "", action, actor_tag, old_value, new_value, todo_group_id)
    )


def _get_group_ids_for_user(conn: sqlite3.Connection, user_id: int) -> list[int]:
    """Returns list of group_ids the user belongs to."""
    return [r["group_id"] for r in conn.execute(
        "SELECT group_id FROM group_members WHERE user_id=?", (user_id,)
    )]


def _log_for_groups(conn: sqlite3.Connection, user_id: int,
                    todo_id: str, todo_title: str, action: str, actor_tag: str,
                    old_value: str = None, new_value: str = None,
                    todo_group_id: int = None) -> None:
    """Logs an action to all groups of the given user."""
    for gid in _get_group_ids_for_user(conn, user_id):
        _group_archive_log_add(conn, gid, todo_id, todo_title, action, actor_tag, old_value, new_value, todo_group_id)


def group_archive_log_get(group_id: int, limit: int = 100) -> list[dict]:
    conn = get_conn()
    rows = conn.execute("""
        SELECT l.*, COALESCE(t.kunde, '') AS todo_kunde
        FROM   group_archive_log l
        LEFT JOIN todos t ON t.id = l.todo_id
        WHERE  l.group_id=? ORDER BY l.created DESC LIMIT ?
    """, (group_id, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def admin_all_group_logs(limit: int = 500) -> list[dict]:
    conn = get_conn()
    rows = conn.execute("""
        SELECT l.*, g.name AS group_name, COALESCE(t.kunde, '') AS todo_kunde
        FROM   group_archive_log l
        JOIN   groups g ON g.id = l.group_id
        LEFT JOIN todos t ON t.id = l.todo_id
        ORDER  BY l.created DESC LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def group_archive_todos(group_id: int) -> list[dict]:
    """Returns archived todos of group members, excluding todos assigned to other groups."""
    conn = get_conn()
    rows = conn.execute("""
        SELECT DISTINCT
               t.id, t.title, t.kunde, t.sub, t.subsub, t.prio, t.umgebung,
               t.desc, t.link, t.created, t.archived_at, t.extra_data,
               u.user_tag  AS owner_tag,
               ab.user_tag AS archived_by_tag,
               df.user_tag AS delegated_from_tag
        FROM   todos t
        JOIN   users u  ON u.id  = t.owner_id
        LEFT JOIN users ab ON ab.id = t.archived_by
        LEFT JOIN (
            SELECT a.todo_id, u2.user_tag
            FROM   todo_assignments a
            JOIN   users u2 ON u2.id = a.from_user_id
            WHERE  a.id IN (SELECT MAX(id) FROM todo_assignments GROUP BY todo_id)
        ) df ON df.todo_id = t.id
        WHERE  t.owner_id IN (
                   SELECT user_id FROM group_members WHERE group_id=?
               )
          AND  t.is_archived = 1
          AND  (t.group_id IS NULL OR t.group_id = ?)
        ORDER  BY t.archived_at DESC
    """, (group_id, group_id)).fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        extra = json.loads(d.pop("extra_data", None) or "{}")
        d["kommentare"] = extra.get("kommentare") or []
        result.append(d)
    return result


def data_save(user_id: int, todos: list, archiv: list,
              berichte: list, kunden_notizen: Any, actor_tag: str = None) -> None:
    conn = get_conn()
    try:
        # ── IDs aller Todos die mir delegiert wurden (beliebiger Status) ────────
        # Kein Status-Filter: auch 'completed' Todos dürfen nie als INSERT landen
        # (ein INSERT auf eine fremde todo.id würde UNIQUE constraint verletzen).
        delegated_ids: set[str] = {
            str(r["id"]) for r in conn.execute(
                "SELECT id FROM todos WHERE delegated_to=?",
                (user_id,)
            )
        }

        # ── Snapshot für Archiv-Log (vor dem DELETE) ──────────────────────────
        _snapshot: dict[str, dict] = {}
        for _r in conn.execute(
            "SELECT id, title, is_archived, prio, archived_at, extra_data FROM todos WHERE owner_id=?",
            (user_id,)
        ):
            _ed = {}
            try:
                _ed = json.loads(_r["extra_data"] or "{}")
            except Exception:
                pass
            _last_checkin = conn.execute(
                "SELECT val FROM checkin_log WHERE todo_id=? ORDER BY checked_at DESC LIMIT 1",
                (_r["id"],)
            ).fetchone()
            _snapshot[_r["id"]] = {
                "title":       _r["title"],
                "is_archived": _r["is_archived"],
                "prio":        _r["prio"],
                "archived_at": _r["archived_at"],
                "esk":         _ed.get("esk"),
                "komm_count":  len(_ed.get("kommentare", []) or []),
                "last_checkin_val": _last_checkin["val"] if _last_checkin else None,
            }

        # ── Pool-State sichern ─────────────────────────────────────────────────
        # pool_open=1 → Todo steht im Pool, darf vom DELETE nicht berührt werden.
        # pool_open=0 mit group_id → vom Pool genommenes Todo, group_id muss erhalten bleiben.
        _pool_state: dict[str, tuple] = {
            r["id"]: (r["group_id"], r["pool_open"])
            for r in conn.execute(
                "SELECT id, group_id, pool_open FROM todos WHERE owner_id=?",
                (user_id,)
            )
            if r["group_id"] is not None or r["pool_open"]
        }
        # IDs die im Pool stehen (pool_open=1) → überleben den DELETE unverändert
        _pool_open_ids: set[str] = {tid for tid, (_, po) in _pool_state.items() if po}

        # ── Delegations-State meiner eigenen Todos sichern ────────────────────
        # owner_id=ich, delegated_to gesetzt → Besitzer hat Todo an anderen Nutzer gesendet.
        # Diese Todos überleben den DELETE unberührt (wie pool_open-Todos), damit
        # kein delegierter State verloren geht auch wenn das Todo nicht im Payload ist.
        own_delegated_ids: set[str] = {
            str(r["id"]) for r in conn.execute(
                "SELECT id FROM todos WHERE owner_id=? AND delegated_to IS NOT NULL",
                (user_id,)
            )
        }

        # ── Eigene Todos: DELETE + reinsert (berührt keine shared rows) ────────
        # Archivierte Todos die jünger als 365 Tage sind dürfen nicht gelöscht werden.
        # Pool-Todos (pool_open=1) bleiben ebenfalls erhalten – sie werden vom Pool-System verwaltet.
        # Eigene delegierte Todos (delegated_to IS NOT NULL) bleiben ebenfalls erhalten.
        _locked_ids: set[str] = {
            str(r["id"]) for r in conn.execute(
                "SELECT id FROM todos WHERE owner_id=? AND is_archived=1 "
                "AND archived_at IS NOT NULL "
                "AND datetime('now') < datetime(archived_at, '+365 days')",
                (user_id,)
            )
        }
        # Payload-IDs: locked todos die im Payload sind, werden normal re-inserted
        _payload_all_ids = {str(t.get("id","")) for t in (todos + archiv) if t.get("id")}
        _truly_locked = (_locked_ids - _payload_all_ids) | _pool_open_ids | own_delegated_ids
        if _truly_locked:
            _ph = ",".join("?" * len(_truly_locked))
            conn.execute(f"DELETE FROM todos WHERE owner_id=? AND id NOT IN ({_ph})",
                         (user_id,) + tuple(_truly_locked))
        else:
            conn.execute("DELETE FROM todos WHERE owner_id=?", (user_id,))

        _INSERT_TODO = """
            INSERT INTO todos
              (id, owner_id, title, kunde, sub, subsub, prio, umgebung,
               desc, link, recur, blocked_by, created, archived_at,
               is_archived, sort_order, extra_data)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """
        for i, t in enumerate(todos):
            tid = str(t.get("id", ""))
            if tid not in delegated_ids and tid not in _pool_open_ids and tid not in own_delegated_ids:
                conn.execute(_INSERT_TODO, _todo_row(t, user_id, 0, i))
                _sync_checkins(conn, t, user_id)

        for i, t in enumerate(archiv):
            tid = str(t.get("id", ""))
            if tid not in delegated_ids and tid not in _pool_open_ids and tid not in own_delegated_ids:
                conn.execute(_INSERT_TODO, _todo_row(t, user_id, 1, i))
                _sync_checkins(conn, t, user_id)

        # ── Eigene delegierte Todos: Payload-Felder übernehmen falls vorhanden ──
        # Das Todo wurde nicht gelöscht+re-inserted, daher nur editierbare Felder updaten.
        for t in todos:
            tid = str(t.get("id", ""))
            if tid in own_delegated_ids:
                s = lambda key, default="": t.get(key) or default
                _SKIP_DEL = set(_TODO_COLS) | {
                    "id", "blocked_by", "blockedBy", "checkinHistory",
                    "_delegated_from_tag", "_rejected_by_tag", "_rejection_comment",
                    "_erledigt_von_tag", "_archived_by_tag",
                }
                extra = {k: v for k, v in t.items() if k not in _SKIP_DEL}
                conn.execute(
                    "UPDATE todos SET title=?, kunde=?, sub=?, subsub=?, prio=?, "
                    "umgebung=?, desc=?, link=?, recur=?, blocked_by=?, extra_data=?, sort_order=? "
                    "WHERE id=? AND owner_id=?",
                    (s("title"), s("kunde"), s("sub"), s("subsub"),
                     s("prio", "mittel"), s("umgebung", "intern"),
                     s("desc"), s("link"), s("recur"),
                     s("blocked_by") or s("blockedBy"),
                     json.dumps(extra), todos.index(t), tid, user_id)
                )
                _sync_checkins(conn, t, user_id)

        # ── group_id wiederherstellen (nur für pool_open=0 Todos mit group_id) ──
        # pool_open=1 Todos wurden nicht gelöscht, brauchen kein Restore.
        for tid, (gid, pool_open) in _pool_state.items():
            if not pool_open:
                conn.execute(
                    "UPDATE todos SET group_id=?, pool_open=0 WHERE id=? AND owner_id=?",
                    (gid, tid, user_id)
                )

        # ── Delegierte Todos updaten (shared rows, owner != ich) ───────────────
        _UPDATE_DELEGATED = """
            UPDATE todos SET
              title=?, kunde=?, sub=?, subsub=?, prio=?, umgebung=?,
              desc=?, link=?, recur=?, blocked_by=?, extra_data=?
            WHERE id=? AND delegated_to=? AND delegated_status='accepted'
        """
        _now = datetime.utcnow().isoformat(timespec="seconds")
        for t in todos:
            tid = str(t.get("id", ""))
            if tid in delegated_ids:
                s = lambda key, default="": t.get(key) or default
                _SKIP2 = set(_TODO_COLS) | {
                    "id", "blocked_by", "blockedBy", "checkinHistory",
                    "_delegated_from_tag", "_rejected_by_tag", "_rejection_comment",
                    "_erledigt_von_tag", "_archived_by_tag",
                }
                extra = {k: v for k, v in t.items() if k not in _SKIP2}
                conn.execute(_UPDATE_DELEGATED, (
                    s("title"), s("kunde"), s("sub"), s("subsub"),
                    s("prio", "mittel"), s("umgebung", "intern"),
                    s("desc"), s("link"), s("recur"),
                    s("blocked_by") or s("blockedBy"),
                    json.dumps(extra), tid, user_id,
                ))
                _sync_checkins(conn, t, user_id)

        # Delegiertes Todo archiviert → als abgeschlossen markieren
        for t in archiv:
            tid = str(t.get("id", ""))
            if tid in delegated_ids:
                conn.execute(
                    "UPDATE todos SET is_archived=1, archived_at=?, archived_by=?, "
                    "delegated_status='completed' "
                    "WHERE id=? AND delegated_to=?",
                    (_now, user_id, tid, user_id)
                )
                # Assignment ebenfalls als completed markieren
                conn.execute(
                    "UPDATE todo_assignments SET status='completed', updated=? "
                    "WHERE todo_id=? AND to_user_id=? AND status='accepted'",
                    (_now, tid, user_id)
                )

        # ── Berichte ──────────────────────────────────────────────────────────
        conn.execute("DELETE FROM berichte WHERE user_id=?", (user_id,))
        conn.executemany(
            "INSERT INTO berichte (id, user_id, data) VALUES (?,?,?)",
            [(b["id"], user_id, json.dumps(b)) for b in berichte]
        )

        # ── Einstellungen ─────────────────────────────────────────────────────
        conn.execute(
            "INSERT INTO settings (user_id, data) VALUES (?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET data=excluded.data",
            (user_id, json.dumps({"kundenNotizen": kunden_notizen})),
        )

        # ── Archiv-Log: Diff und Logging ──────────────────────────────────────
        _group_ids = [r["group_id"] for r in conn.execute(
            "SELECT group_id FROM group_members WHERE user_id=?", (user_id,)
        )]
        if _group_ids:
            _row = conn.execute("SELECT user_tag FROM users WHERE id=?", (user_id,)).fetchone()
            _tag = actor_tag or (_row["user_tag"] if _row else "?")

            _new_active = {str(t.get("id", "")): t for t in todos if t.get("id")}
            _new_archiv = {str(t.get("id", "")): t for t in archiv if t.get("id")}
            _new_all = {**_new_active, **_new_archiv}

            _old_ids = set(_snapshot.keys())
            _new_ids = set(_new_all.keys()) - delegated_ids

            for _tid in (_new_ids - _old_ids):
                _t = _new_all[_tid]
                _tgid = _pool_state.get(_tid, (None, None))[0]
                for _gid in _group_ids:
                    _group_archive_log_add(conn, _gid, _tid, _t.get("title"), "created", _tag, todo_group_id=_tgid)

            for _tid in (_old_ids - _new_ids):
                _old = _snapshot[_tid]
                _tgid = _pool_state.get(_tid, (None, None))[0]
                for _gid in _group_ids:
                    _group_archive_log_add(conn, _gid, _tid, _old["title"], "deleted", _tag, todo_group_id=_tgid)

            for _tid in (_old_ids & _new_ids):
                _old = _snapshot[_tid]
                _t = _new_all[_tid]
                _tgid = _pool_state.get(_tid, (None, None))[0]
                _was_archived = _old["is_archived"]
                _is_archived = 1 if _tid in _new_archiv else 0
                if _was_archived == 0 and _is_archived == 1:
                    for _gid in _group_ids:
                        _group_archive_log_add(conn, _gid, _tid, _t.get("title"), "completed", _tag, todo_group_id=_tgid)
                elif _old["prio"] != _t.get("prio", _old["prio"]):
                    for _gid in _group_ids:
                        _group_archive_log_add(conn, _gid, _tid, _t.get("title"), "status_changed",
                                               _tag, _old["prio"], _t.get("prio"), todo_group_id=_tgid)
                # Checkin geändert
                _new_checkins = _t.get("checkinHistory") or []
                _new_last_val = _new_checkins[-1].get("val") if _new_checkins else None
                if _new_last_val and _new_last_val != _old.get("last_checkin_val"):
                    for _gid in _group_ids:
                        _group_archive_log_add(conn, _gid, _tid, _t.get("title"), "checkin_changed",
                                               _tag, _old.get("last_checkin_val") or "offen", _new_last_val,
                                               todo_group_id=_tgid)
                # Kommentar hinzugefügt
                _new_ed = {}
                try:
                    _new_ed = json.loads(_t.get("extra_data") or "{}")
                except Exception:
                    _new_ed = {k: v for k, v in _t.items() if k not in set(_TODO_COLS)}
                _new_komm_count = len(_new_ed.get("kommentare", []) or [])
                if _new_komm_count > _old.get("komm_count", 0):
                    _new_komm = (_new_ed.get("kommentare") or [])[-1]
                    _komm_text = str(_new_komm.get("text",""))[:50] if isinstance(_new_komm, dict) else str(_new_komm)[:50]
                    for _gid in _group_ids:
                        _group_archive_log_add(conn, _gid, _tid, _t.get("title"), "comment_added",
                                               _tag, new_value=_komm_text, todo_group_id=_tgid)
                # Eskalation
                _new_esk = _t.get("esk") or _new_ed.get("esk")
                _old_esk = _old.get("esk")
                if _new_esk and _new_esk != _old_esk:
                    for _gid in _group_ids:
                        _group_archive_log_add(conn, _gid, _tid, _t.get("title"), "escalated",
                                               _tag, old_value=str(_old_esk or ""), new_value=str(_new_esk),
                                               todo_group_id=_tgid)

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _sync_checkins(conn: sqlite3.Connection, t: dict, user_id: int) -> None:
    checkins = t.get("checkinHistory") or []
    if not checkins:
        return
    conn.execute("DELETE FROM checkin_log WHERE todo_id=? AND user_id=?",
                 (t["id"], user_id))
    conn.executemany(
        "INSERT INTO checkin_log (todo_id, user_id, checked_at, val) VALUES (?,?,?,?)",
        [(t["id"], user_id,
          c.get("date", "") + "T" + c.get("time", "00:00"),
          c.get("val", "")) for c in checkins if isinstance(c, dict)]
    )


def data_load_for_viewer(owner_id: int, viewer_id: int) -> Optional[dict]:
    conn = get_conn()
    share = conn.execute(
        "SELECT permission FROM shares WHERE owner_id=? AND viewer_id=? AND status='accepted'",
        (owner_id, viewer_id),
    ).fetchone()
    conn.close()
    if not share:
        return None
    data = data_load(owner_id)
    data["permission"] = share["permission"]
    return data


def data_save_for_writer(owner_id: int, writer_id: int,
                         todos: list, archiv: list,
                         berichte: list, kunden_notizen: Any) -> bool:
    conn = get_conn()
    share = conn.execute(
        "SELECT permission FROM shares WHERE owner_id=? AND viewer_id=? AND permission='write' AND status='accepted'",
        (owner_id, writer_id),
    ).fetchone()
    conn.close()
    if not share:
        return False
    data_save(owner_id, todos, archiv, berichte, kunden_notizen)
    return True


# ── Posteingang ────────────────────────────────────────────────────────────────
def inbox_insert(inbox_id: str, from_id: int, to_id: int,
                 original_todo_id: str, todo_data: str, comment: Optional[str]) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT INTO inbox (id, from_user_id, to_user_id, original_todo_id, "
        "todo_data, assign_comment) VALUES (?,?,?,?,?,?)",
        (inbox_id, from_id, to_id, original_todo_id, todo_data, comment),
    )
    conn.commit()
    conn.close()


def inbox_list(to_user_id: int) -> list[dict]:
    conn = get_conn()
    rows = conn.execute("""
        SELECT i.id, i.todo_data, i.assign_comment, i.created,
               u.user_tag AS from_user_tag
        FROM   inbox i
        JOIN   users u ON u.id = i.from_user_id
        WHERE  i.to_user_id=? AND i.status='pending'
        ORDER  BY i.created DESC
    """, (to_user_id,)).fetchall()
    conn.close()
    return [{"id": r["id"], "todo": json.loads(r["todo_data"]),
             "comment": r["assign_comment"],
             "from_user_tag": r["from_user_tag"],
             "created": r["created"]} for r in rows]


def inbox_count(to_user_id: int) -> int:
    conn = get_conn()
    n = conn.execute(
        "SELECT COUNT(*) FROM inbox WHERE to_user_id=? AND status='pending'",
        (to_user_id,),
    ).fetchone()[0]
    conn.close()
    return n


def inbox_pending(inbox_id: str, to_user_id: int):
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM inbox WHERE id=? AND to_user_id=? AND status='pending'",
        (inbox_id, to_user_id),
    ).fetchone()
    conn.close()
    return row


def inbox_set_status(inbox_id: str, status: str, comment: Optional[str]) -> None:
    conn = get_conn()
    conn.execute("UPDATE inbox SET status=?, response_comment=? WHERE id=?",
                 (status, comment, inbox_id))
    conn.commit()
    conn.close()


def inbox_add_todo(user_id: int, todo: dict) -> None:
    """Fügt ein angenommenes Inbox-Todo dem User hinzu."""
    conn = get_conn()
    count = conn.execute(
        "SELECT COUNT(*) FROM todos WHERE owner_id=? AND is_archived=0",
        (user_id,)
    ).fetchone()[0]
    conn.execute("""
        INSERT INTO todos
          (id, owner_id, title, kunde, sub, subsub, prio, umgebung,
           desc, link, recur, blocked_by, created, archived_at,
           is_archived, sort_order, extra_data)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
          owner_id=excluded.owner_id, title=excluded.title,
          is_archived=0, sort_order=excluded.sort_order,
          extra_data=excluded.extra_data
    """, _todo_row(todo, user_id, 0, count))
    _sync_checkins(conn, todo, user_id)
    conn.commit()
    conn.close()


# ── Benutzer ───────────────────────────────────────────────────────────────────
def _assign_discriminator(conn: sqlite3.Connection, username: str) -> int:
    used = {r[0] for r in conn.execute(
        "SELECT discriminator FROM users WHERE username=?", (username,)
    )}
    available = list(set(range(1, 10000)) - used)
    if not available:
        raise ValueError(f"Alle Diskriminatoren für '{username}' vergeben")
    return random.choice(available)


def user_create(username: str, pw_hash: str) -> dict:
    conn = get_conn()
    try:
        disc  = _assign_discriminator(conn, username)
        tag   = f"{username}#{disc:04d}"
        count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        role  = "admin" if count == 0 else "member"
        conn.execute(
            "INSERT INTO users (username, discriminator, user_tag, pw_hash, role) "
            "VALUES (?,?,?,?,?)",
            (username, disc, tag, pw_hash, role),
        )
        conn.commit()
        return dict(conn.execute("SELECT * FROM users WHERE user_tag=?", (tag,)).fetchone())
    finally:
        conn.close()


def repair_user_tag(user_id: int, username: str) -> str:
    conn = get_conn()
    try:
        disc = _assign_discriminator(conn, username)
        tag  = f"{username}#{disc:04d}"
        conn.execute("UPDATE users SET discriminator=?, user_tag=? WHERE id=?",
                     (disc, tag, user_id))
        conn.commit()
        return tag
    finally:
        conn.close()


def user_by_tag(tag: str):
    conn = get_conn()
    if "#" in tag:
        row = conn.execute("SELECT * FROM users WHERE user_tag=?", (tag,)).fetchone()
    else:
        rows = conn.execute("SELECT * FROM users WHERE username=?", (tag,)).fetchall()
        row  = rows[0] if len(rows) == 1 else None
    conn.close()
    return row


def user_by_id(user_id: int):
    conn = get_conn()
    row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    conn.close()
    return row


def users_list(exclude_id: int, same_group_only: bool = False,
               current_user_id: Optional[int] = None) -> list[dict]:
    conn = get_conn()
    if same_group_only and current_user_id is not None:
        # Verbundene User + alle System-Admins (auch ohne Verbindung sichtbar)
        rows = conn.execute("""
            SELECT DISTINCT u.id, u.username, u.discriminator, u.user_tag, u.role
            FROM   users u
            WHERE  u.id != ?
              AND  u.status = 'active'
              AND (
                u.role = 'admin'
                OR u.id IN (SELECT user_b FROM user_connections WHERE user_a=?)
                OR u.id IN (SELECT user_a FROM user_connections WHERE user_b=?)
              )
            ORDER BY u.user_tag
        """, (exclude_id, current_user_id, current_user_id)).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, username, discriminator, user_tag, role FROM users "
            "WHERE id != ? AND status='active' ORDER BY user_tag",
            (exclude_id,),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def user_set_role(user_id: int, role: str) -> None:
    conn = get_conn()
    conn.execute("UPDATE users SET role=? WHERE id=?", (role, user_id))
    conn.commit()
    conn.close()


def user_set_pw(user_id: int, pw_hash: str) -> None:
    conn = get_conn()
    conn.execute("UPDATE users SET pw_hash=? WHERE id=?", (pw_hash, user_id))
    conn.commit()
    conn.close()


def user_deactivate(user_id: int, group_admin_id: int, group_admin_tag: str) -> None:
    """
    Deactivates a user:
    1. Sets status='inactive'
    2. Delegates all open todos of deactivated user to the group admin
    3. Clears all incoming delegations to the deactivated user (returns to owners)
    """
    conn = get_conn()
    conn.execute(
        "UPDATE users SET status='inactive', deactivated_at=datetime('now') WHERE id=?",
        (user_id,)
    )

    comment = f"Automatisch delegiert – User deaktiviert von {group_admin_tag}"

    # ── Step 2: Delegate all open todos of deactivated user to group admin ─────
    open_todos = conn.execute(
        "SELECT id FROM todos WHERE owner_id=? AND is_archived=0 AND delegated_to IS NULL",
        (user_id,)
    ).fetchall()
    for t in open_todos:
        todo_id = t["id"]
        conn.execute(
            "INSERT INTO todo_assignments (todo_id, from_user_id, to_user_id, comment, status) "
            "VALUES (?,?,?,?,'pending')",
            (todo_id, user_id, group_admin_id, comment)
        )
        conn.execute(
            "UPDATE todos SET delegated_to=?, delegated_status='pending' WHERE id=?",
            (group_admin_id, todo_id)
        )

    # ── Step 3: Clear incoming delegations to deactivated user ────────────────
    incoming = conn.execute(
        "SELECT id, todo_id FROM todo_assignments WHERE to_user_id=? AND status IN ('pending','accepted')",
        (user_id,)
    ).fetchall()
    for a in incoming:
        conn.execute(
            "UPDATE todo_assignments SET status='returned', updated=datetime('now') WHERE id=?",
            (a["id"],)
        )
        conn.execute(
            "UPDATE todos SET delegated_to=NULL, delegated_status=NULL WHERE id=?",
            (a["todo_id"],)
        )

    conn.commit()
    conn.close()


def user_activate(user_id: int) -> None:
    conn = get_conn()
    conn.execute("UPDATE users SET status='active' WHERE id=?", (user_id,))
    conn.commit()
    conn.close()


def user_is_active(user_id: int) -> bool:
    conn = get_conn()
    row = conn.execute("SELECT status FROM users WHERE id=?", (user_id,)).fetchone()
    conn.close()
    return row is not None and row["status"] == "active"


def users_all() -> list[dict]:
    conn = get_conn()
    users = [dict(r) for r in conn.execute(
        "SELECT id, username, discriminator, user_tag, role, status, created "
        "FROM users ORDER BY user_tag"
    )]
    for u in users:
        u["groups"] = [dict(r) for r in conn.execute("""
            SELECT g.id, g.name FROM groups g
            JOIN group_members gm ON gm.group_id = g.id
            WHERE gm.user_id=?
        """, (u["id"],))]
    conn.close()
    return users


def user_count() -> int:
    conn = get_conn()
    count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    conn.close()
    return count


# ── Gruppen ────────────────────────────────────────────────────────────────────
def groups_list_all() -> list[dict]:
    conn = get_conn()
    groups = conn.execute("SELECT * FROM groups ORDER BY name").fetchall()
    result = []
    for g in groups:
        members = conn.execute("""
            SELECT u.id, u.user_tag, u.status, gm.role, gm.can_post_to_pool
            FROM group_members gm JOIN users u ON u.id=gm.user_id
            WHERE gm.group_id=? ORDER BY gm.role DESC, u.user_tag
        """, (g["id"],)).fetchall()
        d = dict(g)
        d["members"] = [dict(m) for m in members]
        result.append(d)
    conn.close()
    return result


def group_create(name: str, created_by: int, auto_join: bool = True) -> dict:
    conn = get_conn()
    conn.execute("INSERT INTO groups (name, created_by) VALUES (?,?)", (name, created_by))
    group_id = conn.execute("SELECT id FROM groups WHERE name=?", (name,)).fetchone()["id"]
    if auto_join:
        # Creator becomes group admin automatically (only for non-system-admins)
        conn.execute(
            "INSERT OR IGNORE INTO group_members (group_id, user_id, role) VALUES (?,?,'admin')",
            (group_id, created_by)
        )
    conn.commit()
    row = conn.execute("SELECT * FROM groups WHERE id=?", (group_id,)).fetchone()
    conn.close()
    return dict(row)


def group_delete(group_id: int) -> None:
    conn = get_conn()
    conn.execute("DELETE FROM groups WHERE id=?", (group_id,))
    conn.commit()
    conn.close()


def group_add_member(group_id: int, user_id: int, role: str = "member") -> None:
    conn = get_conn()
    conn.execute("INSERT OR IGNORE INTO group_members (group_id, user_id, role) VALUES (?,?,?)",
                 (group_id, user_id, role))
    # Neue Verbindungen zu allen bisherigen Gruppenmitgliedern anlegen
    others = conn.execute(
        "SELECT user_id FROM group_members WHERE group_id=? AND user_id!=?",
        (group_id, user_id)
    ).fetchall()
    for other in others:
        _connection_ensure(conn, user_id, other["user_id"])
    conn.commit()
    conn.close()


def group_remove_member(group_id: int, user_id: int) -> None:
    conn = get_conn()
    # Mitglieder VOR dem Löschen holen
    others = conn.execute(
        "SELECT user_id FROM group_members WHERE group_id=? AND user_id!=?",
        (group_id, user_id)
    ).fetchall()
    conn.execute("DELETE FROM group_members WHERE group_id=? AND user_id=?",
                 (group_id, user_id))
    # Für jeden ehemaligen Mitstreiter prüfen ob noch eine andere Verbindung besteht
    for other in others:
        _connection_cleanup(conn, user_id, other["user_id"])
    conn.commit()
    conn.close()


def user_groups(user_id: int) -> list[dict]:
    conn = get_conn()
    rows = conn.execute("""
        SELECT g.id, g.name FROM groups g
        JOIN group_members gm ON gm.group_id = g.id
        WHERE gm.user_id=? ORDER BY g.name
    """, (user_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def group_get_member_role(group_id: int, user_id: int) -> Optional[str]:
    """Returns 'admin'/'member' if user is in group, None if not."""
    conn = get_conn()
    row = conn.execute(
        "SELECT role FROM group_members WHERE group_id=? AND user_id=?",
        (group_id, user_id)
    ).fetchone()
    conn.close()
    return row["role"] if row else None


def group_set_member_role(group_id: int, user_id: int, role: str) -> bool:
    conn = get_conn()
    cur = conn.execute(
        "UPDATE group_members SET role=? WHERE group_id=? AND user_id=?",
        (role, group_id, user_id)
    )
    conn.commit()
    conn.close()
    return cur.rowcount > 0


def group_rename(group_id: int, name: str) -> bool:
    conn = get_conn()
    try:
        cur = conn.execute("UPDATE groups SET name=? WHERE id=?", (name, group_id))
        conn.commit()
        return cur.rowcount > 0
    except Exception:
        return False
    finally:
        conn.close()


def user_member_groups(user_id: int) -> list[dict]:
    """All groups the user belongs to (any role), with active members + own pool permission."""
    conn = get_conn()
    rows = conn.execute("""
        SELECT g.id, g.name, gm.role, gm.can_post_to_pool
        FROM   groups g
        JOIN   group_members gm ON gm.group_id = g.id
        WHERE  gm.user_id=?
        ORDER  BY g.name
    """, (user_id,)).fetchall()
    result = []
    for r in rows:
        g = dict(r)
        members = conn.execute("""
            SELECT u.id, u.user_tag, gm2.role, gm2.can_post_to_pool
            FROM   group_members gm2
            JOIN   users u ON u.id = gm2.user_id
            WHERE  gm2.group_id=? AND u.status='active'
            ORDER  BY gm2.role DESC, u.user_tag
        """, (g["id"],)).fetchall()
        g["members"] = [dict(m) for m in members]
        result.append(g)
    conn.close()
    return result


def user_delete(user_id: int) -> None:
    """Archive all active todos, remove all FK references, then delete user."""
    conn = get_conn()
    try:
        now = datetime.utcnow().isoformat(timespec="seconds")

        # 1. Archive all active todos owned by user (owner_id kept for archive history)
        conn.execute(
            "UPDATE todos SET is_archived=1, archived_at=? WHERE owner_id=? AND is_archived=0",
            (now, user_id),
        )

        # 2. Nullify delegated_to on todos where this user is the delegate
        conn.execute(
            "UPDATE todos SET delegated_to=NULL, delegated_status=NULL WHERE delegated_to=?",
            (user_id,),
        )

        # 3. All assignment rows (any status) — sender or receiver
        conn.execute(
            "DELETE FROM todo_assignments WHERE from_user_id=? OR to_user_id=?",
            (user_id, user_id),
        )

        # 4. Shares
        conn.execute(
            "DELETE FROM shares WHERE owner_id=? OR viewer_id=?",
            (user_id, user_id),
        )

        # 5. Group membership
        conn.execute("DELETE FROM group_members WHERE user_id=?", (user_id,))

        # 6. Invites created by this user
        conn.execute("DELETE FROM invites WHERE created_by=?", (user_id,))

        # 7. User connections (if table exists)
        conn.execute(
            "DELETE FROM user_connections WHERE user_a=? OR user_b=?",
            (user_id, user_id),
        )

        # 8. Inbox rows referencing this user
        conn.execute(
            "DELETE FROM inbox WHERE from_user_id=? OR to_user_id=?",
            (user_id, user_id),
        )

        # 9. Delete user — group_archive_log.actor_user_tag is TEXT, no FK constraint
        conn.execute("DELETE FROM users WHERE id=?", (user_id,))

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def user_admin_groups(user_id: int) -> list[dict]:
    """Groups where user has role='admin'."""
    conn = get_conn()
    rows = conn.execute("""
        SELECT g.id, g.name, gm.role
        FROM   groups g
        JOIN   group_members gm ON gm.group_id = g.id
        WHERE  gm.user_id=? AND gm.role='admin'
        ORDER  BY g.name
    """, (user_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ══════════════════════════════════════════════════════════════════════════════
# Pool (Schwarzes Brett)
# ══════════════════════════════════════════════════════════════════════════════

def pool_can_post(group_id: int, user_id: int) -> bool:
    """True wenn user Gruppen-Admin ist ODER can_post_to_pool=1 hat."""
    conn = get_conn()
    row = conn.execute(
        "SELECT role, can_post_to_pool FROM group_members WHERE group_id=? AND user_id=?",
        (group_id, user_id),
    ).fetchone()
    conn.close()
    if not row:
        return False
    return row["role"] == "admin" or bool(row["can_post_to_pool"])


def pool_create(group_id: int, creator_id: int, title: str, kunde: str = "",
                sub: str = "", subsub: str = "", prio: str = "mittel",
                desc: str = "", link: str = "") -> dict:
    import uuid
    conn = get_conn()
    try:
        todo_id = str(uuid.uuid4())
        now = datetime.utcnow().isoformat(timespec="seconds")
        _creator_row = conn.execute("SELECT user_tag FROM users WHERE id=?", (creator_id,)).fetchone()
        creator_tag = _creator_row["user_tag"] if _creator_row else "?"
        conn.execute("""
            INSERT INTO todos (id, owner_id, title, kunde, sub, subsub, prio, desc, link,
                               created, group_id, pool_open)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,1)
        """, (todo_id, creator_id, title, kunde, sub, subsub, prio, desc, link, now, group_id))
        _group_archive_log_add(conn, group_id, todo_id, title, "pool_created", creator_tag, todo_group_id=group_id)
        conn.commit()
        row = conn.execute("SELECT * FROM todos WHERE id=?", (todo_id,)).fetchone()
        return dict(row)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def pool_list(group_id: int) -> list[dict]:
    """Alle offenen (noch nicht genommenen) Pool-Todos einer Gruppe."""
    conn = get_conn()
    try:
        rows = conn.execute("""
            SELECT t.*, u.user_tag AS creator_tag
            FROM   todos t
            JOIN   users u ON u.id = t.owner_id
            WHERE  t.group_id=? AND t.pool_open=1 AND t.is_archived=0
            ORDER  BY t.created DESC
        """, (group_id,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def pool_take(group_id: int, todo_id: str, user_id: int) -> dict:
    """Nimmt ein Pool-Todo: owner_id → user_id, pool_open → 0."""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM todos WHERE id=? AND group_id=? AND pool_open=1",
            (todo_id, group_id),
        ).fetchone()
        if not row:
            raise ValueError("Todo nicht gefunden oder bereits genommen")
        _taker_row = conn.execute("SELECT user_tag FROM users WHERE id=?", (user_id,)).fetchone()
        taker_tag = _taker_row["user_tag"] if _taker_row else "?"
        conn.execute(
            "UPDATE todos SET owner_id=?, pool_open=0 WHERE id=?",
            (user_id, todo_id),
        )
        _group_archive_log_add(conn, group_id, todo_id, dict(row)["title"],
                               "pool_taken", taker_tag, "offen", taker_tag, todo_group_id=group_id)
        conn.commit()
        updated = conn.execute("SELECT * FROM todos WHERE id=?", (todo_id,)).fetchone()
        return dict(updated)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def todo_send_to_pool(todo_id: str, group_id: int, user_id: int) -> dict:
    """Verschiebt ein Todo in den Pool: der User muss Besitzer ODER delegated_to-Empfänger sein."""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM todos WHERE id=? AND is_archived=0 AND pool_open=0 "
            "AND (owner_id=? OR delegated_to=?)",
            (todo_id, user_id, user_id),
        ).fetchone()
        if not row:
            raise ValueError("Todo nicht gefunden oder bereits im Pool")
        actor_row = conn.execute("SELECT user_tag FROM users WHERE id=?", (user_id,)).fetchone()
        actor_tag = actor_row["user_tag"] if actor_row else "?"
        conn.execute(
            "UPDATE todos SET group_id=?, pool_open=1 WHERE id=?",
            (group_id, todo_id),
        )
        _group_archive_log_add(conn, group_id, todo_id, dict(row)["title"],
                               "pool_created", actor_tag, todo_group_id=group_id)
        conn.commit()
        updated = conn.execute("SELECT * FROM todos WHERE id=?", (todo_id,)).fetchone()
        return dict(updated)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def pool_set_permission(group_id: int, user_id: int, can_post: bool) -> bool:
    conn = get_conn()
    cur = conn.execute(
        "UPDATE group_members SET can_post_to_pool=? WHERE group_id=? AND user_id=?",
        (1 if can_post else 0, group_id, user_id),
    )
    conn.commit()
    conn.close()
    return cur.rowcount > 0


def group_list_members(group_id: int) -> list[dict]:
    conn = get_conn()
    rows = conn.execute("""
        SELECT u.id, u.user_tag, u.username, u.status, gm.role, gm.can_post_to_pool
        FROM   group_members gm
        JOIN   users u ON u.id = gm.user_id
        WHERE  gm.group_id=?
        ORDER  BY gm.role DESC, u.user_tag
    """, (group_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def invite_create_for_group(token: str, created_by: int, group_id: int) -> dict:
    conn = get_conn()
    conn.execute(
        "INSERT INTO invites (token, created_by, group_id) VALUES (?,?,?)",
        (token, created_by, group_id)
    )
    conn.commit()
    row = conn.execute("SELECT * FROM invites WHERE token=?", (token,)).fetchone()
    conn.close()
    return dict(row)


def _canonical(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a < b else (b, a)


def _connection_ensure(conn: sqlite3.Connection, a: int, b: int) -> None:
    """Stellt sicher dass ein user_connections-Eintrag für (a,b) existiert."""
    ua, ub = _canonical(a, b)
    conn.execute(
        "INSERT OR IGNORE INTO user_connections (user_a, user_b) VALUES (?,?)", (ua, ub)
    )


def _connection_cleanup(conn: sqlite3.Connection, a: int, b: int) -> None:
    """Löscht den Eintrag aus user_connections wenn keine Verbindung mehr besteht."""
    ua, ub = _canonical(a, b)
    still_connected = conn.execute("""
        SELECT 1 WHERE
          EXISTS (
            SELECT 1 FROM shares
             WHERE (owner_id=? AND viewer_id=?) OR (owner_id=? AND viewer_id=?)
          )
          OR EXISTS (
            SELECT 1 FROM group_members x
            JOIN   group_members y ON x.group_id = y.group_id
             WHERE x.user_id=? AND y.user_id=?
          )
    """, (a, b, b, a, a, b)).fetchone()
    if not still_connected:
        conn.execute(
            "DELETE FROM user_connections WHERE user_a=? AND user_b=?", (ua, ub)
        )


def same_group(user_a: int, user_b: int) -> bool:
    conn = get_conn()
    row = conn.execute("""
        SELECT 1 FROM group_members a
        JOIN group_members b ON a.group_id = b.group_id
        WHERE a.user_id=? AND b.user_id=? LIMIT 1
    """, (user_a, user_b)).fetchone()
    conn.close()
    return row is not None


def users_connected(user_a: int, user_b: int) -> bool:
    """True wenn in user_connections (gleiche Gruppe ODER aktive Freigabe)."""
    ua, ub = _canonical(user_a, user_b)
    conn = get_conn()
    row = conn.execute(
        "SELECT 1 FROM user_connections WHERE user_a=? AND user_b=?", (ua, ub)
    ).fetchone()
    conn.close()
    return row is not None


# ── Freigaben ──────────────────────────────────────────────────────────────────
def shares_by_owner(owner_id: int) -> list[dict]:
    conn = get_conn()
    rows = conn.execute("""
        SELECT s.id, s.permission, s.status, s.created, u.user_tag AS viewer_tag
        FROM   shares s JOIN users u ON u.id = s.viewer_id
        WHERE  s.owner_id=? ORDER BY u.user_tag
    """, (owner_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def share_create(owner_id: int, viewer_id: int, permission: str) -> dict:
    conn = get_conn()
    # In gleicher Gruppe → sofort akzeptiert; sonst pending
    in_group = same_group(owner_id, viewer_id)
    status = "accepted" if in_group else "pending"
    conn.execute(
        "INSERT INTO shares (owner_id, viewer_id, permission, status) VALUES (?,?,?,?) "
        "ON CONFLICT(owner_id, viewer_id) DO UPDATE SET "
        "  permission=excluded.permission, status=excluded.status",
        (owner_id, viewer_id, permission, status),
    )
    if in_group:
        _connection_ensure(conn, owner_id, viewer_id)
    conn.commit()
    row = conn.execute("SELECT * FROM shares WHERE owner_id=? AND viewer_id=?",
                       (owner_id, viewer_id)).fetchone()
    conn.close()
    return dict(row)


def shares_pending_for(viewer_id: int) -> list[dict]:
    """Eingehende Freigabe-Anfragen die noch nicht beantwortet wurden."""
    conn = get_conn()
    rows = conn.execute("""
        SELECT s.id, s.permission, s.status, s.created,
               u.user_tag AS owner_tag
        FROM   shares s JOIN users u ON u.id = s.owner_id
        WHERE  s.viewer_id=? AND s.status='pending'
        ORDER  BY s.created DESC
    """, (viewer_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def share_accept(share_id: int, viewer_id: int) -> bool:
    """Bestätigt eine eingehende Freigabe-Anfrage."""
    conn = get_conn()
    share = conn.execute(
        "SELECT owner_id FROM shares WHERE id=? AND viewer_id=? AND status='pending'",
        (share_id, viewer_id)
    ).fetchone()
    if not share:
        conn.close()
        return False
    conn.execute("UPDATE shares SET status='accepted' WHERE id=?", (share_id,))
    _connection_ensure(conn, share["owner_id"], viewer_id)
    conn.commit()
    conn.close()
    return True


def share_reject(share_id: int, viewer_id: int) -> bool:
    """Lehnt eine Freigabe-Anfrage ab und löscht sie."""
    conn = get_conn()
    share = conn.execute(
        "SELECT owner_id FROM shares WHERE id=? AND viewer_id=? AND status='pending'",
        (share_id, viewer_id)
    ).fetchone()
    if not share:
        conn.close()
        return False
    conn.execute("DELETE FROM shares WHERE id=?", (share_id,))
    # pending → nie accepted → keine user_connections angelegt → kein cleanup nötig
    conn.commit()
    conn.close()
    return True


def share_delete(share_id: int, owner_id: int) -> bool:
    conn = get_conn()
    share = conn.execute(
        "SELECT viewer_id, status FROM shares WHERE id=? AND owner_id=?", (share_id, owner_id)
    ).fetchone()
    if not share:
        conn.close()
        return False
    viewer_id = share["viewer_id"]
    conn.execute("DELETE FROM shares WHERE id=? AND owner_id=?", (share_id, owner_id))
    if share["status"] == "accepted":
        _connection_cleanup(conn, owner_id, viewer_id)
    conn.commit()
    conn.close()
    return True


# ── Einladungs-Tokens ──────────────────────────────────────────────────────────
def invite_create(token: str, created_by: int) -> dict:
    conn = get_conn()
    conn.execute("INSERT INTO invites (token, created_by) VALUES (?,?)", (token, created_by))
    conn.commit()
    row = conn.execute("SELECT * FROM invites WHERE token=?", (token,)).fetchone()
    conn.close()
    return dict(row)


def invite_check(token: str) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute("""
        SELECT * FROM invites
        WHERE token=? AND used=0
          AND created_at >= datetime('now', '-24 hours')
    """, (token,)).fetchone()
    conn.close()
    return dict(row) if row else None


def invite_use(token: str, new_user_id: Optional[int] = None) -> None:
    conn = get_conn()
    invite = conn.execute("SELECT group_id FROM invites WHERE token=?", (token,)).fetchone()
    conn.execute("UPDATE invites SET used=1 WHERE token=?", (token,))
    if invite and invite["group_id"] and new_user_id:
        # Auto-join the group the invite was created for
        gid = invite["group_id"]
        conn.execute(
            "INSERT OR IGNORE INTO group_members (group_id, user_id, role) VALUES (?,?,'member')",
            (gid, new_user_id)
        )
        # Ensure connections to all group members
        others = conn.execute(
            "SELECT user_id FROM group_members WHERE group_id=? AND user_id!=?",
            (gid, new_user_id)
        ).fetchall()
        for other in others:
            _connection_ensure(conn, new_user_id, other["user_id"])
    conn.commit()
    conn.close()


# ── Todo-Zuweisungen ───────────────────────────────────────────────────────────
def assignment_create(todo_id: str, from_user_id: int,
                      to_user_id: int, comment: Optional[str]) -> dict:
    conn = get_conn()
    conn.execute(
        "INSERT INTO todo_assignments (todo_id, from_user_id, to_user_id, comment) "
        "VALUES (?,?,?,?)",
        (todo_id, from_user_id, to_user_id, comment),
    )
    # delegated_to auf Todo setzen
    conn.execute(
        "UPDATE todos SET delegated_to=?, delegated_status='pending' WHERE id=?",
        (to_user_id, todo_id),
    )
    conn.commit()
    # Log delegation to all groups of the todo owner
    _todo_info = conn.execute("SELECT title FROM todos WHERE id=?", (todo_id,)).fetchone()
    _from_tag  = conn.execute("SELECT user_tag FROM users WHERE id=?", (from_user_id,)).fetchone()
    _to_tag    = conn.execute("SELECT user_tag FROM users WHERE id=?", (to_user_id,)).fetchone()
    if _todo_info and _from_tag and _to_tag:
        _log_for_groups(conn, from_user_id, todo_id,
                        _todo_info["title"], "delegated",
                        _from_tag["user_tag"],
                        new_value=f"an {_to_tag['user_tag']}")
        conn.commit()
    row = conn.execute(
        "SELECT * FROM todo_assignments WHERE todo_id=? AND to_user_id=? "
        "ORDER BY id DESC LIMIT 1", (todo_id, to_user_id)
    ).fetchone()
    conn.close()
    return dict(row)


def assignment_list_for_user(to_user_id: int,
                              status: str = "pending") -> list[dict]:
    """Offene Zuweisungen für einen Empfänger – Ersatz für inbox_list."""
    conn = get_conn()
    rows = conn.execute("""
        SELECT a.id, a.todo_id, a.comment, a.created,
               t.title, t.prio, t.kunde, t.sub, t.desc,
               uf.user_tag AS from_user_tag
        FROM   todo_assignments a
        JOIN   todos t  ON t.id  = a.todo_id
        JOIN   users uf ON uf.id = a.from_user_id
        WHERE  a.to_user_id=? AND a.status=?
        ORDER  BY a.created DESC
    """, (to_user_id, status)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def assignment_count_pending(to_user_id: int) -> int:
    conn = get_conn()
    n = conn.execute(
        "SELECT COUNT(*) FROM todo_assignments WHERE to_user_id=? AND status='pending'",
        (to_user_id,),
    ).fetchone()[0]
    conn.close()
    return n


def assignment_set_status(assignment_id: int, to_user_id: int,
                           status: str, response: Optional[str]) -> bool:
    """Setzt Status einer Zuweisung; prüft dass to_user_id der Empfänger ist."""
    conn = get_conn()
    row = conn.execute(
        "SELECT todo_id FROM todo_assignments WHERE id=? AND to_user_id=?",
        (assignment_id, to_user_id),
    ).fetchone()
    if not row:
        conn.close()
        return False
    conn.execute(
        "UPDATE todo_assignments SET status=?, response=?, updated=datetime('now') "
        "WHERE id=?",
        (status, response, assignment_id),
    )
    todo_row = conn.execute(
        "SELECT owner_id FROM todos WHERE id=?", (row["todo_id"],)
    ).fetchone()
    owner_id = todo_row["owner_id"] if todo_row else None

    if status == "accepted" and owner_id == to_user_id:
        # Todo kehrt zum ursprünglichen Besitzer zurück → alle Assignments schließen
        conn.execute(
            "UPDATE todos SET delegated_to=NULL, delegated_status=NULL WHERE id=?",
            (row["todo_id"],),
        )
        # Alle noch offenen Assignments für dieses Todo als 'returned' markieren
        conn.execute(
            "UPDATE todo_assignments SET status='returned', updated=datetime('now') "
            "WHERE todo_id=? AND status IN ('pending','accepted')",
            (row["todo_id"],),
        )
    elif status in ("rejected", "completed"):
        conn.execute(
            "UPDATE todos SET delegated_to=NULL, delegated_status=NULL WHERE id=?",
            (row["todo_id"],),
        )
    else:
        # accepted, aber nicht beim Owner → delegated_status setzen
        conn.execute(
            "UPDATE todos SET delegated_status=? WHERE id=?",
            (status, row["todo_id"]),
        )
    conn.commit()
    # Log delegation result to all groups of the todo owner
    _todo_info2 = conn.execute(
        "SELECT t.title, t.owner_id FROM todos t WHERE t.id=?", (row["todo_id"],)
    ).fetchone()
    _actor_tag = conn.execute("SELECT user_tag FROM users WHERE id=?", (to_user_id,)).fetchone()
    if _todo_info2 and _actor_tag and status in ("accepted", "rejected"):
        _action = "delegation_accepted" if status == "accepted" else "delegation_rejected"
        _log_for_groups(conn, _todo_info2["owner_id"], row["todo_id"],
                        _todo_info2["title"], _action, _actor_tag["user_tag"])
        conn.commit()
    conn.close()
    return True


def assignment_retract(todo_id: str, owner_id: int) -> bool:
    """Zuweisung zurückziehen – nur solange status='pending'."""
    conn = get_conn()
    row = conn.execute(
        "SELECT id FROM todo_assignments "
        "WHERE todo_id=? AND from_user_id=? AND status='pending'",
        (todo_id, owner_id),
    ).fetchone()
    if not row:
        conn.close()
        return False
    conn.execute(
        "UPDATE todo_assignments SET status='retracted', updated=datetime('now') WHERE id=?",
        (row["id"],),
    )
    conn.execute(
        "UPDATE todos SET delegated_to=NULL, delegated_status=NULL WHERE id=? AND owner_id=?",
        (todo_id, owner_id),
    )
    conn.commit()
    conn.close()
    return True


def assignment_list_by_sender(from_user_id: int) -> list[dict]:
    """Alle Zuweisungen die ein User abgeschickt hat (für Delegations-Badge)."""
    conn = get_conn()
    rows = conn.execute("""
        SELECT a.id, a.todo_id, a.status, a.created,
               ut.user_tag AS to_user_tag
        FROM   todo_assignments a
        JOIN   users ut ON ut.id = a.to_user_id
        WHERE  a.from_user_id=? AND a.status='pending'
        ORDER  BY a.created DESC
    """, (from_user_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]
