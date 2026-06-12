"""
LangGraph 工作流定义 —— 整个 Agent 的核心调度逻辑。
"""

import json
import os
from typing import Literal

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, StateGraph
from pydantic import SecretStr

from .models import AgentState, Difficulty, Task
from .tools import DATA_DIR, load_engineers, retrieve_knowledge

_env_paths = [
    DATA_DIR / ".env",
    DATA_DIR.parent / ".env",
]
for _p in _env_paths:
    if _p.exists():
        load_dotenv(_p)
        print(f"[graph] 已加载环境变量：{_p}")
        break
else:
    load_dotenv()
    print("[graph] 未找到 .env 文件，使用环境变量或默认值")


def _get_text(response) -> str:
    """从 LLM 响应中安全提取纯文本字符串。"""
    content = response.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        # 多模态格式：[{"type":"text","text":"..."}, ...]
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(item.get("text", str(item)))
            else:
                parts.append(str(item))
        return "".join(parts)
    return str(content)


open_code_go_api = os.getenv("open_code_go_api")
# LLM 客户端（统一用这一个）
llm = ChatOpenAI(
    model=os.getenv("model") or " ",
    base_url=os.getenv("base_url") or " ",
    api_key=SecretStr(open_code_go_api or ""),
    temperature=0,  # 设为 0 让输出更稳定
)

# ==================== 分类节点 ====================

CLASSIFY_PROMPT = """你是一个 IT 运维任务分类器。根据用户的任务描述，判断难度等级。

难度定义：
- easy（简单）：标准桌面支持问题。包括但不限于：打印机连接、密码重置、VPN配置、
  邮箱设置、Outlook配置、软件安装、系统蓝屏、网线连接、WiFi问题、显示器问题。
- hard（困难）：需要高级排查的问题。包括但不限于：服务器故障、数据库异常、网络交换机配置、
  安全事件、防火墙策略、Linux系统管理、存储设备、虚拟化平台。

以下情况必须强制分类为 hard：
1. 用户明确要求人工协助或IT工程师支持（如"需要IT协助"、"找IT"、"叫IT"、"人工处理"、"帮忙看看"、"远程协助"等）
2. 用户描述过于模糊，无法判断具体是什么技术问题（如只说"出问题了"、"帮我处理一下"等）
3. 用户表达了紧迫性或焦虑情绪（如"紧急"、"崩溃"、"全断了"、"业务中断"等）
4. 用户提到需要人工或工程师介入（如"需要上门"、"需要现场"、"需要人来看"等）

请只回复一个 JSON 对象，格式：
{"difficulty": "easy", "reason": "一句话说明判断依据"}

不要回复任何其他内容。"""


# 命中以下任意关键词，直接判定为 hard，无需 LLM 参与
HARD_KEYWORDS = [
    "IT协助",
    "IT支持",
    "技术支持",
    "人工处理",
    "需要工程师",
    "紧急",
    "求助",
    "搞不定",
    "帮帮忙",
    "找IT",
    "叫IT",
    "需要IT",
    "帮忙看看",
    "过来看看",
    "远程协助",
    "上门",
    "现场",
    "人来看",
    "人工介入",
    "需要人",
    "崩溃",
    "全断了",
    "业务中断",
    "需要协助",
    "帮我处理",
]


def classify_node(state: AgentState) -> dict:
    """
    调用 LLM 对任务进行难度分类。
    输入：state.task
    输出：state.difficulty
    """
    task = state.task
    if task is None:
        return {"difficulty": "easy"}

    # ===== 第一层：关键词预筛（确定性规则，不走 LLM）=====
    user_text = f"{task.title} {task.description}"
    for kw in HARD_KEYWORDS:
        if kw in user_text:
            print(f"[classify] 关键词命中「{kw}」→ 直接判定为 hard")
            return {"difficulty": "hard"}
    # =====================================================

    messages = [
        SystemMessage(content=CLASSIFY_PROMPT),
        HumanMessage(content=f"任务标题：{task.title}\n任务描述：{task.description}"),
    ]
    response = llm.invoke(messages)

    # 解析 LLM 返回的 JSON
    try:
        result = json.loads(_get_text(response))
        difficulty = result.get("difficulty", "easy")
    except json.JSONDecodeError:
        # 解析失败，默认当作困难任务（安全优先原则）
        difficulty = "hard"

    return {"difficulty": difficulty}


# ==================== 路由判断 ====================


def route_after_classify(state: AgentState) -> Literal["retrieve", "assign"]:
    """
    分类后走哪条路：简单 → 检索知识库；困难 → 分配工程师
    """
    if state.difficulty == Difficulty.EASY:
        return "retrieve"
    else:
        return "assign"


# ==================== 知识检索节点 ====================


def retrieve_node(state: AgentState) -> dict:
    """
    从向量知识库检索与任务相关的文档，并附加工程师团队信息。
    """
    if state.task is None:
        return {"knowledge_context": ""}
    query = f"{state.task.title}\n{state.task.description}"
    context = retrieve_knowledge(query)

    # 附加工程师团队信息，让 LLM 回答时了解可用的人力资源
    engineers = load_engineers()
    if engineers:
        lines_info = [f"## 当前IT工程师团队（共{len(engineers)}人）"]
        for e in engineers:
            skills = ", ".join(e.get("skills", []))
            current = e.get("current_load", 0)
            available = "在岗" if e.get("available", True) else "休假"
            lines_info.append(
                f"- {e['name']}：擅长 {skills}（{available}，当前任务数：{current}）"
            )
        context = context + "\n\n" + "\n".join(lines_info)

    return {"knowledge_context": context}


