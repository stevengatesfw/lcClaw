# -*- coding: utf-8 -*-
# pylint: disable=redefined-outer-name,unused-argument
import asyncio
import mimetypes
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from agentscope_runtime.engine.app import AgentApp

from .runner import AgentRunner
from ..config import (  # pylint: disable=no-name-in-module
    load_config,
    update_last_dispatch,
    ConfigWatcher,
)
from ..config.utils import (
    copaw_storage_isolation_enabled,
    get_jobs_path,
    get_chats_path_for_user,
    get_config_path,
)
from ..constant import DOCS_ENABLED, LOG_LEVEL_ENV, CORS_ORIGINS, WORKING_DIR
from ..__version__ import __version__
from ..utils.logging import setup_logger, add_copaw_file_handler
from .channels import ChannelManager  # pylint: disable=no-name-in-module
from .channels.utils import make_process_from_runner
from .mcp import MCPClientManager, MCPConfigWatcher  # MCP hot-reload support
from .runner.repo.json_repo import JsonChatRepository
from .crons.repo.aggregating import AggregatingJobRepository
from .crons.repo.json_repo import JsonJobRepository
from .crons.manager import CronManager
from .runner.manager import ChatManager
from .routers import router as api_router
from .routers.voice import voice_router
from ..envs import load_envs_into_environ
from .auth import AgentProcessUserInjectMiddleware, UserIdContextMiddleware

# Apply log level on load so reload child process gets same level as CLI.
logger = setup_logger(os.environ.get(LOG_LEVEL_ENV, "info"))

# Ensure static assets are served with browser-compatible MIME types across
# platforms (notably Windows may miss .js/.mjs mappings).
mimetypes.init()
mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("application/javascript", ".mjs")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("application/wasm", ".wasm")

# Load persisted env vars into os.environ (single-tenant only).
# With storage isolation (LAZY_PLATFORM_KEY), envs are per-user via API.
if not copaw_storage_isolation_enabled():
    load_envs_into_environ()

runner = AgentRunner()

agent_app = AgentApp(
    app_name="Friday",
    app_description="A helpful assistant",
    runner=runner,
)


