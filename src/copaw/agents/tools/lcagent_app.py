"""LCAgent platform callbacks (Flask console API) from CoPaw tools."""
# NOTE: do NOT add `from __future__ import annotations` here. agentscope's
# _parse_tool_function reads raw param.annotation and feeds it to a dynamic
# pydantic model; stringized annotations (e.g. 'Literal[...]') cannot be
# resolved in pydantic's rebuild namespace and raise class-not-fully-defined.

import json
import logging
from typing import Any, Literal
from urllib.parse import quote

import httpx
from agentscope.message import TextBlock
from agentscope.tool import ToolResponse

from ...constant import TRUNCATION_NOTICE_MARKER
from ...context import get_process_request_meta, get_request_authorization
from .lcagent_media import register_invoke_lcagent_reply

logger = logging.getLogger(__name__)

# Structured console payloads (catalog index, change-set summaries) stay below
# this budget; anything larger is cut here with an explicit marker instead of
# being silently truncated mid-JSON by memory-level tool result compaction.
_LCAGENT_TOOL_MAX_BYTES = 24 * 1024
_LCAGENT_TOOL_TAIL_BYTES = 2 * 1024


def _lcagent_tool_text(text: str) -> ToolResponse:
    text = text or ""
    encoded = text.encode("utf-8")
    if len(encoded) > _LCAGENT_TOOL_MAX_BYTES:
        head = encoded[:_LCAGENT_TOOL_MAX_BYTES].decode("utf-8", errors="ignore")
        tail = encoded[-_LCAGENT_TOOL_TAIL_BYTES:].decode("utf-8", errors="ignore")
        text = (
            f"{head}\n\n{TRUNCATION_NOTICE_MARKER}\n"
            f"工具返回共 {len(encoded)} 字节，已截断并保留头部与尾部。"
            "请改用分层按需读取（catalog 索引 + get_component_schema / "
            "get_model_detail / get_mcp_tools / get_node），不要重复拉取全量。\n\n"
            f"{tail}"
        )
    return ToolResponse(
        content=[TextBlock(type="text", text=text)],
    )


def _bound_workspace(meta: dict[str, Any], expected_type: str) -> tuple[dict[str, Any] | None, str]:
    raw = meta.get("lcagent_workspace")
    if not isinstance(raw, dict):
        return None, ""
    workspace_type = str(raw.get("type") or "").strip()
    workspace_id = str(raw.get("id") or "").strip()
    if workspace_type != expected_type or not workspace_id:
        return None, (
            f"WORKSPACE_TYPE_MISMATCH: 当前助手绑定到 {workspace_type or 'unknown'} "
            f"workspace，不能使用 {expected_type} 编辑工具。"
        )
    return {**raw, "type": workspace_type, "id": workspace_id}, ""


