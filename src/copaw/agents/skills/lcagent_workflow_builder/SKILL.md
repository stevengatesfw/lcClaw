---
name: lcagent_workflow_builder
description: "在 LCAgent 中创建或修改工作流：读取权威目录，正确使用自动起止节点与可用模型，生成待用户确认的 ChangeSet，并在确认后核验创建结果。"
metadata:
  builtin_skill_version: "3.1"
  copaw:
    emoji: "🧩"
    requires: {}
---

# LCAgent 工作流创建与编辑

当用户要求创建、搭建、修改 LCAgent 工作流或画布时，必须使用本 skill。

## 强制流程

1. 先调用 `manage_lcagent_workflow(action="catalog")`，读取当前用户实时可用的组件、模型和资源。不要依赖记忆猜测目录内容。
   - `components[].nodeType` 是 `add_node.node_type` 的唯一合法取值。
   - `patchSchema` 是 Patch operation 的权威 JSON Schema；只使用其中声明的 operation 和字段。
   - `components[].configSchema` 是节点详细参数的权威 Schema；遵守类型、枚举、范围与 `additionalProperties`。
   - 当前目录为 V2-only；不要提议目录中不存在的旧节点，也不要把旧节点记忆当成当前能力。
   - 创作类节点使用逻辑组件名（如 `llm-text-generation`），不要自行改写为其底层 `persistedType`。
2. 创建新工作流时，平台已经自动创建：
   - `__start__`（开始节点）
   - `__end__`（结束节点）
   Patch 中禁止再次添加 `start` 或 `end`，只需连接这两个固定 ID。
3. 组件的 `modelKinds` 非空时，只能选择 `resources.models[].kind` 与其匹配的模型，并把该条目的 `nodeConfig` 原样合并到 `add_node.config`。禁止自行填写或猜测：
   - `payload__model_source`
   - `payload__source`
   - `payload__base_model`
   - `payload__model_id`
   - 模型条目存在 `capabilities` 时，它是分辨率、比例、时长、帧率等模型相关参数的更窄约束；不得只按组件通用 `configSchema` 选择模型不支持的值。
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
9. MCP、知识库、数据库、Skill 和已发布工作流是 AgentV2 的资源能力，不要把连接地址、密钥或检索配置内联进节点：
   - 先从 `resources.bindingSchemas`（同内容也在 `resources.resourceSchemas`）及对应资源列表选择实时 `resource_id`，再使用 `bind_resource`。
   - 对 `agent-v2` / `database-agent-v2`，平台会把绑定同步到 `payload__agent_config.capabilities`；不要使用已移除的 `toolIds`。
   - MCP 条目中的 `tools[].inputSchema` 只用于了解工具参数；不要在 Patch 中伪造 MCP 服务配置。
   - 知识库条目的 `queryParamsSchema` 定义 `topk`、检索模式、BM25/混合权重和重排开关；只传该 Schema 声明的键，索引/嵌入/重排模型仍由知识库资源维护。
10. 每个新增节点都必须位于一条完整的 `__start__ → ... → __end__` 路径上。多端口控制流组件必须显式写端口。
11. 调用一次 `manage_lcagent_workflow(action="validate", ...)`。若失败，根据返回的 diagnostics 修正具体字段；不要盲目重复提交同一 Patch。
12. validate 成功只会生成 `pending` ChangeSet，应用此时尚未创建或修改。当前聊天会直接展示 Diff 和“确认应用/取消”按钮；引导用户在对话内确认，不要要求用户跳转首页。不得声称“已创建”。
13. 平台默认会对 Agent 修改的拓扑自动从左到右排版，并仅在边界为空时推导输入输出。修复已有画布布局时提交 `layout_graph`；精确边界参数始终使用 `set_workflow_io`。
14. 用户表示已经确认后，调用 `manage_lcagent_workflow(action="get_change_set", change_set_id="...")`：
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

`MODEL_NODE_CONFIG` 必须替换为 catalog 某一匹配模型的完整 `nodeConfig` 对象，不得保留占位键。

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
        "payload__prompt": "请准确回答用户问题",
        "MODEL_NODE_CONFIG": "从 catalog 原样合并，不保留此占位键"
      }
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
