# -*- coding: utf-8 -*-
# pylint: disable=unused-argument too-many-branches too-many-statements
import asyncio
import json
import logging
from pathlib import Path

from agentscope.pipeline import stream_printing_messages
from agentscope.tool import Toolkit
from agentscope_runtime.engine.runner import Runner
from agentscope_runtime.engine.schemas.agent_schemas import AgentRequest
from dotenv import load_dotenv

from .command_dispatch import (
    _get_last_user_text,
    _is_command,
    run_command_path,
)
from .query_error_dump import write_query_error_dump
from .session import SafeJSONSession
from .utils import build_env_context
from ..channels.schema import DEFAULT_CHANNEL
from ...agents.memory import MemoryManager
from ...agents.model_factory import create_model_and_formatter
from ...agents.react_agent import CoPawAgent
from ...agents.tools import read_file, write_file, edit_file
from ...agents.utils.token_counting import _get_token_counter
from ...config import load_config
from ...config.utils import (
    copaw_storage_isolation_enabled,
    get_effective_config_path_for_runner,
    get_sessions_dir_for_user,
    get_user_working_dir,
)
from ...context import set_current_working_dir, reset_current_working_dir, get_context_user_id
from ..mcp import MCPClientManager
from ...constant import MEMORY_COMPACT_RATIO, WORKING_DIR

logger = logging.getLogger(__name__)


