"""
FastAPI 入口 —— 把 Agent 包装成 Web API。
"""

import os
os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
os.environ.setdefault("HF_HUB_OFFLINE", "0")
import threading

import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel

from .graph import agent_app
from .models import AgentState, Task

api = FastAPI(title="运维任务分配 Agent")


class TaskRequest(BaseModel):
    title: str
    description: str
    submitted_by: str


class TaskResponse(BaseModel):
    status: str  # "auto_answered" 或 "assigned"
    difficulty: str
    response: str
    assigned_to: str | None = None


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
