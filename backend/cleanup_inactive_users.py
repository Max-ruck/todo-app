#!/usr/bin/env python3
"""
cleanup_inactive_users.py
Löscht User-Accounts die seit mindestens 180 Tagen inaktiv sind.

Ausführung:
  python3 cleanup_inactive_users.py
  python3 cleanup_inactive_users.py --dry-run   # zeigt nur was gelöscht würde

Cronjob (täglich um 03:00):
  0 3 * * * /usr/bin/python3 /path/to/backend/cleanup_inactive_users.py >> /path/to/backend/cleanup.log 2>&1
"""

import sys
import logging
from datetime import datetime, timezone
from pathlib import Path

# ── Pfad-Setup: Backend-Verzeichnis zum sys.path hinzufügen ──────────────────
BACKEND_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

from dotenv import load_dotenv
load_dotenv(BACKEND_DIR / ".env")

import database as db

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_FILE = BACKEND_DIR / "cleanup.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

INACTIVE_DAYS = 180


def find_expired_users(conn) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id, user_tag, status, deactivated_at
        FROM   users
        WHERE  status = 'inactive'
          AND  deactivated_at IS NOT NULL
          AND  deactivated_at <= datetime('now', ? )
        """,
        (f"-{INACTIVE_DAYS} days",),
    ).fetchall()
    return [dict(r) for r in rows]


def run(dry_run: bool = False) -> None:
    db.init_db()
    conn = db.get_conn()

    log.info("=" * 60)
    log.info("Cleanup gestartet%s", "  [DRY-RUN]" if dry_run else "")

    expired = find_expired_users(conn)

    if not expired:
        log.info("Keine abgelaufenen Accounts gefunden.")
        log.info("Cleanup beendet.")
        return

    log.info("Gefunden: %d Account(s) zur Löschung", len(expired))

    deleted = 0
    errors  = 0
    for user in expired:
        uid  = user["id"]
        tag  = user["user_tag"]
        since = user["deactivated_at"]
        try:
            if not dry_run:
                db.user_delete(uid)
            log.info(
                "%s  user_id=%-6s  tag=%-20s  deactivated_at=%s",
                "[DRY]" if dry_run else "[DEL]",
                uid,
                tag,
                since,
            )
            deleted += 1
        except Exception as exc:
            log.error("Fehler beim Löschen von user_id=%s (%s): %s", uid, tag, exc)
            errors += 1

    log.info(
        "Cleanup abgeschlossen: %d gelöscht, %d Fehler%s.",
        deleted,
        errors,
        " (kein echter Löschvorgang)" if dry_run else "",
    )


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    run(dry_run=dry_run)