class AgentRunner(Runner):
    def __init__(self) -> None:
        super().__init__()
        self.framework_type = "agentscope"
        self._chat_manager = None  # ChatManager instance (when not using factory)
        self._chat_manager_factory = None  # Callable[[str], ChatManager] for per-user
        self._mcp_manager = None  # root MCP when not storage-isolated
        self._mcp_managers_cache: dict[str, MCPClientManager] = {}
        self._mcp_managers_cache_lock = asyncio.Lock()
        self._memory_manager_cache: dict[str, MemoryManager] = {}

    def set_chat_manager(self, chat_manager_or_factory):
        """Set chat manager for auto-registration.

        Args:
            chat_manager_or_factory: ChatManager instance, or callable(user_id) -> ChatManager
        """
        if callable(chat_manager_or_factory):
            self._chat_manager_factory = chat_manager_or_factory
            self._chat_manager = None
        else:
            self._chat_manager_factory = None
            self._chat_manager = chat_manager_or_factory

    def _get_chat_manager(self, user_id: str | None):
        """Resolve ChatManager for user_id (factory or single instance)."""
        if self._chat_manager_factory is not None:
            return self._chat_manager_factory(user_id or "")
        return self._chat_manager

    async def _get_memory_manager(self, user_id: str | None) -> MemoryManager | None:
        """Resolve MemoryManager for user_id (per-user when isolation enabled).

        Lazy-creates and caches MemoryManager per user. Each instance uses
        users/<user_id> as working_dir when isolation is enabled.
        """
        cache_key = user_id or "default"
        if cache_key in self._memory_manager_cache:
            return self._memory_manager_cache[cache_key]
        try:
            cfg_path = get_effective_config_path_for_runner(user_id)
            config = load_config(cfg_path)
            max_input_length = config.agents.running.max_input_length
            chat_model, formatter = create_model_and_formatter()
            token_counter = _get_token_counter()
            toolkit = Toolkit()
            toolkit.register_tool_function(read_file)
            toolkit.register_tool_function(write_file)
            toolkit.register_tool_function(edit_file)
            working_dir = str(get_user_working_dir(user_id))
            mgr = MemoryManager(
                working_dir=working_dir,
                chat_model=chat_model,
                formatter=formatter,
                token_counter=token_counter,
                toolkit=toolkit,
                max_input_length=max_input_length,
                memory_compact_ratio=MEMORY_COMPACT_RATIO,
            )
            await mgr.start()
            self._memory_manager_cache[cache_key] = mgr
            return mgr
        except Exception as e:
            logger.exception("MemoryManager create for user %s failed: %s", cache_key, e)
            return None

    def set_mcp_manager(self, mcp_manager):
        """Set MCP client manager for hot-reload support.

        Args:
            mcp_manager: MCPClientManager instance
        """
        self._mcp_manager = mcp_manager

    async def shutdown_mcp_managers_cache(self) -> None:
        """Close per-user MCP managers (storage isolation)."""
        async with self._mcp_managers_cache_lock:
            items = list(self._mcp_managers_cache.items())
            self._mcp_managers_cache.clear()
        for key, mgr in items:
            try:
                await mgr.close_all()
            except Exception as e:  # pylint: disable=broad-except
                logger.warning(
                    "MCP manager close for user %s failed: %s",
                    key,
                    e,
                )

    async def invalidate_mcp_cache_for_user(self, user_id: str) -> None:
        """Reload MCP on next query after user config change."""
        uid = user_id or "default"
        async with self._mcp_managers_cache_lock:
            mgr = self._mcp_managers_cache.pop(uid, None)
        if mgr is not None:
            try:
                await mgr.close_all()
            except Exception as e:  # pylint: disable=broad-except
                logger.warning(
                    "invalidate_mcp_cache_for_user close failed: %s",
                    e,
                )

    async def _get_mcp_clients_for_user(self, user_id: str | None) -> list:
        """Root manager when single-tenant; per-user lazy MCP when isolated."""
        if not copaw_storage_isolation_enabled():
            if self._mcp_manager is not None:
                return await self._mcp_manager.get_clients()
            return []
        uid = user_id or "default"
        mgr = self._mcp_managers_cache.get(uid)
        if mgr is None:
            async with self._mcp_managers_cache_lock:
                mgr = self._mcp_managers_cache.get(uid)
                if mgr is None:
                    cfg_path = get_effective_config_path_for_runner(user_id)
                    cfg = load_config(cfg_path)
                    mgr = MCPClientManager()
                    if hasattr(cfg, "mcp"):
                        try:
                            await mgr.init_from_config(cfg.mcp)
                        except Exception:
                            logger.exception("MCP init failed for user %s", uid)
                    self._mcp_managers_cache[uid] = mgr
        return await mgr.get_clients()

    async def query_handler(
        self,
        msgs,
        request: AgentRequest = None,
        **kwargs,
    ):
        """
        Handle agent query.
        """
        # Command path: do not create agent; yield from run_command_path
        query = _get_last_user_text(msgs)
        if query and _is_command(query):
            logger.info("Command path: %s", query.strip()[:50])
            async for msg, last in run_command_path(request, msgs, self):
                yield msg, last
            return

        agent = None
        chat = None
        mgr = None
        session_state_loaded = False
        try:
            session_id = request.session_id
            # Prefer JWT context (set by UserIdContextMiddleware) over body; body may not be updated by middleware in some stacks
            user_id = get_context_user_id() or getattr(request, "user_id", None)
            channel = getattr(request, "channel", DEFAULT_CHANNEL)

            logger.info(
                "Handle agent query:\n%s",
                json.dumps(
                    {
                        "session_id": session_id,
                        "user_id": user_id,
                        "channel": channel,
                        "msgs_len": len(msgs) if msgs else 0,
                        "msgs_str": str(msgs)[:300] + "...",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )

            user_working_dir = get_user_working_dir(user_id)
            set_current_working_dir(user_working_dir)
            env_context = build_env_context(
                session_id=session_id,
                user_id=user_id,
                channel=channel,
                working_dir=str(user_working_dir),
            )

            mcp_clients = await self._get_mcp_clients_for_user(user_id)

            memory_manager = await self._get_memory_manager(user_id)

            cfg_path = get_effective_config_path_for_runner(user_id)
            config = load_config(cfg_path)
            max_iters = config.agents.running.max_iters
            max_input_length = config.agents.running.max_input_length

            agent = CoPawAgent(
                env_context=env_context,
                mcp_clients=mcp_clients,
                memory_manager=memory_manager,
                max_iters=max_iters,
                max_input_length=max_input_length,
            )
            await agent.register_mcp_clients()
            agent.set_console_output_enabled(enabled=False)

            logger.debug(
                f"Agent Query msgs {msgs}",
            )

            name = "New Chat"
            if len(msgs) > 0:
                content = msgs[0].get_text_content()
                if content:
                    name = msgs[0].get_text_content()[:10]
                else:
                    name = "Media Message"

            mgr = self._get_chat_manager(user_id)
            if mgr is not None:
                chat = await mgr.get_or_create_chat(
                    session_id,
                    user_id,
                    channel,
                    name=name,
                )

            try:
                await self.session.load_session_state(
                    session_id=session_id,
                    user_id=user_id,
                    agent=agent,
                )
            except KeyError as e:
                logger.warning(
                    "load_session_state skipped (state schema mismatch): %s; "
                    "will save fresh state on completion to recover file",
                    e,
                )
            session_state_loaded = True

            # Rebuild system prompt so it always reflects the latest
            # AGENTS.md / SOUL.md / PROFILE.md, not the stale one saved
            # in the session state.
            agent.rebuild_sys_prompt()

            async for msg, last in stream_printing_messages(
                agents=[agent],
                coroutine_task=agent(msgs),
            ):
                yield msg, last

        except asyncio.CancelledError as exc:
            logger.info(f"query_handler: {session_id} cancelled!")
            if agent is not None:
                await agent.interrupt()
            raise RuntimeError("Task has been cancelled!") from exc
        except Exception as e:
            debug_dump_path = write_query_error_dump(
                request=request,
                exc=e,
                locals_=locals(),
            )
            path_hint = (
                f"\n(Details:  {debug_dump_path})" if debug_dump_path else ""
            )
            logger.exception(f"Error in query handler: {e}{path_hint}")
            if debug_dump_path:
                setattr(e, "debug_dump_path", debug_dump_path)
                if hasattr(e, "add_note"):
                    e.add_note(
                        f"(Details:  {debug_dump_path})",
                    )
                suffix = f"\n(Details:  {debug_dump_path})"
                e.args = (
                    (f"{e.args[0]}{suffix}" if e.args else suffix.strip()),
                ) + e.args[1:]
            raise
        finally:
            reset_current_working_dir()
            if agent is not None and session_state_loaded:
                await self.session.save_session_state(
                    session_id=session_id,
                    user_id=user_id,
                    agent=agent,
                )

            if mgr is not None and chat is not None:
                await mgr.update_chat(chat)

    async def init_handler(self, *args, **kwargs):
        """
        Init handler.
        """
        # Load environment variables from .env file
        env_path = Path(__file__).resolve().parents[4] / ".env"
        if env_path.exists():
            load_dotenv(env_path)
            logger.debug(f"Loaded environment variables from {env_path}")
        else:
            logger.debug(
                f".env file not found at {env_path}, "
                "using existing environment variables",
            )

        session_dir = str(get_sessions_dir_for_user(None))
        self.session = SafeJSONSession(save_dir=session_dir)

        # MemoryManager is now lazy-created per user in query_handler

    async def shutdown_handler(self, *args, **kwargs):
        """
        Shutdown handler.
        """
        for cache_key, mgr in list(self._memory_manager_cache.items()):
            try:
                await mgr.close()
            except Exception as e:
                logger.warning(
                    "MemoryManager close for user %s failed: %s", cache_key, e
                )
        self._memory_manager_cache.clear()
        await self.shutdown_mcp_managers_cache()