def invoke_lcagent_published_app(  # pylint: disable=too-many-return-statements,too-many-branches
    query: str,
    app_id: str = "",
) -> ToolResponse:
    """调用 LCAgent 上已发布的工作流应用，返回应用主回复文本。

    已发布（status 正常）且当前用户可见的应用均可调用，不依赖单独开启「API / API 调用」开关。
    用户在浏览器可使用 ``{控制台根}/agent/<应用UUID>`` 打开同一应用（与控制台「复制应用链接」一致）；
    meta 中若有 ``lcagent_console_public_base``，可与系统提示中的链接说明一致。

    将 app_id 留空时使用 meta 中的 lcagent_published_app_id（由 LCAgent 代理按可见已发布应用解析）。
    meta 中可能还有 lcagent_published_app_name / lcagent_published_app_description，
    回答用户「应用是做什么的」时请用这些信息。

    lcagent_published_apps 列表的每个条目带 ``kind`` 字段：``agent`` 为独立 agent 应用
    （表单化配置、非工作流编排），``workflow`` 为工作流应用；两类调用方式完全相同。

    Args:
        query: 转发给应用的用户问题或指令。
        app_id: LCAgent 应用 UUID；空字符串时使用 meta.lcagent_published_app_id。

    经 LCAgent 代理且用户在本轮消息中上传附件时，meta ``lcagent_user_attachment_paths`` 会列出
    服务端路径，本工具会随 ``run_app`` 一并提交，工作流可收到与控制台一致的 input_files。

    Returns:
        ``ToolResponse``，正文为应用输出（可能含文件 URL）或错误说明。
        其中的 ``/app/upload/``、``/console/api/files/download`` 等资源在
        **LCAgent 服务器** 上，勿在当前环境用本地文件工具去读取。
        **将本工具返回的 Markdown / 路径原样写入面向用户的最终回复**；
        若回复已含图片或文件链接，**不要**再调用 ``send_file_to_user``。
    """
    meta = get_process_request_meta()
    base = (meta.get("lcagent_console_api_base") or "").strip().rstrip("/")
    if not base:
        return _lcagent_tool_text(
            "错误：缺少 lcagent_console_api_base。"
            "请通过 LCAgent 的 CoPaw 代理（POST /console/api/copaw/agent/process）访问。",
        )
    aid = (app_id or "").strip() or str(
        meta.get("lcagent_published_app_id") or ""
    ).strip()
    if not aid:
        return _lcagent_tool_text(
            "错误：未指定应用 ID，且 meta 中无 lcagent_published_app_id。"
            "请在参数 app_id 中填写目标应用的 UUID；用户可从 LCAgent 控制台「复制应用链接」"
            "（形如 /agent/<UUID>）中取得。",
        )
    auth = get_request_authorization().strip()
    if not auth:
        return _lcagent_tool_text("错误：缺少 Authorization，无法以当前用户调用 LCAgent。")

    url = f"{base}/console/api/copaw/lcagent/run_app"
    payload: dict[str, Any] = {"app_id": aid, "query": query}
    raw_att = meta.get("lcagent_user_attachment_paths")
    if isinstance(raw_att, list) and raw_att:
        paths = [str(p).strip() for p in raw_att if str(p).strip()]
        if paths:
            payload["file_paths"] = paths

    try:
        with httpx.Client(
            timeout=httpx.Timeout(600.0, connect=30.0)
        ) as client:
            resp = client.post(
                url,
                json=payload,
                headers={
                    "Authorization": auth,
                    "Content-Type": "application/json",
                },
            )
    except httpx.RequestError as exc:
        logger.warning("invoke_lcagent_published_app: request error %s", exc)
        return _lcagent_tool_text(f"调用 LCAgent 失败（网络）: {exc}")

    snippet = (resp.text or "")[:800]
    if resp.status_code != 200:
        logger.warning(
            "invoke_lcagent_published_app: HTTP %s url=%s snippet=%s",
            resp.status_code,
            url,
            snippet[:200],
        )
        return _lcagent_tool_text(f"调用失败: HTTP {resp.status_code} {snippet}")

    try:
        data = resp.json()
    except json.JSONDecodeError:
        return _lcagent_tool_text(f"调用失败: 响应非 JSON {snippet}")

    if not isinstance(data, dict):
        return _lcagent_tool_text(str(data))

    st: Any = data.get("status")
    if st not in (0, None, "0"):
        return _lcagent_tool_text(f"调用失败: {data.get('message') or snippet}")

    result = data.get("result")
    if isinstance(result, dict) and "reply" in result:
        reply_text = str(result.get("reply") or "")
        register_invoke_lcagent_reply(reply_text)
        return _lcagent_tool_text(reply_text)
    if isinstance(result, str):
        register_invoke_lcagent_reply(result)
        return _lcagent_tool_text(result)
    if result is not None:
        reply_text = json.dumps(result, ensure_ascii=False)
        register_invoke_lcagent_reply(reply_text)
        return _lcagent_tool_text(reply_text)
    return _lcagent_tool_text(snippet)


