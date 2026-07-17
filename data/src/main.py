"""
FastAPI 入口 -- 统一 API。

POST /api/v1/message 为统一入口（预处理 -> AI -> 后处理）。
"""

import asyncio
import json
import logging
import os

os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
os.environ.setdefault("HF_HUB_OFFLINE", "0")
import threading
from pathlib import Path

import uvicorn
from fastapi import Depends, FastAPI
from starlette.concurrency import run_in_threadpool

from . import db_manager
from .auth import verify_api_key
from .database import (
    create_database_if_not_exists,
    init_db,
    migrate_difficulty_values,
    migrate_engineers_schema,
)
from .log_config import gen_request_id, set_request_id, setup_logging
from .models import MessageRequest, MessageResponse

# 启动时初始化日志系统（最先执行，后续日志走统一格式）
setup_logging()

logger = logging.getLogger(__name__)

api = FastAPI(title="运维任务分配 Agent")


# ==================== 启动初始化 + 数据迁移 ====================


def _clean_engineer_name(raw_name: str, mobile: str) -> tuple[str, str]:
    """
    清洗工程师姓名：剥离姓名中混入的手机号。
    返回 (清洗后姓名, 手机号)。若姓名含手机号且 mobile 为空，用剥离出的手机号回填。
    """
    import re

    mobile_re = re.compile(r"1[3-9]\d{9}")
    m = mobile_re.search(raw_name or "")
    if m:
        clean_name = mobile_re.sub("", raw_name).strip()
        extracted_mobile = m.group(0)
        final_mobile = mobile or extracted_mobile
        return clean_name, final_mobile
    return (raw_name or "").strip(), mobile or ""


def migrate_engineers_json_to_db():
    """首次启动：将 engineers.json 导入数据库（含姓名手机号清洗）。"""
    json_path = Path(__file__).parent.parent / "engineers.json"

    if db_manager.count_engineers() > 0:
        logger.info("engineers 表已有数据，跳过 JSON 迁移")
        return

    if not json_path.exists():
        logger.info("engineers.json 不存在，跳过")
        return

    try:
        with open(json_path, "r", encoding="utf-8") as f:
            engineers = json.load(f)
        for e in engineers:
            # 清洗：剥离姓名里混入的手机号，避免污染唯一匹配
            clean_name, clean_mobile = _clean_engineer_name(
                e.get("name", ""), e.get("mobile", "")
            )
            e["name"] = clean_name
            e["mobile"] = clean_mobile
            db_manager.create_engineer(e)
        logger.info("已导入 %d 位工程师到数据库", len(engineers))
    except Exception:
        logger.exception("工程师导入失败")


@api.on_event("startup")
def on_startup():
    """应用启动时：建库 -> 建表 -> 迁移工程师数据 -> 启动定时提醒"""
    try:
        create_database_if_not_exists()
        init_db()
        migrate_engineers_schema()
        migrate_difficulty_values()
        migrate_engineers_json_to_db()
    except Exception:
        logger.exception("数据库初始化失败，将以降级模式运行")

    try:
        from .scheduler import start_scheduler

        start_scheduler()
    except Exception:
        logger.exception("定时提醒启动失败")


# ==================== 新架构：统一消息入口 ====================


@api.post("/api/v1/message", response_model=MessageResponse)
async def handle_message(req: MessageRequest, role: str = Depends(verify_api_key)):
    """
    ★ 统一消息入口 -- 所有消息源（钉钉/API/Web）统一走此接口。

    混合架构：预处理 -> 路由分流（确定性/简单/Agent）-> 后处理
    需要 service 角色 API Key。
    """
    from .postprocess import postprocess
    from .preprocess import preprocess
    from .router import route

    # 生成请求级 ID，透传到整个调用链（contextvars + 线程池自动传播）
    request_id = gen_request_id()
    set_request_id(request_id)

    logger.info("收到消息 来源=%s 发送者=%s", req.source, req.sender_name)
    logger.debug("消息内容：%s", req.content[:80])

    # ① 预处理：脱敏 + 意图检测 + 复杂度检测
    try:
        pre = await run_in_threadpool(preprocess, req.content)
    except Exception:
        logger.exception("预处理失败")
        return MessageResponse(response="处理出错，请重试。")

    # ② 混合路由：按意图/复杂度分流处理
    try:
        result = await run_in_threadpool(
            route, pre, req.sender_name, req.sender_id
        )
    except Exception:
        logger.exception("路由处理失败")
        return MessageResponse(
            intent=pre["intent"],
            complexity=pre["complexity"],
            response="处理出错，请联系 IT 工程师。",
        )

    intent = result.get("intent", pre["intent"])
    complexity = result.get("complexity", pre["complexity"])
    model_used = result.get("model_used", "")
    ai_response = result["response"]

    # ③ 后处理：仅对需要存库的走（简单报障/Agent报障）
    task_no = ""
    memory_saved = False
    response = ai_response

    if result.get("needs_postprocess"):
        try:
            post = await run_in_threadpool(
                postprocess,
                raw_query=req.content,
                ai_response=ai_response,
                intent=intent,
                complexity=complexity,
                model_used=model_used,
                sender_name=req.sender_name,
                sender_id=req.sender_id,
                assigned_engineer=result.get("assigned_engineer", ""),
            )
            task_no = post["task_no"]
            response = post["response"]

            # 摘要异步化：不阻塞响应，丢线程池后台执行
            task_id = post.get("task_id")
            if task_id is not None:
                from .postprocess import summarize_and_vectorize_async

                asyncio.ensure_future(
                    run_in_threadpool(
                        summarize_and_vectorize_async,
                        safe_query=post.get("safe_query", ""),
                        safe_response=response,
                        task_id=task_id,
                        intent=intent,
                        complexity=complexity,
                        model_used=model_used,
                    )
                )
        except Exception:
            logger.exception("后处理失败")

    return MessageResponse(
        intent=intent,
        complexity=complexity,
        model_used=model_used,
        response=response,
        task_no=task_no,
        memory_saved=memory_saved,
    )


# ==================== 管理接口 ====================


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.get("/tasks")
async def list_tasks(limit: int = 20, role: str = Depends(verify_api_key)):
    try:
        return {"tasks": db_manager.list_recent_tasks(limit)}
    except Exception as e:
        return {"error": str(e), "tasks": []}


@api.get("/engineers")
async def list_engineers(role: str = Depends(verify_api_key)):
    try:
        engineers = db_manager.load_engineers_from_db()
        # 批量计算负载（1 条 SQL 替代 N 条，消除 N+1）
        load_map = db_manager.count_active_tasks_batch()
        for e in engineers:
            e["current_load"] = load_map.get(e["name"], 0)
        return {"engineers": engineers}
    except Exception as e:
        return {"error": str(e), "engineers": []}


@api.get("/memories")
async def list_memories(limit: int = 20, role: str = Depends(verify_api_key)):
    """查询最近的交互记忆（调试用）"""
    try:
        return {"memories": db_manager.list_memories(limit)}
    except Exception as e:
        return {"error": str(e), "memories": []}


# ========== 启动方式 ==========
if __name__ == "__main__":
    from .dingtalk_stream import start_stream_bot

    enable_dingtalk = bool(os.getenv("DINGTALK_CLIENT_ID", ""))

    if enable_dingtalk:
        logger.info("在后台线程启动钉钉 Stream 连接...")
        thread = threading.Thread(
            target=start_stream_bot,
            daemon=True,
        )
        thread.start()

    uvicorn.run("src.main:api", host="0.0.0.0", port=8000, reload=False)
