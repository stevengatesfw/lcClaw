# -*- coding: utf-8 -*-
import json
import mimetypes
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from starlette.responses import FileResponse

from ..auth import _get_platform_key, verify_lcagent_token
from ...constant import USERS_DIR

router = APIRouter(prefix="/files", tags=["files"])

_WORKSPACES = "/workspaces/"


@router.get("/workspaces", summary="List workspace files for a user")
async def list_workspace_files(
    request: Request,
    user_id: str = "",
):
    from datetime import datetime
    base = Path(USERS_DIR).resolve()
    uid = user_id or str(getattr(request.state, "user", ""))
    if not uid:
        raise HTTPException(status_code=400, detail="Missing user_id")

    user_root = base
    for seg in uid.replace("\\", "/").split("/"):
        if not seg or seg in (".", ".."):
            raise HTTPException(status_code=400, detail="Invalid user_id")
        user_root = user_root / seg

    ws_dir = user_root / "workspaces" / "default"

    skip_projects = {"browser", "dialog", "embedding_cache", "file_store", "media", "memory", "tool_result", ".trash"}
    skip_names = {"agent.json", "skill.json", "chats.json", "jobs.json",
                  "chats.json.tmp", "config.json", "token_usage.json",
                  "copaw_file_metadata.json"}
    useful_exts = {".md", ".html", ".pdf", ".json", ".yaml", ".yml", ".csv", ".txt",
                   ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".mp4", ".avi", ".mov", ".py", ".js", ".ts", ".css", ".pptx", ".docx"}

    def _entry(f: Path) -> dict:
        rel = str(f.relative_to(ws_dir))
        mtime = f.stat().st_mtime
        return {
            "name": f.name,
            "path": str(f),
            "relative_path": rel,
            "size": f.stat().st_size,
            "modified": datetime.fromtimestamp(mtime).isoformat(),
            "preview_url": f"/copaw/api/files/preview/{uid}/workspaces/default/{rel}",
        }

    def _should_skip(f: Path, skip_names: set) -> bool:
        return f.is_dir() or f.name.startswith(".") or f.suffix.lower() not in useful_exts or f.name in skip_names

    items = []

    if ws_dir.is_dir():
        for project_dir in sorted(ws_dir.iterdir()):
            if not project_dir.is_dir() or project_dir.name.startswith(".") or project_dir.name in skip_projects:
                continue
            files = []
            latest_mtime = 0
            for f in sorted(project_dir.rglob("*")):
                if _should_skip(f, skip_names):
                    continue
                e = _entry(f)
                files.append(e)
                mtime = f.stat().st_mtime
                if mtime > latest_mtime:
                    latest_mtime = mtime

            if files:
                items.append({
                    "project": project_dir.name,
                    "files": files,
                "modified": datetime.fromtimestamp(latest_mtime).isoformat() if latest_mtime else "",
            })

    root_files = []
    if ws_dir.is_dir():
        for f in sorted(ws_dir.iterdir()):
            if _should_skip(f, skip_names):
                continue
            root_files.append(_entry(f))

    # Also scan user_root for files not in workspaces/default/
    root_skip_dirs = {"__pycache__", "dialog", "embedding_cache",
                      "file_store", "memory", "sessions", "tool_result", "workspaces"}
    from urllib.parse import quote
    for f in sorted(user_root.iterdir()):
        if f.is_dir() and f.name in root_skip_dirs:
            continue
        if _should_skip(f, skip_names):
            continue
        mtime = f.stat().st_mtime
        segs = "/".join(quote(p, safe="") for p in f.absolute().parts[1:])
        root_files.append({
            "name": f.name,
            "path": str(f),
            "relative_path": str(f.relative_to(user_root)),
            "size": f.stat().st_size,
            "modified": datetime.fromtimestamp(mtime).isoformat(),
            "preview_url": f"/copaw/api/files/preview/{segs}",
        })

    if root_files:
        items.append({
            "project": "single_file",
            "files": root_files,
            "modified": "",
        })

    return {"user_id": uid, "workspaces": items}


@router.get("/chats", summary="List copaw chat sessions for a user")
async def list_chats(user_id: str = ""):
    if not user_id:
        return {"chats": [], "error": "no user_id"}
    try:
        from sqlalchemy import text as sa_text
        from ..runner.repo.db_repo import _get_engine
        with _get_engine().connect() as conn:
            rows = conn.execute(
                sa_text(
                    "SELECT id, session_id, name, user_id, channel, "
                    "status, created_at, updated_at "
                    "FROM copaw_chat_sessions WHERE user_id = :uid "
                    "ORDER BY created_at DESC"
                ),
                {"uid": user_id},
            ).fetchall()
        chats = []
        for r in rows:
            chats.append({
                "id": r[0],
                "session_id": r[1],
                "name": r[2] or "New Chat",
                "user_id": r[3],
                "channel": r[4] or "console",
                "status": r[5] or "idle",
                "created_at": _fmt_dt(r[6]),
                "updated_at": _fmt_dt(r[7]),
            })
        return {"version": 1, "chats": chats}
    except Exception as e:
        return {"chats": [], "error": str(e)}


