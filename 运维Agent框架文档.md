# 🛠️ 运维任务分配 Agent -- 框架文档

## 一、项目概述

一个基于 **FastAPI + LangChain + PostgreSQL + pgvector** 的智能运维任务处理系统。

### 它能做什么

| 场景 | 自动化流程 |
|------|-----------|
| 用户提交「打印机连不上」 | -> 预处理（脱敏+意图+复杂度）-> 固定流程（知识库+单次LLM）-> 自动回复 -> 存库+记忆 |
| 用户提交「数据库主库崩溃」 | -> 预处理 -> Agent流程（模型路由+工具调用）-> 分配工程师 -> 存库+记忆 |
| 用户回复「还是不行」 | -> 预处理识别反馈 -> 复用feedback.py -> 升级分配/催办 |
| 工程师回复「已解决」 | -> 预处理识别反馈 -> 标记resolved -> 通知提交人 |
| 用户在钉钉给机器人发「hello」 | -> 钉钉纯转发 -> API预处理 -> 闲聊快速回复 |

### 核心技术栈

| 技术 | 在项目中扮演的角色 |
|------|-------------------|
| **FastAPI** | Web 接口 -- 统一 API 入口 `POST /api/v1/message` |
| **LangChain** | LLM 调用 + 工具调用（tool calling），单 Agent 架构 |
| **PostgreSQL** | 关系型 + 向量型统一数据库 |
| **pgvector** | PostgreSQL 向量扩展，知识库和记忆的语义检索 |
| **SQLAlchemy 2.0** | ORM，Mapped 风格类型安全 |
| **HuggingFace** | 本地 Embedding 模型（text2vec-base-chinese，768 维） |
| **APScheduler** | 后台定时调度，超时提醒/转派 |
| **钉钉 Stream SDK** | 钉钉 WebSocket 长连接，纯转发层 |

---

## 二、系统架构

### 2.1 混合路由架构

```mermaid
flowchart TD
    DT[📱 钉钉消息] --> STREAM[钉钉 Stream 纯转发]
    API_USER[👤 API/用户消息] --> MSG
    STREAM --> MSG["POST /api/v1/message 统一入口"]

    MSG --> PRE

    subgraph PREPROCESS[预处理层 preprocess.py]
        PRE["① 预处理"]
        PRE --> MASK["脱敏 手机/IP/邮箱/身份证/密码"]
        MASK --> INTENT["意图检测 报障/闲聊/反馈/转人工/查询"]
        INTENT --> CPLX["复杂度检测 simple/medium/hard"]
    end

    CPLX --> ROUTER

    subgraph ROUTERLAYER[混合路由 router.py]
        ROUTER{"② 路由分流"}
        ROUTER -->|闲聊| CASUAL["确定性流程 快速LLM回复"]
        ROUTER -->|反馈| FEEDBACK["确定性流程 复用feedback.py"]
        ROUTER -->|查询| QUERY["确定性流程 查数据库"]
        ROUTER -->|简单报障| SIMPLE["固定流程 知识库+单次LLM"]
        ROUTER -->|复杂报障| AGENT["Agent流程 AI+工具调用"]
    end

    CASUAL --> POST_FLAG{需要存库?}
    FEEDBACK --> POST_FLAG
    QUERY --> POST_FLAG
    SIMPLE --> POST_FLAG
    AGENT --> POST_FLAG

    POST_FLAG -->|是| POST
    POST_FLAG -->|否| RESP

    subgraph POSTPROCESS[后处理层 postprocess.py]
        POST["③ 后处理"]
        POST --> SAFE["脱敏回答"]
        SAFE --> STORE["入库"]
        STORE --> SUM["LLM 总结"]
        SUM --> VEC["向量化记忆"]
    end

    VEC --> RESP[响应返回]
    RESP --> STREAM
    RESP --> API_USER

    SIMPLE -.-> KB["PostgreSQL 知识库+记忆"]
    AGENT -.-> KB
    AGENT -.-> ENG["engineers 负载均衡"]
    STORE -.-> DB["PostgreSQL 任务/记忆"]
```

### 2.2 核心设计原则

**能确定的用规则，不确定的才交给 AI。**

| 路由分支 | 处理方式 | LLM 调用 | 存库 | 延迟 |
|---------|---------|---------|------|------|
| 闲聊 | 确定性流程：快速 LLM 回复 | 1次（200 token） | 否 | <1秒 |
| 反馈 | 确定性流程：复用 feedback.py | 0次 | 否 | <0.5秒 |
| 查询状态 | 确定性流程：查数据库 | 0次 | 否 | <0.5秒 |
| 简单报障 | 固定流程：知识库+单次 LLM | 1次 | 是 | 1-2秒 |
| 复杂报障 | Agent 流程：AI+工具调用 | 2-4次 | 是 | 3-8秒 |
| 转人工 | Agent 流程：分配工程师 | 2-4次 | 是 | 3-8秒 |

### 2.3 任务状态机

```
┌────────────────┐     用户反馈"未解决"      ┌──────────────┐     工程师回复"已解决"     ┌──────────┐
│ auto_answered  │ ─────────────────────────-> │   assigned   │ ───────────────────────-> │ resolved │
│  (自动已回答)   │                             │  (已分配)     │                           │  (已解决)  │
└────────────────┘                             └──────────────┘                           └──────────┘
        │                                           │
        │ 用户反馈"已解决"                            │ 用户反馈"未解决"-> 重新催办
        └───────────────────────────────────────────┘
```

---

## 三、项目文件结构

