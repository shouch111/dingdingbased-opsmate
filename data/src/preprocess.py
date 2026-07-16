"""
预处理层 -- 脱敏 + 意图检测 + 复杂度检测。

在 AI 调用前完成，全部用确定性规则 + 轻量 LLM 兜底，不走主 LLM。
"""

import re

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from .config import (
    INTENT_LLM_FALLBACK,
    LLM_API_KEY,
    LLM_BASE_URL,
    LLM_REQUEST_TIMEOUT,
)

import logging

logger = logging.getLogger(__name__)

# ==================== 辅助函数 ====================


def _extract_text(response) -> str:
    """从 LLM 响应中安全提取文本"""
    content = response.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
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


# ==================== 脱敏规则 ====================

DESENSITIZE_PATTERNS = [
    # 手机号
    (re.compile(r"1[3-9]\d{9}"), "[PHONE]"),
    # IP 地址
    (re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"), "[IP]"),
    # 邮箱
    (re.compile(r"[\w.-]+@[\w.-]+\.\w+"), "[EMAIL]"),
    # 身份证
    (re.compile(r"\d{17}[\dXx]"), "[IDCARD]"),
    # 密码
    (re.compile(r"(?<=密码[是为：:\s])\S+"), "[MASKED]"),
]


def desensitize(text: str) -> str:
    """对文本脱敏：手机号/IP/邮箱/身份证/密码 -> 占位符"""
    if not text:
        return ""
    result = text
    for pattern, replacement in DESENSITIZE_PATTERNS:
        result = pattern.sub(replacement, result)
    return result


# ==================== 意图检测 ====================

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
    "hello",
    "hi",
]

FEEDBACK_RESOLVED_KEYWORDS = [
    "解决了",
    "可以了",
    "好了",
    "没问题了",
    "搞定了",
    "已恢复",
    "恢复正常",
    "弄好了",
    "处理好了",
]

FEEDBACK_UNRESOLVED_KEYWORDS = [
    "没解决",
    "不行",
    "没用",
    "还是不行",
    "搞不定",
    "还是有问题",
    "没反应",
    "不行了",
    "没好",
    "未能解决",
    "还不行",
    "没弄好",
    "还是没好",
]

REQUEST_HUMAN_KEYWORDS = [
    "IT协助",
    "IT支持",
    "技术支持",
    "人工处理",
    "需要工程师",
    "紧急",
    "求助",
    "找IT",
    "叫IT",
    "需要IT",
    "远程协助",
    "上门",
    "现场",
    "人工介入",
    "需要人",
    "崩溃",
    "业务中断",
]

QUERY_STATUS_KEYWORDS = [
    "我的任务",
    "任务进度",
    "查一下",
    "任务状态",
    "我的工单",
]


def detect_intent(text: str) -> tuple[str, float]:
    """
    意图检测：规则优先，未命中走轻量 LLM。
    返回 (intent, confidence)
    """
    if not text:
        return ("report_issue", 0.5)

    text_lower = text.lower()

    # 1. 闲聊
    for kw in CASUAL_PATTERNS:
        if kw.lower() in text_lower:
            return ("casual_chat", 0.95)

    # 2. 反馈（先检测未解决，避免"没解决"被"解决"误判）
    for kw in FEEDBACK_UNRESOLVED_KEYWORDS:
        if kw in text:
            return ("feedback_unresolved", 0.95)

    for kw in FEEDBACK_RESOLVED_KEYWORDS:
        if kw in text:
            return ("feedback_resolved", 0.95)

    # 3. 转人工
    for kw in REQUEST_HUMAN_KEYWORDS:
        if kw in text:
            return ("request_human", 0.95)

    # 4. 查询状态
    for kw in QUERY_STATUS_KEYWORDS:
        if kw in text:
            return ("query_status", 0.90)

    # 5. 规则未命中 -> LLM 兜底
    if INTENT_LLM_FALLBACK:
        return _llm_detect_intent(text)

    # 6. 默认报障
    return ("report_issue", 0.5)


