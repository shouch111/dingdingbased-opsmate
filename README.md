# 🛠️ Ops Agent -- 运维任务智能处理系统

一个基于 **FastAPI + LangChain + ChromaDB + MySQL** 的智能运维任务处理系统。

> **核心能力：** 统一 API 入口，消息预处理（脱敏+意图+复杂度），混合路由（确定性流程+Agent），模型路由，交互记忆持久化，钉钉 Stream 接入。

---

## 架构概览

```mermaid
flowchart TD
    DT[📱 钉钉消息] --> STREAM[钉钉 Stream\n纯转发]
    API_USER[👤 API/用户消息] --> MSG
    STREAM --> MSG["POST /api/v1/message\n统一入口"]

    MSG --> PRE

    subgraph PREPROCESS[预处理层 preprocess.py]
        PRE["① 预处理"]
        PRE --> MASK["脱敏\n手机/IP/邮箱/身份证/密码"]
        MASK --> INTENT["意图检测\n报障/闲聊/反馈/转人工/查询"]
        INTENT --> CPLX["复杂度检测\nsimple/medium/hard"]
    end

    CPLX --> ROUTER

    subgraph ROUTERLAYER[混合路由 router.py]
        ROUTER{"② 路由分流"}
        ROUTER -->|闲聊| CASUAL["确定性流程\n快速LLM回复"]
        ROUTER -->|反馈| FEEDBACK["确定性流程\n复用feedback.py"]
        ROUTER -->|查询| QUERY["确定性流程\n查数据库"]
        ROUTER -->|简单报障| SIMPLE["固定流程\n知识库+单次LLM"]
        ROUTER -->|复杂报障| AGENT["Agent流程\nAI+工具调用"]
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

    SIMPLE -.-> KB[("🗄️ ChromaDB\n知识库")]
    AGENT -.-> KB
    AGENT -.-> MEM[("🧠 ChromaDB\n记忆库")]
    SIMPLE -.-> MEM
    AGENT -.-> ENG[("👥 engineers\n负载均衡")]
    STORE -.-> DB[("🗄️ MySQL\n任务/记忆")]
```

### 混合架构说明

核心原则：**能确定的用规则，不确定的才交给 AI。**

| 路由分支 | 处理方式 | LLM 调用 | 存库 | 适用场景 |
|---------|---------|---------|------|---------|
| **闲聊** | 确定性流程：快速 LLM 回复 | 1次（max_tokens=200） | 否 | 问候/致谢/测试 |
| **反馈** | 确定性流程：复用 feedback.py | 0次 | 否 | 已解决/未解决 |
| **查询状态** | 确定性流程：查数据库 | 0次 | 否 | 我的任务/进度 |
| **简单报障** | 固定流程：知识库+单次 LLM | 1次 | 是 | 打印机/VPN/邮箱 |
| **复杂报障** | Agent 流程：AI+工具调用 | 2-4次 | 是 | 服务器/数据库/网络 |
| **转人工** | Agent 流程：分配工程师 | 2-4次 | 是 | 明确要求人工 |

| 层 | 模块 | 职责 |
|----|------|------|
| **预处理层** | `preprocess.py` | 脱敏（5类正则）+ 意图检测（6种意图）+ 复杂度检测（3档），规则优先 + 轻量 LLM 兜底 |
| **混合路由** | `router.py` | 按意图/复杂度分流：确定性流程（闲聊/反馈/查询/简单报障）+ Agent 流程（复杂报障）|
| **Agent 层** | `ai_agent.py` + `agent_tools.py` | 按复杂度路由模型，意图注入上下文，单 Agent 自主调用工具（仅复杂问题） |
| **后处理层** | `postprocess.py` + `memory.py` | 二次脱敏 + 入库 + LLM 总结 + 向量化持久化记忆（仅报障场景） |

---

## 项目结构

