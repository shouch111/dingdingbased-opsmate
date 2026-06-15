# 更新日志

## [v0.2.0] - 2026-06-15

### 🆕 新增
- 钉钉 Stream 模式单聊机器人（WebSocket 长连接，无需公网 URL）
- 工程师钉钉 ID 自动绑定（首次发消息自动回填 `dingtalk_user_id`）
- 钉钉私聊通知：困难任务自动私发完整工单给对应工程师
- 闲聊模式预筛：问候语、致谢不再被误判为 hard 任务

### 🔧 优化
- `assign_node` 增加名字校验，防止 LLM 编造不存在的工程师
- `CLASSIFY_PROMPT` 优化，区分模糊 IT 问题与日常闲聊
- `MATCH_PROMPT` 强化约束，要求 LLM 只从名单中选择
- 私聊通知改用 `sender_staff_id`，修复 `staffId.notExisted` 错误

### 📝 文档
- 更新 README，补充钉钉 Stream 接入步骤和自动绑定说明
- 同步更新运维 Agent 框架文档

---

## [v0.1.0] - 2026-06

### 🆕 初始版本
- LangGraph 任务分类工作流（classify → retrieve/assign → answer/notify）
- FastAPI REST API（POST /task、GET /health）
- ChromaDB 向量知识库 + 中文语义检索
- LLM 难度自动分类（easy / hard）
- 简单任务知识库检索 + 自动回复
- 困难任务工程师匹配 + 企业微信/钉钉群通知
- 15 个预置知识库文档（打印机、VPN、邮箱、Office 激活等）
