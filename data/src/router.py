"""
混合路由 -- 预处理后按意图/复杂度分流。

核心原则：能确定的用规则，不确定的才交给 AI。

确定性流程（快、省、可控）：
- 闲聊 -> 快速 LLM 回复（max_tokens=200）
- 反馈 -> 复用 feedback.py 逻辑（升级/催办/关闭）
- 查询状态 -> 查数据库返回

简单报障（一步到位）：
- simple + report_issue -> 知识库检索 + 单次 LLM 生成

Agent 流程（灵活）：
- medium/hard + report_issue -> AI + 工具调用（自主决策）
- request_human -> AI + 工具调用（分配工程师）
"""

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from . import db_manager
from .config import (
    LLM_API_KEY,
    LLM_BASE_URL,
    LLM_REQUEST_TIMEOUT,
    MODEL_ROUTING,
)
from .llm_utils import safe_llm_invoke

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


def _ok(response, model_used="", needs_postprocess=False, intent="", complexity="", assigned_engineer=""):
    """构造统一返回格式"""
    return {
        "response": response,
        "model_used": model_used,
        "needs_postprocess": needs_postprocess,
        "intent": intent,
        "complexity": complexity,
        "assigned_engineer": assigned_engineer,
    }


# ==================== 主路由 ====================


def route(preprocess_result: dict, sender_name: str, sender_id: str) -> dict:
    """
    主路由：预处理结果 -> 分流处理 -> 返回统一格式。

    返回: {response, model_used, needs_postprocess, intent, complexity}
    """
    intent = preprocess_result["intent"]
    complexity = preprocess_result["complexity"]
    desensitized = preprocess_result["desensitized"]
    raw_content = preprocess_result["raw_content"]

    # ① 闲聊 -> 快速 LLM 回复
    if intent == "casual_chat":
        logger.info("闲聊 -> 固定流程")
        return _handle_casual_chat(desensitized, intent, complexity)

    # ② 反馈 -> 复用 feedback.py
    if intent in ("feedback_resolved", "feedback_unresolved"):
        result = _handle_feedback(
            sender_name, sender_id, raw_content, intent, complexity
        )
        if result:
            return result
        # feedback 返回 None -> 无 active 任务 -> 当新问题处理
        from .preprocess import detect_complexity

        intent = "report_issue"
        complexity = detect_complexity(raw_content, "report_issue")
        logger.info("反馈无 active 任务 -> 转为报障 (complexity=%s)", complexity)

    # ③ 查询状态 -> 查数据库
    if intent == "query_status":
        logger.info("查询状态 -> 固定流程")
        return _handle_query_status(sender_id, intent, complexity)

    # ④ 报障/转人工 -> 按复杂度分流
    if intent in ("report_issue", "request_human"):
        if complexity == "simple" and intent == "report_issue":
            # 简单报障 -> 知识库 + 单次 LLM（一步到位）
            logger.info("简单报障 -> 固定流程（知识库+单次LLM）")
            return _handle_simple_report(
                desensitized, sender_name, sender_id, intent, complexity
            )
        else:
            # medium/hard 或转人工 -> Agent + 工具
            logger.info("%s报障 -> Agent 流程（工具调用）", complexity)
            return _handle_agent(
                preprocess_result, sender_name, sender_id, intent, complexity
            )

    # 默认 -> Agent
    return _handle_agent(preprocess_result, sender_name, sender_id, intent, complexity)


# ==================== 确定性流程处理器 ====================


def _handle_casual_chat(desensitized: str, intent: str, complexity: str) -> dict:
    """闲聊 -> 快速 LLM 回复"""
    try:
        config = MODEL_ROUTING["simple"]
        llm = ChatOpenAI(
            model=config["model"],
            base_url=LLM_BASE_URL,
            api_key=SecretStr(LLM_API_KEY or ""),
            temperature=0.3,
            model_kwargs={"max_tokens": 200},
            timeout=LLM_REQUEST_TIMEOUT,
        )
        response = safe_llm_invoke(
            llm,
            [
                SystemMessage(
                    content="你是公司IT运维助手，用户在闲聊。请友好简短回复，不超过2句话。"
                ),
                HumanMessage(content=desensitized),
            ],
        )
        reply = _extract_text(response)
        return _ok(
            reply,
            config["model"],
            needs_postprocess=False,
            intent=intent,
            complexity=complexity,
        )
    except Exception:
        logger.exception("闲聊 LLM 失败")
        return _ok(
            "你好！我是运维助手，有什么可以帮您的？",
            intent=intent,
            complexity=complexity,
        )


