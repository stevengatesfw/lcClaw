# -*- coding: utf-8 -*-
import mimetypes
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from starlette.responses import FileResponse

from ..auth import _get_platform_key, verify_lcagent_token
from ...constant import USERS_DIR

router = APIRouter(prefix="/files", tags=["files"])

_WORKSPACES = "/workspaces/"


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
