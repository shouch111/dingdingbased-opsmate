"""
数据结构定义（Pydantic）-- 新架构版。

新增：Intent（意图）、Complexity（复杂度）、统一消息请求/响应。
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class Difficulty(str, Enum):
    SIMPLE = "simple"
    MEDIUM = "medium"
    HARD = "hard"


class Intent(str, Enum):
    """消息意图（预处理层输出）"""

    REPORT_ISSUE = "report_issue"  # 报障
    CASUAL_CHAT = "casual_chat"  # 闲聊
    FEEDBACK_RESOLVED = "feedback_resolved"  # 反馈已解决
    FEEDBACK_UNRESOLVED = "feedback_unresolved"  # 反馈未解决
    REQUEST_HUMAN = "request_human"  # 转人工
    QUERY_STATUS = "query_status"  # 查询任务状态


class Complexity(str, Enum):
    """复杂度（预处理层输出，驱动模型路由）"""

    SIMPLE = "simple"
    MEDIUM = "medium"
    HARD = "hard"


class Task(BaseModel):
    title: str  # 必填
    description: str  # 必填
    submitted_by: str = ""  # 可选，默认空
    difficulty: Difficulty = Field(default=Difficulty.SIMPLE)


class Engineer(BaseModel):
    name: str  # 必填（允许同名）
    staff_id: str = ""  # 员工工号（钉钉 staffId，唯一识别）
    skills: list[str] = Field(default_factory=list)
    mobile: str = ""  # 手机号，用于钉钉群 @ 提及
    dingtalk_user_id: str = ""  # 钉钉 UserId，用于私聊消息推送
    current_load: int = 0  # 当前手上有几个任务
    available: bool = True  # 是否在岗


# ==================== 统一 API 请求/响应（新架构） ====================


class MessageRequest(BaseModel):
    """统一消息入口请求体（POST /api/v1/message）"""

    source: str = "api"  # dingtalk / api / web
    sender_id: str = ""  # 发送者唯一 ID
    sender_name: str = "用户"  # 发送者昵称
    content: str  # 原始消息内容
    metadata: dict = Field(default_factory=dict)


class MessageResponse(BaseModel):
    """统一消息入口响应体"""

    intent: str = ""
    complexity: str = ""
    model_used: str = ""
    response: str = ""
    task_no: str = ""
    memory_saved: bool = False


# ==================== 预处理结果 ====================


class PreprocessResult(BaseModel):
    """预处理层输出"""

    raw_content: str = ""  # 原始内容
    desensitized: str = ""  # 脱敏后内容
    intent: str = "report_issue"  # 意图
    intent_confidence: float = 1.0  # 意图置信度
    complexity: str = "simple"  # 复杂度


# ==================== 兼容旧 AgentState ====================


class AgentState(BaseModel):
    """工作流内部状态（兼容旧 graph.py，逐步废弃）"""

    task: Optional[Task] = None
    difficulty: Difficulty | None = None
    knowledge_context: str = ""
    final_response: str = ""
    assigned_engineer: str = ""
    submitter_id: str = ""  # 提交人钉钉 ID（反馈追踪用）
    task_no: str = ""  # 任务编号（存库后回填）
