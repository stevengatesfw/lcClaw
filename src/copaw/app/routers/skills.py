# -*- coding: utf-8 -*-
import logging
from typing import Any, Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from ...agents.skills_manager import (
    SkillService,
    SkillInfo,
    list_available_skills,
    get_user_enabled_skills,
    add_user_enabled_skill,
    remove_user_enabled_skill,
)
from ...agents.skills_hub import (
    search_hub_skills,
    install_skill_from_hub,
)
from ..auth import get_current_user_id
from ...context import get_context_user_id


logger = logging.getLogger(__name__)


class SkillSpec(SkillInfo):
    enabled: bool = False


class CreateSkillRequest(BaseModel):
    name: str = Field(..., description="Skill name")
    content: str = Field(..., description="Skill content (SKILL.md)")
    references: dict[str, Any] | None = Field(
        None,
        description="Optional tree structure for references/. "
        "Can be flat {filename: content} or nested "
        "{dirname: {filename: content}}",
    )
    scripts: dict[str, Any] | None = Field(
        None,
        description="Optional tree structure for scripts/. "
        "Can be flat {filename: content} or nested "
        "{dirname: {filename: content}}",
    )


class HubSkillSpec(BaseModel):
    slug: str
    name: str
    description: str = ""
    version: str = ""
    source_url: str = ""


class HubInstallRequest(BaseModel):
    bundle_url: str = Field(..., description="Skill URL")
    version: str = Field(default="", description="Optional version tag")
    enable: bool = Field(default=True, description="Enable after import")
    overwrite: bool = Field(
        default=False,
        description="Overwrite existing customized skill",
    )


router = APIRouter(prefix="/skills", tags=["skills"])


def _resolve_user_id(user_id: Optional[str]) -> str:
    """Resolve user_id from dependency or context (JWT)."""
    return user_id or get_context_user_id() or "default"


@router.get("")
async def list_skills(
    user_id: Optional[str] = Depends(get_current_user_id),
) -> list[SkillSpec]:
    uid = _resolve_user_id(user_id)
    all_skills = SkillService.list_all_skills(user_id=uid)

    if uid != "default":
        available_set = get_user_enabled_skills(uid)
    else:
        available_set = set(list_available_skills(uid))

    skills_spec = []
    for skill in all_skills:
        skills_spec.append(
            SkillSpec(
                name=skill.name,
                content=skill.content,
                source=skill.source,
                path=skill.path,
                references=skill.references,
                scripts=skill.scripts,
                enabled=skill.name in available_set,
            ),
        )
    return skills_spec


@router.get("/available")
async def get_available_skills(
    user_id: Optional[str] = Depends(get_current_user_id),
) -> list[SkillSpec]:
    uid = _resolve_user_id(user_id)
    available_skills = SkillService.list_available_skills(user_id=uid)
    skills_spec = []
    for skill in available_skills:
        skills_spec.append(
            SkillSpec(
                name=skill.name,
                content=skill.content,
                source=skill.source,
                path=skill.path,
                references=skill.references,
                scripts=skill.scripts,
                enabled=True,
            ),
        )
    return skills_spec


@router.get("/hub/search")
async def search_hub(
    q: str = "",
    limit: int = 20,
) -> list[HubSkillSpec]:
    results = search_hub_skills(q, limit=limit)
    return [
        HubSkillSpec(
            slug=item.slug,
            name=item.name,
            description=item.description,
            version=item.version,
            source_url=item.source_url,
        )
        for item in results
    ]


def _github_token_hint(bundle_url: str) -> str:
    """Hint to set GITHUB_TOKEN when URL is from GitHub/skills.sh."""
    if not bundle_url:
        return ""
    lower = bundle_url.lower()
    if "skills.sh" in lower or "github.com" in lower:
        return " Tip: set GITHUB_TOKEN (or GH_TOKEN) to avoid rate limits."
    return ""