@router.get("/chat/{session_id:path}", summary="Get copaw chat messages for a session")
async def get_chat(session_id: str, user_id: str = ""):
    if not user_id:
        return {"detail": "Missing user_id"}
    try:
        from sqlalchemy import text as sa_text
        from ..runner.repo.db_repo import _get_engine
        with _get_engine().connect() as conn:
            rows = conn.execute(
                sa_text(
                    "SELECT role, content FROM copaw_chat_messages "
                    "WHERE session_id = :sid ORDER BY id ASC"
                ),
                {"sid": session_id},
            ).fetchall()
        messages = [{"role": r[0], "content": r[1] or ""} for r in rows]
        return {"messages": messages}
    except Exception as e:
        return {"messages": [], "error": str(e)}


@router.get("/chat-by-project/{project_name:path}", summary="Find chat session that generated a workspace project")
async def get_chat_by_project(project_name: str, user_id: str = ""):
    if not user_id or not project_name:
        return {"messages": [], "error": "Missing user_id or project_name"}
    try:
        from sqlalchemy import text as sa_text
        from ..runner.repo.db_repo import _get_engine
        pat = f"%{project_name}%"
        with _get_engine().connect() as conn:
            # Search message content (equivalent to old full-text scan of session files)
            msg_row = conn.execute(
                sa_text(
                    "SELECT cm.session_id FROM copaw_chat_messages cm "
                    "JOIN copaw_chat_sessions cs ON cm.session_id = cs.session_id "
                    "WHERE cs.user_id = :uid AND cm.content LIKE :pat "
                    "LIMIT 1"
                ),
                {"uid": user_id, "pat": pat},
            ).fetchone()

            if msg_row is not None:
                sid = msg_row[0]
            else:
                # Fallback: search session_id for project name match
                sess_row = conn.execute(
                    sa_text(
                        "SELECT session_id FROM copaw_chat_sessions "
                        "WHERE user_id = :uid AND session_id LIKE :pat "
                        "ORDER BY updated_at DESC LIMIT 1"
                    ),
                    {"uid": user_id, "pat": pat},
                ).fetchone()
                if sess_row is None:
                    return {"messages": []}
                sid = sess_row[0]

            msg_rows = conn.execute(
                sa_text(
                    "SELECT role, content FROM copaw_chat_messages "
                    "WHERE session_id = :sid ORDER BY id ASC"
                ),
                {"sid": sid},
            ).fetchall()
        messages = [{"role": r[0], "content": r[1] or ""} for r in msg_rows]
        return {"messages": messages, "session_file": sid}
    except Exception as e:
        return {"messages": [], "error": str(e)}


def _fmt_dt(val):
    if val is None:
        return None
    if hasattr(val, "isoformat"):
        return val.isoformat()
    return str(val)


def _ensure_workspace_preview_auth(request: Request, storage_user_key: str) -> None:
    """When LCAgent JWT is configured, only the owner may read ``users/.../``."""
    if not _get_platform_key():
        return

    uid = getattr(request.state, "user", None)
    if uid is None:
        auth = request.headers.get("Authorization", "")
        uid = verify_lcagent_token(auth) if auth else None
    if uid is None:
        q_token = request.query_params.get("token") or request.query_params.get("_token")
        uid = verify_lcagent_token(q_token or "") if q_token else None
    if uid is None:
        raise HTTPException(status_code=401, detail="Unauthorized")
    su = str(storage_user_key).strip()
    jwt_uid = str(uid)
    if jwt_uid == su:
        return
    if su.startswith(jwt_uid + "/"):
        return
    raise HTTPException(status_code=403, detail="Forbidden")


@router.api_route(
    "/preview/{resource_path:path}",
    methods=["GET", "HEAD"],
    summary="Preview workspace file under users/.../workspaces/<ws>/...",
)
async def preview_workspace_file(
    request: Request,
    resource_path: str,
):
    """Map paths like ``users/<tenant>/.../workspaces/<ws>/<rel>`` from USERS_DIR.

    ``resource_path`` is the path after ``/preview/``, built by
    :func:`copaw.agents.tools.send_file._working_abspath_to_preview_url` from
    relative segments under ``USERS_DIR`` (flat or nested tenant dirs).

    Paths without ``/workspaces/`` use the legacy absolute-file behavior.
    """
    if _WORKSPACES not in resource_path:
        path = Path(resource_path)
        if not path.is_absolute():
            path = Path("/" + resource_path)
        path = path.resolve()
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Not found")
        return FileResponse(path, filename=path.name)
    head, tail = resource_path.split(_WORKSPACES, 1)
    head = head.strip().strip("/")
    tail = tail.strip().strip("/")
    if not head or not tail:
        raise HTTPException(status_code=404, detail="Not found")
    workspace_parts = tail.split("/", 1)
    workspace = workspace_parts[0]
    rel_path = workspace_parts[1] if len(workspace_parts) > 1 else ""

    _ensure_workspace_preview_auth(request, head.replace("\\", "/"))

    base = Path(USERS_DIR).resolve()
    user_root = base
    for seg in head.replace("\\", "/").split("/"):
        if not seg or seg in (".", ".."):
            raise HTTPException(status_code=404, detail="Not found")
        user_root = user_root / seg
    root = (user_root / "workspaces" / workspace).resolve()
    try:
        root.relative_to(base)
    except ValueError:
        raise HTTPException(status_code=404, detail="Not found") from None
    target = (root / rel_path).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise HTTPException(status_code=404, detail="Not found") from None
    if not target.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    media, _ = mimetypes.guess_type(str(target))
    return FileResponse(
        path=str(target),
        media_type=media or "application/octet-stream",
        filename=target.name,
        content_disposition_type="inline",
    )
