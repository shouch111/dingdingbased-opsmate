# 更新日志

## [v2.2.0] - 2026-07-10

### 🔄 重构 - 数据库统一为 PostgreSQL + pgvector
- **废弃 ChromaDB**，将两套数据库（MySQL + ChromaDB）合并为单一 PostgreSQL
- 关系型数据 + 向量数据统一存储：engineers/tasks/feedbacks/memories/knowledge_docs
- pgvector 扩展提供原生向量检索（cosine_distance），一条 SQL 完成语义检索
- 新增 `KnowledgeDoc` 表：知识库分块 + 向量，替代原 ChromaDB chroma_db/
- `Memory` 表新增 `embedding` 列，替代原 ChromaDB memory_db/
- 新增 `embedding.py`：共享 Embedding 服务（单例模型，避免重复加载）
- 记忆存储从双写（ChromaDB + MySQL）简化为一次入库（含向量）

### 🔧 优化
- `database.py`：MySQL->PostgreSQL 驱动，init_db 自动安装 pgvector 扩展
- `db_manager.py`：新增知识库 CRUD + 向量检索函数
- `tools.py`：知识库检索改用 pgvector，废弃 ChromaDB 依赖
- `memory.py`：记忆检索改用 pgvector，废弃 ChromaDB 依赖
- `postprocess.py`：简化记忆存储逻辑
- `config.py`：移除 MEMORY_DB_PATH
- `requirements.txt`：pymysql->psycopg2-binary，新增 pgvector，移除 chromadb

### 📦 依赖变更
- 移除：pymysql, langchain-chroma, chromadb
- 新增：psycopg2-binary, pgvector

### 💡 收益
- 数据库从 2 个减为 1 个，部署和运维简化
- 向量检索从独立进程变为数据库原生，消除数据一致性风险
- 记忆存储从两次写入简化为一次入库

---

## [v2.1.0] - 2026-07-10

### 🆕 新增 - 混合路由架构
- 新增 `router.py`：预处理后按意图/复杂度分流，确定性流程与 Agent 流程结合
- 闲聊/反馈/查询 -> 确定性流程（快、省、可控，不走 Agent）
- 简单报障 -> 知识库+单次LLM（固定流程，一步到位，1-2秒）
- medium/hard 报障 -> Agent+工具调用（灵活，AI 自主决策）
- 反馈无 active 任务时自动转为报障处理

### 🔧 优化
- `main.py`：handle_message 改用 router.route() 替代直接调 ai_agent
- 后处理仅对需要存库的场景执行（闲聊/反馈/查询跳过）

### 💡 设计原则
- 能确定的用规则，不确定的才交给 AI
- 简单问题 1 次 LLM 调用（v1.0 固定流程思路）
- 复杂问题才走 Agent 多轮工具调用（v2.0 思路）
- 两者结合：确定性预处理 + 按需 Agent = 企业级方案

---

## [v2.0.0] - 2026-07-10

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
