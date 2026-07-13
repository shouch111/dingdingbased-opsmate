"""
模型路由配置 -- 按复杂度选择不同 AI 模型。

新架构核心：预处理层检测复杂度 -> config 路由模型 -> AI 处理层调用。
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# -------------------- 环境变量加载 --------------------

DATA_DIR = Path(__file__).parent.parent
_env_paths = [
    DATA_DIR / ".env",
    DATA_DIR.parent / ".env",
]
for _p in _env_paths:
    if _p.exists():
        load_dotenv(_p)
        print(f"[config] 已加载环境变量：{_p}")
        break
else:
    load_dotenv()

# -------------------- LLM 基础配置 --------------------

LLM_API_KEY = os.getenv("open_code_go_api", "")
LLM_BASE_URL = os.getenv("base_url", "https://api.deepseek.com")

# -------------------- 模型路由 --------------------
# 按复杂度选择模型：simple/medium/hard -> 不同模型

MODEL_SIMPLE = os.getenv("MODEL_SIMPLE", "deepseek-chat")
MODEL_MEDIUM = os.getenv("MODEL_MEDIUM", "deepseek-chat")
MODEL_HARD = os.getenv("MODEL_HARD", "deepseek-reasoner")

MODEL_ROUTING = {
    "simple": {
        "model": MODEL_SIMPLE,
        "temperature": 0,
        "max_tokens": 1000,
        "tools_enabled": False,  # 简单问题不给工具，直接回答
        "description": "标准桌面问题，快速回答",
    },
    "medium": {
        "model": MODEL_MEDIUM,
        "temperature": 0,
        "max_tokens": 2000,
        "tools_enabled": True,
        "description": "需要工具辅助排查",
    },
    "hard": {
        "model": MODEL_HARD,
        "temperature": 0,
        "max_tokens": 4000,
        "tools_enabled": True,
        "description": "复杂问题，需要深度推理",
    },
}

# -------------------- 预处理配置 --------------------

# 意图检测：规则未命中时是否走 LLM 兜底
INTENT_LLM_FALLBACK = os.getenv("INTENT_LLM_FALLBACK", "true").lower() == "true"

# -------------------- 记忆配置 --------------------

MEMORY_ENABLED = os.getenv("MEMORY_ENABLED", "true").lower() == "true"
MEMORY_SEARCH_TOP_K = int(os.getenv("MEMORY_SEARCH_TOP_K", "3"))

# -------------------- 工具调用限制 --------------------

# AI 工具调用最大轮次（防止死循环）
MAX_TOOL_ROUNDS = 3
