from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class Difficulty(str, Enum):
    EASY = "easy"
    HARD = "hard"


class Task(BaseModel):
    title: str  # 必填
    description: str  # 必填
    submitted_by: str = ""  # 可选，默认空
    difficulty: Difficulty = Field(default=Difficulty.EASY)


class Engineer(BaseModel):
    name: str  # 必填
    skills: list[str] = Field(default_factory=list)
    current_load: int = 0 #  当前手上有几个任务
    available: bool = True  # 是否在岗


class AgentState(BaseModel):
    task: Optional[Task] = None
    difficulty: Difficulty | None = None
    knowledge_context: str = ""
    final_response: str = ""
    assigned_engineer: str = ""