def _handle_feedback(
    sender_name: str, sender_id: str, raw_content: str, intent: str, complexity: str
) -> dict | None:
    """
    反馈 -> 复用 feedback.py 逻辑。
    返回 None 表示无 active 任务，调用方应当新问题处理。
    """
    try:
        from . import feedback

        result = feedback.handle_message(sender_name, sender_id, raw_content)
        if result and result.get("is_feedback"):
            return _ok(
                result["reply"],
                needs_postprocess=False,
                intent=intent,
                complexity=complexity,
            )
        return None
    except Exception:
        logger.exception("反馈处理异常")
        return None


def _handle_query_status(sender_id: str, intent: str, complexity: str) -> dict:
    """查询状态 -> 查数据库返回"""
    try:
        if not sender_id:
            return _ok(
                "无法查询任务状态，缺少用户标识。", intent=intent, complexity=complexity
            )

        active_task = db_manager.get_user_active_task(sender_id)
        if active_task:
            status_map = {
                "auto_answered": "自动回答中（等待您的反馈）",
                "assigned": f"已分配工程师 {active_task['assigned_engineer']}",
            }
            status_text = status_map.get(active_task["status"], active_task["status"])
            reply = (
                f"📋 您的任务状态：\n\n"
                f"- 编号：{active_task['task_no']}\n"
                f"- 标题：{active_task['title']}\n"
                f"- 状态：{status_text}\n"
                f"- 提交时间：{active_task['created_at']}"
            )
        else:
            reply = "您当前没有进行中的任务。"

        return _ok(reply, intent=intent, complexity=complexity)
    except Exception as e:
        return _ok(f"查询失败：{e}", intent=intent, complexity=complexity)


# ==================== 简单报障处理器（固定流程） ====================


def _handle_simple_report(
    desensitized: str, sender_name: str, sender_id: str, intent: str, complexity: str
) -> dict:
    """
    简单报障 -> 知识库检索 + 单次 LLM 生成（固定流程，一步到位）。

    不走 Agent 工具调用，直接检索知识库 + 一次 LLM 调用。
    延迟低、成本固定、可预测。
    """
    from .tools import retrieve_knowledge

    config = MODEL_ROUTING["simple"]
    model_name = config["model"]

    # 1. 检索知识库
    knowledge = retrieve_knowledge(desensitized, top_k=3)

    # 2. 检索历史记忆
    memory_text = ""
    try:
        from . import memory as memory_module

        memory_text = memory_module.search_memory(desensitized)
    except Exception:
        pass

    # 3. 构建 prompt
    context_parts = []
    if knowledge and "未找到" not in knowledge and "为空" not in knowledge:
        context_parts.append(f"知识库参考内容：\n{knowledge}")
    if memory_text:
        context_parts.append(f"历史记忆：\n{memory_text}")

    context = "\n\n".join(context_parts) if context_parts else "无参考内容"

    prompt = f"""你是公司的 IT 运维助手。基于下面的参考内容，回答用户的问题。

要求：
1. 用通俗易懂的语言，一步步说明解决方案
2. 如果参考内容中有具体操作步骤，完整列出
3. 如果参考内容不足以解决该问题，诚实告知用户，并建议联系 IT 工程师

{context}"""

    # 4. 单次 LLM 调用
    try:
        llm = ChatOpenAI(
            model=model_name,
            base_url=LLM_BASE_URL,
            api_key=SecretStr(LLM_API_KEY or ""),
            temperature=0,
            model_kwargs={"max_tokens": config["max_tokens"]},
            timeout=LLM_REQUEST_TIMEOUT,
        )
        response = safe_llm_invoke(
            llm,
            [
                SystemMessage(content=prompt),
                HumanMessage(content=desensitized),
            ],
        )
        answer = _extract_text(response)
    except Exception:
        logger.exception("简单报障 LLM 失败")
        answer = "处理出错，请稍后重试或联系 IT 工程师。"

    return _ok(
        answer, model_name, needs_postprocess=True, intent=intent, complexity=complexity
    )


# ==================== Agent 流程处理器 ====================


def _handle_agent(
    preprocess_result: dict,
    sender_name: str,
    sender_id: str,
    intent: str,
    complexity: str,
) -> dict:
    """
    Agent 流程 -> AI + 工具调用（灵活，适用于 medium/hard）。

    复用 ai_agent.ai_process()，AI 自主决定是否调用工具。
    """
    from .ai_agent import ai_process

    try:
        ai_result = ai_process(
            desensitized_content=preprocess_result["desensitized"],
            intent=intent,
            complexity=complexity,
            sender_id=sender_id,
        )
        return _ok(
            ai_result["response"],
            ai_result["model_used"],
            needs_postprocess=True,
            intent=intent,
            complexity=complexity,
            assigned_engineer=ai_result.get("assigned_engineer", ""),
        )
    except Exception:
        logger.exception("Agent 处理失败")
        return _ok(
            "处理出错，请联系 IT 工程师。",
            needs_postprocess=False,
            intent=intent,
            complexity=complexity,
        )
