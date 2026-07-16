"""
后处理层 -- 脱敏回答 + 入库 + 总结 + 向量化记忆。

AI 处理完成后执行：
1. 对 AI 回答做二次脱敏（AI 可能引用了敏感信息）
2. 任务存库（脱敏版本）
3. LLM 总结 query + answer
4. 计算向量存入记忆库
"""

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from . import db_manager
from .config import LLM_API_KEY, LLM_BASE_URL, LLM_REQUEST_TIMEOUT
from .llm_utils import safe_llm_invoke
from .preprocess import desensitize

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


# ==================== 后处理主入口 ====================


def postprocess(
    raw_query: str,
    ai_response: str,
    intent: str,
    complexity: str,
    model_used: str,
    sender_name: str,
    sender_id: str,
    assigned_engineer: str = "",
) -> dict:
    """
    后处理主入口：脱敏 -> 入库 -> 总结 -> 向量化。

    assigned_engineer 由 ai_agent 透传（工具执行时写入），
    非空表示已分配工程师 -> status=assigned。
    返回 {task_no, memory_saved, response}
    """
    # 1. 对 AI 回答做二次脱敏
    safe_response = desensitize(ai_response)

    # 2. 脱敏后的 query 也存库
    safe_query = desensitize(raw_query)

    # 3. 判断任务状态（基于结构化标记，不解析 LLM 自然语言）
    if assigned_engineer:
        # 工具已成功分配工程师 -> assigned
        status = "assigned"
    elif intent in ("casual_chat",):
        # 闲聊不入库
        return {
            "task_no": "",
            "memory_saved": False,
            "response": safe_response,
        }
    else:
        status = "auto_answered"

    # difficulty 与 complexity 取值统一（simple/medium/hard），直接使用
    difficulty = complexity or "medium"

    # 4. assigned_engineer 直接用透传的值（不再从自然语言提取）

    # 5. 存库
    task_no = ""
    task_id = None
    try:
        task_dict = db_manager.create_task(
            title=safe_query[:80],
            description=safe_query,
            submitted_by=sender_name,
            submitter_id=sender_id,
            difficulty=difficulty,
            status=status,
            assigned_engineer=assigned_engineer,
            final_response=safe_response,
            intent=intent,
            complexity=complexity,
            model_used=model_used,
            raw_content=safe_query,
        )
        task_no = task_dict.get("task_no", "")
        task_id = task_dict.get("id")
        logger.info("任务已存库：%s（%s）", task_no, status)
    except Exception:
        logger.exception("任务存库失败（不阻断流程）")

    # 6. 总结 + 向量化（异步执行，不阻塞响应）
    # 返回 task_id 供调用方异步触发摘要

    # 7. 回答末尾附任务编号
    if task_no:
        safe_response = f"{safe_response}\n\n---\n📋 任务编号：**{task_no}**"

    return {
        "task_no": task_no,
        "memory_saved": False,
        "response": safe_response,
        "task_id": task_id,
        "safe_query": safe_query,
    }


# ==================== 辅助函数 ====================


def _summarize_and_vectorize(
    query: str,
    answer: str,
    task_id: int,
    intent: str,
    complexity: str,
    model_used: str,
) -> bool:
    """总结 query+answer 并向量化存储到记忆库（同步版本，保留供旧路径调用）"""
    # 1. LLM 生成摘要
    summary = _generate_summary(query, answer)
    if not summary:
        return False

    logger.debug("摘要：%s...", summary[:60])

    # 2. 向量化存储（PostgreSQL memories 表，含 embedding）
    from . import memory as memory_module

    return memory_module.save_memory(
        summary=summary,
        task_id=task_id,
        metadata={"intent": intent, "complexity": complexity},
    )


def summarize_and_vectorize_async(
    safe_query: str,
    safe_response: str,
    task_id: int,
    intent: str,
    complexity: str,
    model_used: str,
):
    """异步摘要+向量化入口（由 main.py 丢线程池后台执行，不阻塞用户响应）"""
    try:
        _summarize_and_vectorize(
            safe_query, safe_response, task_id, intent, complexity, model_used
        )
        logger.info("异步摘要完成 task_id=%d", task_id)
    except Exception:
        logger.exception("异步摘要失败 task_id=%d", task_id)


def _generate_summary(query: str, answer: str) -> str:
    """用 LLM 生成 query+answer 的简短摘要"""
    try:
        llm = ChatOpenAI(
            model="deepseek-chat",
            base_url=LLM_BASE_URL,
            api_key=SecretStr(LLM_API_KEY or ""),
            temperature=0,
            model_kwargs={"max_tokens": 100},
            timeout=LLM_REQUEST_TIMEOUT,
        )

        prompt = """请用一句话总结以下运维问答的核心内容（不超过50字）。
格式：问题简述 -> 解决方案简述

用户问题：
{query}

AI回答：
{answer}

只回复一句话摘要，不要其他内容。"""

        response = safe_llm_invoke(
            llm,
            [
                SystemMessage(
                    content=prompt.format(query=query[:200], answer=answer[:500])
                ),
                HumanMessage(content="请生成摘要"),
            ],
        )

        summary = _extract_text(response).strip()
        return summary if summary else f"{query[:30]} -> {answer[:30]}"
    except Exception:
        logger.exception("LLM 摘要生成失败")
        # 降级：截取前 50 字
        return f"{query[:25]} -> {answer[:25]}"