@router.post("/hub/install")
async def install_from_hub(
    request: HubInstallRequest,
    user_id: Optional[str] = Depends(get_current_user_id),
):
    uid = _resolve_user_id(user_id)
    try:
        result = install_skill_from_hub(
            bundle_url=request.bundle_url,
            version=request.version,
            enable=request.enable,
            overwrite=request.overwrite,
            user_id=uid,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:
        # Upstream hub is flaky/rate-limited sometimes; surface as bad gateway.
        detail = str(e) + _github_token_hint(request.bundle_url)
        logger.exception(
            "Skill hub install failed (upstream/rate limit): %s",
            e,
        )
        raise HTTPException(status_code=502, detail=detail) from e
    except Exception as e:
        detail = f"Skill hub import failed: {e}" + _github_token_hint(
            request.bundle_url,
        )
        logger.exception("Skill hub import failed: %s", e)
        raise HTTPException(status_code=502, detail=detail) from e
    return {
        "installed": True,
        "name": result.name,
        "enabled": result.enabled,
        "source_url": result.source_url,
    }


@router.post("/batch-disable")
async def batch_disable_skills(
    skill_name: list[str],
    user_id: Optional[str] = Depends(get_current_user_id),
) -> None:
    uid = _resolve_user_id(user_id)
    if uid != "default":
        for name in skill_name:
            remove_user_enabled_skill(uid, name)
    else:
        for name in skill_name:
            SkillService.disable_skill(name, user_id=uid)


@router.post("/batch-enable")
async def batch_enable_skills(
    skill_name: list[str],
    user_id: Optional[str] = Depends(get_current_user_id),
) -> None:
    uid = _resolve_user_id(user_id)
    if uid != "default":
        for name in skill_name:
            add_user_enabled_skill(uid, name)
    else:
        for name in skill_name:
            SkillService.enable_skill(name, user_id=uid)


@router.post("")
async def create_skill(
    request: CreateSkillRequest,
    user_id: Optional[str] = Depends(get_current_user_id),
):
    uid = _resolve_user_id(user_id)
    result = SkillService.create_skill(
        name=request.name,
        content=request.content,
        references=request.references,
        scripts=request.scripts,
        user_id=uid,
    )
    return {"created": result}


@router.post("/{skill_name}/disable")
async def disable_skill(
    skill_name: str,
    user_id: Optional[str] = Depends(get_current_user_id),
):
    uid = _resolve_user_id(user_id)
    if uid != "default":
        remove_user_enabled_skill(uid, skill_name)
        return {"disabled": True}
    result = SkillService.disable_skill(skill_name, user_id=uid)
    return {"disabled": result}


@router.post("/{skill_name}/enable")
async def enable_skill(
    skill_name: str,
    user_id: Optional[str] = Depends(get_current_user_id),
):
    uid = _resolve_user_id(user_id)
    if uid != "default":
        add_user_enabled_skill(uid, skill_name)
        return {"enabled": True}
    result = SkillService.enable_skill(skill_name, user_id=uid)
    return {"enabled": result}


@router.delete("/{skill_name}")
async def delete_skill(
    skill_name: str,
    user_id: Optional[str] = Depends(get_current_user_id),
):
    """Delete a skill from customized_skills directory permanently.

    This only deletes skills from customized_skills directory.
    Built-in skills cannot be deleted.
    """
    uid = _resolve_user_id(user_id)
    result = SkillService.delete_skill(skill_name, user_id=uid)
    return {"deleted": result}


@router.get("/{skill_name}/files/{source}/{file_path:path}")
async def load_skill_file(
    skill_name: str,
    source: str,
    file_path: str,
    user_id: Optional[str] = Depends(get_current_user_id),
):
    """Load a specific file from a skill's references or scripts directory.

    Args:
        skill_name: Name of the skill
        source: Source directory ("builtin" or "customized")
        file_path: Path relative to skill directory, must start with
                   "references/" or "scripts/"

    Returns:
        File content as string, or None if not found

    Example:
        GET /skills/my_skill/files/customized/references/doc.md
        GET /skills/builtin_skill/files/builtin/scripts/utils/helper.py
    """
    uid = _resolve_user_id(user_id)
    content = SkillService.load_skill_file(
        skill_name=skill_name,
        file_path=file_path,
        source=source,
        user_id=uid,
    )
    return {"content": content}
