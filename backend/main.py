import base64
import hashlib
import json
import logging
import os

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
import random
import string
import time
import traceback
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, List, Optional

import bcrypt
import jwt as pyjwt
from dotenv import load_dotenv
from fastapi import Body, Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

import database as db

# ── .env laden (nur wenn Datei vorhanden, prod nutzt echte Env-Vars) ──────────
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    load_dotenv(_env_file)

# ── Konfiguration ──────────────────────────────────────────────────────────────
SECRET_KEY        = os.environ.get("SECRET_KEY", "BITTE-IN-PRODUKTION-AENDERN")
DATABASE_URL      = os.environ.get("DATABASE_URL", "todo.db")
BASE_URL          = os.environ.get("BASE_URL", "https://DEINE-DOMAIN.de/todo")
ALGORITHM         = "HS256"
TOKEN_EXPIRE_DAYS = 30
FRONTEND_DIR      = os.path.join(os.path.dirname(__file__), "..", "frontend")

# DATABASE_URL an database-Modul weitergeben bevor init_db() aufgerufen wird
db.DB_PATH = (
    DATABASE_URL if os.path.isabs(DATABASE_URL)
    else str(Path(__file__).parent / DATABASE_URL)
)

# ── Rate Limiter ───────────────────────────────────────────────────────────────
limiter = Limiter(key_func=get_remote_address, default_limits=["100/minute"])

# ── Passwort-Hashing (SHA-256 pre-hash → immer ≤44 Bytes → bcrypt-safe) ───────
oauth2 = OAuth2PasswordBearer(tokenUrl="/api/auth/login")


def _bcrypt_input(pw: str) -> bytes:
    return base64.b64encode(hashlib.sha256(pw.encode()).digest())


def hash_pw(pw: str) -> str:
    return bcrypt.hashpw(_bcrypt_input(pw), bcrypt.gensalt()).decode()


