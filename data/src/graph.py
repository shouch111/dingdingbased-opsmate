"""
工程师分配 + 钉钉通知 -- 供 feedback.py / scheduler.py / agent_tools.py 复用。

v3.4.0 起移除旧版 LangGraph 工作流（classify/retrieve/answer/assign 节点 + build_graph），
新架构走 router.py + ai_agent.py。本文件只保留被其他模块依赖的纯函数：
- assign_engineer / assign_engineer_by_algorithm：负载均衡分配
- _get_dingtalk_access_token / _send_dingtalk_direct_message / _notify_engineer：钉钉通知
"""

import json
import logging
import os

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from .llm_utils import safe_llm_invoke
from .models import Task
from .preprocess import CASUAL_PATTERNS, HARD_KEYWORDS  # noqa: F401 统一关键词来源
from .tools import DATA_DIR, load_engineers
from .utils import extract_text

logger = logging.getLogger(__name__)

# LLM 调用超时（秒）
_GRAPH_LLM_TIMEOUT = float(os.getenv("LLM_REQUEST_TIMEOUT", "60"))

_env_paths = [
    DATA_DIR / ".env",
    DATA_DIR.parent / ".env",
]
for _p in _env_paths:
    if _p.exists():
        load_dotenv(_p)
        logger.info("已加载环境变量：%s", _p)
        break
else:
    load_dotenv()
    logger.warning("未找到 .env 文件，使用环境变量或默认值")

# 旧版 LangGraph 用的模块级 LLM（assign_engineer 仍需）
# 新架构由 ai_agent._get_llm 按需实例化，此处仅供 assign_engineer 使用
_open_code_go_api = os.getenv("open_code_go_api")
_llm = ChatOpenAI(
    model=os.getenv("model") or " ",
    base_url=os.getenv("base_url") or " ",
    api_key=SecretStr(_open_code_go_api or ""),
    temperature=0,
    timeout=_GRAPH_LLM_TIMEOUT,
)


# ==================== 工程师分配 ====================

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
    供反馈升级和定时转派复用。

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
    response = safe_llm_invoke(_llm, messages)

    candidates_names = []
    reason = ""
    try:
        result = json.loads(extract_text(response))
        candidates_names = result.get("candidates", [])
        reason = result.get("reason", "")
    except json.JSONDecodeError:
        logger.warning("LLM 返回解析失败，候选人置空")

    # Step 2: 校验候选人是否真实存在
    matched = [e for e in engineers if e["name"] in candidates_names]

    if not matched:
        # 无候选人 -> 默认第一位
        chosen = engineers[0]
        return chosen["name"], f"无匹配工程师，默认分配（{reason}）"

    # Step 3: 优先在岗，全部不在岗则用不在岗的（方案 B）
    available_pool = [e for e in matched if e.get("available", True)]
    pool = available_pool if available_pool else matched

    # Step 4: 批量计算负载（1 条 SQL 替代 N 条，消除 N+1）
    from . import db_manager

    load_map = db_manager.count_active_tasks_batch()
    for e in pool:
        e["current_load"] = load_map.get(e["name"], 0)

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

    # 批量计算负载（1 条 SQL 替代 N 条，消除 N+1）
    from . import db_manager

    load_map = db_manager.count_active_tasks_batch()
    for e in pool:
        e["current_load"] = load_map.get(e["name"], 0)

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
        logger.warning("未配置 CLIENT_ID / CLIENT_SECRET，无法获取 token")
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
        logger.info("获取 access_token 成功，有效期 %ss", expires_in)
        return token
    except Exception:
        logger.exception("获取 access_token 失败")
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
        logger.warning("user_id 为空，跳过")
        return False

    token = _get_dingtalk_access_token()
    if not token:
        logger.warning("无法获取 access_token，跳过")
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
            logger.info("已向 %s 发送私聊提醒", user_id)
            return True
        else:
            logger.error("发送失败：%s", result)
            return False
    except Exception:
        logger.exception("请求异常")
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
                logger.info("已在群里 @%s(%s)，发送简报", engineer_name, mobile)
            else:
                logger.error("发送失败：%s", resp.text)
        except Exception:
            logger.exception("发送异常")

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
        logger.warning("未配置 dingtalk_user_id，尝试用手机号发送")
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
        logger.warning(
            "%s 未配置 mobile/dingtalk_user_id，跳过私聊提醒", engineer_name
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
        except Exception:
            logger.exception("发送失败")

    if not dingtalk_url and not wechat_url:
        logger.warning(
            "未配置任何 Webhook，跳过通知。应通知 %s 处理：%s",
            engineer_name,
            task.title,
        )