```
运维任务分配agent\
├── requirements.txt          ← Python 依赖
├── 运维Agent框架文档.md       ← 本文档
├── CHANGELOG.md              ← 更新日志
├── 新版架构方案.md            ← v2.0 架构设计文档（历史）
├── 第一阶段需求文档.md        ← v1.0 需求设计文档（历史）
│
└── data/                     ← 数据与代码
    ├── .env                  ← 环境变量（不提交）
    ├── engineers.json        ← 工程师名单（首次启动自动迁移到 DB）
    ├── knowledge/            ← 知识库文档（skill 文档，.md 格式）
    │
    └── src/                  ← 源代码
        ├── __init__.py       ← 包声明
        │
        ├── config.py         ← 模型路由 + 预处理 + 记忆配置
        ├── models.py         ← 数据结构（Intent/Complexity/MessageRequest）
        ├── database.py       ← ORM 模型（PostgreSQL + pgvector，5张表）
        ├── db_manager.py     ← 数据库 CRUD + 向量检索封装
        ├── embedding.py      ← 共享 Embedding 服务（单例模型）
        ├── tools.py          ← 知识库检索（pgvector）+ 工程师加载
        │
        ├── preprocess.py     ← 预处理层（脱敏+意图+复杂度）
        ├── router.py         ← 混合路由（确定性流程+Agent分流）
        ├── ai_agent.py       ← AI 处理层（模型路由+工具调用，仅复杂问题）
        ├── agent_tools.py    ← AI 工具定义（time/knowledge/memory/assign/query）
        ├── postprocess.py    ← 后处理层（脱敏入库+总结+向量化记忆）
        ├── memory.py         ← 交互记忆管理（pgvector 向量检索）
        │
        ├── dingtalk_stream.py← 钉钉 Stream（纯转发层）
        ├── auth.py            ← API 鉴权（API Key + 角色控制）
        ├── log_config.py      ← 集中日志配置（控制台+文件轮转+请求ID关联）
        ├── llm_utils.py       ← LLM 调用统一入口（重试+熔断保护）
        ├── utils.py           ← 公共工具函数（extract_text 等）
        ├── scheduler.py      ← 定时提醒调度器（超时提醒/转派）
        ├── graph.py          ← 工程师分配 + 钉钉通知纯函数
        ├── feedback.py       ← 反馈处理（被 router 复用）
        └── main.py           ← FastAPI 入口（统一 API + 鉴权）
```

---

## 四、各文件职责说明

### 4.1 `config.py` -- 模型路由配置

| 配置项 | 说明 |
|--------|------|
| `LLM_API_KEY` / `LLM_BASE_URL` | LLM API 密钥和地址（LLM_API_KEY 为空时输出 warning） |
| `MODEL_ROUTING` | 按复杂度路由模型：simple->deepseek-chat，hard->deepseek-reasoner |
| `INTENT_LLM_FALLBACK` | 意图检测规则未命中时是否走 LLM 兜底 |
| `MEMORY_ENABLED` | 是否启用交互记忆 |
| `MEMORY_SEARCH_TOP_K` | 记忆检索返回条数 |
| `MAX_TOOL_ROUNDS` | AI 工具调用最大轮次（防死循环，默认 3） |
| `LLM_REQUEST_TIMEOUT` | LLM 请求软超时（默认 60s） |
| `LLM_REQUEST_TIMEOUT_HARD` | LLM 请求硬超时（默认 120s） |
| `LLM_RETRY_MAX_ATTEMPTS` | LLM 重试最大次数（默认 3） |
| `LLM_RETRY_MIN_WAIT` / `LLM_RETRY_MAX_WAIT` | 指数退避最小/最大等待秒数（1s/4s） |
| `LLM_CIRCUIT_FAILURE_THRESHOLD` | 熔断阈值（连续失败 5 次短路） |
| `LLM_CIRCUIT_RECOVERY_SECONDS` | 熔断恢复时间（默认 60s） |
| `LOG_LEVEL` / `LOG_FILE` / `LOG_MAX_SIZE` / `LOG_BACKUP_COUNT` | 日志级别/路径/轮转大小/保留份数 |
| `API_KEY` / `API_KEY_READONLY` / `API_KEY_ADMIN` | API 鉴权密钥（service/readonly/admin 三种角色） |

### 4.2 `models.py` -- 数据结构

| 类名 | 用途 |
|------|------|
| `Difficulty` | 任务难度枚举（SIMPLE/MEDIUM/HARD，v2.7.0 统一） |
| `Intent` | 消息意图枚举（6 种：报障/闲聊/反馈已解决/反馈未解决/转人工/查询） |
| `Complexity` | 复杂度枚举（3 档：SIMPLE/MEDIUM/HARD，驱动模型路由） |
| `MessageRequest` | 统一 API 请求体（source/sender_id/sender_name/content） |
| `MessageResponse` | 统一 API 响应体（intent/complexity/model_used/response/task_no/memory_saved） |
| `PreprocessResult` | 预处理层输出 |

> v3.4.0 已删除旧版 `AgentState`（LangGraph 工作流状态，不再使用）。

### 4.3 `database.py` -- ORM 模型（PostgreSQL + pgvector）

5 张表，统一存储关系型数据 + 向量数据（`difficulty` 列自 v2.7.0 起为 `String(20)`，值集合 `simple/medium/hard`）：

| 表名 | 说明 | 向量列 |
|------|------|--------|
| `engineers` | IT 工程师（name/staff_id/skills/mobile/dingtalk_user_id/available） | 无 |
| `tasks` | 运维任务（含 intent/complexity/model_used/raw_content） | 无 |
| `feedbacks` | 任务反馈记录 | 无 |
| `memories` | 交互记忆（summary + embedding） | `embedding Vector(768)` |
| `knowledge_docs` | 知识库分块（source/content + embedding） | `embedding Vector(768)` |

**关键函数：**
- `create_database_if_not_exists()` -- 连接 postgres 库，自动创建目标数据库
- `init_db()` -- 安装 pgvector 扩展 + 建表 + 创建索引
- `migrate_difficulty_values()` -- v2.7.0 启动时将旧值 `easy` 迁移为 `simple`
- `get_db()` -- FastAPI 依赖注入