def manage_lcagent_workflow(
    action: Literal[
        "catalog",
        "get_component_schema",
        "get_model_detail",
        "get_mcp_tools",
        "context",
        "get_node",
        "get_node_schema",
        "validate",
        "get_change_set",
    ],
    app_id: str = "",
    node_id: str = "",
    component_type: str = "",
    resource_id: str = "",
    patch_json: str = "",
    change_set_id: str = "",
    create_app_name: str = "",
    create_app_description: str = "",
    base_revision: str = "",
) -> ToolResponse:
    """分层读取工作流编辑目录，或生成待用户确认的工作流变更集。

    此工具只能读取和预校验，不能确认、发布或删除应用。目录按需分层读取，
    不要试图一次拿到全量：``catalog`` 只返回**索引**（componentIndex 组件清单、
    operationIndex 操作字段清单、creationGuide 规则、瘦身 resources）；
    ``get_component_schema`` 按 ``component_type`` 返回单组件完整 configSchema
    与 defaults；``get_model_detail`` / ``get_mcp_tools`` 按 ``resource_id``
    返回模型 nodeConfig+capabilities / MCP tools[].inputSchema。

    ``add_node.node_type`` 只能取 componentIndex[].nodeType；创建前必须先调用
    ``catalog``，add_node 或 update_node_config 前必须先 ``get_component_schema``
    查看字段类型、枚举与默认值。禁止凭记忆猜测 node_type 或字段名，禁止用
    shell/curl 绕过本工具探索目录 API。新画布已自带 ``__start__``/``__end__``，
    禁止重复添加。明确模型优先用 ``set_model.selection_id``（取索引
    resources.models[].id，服务端解析当前 nodeConfig）；仅当需要把 nodeConfig
    内联进 add_node.config 时才调用 ``get_model_detail`` 并原样复制，不得根据
    模型名猜测 source/model_id。

    Patch 操作词汇与规则以 catalog 返回的 operationIndex/creationGuide 和
    lcagent_workflow_builder skill 为准。``validate`` 成功后当前聊天会直接渲染
    含 Diff 与确认/取消按钮的 ChangeSet 卡片；请引导用户在对话内确认，不要
    让用户跳转首页寻找卡片，也不得声称修改已经生效。用户确认后用
    ``get_change_set`` 核验 ``status=applied`` 和 ``target.appId``，否则应用
    仍未创建。独立 Agent 应用应使用 ``manage_lcagent_agent``；工作流里的
    ``agent-v2``/``database-agent-v2`` 是画布节点，仍使用本工具。

    Args:
        action: ``catalog`` 目录索引；``get_component_schema`` 单组件完整
            Schema；``get_model_detail`` / ``get_mcp_tools`` 单个资源详情；
            ``context`` 读取绑定画布快照；``get_node`` / ``get_node_schema``
            按稳定节点 ID 读取；``validate`` 预校验 Patch；``get_change_set``
            查询变更集状态摘要。
        app_id: 修改已有应用时的应用 UUID；留空表示创建新应用。
        node_id: ``get_node`` / ``get_node_schema`` 所需的稳定节点 ID。
        component_type: ``get_component_schema`` 所需组件类型
            （componentIndex[].nodeType，如 ``llm-text-generation``）。
        resource_id: ``get_model_detail``（resources.models[].id）或
            ``get_mcp_tools``（resources.mcpServers[].id）所需资源 ID。
        patch_json: ``validate`` 所需的 JSON，格式为
            ``{"summary":"...","operations":[...]}``；一个 operations 数组作为
            原子候选批处理校验，操作字段结构以 operationIndex 与组件
            configSchema 为准。
        change_set_id: ``get_change_set`` 所需的变更集 ID。
        create_app_name: 创建应用时的名称。
        create_app_description: 创建应用时的描述。
        base_revision: 修改已有草稿时可选的基础版本哈希。

    Returns:
        结构化 JSON：目录索引、组件/资源详情、诊断或 ``lcagent_change_set``
        摘要。变更集仍需用户确认；超长返回会被显式截断并提示按需读取。
    """
    meta = get_process_request_meta()
    base = (meta.get("lcagent_console_api_base") or "").strip().rstrip("/")
    auth = get_request_authorization().strip()
    if not base:
        return _lcagent_tool_text("错误：缺少 lcagent_console_api_base。")
    if not auth:
        return _lcagent_tool_text("错误：缺少 Authorization，无法访问工作流。")
    bound, binding_error = _bound_workspace(meta, "workflow")
    if binding_error:
        return _lcagent_tool_text(f"错误：{binding_error}")
    if bound:
        bound_id = str(bound["id"])
        if app_id.strip() and app_id.strip() != bound_id:
            return _lcagent_tool_text(
                "错误：WORKSPACE_BINDING_MISMATCH: 当前助手不能访问其它工作流。"
            )
        app_id = bound_id

    method = "GET"
    payload = None
    if action == "catalog":
        path = "/console/api/workflow-editing/catalog?view=index"
    elif action == "get_component_schema":
        if not component_type.strip():
            return _lcagent_tool_text(
                "错误：get_component_schema 需要 component_type"
                "（catalog componentIndex[].nodeType）。"
            )
        path = (
            "/console/api/workflow-editing/catalog?view=component&node_type="
            + quote(component_type.strip())
        )
    elif action == "get_model_detail":
        if not resource_id.strip():
            return _lcagent_tool_text(
                "错误：get_model_detail 需要 resource_id"
                "（catalog resources.models[].id）。"
            )
        path = (
            "/console/api/workflow-editing/catalog?view=model&id="
            + quote(resource_id.strip())
        )
    elif action == "get_mcp_tools":
        if not resource_id.strip():
            return _lcagent_tool_text(
                "错误：get_mcp_tools 需要 resource_id"
                "（catalog resources.mcpServers[].id）。"
            )
        path = (
            "/console/api/workflow-editing/catalog?view=mcp&id="
            + quote(resource_id.strip())
        )
    elif action in {"context", "get_node", "get_node_schema"}:
        if not app_id.strip():
            return _lcagent_tool_text(f"错误：{action} 需要 app_id 或编辑器 workspace binding。")
        path = f"/console/api/workspace-context/workflow/{app_id.strip()}"
        if action != "context":
            if not node_id.strip():
                return _lcagent_tool_text(f"错误：{action} 需要 node_id。")
            path += f"/nodes/{node_id.strip()}"
            if action == "get_node_schema":
                path += "/schema"
    elif action == "get_change_set":
        if not change_set_id.strip():
            return _lcagent_tool_text("错误：get_change_set 需要 change_set_id。")
        path = (
            "/console/api/workflow-editing/change-sets/"
            f"{quote(change_set_id.strip())}?view=summary"
        )
    elif action == "validate":
        if bound and not (base_revision.strip() or str(bound.get("revision") or "").strip()):
            return _lcagent_tool_text(
                "错误：REVISION_REQUIRED: 绑定画布缺少权威 revision，请重新读取 context。"
            )
        try:
            patch = json.loads(patch_json)
        except json.JSONDecodeError as exc:
            return _lcagent_tool_text(f"错误：patch_json 不是合法 JSON: {exc}")
        if not isinstance(patch, dict):
            return _lcagent_tool_text("错误：patch_json 顶层必须是对象。")
        if app_id.strip():
            target = {
                "kind": "workflow",
                "mode": "existing",
                "app_id": app_id.strip(),
            }
        else:
            if not create_app_name.strip():
                return _lcagent_tool_text(
                    "错误：创建新应用时需要 create_app_name。",
                )
            target = {
                "kind": "workflow",
                "mode": "create",
                "name": create_app_name.strip(),
                "description": create_app_description.strip(),
            }
        path = "/console/api/workflow-editing/change-sets/validate?view=summary"
        method = "POST"
        payload = {
            "target": target,
            "patch": patch,
            **({"workspace": {"type": "workflow", "id": app_id.strip()}} if bound else {}),
            **(
                {"baseRevision": base_revision.strip() or str(bound.get("revision") or "")}
                if base_revision.strip() or bound
                else {}
            ),
        }
    else:
        return _lcagent_tool_text(f"错误：不支持的 action: {action}")

    try:
        with httpx.Client(timeout=httpx.Timeout(60.0, connect=15.0)) as client:
            response = client.request(
                method,
                f"{base}{path}",
                json=payload,
                headers={
                    "Authorization": auth,
                    "Content-Type": "application/json",
                },
            )
    except httpx.RequestError as exc:
        logger.warning("manage_lcagent_workflow: request error %s", exc)
        return _lcagent_tool_text(f"访问 LCAgent 工作流接口失败: {exc}")

    try:
        body: Any = response.json()
    except json.JSONDecodeError:
        body = {"message": (response.text or "")[:1000]}
    if response.status_code >= 400:
        return _lcagent_tool_text(
            json.dumps(
                {
                    "ok": False,
                    "httpStatus": response.status_code,
                    "error": body,
                },
                ensure_ascii=False,
            ),
        )

    if action == "validate" and isinstance(body, dict):
        body = {
            "kind": "lcagent_change_set",
            "requiresUserConfirmation": True,
            "creationState": "pending_not_created",
            "nextAction": "请用户在当前聊天的 ChangeSet 卡片检查 Diff 并点击确认应用",
            **body,
        }
    elif action == "get_change_set" and isinstance(body, dict):
        status = body.get("status")
        body["creationState"] = (
            "created" if status == "applied" and (body.get("target") or {}).get("appId")
            else "pending_not_created" if status == "pending"
            else "not_created"
        )
    return _lcagent_tool_text(json.dumps(body, ensure_ascii=False))


