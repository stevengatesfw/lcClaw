# -*- coding: utf-8 -*-
"""Workspace API – download / upload workspace as a zip (global or per-user)."""

from __future__ import annotations

import io
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse

from ...config.utils import copaw_storage_isolation_enabled, get_user_working_dir
from ...constant import WORKING_DIR
from ..auth import get_current_user_id_required

router = APIRouter(prefix="/workspace", tags=["workspace"])


def _workspace_root(uid: str) -> Path:
    """Directory to pack or merge into."""
    if copaw_storage_isolation_enabled():
        if not uid:
            raise HTTPException(
                status_code=401,
                detail="请先登录 LCAgent 后再使用 CoPaw。",
            )
        return get_user_working_dir(uid)
    return WORKING_DIR


def _zip_directory(root: Path) -> io.BytesIO:
    """Create an in-memory zip archive of *root* and return the buffer.

    All files **and** directories (including empty ones) are included.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if root.is_dir():
            for entry in sorted(root.rglob("*")):
                arcname = entry.relative_to(root).as_posix()
                if entry.is_file():
                    zf.write(entry, arcname)
                elif entry.is_dir():
                    zf.write(entry, arcname + "/")
    buf.seek(0)
    return buf


def _validate_zip_data(data: bytes, sandbox_root: Path) -> None:
    """Ensure *data* is a valid zip without path-traversal entries."""
    if not zipfile.is_zipfile(io.BytesIO(data)):
        raise HTTPException(
            status_code=400,
            detail="Uploaded file is not a valid zip archive",
        )
    base = sandbox_root.resolve()
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for name in zf.namelist():
            resolved = (sandbox_root / name).resolve()
            if not str(resolved).startswith(str(base)):
                raise HTTPException(
                    status_code=400,
                    detail=f"Zip contains unsafe path: {name}",
                )


@router.get(
    "/download",
    summary="Download workspace as zip",
    description=(
        "Package WORKING_DIR (or the current user's directory when "
        "LAZY_PLATFORM_KEY is set) into a zip archive."
    ),
    responses={
        200: {
            "content": {"application/zip": {}},
            "description": "Zip archive of workspace root",
        },
    },
)
async def download_workspace(
    uid: str = Depends(get_current_user_id_required),
):
    """Stream workspace root as a zip file."""
    root = _workspace_root(uid)
    root.mkdir(parents=True, exist_ok=True)

    buf = _zip_directory(root)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"copaw_workspace_{timestamp}.zip"

    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


@router.post(
    "/upload",
    response_model=dict,
    summary="Upload zip and merge into workspace",
    description=(
        "Upload a zip archive. Paths are merged into WORKING_DIR or the "
        "current user's directory when LAZY_PLATFORM_KEY is set."
    ),
)
async def upload_workspace(
    file: UploadFile = File(
        ...,
        description="Zip archive to merge into workspace",
    ),
    uid: str = Depends(get_current_user_id_required),
) -> dict:
    """Merge uploaded zip contents into workspace root (overwrite, do not clear)."""
    if file.content_type and file.content_type not in (
        "application/zip",
        "application/x-zip-compressed",
        "application/octet-stream",
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Expected a zip file, got content-type: {file.content_type}"
            ),
        )

    dest_root = _workspace_root(uid)

    data = await file.read()
    _validate_zip_data(data, dest_root)

    tmp_dir = None
    try:
        tmp_dir = Path(tempfile.mkdtemp(prefix="copaw_upload_"))
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            zf.extractall(tmp_dir)

        top_entries = list(tmp_dir.iterdir())
        extract_root = tmp_dir
        if len(top_entries) == 1 and top_entries[0].is_dir():
            extract_root = top_entries[0]

        dest_root.mkdir(parents=True, exist_ok=True)

        for item in extract_root.iterdir():
            dest = dest_root / item.name
            if item.is_file():
                shutil.copy2(item, dest)
            else:
                if dest.exists() and dest.is_file():
                    dest.unlink()
                shutil.copytree(item, dest, dirs_exist_ok=True)

        return {"success": True}

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to merge workspace: {exc}",
        ) from exc
    finally:
        if tmp_dir and tmp_dir.is_dir():
            shutil.rmtree(tmp_dir, ignore_errors=True)