**关键索引（v3.1.0/v3.3.0 新增）：**
- 复合索引：`(submitter, status)`、`(engineer, status)` -- 消除任务查询 N+1
- HNSW 向量索引：`knowledge_docs.embedding`、`memories.embedding` -- 加速向量检索

### 4.4 `db_manager.py` -- 数据库 CRUD + 向量检索

| 函数分类 | 函数 | 说明 |
|---------|------|------|
| 工程师 | `load_engineers_from_db()` / `get_engineer_by_staff_id()` / `get_engineer_by_mobile()` / `get_engineers_by_name()` / `update_engineer_binding()` | 工程师查询 + 身份绑定回填（按工号） |
| 任务 | `create_task()` / `get_user_active_task()` / `update_task_status()` | 含 intent/complexity/model_used |
| 任务 | `count_active_tasks_batch(names)` | v3.1.0 新增：一条 SQL 批量统计多名工程师负载，1+N 降为 2 次查询 |
| 反馈 | `create_feedback()` / `count_reminders()` | 提醒记录复用 feedbacks 表 |
| 记忆 | `create_memory(embedding)` / `search_memories_by_vector()` | pgvector cosine_distance |
| 知识库 | `add_knowledge_chunk()` / `search_knowledge_by_vector()` | pgvector cosine_distance |
| 知识库 | `get_knowledge_file_hashes()` / `delete_knowledge_by_source()` | 增量同步用 |

### 4.5 `embedding.py` -- 共享 Embedding 服务

| 函数 | 说明 |
|------|------|
| `get_embeddings()` | 获取 HuggingFaceEmbeddings 单例（text2vec-base-chinese，768 维） |
| `compute_embedding(text)` | 计算文本向量，返回 list[float] |

> 所有需要计算向量的模块（tools.py / memory.py）统一通过本模块获取，避免重复加载模型。

### 4.6 `preprocess.py` -- 预处理层

AI 调用前完成，全部用确定性规则 + 轻量 LLM 兜底：

| 步骤 | 函数 | 说明 |
|------|------|------|
| 脱敏 | `desensitize(text)` | 5 类正则：手机号/IP/邮箱/身份证/密码 -> 占位符 |
| 意图+复杂度检测 | `_llm_detect_intent_and_complexity(text)` | v3.0.0 起合并为单次 LLM 调用，同时输出意图和复杂度 |
| 规则命中 | `_complexity_rule_hit(text)` | v3.0.0 新增：关键词命中复杂度规则时免 LLM |
| 主入口 | `preprocess(raw_content)` | 返回 {desensitized, intent, complexity} |

> v3.0.0 调用次数优化：规则未命中时 4->3 次，简单报障 4->2 次。
> v3.4.0 `CASUAL_PATTERNS` / `HARD_KEYWORDS` 统一到本模块，`graph.py` 改为 import。
> v2.7.0 删除了独立的 `_llm_detect_complexity()`（已合并入 `_llm_detect_intent_and_complexity`）。

### 4.7 `router.py` -- 混合路由（核心）

| 函数 | 说明 |
|------|------|
| `route(preprocess_result, sender_name, sender_id)` | 主路由：按意图/复杂度分流 |
| `_handle_casual_chat()` | 闲聊 -> 快速 LLM 回复（max_tokens=200） |
| `_handle_feedback()` | 反馈 -> 复用 feedback.py |
| `_handle_query_status()` | 查询 -> 查数据库返回 |
| `_handle_simple_report()` | 简单报障 -> 知识库+单次 LLM（固定流程，一步到位） |
| `_handle_agent()` | 复杂报障 -> 调用 ai_agent.ai_process()（Agent+工具），透传 `assigned_engineer` |

> 反馈无 active 任务时自动转为报障，重新检测复杂度。
> v2.6.0：透传 `assigned_engineer` 字段给 postprocess，用于确定性判定 status=assigned。

### 4.8 `ai_agent.py` -- AI 处理层（仅复杂问题）

| 函数 | 说明 |
|------|------|
| `ai_process(desensitized, intent, complexity, sender_id)` | 主入口：模型路由+意图注入+工具调用，返回 `assigned_engineer` 字段 |
| `_get_llm(complexity)` | 按复杂度获取 LLM（simple->chat, hard->reasoner），v2.5.0 加 `timeout=` |
| `_get_tools(complexity)` | 按复杂度获取工具列表 |
| `_build_system_prompt(intent, complexity)` | 构建 system prompt（注入意图上下文，v3.4.0 加 Prompt 注入防护） |

> AI 自主决定是否调用工具，最多 MAX_TOOL_ROUNDS（3）轮，防止死循环。
> v2.6.0：`ai_process` 读取 `assign_engineer` 工具通过 `contextvars` 写入的分配结果，返回 `assigned_engineer` 字段，供 postprocess 确定性判定 status=assigned。
> v2.9.0：LLM 调用统一改用 `llm_utils.safe_llm_invoke()`（重试+熔断）。

### 4.9 `agent_tools.py` -- AI 工具定义

| 工具 | 说明 |
|------|------|
| `get_current_time` | 获取当前时间 |
| `search_knowledge(query)` | 检索知识库（pgvector 向量检索） |
| `search_memory(query)` | 检索历史交互记忆（pgvector 向量检索） |
| `assign_engineer(candidates, title, desc)` | 分配工程师（AI 通过 Skill 获取信息并传候选人，纯算法选人，0次LLM；v2.6.0 成功时用 `contextvars` 写入分配结果） |
| `query_user_tasks(sender_id)` | 查询用户任务状态 |

> MCP（Model Context Protocol）作为预留扩展点，未来接入监控/工单/AD 域等外部系统。

### 4.10 `postprocess.py` -- 后处理层

**仅对报障场景执行**（闲聊/反馈/查询跳过）：