def manage_lcagent_agent(
    action: Literal["catalog", "context", "get_node", "get_node_schema", "get_agent", "propose", "get_change_set"],
    app_id: str = "",
    node_id: str = "",
    patch_json: str = "",
    change_set_id: str = "",
    create_app_name: str = "",
    create_app_description: str = "",
    base_revision: str = "",
) -> ToolResponse:
    """读取独立 Agent 的隐藏固定画布/配置，或生成待确认的 Agent 变更集。

    此工具只能读取和预校验，不能确认或发布。``propose`` 成功后，当前聊天会
    直接渲染含具体改动与确认/取消按钮的 ChangeSet 卡片；请引导用户在对话内
    确认，不要让用户跳转首页寻找卡片，也不得声称修改已经生效。
    独立 Agent 并非“没有节点”：它本质上是隐藏的 V2 固定画布
    ``__start__ → hidden-agent-v2 → __end__``。本工具通过 AgentPatch 修改中间
    ``agent-v2`` 节点，并由平台自动重建端口、连线和整图。不要向用户声称独立
    Agent 没有节点或不是画布实现。固定拓扑不支持添加其它节点；若用户确实要
    任意增删节点，应创建/修改工作流应用并使用 ``manage_lcagent_workflow``。

    Args:
        action: ``catalog`` 获取隐藏画布契约、Agent 默认配置与资源目录；
            ``get_agent`` 读取某个独立 Agent 的当前配置、隐藏画布契约与
            baseRevision；``propose`` 预校验 AgentPatch；
            ``get_change_set`` 查询变更集状态。
        app_id: 修改已有 Agent 时的应用 UUID；留空表示创建新 Agent。
        patch_json: ``propose`` 所需的 JSON，格式为
            ``{"summary":"...","operations":[...]}``，operations 支持
            update_identity / update_strategy / update_model（应从
            ``resources.models[]`` 选择模型，将该条目的 ``executorModelId``
            传入 ``executorModelId``，并将其 ``nodeConfig`` 原样放入 operation
            的 ``nodeConfig``）/
            update_capabilities(list+op:add|remove|set+values) / update_limits /
            update_output_schema / update_system_prompt / update_human_policy。
        change_set_id: ``get_change_set`` 所需的变更集 ID。
        create_app_name: 创建新 Agent 时的名称。
        create_app_description: 创建新 Agent 时的描述。
        base_revision: 修改已有 Agent 时可选的基础版本哈希。

    Returns:
        包含结构化目录、诊断或 ``lcagent_agent_change_set`` 的 JSON。变更集仍需用户确认。
    """
    meta = get_process_request_meta()
    base = (meta.get("lcagent_console_api_base") or "").strip().rstrip("/")
    auth = get_request_authorization().strip()
    if not base:
        return _lcagent_tool_text("错误：缺少 lcagent_console_api_base。")
    if not auth:
        return _lcagent_tool_text("错误：缺少 Authorization，无法访问 Agent。")
    bound, binding_error = _bound_workspace(meta, "agent")
    if binding_error:
        return _lcagent_tool_text(f"错误：{binding_error}")
    if bound:
        bound_id = str(bound["id"])
        if app_id.strip() and app_id.strip() != bound_id:
            return _lcagent_tool_text(
                "错误：WORKSPACE_BINDING_MISMATCH: 当前助手不能访问其它 Agent。"
            )
        app_id = bound_id

    method = "GET"
    payload = None
    if action == "catalog":
        path = "/console/api/agent-editing/catalog"
    elif action in {"context", "get_node", "get_node_schema"}:
        if not app_id.strip():
            return _lcagent_tool_text(f"错误：{action} 需要 app_id 或编辑器 workspace binding。")
        path = f"/console/api/workspace-context/agent/{app_id.strip()}"
        if action != "context":
            if not node_id.strip():
                return _lcagent_tool_text(f"错误：{action} 需要 node_id。")
            path += f"/nodes/{node_id.strip()}"
            if action == "get_node_schema":
                path += "/schema"
    elif action == "get_agent":
        if not app_id.strip():
            return _lcagent_tool_text("错误：get_agent 需要 app_id。")
        path = f"/console/api/agent-editing/apps/{app_id.strip()}/agent"
    elif action == "get_change_set":
        if not change_set_id.strip():
            return _lcagent_tool_text("错误：get_change_set 需要 change_set_id。")
        path = f"/console/api/agent-editing/change-sets/{change_set_id.strip()}"
    elif action == "propose":
        if bound and not (base_revision.strip() or str(bound.get("revision") or "").strip()):
            return _lcagent_tool_text(
                "错误：REVISION_REQUIRED: 绑定 Agent 缺少权威 revision，请重新读取 context。"
            )
        try:
            patch = json.loads(patch_json)
        except json.JSONDecodeError as exc:
            return _lcagent_tool_text(f"错误：patch_json 不是合法 JSON: {exc}")
        if not isinstance(patch, dict):
            return _lcagent_tool_text("错误：patch_json 顶层必须是对象。")
        if app_id.strip():
            target = {"kind": "agent", "mode": "existing", "app_id": app_id.strip()}
        else:
            if not create_app_name.strip():
                return _lcagent_tool_text("错误：创建新 Agent 时需要 create_app_name。")
            target = {
                "kind": "agent",
                "mode": "create",
                "name": create_app_name.strip(),
                "description": create_app_description.strip(),
            }
        path = "/console/api/agent-editing/change-sets/validate"
        method = "POST"
        payload = {
            "target": target,
            "patch": patch,
            **({"workspace": {"type": "agent", "id": app_id.strip()}} if bound else {}),
            **(
                {"baseRevision": base_revision.strip() or str(bound.get("revision") or "")}
                if base_revision.strip() or bound
                else {}
            ),
        }
    else:
        return _lcagent_tool_text(f"错误：不支持的 action: {action}")

    try:
        with httpx.Client(timeout=httpx.Timeout(60.0, connect=15.0)) as client:
            response = client.request(
                method,
                f"{base}{path}",
                json=payload,
                headers={
                    "Authorization": auth,
                    "Content-Type": "application/json",
                },
            )
    except httpx.RequestError as exc:
        logger.warning("manage_lcagent_agent: request error %s", exc)
        return _lcagent_tool_text(f"访问 LCAgent Agent 接口失败: {exc}")

    try:
        body: Any = response.json()
    except json.JSONDecodeError:
        body = {"message": (response.text or "")[:1000]}
    if response.status_code >= 400:
        return _lcagent_tool_text(
            json.dumps(
                {"ok": False, "httpStatus": response.status_code, "error": body},
                ensure_ascii=False,
            ),
        )

    if action == "propose" and isinstance(body, dict):
        body = {
            "kind": "lcagent_agent_change_set",
            "requiresUserConfirmation": True,
            "nextAction": "请用户在当前聊天的 ChangeSet 卡片检查改动并点击确认应用",
            **body,
        }
    return _lcagent_tool_text(json.dumps(body, ensure_ascii=False))