```
ops-agent/
├── README.md                 ← 本文档
├── requirements.txt          ← Python 依赖
├── .env.example              ← 环境变量模板
├── CHANGELOG.md              ← 更新日志
├── 新版架构方案.md            ← v2.0 架构设计文档
├── 运维Agent框架文档.md       ← 框架设计文档（含版本控制）
├── 第一阶段需求文档.md        ← 第一阶段需求设计文档
│
└── data/                     ← 数据与代码
    ├── .env                  ← 实际环境变量（不提交！）
    ├── engineers.json        ← 工程师名单（首次启动自动迁移到 DB）
    ├── knowledge/            ← 知识库文档（skill 文档）
    ├── chroma_db/            ← 知识库向量存储（自动生成）
    ├── memory_db/            ← ★ 交互记忆向量存储（自动生成）
    │
    └── src/                  ← 源代码
        ├── __init__.py
        │
        ├── config.py         ← ★ 模型路由配置
        ├── models.py         ← 数据结构定义（Intent/Complexity/MessageRequest）
        ├── database.py       ← ORM 模型（Engineer/Task/Feedback/Memory）
        ├── db_manager.py     ← 数据库 CRUD 封装
        ├── tools.py          ← 知识库检索工具
        │
        ├── preprocess.py     ← ★ 预处理层（脱敏+意图+复杂度）
        ├── router.py         ← ★ 混合路由（确定性流程+Agent分流）
        ├── ai_agent.py       ← ★ AI 处理层（模型路由+工具调用，仅复杂问题）
        ├── agent_tools.py    ← ★ AI 工具定义（time/knowledge/memory/assign）
        ├── postprocess.py    ← ★ 后处理层（脱敏入库+总结+向量化）
        ├── memory.py         ← ★ 交互记忆管理（向量存储+检索）
        │
        ├── dingtalk_stream.py← 钉钉 Stream（纯转发层）
        ├── scheduler.py      ← 定时提醒调度器（超时提醒/转派）
        ├── graph.py          ← 旧版 LangGraph 工作流（兼容保留）
        ├── feedback.py       ← 反馈处理（被 router 复用）
        └── main.py           ← FastAPI 入口（统一 API + 旧版兼容）
```

> ★ 标记为 v2.0 新架构新增模块

---

## 快速开始

### 1. 环境要求

