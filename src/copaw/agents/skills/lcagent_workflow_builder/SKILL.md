---
name: lcagent_workflow_builder
description: "在 LCAgent 中创建或修改工作流：读取权威目录，正确使用自动起止节点与可用模型，生成待用户确认的 ChangeSet，并在确认后核验创建结果。"
metadata:
  builtin_skill_version: "4.2"
  copaw:
    emoji: "🧩"
    requires: {}
---

# LCAgent 工作流创建与编辑

当用户要求创建、搭建、修改 LCAgent 工作流或画布时，必须使用本 skill。

## 强制流程

1. 若当前请求 meta 带编辑器 workspace binding，先调用对应工具的 `context`，并只编辑该绑定的 workflow/agent；需要细节时按稳定 ID 调用 `get_node` / `get_node_schema`。禁止传入另一个 app_id。随后调用 `manage_lcagent_workflow(action="catalog")` 读取实时**目录索引**（`componentIndex` 组件清单、`operationIndex` 操作字段清单、`creationGuide` 规则、瘦身 `resources`）。不要依赖记忆猜测目录内容。
   - `componentIndex[].nodeType` 是 `add_node.node_type` 的唯一合法取值。
   - `add_node` 或 `update_node_config` 前，必须先调用 `manage_lcagent_workflow(action="get_component_schema", component_type="<nodeType>")` 获取该组件完整 `configSchema` 与 `defaults`；遵守类型、枚举、范围、默认值与 `additionalProperties`，不得只看 `editableFields` 字段名后猜测复杂参数。
   - `operationIndex` + `creationGuide.operations` 是 Patch operation 词汇的权威来源；只使用其中声明的 operation 和字段。
   - 禁止凭记忆猜测 node_type / 字段名后靠 validate 试错，禁止用 shell、curl 或浏览器绕过工具探索目录 API。
   - 当前目录为 V2-only；不要提议目录中不存在的旧节点，也不要把旧节点记忆当成当前能力。
   - 创作类节点使用逻辑组件名（如 `llm-text-generation`），不要自行改写为其底层 `persistedType`。
2. 创建新工作流时，平台已经自动创建：
   - `__start__`（开始节点）
   - `__end__`（结束节点）
   Patch 中禁止再次添加 `start` 或 `end`，只需连接这两个固定 ID。
3. 组件的 `modelKinds` 非空时，只能选择 `resources.models[].kind` 与其匹配的模型（索引条目只有 `id`/`name`/`kind`/`source`/`provider`）。新增节点可先使用 Auto 默认值；选择明确模型时优先使用 `set_model.selection_id=resources.models[].id`，由服务端解析当前 catalog 配置，无需内联 nodeConfig。仅当确需把配置内联进 `add_node.config` 时，调用 `manage_lcagent_workflow(action="get_model_detail", resource_id="<models[].id>")` 获取该模型的完整 `nodeConfig` 与 `capabilities` 并原样复制。禁止自行填写或猜测：
   - `payload__model_source`
   - `payload__source`
   - `payload__base_model`
   - `payload__model_id`
   - `get_model_detail` 返回的 `capabilities` 是分辨率、比例、时长、帧率等模型相关参数的更窄约束；不得只按组件通用 `configSchema` 选择模型不支持的值。
   - 高级参数使用 `set_advanced_parameters`，只传 `configSchema.properties.payload__model_generate_control` 声明的键。
4. `payload__model_source` 的合法值只有 `online_model`、`inference_service`、`auto`。`agent-v2` / `database-agent-v2` 不支持 `auto`，必须使用 catalog 给出的明确模型配置。
5. 用户明确要求输入输出时，使用 `set_workflow_io`。不要手写开始/结束节点的底层 `config__*ports` 或 `config__*shape`：
   - `inputs` 对应开始节点输出参数。
   - `outputs` 对应结束节点返回参数。
   - `value_type` 只能取 `string|number|integer|boolean|object|array|file|image|video|audio|any`。
6. 一个节点输出要作为另一节点输入时，使用 `bind_node_input`，不要用裸 `connect_nodes` 冒充变量引用：
   - `source` / `target` 可使用本 Patch 中的 `temp_id`。
   - `source_output` / `target_input` 必须来自 catalog 的端口或 Shape 名称。
   - 同一目标输入默认替换旧绑定；用户禁止替换时写 `replace=false`。
   - `connect_nodes` 只用于不需要字段 mapping 的控制流连接。