def run_lcagent_workflow(  # pylint: disable=too-many-return-statements,too-many-branches
    action: Literal["start", "get_run", "get_events", "get_node_result", "stop", "resume"],
    app_id: str = "",
    scope: str = "",
    node_id: str = "",
    base_revision: str = "",
    inputs_json: str = "",
    boundary_inputs_json: str = "",
    run_id: str = "",
    after_sequence: int = 0,
    parent_run_id: str = "",
) -> ToolResponse:
    """创建/查询/停止/恢复统一工作流调试运行（node、downstream、workflow scope）。

    这是唯一的调试运行入口：``start`` 不阻塞，服务端返回 ``lcagent_workflow_run``
    结构化结果（runId/status/scope/baseRevision/approvalRequired/resumable），前端会
    渲染 WorkflowRunCard。运行失败后的修复必须走 ``manage_lcagent_workflow`` 的
    ChangeSet 确认流程，然后用新的 ``baseRevision`` 和 ``parent_run_id`` 创建子运行；
    不得声称未确认的修改已修复。``approvalRequired=true`` 的运行由用户在卡片确认，
    不要在对话里声称已执行。

    Args:
        action: ``start`` 创建运行；``get_run`` 查询运行权威状态；``get_events``
            按 ``after_sequence`` 增量拉取事件；``get_node_result`` 读取某节点
            结构化输入/输出/错误；``stop`` 请求停止；``resume`` 仅对
            ``resumable=true`` 的运行有效。
        app_id: 目标工作流应用 UUID；留空使用编辑器 workspace binding。
        scope: ``start`` 必填：``node``（单节点）/``downstream``（该节点及下游）/
            ``workflow``（全图）。``node``/``downstream`` 必须带 ``node_id``。
        node_id: 目标节点稳定 ID（来自 context/事件中的 nodeId，不要用显示名）。
        base_revision: ``start`` 必填的草稿图哈希（来自 context 的 revision）。
        inputs_json: 节点/全图输入，JSON 对象字符串。
        boundary_inputs_json: ``downstream`` 边界输入，键为 ``"nodeId:inputName"``。
        run_id: 已有运行 UUID（除 ``start`` 外必填）。
        after_sequence: ``get_events`` 的续读游标（上次最大 sequence）。
        parent_run_id: repair 重跑时指向失败运行的 UUID。

    Returns:
        服务端结构化 JSON；错误时为含 ``ok=false`` 与稳定 ``code`` 的 JSON。
    """
    meta = get_process_request_meta()
    base = (meta.get("lcagent_console_api_base") or "").strip().rstrip("/")
    auth = get_request_authorization().strip()
    if not base:
        return _lcagent_tool_text("错误：缺少 lcagent_console_api_base。")
    if not auth:
        return _lcagent_tool_text("错误：缺少 Authorization，无法访问工作流。")
    bound, binding_error = _bound_workspace(meta, "workflow")
    if binding_error:
        return _lcagent_tool_text(f"错误：{binding_error}")
    if bound:
        bound_id = str(bound["id"])
        if app_id.strip() and app_id.strip() != bound_id:
            return _lcagent_tool_text(
                "错误：WORKSPACE_BINDING_MISMATCH: 当前助手不能运行其它工作流。"
            )
        app_id = bound_id

    method = "GET"
    payload = None
    if action == "start":
        if not app_id.strip():
            return _lcagent_tool_text("错误：start 需要 app_id 或编辑器 workspace binding。")
        if not scope.strip():
            return _lcagent_tool_text("错误：start 需要 scope（node|downstream|workflow）。")
        revision = base_revision.strip() or (str(bound.get("revision") or "").strip() if bound else "")
        if not revision:
            return _lcagent_tool_text(
                "错误：REVISION_REQUIRED: start 需要 base_revision，请先读取 context。"
            )
        inputs: dict[str, Any] = {}
        if inputs_json.strip():
            try:
                parsed_inputs = json.loads(inputs_json)
            except json.JSONDecodeError as exc:
                return _lcagent_tool_text(f"错误：inputs_json 不是合法 JSON: {exc}")
            if not isinstance(parsed_inputs, dict):
                return _lcagent_tool_text("错误：inputs_json 顶层必须是对象。")
            inputs = parsed_inputs
        boundary_inputs: dict[str, Any] = {}
        if boundary_inputs_json.strip():
            try:
                parsed_boundary = json.loads(boundary_inputs_json)
            except json.JSONDecodeError as exc:
                return _lcagent_tool_text(f"错误：boundary_inputs_json 不是合法 JSON: {exc}")
            if not isinstance(parsed_boundary, dict):
                return _lcagent_tool_text("错误：boundary_inputs_json 顶层必须是对象。")
            boundary_inputs = parsed_boundary
        idempotency_key = "run:{app}:{scope}:{node}:{rev}:{parent}".format(
            app=app_id.strip(),
            scope=scope.strip(),
            node=node_id.strip() or "-",
            rev=revision[:24],
            parent=parent_run_id.strip() or "-",
        )
        method = "POST"
        path = "/console/api/workflow-runs"
        payload = {
            "workspace": {"type": "workflow", "id": app_id.strip()},
            "baseRevision": revision,
            "scope": scope.strip(),
            "inputs": inputs,
            "boundaryInputs": boundary_inputs,
            "debug": True,
            "idempotencyKey": idempotency_key,
            **({"nodeId": node_id.strip()} if node_id.strip() else {}),
            **({"parentRunId": parent_run_id.strip()} if parent_run_id.strip() else {}),
        }
    elif action == "get_run":
        if not run_id.strip():
            return _lcagent_tool_text("错误：get_run 需要 run_id。")
        path = f"/console/api/workflow-runs/{run_id.strip()}"
    elif action == "get_events":
        if not run_id.strip():
            return _lcagent_tool_text("错误：get_events 需要 run_id。")
        cursor = max(0, int(after_sequence or 0))
        path = f"/console/api/workflow-runs/{run_id.strip()}/events?afterSequence={cursor}&limit=200"
    elif action == "get_node_result":
        if not run_id.strip():
            return _lcagent_tool_text("错误：get_node_result 需要 run_id。")
        if not node_id.strip():
            return _lcagent_tool_text("错误：get_node_result 需要 node_id。")
        path = f"/console/api/workflow-runs/{run_id.strip()}/nodes/{node_id.strip()}"
    elif action == "stop":
        if not run_id.strip():
            return _lcagent_tool_text("错误：stop 需要 run_id。")
        method = "POST"
        path = f"/console/api/workflow-runs/{run_id.strip()}/stop"
        payload = {"reason": "assistant_requested"}
    elif action == "resume":
        if not run_id.strip():
            return _lcagent_tool_text("错误：resume 需要 run_id。")
        method = "POST"
        path = f"/console/api/workflow-runs/{run_id.strip()}/resume"
        payload = {}
    else:
        return _lcagent_tool_text(f"错误：不支持的 action: {action}")

    try:
        with httpx.Client(timeout=httpx.Timeout(60.0, connect=15.0)) as client:
            response = client.request(
                method,
                f"{base}{path}",
                json=payload,
                headers={
                    "Authorization": auth,
                    "Content-Type": "application/json",
                },
            )
    except httpx.RequestError as exc:
        logger.warning("run_lcagent_workflow: request error %s", exc)
        return _lcagent_tool_text(f"访问 LCAgent 运行接口失败: {exc}")

    try:
        body: Any = response.json()
    except json.JSONDecodeError:
        body = {"message": (response.text or "")[:1000]}
    if response.status_code >= 400:
        return _lcagent_tool_text(
            json.dumps(
                {"ok": False, "httpStatus": response.status_code, "error": body},
                ensure_ascii=False,
            ),
        )
    if action == "start" and isinstance(body, dict):
        body = {"kind": "lcagent_workflow_run", "requiresUserConfirmation": False, **body}
    return _lcagent_tool_text(json.dumps(body, ensure_ascii=False))
