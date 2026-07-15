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
from .tools import DATA_DIR, count_active_tasks, load_engineers, retrieve_knowledge

# LLM 调用超时（秒）
_GRAPH_LLM_TIMEOUT = float(os.getenv("LLM_REQUEST_TIMEOUT", "60"))

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
    timeout=_GRAPH_LLM_TIMEOUT,
)

# ==================== 分类节点 ====================


CLASSIFY_PROMPT = """你是一个 IT 运维任务分类器。根据用户的任务描述，判断难度等级。

难度定义：
- simple（简单）：标准桌面支持问题、日常闲聊、非技术性咨询。
  包括：打印机、密码重置、VPN、邮箱、软件安装、系统蓝屏、WiFi、
  打招呼、致谢、询问功能等。
- hard（困难）：需要高级排查或人工介入的问题。
  包括：服务器故障、数据库异常、交换机配置、安全事件、防火墙、
  Linux系统管理、虚拟化平台等。

以下情况必须强制分类为 hard：
1. 用户明确要求人工协助或IT工程师到场处理
2. 用户描述了具体的技术问题但信息不足，需要人工排查
3. 用户表达了紧迫性或焦虑情绪（紧急、崩溃等）

以下情况应分类为 simple：
- 问候、致谢等日常闲聊（非技术问题）
- 询问机器人功能的非技术性问题
- 标准 IT 桌面支持问题

请只回复一个 JSON 对象，格式：
{"difficulty": "simple", "reason": "一句话说明判断依据"}

不要回复任何其他内容。"""

CASUAL_PATTERNS = [
    "你好",
    "在吗",
    "在么",
    "早上好",
    "下午好",
    "晚上好",
    "谢谢",
    "感谢",
    "多谢",
    "辛苦",
    "再见",
    "拜拜",
    "你是谁",
    "你叫什么",
    "你能做什么",
    "你有什么功能",
    "测试",
    "test",
]

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
        return {"difficulty": "simple"}

    # ===== 第一层：关键词预筛（确定性规则，不走 LLM）=====
    user_text = f"{task.title} {task.description}"
    # ===== 第零层：闲聊预筛（非 IT 任务，直接判定 simple）=====
    user_text_lower = user_text.lower()
    for pattern in CASUAL_PATTERNS:
        if pattern.lower() in user_text_lower:
            print(f"[classify] 闲聊命中「{pattern}」-> 直接判定为 simple")
            return {"difficulty": "simple"}
    # =====================================================

    for kw in HARD_KEYWORDS:
        if kw in user_text:
            print(f"[classify] 关键词命中「{kw}」-> 直接判定为 hard")
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
        difficulty = result.get("difficulty", "simple")
    except json.JSONDecodeError:
        # 解析失败，默认当作困难任务（安全优先原则）
        difficulty = "hard"

    return {"difficulty": difficulty}


# ==================== 路由判断 ====================


def route_after_classify(state: AgentState) -> Literal["retrieve", "assign"]:
    """
    分类后走哪条路：简单 → 检索知识库；困难 → 分配工程师
    """
    if state.difficulty == Difficulty.SIMPLE:
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


# ==================== 存库辅助函数 ====================


def _save_task_to_db(
    state: AgentState, final_response: str, status: str, engineer: str = ""
) -> str:
    """
    将任务存入数据库，返回任务编号 task_no。
    存库失败不阻断主流程，返回空字符串。
    """
    if state.task is None:
        return ""
    try:
        from . import db_manager

        task_dict = db_manager.create_task(
            title=state.task.title,
            description=state.task.description,
            submitted_by=state.task.submitted_by,
            submitter_id=state.submitter_id,
            difficulty=state.difficulty.value if state.difficulty else "simple",
            status=status,
            assigned_engineer=engineer,
            final_response=final_response,
        )
        task_no = task_dict.get("task_no", "")
        print(f"[graph] ✅ 任务已存库：{task_no}（{status}）")
        return task_no
    except Exception as e:
        print(f"[graph] ⚠️ 任务存库失败（不阻断流程）：{e}")
        return ""


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
    基于检索到的知识，用 LLM 生成面向用户的回答，并存库（status=auto_answered）。
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
    answer = _get_text(response)

    # 存库（auto_answered），失败不阻断主流程
    task_no = _save_task_to_db(state, answer, status="auto_answered")

    # 在回答末尾附上任务编号，方便用户反馈时引用
    if task_no:
        answer = (
            f"{answer}\n\n---\n📋 任务编号：**{task_no}**（如未解决请回复“未解决”）"
        )

    return {"final_response": answer, "task_no": task_no}


