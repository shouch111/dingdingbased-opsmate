# 更新日志

## [v0.4.0] - 2026-07-09

### 🆕 新增 - 定时重新提醒
- 新增 `scheduler.py`：APScheduler 后台调度器，每分钟扫描超时任务
- 任务 assigned 后 30 分钟未解决 -> 钉钉私聊提醒工程师（循环提醒）
- 达到最大提醒次数（默认 3 次，1.5 小时）-> 自动转派其他工程师（排除当前）
- 仅一名工程师无法转派时 -> 继续提醒 + 通知 IT 群人工介入
- 转派时通知提交人 + 新工程师
- 提醒记录复用 feedbacks 表（feedback_by=系统提醒|工程师名），不改表结构
- 间隔和次数可通过 `.env` 配置（REMINDER_INTERVAL_MINUTES / REMINDER_MAX_COUNT）

### 🔧 优化
- `graph.py`：assign_engineer() 新增 exclude_name 参数，转派时排除当前工程师
- `db_manager.py`：新增 get_assigned_tasks / count_reminders / get_last_reminder_time / create_reminder
- `main.py`：启动时自动启动定时提醒调度器

### 📦 依赖
- 新增 apscheduler>=3.10.0

---

## [v0.3.0] - 2025 第一阶段

### 🆕 新增 — 任务持久化
- 引入 MySQL + SQLAlchemy ORM，任务/工程师/反馈数据持久化
- 新增 `database.py`（ORM 模型 + 连接管理 + 自动建库建表）
- 新增 `db_manager.py`（全部 CRUD 操作封装）
- 任务状态机：auto_answered / assigned / resolved
- `current_load` 改为动态查询，不再手动维护
- `main.py` 启动时自动建库建表 + engineers.json 数据迁移
- 新增 `/tasks`、`/engineers` API 接口

### 🆕 新增 — 反馈闭环
- 新增 `feedback.py`：反馈识别 + 路由 + 处理逻辑
- 钉钉消息先判断反馈（关键词匹配），再走新任务流程
- 用户反馈"未解决"：auto_answered 升级分配工程师，assigned 重新催办
- 用户反馈"已解决"：标记 resolved 关闭任务
- 工程师回复"已解决"：标记 resolved + 私聊通知提交人
- 方案 A：按钉钉 ID 追踪用户最近一条 active 任务

### 🆕 新增 — 负载均衡
- 混合策略：LLM 筛技能（返回候选人列表）+ 算法做负载均衡
- 优先分配无任务工程师，同负载随机选择
- 全部不在岗时仍从匹配人选最低负载（方案 B）
- `assign_engineer()` 独立函数，供 assign_node 和反馈升级复用

### 🔧 优化
- `tools.py`：load_engineers 改为查 DB，JSON 降级兜底
- `dingtalk_stream.py`：_auto_fill_engineer_id 改为写 DB
- `graph.py`：answer/assign 节点末尾存库，回答附任务编号
- `models.py`：AgentState 新增 submitter_id、task_no 字段
- ORM 模型使用 SQLAlchemy 2.0 Mapped 风格

### 📦 依赖
- 新增 sqlalchemy>=2.0.0、pymysql>=1.1.0、cryptography>=42.0.0

---

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
