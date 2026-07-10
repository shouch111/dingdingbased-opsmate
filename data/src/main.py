"""
FastAPI 入口 -- 统一 API + 旧版 /task 兼容。

新架构：POST /api/v1/message 为统一入口（预处理 -> AI -> 后处理）。
旧版 POST /task 保留兼容。
"""

import json
import os

os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
os.environ.setdefault("HF_HUB_OFFLINE", "0")
import threading
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel

from . import db_manager
from .database import create_database_if_not_exists, init_db
from .models import AgentState, MessageRequest, MessageResponse, Task

api = FastAPI(title="运维任务分配 Agent")


# ==================== 旧版数据模型（兼容） ====================


class TaskRequest(BaseModel):
    title: str
    description: str
    submitted_by: str


class TaskResponse(BaseModel):
    status: str
    difficulty: str
    response: str
    assigned_to: str | None = None


# ==================== 启动初始化 + 数据迁移 ====================


def migrate_engineers_json_to_db():
    """首次启动：将 engineers.json 导入数据库。"""
    json_path = Path(__file__).parent.parent / "engineers.json"

    if db_manager.count_engineers() > 0:
        print("[迁移] engineers 表已有数据，跳过 JSON 迁移")
        return

    if not json_path.exists():
        print("[迁移] engineers.json 不存在，跳过")
        return

    try:
        with open(json_path, "r", encoding="utf-8") as f:
            engineers = json.load(f)
        for e in engineers:
            db_manager.create_engineer(e)
        print(f"[迁移] 已导入 {len(engineers)} 位工程师到数据库")
    except Exception as e:
        print(f"[迁移] ❌ 导入失败：{e}")


@api.on_event("startup")
def on_startup():
    """应用启动时：建库 -> 建表 -> 迁移工程师数据 -> 启动定时提醒"""
    try:
        create_database_if_not_exists()
        init_db()
        migrate_engineers_json_to_db()
    except Exception as e:
        print(f"[startup] ⚠️ 数据库初始化失败，将以降级模式运行：{e}")

    try:
        from .scheduler import start_scheduler

        start_scheduler()
    except Exception as e:
        print(f"[startup] ⚠️ 定时提醒启动失败：{e}")


# ==================== 新架构：统一消息入口 ====================


@api.post("/api/v1/message", response_model=MessageResponse)
async def handle_message(req: MessageRequest):
    """
    ★ 统一消息入口 -- 所有消息源（钉钉/API/Web）统一走此接口。

    流程：预处理（脱敏+意图+复杂度）-> AI 处理（模型路由+工具）-> 后处理（入库+记忆）
    """
    from .ai_agent import ai_process
    from .postprocess import postprocess
    from .preprocess import preprocess

    print(f"\n{'=' * 60}")
    print(f"[message] 来源={req.source} 发送者={req.sender_name}")
    print(f"[message] 内容={req.content[:80]}...")
    print(f"{'=' * 60}")

    # ① 预处理：脱敏 + 意图检测 + 复杂度检测
    try:
        pre = preprocess(req.content)
    except Exception as e:
        print(f"[message] 预处理失败：{e}")
        return MessageResponse(response="处理出错，请重试。")

    intent = pre["intent"]
    complexity = pre["complexity"]
    desensitized = pre["desensitized"]

    # ② AI 处理：模型路由 + 意图注入 + 工具调用
    try:
        ai_result = ai_process(
            desensitized_content=desensitized,
            intent=intent,
            complexity=complexity,
            sender_id=req.sender_id,
        )
        ai_response = ai_result["response"]
        model_used = ai_result["model_used"]
    except Exception as e:
        print(f"[message] AI 处理失败：{e}")
        return MessageResponse(
            intent=intent,
            complexity=complexity,
            response="处理出错，请联系 IT 工程师。",
        )

    # ③ 后处理：脱敏入库 + 总结 + 向量化记忆
    try:
        post = postprocess(
            raw_query=req.content,
            ai_response=ai_response,
            intent=intent,
            complexity=complexity,
            model_used=model_used,
            sender_name=req.sender_name,
            sender_id=req.sender_id,
        )
    except Exception as e:
        print(f"[message] 后处理失败：{e}")
        post = {"task_no": "", "memory_saved": False, "response": ai_response}

    return MessageResponse(
        intent=intent,
        complexity=complexity,
        model_used=model_used,
        response=post["response"],
        task_no=post["task_no"],
        memory_saved=post["memory_saved"],
    )


# ==================== 旧版接口（兼容保留） ====================


@api.post("/task", response_model=TaskResponse)
async def handle_task(req: TaskRequest):
    """旧版接口：接收任务，运行 LangGraph 工作流（兼容保留）。"""
    from .graph import agent_app

    initial_state = AgentState(
        task=Task(
            title=req.title,
            description=req.description,
            submitted_by=req.submitted_by,
        ),
        difficulty=None,
        knowledge_context="",
        final_response="",
        assigned_engineer="",
    )

    result = agent_app.invoke(initial_state)

    if result["assigned_engineer"]:
        status = "assigned"
    else:
        status = "auto_answered"

    return TaskResponse(
        status=status,
        difficulty=result["difficulty"],
        response=result["final_response"],
        assigned_to=result.get("assigned_engineer"),
    )


# ==================== 管理接口 ====================


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.get("/tasks")
async def list_tasks(limit: int = 20):
    try:
        return {"tasks": db_manager.list_recent_tasks(limit)}
    except Exception as e:
        return {"error": str(e), "tasks": []}


@api.get("/engineers")
async def list_engineers():
    try:
        engineers = db_manager.load_engineers_from_db()
        for e in engineers:
            e["current_load"] = db_manager.count_active_tasks(e["name"])
        return {"engineers": engineers}
    except Exception as e:
        return {"error": str(e), "engineers": []}


@api.get("/memories")
async def list_memories(limit: int = 20):
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
        print("[启动] 在后台线程启动钉钉 Stream 连接...")
        thread = threading.Thread(
            target=start_stream_bot,
            daemon=True,
        )
        thread.start()

    uvicorn.run("src.main:api", host="0.0.0.0", port=8000, reload=False)