def verify_pw(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(_bcrypt_input(plain), hashed.encode())


def make_token(user_id: int, user_tag: str, role: str) -> str:
    exp = datetime.utcnow() + timedelta(days=TOKEN_EXPIRE_DAYS)
    return pyjwt.encode(
        {"sub": user_tag, "uid": user_id, "role": role, "exp": exp},
        SECRET_KEY, algorithm=ALGORITHM,
    )


def gen_id() -> str:
    return str(int(time.time() * 1000))[-8:] + "".join(
        random.choices(string.ascii_lowercase + string.digits, k=4)
    )


# ── Dependency: eingeloggter User ──────────────────────────────────────────────
async def get_current_user(token: str = Depends(oauth2)):
    exc = HTTPException(status.HTTP_401_UNAUTHORIZED, "Ungültige Anmeldedaten",
                        headers={"WWW-Authenticate": "Bearer"})
    try:
        payload = pyjwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_tag: str = payload.get("sub")
        if not user_tag:
            raise exc
    except pyjwt.PyJWTError:
        raise exc
    user = db.user_by_tag(user_tag)
    if not user:
        raise exc
    return user


def require_admin(u=Depends(get_current_user)):
    if u["role"] != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Nur Admins")
    return u


def require_manager_or_admin(u=Depends(get_current_user)):
    if u["role"] not in ("admin", "manager"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Nur Manager oder Admins")
    return u


def require_group_admin(group_id: int, u: dict) -> dict:
    """Raises 403 if user is not system admin or group admin of group_id."""
    if u["role"] == "admin":
        return u
    role = db.group_get_member_role(group_id, u["id"])
    if role != "admin":
        raise HTTPException(403, "Gruppen-Admin Berechtigung erforderlich")
    return u


# ── Pydantic-Schemas ───────────────────────────────────────────────────────────
class UserCreate(BaseModel):
    username: str
    password: str

class PasswordReset(BaseModel):
    password: str

class PasswordChange(BaseModel):
    old_password: str
    new_password: str

class RoleUpdate(BaseModel):
    role: str          # admin | manager | member

class DataPayload(BaseModel):
    todos:         List[Any] = []
    archiv:        List[Any] = []
    berichte:      List[Any] = []
    kundenNotizen: Any       = {}

class AssignRequest(BaseModel):
    to_user_tag: str
    comment:     Optional[str] = None

class InboxReply(BaseModel):
    comment: Optional[str] = None

class GroupCreate(BaseModel):
    name: str

class GroupMemberAction(BaseModel):
    user_id: int

class ShareCreate(BaseModel):
    viewer_tag: str
    permission: str = "read"   # read | write

class GroupRename(BaseModel):
    name: str

class GroupMemberRoleUpdate(BaseModel):
    role: str  # 'member' | 'admin'


# ── App ────────────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app):
    db.init_db()
    yield

app = FastAPI(title="Todo-App API", lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


# ══════════════════════════════════════════════════════════════════════════════
# AUTH
# ══════════════════════════════════════════════════════════════════════════════
@app.get("/api/auth/check-invite")
@limiter.limit("10/minute")
def check_invite(request: Request, token: str):
    """Prüft ob ein Einladungs-Token gültig ist (unverbraucht, < 24h)."""
    valid = db.invite_check(token) is not None
    # Erster User darf immer registrieren
    if not valid and db.user_count() == 0:
        valid = True
    return {"valid": valid}


@app.post("/api/auth/register", status_code=201)
@limiter.limit("5/hour")
def register(request: Request, body: UserCreate, token: str = ""):
    if not body.username.strip() or not body.password:
        raise HTTPException(400, "Benutzername und Passwort erforderlich")
    clean = body.username.strip()
    if "#" in clean:
        raise HTTPException(400, "Benutzername darf kein '#' enthalten")

    # Registrierung nur erlaubt wenn: kein User existiert ODER gültiger Token
    if db.user_count() > 0:
        invite = db.invite_check(token) if token else None
        if not invite:
            raise HTTPException(403, "Ungültiger oder abgelaufener Einladungs-Link")

    try:
        user = db.user_create(clean, hash_pw(body.password))
    except Exception as e:
        raise HTTPException(400, str(e))

    # Token als verwendet markieren
    if token:
        db.invite_use(token, user["id"])

    return {"ok": True, "user_tag": user["user_tag"]}


@app.post("/api/auth/login")
@limiter.limit("10/minute")
def login(request: Request, form: OAuth2PasswordRequestForm = Depends()):
    user = db.user_by_tag(form.username)
    if not user or not verify_pw(form.password, user["pw_hash"]):
        raise HTTPException(400, "Falscher Benutzername oder Passwort")
    if (user["status"] or "active") != "active":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Dieser Account ist deaktiviert")

    # Defensiv: user_tag reparieren falls Migrations-Backfill noch nicht lief
    user_tag = user["user_tag"]
    if not user_tag:
        user_tag = db.repair_user_tag(user["id"], user["username"])

    return {
        "access_token": make_token(user["id"], user_tag, user["role"]),
        "token_type":   "bearer",
        "user_tag":     user_tag,
        "user_id":      user["id"],
        "role":         user["role"],
    }


@app.get("/api/auth/me")
def me(u=Depends(get_current_user)):
    return {"id": u["id"], "username": u["username"], "user_tag": u["user_tag"], "role": u["role"]}


@app.post("/api/auth/change-password")
def change_password(body: PasswordChange, u=Depends(get_current_user)):
    if not verify_pw(body.old_password, u["pw_hash"]):
        raise HTTPException(400, "Altes Passwort ist falsch")
    if len(body.new_password) < 4:
        raise HTTPException(400, "Neues Passwort muss mindestens 4 Zeichen haben")
    db.user_set_pw(u["id"], hash_pw(body.new_password))
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════════════════
# USERS  (für Assign-Dropdown)
# ══════════════════════════════════════════════════════════════════════════════
@app.get("/api/users")
def list_users(u=Depends(get_current_user)):
    """Alle Rollen: nur verbundene User (gleiche Gruppe oder Freigabe)."""
    return db.users_list(exclude_id=u["id"],
                         same_group_only=True,
                         current_user_id=u["id"])


# ══════════════════════════════════════════════════════════════════════════════
# DATEN
# ══════════════════════════════════════════════════════════════════════════════
@app.get("/api/data")
def load_data(u=Depends(get_current_user)):
    return db.data_load(u["id"])


@app.put("/api/data")
def save_data(payload: DataPayload, u=Depends(get_current_user)):
    db.data_save(u["id"], payload.todos, payload.archiv,
                 payload.berichte, payload.kundenNotizen)
    return {"ok": True}


# ── Geteilte Daten lesen/schreiben ─────────────────────────────────────────────
@app.get("/api/shared/{owner_id}/data")
def read_shared(owner_id: int, u=Depends(get_current_user)):
    data = db.data_load_for_viewer(owner_id=owner_id, viewer_id=u["id"])
    if data is None:
        raise HTTPException(403, "Kein Zugriff auf diesen Bereich")
    return data


@app.put("/api/shared/{owner_id}/data")
def write_shared(owner_id: int, payload: DataPayload, u=Depends(get_current_user)):
    ok = db.data_save_for_writer(
        owner_id=owner_id, writer_id=u["id"],
        todos=payload.todos, archiv=payload.archiv,
        berichte=payload.berichte, kunden_notizen=payload.kundenNotizen,
    )
    if not ok:
        raise HTTPException(403, "Keine Schreibberechtigung")
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════════════════
# ZUWEISUNG  (todo_assignments-Tabelle)
# ══════════════════════════════════════════════════════════════════════════════
@app.post("/api/assign/{todo_id}")
def assign_todo(todo_id: str, body: AssignRequest, u=Depends(get_current_user)):
    to_user = db.user_by_tag(body.to_user_tag)
    if not to_user:
        raise HTTPException(404, "Ziel-Benutzer nicht gefunden")
    if (to_user["status"] or "active") != "active":
        raise HTTPException(403, "Dieser User ist deaktiviert")
    if to_user["id"] == u["id"]:
        raise HTTPException(400, "Nicht an sich selbst zuweisen")

    # Alle Rollen: Zuweisung nur an verbundene User (gleiche Gruppe oder Freigabe)
    if not db.users_connected(u["id"], to_user["id"]):
        raise HTTPException(403, "Zuweisung nur an verbundene Benutzer erlaubt (gleiche Gruppe oder Freigabe)")

    # Todo muss dem Sender gehören (owner) ODER er ist aktueller delegated_to-Empfänger
    _c = db.get_conn()
    _todo_row = _c.execute(
        "SELECT id FROM todos WHERE id=? AND is_archived=0 AND (owner_id=? OR delegated_to=?)",
        (todo_id, u["id"], u["id"]),
    ).fetchone()
    _c.close()
    if not _todo_row:
        raise HTTPException(404, "Todo nicht gefunden oder kein Zugriff")

    assignment = db.assignment_create(
        todo_id=todo_id,
        from_user_id=u["id"],
        to_user_id=to_user["id"],
        comment=body.comment,
    )
    return {"ok": True, "assignment_id": assignment["id"]}


# ══════════════════════════════════════════════════════════════════════════════
# POSTEINGANG  (liest aus todo_assignments)
# ══════════════════════════════════════════════════════════════════════════════
@app.get("/api/inbox")
def get_inbox(u=Depends(get_current_user)):
    rows = db.assignment_list_for_user(u["id"], status="pending")
    # Frontend-kompatibles Format: {id, todo, comment, from_user_tag, created}
    result = []
    for r in rows:
        result.append({
            "id":            r["id"],
            "from_user_tag": r["from_user_tag"],
            "comment":       r["comment"],
            "created":       r["created"],
            "todo": {
                "id":     r["todo_id"],
                "title":  r["title"],
                "prio":   r["prio"],
                "kunde":  r["kunde"],
                "sub":    r["sub"],
                "desc":   r["desc"],
            },
        })
    return result


@app.get("/api/inbox/count")
def inbox_count(u=Depends(get_current_user)):
    return {"count": db.assignment_count_pending(u["id"])}


@app.post("/api/inbox/{assignment_id}/accept")
def accept_inbox(assignment_id: int, body: InboxReply, u=Depends(get_current_user)):
    # Setzt delegated_status='accepted' → Todo taucht beim Empfänger via data_load auf
    ok = db.assignment_set_status(assignment_id, u["id"], "accepted", body.comment)
    if not ok:
        raise HTTPException(404, "Zuweisung nicht gefunden")
    return {"ok": True}


@app.post("/api/inbox/{assignment_id}/reject")
def reject_inbox(assignment_id: int, body: InboxReply, u=Depends(get_current_user)):
    ok = db.assignment_set_status(assignment_id, u["id"], "rejected", body.comment)
    if not ok:
        raise HTTPException(404, "Zuweisung nicht gefunden")
    return {"ok": True}


@app.post("/api/assign/{todo_id}/retract")
def retract_assign(todo_id: str, u=Depends(get_current_user)):
    """Zuweisung zurückziehen – nur solange noch pending."""
    ok = db.assignment_retract(todo_id, u["id"])
    if not ok:
        raise HTTPException(404, "Keine rückziehbare Zuweisung gefunden")
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════════════════
# FREIGABEN
# ══════════════════════════════════════════════════════════════════════════════
@app.get("/api/shares")
def get_shares(u=Depends(get_current_user)):
    return {"outgoing": db.shares_by_owner(u["id"])}


@app.post("/api/shares")
def create_share(body: ShareCreate, u=Depends(get_current_user)):
    viewer = db.user_by_tag(body.viewer_tag)
    if not viewer:
        raise HTTPException(404, "Benutzer nicht gefunden")
    if (viewer["status"] or "active") != "active":
        raise HTTPException(403, "Dieser User ist deaktiviert")
    if viewer["id"] == u["id"]:
        raise HTTPException(400, "Nicht mit sich selbst teilen")
    if body.permission not in ("read", "write"):
        raise HTTPException(400, "permission muss 'read' oder 'write' sein")
    share = db.share_create(u["id"], viewer["id"], body.permission)
    return share


@app.get("/api/shares/pending")
def get_pending_shares(u=Depends(get_current_user)):
    """Eingehende Freigabe-Anfragen (noch nicht beantwortet)."""
    return db.shares_pending_for(u["id"])


@app.post("/api/shares/{share_id}/accept")
def accept_share(share_id: int, u=Depends(get_current_user)):
    if not db.share_accept(share_id, u["id"]):
        raise HTTPException(404, "Anfrage nicht gefunden oder bereits beantwortet")
    return {"ok": True}


@app.post("/api/shares/{share_id}/reject")
def reject_share(share_id: int, u=Depends(get_current_user)):
    if not db.share_reject(share_id, u["id"]):
        raise HTTPException(404, "Anfrage nicht gefunden oder bereits beantwortet")
    return {"ok": True}


@app.delete("/api/shares/{share_id}")
def delete_share(share_id: int, u=Depends(get_current_user)):
    if not db.share_delete(share_id, u["id"]):
        raise HTTPException(404, "Freigabe nicht gefunden")
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN
# ══════════════════════════════════════════════════════════════════════════════
@app.post("/api/admin/invite", status_code=201)
def admin_create_invite(request: Request, u=Depends(require_admin)):
    token = "".join(random.choices(string.ascii_letters + string.digits, k=32))
    invite = db.invite_create(token, u["id"])
    scheme = request.headers.get('x-forwarded-proto', request.url.scheme)
    base_url = f"{scheme}://{request.headers.get('host', BASE_URL.split('://')[-1])}"
    link = f"{base_url}/todo/login.html?token={token}"
    return {"token": token, "link": link, "expires_in": "24 Stunden"}


@app.get("/api/admin/users")
def admin_list_users(u=Depends(require_admin)):
    return db.users_all()


@app.put("/api/admin/users/{user_id}/role")
def admin_set_role(user_id: int, body: RoleUpdate, u=Depends(require_admin)):
    if body.role not in ("admin", "manager", "member"):
        raise HTTPException(400, "Ungültige Rolle")
    target = db.user_by_id(user_id)
    if not target:
        raise HTTPException(404, "User nicht gefunden")
    db.user_set_role(user_id, body.role)
    return {"ok": True}


@app.post("/api/admin/users/{user_id}/password")
def admin_reset_pw(user_id: int, body: PasswordReset, u=Depends(require_admin)):
    if not body.password or len(body.password) < 4:
        raise HTTPException(400, "Passwort zu kurz (min. 4 Zeichen)")
    target = db.user_by_id(user_id)
    if not target:
        raise HTTPException(404, "User nicht gefunden")
    db.user_set_pw(user_id, hash_pw(body.password))
    return {"ok": True}


@app.get("/api/admin/groups")
def admin_list_groups(u=Depends(require_admin)):
    return db.groups_list_all()


@app.post("/api/admin/groups", status_code=201)
def admin_create_group(body: GroupCreate, u=Depends(require_admin)):
    if not body.name.strip():
        raise HTTPException(400, "Gruppenname darf nicht leer sein")
    try:
        return db.group_create(body.name.strip(), u["id"], auto_join=False)
    except Exception:
        raise HTTPException(400, "Gruppenname bereits vergeben")


@app.delete("/api/admin/groups/{group_id}")
def admin_delete_group(group_id: int, u=Depends(require_admin)):
    db.group_delete(group_id)
    return {"ok": True}


@app.post("/api/admin/groups/{group_id}/members")
def admin_add_member(group_id: int, body: GroupMemberAction, u=Depends(require_admin)):
    db.group_add_member(group_id, body.user_id)
    return {"ok": True}


@app.delete("/api/admin/groups/{group_id}/members/{user_id}")
def admin_remove_member(group_id: int, user_id: int, u=Depends(require_admin)):
    db.group_remove_member(group_id, user_id)
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════════════════
# GRUPPEN-ADMIN  (group_admin oder system admin)
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/groups/member-of")
def get_my_groups(u=Depends(get_current_user)):
    """Alle Gruppen in denen der User Mitglied ist (beliebige Rolle)."""
    return db.user_member_groups(u["id"])


@app.get("/api/groups/mine")
def get_my_admin_groups(u=Depends(get_current_user)):
    """Gruppen in denen der User Gruppen-Admin ist (+ Mitgliederliste)."""
    if u["role"] == "admin":
        return db.groups_list_all()
    groups = db.user_admin_groups(u["id"])
    result = []
    for g in groups:
        members = db.group_list_members(g["id"])
        result.append({**g, "members": members})
    return result


@app.put("/api/groups/{group_id}")
def rename_group(group_id: int, body: GroupRename,
                 u=Depends(get_current_user)):
    u = require_group_admin(group_id, u)  # inline check
    if not body.name.strip():
        raise HTTPException(400, "Name darf nicht leer sein")
    ok = db.group_rename(group_id, body.name.strip())
    if not ok:
        raise HTTPException(404, "Gruppe nicht gefunden oder Name vergeben")
    return {"ok": True}


@app.post("/api/groups/{group_id}/invite", status_code=201)
def group_create_invite(request: Request, group_id: int, u=Depends(get_current_user)):
    u = require_group_admin(group_id, u)
    token = "".join(random.choices(string.ascii_letters + string.digits, k=32))
    db.invite_create_for_group(token, u["id"], group_id)
    scheme = request.headers.get('x-forwarded-proto', request.url.scheme)
    base_url = f"{scheme}://{request.headers.get('host', BASE_URL.split('://')[-1])}"
    link = f"{base_url}/todo/login.html?token={token}"
    return {"token": token, "link": link, "expires_in": "24 Stunden"}


@app.put("/api/groups/{group_id}/members/{user_id}/role")
def set_group_member_role(group_id: int, user_id: int,
                          body: GroupMemberRoleUpdate,
                          u=Depends(get_current_user)):
    u = require_group_admin(group_id, u)
    if body.role not in ("member", "admin"):
        raise HTTPException(400, "role muss 'member' oder 'admin' sein")
    if user_id == u["id"]:
        raise HTTPException(400, "Eigene Rolle kann nicht geändert werden")
    target_role = db.group_get_member_role(group_id, user_id)
    if target_role is None:
        raise HTTPException(404, "User nicht in dieser Gruppe")
    db.group_set_member_role(group_id, user_id, body.role)
    return {"ok": True}


@app.delete("/api/groups/{group_id}/members/{user_id}")
def group_remove_member_endpoint(group_id: int, user_id: int,
                                  u=Depends(get_current_user)):
    # System admin OR group admin (but group admin can't remove themselves if last admin)
    if u["role"] != "admin":
        role = db.group_get_member_role(group_id, u["id"])
        if role != "admin":
            raise HTTPException(403, "Gruppen-Admin Berechtigung erforderlich")
    db.group_remove_member(group_id, user_id)
    return {"ok": True}


@app.get("/api/groups/{group_id}/archive")
def get_group_archive(group_id: int, u=Depends(get_current_user)):
    role = db.group_get_member_role(group_id, u["id"])
    if role is None and u["role"] != "admin":
        raise HTTPException(403, "Kein Zugriff")
    return db.group_archive_todos(group_id)


@app.get("/api/groups/{group_id}/archive-log")
def get_group_archive_log(group_id: int, u=Depends(get_current_user)):
    role = db.group_get_member_role(group_id, u["id"])
    if role is None and u["role"] != "admin":
        raise HTTPException(403, "Kein Zugriff auf diesen Gruppen-Log")
    return db.group_archive_log_get(group_id)


@app.put("/api/groups/{group_id}/members/{user_id}/deactivate")
def deactivate_group_member(group_id: int, user_id: int, u=Depends(get_current_user)):
    require_group_admin(group_id, u)
    target = db.user_by_id(user_id)
    if not target:
        raise HTTPException(404, "User nicht gefunden")
    if (target["status"] or "active") == "inactive":
        raise HTTPException(400, "User ist bereits deaktiviert")
    # Gruppen-Admin darf sich nicht selbst deaktivieren
    if user_id == u["id"]:
        raise HTTPException(400, "Du kannst dich nicht selbst deaktivieren")
    db.user_deactivate(user_id, u["id"], u["user_tag"])
    return {"ok": True}


@app.put("/api/admin/users/{user_id}/activate")
def activate_user(user_id: int, u=Depends(require_admin)):
    target = db.user_by_id(user_id)
    if not target:
        raise HTTPException(404, "User nicht gefunden")
    db.user_activate(user_id)
    return {"ok": True}


@app.put("/api/admin/users/{user_id}/deactivate")
def admin_deactivate_user(user_id: int, u=Depends(require_admin)):
    target = db.user_by_id(user_id)
    if not target:
        raise HTTPException(404, "User nicht gefunden")
    if (target["status"] or "active") != "active":
        raise HTTPException(400, "User ist bereits inaktiv")
    db.user_deactivate(user_id, u["id"], u["user_tag"])
    return {"ok": True}


@app.delete("/api/admin/users/{user_id}")
def delete_user(user_id: int, u=Depends(require_admin)):
    target = db.user_by_id(user_id)
    if not target:
        raise HTTPException(404, "User nicht gefunden")
    if (target["status"] or "active") == "active":
        raise HTTPException(400, "Nur inaktive User können gelöscht werden")
    db.user_delete(user_id)
    return {"ok": True}


# ── Pool (Schwarzes Brett) ────────────────────────────────────────────────────

class PoolTodoCreate(BaseModel):
    title: str
    kunde: str = ""
    sub: str = ""
    subsub: str = ""
    prio: str = "mittel"
    desc: str = ""
    link: str = ""


@app.post("/api/groups/{group_id}/pool", status_code=201)
def pool_create(group_id: int, body: PoolTodoCreate, u=Depends(get_current_user)):
    logging.info("[pool_create] group_id=%s user=%s body=%s", group_id, u["user_tag"], body.dict())
    # Schritt 1: Mitgliedschaft prüfen
    if db.group_get_member_role(group_id, u["id"]) is None:
        logging.warning("[pool_create] 403 – user %s ist kein Mitglied von Gruppe %s", u["user_tag"], group_id)
        raise HTTPException(403, "Kein Mitglied dieser Gruppe")
    # Schritt 2: Pool-Berechtigung prüfen (Admin oder can_post_to_pool)
    if not db.pool_can_post(group_id, u["id"]):
        logging.warning("[pool_create] 403 – user %s hat keine Pool-Berechtigung für Gruppe %s", u["user_tag"], group_id)
        raise HTTPException(403, "Keine Pool-Berechtigung")
    try:
        todo = db.pool_create(
            group_id, u["id"], body.title, body.kunde,
            body.sub, body.subsub, body.prio, body.desc, body.link,
        )
    except Exception:
        logging.error("[pool_create] DB-Fehler:\n%s", traceback.format_exc())
        raise HTTPException(500, "Interner Fehler beim Erstellen des Pool-Todos")
    return todo


@app.get("/api/groups/{group_id}/pool")
def pool_list(group_id: int, u=Depends(get_current_user)):
    if db.group_get_member_role(group_id, u["id"]) is None and u["role"] != "admin":
        raise HTTPException(403, "Kein Zugriff")
    return db.pool_list(group_id)


@app.post("/api/groups/{group_id}/pool/{todo_id}/take")
def pool_take(group_id: int, todo_id: str, u=Depends(get_current_user)):
    if db.group_get_member_role(group_id, u["id"]) is None and u["role"] != "admin":
        raise HTTPException(403, "Kein Zugriff")
    try:
        todo = db.pool_take(group_id, todo_id, u["id"])
    except ValueError as e:
        raise HTTPException(400, str(e))
    return todo


@app.post("/api/todos/{todo_id}/send-to-pool")
def todo_send_to_pool(todo_id: str, group_id: int = Body(..., embed=True),
                      u=Depends(get_current_user)):
    # Schritt 1: Mitgliedschaft prüfen
    if db.group_get_member_role(group_id, u["id"]) is None:
        raise HTTPException(403, "Kein Mitglied dieser Gruppe")
    # Schritt 2: Pool-Berechtigung prüfen
    if not db.pool_can_post(group_id, u["id"]):
        raise HTTPException(403, "Keine Pool-Berechtigung")
    try:
        todo = db.todo_send_to_pool(todo_id, group_id, u["id"])
    except ValueError as e:
        raise HTTPException(400, str(e))
    return todo


@app.put("/api/groups/{group_id}/members/{user_id}/pool-permission")
def set_pool_permission(group_id: int, user_id: int,
                        can_post: bool = Body(..., embed=True),
                        u=Depends(get_current_user)):
    require_group_admin(group_id, u)
    if not db.pool_set_permission(group_id, user_id, can_post):
        raise HTTPException(404, "Mitglied nicht gefunden")
    return {"ok": True}


@app.get("/api/admin/groups/logs")
def admin_all_group_logs(u=Depends(require_admin)):
    return db.admin_all_group_logs()


@app.put("/api/users/me/deactivate")
def self_deactivate(u=Depends(get_current_user)):
    if (u["status"] or "active") != "active":
        raise HTTPException(400, "Account ist bereits inaktiv")
    db.user_deactivate(u["id"], u["id"], u["user_tag"])
    return {"ok": True}


@app.delete("/api/users/me")
def self_delete(u=Depends(get_current_user), password: str = Body(..., embed=True)):
    if not verify_pw(password, u["pw_hash"]):
        raise HTTPException(400, "Passwort falsch")
    db.user_delete(u["id"])
    return {"ok": True}


@app.get("/api/users/me/export")
def self_export(u=Depends(get_current_user)):
    data = db.data_load(u["id"])
    groups = db.user_member_groups(u["id"])
    logs: list = []
    for g in groups:
        logs.extend(db.group_archive_log_get(g["id"], limit=500))
    return {
        "exported_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "user_tag": u["user_tag"],
        "todos": data.get("todos", []),
        "archiv": data.get("archiv", []),
        "berichte": data.get("berichte", []),
        "activity_log": logs,
    }


# ── Frontend (muss zuletzt stehen) ─────────────────────────────────────────────
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
