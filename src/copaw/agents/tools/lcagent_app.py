"""LCAgent platform callbacks (Flask console API) from CoPaw tools."""
# NOTE: do NOT add `from __future__ import annotations` here. agentscope's
# _parse_tool_function reads raw param.annotation and feeds it to a dynamic
# pydantic model; stringized annotations (e.g. 'Literal[...]') cannot be
# resolved in pydantic's rebuild namespace and raise class-not-fully-defined.

import json
import logging
from typing import Any, Literal

import httpx
from agentscope.message import TextBlock
from agentscope.tool import ToolResponse

from ...context import get_process_request_meta, get_request_authorization
from .lcagent_media import register_invoke_lcagent_reply

logger = logging.getLogger(__name__)


def _lcagent_tool_text(text: str) -> ToolResponse:
    return ToolResponse(
        content=[TextBlock(type="text", text=text)],
    )


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
    action: Literal["catalog", "validate", "get_change_set"],
    app_id: str = "",
    patch_json: str = "",
    change_set_id: str = "",
    create_app_name: str = "",
    create_app_description: str = "",
    base_revision: str = "",
) -> ToolResponse:
    """读取工作流编辑目录或生成待用户确认的工作流变更集。

    此工具只能读取和预校验，不能确认、发布或删除应用。``validate`` 成功后，
    当前聊天会直接渲染含 Diff 与确认/取消按钮的 ChangeSet 卡片；请引导用户在
    对话内确认，不要让用户跳转首页寻找卡片，也不得声称修改已经生效。
    创建前必须先调用 ``catalog``。目录是 V2 画布组件的唯一事实源：新画布已
    自带 ``__start__``/``__end__``，禁止重复添加；按组件 ``modelKinds`` 选择模型，
    并原样复制 ``resources.models[].nodeConfig``，不得根据模型名猜测 source/model_id。
    ``patchSchema`` 定义可用 Patch 操作，``components[].configSchema`` 定义节点参数
    类型、枚举、范围与默认值；模型条目的 ``capabilities`` 是该模型参数的更窄约束。
    不得只看 ``editableFields`` 后猜测复杂参数。
    独立 Agent 应用应使用 ``manage_lcagent_agent``；工作流里的 ``agent-v2`` /
    ``database-agent-v2`` 是画布节点，仍使用本工具。用户确认后用
    ``get_change_set`` 核验 ``status=applied`` 和 ``target.appId``，否则应用仍未创建。

    Args:
        action: ``catalog`` 获取组件/资源目录；``validate`` 预校验 Patch；
            ``get_change_set`` 查询变更集状态。
        app_id: 修改已有应用时的应用 UUID；留空表示创建新应用。
        patch_json: ``validate`` 所需的 JSON，格式为
            ``{"summary":"...","operations":[...]}``。
            除增删节点/连线/更新配置外，还支持资源绑定操作：
            ``{"type":"bind_resource","node_id":"...","resource_type":
            "skill|mcp|knowledge|database|tool|app","resource_id":"..."}``
            与同形状的 ``unbind_resource``；绑定在用户确认变更集时与图写入
            同一事务生效，validate 期会校验资源存在性。修复已有画布的重叠、
            逆向布局或空边界参数可提交 ``{"type":"layout_graph"}``；平台还会
            默认对 Agent 修改后的拓扑自动排版并推导空的开始/结束 Schema。
            精确声明输入输出使用 ``set_workflow_io``；把某节点输出引用为另一节点
            输入使用 ``bind_node_input``，由平台生成带字段 mapping 的边。普通
            ``connect_nodes`` 仅用于无需字段 mapping 的控制流连接。分支/迭代条件
            使用 ``bind_condition_reference``，迭代 ``source``/``keyReference``/
            ``initialInput``/``customMapping`` 使用 ``bind_variable_reference``；
            提示词内引用使用 ``insert_prompt_reference``（会同步占位符、结构化
            ``payload__prompt_refs``/媒体 refs 与引用边）。目录的
            ``resources.bindingSchemas``/``resources.resourceSchemas`` 说明 MCP、数据库、知识库、Skill 和已发布
            工作流的资源列表、能力字段与绑定方式；MCP 条目中的 ``tools[].inputSchema``
            仅用于读取工具参数，不要把敏感连接配置写入 Patch。
        change_set_id: ``get_change_set`` 所需的变更集 ID。
        create_app_name: 创建应用时的名称。
        create_app_description: 创建应用时的描述。
        base_revision: 修改已有草稿时可选的基础版本哈希。

    Returns:
        包含结构化目录、诊断或 ``lcagent_change_set`` 的 JSON。变更集仍需用户确认。
    """
    meta = get_process_request_meta()
    base = (meta.get("lcagent_console_api_base") or "").strip().rstrip("/")
    auth = get_request_authorization().strip()
    if not base:
        return _lcagent_tool_text("错误：缺少 lcagent_console_api_base。")
    if not auth:
        return _lcagent_tool_text("错误：缺少 Authorization，无法访问工作流。")

    method = "GET"
    payload = None
    if action == "catalog":
        path = "/console/api/workflow-editing/catalog"
    elif action == "get_change_set":
        if not change_set_id.strip():
            return _lcagent_tool_text("错误：get_change_set 需要 change_set_id。")
        path = f"/console/api/workflow-editing/change-sets/{change_set_id.strip()}"
    elif action == "validate":
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
        path = "/console/api/workflow-editing/change-sets/validate"
        method = "POST"
        payload = {
            "target": target,
            "patch": patch,
            **({"baseRevision": base_revision.strip()} if base_revision.strip() else {}),
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
    action: Literal["catalog", "get_agent", "propose", "get_change_set"],
    app_id: str = "",
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

    method = "GET"
    payload = None
    if action == "catalog":
        path = "/console/api/agent-editing/catalog"
    elif action == "get_agent":
        if not app_id.strip():
            return _lcagent_tool_text("错误：get_agent 需要 app_id。")
        path = f"/console/api/agent-editing/apps/{app_id.strip()}/agent"
    elif action == "get_change_set":
        if not change_set_id.strip():
            return _lcagent_tool_text("错误：get_change_set 需要 change_set_id。")
        path = f"/console/api/agent-editing/change-sets/{change_set_id.strip()}"
    elif action == "propose":
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
            **({"baseRevision": base_revision.strip()} if base_revision.strip() else {}),
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
