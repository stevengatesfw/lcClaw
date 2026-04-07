# -*- coding: utf-8 -*-
# pylint: disable=unused-argument too-many-branches too-many-statements
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from agentscope.message import Msg, TextBlock
from agentscope.pipeline import stream_printing_messages
from agentscope_runtime.engine.runner import Runner
from agentscope_runtime.engine.schemas.agent_schemas import AgentRequest
from agentscope_runtime.engine.schemas.exception import AgentException
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
from ...agents.react_agent import CoPawAgent
from ...agents.memory import ReMeLightMemoryManager
from ...config import load_config
from ...config.config import load_agent_config
from ...config.utils import (
    copaw_storage_isolation_enabled,
    get_effective_config_path_for_runner,
    get_user_working_dir,
)
from ...constant import WORKING_DIR
from ...constant import TOOL_GUARD_APPROVAL_TIMEOUT_SECONDS
from ...context import (
    get_context_user_id,
    get_process_request_meta,
    reset_current_working_dir,
    set_current_working_dir,
)
from ...providers.models import ResolvedModelConfig
from ...security.tool_guard.approval import ApprovalDecision
from ...security.tool_guard.models import TOOL_GUARD_DENIED_MARK
from ..mcp import MCPClientManager

if TYPE_CHECKING:
    from ...agents.memory import BaseMemoryManager

logger = logging.getLogger(__name__)

_APPROVE_EXACT = frozenset(
    {
        "approve",
        "/approve",
        "/daemon approve",
    },
)


def _is_approval(text: str) -> bool:
    """Return True only when *text* is exactly ``approve``,
    ``/approve``, or ``/daemon approve`` (case-insensitive).

    Leading/trailing whitespace and blank lines are stripped before
    comparison.  Everything else is treated as denial.
    """
    normalized = " ".join(text.split()).lower()
    return normalized in _APPROVE_EXACT


def _llm_cfg_from_process_meta() -> Optional[ResolvedModelConfig]:
    """Build ResolvedModelConfig from agent/process meta (LCAgent proxy)."""
    meta = get_process_request_meta()
    raw = meta.get("lcagent_resolved_llm")
    if not isinstance(raw, dict):
        return None
    try:
        return ResolvedModelConfig.model_validate(raw)
    except Exception:
        logger.warning("Invalid lcagent_resolved_llm in meta", exc_info=True)
        return None