| 步骤 | 函数 | 说明 |
|------|------|------|
| 二次脱敏 | -- | 对 AI 回答做脱敏（防止 AI 引用了敏感信息） |
| 入库 | -- | 存脱敏版本 + intent/complexity/model_used |
| 状态判定 | -- | v2.6.0 起根据透传的 `assigned_engineer` 字段确定性判定 status=assigned |
| LLM 总结+向量化 | `summarize_and_vectorize_async()` | v3.0.0 拆出：生成摘要（≤50字）+ 计算embedding存入 memories 表 |

> v2.6.0：删除靠字符串匹配"已分配"判定状态的逻辑和 `_extract_engineer()` 函数。
> v3.0.0：摘要任务丢 `asyncio.ensure_future(run_in_threadpool(...))` 后台执行，不阻塞响应。
> v3.0.0：删除独立的 `_llm_detect_complexity()`（已并入 preprocess）。

### 4.11 `memory.py` -- 交互记忆管理

| 函数 | 说明 |
|------|------|
| `save_memory(summary, task_id, metadata)` | 计算向量 + 存入 memories 表 |
| `search_memory(query, top_k)` | pgvector cosine_distance 检索历史记忆 |
| `get_memory_count()` | 记忆总数 |

### 4.12 `tools.py` -- 知识库检索 + 工程师加载

| 函数 | 说明 |
|------|------|
| `sync_knowledge(force)` | 增量同步知识库（比较文件 MD5 哈希，变更的分块+向量化+存入 knowledge_docs） |
| `retrieve_knowledge(query, top_k)` | pgvector 向量检索知识库（自动触发增量同步，5秒冷却） |
| `load_engineers()` | 从 DB 加载工程师（降级读 JSON） |
| `count_active_tasks(name)` | 动态计算工程师负载（v3.1.0 起内部改用 `db_manager.count_active_tasks_batch()` 批量查询） |

### 4.13 `dingtalk_stream.py` -- 钉钉 Stream（纯转发层）

| 内容 | 说明 |
|------|------|
| `OpsAgentChatbot.process()` | v2.5.0 恢复 `async def`；`requests.post` 包 `run_in_threadpool`，不阻塞事件循环 |
| `_auto_bind_engineer()` | 委托 `engineer_matcher` 按工号绑定工程师身份（工号直连/首次登记） |
| `start_stream_bot()` | 启动 WebSocket 长连接 |

> 零业务代码，所有逻辑集中在 API 层。工程师身份匹配委托给独立的 `engineer_matcher.py`（见 4.14）。

### 4.14 `engineer_matcher.py` -- 工程师身份匹配层（按工号绑定）

| 函数 | 说明 |
|------|------|
| `match_and_bind(sender_nick, sender_staff_id, sender_user_id)` | 按工号绑定工程师身份，返回 `MatchResult` |
| `_locate_unbound_engineer()` | 首次绑定：在未登记工号的工程师中用姓名/手机号唯一定位 |
| `_bind_user_id()` / `_bind_staff_and_user()` | 工号直连绑定 / 首次登记工号+绑定 |

> 独立模块，不依赖钉钉 SDK（只接收字符串参数）；`dingtalk_user_id` 仅在工号确立后写入。
> 工号取自钉钉消息回调的 `sender_staff_id`，无需对接通讯录 API；名单无需预填工号，首次发消息自动回填。

### 4.15 `scheduler.py` -- 定时提醒调度器

| 函数 | 说明 |
|------|------|
| `check_overdue_tasks()` | 每分钟扫描 assigned 状态任务 |
| `_send_reminder()` | 钉钉私聊提醒工程师 |
| `_try_reassign()` | 达上限后转派（排除当前工程师） |
| `start_scheduler()` | 启动 APScheduler |

> 提醒记录复用 feedbacks 表（feedback_by = "系统提醒|工程师名"），不改表结构。

### 4.15 `graph.py` -- 工程师分配 + 钉钉通知纯函数

v3.4.0 删除旧版 LangGraph 工作流（`classify_node` / `route_after_classify` / `retrieve_node` / `answer_node` / `assign_node` / `build_graph` / `agent_app`，约 400 行），只保留纯函数：

| 函数 | 被谁调用 |
|------|---------|
| `assign_engineer(task, exclude_name)` | agent_tools.py / feedback.py / scheduler.py |
| `_notify_engineer(name, task)` | feedback.py / scheduler.py |
| `_send_dingtalk_direct_message(user_id, title, text)` | feedback.py / scheduler.py |

> v2.7.0：`CLASSIFY_PROMPT` 和 `classify_node` 中的 `easy` 统一改为 `simple`。
> v3.4.0：`CASUAL_PATTERNS` / `HARD_KEYWORDS` 迁出到 `preprocess.py`，本模块改为 import。

### 4.16 `feedback.py` -- 反馈处理（被 router 复用）

| 函数 | 说明 |
|------|------|
| `handle_message(sender_nick, sender_id, text)` | 主入口：识别身份 + 检测反馈 |
| `identify_sender(nick, id)` | v3.1.0 起优先用 `staff_id` 查找工程师，姓名降级兜底 |
| `_handle_engineer_resolved()` | 工程师回复"已解决" -> 标记 resolved + 通知用户 |
| `_handle_escalation()` | simple 未解决 -> 调用 assign_engineer 升级分配 |
| `_handle_re_escalation()` | assigned 未解决 -> 重新催办 |

### 4.17 `main.py` -- FastAPI 入口

| 路由 | 方法 | 说明 |
|------|------|------|
| `/api/v1/message` | POST | ★ 统一入口：预处理 -> 路由 -> 后处理（需 X-API-Key） |
| `/health` | GET | 健康检查（无需鉴权） |
| `/tasks` | GET | 查询任务列表（需 X-API-Key） |
| `/engineers` | GET | 查询工程师名单（需 X-API-Key） |
| `/memories` | GET | 查询交互记忆（需 X-API-Key） |