def _llm_detect_intent(text: str) -> tuple[str, float]:
    """轻量 LLM 意图分类（max_tokens=20，成本极低）"""
    try:
        llm = ChatOpenAI(
            model="deepseek-chat",
            base_url=LLM_BASE_URL,
            api_key=SecretStr(LLM_API_KEY or ""),
            temperature=0,
            model_kwargs={"max_tokens": 20},
            timeout=LLM_REQUEST_TIMEOUT,
        )
        prompt = """判断用户消息的意图，只回复一个词：
- report_issue: 报告IT故障
- casual_chat: 闲聊问候
- feedback_resolved: 反馈问题已解决
- feedback_unresolved: 反馈问题未解决
- request_human: 要求人工处理
- query_status: 查询任务状态

只回复上述一个词，不要其他内容。"""

        response = llm.invoke(
            [
                SystemMessage(content=prompt),
                HumanMessage(content=text[:200]),
            ]
        )
        intent = _extract_text(response).strip().lower()
        valid = {
            "report_issue",
            "casual_chat",
            "feedback_resolved",
            "feedback_unresolved",
            "request_human",
            "query_status",
        }
        if intent in valid:
            return (intent, 0.80)
        return ("report_issue", 0.60)
    except Exception:
        logger.exception("LLM 意图检测失败")
        return ("report_issue", 0.50)


# ==================== 复杂度检测 ====================

HARD_KEYWORDS = [
    "IT协助",
    "IT支持",
    "技术支持",
    "人工处理",
    "需要工程师",
    "紧急",
    "求助",
    "搞不定",
    "远程协助",
    "上门",
    "现场",
    "人工介入",
    "需要人",
    "崩溃",
    "全断了",
    "业务中断",
    "服务器",
    "数据库",
    "交换机",
    "防火墙",
    "虚拟化",
    "Linux系统",
    "安全事件",
]


def detect_complexity(text: str, intent: str = "") -> str:
    """
    复杂度检测：规则优先，未命中走轻量 LLM。
    返回 simple / medium / hard
    """
    if not text:
        return "simple"

    # 闲聊 -> simple
    text_lower = text.lower()
    for kw in CASUAL_PATTERNS:
        if kw.lower() in text_lower:
            return "simple"

    # 反馈/查询 -> simple（不需要复杂处理）
    if intent in (
        "feedback_resolved",
        "feedback_unresolved",
        "casual_chat",
        "query_status",
    ):
        return "simple"

    # 困难关键词 -> hard
    for kw in HARD_KEYWORDS:
        if kw in text:
            return "hard"

    # 规则未命中 -> LLM 判断
    if INTENT_LLM_FALLBACK:
        return _llm_detect_complexity(text)

    # 默认 medium
    return "medium"


def _llm_detect_complexity(text: str) -> str:
    """轻量 LLM 复杂度分类"""
    try:
        llm = ChatOpenAI(
            model="deepseek-chat",
            base_url=LLM_BASE_URL,
            api_key=SecretStr(LLM_API_KEY or ""),
            temperature=0,
            model_kwargs={"max_tokens": 10},
            timeout=LLM_REQUEST_TIMEOUT,
        )
        prompt = """判断IT运维问题的复杂度，只回复一个词：
- simple: 标准桌面问题（打印机/VPN/邮箱/密码/软件安装）
- medium: 需要排查但不算严重（网络慢/软件报错/配置问题）
- hard: 严重故障需人工介入（服务器宕机/数据库异常/安全事件）

只回复 simple/medium/hard 一个词。"""

        response = llm.invoke(
            [
                SystemMessage(content=prompt),
                HumanMessage(content=text[:200]),
            ]
        )
        result = _extract_text(response).strip().lower()
        if result in ("simple", "medium", "hard"):
            return result
        return "medium"
    except Exception:
        logger.exception("LLM 复杂度检测失败")
        return "medium"


# ==================== 预处理主入口 ====================


def preprocess(raw_content: str) -> dict:
    """
    预处理主入口：脱敏 -> 意图检测 -> 复杂度检测。
    返回 {raw_content, desensitized, intent, intent_confidence, complexity}
    """
    # 1. 脱敏
    desensitized = desensitize(raw_content)

    # 2. 意图检测（用原始文本检测，避免脱敏影响关键词）
    intent, confidence = detect_intent(raw_content)

    # 3. 复杂度检测（用原始文本检测）
    complexity = detect_complexity(raw_content, intent)

    logger.info(
        "意图=%s(%d%%) 复杂度=%s 脱敏=%s",
        intent,
        int(confidence * 100),
        complexity,
        "是" if desensitized != raw_content else "否",
    )

    return {
        "raw_content": raw_content,
        "desensitized": desensitized,
        "intent": intent,
        "intent_confidence": confidence,
        "complexity": complexity,
    }