7. 分支/迭代的条件变量必须使用 `bind_condition_reference`：
   - `target` 是 `branch-v2` 或 `iteration-v2`，`condition_id` 来自分支条件或 `exitCondition.conditions` 的 `id`。
   - `source_output` 使用来源节点的 Shape 变量名/输出端口，`path` 可选；平台会同时更新 `condition.left` 和 `__ref__<condition_id>` 引用边。
   - 迭代的 `source`、`keyReference`、`initialInput` 或 `customMapping` 使用 `bind_variable_reference`；`customMapping` 必须提供 `mapping_target`。
8. 提示词内引用必须使用 `insert_prompt_reference`，不要只把节点名拼进字符串：
   - 平台会把占位符写入 `payload__prompt`，并把 `{nodeId, sourcePortId, outputKey, valueType, ...}` 写入 `payload__prompt_refs` 或指定的媒体 refs 字段。
   - 文本默认 `reference_type="prompt"`；图像/视频/音频可使用 `reference_type="image"` 或明确 `reference_field`（如 `payload__base_image_refs`、`payload__first_frame_refs`）。
   - `path` 用于对象/数组下钻。该操作也会生成仅用于运行顺序的引用边。
   - 设置提示词正文使用 `set_prompt`；它与引用操作同样进入 typed Canvas Patch，不要退回通用字段字典。
   - 文本/图片/视频/音频和资源内容块使用 `set_content_block` / `remove_content_block`。媒体 `slot` 必须取 `operationIndex`/`creationGuide.operations.setContentBlock` 声明值，`value` 只保存已上传文件路径或非密钥资源引用；不得内联凭据。
9. MCP、知识库、数据库、Skill 和已发布工作流是 AgentV2 的资源能力，不要把连接地址、密钥或检索配置内联进节点：
   - 先从 `resources.bindingSchemas` 及对应资源索引列表选择实时 `resource_id`，再使用 `bind_resource`。
   - 对 `agent-v2` / `database-agent-v2`，平台会把绑定同步到 `payload__agent_config.capabilities`；不要使用已移除的 `toolIds`。
   - 索引里的 MCP 条目只有 `toolNames`；需要了解工具参数时调用 `manage_lcagent_workflow(action="get_mcp_tools", resource_id="<mcpServers[].id>")` 读取 `tools[].inputSchema`。不要在 Patch 中伪造 MCP 服务配置。
   - 知识库检索参数 Schema 在 `bindingSchemas.knowledge.queryParamsSchema`，定义 `topk`、检索模式、BM25/混合权重和重排开关；只传该 Schema 声明的键，索引/嵌入/重排模型仍由知识库资源维护。
10. 每个新增节点都必须位于一条完整的 `__start__ → ... → __end__` 路径上。多端口控制流组件必须显式写端口。
11. 调用一次 `manage_lcagent_workflow(action="validate", ...)`。若失败，按返回的结构化信息修正：`diagnostics[].code/message`（如 `SCHEMA_VALIDATION_FAILED`、`NODE_NOT_FOUND`，消息里会列出合法字段或现有节点 id）、`errors[].loc`（定位到具体 operation 与字段）、`operationTypes`（合法操作清单，`UNKNOWN_OPERATION_TYPE` 时按它改写）。只修正报错的字段后重新提交完整 operations 数组；不要盲目重复提交同一 Patch，也不要因此重新拉取全量目录。
12. validate 成功只会生成 `pending` ChangeSet，应用此时尚未创建或修改。成功返回是**摘要**（`diff` 为前 20 条要点、`diffTotal`/`diffTruncated` 标注截断，完整 Diff 由前端卡片自行拉取），足以向用户概述改动。当前聊天会直接展示 Diff 和“确认应用/取消”按钮；引导用户在对话内确认，不要要求用户跳转首页。不得声称“已创建”。
13. 平台默认会对 Agent 修改的拓扑自动从左到右排版，并仅在边界为空时推导输入输出。修复已有画布布局时提交 `layout_graph`；精确边界参数始终使用 `set_workflow_io`。
14. 用户表示已经确认后，调用**一次** `manage_lcagent_workflow(action="get_change_set", change_set_id="...")`（返回状态摘要，足够核验，不要重复调用）：
   - 只有 `status=applied` 且 `target.appId` 非空，才可报告创建成功并给出画布入口。
   - `status=pending` 表示仍未创建。
   - `rejected` / `expired` 表示未创建，需要重新提出 ChangeSet。

## 应用类型边界