def _meta_bool(value, default: bool = False) -> bool:
    """Parse bool-like values from request meta."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return default


def _feature_flags_from_process_meta() -> tuple[bool, bool]:
    """Read lcagent feature switches from agent/process meta.

    Returns:
        tuple[bool, bool]: (enable_agent, enable_skills)
    """
    meta = get_process_request_meta()
    enable_agent = _meta_bool(meta.get("lcagent_enable_agent"), default=False)
    enable_skills = _meta_bool(
        meta.get("lcagent_enable_skills"), default=False
    )
    return enable_agent, enable_skills


class AgentRunner(Runner):
    def __init__(
        self,
        agent_id: str = "default",
        workspace_dir: Path | None = None,
        task_tracker: Any | None = None,
    ) -> None:
        super().__init__()
        self.framework_type = "agentscope"
        self.agent_id = agent_id
        self.workspace_dir = workspace_dir
        self._chat_manager = None
        self._chat_manager_factory = None
        self._mcp_manager = None
        self._mcp_managers_cache: dict[str, MCPClientManager] = {}
        self._mcp_managers_cache_lock = asyncio.Lock()
        self._memory_manager_cache: dict[tuple, ReMeLightMemoryManager] = {}
        self._request_llm_cfg_override: Optional[ResolvedModelConfig] = None
        self._workspace: Any = None
        self.memory_manager: BaseMemoryManager | None = None
        self._task_tracker = task_tracker

    def _memory_manager_cache_key(self, user_id: str | None) -> tuple:
        """Include LCAgent llm override so cache matches active home model."""
        uid = user_id or "default"
        o = self._request_llm_cfg_override
        if o is None:
            return (uid, "default")
        return (
            uid,
            "lcagent",
            o.model,
            o.base_url,
            (o.api_key or "")[:16],
        )

    def set_chat_manager(self, chat_manager_or_factory):
        """Set chat manager for auto-registration.

        Args:
            chat_manager_or_factory: ChatManager or callable(uid) -> ChatManager
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

    async def _get_memory_manager(
        self, user_id: str | None
    ) -> ReMeLightMemoryManager | None:
        """Resolve ReMeLight memory manager for user_id (per-user if isolated).

        Lazy-creates and caches per user. Uses users/<user_id> as working_dir
        when isolation is enabled. Matches Workspace memory wiring.
        """
        cache_key = self._memory_manager_cache_key(user_id)
        if cache_key in self._memory_manager_cache:
            return self._memory_manager_cache[cache_key]
        try:
            working_dir = str(get_user_working_dir(user_id))
            mgr = ReMeLightMemoryManager(
                working_dir=working_dir,
                agent_id=self.agent_id,
            )
            await mgr.start()
            self._memory_manager_cache[cache_key] = mgr
            return mgr
        except Exception as e:
            logger.exception(
                "ReMeLightMemoryManager create for user %s failed: %s",
                cache_key,
                e,
            )
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
                            logger.exception(
                                "MCP init failed for user %s", uid
                            )
                    self._mcp_managers_cache[uid] = mgr
        return await mgr.get_clients()

    def set_workspace(self, workspace):
        """Set workspace for control command handlers.

        Args:
            workspace: Workspace instance
        """
        self._workspace = workspace

    _APPROVAL_TIMEOUT_SECONDS = TOOL_GUARD_APPROVAL_TIMEOUT_SECONDS

    async def _resolve_pending_approval(
        self,
        session_id: str,
        query: str | None,
    ) -> tuple[Msg | None, bool, dict[str, Any] | None]:
        """Check for a pending tool-guard approval for *session_id*.

        Returns ``(response_msg, was_consumed, approved_tool_call)``:

        - ``(None, False, None)`` — no pending approval, continue normally.
        - ``(Msg, True, None)``   — denied; yield the Msg and stop.
        - ``(None, True, dict)``  — approved with stored tool call.

        Approvals are resolved FIFO per session (oldest pending first).
        """
        if not session_id:
            return None, False, None

        from ..approvals import get_approval_service

        svc = get_approval_service()
        pending = await svc.get_pending_by_session(session_id)
        if pending is None:
            return None, False, None

        elapsed = time.time() - pending.created_at
        if elapsed > self._APPROVAL_TIMEOUT_SECONDS:
            await svc.resolve_request(
                pending.request_id,
                ApprovalDecision.TIMEOUT,
            )
            return (
                Msg(
                    name="Friday",
                    role="assistant",
                    content=[
                        TextBlock(
                            type="text",
                            text=(
                                f"⏰ Tool `{pending.tool_name}` approval "
                                f"timed out ({int(elapsed)}s) — denied.\n"
                                f"工具 `{pending.tool_name}` 审批超时"
                                f"（{int(elapsed)}s），已拒绝执行。"
                            ),
                        ),
                    ],
                ),
                True,
                None,
            )

        normalized = (query or "").strip().lower()
        if _is_approval(normalized):
            resolved = await svc.resolve_request(
                pending.request_id,
                ApprovalDecision.APPROVED,
            )
            approved_tool_call: dict[str, Any] | None = None
            record = resolved or pending
            if isinstance(record.extra, dict):
                candidate = record.extra.get("tool_call")
                if isinstance(candidate, dict):
                    approved_tool_call = dict(candidate)
                    siblings = record.extra.get("sibling_tool_calls")
                    if isinstance(siblings, list):
                        approved_tool_call["_sibling_tool_calls"] = siblings
                    remaining = record.extra.get("remaining_queue")
                    if isinstance(remaining, list):
                        approved_tool_call["_remaining_queue"] = remaining
                    thinking_blocks = record.extra.get("thinking_blocks")
                    if isinstance(thinking_blocks, list):
                        approved_tool_call[
                            "_thinking_blocks"
                        ] = thinking_blocks
            return None, True, approved_tool_call

        await svc.resolve_request(
            pending.request_id,
            ApprovalDecision.DENIED,
        )
        return (
            Msg(
                name="Friday",
                role="assistant",
                content=[
                    TextBlock(
                        type="text",
                        text=(
                            f"❌ Tool `{pending.tool_name}` denied.\n"
                            f"工具 `{pending.tool_name}` 已拒绝执行。"
                        ),
                    ),
                ],
            ),
            True,
            None,
        )

    async def query_handler(
        self,
        msgs,
        request: AgentRequest = None,
        **kwargs,
    ):
        """
        Handle agent query.
        """
        logger.debug(
            f"AgentRunner.query_handler called: agent_id={self.agent_id}, "
            f"msgs={msgs}, request={request}",
        )
        query = _get_last_user_text(msgs)
        session_id = getattr(request, "session_id", "") or ""

        (
            approval_response,
            approval_consumed,
            approved_tool_call,
        ) = await self._resolve_pending_approval(session_id, query)
        if approval_response is not None:
            yield approval_response, True
            user_id = getattr(request, "user_id", "") or ""
            await self._cleanup_denied_session_memory(
                session_id,
                user_id,
                denial_response=approval_response,
            )
            return

        if not approval_consumed and query and _is_command(query):
            logger.info("Command path: %s", query.strip()[:50])
            async for msg, last in run_command_path(request, msgs, self):
                yield msg, last
            return

        logger.debug(
            f"AgentRunner.stream_query: request={request}, "
            f"agent_id={self.agent_id}",
        )

        # Set agent context for model creation
        from ..agent_context import set_current_agent_id

        set_current_agent_id(self.agent_id)

        agent = None
        chat = None
        mgr = None
        session_state_loaded = False
        try:
            self._request_llm_cfg_override = _llm_cfg_from_process_meta()
            enable_agent, enable_skills = _feature_flags_from_process_meta()
            session_id = request.session_id
            # Prefer JWT context (UserIdContextMiddleware) over body; body
            # may not be updated by middleware in some stacks.
            user_id = get_context_user_id() or getattr(
                request, "user_id", None
            )
            channel = getattr(request, "channel", DEFAULT_CHANNEL)

            logger.info(
                "Handle agent query:\n%s",
                json.dumps(
                    {
                        "session_id": session_id,
                        "user_id": user_id,
                        "channel": channel,
                        "enable_agent": enable_agent,
                        "enable_skills": enable_skills,
                        "msgs_len": len(msgs) if msgs else 0,
                        "msgs_str": str(msgs)[:300] + "...",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )

            user_working_dir = get_user_working_dir(user_id)
            set_current_working_dir(user_working_dir)
            wd = (
                str(user_working_dir)
                if copaw_storage_isolation_enabled()
                else (
                    str(self.workspace_dir)
                    if self.workspace_dir
                    else str(WORKING_DIR)
                )
            )
            env_context = build_env_context(
                session_id=session_id,
                user_id=user_id,
                channel=channel,
                working_dir=wd,
            )
            env_context += (
                "\n- LCAgent 功能开关:\n"
                f"  - enable_agent: {enable_agent}\n"
                f"  - enable_skills: {enable_skills}\n"
            )
            proc_meta = get_process_request_meta()
            _pub_base = (
                str(
                    proc_meta.get("lcagent_console_public_base") or "",
                )
                .strip()
                .rstrip("/")
            )
            _link_tpl = (
                f"{_pub_base}/agent/<应用UUID>"
                if _pub_base
                else (
                    "/agent/<应用UUID>（在 LCAgent 控制台域名下打开，"
                    "与「复制应用链接」一致）"
                )
            )
            _raw_pub_apps = proc_meta.get("lcagent_published_apps")
            _has_published_app_list = (
                enable_agent
                and isinstance(_raw_pub_apps, list)
                and len(_raw_pub_apps) > 0
            )
            if _has_published_app_list:
                env_context += (
                    "\n- 当前用户可见的已发布应用"
                    "（来自 meta.lcagent_published_apps）。"
                    "用户问「有哪些已发布应用」时**仅依据下列列表**，"
                    "不得编造。\n"
                )
                for entry in _raw_pub_apps:
                    if not isinstance(entry, dict):
                        continue
                    aid = str(entry.get("id") or "").strip()
                    nm = str(entry.get("name") or "").strip() or aid
                    link = str(entry.get("app_link") or "").strip()
                    desc = str(entry.get("description") or "").strip()
                    env_context += f"  - 名称: {nm}\n    app_id: {aid}\n"
                    if link:
                        env_context += f"    应用链接: {link}\n"
                    if desc:
                        env_context += f"    简介: {desc}\n"
                env_context += (
                    "  调用 invoke_lcagent_published_app 时传入所选 app_id。"
                    "若下文有「默认已发布应用」，可将 app_id **留空**；"
                    "否则**不可**留空。\n"
                )

            pub_app = proc_meta.get("lcagent_published_app_id")
            if enable_agent and pub_app:
                _default_link = (
                    f"{_pub_base}/agent/{pub_app}"
                    if _pub_base
                    else f"/agent/{pub_app}"
                )
                env_context += (
                    "\n- 本轮默认已发布应用（invoke_lcagent_published_app 在 "
                    "app_id 留空时使用该 UUID）:\n"
                    f"  - 应用 ID: {pub_app}\n"
                    f"  - 应用链接（浏览器打开）: {_default_link}\n"
                )
                pub_name = str(
                    proc_meta.get("lcagent_published_app_name") or ""
                ).strip()
                pub_desc = str(
                    proc_meta.get("lcagent_published_app_description") or "",
                ).strip()
                if pub_name:
                    env_context += f"  - 应用名称: {pub_name}\n"
                if pub_desc:
                    _lim = 2000
                    _d = (
                        pub_desc
                        if len(pub_desc) <= _lim
                        else pub_desc[:_lim] + "…"
                    )
                    env_context += f"  - 应用简介: {_d}\n"
                env_context += (
                    "  - 说明: 介绍应用时优先用名称与简介，不要只报 UUID；"
                    "未指定其它应用时可将 app_id 留空用默认。\n"
                )
            elif enable_agent:
                if _has_published_app_list:
                    env_context += (
                        "\n- meta 未注入 lcagent_published_app_id（无默认）。"
                        "请从上方列表选 app_id 调用 invoke_lcagent_published_app，"
                        "勿留空。\n"
                    )
                else:
                    env_context += (
                        "\n- 已发布应用：meta 无 lcagent_published_app_id 与列表；"
                        "invoke_lcagent_published_app 请在 app_id 传入 UUID。\n"
                        f"  - 链接形态: {_link_tpl}；可见应用均可调用，"
                        "**无需**单独开 API 开关。\n"
                    )

            if enable_agent and proc_meta.get("lcagent_console_api_base"):
                env_context += (
                    "\n- 已发布应用返回的路径与链接：\n"
                    "  /app/upload/、/tmp/、/console/api/files/download?… 等"
                    "在 **LCAgent 后端**，**不在** CoPaw 工作目录。"
                    "勿用 read_file 等本地验证；有链接则原样给用户。\n"
                )
                if proc_meta.get("lcagent_user_attachment_paths"):
                    env_context += (
                        "  附件路径在 meta.lcagent_user_attachment_paths；"
                        "invoke_lcagent_published_app 会随请求传给工作流。\n"
                    )
                    att_names = proc_meta.get("lcagent_user_attachment_names")
                    if isinstance(att_names, list) and att_names:
                        names_line = ", ".join(
                            str(x) for x in att_names if str(x).strip()
                        )
                        env_context += (
                            "  附件文件名（用户问「上传了什么」据此回答，"
                            "勿扫工作区）："
                            f"{names_line}\n"
                        )

            mcp_clients = await self._get_mcp_clients_for_user(user_id)

            agent_config = load_agent_config(self.agent_id)
            if (
                self._request_llm_cfg_override is not None
                or copaw_storage_isolation_enabled()
            ):
                memory_manager = await self._get_memory_manager(user_id)
            else:
                memory_manager = self.memory_manager

            agent = CoPawAgent(
                agent_config=agent_config,
                env_context=env_context,
                mcp_clients=mcp_clients,
                memory_manager=memory_manager,
                llm_cfg=self._request_llm_cfg_override,
                enable_agent_mode=enable_agent,
                enable_skills=enable_skills,
                request_context={
                    "session_id": session_id,
                    "user_id": user_id,
                    "channel": channel,
                    "agent_id": self.agent_id,
                    **(
                        {
                            "forced_tool_call_json": json.dumps(
                                approved_tool_call,
                                ensure_ascii=False,
                            ),
                        }
                        if approved_tool_call
                        else {}
                    ),
                },
                workspace_dir=self.workspace_dir,
                task_tracker=self._task_tracker,
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
                logger.debug(
                    "Runner: get_or_create_chat session_id=%s user_id=%s "
                    "channel=%s name=%s",
                    session_id,
                    user_id,
                    channel,
                    name,
                )
                chat = await mgr.get_or_create_chat(
                    session_id,
                    user_id,
                    channel,
                    name=name,
                )
                logger.debug(f"Runner: Got chat: {chat.id}")
            else:
                logger.warning(
                    f"ChatManager is None! Cannot auto-register chat for "
                    f"session_id={session_id}",
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
            raise AgentException("Task has been cancelled!") from exc
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
            self._request_llm_cfg_override = None
            reset_current_working_dir()
            if agent is not None and session_state_loaded:
                await self.session.save_session_state(
                    session_id=session_id,
                    user_id=user_id,
                    agent=agent,
                )

            if mgr is not None and chat is not None:
                await mgr.update_chat(chat)

    async def _cleanup_denied_session_memory(
        self,
        session_id: str,
        user_id: str,
        denial_response: "Msg | None" = None,
    ) -> None:
        """Clean up session memory after a tool-guard denial.

        In the deny path (no agent is created), this method:

        1. Removes the LLM denial explanation (the assistant message
           immediately following the last marked entry).
        2. Strips ``TOOL_GUARD_DENIED_MARK`` from all marks lists so
           the kept tool-call info becomes normal memory entries.
        3. Appends *denial_response* (e.g. "❌ Tool denied") to the
           persisted session memory.
        """
        if not hasattr(self, "session") or self.session is None:
            return

        path = self.session._get_save_path(  # pylint: disable=protected-access
            session_id,
            user_id,
        )
        if not Path(path).exists():
            return

        try:
            with open(
                path,
                "r",
                encoding="utf-8",
                errors="surrogatepass",
            ) as f:
                states = json.load(f)

            agent_state = states.get("agent", {})
            memory_state = agent_state.get("memory", {})
            content = memory_state.get("content", [])

            if not content:
                return

            def _is_marked(entry):
                return (
                    isinstance(entry, list)
                    and len(entry) >= 2
                    and isinstance(entry[1], list)
                    and TOOL_GUARD_DENIED_MARK in entry[1]
                )

            last_marked_idx = -1
            for i, entry in enumerate(content):
                if _is_marked(entry):
                    last_marked_idx = i

            modified = False

            if last_marked_idx >= 0 and last_marked_idx + 1 < len(content):
                next_entry = content[last_marked_idx + 1]
                if (
                    isinstance(next_entry, list)
                    and len(next_entry) >= 1
                    and isinstance(next_entry[0], dict)
                    and next_entry[0].get("role") == "assistant"
                ):
                    del content[last_marked_idx + 1]
                    modified = True

            for entry in content:
                if _is_marked(entry):
                    entry[1].remove(TOOL_GUARD_DENIED_MARK)
                    modified = True

            if denial_response is not None:
                ts = getattr(denial_response, "timestamp", None)
                msg_dict = {
                    "id": getattr(denial_response, "id", ""),
                    "name": getattr(denial_response, "name", "Friday"),
                    "role": getattr(denial_response, "role", "assistant"),
                    "content": denial_response.content,
                    "metadata": getattr(
                        denial_response,
                        "metadata",
                        None,
                    ),
                    "timestamp": str(ts) if ts is not None else "",
                }
                content.append([msg_dict, []])
                modified = True

            if modified:
                with open(
                    path,
                    "w",
                    encoding="utf-8",
                    errors="surrogatepass",
                ) as f:
                    json.dump(states, f, ensure_ascii=False)
                logger.info(
                    "Tool guard: cleaned up denied session memory in %s",
                    path,
                )
        except Exception:  # pylint: disable=broad-except
            logger.warning(
                "Failed to clean up denied messages from session %s",
                session_id,
                exc_info=True,
            )

    async def init_handler(self, *args, **kwargs):
        """
        Init handler.
        """
        # Load environment variables from .env file
        # env_path = Path(__file__).resolve().parents[4] / ".env"
        env_path = Path("./") / ".env"
        if env_path.exists():
            load_dotenv(env_path)
            logger.debug(f"Loaded environment variables from {env_path}")
        else:
            logger.debug(
                f".env file not found at {env_path}, "
                "using existing environment variables",
            )

        if copaw_storage_isolation_enabled():
            self.session = SafeJSONSession(save_dir=str(WORKING_DIR))
        else:
            session_dir = str(
                (self.workspace_dir if self.workspace_dir else WORKING_DIR)
                / "sessions",
            )
            self.session = SafeJSONSession(save_dir=session_dir)

    async def shutdown_handler(self, *args, **kwargs):
        """
        Shutdown handler.
        """
        for cache_key, mgr in list(self._memory_manager_cache.items()):
            try:
                await mgr.close()
            except Exception as e:
                logger.warning(
                    "memory manager close for user %s failed: %s",
                    cache_key,
                    e,
                )
        self._memory_manager_cache.clear()
        await self.shutdown_mcp_managers_cache()
