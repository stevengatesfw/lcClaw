# -*- coding: utf-8 -*-
# pylint: disable=redefined-outer-name
"""Unit tests for workspace file preview/delete (/api/files/preview)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from copaw.app.routers.files import router

app = FastAPI()
app.include_router(router, prefix="/api")


@pytest.fixture
def api_client():
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


def _workspace_file(users_dir: Path, uid: str, project: str, name: str, content: str = "hello") -> Path:
    path = users_dir / uid / "workspaces" / "default" / project / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


async def test_delete_workspace_file(api_client, tmp_path: Path):
    users_dir = tmp_path / "users"
    target = _workspace_file(users_dir, "u1", "demo", "out.md")
    with (
        patch("copaw.app.routers.files.USERS_DIR", users_dir),
        patch("copaw.app.routers.files._get_platform_key", return_value=None),
    ):
        async with api_client:
            resp = await api_client.delete(
                "/api/files/preview/u1/workspaces/default/demo/out.md"
            )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True
    assert not target.exists()


async def test_delete_user_root_file(api_client, tmp_path: Path):
    users_dir = tmp_path / "users"
    target = users_dir / "u1" / "note.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("note", encoding="utf-8")
    with (
        patch("copaw.app.routers.files.USERS_DIR", users_dir),
        patch("copaw.app.routers.files._get_platform_key", return_value=None),
    ):
        async with api_client:
            resp = await api_client.delete("/api/files/preview/u1/note.txt")
    assert resp.status_code == 200, resp.text
    assert not target.exists()


async def test_delete_missing_file_returns_404(api_client, tmp_path: Path):
    users_dir = tmp_path / "users"
    users_dir.mkdir(parents=True, exist_ok=True)
    with (
        patch("copaw.app.routers.files.USERS_DIR", users_dir),
        patch("copaw.app.routers.files._get_platform_key", return_value=None),
    ):
        async with api_client:
            resp = await api_client.delete(
                "/api/files/preview/u1/workspaces/default/demo/missing.md"
            )
    assert resp.status_code == 404


async def test_get_workspace_file_still_works(api_client, tmp_path: Path):
    users_dir = tmp_path / "users"
    _workspace_file(users_dir, "u1", "demo", "out.md", "preview-me")
    with (
        patch("copaw.app.routers.files.USERS_DIR", users_dir),
        patch("copaw.app.routers.files._get_platform_key", return_value=None),
    ):
        async with api_client:
            resp = await api_client.get(
                "/api/files/preview/u1/workspaces/default/demo/out.md"
            )
    assert resp.status_code == 200, resp.text
    assert resp.text == "preview-me"