- 独立 Agent 不是“没有节点”：其本质是隐藏的固定 V2 画布 `__start__ → hidden-agent-v2 → __end__`。修改身份、策略、系统提示词、模型或资源绑定时使用 `manage_lcagent_agent`；该工具修改中间 AgentV2 节点后由平台重建整图。
- `manage_lcagent_agent(action="catalog")` 返回的 `resources.models[]` 同时包含稳定选择键 `id`、运行时 `executorModelId` 和完整非敏感 `nodeConfig`；`update_model.executorModelId` 使用前者对应条目的 `executorModelId`，并原样提交其 `nodeConfig`。
- 独立 Agent 的隐藏拓扑不可增删节点或改线；用户需要加入文本生成等其它节点时，才应创建或修改工作流应用。
- 用户要编辑工作流画布：使用本 skill 和 `manage_lcagent_workflow`。
- `agent-v2` 和 `database-agent-v2` 也是工作流画布中的 V2 节点。不要因为目标节点叫 Agent，就断言整个目标一定是独立 Agent 应用；应以目标应用类型和实时 catalog 为准。

## 带输入输出与引用的 Patch

`MODEL_NODE_CONFIG` 仅在必须内联模型配置时使用：替换为 `get_model_detail` 返回的完整 `nodeConfig` 对象，不得保留占位键。优先做法是新节点保留 Auto 默认值，再在同一 Patch 里用 `set_model.selection_id` 指定明确模型（服务端解析 nodeConfig，无需内联）。

```json
{
  "summary": "创建问答工作流",
  "operations": [
    {
      "type": "set_workflow_io",
      "inputs": [{"name": "question", "value_type": "string", "required": true}],
      "outputs": [{"name": "answer", "value_type": "string", "required": true}]
    },
    {
      "type": "add_node",
      "temp_id": "answer",
      "node_type": "llm-text-generation",
      "config": {
        "title": "问答模型",
        "payload__prompt": "请准确回答用户问题"
      }
    },
    {
      "type": "set_model",
      "node_id": "answer",
      "selection_id": "<catalog 索引 resources.models[].id；用户未指定模型时可省略本操作，使用 Auto 默认>"
    },
    {
      "type": "bind_node_input",
      "source": "__start__",
      "target": "answer",
      "source_output": "question",
      "target_input": "query"
    },
    {
      "type": "bind_node_input",
      "source": "answer",
      "target": "__end__",
      "source_output": "output",
      "target_input": "answer"
    },
    {
      "type": "insert_prompt_reference",
      "source": "__start__",
      "target": "answer",
      "source_output": "question",
      "prompt_field": "user",
      "reference_type": "prompt"
    }
  ]
}
```

## 输出要求

- validate 成功：说“修改方案已生成，尚未生效”，并引导点击当前聊天卡片中的“确认应用”。不要重复输出冗长的手工操作步骤。
- 确认核验成功：说“已创建/已应用”，给出 `target.appId`。
- 不把 Schema、拓扑、编译校验通过描述为真实运行成功；validate 不调用模型和外部工具。

## 运行调试与受控修复（run_lcagent_workflow）

- 真实运行必须使用 `run_lcagent_workflow`，不得用编辑工具冒充运行，也不得声称 validate 成功即“运行成功”。
- `start` 需要 `scope`（`node|downstream|workflow`）、`base_revision`（来自 `context` 的 revision）与输入；`node`/`downstream` 必须带稳定 `node_id`（来自 context/事件，不要用显示名），`downstream` 的边界输入键为 `"nodeId:inputName"`。
- `start` 返回 `kind=lcagent_workflow_run` 的运行卡片后即返回，不阻塞等待长任务；后续用 `get_run` / `get_events`（带 `after_sequence` 续读）/ `get_node_result` 查看状态与节点结构化输入/输出/错误。
- 高风险运行会返回 `approvalRequired=true` 并保持 `waiting_human`：提示用户在卡片中批准或拒绝；批准只执行已持久化的精确运行请求，禁止重新生成 scope 或 inputs。
- 失败修复闭环必须按顺序：`get_run`/`get_events` 锁定 `node.failed` 的稳定 nodeId 与 `error.code` → `get_node`/`get_node_schema` 定位原因 → `manage_lcagent_workflow(action="validate")` 生成 ChangeSet → 用户确认 → `get_change_set` 核验 `status=applied` 与新 revision → `start` 新运行（`parent_run_id` 指向失败运行，`base_revision` 使用新 revision）。
- ChangeSet 未确认、被拒绝、或返回 `REVISION_CONFLICT` 时，禁止声称“已修复”或直接重跑旧 revision。
- `stop`/`resume` 只调用权威 API；`resumable=false` 的运行停止后不可继续，应创建新运行。