- Python 3.10+
- MySQL 8.0+（首次启动自动建库建表）
- 一个 LLM API Key（[DeepSeek](https://platform.deepseek.com) 推荐）
- Windows / macOS / Linux

### 2. 安装依赖

```bash
git clone <your-repo-url>
cd ops-agent
pip install -r requirements.txt
```

### 3. 配置环境变量

```bash
cp .env.example data/.env
# 编辑 data/.env
```

```env
# ========== LLM API（必填）==========
open_code_go_api=sk-你的API密钥
model=deepseek-chat
base_url=https://api.deepseek.com

# ========== MySQL（必填，首次启动自动建库建表）==========
MYSQL_HOST=localhost
MYSQL_PORT=3306
MYSQL_USER=root
MYSQL_PASSWORD=你的密码
MYSQL_DATABASE=ops_agent

# ========== 模型路由（可选，有默认值）==========
MODEL_SIMPLE=deepseek-chat
MODEL_MEDIUM=deepseek-chat
MODEL_HARD=deepseek-reasoner

# ========== 预处理（可选）==========
INTENT_LLM_FALLBACK=true

# ========== 记忆系统（可选）==========
MEMORY_ENABLED=true
MEMORY_DB_PATH=data/memory_db
MEMORY_SEARCH_TOP_K=3

# ========== 钉钉 Stream（可选）==========
DINGTALK_CLIENT_ID=你的AppKey
DINGTALK_CLIENT_SECRET=你的AppSecret

# ========== 定时提醒（可选）==========
REMINDER_INTERVAL_MINUTES=30
REMINDER_MAX_COUNT=3
```

### 4. 准备知识库

在 `data/knowledge/` 下创建 `.md` 文件：

```markdown
# 问题标题

## 症状
- 症状描述

## 解决步骤
1. 第一步
2. 第二步
```

### 5. 配置工程师名单

编辑 `data/engineers.json`（首次启动自动迁移到数据库）：

```json
[
  {
    "name": "张三",
    "skills": ["打印机", "电脑硬件", "Windows系统"],
    "mobile": "13800000001",
    "dingtalk_user_id": "",
    "available": true
  }
]
```

### 6. 启动

```bash
cd data
python -m src.main
```

启动时自动：建库 -> 建表 -> 迁移工程师数据 -> 启动定时提醒 -> 启动 FastAPI + 钉钉 Stream

### 7. 测试

```bash
# ★ 统一入口（推荐）
curl -X POST http://localhost:8000/api/v1/message \
  -H "Content-Type: application/json" \
  -d '{"source":"api","sender_name":"小明","content":"打印机连不上"}'

# 健康检查
curl http://localhost:8000/health

# 查询任务列表
curl http://localhost:8000/tasks

# 查询工程师名单
curl http://localhost:8000/engineers

# 查询交互记忆
curl http://localhost:8000/memories
```

---

## 预处理层

消息进入 API 后，**AI 调用前**先完成三步预处理：

### 脱敏

| 类型 | 正则匹配 | 替换为 | 示例 |
|------|---------|--------|------|
| 手机号 | `1[3-9]\d{9}` | `[PHONE]` | `13800001234` -> `[PHONE]` |
| IP 地址 | `\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}` | `[IP]` | `192.168.1.100` -> `[IP]` |
| 邮箱 | `[\w.-]+@[\w.-]+\.\w+` | `[EMAIL]` | `zhang@co.com` -> `[EMAIL]` |
| 身份证 | `\d{17}[\dXx]` | `[IDCARD]` | `110101199001011234` -> `[IDCARD]` |
| 密码 | `密码[是为：:]\s*(\S+)` | `[MASKED]` | `密码是abc123` -> `密码：[MASKED]` |

> 脱敏后文本发给 AI，原始文本不入库，入库时存脱敏版本。

### 意图检测

| 意图 | 说明 | 检测方式 |
|------|------|---------|
| `report_issue` | 报障 | 默认意图 |
| `casual_chat` | 闲聊 | 关键词：你好/谢谢/再见 |
| `feedback_resolved` | 反馈已解决 | 关键词：解决了/搞定了 |
| `feedback_unresolved` | 反馈未解决 | 关键词：没解决/还是不行 |
| `request_human` | 转人工 | 关键词：IT协助/需要工程师 |
| `query_status` | 查询任务状态 | 关键词：我的任务/进度 |

### 复杂度检测

| 复杂度 | 说明 | 驱动模型 |
|--------|------|---------|
| `simple` | 标准桌面问题 | deepseek-chat（快速便宜） |
| `medium` | 需要工具辅助排查 | deepseek-chat + 工具 |
| `hard` | 严重故障需人工 | deepseek-reasoner（推理模型） |

---

## 混合路由

预处理后按意图/复杂度分流，**确定性流程与 Agent 流程结合**：

| 路由分支 | 处理方式 | LLM 调用 | 特点 |
|---------|---------|---------|------|
| 闲聊 | 快速 LLM 回复 | 1次 | 不走 Agent，max_tokens=200 |
| 反馈 | 复用 feedback.py | 0次 | 升级/催办/关闭，纯 DB 操作 |
| 查询状态 | 查数据库返回 | 0次 | 纯 DB 操作 |
| 简单报障 | 知识库+单次 LLM | 1次 | 固定流程，一步到位，1-2秒 |
| 复杂报障 | Agent+工具调用 | 2-4次 | AI 自主决策，最多3轮工具调用 |
| 转人工 | Agent+工具调用 | 2-4次 | AI 调用 assign_engineer 工具 |

> 反馈无 active 任务时自动转为报障处理，重新检测复杂度。

---

## Agent 层（仅复杂问题）

### 模型路由

按复杂度自动选择模型，**简单问题用小模型省钱，复杂问题用大模型保证质量**：

```python
MODEL_ROUTING = {
    "simple": {"model": "deepseek-chat", "tools_enabled": False},
    "medium": {"model": "deepseek-chat", "tools_enabled": True},
    "hard":   {"model": "deepseek-reasoner", "tools_enabled": True},
}
```

### 工具调用（单 Agent，不做多节点编排）

| 工具 | 说明 |
|------|------|
| `get_current_time` | 获取当前时间 |
| `search_knowledge` | 检索知识库（skill 文档） |
| `search_memory` | 检索历史交互记忆 |
| `assign_engineer` | 分配工程师（负载均衡） |
| `query_user_tasks` | 查询用户任务状态 |

> AI 自主决定是否调用工具，最多 3 轮工具调用，防止死循环。
> MCP（Model Context Protocol）作为预留扩展点，未来接入监控/工单/AD 域等外部系统。

---

## 后处理层

**仅对报障场景执行**（闲聊/反馈/查询跳过）：

```
AI 原始回答
  ↓
① 二次脱敏（AI 可能引用了敏感信息）
  ↓
② 入库（存脱敏版本 + intent/complexity/model_used）
  ↓
③ LLM 总结（"打印机离线 -> 重启服务"，不超过50字）
  ↓
④ 向量化存储（embedding 存入记忆库，供未来检索）
```

### 持久化记忆

每次报障交互都会生成一条记忆，向量化后存入 ChromaDB（独立 collection）：

```
用户第二次报"VPN连不上"
  ↓
search_memory 检索到："VPN连不上 -> 重装客户端 -> 已解决 (T1002)"
  ↓
AI 回答："您上次也遇到过 VPN 问题，当时通过重装客户端解决了..."
```

---

## 钉钉接入

钉钉 Stream 模式作为**纯转发层**，不处理业务逻辑：

```
钉钉消息 -> Stream 收到 -> 转发 POST /api/v1/message -> 收到响应 -> 回复用户
```

### 接入步骤

1. 在 [钉钉开放平台](https://open.dingtalk.com) 创建企业应用，获取 AppKey 和 AppSecret
2. 在 `.env` 中填入 `DINGTALK_CLIENT_ID` 和 `DINGTALK_CLIENT_SECRET`
3. 启动服务 -> 工程师给机器人发消息 -> UserID 自动绑定到数据库

---

## 定时提醒

任务分配后超时未解决，自动提醒并转派：

```
任务 assigned -> 30分钟未解决 -> 第1次提醒
             -> 30分钟 -> 第2次提醒
             -> 30分钟 -> 第3次提醒
             -> 30分钟（达上限3次）
               ├─ 有其他工程师 -> 自动转派（排除当前）
               └─ 仅一人 -> 继续提醒 + 通知 IT 群
```

---

## API 接口

### POST /api/v1/message ★ 统一入口（推荐）

**请求：**
```json
{
  "source": "dingtalk",
  "sender_id": "REMOVED",
  "sender_name": "小明",
  "content": "打印机连不上，IP是192.168.1.100"
}
```

**响应：**
```json
{
  "intent": "report_issue",
  "complexity": "simple",
  "model_used": "deepseek-chat",
  "response": "请按以下步骤操作：1. 检查电源...\n\n📋 任务编号：T1001",
  "task_no": "T1001",
  "memory_saved": true
}
```

### POST /task（旧版兼容）

旧版 LangGraph 工作流接口，保留兼容。

### 管理接口

| 接口 | 方法 | 说明 |
|------|------|------|
| `/health` | GET | 健康检查 |
| `/tasks` | GET | 查询任务列表 |
| `/engineers` | GET | 查询工程师名单（含动态负载） |
| `/memories` | GET | 查询交互记忆 |

---

## 让 AI 理解本项目

```
这是一个基于 FastAPI + LangChain + MySQL 的运维任务处理系统（v2.1 混合架构）。

统一入口：POST /api/v1/message 接管所有消息源（钉钉/API/Web）。
钉钉 Stream（dingtalk_stream.py）是纯转发层，收到消息转发给 API，零业务代码。

混合路由（router.py）：预处理后按意图/复杂度分流
- 确定性流程（不走Agent）：闲聊(快速LLM) / 反馈(复用feedback.py) / 查询(查DB) / 简单报障(知识库+单次LLM)
- Agent流程（仅复杂问题）：medium/hard报障 -> ai_agent.py 模型路由+工具调用，最多3轮

预处理层（preprocess.py）：脱敏（5类正则）-> 意图检测（6种）-> 复杂度检测（3档）
Agent层（ai_agent.py）：按复杂度路由模型，意图注入，单agent+5工具（agent_tools.py）
后处理层（postprocess.py）：仅报障场景 -> 二次脱敏 -> 入库 -> LLM总结 -> 向量化记忆（memory.py）

数据库：MySQL + SQLAlchemy 2.0，4张表：engineers/tasks/feedbacks/memories
知识库+记忆库：ChromaDB双collection（tools.py + memory.py）
负载均衡：graph.py assign_engineer()，作为AI工具被调用
定时提醒：scheduler.py，APScheduler后台扫描，3次未响应自动转派
旧版兼容：POST /task + graph.py + feedback.py 保留

入口 main.py，启动时自动建库建表+迁移engineers.json+启动定时提醒。
```

---

## 常见问题

| 问题 | 原因 | 解决 |
|------|------|------|
| 启动报 ModuleNotFoundError | 依赖未装 | `pip install -r requirements.txt` |
| 数据库连接失败 | MySQL 未启动或密码错误 | 检查 MySQL 服务 + `.env` 中 MYSQL 配置 |
| 数据库表未创建 | 首次启动初始化失败 | 查看启动日志，确认 `init_db()` 执行成功 |
| 脱敏未生效 | 正则未覆盖 | 检查 `preprocess.py` 中 DESENSITIZE_PATTERNS |
| 意图检测误判 | 关键词未覆盖 | 检查 `preprocess.py` 关键词列表，或开启 LLM 兜底 |
| 模型路由不对 | `.env` 配置有误 | 检查 `MODEL_SIMPLE` / `MODEL_MEDIUM` / `MODEL_HARD` |
| 简单问题走了Agent | 复杂度检测不准 | 检查 `preprocess.py` 复杂度规则，或调整 HARD_KEYWORDS |
| 记忆未存储 | 记忆功能未启用 | 检查 `MEMORY_ENABLED=true` |
| 钉钉转发超时 | LLM 响应慢 | 调整 `dingtalk_stream.py` 中 timeout（默认 120s） |
| 定时提醒未触发 | 调度器未启动 | 查看启动日志是否有 `[scheduler] ✅ 定时提醒已启动` |
| 知识库检索不到 | 向量库未重建 | 删 `chroma_db/` 后重启 |
| 钉钉私聊通知发不出 | dingtalk_user_id 不正确 | 让工程师给机器人发消息自动绑定 |

---

## 技术栈

| 组件 | 选型 | 原因 |
|------|------|------|
| AI 框架 | LangChain tool calling | 单 agent + 工具调用，灵活简洁 |
| 模型路由 | .env 配置 + config.py | 按复杂度选模型，成本可控 |
| 向量数据库 | ChromaDB | 知识库 + 记忆库双 collection |
| 关系型数据库 | MySQL | 任务/工程师/反馈/记忆持久化 |
| ORM | SQLAlchemy 2.0 | Mapped 风格类型安全 |
| Embedding | HuggingFace (text2vec-base-chinese) | 免费、离线、中文优化 |
| LLM | OpenAI 兼容 API | DeepSeek/OpenAI/通义千问随意切换 |
| Web 框架 | FastAPI | 异步、自带 Swagger 文档 |
| 定时调度 | APScheduler | 进程内后台调度 |
| 脱敏 | Python regex | 确定性规则，零 LLM 成本 |

---

## 版本

| 版本 | 日期 | 说明 |
|------|------|------|
| **v2.1.0** | 2026-07-10 | 混合路由：确定性流程+Agent，简单问题一步到位 |
| v2.0.0 | 2026-07-10 | 新版架构：统一入口 + 预处理 + 模型路由 + 工具化AI + 记忆 |
| v1.1.0 | 2026-07-10 | 定时重新提醒：超时提醒 + 自动转派 |
| v1.0.0 | 2026-07-09 | 第一次大改版：任务持久化 + 反馈闭环 + 负载均衡 |
| v0.2.0 | 2026-06-15 | 钉钉 Stream 接入 |
| v0.1.0 | 2026-06 | 初始版本 |

详见 `新版架构方案.md`、`运维Agent框架文档.md` 第十一章「版本控制」和 `CHANGELOG.md`。

---

## License

MIT