> v2.5.0：所有接口需 `X-API-Key`（`/health` 除外），角色控制由 `auth.py` 完成。
> v2.5.0：async 端点内同步调用包 `run_in_threadpool`，事件循环不再被 LLM 阻塞。
> v3.0.0：摘要任务丢 `asyncio.ensure_future(run_in_threadpool(...))` 后台执行，不阻塞响应。
> v3.4.0：删除旧版 `/task` 接口及 `TaskRequest`/`TaskResponse`（不再保留旧版兼容）。

**启动流程：** `create_database_if_not_exists()` -> `init_db()` -> `migrate_difficulty_values()` -> `migrate_engineers_json_to_db()` -> `start_scheduler()` -> FastAPI + 钉钉 Stream

### 4.18 `auth.py` -- API 鉴权（v2.5.0 新增）

| 内容 | 说明 |
|------|------|
| API Key 验证 | 所有接口需 `X-API-Key` 请求头（`/health` 除外） |
| 角色控制 | `service` / `readonly` / `admin` 三种角色，不同角色可访问不同接口 |
| FastAPI 依赖 | 作为 Depends 注入到路由，统一鉴权 |

> Key 通过 `.env` 配置（`API_KEY` / `API_KEY_READONLY` / `API_KEY_ADMIN`）。

### 4.19 `log_config.py` -- 集中日志配置（v2.8.0 新增）

| 内容 | 说明 |
|------|------|
| 日志格式 | `时间 \| 级别 \| 模块 \| req=请求ID \| 消息` |
| 双输出 | 控制台 + 文件轮转（按大小切分，保留 N 份） |
| 请求级关联 | `contextvars` + `RequestIdFilter`，同一请求日志可串联 |
| 配置项 | `LOG_LEVEL` / `LOG_FILE` / `LOG_MAX_SIZE` / `LOG_BACKUP_COUNT` |

> v2.8.0 全项目 15 个模块约 112 处 `print` 替换为 `logging` 调用。

### 4.20 `llm_utils.py` -- LLM 调用统一入口（v2.9.0 新增）

| 函数 | 说明 |
|------|------|
| `safe_llm_invoke(llm, input)` | LLM 调用统一入口：重试 + 熔断保护 |

- **指数退避重试**：3 次（1s/2s/4s），仅对瞬时故障重试
- **熔断器**：连续 5 次失败短路 60s，半开探测恢复
- 依赖 `tenacity>=8.0`（已加入 `requirements.txt`）
- 全项目 9 处 `llm.invoke` 改为 `safe_llm_invoke`

### 4.21 `utils.py` -- 公共工具函数（v3.4.0 新增）

| 函数 | 说明 |
|------|------|
| `extract_text()` | 抽取文本公共函数，消除 5 处重复代码 |

> v3.4.0 代码质量与技术债清理的产物，后续新增公共函数统一放这里。

---

## 五、配置文件说明

### 5.1 `.env` 环境变量

| 变量名 | 说明 | 示例值 |
|--------|------|--------|
| `open_code_go_api` | LLM API Key | `sk-xxxxxxxx` |
| `model` | LLM 模型名 | `deepseek-chat` |
| `base_url` | LLM API 地址 | `https://api.deepseek.com` |
| `PG_HOST` | PostgreSQL 主机 | `localhost` |
| `PG_PORT` | PostgreSQL 端口 | `5432` |
| `PG_USER` | PostgreSQL 用户名 | `postgres` |
| `PG_PASSWORD` | PostgreSQL 密码 | `你的密码` |
| `PG_DATABASE` | PostgreSQL 数据库名 | `ops_agent` |
| `MODEL_SIMPLE` | 简单问题模型 | `deepseek-chat` |
| `MODEL_MEDIUM` | 中等问题模型 | `deepseek-chat` |
| `MODEL_HARD` | 复杂问题模型 | `deepseek-reasoner` |
| `INTENT_LLM_FALLBACK` | 意图检测 LLM 兜底 | `true` |
| `MEMORY_ENABLED` | 启用记忆 | `true` |
| `MEMORY_SEARCH_TOP_K` | 记忆检索条数 | `3` |
| `DINGTALK_CLIENT_ID` | 钉钉 AppKey | `dingkc...` |
| `DINGTALK_CLIENT_SECRET` | 钉钉 AppSecret | `abc123...` |
| `REMINDER_INTERVAL_MINUTES` | 提醒间隔 | `30` |
| `REMINDER_MAX_COUNT` | 最大提醒次数 | `3` |
| `LLM_REQUEST_TIMEOUT` | v2.5.0 LLM 请求软超时（秒） | `60` |
| `LLM_REQUEST_TIMEOUT_HARD` | v2.5.0 LLM 请求硬超时（秒） | `120` |
| `LLM_RETRY_MAX_ATTEMPTS` | v2.9.0 LLM 重试最大次数 | `3` |
| `LLM_RETRY_MIN_WAIT` | v2.9.0 指数退避最小等待（秒） | `1` |
| `LLM_RETRY_MAX_WAIT` | v2.9.0 指数退避最大等待（秒） | `4` |
| `LLM_CIRCUIT_FAILURE_THRESHOLD` | v2.9.0 熔断阈值（连续失败次数） | `5` |
| `LLM_CIRCUIT_RECOVERY_SECONDS` | v2.9.0 熔断恢复时间（秒） | `60` |
| `LOG_LEVEL` | v2.8.0 日志级别 | `INFO` |
| `LOG_FILE` | v2.8.0 日志文件路径 | `logs/ops_agent.log` |
| `LOG_MAX_SIZE` | v2.8.0 单个日志文件最大字节数 | `10485760` |
| `LOG_BACKUP_COUNT` | v2.8.0 日志保留份数 | `7` |
| `API_KEY` | v2.5.0 service 角色密钥 | `sk-service-xxxx` |
| `API_KEY_READONLY` | v2.5.0 readonly 角色密钥 | `sk-readonly-xxxx` |
| `API_KEY_ADMIN` | v2.5.0 admin 角色密钥 | `sk-admin-xxxx` |