# ==================== 答案生成节点 ====================

ANSWER_PROMPT = """你是公司的 IT 运维助手。基于下面的知识库内容和工程师团队信息，回答用户的问题。

要求：
1. 用通俗易懂的语言，一步步说明解决方案
2. 如果知识库中有具体操作步骤，完整列出
3. 如果知识库内容不足以解决该问题，诚实告知用户，并建议联系 IT 工程师

知识库参考内容：
{context}"""


def answer_node(state: AgentState) -> dict:
    """
    基于检索到的知识，用 LLM 生成面向用户的回答。
    """
    if state.task is None:
        return {"final_response": "信息缺失，请重试"}
    messages = [
        SystemMessage(content=ANSWER_PROMPT.format(context=state.knowledge_context)),
        HumanMessage(
            content=f"问题：{state.task.title}\n详细描述：{state.task.description}"
        ),
    ]
    response = llm.invoke(messages)
    return {"final_response": _get_text(response)}


# ==================== 工程师分配节点 ====================

MATCH_PROMPT = """你是一个 IT 任务分配助手。根据任务描述和工程师列表，选出最合适的工程师。

工程师名单：
{engineers}

请分析任务需要什么技能，哪个工程师的技能最匹配。
只回复一个 JSON 对象：
{{"engineer_name": "张三", "reason": "一句话理由"}}

不要回复其他内容。"""


def assign_node(state: AgentState) -> dict:
    """
    对于困难任务：用 LLM 匹配最合适的工程师，然后发送通知。
    """

    if state.task is None:
        return {"final_response": "任务信息缺失，无法分配。"}

    engineers = load_engineers()

    # 用 LLM 做技能匹配
    messages = [
        SystemMessage(
            content=MATCH_PROMPT.format(
                engineers=json.dumps(engineers, ensure_ascii=False, indent=2)
            )
        ),
        HumanMessage(
            content=f"任务：{state.task.title}\n描述：{state.task.description}"
        ),
    ]
    response = llm.invoke(messages)

    try:
        result = json.loads(_get_text(response))
        engineer_name = result.get("engineer_name", "无人匹配")
        reason = result.get("reason", "")
    except json.JSONDecodeError:
        engineer_name = engineers[0]["name"] if engineers else "无人匹配"
        reason = "自动解析失败，已默认分配"

    # 尝试发送企业微信通知（如果配置了 Webhook）
    _notify_engineer(engineer_name, state.task)

    final_msg = f"""🔧 任务已分配给 **{engineer_name}**。

分配原因：{reason}

请 {engineer_name} 尽快处理：
- 任务：{state.task.title}
- 描述：{state.task.description}
- 提交人：{state.task.submitted_by}"""

    return {
        "assigned_engineer": engineer_name,
        "final_response": final_msg,
    }


def _notify_engineer(engineer_name: str, task: Task):
    """
    发送通知：优先钉钉，其次企业微信。
    """
    dingtalk_url = os.getenv("DINGTALK_WEBHOOK", "")
    wechat_url = os.getenv("WECHAT_WEBHOOK", "")

    import requests

    # --- 钉钉消息 ---
    if dingtalk_url:
        dingtalk_payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": "🚨 新任务分配",
                "text": f"""## 🚨 新运维任务分配
> 负责人：**{engineer_name}**
> 任务：{task.title}
> 提交人：{task.submitted_by}

**详细描述：**
{task.description}

---
[查看详情](http://your-ticket-system)""",
            },
        }
        try:
            requests.post(dingtalk_url, json=dingtalk_payload, timeout=5)
        except Exception as e:
            print(f"[钉钉通知] 发送失败：{e}")

    # --- 企业微信消息（兼容）---
    elif wechat_url:
        wechat_payload = {
            "msgtype": "markdown",
            "markdown": {
                "content": f"""## 🚨 新任务分配
> 负责人：<font color="warning">{engineer_name}</font>
> 任务：{task.title}

**详细描述：**
{task.description}"""
            },
        }
        try:
            requests.post(wechat_url, json=wechat_payload, timeout=5)
        except Exception as e:
            print(f"[企微通知] 发送失败：{e}")

    else:
        print(f"[通知] 未配置 Webhook，跳过。应通知 {engineer_name} 处理：{task.title}")


# ==================== 组装工作流 ====================


def build_graph():
    """
    把上面的节点串起来，构建完整工作流。
    """
    workflow = StateGraph(AgentState)

    # 添加节点
    workflow.add_node("classify", classify_node)
    workflow.add_node("retrieve", retrieve_node)
    workflow.add_node("answer", answer_node)
    workflow.add_node("assign", assign_node)

    # 入口
    workflow.set_entry_point("classify")

    # 条件分支：分类后走哪条路
    workflow.add_conditional_edges(
        "classify",
        route_after_classify,
        {
            "retrieve": "retrieve",
            "assign": "assign",
        },
    )

    # 分支终点都连到 END
    workflow.add_edge("retrieve", "answer")
    workflow.add_edge("answer", END)
    workflow.add_edge("assign", END)

    return workflow.compile()


# 编译一次，全局复用
agent_app = build_graph()