@asynccontextmanager
async def lifespan(
    app: FastAPI,
):  # pylint: disable=too-many-statements,too-many-branches
    add_copaw_file_handler(WORKING_DIR / "copaw.log")
    await runner.start()

    _storage_iso = copaw_storage_isolation_enabled()

    # --- MCP client manager init (root config; per-user MCP when isolated) ---
    config = load_config(get_config_path())
    mcp_manager = MCPClientManager()
    if not _storage_iso and hasattr(config, "mcp"):
        try:
            await mcp_manager.init_from_config(config.mcp)
            runner.set_mcp_manager(mcp_manager)
            logger.debug("MCP client manager initialized")
        except Exception:
            logger.exception("Failed to initialize MCP manager")
    else:
        runner.set_mcp_manager(None)
        if _storage_iso:
            logger.debug("Storage isolation: MCP is per-user (not global manager)")

    # --- channel connector init/start (from config.json) ---
    channel_manager = ChannelManager.from_config(
        process=make_process_from_runner(runner),
        config=config,
        on_last_dispatch=update_last_dispatch,
    )
    await channel_manager.start_all()

    # --- cron init/start ---
    if _storage_iso:
        job_repo: AggregatingJobRepository | JsonJobRepository = (
            AggregatingJobRepository(include_legacy=True)
        )
    else:
        job_repo = JsonJobRepository(get_jobs_path())
    cron_manager = CronManager(
        repo=job_repo,
        runner=runner,
        channel_manager=channel_manager,
        timezone="UTC",
        storage_isolation_enabled=_storage_iso,
    )
    await cron_manager.start()

    # --- chat manager init and connect to runner.session ---
    # Per-user ChatManager (always uses users/<user_id>/chats.json).
    _chat_manager_cache: dict = {}

    def _get_chat_manager_for_user(user_id: Optional[str]) -> ChatManager:
        """Return ChatManager for user. Always uses per-user repo."""
        cache_key = user_id or "default"
        if cache_key not in _chat_manager_cache:
            path = get_chats_path_for_user(user_id)
            _chat_manager_cache[cache_key] = ChatManager(
                repo=JsonChatRepository(path),
            )
        return _chat_manager_cache[cache_key]

    # Runner uses factory so agent/process can get per-user manager
    runner.set_chat_manager(_get_chat_manager_for_user)

    # --- config file watcher (channels + heartbeat hot-reload on change) ---
    config_watcher = ConfigWatcher(
        channel_manager=channel_manager,
        cron_manager=cron_manager,
        storage_isolation_enabled=_storage_iso,
    )
    await config_watcher.start()

    # --- MCP config watcher (auto-reload MCP clients on change) ---
    mcp_watcher = None
    if not _storage_iso and hasattr(config, "mcp"):
        try:
            mcp_watcher = MCPConfigWatcher(
                mcp_manager=mcp_manager,
                config_loader=load_config,
                config_path=get_config_path(),
            )
            await mcp_watcher.start()
            logger.debug("MCP config watcher started")
        except Exception:
            logger.exception("Failed to start MCP watcher")

    # expose to endpoints
    app.state.runner = runner
    app.state.channel_manager = channel_manager
    app.state.cron_manager = cron_manager
    app.state.chat_manager_factory = _get_chat_manager_for_user
    app.state.config_watcher = config_watcher
    app.state.mcp_manager = mcp_manager
    app.state.mcp_watcher = mcp_watcher

    _restart_task: asyncio.Task | None = None

    async def _restart_services() -> None:
        """Stop all managers, then rebuild from config (no exit).

        Single-flight: only one restart runs at a time. Concurrent or
        duplicate callers wait for the in-progress restart and return
        successfully. Uses asyncio.shield() so that when the caller
        (e.g. channel request) is cancelled, the restart task keeps
        running and does not propagate cancellation into deep task
        trees (avoids RecursionError on cancel).
        """
        # pylint: disable=too-many-statements
        nonlocal _restart_task
        # Caller task (in _local_tasks) must not be cancelled so it can
        # yield the final "Restart completed" message.
        restart_requester_task = asyncio.current_task()

        async def _run_then_clear() -> None:
            try:
                await _do_restart_services(
                    restart_requester_task=restart_requester_task,
                )
            finally:
                nonlocal _restart_task
                _restart_task = None

        if _restart_task is not None and not _restart_task.done():
            logger.info(
                "_restart_services: waiting for in-progress restart to finish",
            )
            await asyncio.shield(_restart_task)
            return
        if _restart_task is not None and _restart_task.done():
            _restart_task = None
        logger.info("_restart_services: starting restart")
        _restart_task = asyncio.create_task(_run_then_clear())
        await asyncio.shield(_restart_task)

    async def _teardown_new_stack(
        mcp_watcher=None,
        config_watcher=None,
        cron_mgr=None,
        ch_mgr=None,
        mcp_mgr=None,
    ) -> None:
        """Stop new stack in reverse start order (for rollback on failure)."""
        if mcp_watcher is not None:
            try:
                await mcp_watcher.stop()
            except Exception:
                logger.debug(
                    "rollback: mcp_watcher.stop failed",
                    exc_info=True,
                )
        if config_watcher is not None:
            try:
                await config_watcher.stop()
            except Exception:
                logger.debug(
                    "rollback: config_watcher.stop failed",
                    exc_info=True,
                )
        if cron_mgr is not None:
            try:
                await cron_mgr.stop()
            except Exception:
                logger.debug(
                    "rollback: cron_manager.stop failed",
                    exc_info=True,
                )
        if ch_mgr is not None:
            try:
                await ch_mgr.stop_all()
            except Exception:
                logger.debug(
                    "rollback: channel_manager.stop_all failed",
                    exc_info=True,
                )
        if mcp_mgr is not None:
            try:
                await mcp_mgr.close_all()
            except Exception:
                logger.debug(
                    "rollback: mcp_manager.close_all failed",
                    exc_info=True,
                )

    async def _do_restart_services(
        restart_requester_task: asyncio.Task | None = None,
    ) -> None:
        """Cancel in-flight agent requests first (so they can send error to
        channel), then stop old stack, then start new stack and swap.
        """
        # pylint: disable=too-many-statements
        try:
            config = load_config(get_config_path())
        except Exception:
            logger.exception("restart_services: load_config failed")
            return

        # 1) Cancel in-flight agent requests. Do not wait for them so the
        # console restart task never blocks (avoid deadlock when cancelled
        # task is slow to exit).
        local_tasks = getattr(agent_app, "_local_tasks", None)
        if local_tasks:
            to_cancel = [
                t
                for t in list(local_tasks.values())
                if t is not restart_requester_task and not t.done()
            ]
            for t in to_cancel:
                t.cancel()
            if to_cancel:
                logger.info(
                    "restart: cancelled %s in-flight task(s), not waiting",
                    len(to_cancel),
                )

        # 2) Stop old stack
        cfg_w = app.state.config_watcher
        mcp_w = getattr(app.state, "mcp_watcher", None)
        cron_mgr = app.state.cron_manager
        ch_mgr = app.state.channel_manager
        mcp_mgr = app.state.mcp_manager
        try:
            await cfg_w.stop()
        except Exception:
            logger.exception(
                "restart_services: old config_watcher.stop failed",
            )
        if mcp_w is not None:
            try:
                await mcp_w.stop()
            except Exception:
                logger.exception(
                    "restart_services: old mcp_watcher.stop failed",
                )
        try:
            await cron_mgr.stop()
        except Exception:
            logger.exception(
                "restart_services: old cron_manager.stop failed",
            )
        try:
            await ch_mgr.stop_all()
        except Exception:
            logger.exception(
                "restart_services: old channel_manager.stop_all failed",
            )
        if mcp_mgr is not None:
            try:
                await mcp_mgr.close_all()
            except Exception:
                logger.exception(
                    "restart_services: old mcp_manager.close_all failed",
                )
        try:
            await runner.shutdown_mcp_managers_cache()
        except Exception:
            logger.exception(
                "restart_services: runner.shutdown_mcp_managers_cache failed",
            )

        # 3) Build and start new stack
        _rs_iso = copaw_storage_isolation_enabled()
        new_mcp_manager = MCPClientManager()
        if not _rs_iso and hasattr(config, "mcp"):
            try:
                await new_mcp_manager.init_from_config(config.mcp)
            except Exception:
                logger.exception(
                    "restart_services: mcp init_from_config failed",
                )
                return

        new_channel_manager = ChannelManager.from_config(
            process=make_process_from_runner(runner),
            config=config,
            on_last_dispatch=update_last_dispatch,
        )
        try:
            await new_channel_manager.start_all()
        except Exception:
            logger.exception(
                "restart_services: channel_manager.start_all failed",
            )
            await _teardown_new_stack(mcp_mgr=new_mcp_manager)
            return

        if _rs_iso:
            new_job_repo = AggregatingJobRepository(include_legacy=True)
        else:
            new_job_repo = JsonJobRepository(get_jobs_path())
        new_cron_manager = CronManager(
            repo=new_job_repo,
            runner=runner,
            channel_manager=new_channel_manager,
            timezone="UTC",
            storage_isolation_enabled=_rs_iso,
        )
        try:
            await new_cron_manager.start()
        except Exception:
            logger.exception(
                "restart_services: cron_manager.start failed",
            )
            await _teardown_new_stack(
                ch_mgr=new_channel_manager,
                mcp_mgr=new_mcp_manager,
            )
            return

        new_config_watcher = ConfigWatcher(
            channel_manager=new_channel_manager,
            cron_manager=new_cron_manager,
            storage_isolation_enabled=_rs_iso,
        )
        try:
            await new_config_watcher.start()
        except Exception:
            logger.exception(
                "restart_services: config_watcher.start failed",
            )
            await _teardown_new_stack(
                cron_mgr=new_cron_manager,
                ch_mgr=new_channel_manager,
                mcp_mgr=new_mcp_manager,
            )
            return

        new_mcp_watcher = None
        if not _rs_iso and hasattr(config, "mcp"):
            try:
                new_mcp_watcher = MCPConfigWatcher(
                    mcp_manager=new_mcp_manager,
                    config_loader=load_config,
                    config_path=get_config_path(),
                )
                await new_mcp_watcher.start()
            except Exception:
                logger.exception(
                    "restart_services: mcp_watcher.start failed",
                )
                await _teardown_new_stack(
                    config_watcher=new_config_watcher,
                    cron_mgr=new_cron_manager,
                    ch_mgr=new_channel_manager,
                    mcp_mgr=new_mcp_manager,
                )
                return

        if not _rs_iso and hasattr(config, "mcp"):
            runner.set_mcp_manager(new_mcp_manager)
            app.state.mcp_manager = new_mcp_manager
            app.state.mcp_watcher = new_mcp_watcher
        else:
            runner.set_mcp_manager(None)
            app.state.mcp_manager = None
            app.state.mcp_watcher = None
        app.state.channel_manager = new_channel_manager
        app.state.cron_manager = new_cron_manager
        app.state.config_watcher = new_config_watcher
        logger.info("Daemon restart (in-process) completed: managers rebuilt")

    setattr(runner, "_restart_callback", _restart_services)

    try:
        yield
    finally:
        # Stop current app.state refs (post-restart instances if any)
        cfg_w = getattr(app.state, "config_watcher", None)
        mcp_w = getattr(app.state, "mcp_watcher", None)
        cron_mgr = getattr(app.state, "cron_manager", None)
        ch_mgr = getattr(app.state, "channel_manager", None)
        mcp_mgr = getattr(app.state, "mcp_manager", None)
        # stop order: watchers -> cron -> channels -> mcp -> runner
        if cfg_w is not None:
            try:
                await cfg_w.stop()
            except Exception:
                pass
        if mcp_w is not None:
            try:
                await mcp_w.stop()
            except Exception:
                pass
        if cron_mgr is not None:
            try:
                await cron_mgr.stop()
            except Exception:
                pass
        if ch_mgr is not None:
            try:
                await ch_mgr.stop_all()
            except Exception:
                pass
        if mcp_mgr is not None:
            try:
                await mcp_mgr.close_all()
            except Exception:
                pass
        await runner.stop()