### 5.2 工程师名单

首次启动从 `engineers.json` 自动迁移到 PostgreSQL `engineers` 表。`current_load` 不存字段，动态查询（v3.1.0 起改用 `count_active_tasks_batch()` 批量统计）。

### 5.3 知识库文档

放在 `data/knowledge/` 下，`.md` 格式。支持增量热更新：增删改文件后自动同步到 `knowledge_docs` 表（pgvector 向量化）。

### 5.4 依赖锁定（v3.2.0）

| 文件 | 说明 |
|------|------|
| `requirements.txt` | 全部 `>=` 改为 `==` 锁定确切版本 |
| `requirements.lock` | 完整依赖快照，含传递依赖，用于复现环境 |

---

## 六、启动和测试

### 6.1 安装依赖

```bash
cd 运维任务分配agent
pip install -r requirements.txt
```

### 6.2 配置环境变量

编辑 `data/.env`：

```env
open_code_go_api=sk-xxxxxxxx
model=deepseek-chat
base_url=https://api.deepseek.com

PG_HOST=localhost
PG_PORT=5432
PG_USER=postgres
PG_PASSWORD=你的密码
PG_DATABASE=ops_agent

# v2.5.0 API 鉴权
API_KEY=sk-service-xxxx
API_KEY_READONLY=sk-readonly-xxxx
API_KEY_ADMIN=sk-admin-xxxx

# v2.8.0 日志
LOG_LEVEL=INFO
LOG_FILE=logs/ops_agent.log
LOG_MAX_SIZE=10485760
LOG_BACKUP_COUNT=7

# v2.9.0 LLM 重试/熔断
LLM_RETRY_MAX_ATTEMPTS=3
LLM_CIRCUIT_FAILURE_THRESHOLD=5
```

### 6.3 启动服务

```bash
cd data
python -m src.main
```

启动时自动：建库 -> 安装 pgvector 扩展 -> 建表 -> 创建索引 -> 迁移 difficulty 值 -> 迁移工程师数据 -> 启动定时提醒 -> FastAPI + 钉钉 Stream

### 6.4 测试

> v2.5.0 起，除 `/health` 外所有接口都需在请求头携带 `X-API-Key`。

```bash
# 统一入口（需鉴权）
curl -X POST http://localhost:8000/api/v1/message \
  -H "Content-Type: application/json" \
  -H "X-API-Key: sk-service-xxxx" \
  -d '{"source":"api","sender_name":"小明","content":"打印机连不上"}'

# 健康检查（无需鉴权）
curl http://localhost:8000/health

# 管理接口（需鉴权）
curl -H "X-API-Key: sk-admin-xxxx" http://localhost:8000/tasks
curl -H "X-API-Key: sk-admin-xxxx" http://localhost:8000/engineers
curl -H "X-API-Key: sk-admin-xxxx" http://localhost:8000/memories
```

---

## 七、完整请求流程

```
用户: "打印机连不上，IP是192.168.1.100"
  │
  ▼
① 钉钉 Stream 转发 -> POST /api/v1/message
  │
  ▼
② 预处理层
  ├─ 脱敏: "打印机连不上，IP是[IP]"
  ├─ 意图检测: report_issue
  └─ 复杂度检测: simple
  │
  ▼
③ 混合路由 -> simple + report_issue -> 固定流程
  ├─ 检索知识库（pgvector）: printer.md
  ├─ 检索记忆（pgvector）: 无相关
  └─ 单次 LLM 生成回答
  │
  ▼
④ 后处理层
  ├─ 脱敏回答
  ├─ 入库: task T1042, status=auto_answered
  ├─ LLM 总结: "打印机离线 -> 检查电源/重启服务"
  └─ 向量化: embedding 存入 memories 表
  │
  ▼
⑤ 响应返回 -> 钉钉回复用户
```

---

## 八、扩展方向

| 扩展 | 怎么做 | 难度 |
|------|--------|------|
| 增加知识 | 往 `data/knowledge/` 添加 `.md` 文件 | ⭐ |
| 增加 AI 工具 | 在 `agent_tools.py` 加 `@tool` 函数 | ⭐⭐ |
| 接入 MCP | 实现 MCP 客户端，标准化外部系统接入 | ⭐⭐⭐ |
| 新意图 | 在 `preprocess.py` 加关键词 + 路由分支 | ⭐⭐ |
| 新消息渠道 | 仿 `dingtalk_stream.py` 写转发层 | ⭐⭐ |
| 新定时任务 | 仿 `scheduler.py` 加 APScheduler job | ⭐⭐ |

---

## 九、常见问题排查

| 问题 | 可能原因 | 解决方法 |
|------|---------|---------|
| 启动报 ModuleNotFoundError | 依赖未安装 | `pip install -r requirements.txt` |
| 数据库连接失败 | PostgreSQL 未启动或密码错误 | 检查 PostgreSQL 服务 + `.env` 中 PG_* 配置 |
| 数据库表未创建 | pgvector 扩展未安装 | 查看启动日志，确认 `init_db()` 成功 |
| 脱敏未生效 | 正则未覆盖 | 检查 `preprocess.py` 中 DESENSITIZE_PATTERNS |
| 意图检测误判 | 关键词未覆盖 | 检查 `preprocess.py` 关键词列表，或开启 LLM 兜底 |
| 简单问题走了 Agent | 复杂度检测不准 | 检查 `preprocess.py` 复杂度规则 |
| 知识库检索不到 | 知识库未同步 | 重启触发同步，或检查 `knowledge_docs` 表 |
| 钉钉转发超时 | LLM 响应慢 | 调整 `dingtalk_stream.py` 中 timeout |
| 定时提醒未触发 | 调度器未启动 | 查看启动日志是否有 `[scheduler] ✅ 定时提醒已启动` |

---

## 十、技术决策备忘