# ==================== 工程师分配节点 ====================

MATCH_PROMPT = """你是一个 IT 任务分配助手。根据任务描述和工程师列表，选出所有技能匹配的工程师。

工程师名单：
{engineers}

请分析任务需要什么技能，哪些工程师的技能匹配。
只回复一个 JSON 对象：
{{"candidates": ["张三", "李四"], "reason": "需要网络和 VPN 技能"}}

重要：
1. candidates 可以是 1 个或多个，必须从上述名单中选，不可编造名字
2. 如果没有匹配的，candidates 返回空数组 []
3. reason 说明匹配理由
不要回复其他内容。"""


def assign_engineer(task: Task, exclude_name: str = "") -> tuple[str, str]:
    """
    混合负载均衡分配算法（LLM 筛技能 + 算法做负载均衡）。
    返回 (工程师姓名, 分配原因)。
    供 assign_node 和反馈升级复用。

    参数:
        exclude_name: 转派时排除的工程师姓名（默认空，不排除）
    """
    import random

    engineers = load_engineers()
    if not engineers:
        return "", "无可用工程师"

    # 排除指定工程师（转派场景）
    if exclude_name:
        engineers = [e for e in engineers if e["name"] != exclude_name]
        if not engineers:
            return "", "无其他可用工程师"

    # Step 1: LLM 返回候选人列表
    messages = [
        SystemMessage(
            content=MATCH_PROMPT.format(
                engineers=json.dumps(engineers, ensure_ascii=False, indent=2)
            )
        ),
        HumanMessage(content=f"任务：{task.title}\n描述：{task.description}"),
    ]
    response = llm.invoke(messages)

    candidates_names = []
    reason = ""
    try:
        result = json.loads(_get_text(response))
        candidates_names = result.get("candidates", [])
        reason = result.get("reason", "")
    except json.JSONDecodeError:
        print("[assign] LLM 返回解析失败，候选人置空")

    # Step 2: 校验候选人是否真实存在
    matched = [e for e in engineers if e["name"] in candidates_names]

    if not matched:
        # 无候选人 → 默认第一位
        chosen = engineers[0]
        return chosen["name"], f"无匹配工程师，默认分配（{reason}）"

    # Step 3: 优先在岗，全部不在岗则用不在岗的（方案 B）
    available_pool = [e for e in matched if e.get("available", True)]
    pool = available_pool if available_pool else matched

    # Step 4: 动态计算负载
    for e in pool:
        e["current_load"] = count_active_tasks(e["name"])

    # Step 5: 选最低负载
    min_load = min(e["current_load"] for e in pool)
    finalists = [e for e in pool if e["current_load"] == min_load]

    # Step 6: 优先 0 负载，同负载随机
    zero_load = [e for e in finalists if e["current_load"] == 0]
    chosen = random.choice(zero_load if zero_load else finalists)

    load_reason = f"技能匹配，当前任务数最少（{chosen['current_load']}个）"
    if not chosen.get("available", True):
        load_reason += "（注意：该工程师当前不在岗，可能响应较慢）"
    full_reason = f"{reason}；{load_reason}" if reason else load_reason

    return chosen["name"], full_reason


def assign_engineer_by_algorithm(candidate_names: list[str]) -> tuple[str, str]:
    """
    纯算法负载均衡（不调用 LLM）。
    从 AI 提供的候选人中选负载最低的在岗工程师。
    供 agent_tools.py 的 assign_engineer 工具调用。
    """
    import random

    engineers = load_engineers()
    if not engineers:
        return "", "无可用工程师"

    # 从全量工程师中筛选 AI 指定的候选人
    matched = [e for e in engineers if e["name"] in candidate_names]

    if not matched:
        return "", "候选人不在工程师名单中"

    # 优先在岗，全部不在岗则用不在岗的（方案 B）
    available_pool = [e for e in matched if e.get("available", True)]
    pool = available_pool if available_pool else matched

    # 动态计算负载
    for e in pool:
        e["current_load"] = count_active_tasks(e["name"])

    # 选最低负载
    min_load = min(e["current_load"] for e in pool)
    finalists = [e for e in pool if e["current_load"] == min_load]

    # 优先 0 负载，同负载随机
    zero_load = [e for e in finalists if e["current_load"] == 0]
    chosen = random.choice(zero_load if zero_load else finalists)

    load_reason = f"当前任务数最少（{chosen['current_load']}个）"
    if not chosen.get("available", True):
        load_reason += "（注意：该工程师当前不在岗，可能响应较慢）"

    return chosen["name"], load_reason