app = FastAPI(
    lifespan=lifespan,
    docs_url="/docs" if DOCS_ENABLED else None,
    redoc_url="/redoc" if DOCS_ENABLED else None,
    openapi_url="/openapi.json" if DOCS_ENABLED else None,
)

# Agent process: inject user_id from JWT when LAZY_PLATFORM_KEY is set
app.add_middleware(AgentProcessUserInjectMiddleware)
# Set user_id in context from JWT for all requests (runner uses when request.user_id empty)
app.add_middleware(UserIdContextMiddleware)

# Apply CORS middleware if CORS_ORIGINS is set
if CORS_ORIGINS:
    origins = [o.strip() for o in CORS_ORIGINS.split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


# Console static dir: env, or copaw package data (console), or cwd.
_CONSOLE_STATIC_ENV = "COPAW_CONSOLE_STATIC_DIR"


def _resolve_console_static_dir() -> str:
    env_dir = os.environ.get(_CONSOLE_STATIC_ENV)
    if env_dir:
        candidate = Path(env_dir)
        if (candidate / "index.html").exists():
            return env_dir
        # lcClaw 控制台未构建时回退到默认 copaw 控制台
        logger.warning(
            "COPAW_CONSOLE_STATIC_DIR=%s 无 index.html，回退到默认控制台", env_dir
        )
    # 默认：copaw 包内 console（优先 venv 安装的包，含完整 console）
    candidates = []
    try:
        import copaw

        candidates.append(Path(copaw.__file__).resolve().parent / "console")
    except ImportError:
        pass
    candidates.append(Path(__file__).resolve().parent.parent / "console")
    # venv 中安装的 copaw 包（PYTHONPATH 优先时源码可能无 console）
    for site in __import__("site").getsitepackages():
        candidates.append(Path(site) / "copaw" / "console")
    for candidate in candidates:
        if candidate.is_dir() and (candidate / "index.html").exists():
            return str(candidate)
    cwd = Path(os.getcwd())
    for subdir in ("console/dist", "console_dist"):
        candidate = cwd / subdir
        if candidate.is_dir() and (candidate / "index.html").exists():
            return str(candidate)
    return str(cwd / "console" / "dist")


_CONSOLE_STATIC_DIR = _resolve_console_static_dir()
_CONSOLE_INDEX = (
    Path(_CONSOLE_STATIC_DIR) / "index.html" if _CONSOLE_STATIC_DIR else None
)
logger.info(f"STATIC_DIR: {_CONSOLE_STATIC_DIR}")


@app.get("/")
def read_root():
    if _CONSOLE_INDEX and _CONSOLE_INDEX.exists():
        return FileResponse(_CONSOLE_INDEX)
    return {
        "message": (
            "CoPaw Web Console is not available. "
            "If you installed CoPaw from source code, please run "
            "`npm ci && npm run build` in CoPaw's `console/` "
            "directory, and restart CoPaw to enable the web console."
        ),
    }


@app.get("/api/version")
def get_version():
    """Return the current CoPaw version."""
    return {"version": __version__}


app.include_router(api_router, prefix="/api")

app.include_router(
    agent_app.router,
    prefix="/api/agent",
    tags=["agent"],
)

# Voice channel: Twilio-facing endpoints at root level (not under /api/).
# POST /voice/incoming, WS /voice/ws, POST /voice/status-callback
app.include_router(voice_router, tags=["voice"])

# Mount console: root static files (logo.png etc.) then assets, then SPA
# fallback.
if os.path.isdir(_CONSOLE_STATIC_DIR):
    _console_path = Path(_CONSOLE_STATIC_DIR)

    @app.get("/logo.png")
    def _console_logo():
        f = _console_path / "logo.png"
        if f.is_file():
            return FileResponse(f, media_type="image/png")

        raise HTTPException(status_code=404, detail="Not Found")

    @app.get("/copaw-symbol.svg")
    def _console_icon():
        f = _console_path / "copaw-symbol.svg"
        if f.is_file():
            return FileResponse(f, media_type="image/svg+xml")

        raise HTTPException(status_code=404, detail="Not Found")

    _assets_dir = _console_path / "assets"
    if _assets_dir.is_dir():
        app.mount(
            "/assets",
            StaticFiles(directory=str(_assets_dir)),
            name="assets",
        )

    @app.get("/{full_path:path}")
    def _console_spa(full_path: str):
        if _CONSOLE_INDEX and _CONSOLE_INDEX.exists():
            return FileResponse(_CONSOLE_INDEX)

        raise HTTPException(status_code=404, detail="Not Found")
