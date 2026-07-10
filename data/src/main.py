"""
FastAPI 入口 —— 把 Agent 包装成 Web API。
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
from .graph import agent_app
from .models import AgentState, Task

api = FastAPI(title="运维任务分配 Agent")


# ==================== 数据模型 ====================


class TaskRequest(BaseModel):
    title: str
    description: str
    submitted_by: str


class TaskResponse(BaseModel):
    status: str  # "auto_answered" 或 "assigned"
    difficulty: str
    response: str
    assigned_to: str | None = None


# ==================== 启动初始化 + 数据迁移 ====================


def migrate_engineers_json_to_db():
    """
    首次启动：将 engineers.json 导入数据库。
    表已有数据则跳过，以 DB 数据为准。
    """
    json_path = Path(__file__).parent.parent / "engineers.json"

    # 表已有数据，跳过
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

    # 启动定时提醒调度器
    try:
        from .scheduler import start_scheduler

        start_scheduler()
    except Exception as e:
        print(f"[startup] ⚠️ 定时提醒启动失败：{e}")


# ==================== API 接口 ====================


@api.post("/task", response_model=TaskResponse)
async def handle_task(req: TaskRequest):
    """
    接收任务，运行 Agent 工作流，返回结果。
    """
    # 构造初始状态
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

    # 运行工作流
    result = agent_app.invoke(initial_state)

    # 判断状态
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


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.get("/tasks")
async def list_tasks(limit: int = 20):
    """查询最近任务列表（调试/管理用）"""
    try:
        return {"tasks": db_manager.list_recent_tasks(limit)}
    except Exception as e:
        return {"error": str(e), "tasks": []}


@api.get("/engineers")
async def list_engineers():
    """查询工程师名单（调试/管理用）"""
    try:
        engineers = db_manager.load_engineers_from_db()
        # 动态填充 current_load
        for e in engineers:
            e["current_load"] = db_manager.count_active_tasks(e["name"])
        return {"engineers": engineers}
    except Exception as e:
        return {"error": str(e), "engineers": []}


# ========== 启动方式 ==========
if __name__ == "__main__":
    from .dingtalk_stream import start_stream_bot

    # 是否启用钉钉 Stream
    enable_dingtalk = bool(os.getenv("DINGTALK_CLIENT_ID", ""))

    if enable_dingtalk:
        print("[启动] 在后台线程启动钉钉 Stream 连接...")
        thread = threading.Thread(
            target=start_stream_bot,
            args=(agent_app,),
            daemon=True,  # 主进程退出时自动关闭
        )
        thread.start()

    # FastAPI 主线程
    uvicorn.run("src.main:api", host="0.0.0.0", port=8000, reload=False)