def assign_node(state: AgentState) -> dict:
    """
    对于困难任务：用负载均衡算法分配工程师，存库，发送通知。
    """
    if state.task is None:
        return {"final_response": "任务信息缺失，无法分配。"}

    # 调用负载均衡算法
    engineer_name, reason = assign_engineer(state.task)

    if not engineer_name:
        final_msg = "❌ 暂无可用工程师，请联系 IT 主管人工处理。"
        return {"assigned_engineer": "", "final_response": final_msg}

    # 存库（assigned）
    final_msg = f"""🔧 任务已分配给 **{engineer_name}**。

分配原因：{reason}

请 {engineer_name} 尽快处理：
- 任务：{state.task.title}
- 描述：{state.task.description}
- 提交人：{state.task.submitted_by}"""

    task_no = _save_task_to_db(
        state, final_msg, status="assigned", engineer=engineer_name
    )
    if task_no:
        final_msg = f"{final_msg}\n\n---\n📋 任务编号：**{task_no}**"

    # 发送通知（钉钉私聊 + 群简报 / 企微）
    try:
        _notify_engineer(engineer_name, state.task)
    except Exception as e:
        print(f"[assign] 通知发送失败：{e}")

    return {
        "assigned_engineer": engineer_name,
        "final_response": final_msg,
        "task_no": task_no,
    }


# ==================== 钉钉 API 辅助函数 ====================

# 缓存 access token，避免每次通知都重新获取
_dingtalk_token_cache: dict = {"token": "", "expires_at": 0}


def _get_dingtalk_access_token() -> str:
    """
    获取钉钉 Open API 的 access_token。
    使用 AppKey 和 AppSecret 换取，token 有效期内复用缓存。
    """
    import time as _time

    now = _time.time()
    if _dingtalk_token_cache["token"] and now < _dingtalk_token_cache["expires_at"]:
        return _dingtalk_token_cache["token"]

    client_id = os.getenv("DINGTALK_CLIENT_ID", "")
    client_secret = os.getenv("DINGTALK_CLIENT_SECRET", "")

    if not client_id or not client_secret:
        print("[钉钉API] 未配置 CLIENT_ID / CLIENT_SECRET，无法获取 token")
        return ""

    import requests as _requests

    try:
        resp = _requests.post(
            "https://api.dingtalk.com/v1.0/oauth2/accessToken",
            json={"appKey": client_id, "appSecret": client_secret},
            timeout=10,
        )
        data = resp.json()
        token = data.get("accessToken", "")
        expires_in = data.get("expireIn", 7200)  # 默认 2 小时
        _dingtalk_token_cache["token"] = token
        _dingtalk_token_cache["expires_at"] = now + expires_in - 300  # 提前 5 分钟刷新
        print(f"[钉钉API] 获取 access_token 成功，有效期 {expires_in}s")
        return token
    except Exception as e:
        print(f"[钉钉API] 获取 access_token 失败：{e}")
        return ""


