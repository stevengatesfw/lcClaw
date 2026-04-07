# -*- coding: utf-8 -*-
"""Aggregate cron jobs from legacy jobs.json and per-user jobs.json."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from ....config.utils import get_jobs_path, get_jobs_path_for_user

from ..models import CronJobSpec, JobsFile
from .base import BaseJobRepository
from .json_repo import JsonJobRepository

logger = logging.getLogger(__name__)


class AggregatingJobRepository(BaseJobRepository):
    """Merge legacy root jobs.json and every ``users/*/jobs.json``."""

    META_OWNER_KEY = "_copaw_user_id"

    def __init__(self, *, include_legacy: bool = True) -> None:
        self._include_legacy = include_legacy

    def _user_dirs_with_jobs(self) -> list[Path]:
        from ....constant import USERS_DIR, JOBS_FILE

        paths: list[Path] = []
        base = USERS_DIR
        if not base.is_dir():
            return paths
        for child in sorted(base.iterdir()):
            if not child.is_dir():
                continue
            jp = child / JOBS_FILE
            if jp.is_file():
                paths.append(jp)
        return paths

    def _all_job_file_paths(self) -> list[Path]:
        paths: list[Path] = []
        if self._include_legacy:
            lp = get_jobs_path()
            if lp.is_file():
                paths.append(lp)
        paths.extend(self._user_dirs_with_jobs())
        return paths

    async def load(self) -> JobsFile:
        merged: list[CronJobSpec] = []
        seen: set[str] = set()
        for path in self._all_job_file_paths():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                jf = JobsFile.model_validate(data)
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                logger.warning("Skip jobs file %s: %s", path, exc)
                continue
            for job in jf.jobs:
                if job.id in seen:
                    logger.warning(
                        "Duplicate job id %s in %s, skipping duplicate",
                        job.id,
                        path,
                    )
                    continue
                seen.add(job.id)
                merged.append(job)
        return JobsFile(version=1, jobs=merged)

    async def save(self, jobs_file: JobsFile) -> None:
        raise NotImplementedError(
            "AggregatingJobRepository does not support bulk save; "
            "use upsert_job/delete_job.",
        )

    def _partition_path_for_job(self, job: CronJobSpec) -> Path:
        uid = job.meta.get(self.META_OWNER_KEY)
        if isinstance(uid, str) and uid.strip():
            return get_jobs_path_for_user(uid)
        return get_jobs_path()

    async def upsert_job(self, spec: CronJobSpec) -> None:
        path = self._partition_path_for_job(spec)
        canonical = path.resolve()
        repo = JsonJobRepository(path)
        await repo.upsert_job(spec)
        # load() merges legacy-first; drop same id elsewhere (no shadow).
        for other in self._all_job_file_paths():
            if other.resolve() == canonical:
                continue
            if await JsonJobRepository(other).delete_job(spec.id):
                logger.debug(
                    "Removed stale job %s from %s after upsert to %s",
                    spec.id,
                    other,
                    path,
                )

    async def delete_job(self, job_id: str) -> bool:
        deleted_any = False
        for path in self._all_job_file_paths():
            if await JsonJobRepository(path).delete_job(job_id):
                deleted_any = True
        return deleted_any