| 决策 | 选择 | 原因 |
|------|------|------|
| 入口 | 统一 API | 钉钉纯转发，所有逻辑集中 API 层 |
| 架构 | 混合路由 | 确定性流程+Agent，能确定的用规则，不确定的才交给AI |
| AI 框架 | LangChain tool calling | 单 agent + 工具，不做多节点编排 |
| 模型路由 | 按复杂度选模型 | 简单用便宜模型，复杂用推理模型，成本可控 |
| 数据库 | PostgreSQL + pgvector | 关系型+向量统一存储，一套数据库 |
| 预处理 | 规则 + 轻量 LLM | 不消耗主 LLM token，成本低 |
| 反馈识别 | 关键词匹配 | 简单可靠，不消耗 LLM 调用 |
| 记忆 | pgvector 向量检索 | 每次交互向量化存储，相似问题命中历史经验 |
| 负载均衡 | LLM 筛技能 + 算法选人 | LLM 负责"谁会做"，算法负责"谁来做" |
| 工程师负载 | 动态查询不存字段 | 避免 current_load 数据不一致 |
| 定时提醒 | APScheduler 进程内 | 轻量，与 FastAPI 同进程 |

---

## 十一、版本控制

### 版本规范

| 版本类型 | 格式 | 说明 |
|---------|------|------|
| 大改版 | `vX.0.0` | 架构级变更 |
| 功能版本 | `v0.X.0` | 新增功能 |
| 修复版本 | `v0.0.X` | Bug 修复 |

### 版本历史

> **v2.5.0 ~ v3.4.0：企业生产化优化阶段。** 在 v2.3.0 Skill+Tool 架构基础上，围绕“可上生产”这一目标推进了三批改造：
> - **稳定性**：API 鉴权、async 阻塞修复、LLM 超时/重试/熔断、结构化日志与请求级关联；
> - **性能与成本**：LLM 调用次数削减、摘要异步化、DB N+1 消除、HNSW 向量索引；
> - **可维护性**：难度枚举统一、任务状态判定改为结构化标记、依赖版本锁定、重复代码抽取、旧版 LangGraph 代码清理。

#### 🏷️ v3.4.0 -- 代码质量与技术债清理（2026-07-17）

> 消除重复、清理旧版代码，为后续迭代算账

- 新增 `utils.py`：抽取 `extract_text()` 公共函数，消除 5 处重复
- `CASUAL_PATTERNS` / `HARD_KEYWORDS` 统一到 `preprocess.py`，`graph.py` 改为 import
- `graph.py`：删除旧版 LangGraph 工作流（classify/retrieve/answer/assign/build_graph/agent_app，约400行），保留 assign_engineer 等纯函数
- `main.py`：删除旧版 `/task` 接口 + `TaskRequest`/`TaskResponse`
- `models.py`：删除 `AgentState`
- `config.py`：`LLM_API_KEY` 为空时输出 warning
- `ai_agent.py`：system prompt 加 Prompt 注入防护

#### 🏷️ v3.3.0 -- 向量检索 HNSW 索引（2026-07-16）

> 加速语义检索，为生产负载做准备

- `database.py`：`init_db()` 新增 2 个 HNSW 向量索引（`knowledge_docs.embedding`、`memories.embedding`）

#### 🏷️ v3.2.0 -- 依赖版本锁定（2026-07-15）

> 锁定确切版本，保证环境可复现

- `requirements.txt`：全部 `>=` 改为 `==` 锁定确切版本
- 新增 `requirements.lock`：完整快照，含传递依赖

#### 🏷️ v3.1.0 -- DB N+1 查询消除 + 反馈身份识别改用 staff_id（2026-07-14）

> 消除负载均衡的 N+1 查询，反馈身份识别更稳定

- `db_manager.py`：新增 `count_active_tasks_batch()`，一条 SQL 批量统计
- `tools.py` / `main.py` / `graph.py`：逐人查负载改为批量统计，1+N 降为 2 次
- `database.py`：新增 2 个复合索引（submitter+status, engineer+status）
- `feedback.py`：`identify_sender()` 优先用 staff_id 查找，姓名降级

#### 🏷️ v3.0.0 -- LLM 调用次数削减 + 摘要异步化（2026-07-13）

> 进一步降低 LLM 成本与响应延迟

- `preprocess.py`：合并意图+复杂度检测为单次 LLM 调用（`_llm_detect_intent_and_complexity`），新增 `_complexity_rule_hit`
- 删除独立的 `_llm_detect_complexity()`
- 调用次数：规则未命中时 4->3，简单报障 4->2
- `postprocess.py`：拆出 `summarize_and_vectorize_async()`
- `main.py`：摘要任务丢 `asyncio.ensure_future(run_in_threadpool(...))` 后台执行，不阻塞响应

#### 🏷️ v2.9.0 -- LLM 重试 + 熔断保护（2026-07-12）

> 应对 LLM 服务波动，避免雪崩

- 新增 `llm_utils.py`：`safe_llm_invoke()` 统一入口
  - 指数退避重试（3次，1s/2s/4s），仅对瞬时故障重试
  - 熔断器（连续5次失败短路60s，半开探测恢复）
- 9 处 `llm.invoke` 改为 `safe_llm_invoke`
- `requirements.txt` 新增 `tenacity>=8.0`
- `.env` 新增：LLM_RETRY_MAX_ATTEMPTS / LLM_RETRY_MIN_WAIT / LLM_RETRY_MAX_WAIT / LLM_CIRCUIT_FAILURE_THRESHOLD / LLM_CIRCUIT_RECOVERY_SECONDS

#### 🏷️ v2.8.0 -- 结构化日志系统（2026-07-11）

> 全项目 print 替换为 logging，请求级关联可追踪

- 新增 `log_config.py`：集中日志配置
  - 格式：`时间 | 级别 | 模块 | req=请求ID | 消息`
  - 双输出：控制台 + 文件轮转
  - request_id 请求级关联（contextvars + RequestIdFilter）
