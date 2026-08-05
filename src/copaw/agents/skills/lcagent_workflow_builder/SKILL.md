---
name: lcagent_workflow_builder
description: "在 LCAgent 中创建或修改工作流：读取权威目录，正确使用自动起止节点与可用模型，生成待用户确认的 ChangeSet，并在确认后核验创建结果。"
metadata:
  builtin_skill_version: "1.0"
  copaw:
    emoji: "🧩"
    requires: {}
---

# LCAgent 工作流创建与编辑

当用户要求创建、搭建、修改 LCAgent 工作流或画布时，必须使用本 skill。

## 强制流程

1. 先调用 `manage_lcagent_workflow(action="catalog")`，读取当前用户实时可用的组件、模型和资源。不要依赖记忆猜测目录内容。
2. 创建新工作流时，平台已经自动创建：
   - `__start__`（开始节点）
   - `__end__`（结束节点）
   Patch 中禁止再次添加 `start` 或 `end`，只需连接这两个固定 ID。
3. 需要 LLM 或 AgentV2 时，只能从 `resources.models` 选择模型，并把该条目的 `nodeConfig` 原样合并到 `add_node.config`。禁止自行填写或猜测：
   - `payload__model_source`
   - `payload__source`
   - `payload__base_model`
   - `payload__model_id`
4. `payload__model_source` 的合法值只有 `online_model`、`inference_service`、`auto`。AgentV2 不支持 `auto`，必须使用 catalog 给出的明确模型配置。
5. 每个新增节点都必须位于一条完整的 `__start__ → ... → __end__` 路径上。多端口组件必须显式写端口；单端口组件可以省略。
6. 调用一次 `manage_lcagent_workflow(action="validate", ...)`。若失败，根据返回的 diagnostics 修正具体字段；不要盲目重复提交同一 Patch。
7. validate 成功只会生成 `pending` ChangeSet，应用此时尚未创建或修改。明确告诉用户：必须在 LCAgent 首页的变更卡片中查看 Diff 并点击“确认”。不得声称“已创建”。
8. 用户表示已经确认后，调用 `manage_lcagent_workflow(action="get_change_set", change_set_id="...")`：
   - 只有 `status=applied` 且 `target.appId` 非空，才可报告创建成功并给出画布入口。
   - `status=pending` 表示仍未创建。
   - `rejected` / `expired` 表示未创建，需要重新提出 ChangeSet。

## 最小 Patch 形状

以下仅展示拓扑；`MODEL_NODE_CONFIG` 必须替换为 catalog 某一模型的完整 `nodeConfig` 对象，不得手写猜测。

```json
{
  "summary": "创建问答工作流",
  "operations": [
    {
      "type": "add_node",
      "temp_id": "answer",
      "node_type": "llm",
      "config": {
        "title": "问答模型",
        "payload__prompt": "请准确回答用户问题",
        "MODEL_NODE_CONFIG": "从 catalog 原样合并，不保留此占位键"
      }
    },
    {"type": "connect_nodes", "source": "__start__", "target": "answer"},
    {"type": "connect_nodes", "source": "answer", "target": "__end__"}
  ]
}
```

## 输出要求

- validate 成功：说“修改方案已生成，尚未生效”，给出 ChangeSet ID，并引导点击首页卡片确认。
- 确认核验成功：说“已创建/已应用”，给出 `target.appId`。
- 不把 Schema、拓扑、编译校验通过描述为真实运行成功；validate 不调用模型和外部工具。
