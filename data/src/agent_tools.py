"""
AI 工具定义 -- 单 agent 可调用的内置工具。

不做复杂多 agent，只给 AI 配置基础工具：
- get_current_time：时间工具
- search_knowledge：检索知识库（skill 文档）
- search_memory：检索历史交互记忆
- assign_engineer：分配工程师（负载均衡）
- MCP：预留扩展点
"""

from datetime import datetime

from langchain_core.tools import tool

from . import db_manager
from .config import MEMORY_ENABLED

# ==================== 时间工具 ====================


@tool
def get_current_time() -> str:
    """获取当前日期和时间"""
    now = datetime.now()
    return f"当前时间：{now.strftime('%Y-%m-%d %H:%M:%S')}（{now.strftime('%A')}）"


# ==================== 知识库检索工具 ====================


@tool
def search_knowledge(query: str) -> str:
    """
    检索运维知识库，获取与问题相关的解决方案文档。
    参数 query: 要检索的问题关键词。
    """
    try:
        from .tools import retrieve_knowledge

        result = retrieve_knowledge(query, top_k=3)
        if result and "未找到" not in result and "为空" not in result:
            return result
        return "知识库中未找到相关内容"
    except Exception as e:
        return f"知识库检索失败：{e}"


# ==================== 记忆检索工具 ====================


@tool
def search_memory(query: str) -> str:
    """
    检索历史交互记忆，查找类似的已解决问题。
    参数 query: 要检索的问题关键词。
    """
    if not MEMORY_ENABLED:
        return "记忆功能未启用"
    try:
        from . import memory as memory_module

        result = memory_module.search_memory(query)
        if result:
            return f"找到以下历史记忆：\n{result}"
        return "无相关历史记忆"
    except Exception as e:
        return f"记忆检索失败：{e}"


# ==================== 工程师分配工具 ====================


@tool
def assign_engineer(candidates: str, task_title: str, task_description: str) -> str:
    """
    分配工程师处理任务（负载均衡算法选人）。
    参数 candidates: 候选工程师姓名，逗号分隔（如 "张三,李四"）
    参数 task_title: 任务标题
    参数 task_description: 任务描述
    """
    try:
        from .graph import assign_engineer_by_algorithm

        # 解析候选人
        names = [n.strip() for n in candidates.split(",") if n.strip()]
        if not names:
            return "未提供候选人，请先检索工程师信息确认候选人"

        # 纯算法选人（0 次 LLM）
        chosen, reason = assign_engineer_by_algorithm(names)

        if not chosen:
            return f"分配失败：{reason}"

        return f"已分配工程师：{chosen}。原因：{reason}"
    except Exception as e:
        return f"工程师分配失败：{e}"


# ==================== 任务状态查询工具 ====================


@tool
def query_user_tasks(sender_id: str) -> str:
    """
    查询用户最近的任务状态。
    参数 sender_id: 用户的钉钉 ID 或唯一标识。
    """
    try:
        if not sender_id:
            return "无法查询：缺少用户标识"

        active_task = db_manager.get_user_active_task(sender_id)
        if active_task:
            return (
                f"您最近的任务：{active_task['task_no']} - {active_task['title']}\n"
                f"状态：{active_task['status']}"
            )
        return "您当前没有进行中的任务"
    except Exception as e:
        return f"任务查询失败：{e}"


# ==================== 工具集合 ====================


def get_all_tools():
    """获取全部工具列表（供 AI 处理层绑定）"""
    return [
        get_current_time,
        search_knowledge,
        search_memory,
        assign_engineer,
        query_user_tasks,
    ]


def get_basic_tools():
    """获取基础工具（simple/medium 复杂度用，不含工程师分配）"""
    return [
        get_current_time,
        search_knowledge,
        search_memory,
    ]