- 全项目 15 个模块约 112 处 print 替换为 logging 调用
- `.env` 新增：LOG_LEVEL / LOG_FILE / LOG_MAX_SIZE / LOG_BACKUP_COUNT

#### 🏷️ v2.7.0 -- 难度枚举统一（2026-07-10）

> 统一 difficulty 与 complexity 的取值集合，避免映射混乱

- `database.py`：`difficulty` 列从 `Enum("easy","hard")` 改为 `String(20)`
- 新增 `migrate_difficulty_values()`：启动时 easy -> simple
- `models.py`：`Difficulty` 枚举 EASY/HARD 改为 SIMPLE/MEDIUM/HARD
- `postprocess.py`：删除 complexity->difficulty 映射，直接用 complexity 值
- `graph.py`：CLASSIFY_PROMPT 和 classify_node 的 easy 改为 simple

#### 🏷️ v2.6.0 -- 任务状态判定改为结构化标记（2026-07-10）

> 去掉靠字符串匹配判定状态的不稳定逻辑

- `agent_tools.py`：`assign_engineer` 工具成功时用 `contextvars` 写入分配结果
- `ai_agent.py`：`ai_process` 读取标记，返回 `assigned_engineer` 字段
- `router.py` / `main.py` / `postprocess.py`：透传 `assigned_engineer`，postprocess 根据它确定性判定 status=assigned
- 删除 postprocess 中靠字符串匹配"已分配"判定状态的逻辑和 `_extract_engineer()` 函数

#### 🏷️ v2.5.0 -- API 鉴权 + async 阻塞修复 + LLM 超时（2026-07-10）

> 面向生产的基础加固：鉴权、不阻塞事件循环、LLM 超时

- 新增 `auth.py`：API Key + 角色控制（service/readonly/admin），所有接口需 X-API-Key（/health 除外）
- `main.py`：async 端点内同步调用包 `run_in_threadpool`，事件循环不再被 LLM 阻塞
- `dingtalk_stream.py`：`requests.post` 包 `run_in_threadpool`；`process` 恢复 `async def`
- `config.py`：新增 `LLM_REQUEST_TIMEOUT`(60s) / `LLM_REQUEST_TIMEOUT_HARD`(120s)
- 全部 7 处 `ChatOpenAI` 加 `timeout=` 参数

#### 🏷️ v2.3.0 -- 负载均衡改为 Skill + Tool 模式（2026-07-10）

> 去掉工具内 LLM 调用，AI 通过 Skill 获取工程师信息，工具只做纯算法选人

- 新增 `engineers-info.md`：工程师团队信息 Skill 文档
- `graph.py` 新增 `assign_engineer_by_algorithm()`：纯算法选人
- `agent_tools.py` 改造 `assign_engineer` 工具：参数改为 `candidates`
- LLM 调用从 2-3 次降为 1 次

#### 🏷️ v2.2.0 -- 数据库统一为 PostgreSQL + pgvector（2026-07-10）

> 废弃 ChromaDB，两套数据库合并为一套 PostgreSQL

- 新增 `embedding.py`：共享 Embedding 服务
- 新增 `KnowledgeDoc` 表：知识库分块 + 向量
- `Memory` 表新增 embedding 列
- 知识库/记忆检索改用 pgvector cosine_distance
- 依赖变更：pymysql->psycopg2-binary，移除 chromadb

#### 🏷️ v2.1.0 -- 混合路由架构（2026-07-10）

> 确定性流程 + Agent，简单问题一步到位

- 新增 `router.py`：预处理后按意图/复杂度分流
- 闲聊/反馈/查询 -> 确定性流程（0-1次LLM）
- 简单报障 -> 固定流程（知识库+单次LLM）
- 复杂报障 -> Agent+工具调用

#### 🏷️ v2.0.0 -- 新版架构重构（2026-07-10）

> 统一入口 + 预处理 + 模型路由 + 工具化AI + 记忆

- 新增 `config.py` / `preprocess.py` / `ai_agent.py` / `agent_tools.py` / `postprocess.py` / `memory.py`
- 统一入口 `POST /api/v1/message`
- 钉钉 Stream 退化为纯转发层
- 预处理层：脱敏 + 意图检测 + 复杂度检测
- 模型路由：按复杂度选模型
- 单 Agent + 5 工具
- 后处理：脱敏入库 + LLM 总结 + 向量化记忆

#### 🏷️ v1.1.0 -- 定时重新提醒（2026-07-10）

- 新增 `scheduler.py`：APScheduler 后台调度
- 30分钟未解决 -> 提醒，3次未响应 -> 自动转派

#### 🏷️ v1.0.0 -- 第一次大改版（2026-07-09）

> 任务持久化 + 反馈闭环 + 负载均衡

- MySQL + SQLAlchemy ORM
- 任务状态机：auto_answered -> assigned -> resolved
- 反馈闭环：关键词识别 + 升级/催办/关闭
- 负载均衡：LLM 筛技能 + 算法选最低负载

#### 🏷️ v0.2.0 -- 钉钉 Stream 接入（2026-06-15）

- 钉钉 Stream 单聊机器人
- 工程师 ID 自动绑定
- 钉钉私聊通知 + 群简报

#### 🏷️ v0.1.0 -- 初始版本（2026-06）

- LangGraph 任务分类工作流
- FastAPI REST API
- ChromaDB 向量知识库

---

> 📅 创建日期：2025-01
> 📅 最近更新：2026-07-17（v3.4.0 代码质量与技术债清理）
> 👤 适用对象：IT 运维团队，1-2 人维护
> 🎯 当前状态：混合路由 + 统一数据库 + Skill+Tool 扩展 + 持久化记忆 + API 鉴权 + 结构化日志 + LLM 重试熔断 + HNSW 向量索引（企业生产化完成）