def _send_dingtalk_direct_message(user_id: str, title: str, text: str) -> bool:
    """
    通过钉钉 Open API 给指定用户发送一条 Markdown 私聊消息。

    参数：
        user_id  : 工程师的钉钉 UserId
        title    : 消息标题
        text     : 消息正文（Markdown 格式）

    返回：成功返回 True，失败返回 False
    """
    if not user_id:
        print("[钉钉私聊] user_id 为空，跳过")
        return False

    token = _get_dingtalk_access_token()
    if not token:
        print("[钉钉私聊] 无法获取 access_token，跳过")
        return False

    client_id = os.getenv("DINGTALK_CLIENT_ID", "")

    import requests as _requests

    payload = {
        "robotCode": client_id,
        "userIds": [user_id],
        "msgKey": "sampleMarkdown",
        "msgParam": json.dumps({"title": title, "text": text}, ensure_ascii=False),
    }

    try:
        resp = _requests.post(
            "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend",
            headers={
                "x-acs-dingtalk-access-token": token,
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=10,
        )
        result = resp.json()
        # 成功的响应通常包含 processQueryKey
        if "processQueryKey" in result or resp.status_code == 200:
            print(f"[钉钉私聊] ✅ 已向 {user_id} 发送私聊提醒")
            return True
        else:
            print(f"[钉钉私聊] 发送失败：{result}")
            return False
    except Exception as e:
        print(f"[钉钉私聊] 请求异常：{e}")
        return False


# ==================== 通知函数 ====================


def _notify_engineer(engineer_name: str, task: Task):
    """
    发送通知：IT 群简报 + 工程师私聊完整工单。

    1. IT 群简报：通过 Webhook 发送摘要消息，@ 对应工程师，让团队知道谁在负责
    2. 私聊完整工单：通过钉钉 Open API 给工程师发送完整的任务详情（仅本人可见）
    """
    # ---- 查找工程师信息 ----
    engineers = load_engineers()
    engineer_info = None
    for e in engineers:
        if e.get("name") == engineer_name:
            engineer_info = e
            break

    mobile = engineer_info.get("mobile", "") if engineer_info else ""
    dingtalk_user_id = (
        engineer_info.get("dingtalk_user_id", "") if engineer_info else ""
    )

    dingtalk_url = os.getenv("DINGTALK_WEBHOOK", "")
    wechat_url = os.getenv("WECHAT_WEBHOOK", "")

    import requests

    # ========== 1. IT 群简报（@ 提及对应工程师）==========
    if dingtalk_url:
        # 截取描述前 100 字作为简报摘要
        brief_desc = task.description[:100].replace("\n", " ")
        if len(task.description) > 100:
            brief_desc += "…"
        at_line = f"\n> @{mobile}" if mobile else ""
        markdown_text = f"""## 🚨 新运维任务
> 负责人：**{engineer_name}**{at_line}
> 任务：{task.title}
> 提交人：{task.submitted_by}

**摘要：**{brief_desc}

---
📋 完整工单已私发 {engineer_name}"""

        dingtalk_payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": "🚨 新运维任务",
                "text": markdown_text,
            },
            "at": {
                "atMobiles": [mobile] if mobile else [],
                "isAtAll": False,
            },
        }

        try:
            resp = requests.post(dingtalk_url, json=dingtalk_payload, timeout=5)
            if resp.status_code == 200:
                print(f"[钉钉群通知] ✅ 已在群里 @{engineer_name}({mobile})，发送简报")
            else:
                print(f"[钉钉群通知] 发送失败：{resp.text}")
        except Exception as e:
            print(f"[钉钉群通知] 发送异常：{e}")

    # ========== 2. 私聊完整工单（钉钉 Open API）==========
    if dingtalk_user_id:
        dm_title = f"🔧 新任务「{task.title}」"
        dm_text = f"""## 🔧 新任务已分配给你

> 任务：**{task.title}**
> 提交人：{task.submitted_by}
> 负责人：**{engineer_name}**

**完整描述：**
{task.description}

---
请尽快处理，如有疑问请联系提交人「{task.submitted_by}」。
（此消息仅你可见）"""
        _send_dingtalk_direct_message(dingtalk_user_id, dm_title, dm_text)
    elif mobile:
        # 如果没有钉钉 UserId但有手机号，尝试用手机号作为 userId
        print(f"[钉钉私聊] 未配置 dingtalk_user_id，尝试用手机号 {mobile} 发送")
        dm_title = f"🔧 新任务「{task.title}」"
        dm_text = f"""## 🔧 新任务已分配给你

> 任务：**{task.title}**
> 提交人：{task.submitted_by}
> 负责人：**{engineer_name}**

**完整描述：**
{task.description}

---
请尽快处理。
（此消息仅你可见）"""
        _send_dingtalk_direct_message(mobile, dm_title, dm_text)
    else:
        print(
            f"[钉钉私聊] {engineer_name} 未配置 mobile/dingtalk_user_id，跳过私聊提醒"
        )

    # ========== 3. 企业微信兜底 ==========
    if not dingtalk_url and wechat_url:
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

    if not dingtalk_url and not wechat_url:
        print(
            f"[通知] 未配置任何 Webhook，跳过。应通知 {engineer_name} 处理：{task.title}"
        )


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
