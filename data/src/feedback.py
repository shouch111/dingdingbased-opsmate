"""
反馈识别与处理 —— 反馈闭环的核心模块。

职责：
1. 识别钉钉消息发送者身份（工程师 or 普通用户）
2. 检测消息中的反馈意图（已解决 / 未解决）
3. 根据身份 + 意图 + 任务状态，执行对应的反馈处理逻辑

反馈处理不经过 LangGraph 主流程，直接操作数据库 + 复用 assign_engineer()。
"""

from typing import Optional

from . import db_manager

# ==================== 关键词定义 ====================

# 用户"已解决"反馈关键词
USER_RESOLVED_KEYWORDS = [
    "解决了",
    "可以了",
    "好了",
    "没问题了",
    "搞定了",
    "已恢复",
    "恢复正常",
    "弄好了",
    "处理好了",
]

# 用户"未解决"反馈关键词
USER_UNRESOLVED_KEYWORDS = [
    "没解决",
    "不行",
    "没用",
    "还是不行",
    "搞不定",
    "还是有问题",
    "没反应",
    "不行了",
    "没好",
    "未能解决",
    "还不行",
    "没弄好",
    "还是没好",
]

# 工程师"已解决"关键词
ENGINEER_RESOLVED_KEYWORDS = [
    "已解决",
    "解决了",
    "搞定",
    "完成",
    "done",
    "resolved",
    "已处理",
    "处理完成",
    "已修复",
]


# ==================== 身份识别 ====================


def identify_sender(sender_nick: str, sender_id: str) -> tuple[str, Optional[dict]]:
    """
    识别发送者身份：是工程师还是普通用户。

    返回:
        ('engineer', engineer_dict)  — 是工程师
        ('user', None)               — 是普通用户
    """
    if not sender_nick:
        return ("user", None)

    try:
        engineer = db_manager.get_engineer_by_name(sender_nick)
        if engineer:
            return ("engineer", engineer)
    except Exception as e:
        print(f"[feedback] 查询工程师身份失败：{e}")

    return ("user", None)


# ==================== 反馈意图检测 ====================


def detect_feedback_intent(text: str, role: str) -> Optional[str]:
    """
    检测消息中的反馈意图。

    参数:
        text: 消息文本
        role: 'engineer' 或 'user'

    返回:
        'resolved'   — 表示已解决
        'unresolved' — 表示未解决
        None         — 不是反馈消息（可能是新任务或追问）
    """
    if not text:
        return None

    text_lower = text.lower()

    if role == "engineer":
        # 工程师只检测"已解决"关键词
        for kw in ENGINEER_RESOLVED_KEYWORDS:
            if kw.lower() in text_lower:
                return "resolved"
        return None

    # 普通用户：先检测"未解决"（优先级高于"已解决"，避免"没解决"被"解决"误判）
    for kw in USER_UNRESOLVED_KEYWORDS:
        if kw in text:
            return "unresolved"

    for kw in USER_RESOLVED_KEYWORDS:
        if kw in text:
            return "resolved"

    return None


# ==================== 反馈处理主入口 ====================


def handle_message(
    sender_nick: str,
    sender_id: str,
    text: str,
) -> Optional[dict]:
    """
    钉钉消息反馈处理主入口。

    判断消息是反馈还是新任务：
    - 是反馈 → 执行反馈处理，返回 {reply, is_feedback: True}
    - 不是反馈 → 返回 None（调用方应走新任务流程）

    返回:
        {"is_feedback": True, "reply": "回复文本"} — 已作为反馈处理
        None — 不是反馈，走新任务流程
    """
    role, engineer_info = identify_sender(sender_nick, sender_id)

    # ---------- 工程师消息 ----------
    if role == "engineer":
        return _handle_engineer_message(engineer_info, text)

    # ---------- 用户消息 ----------
    return _handle_user_message(sender_nick, sender_id, text)


# ==================== 工程师消息处理 ====================


def _handle_engineer_message(
    engineer_info: Optional[dict], text: str
) -> Optional[dict]:
    """处理工程师发送的消息"""
    if not engineer_info:
        return None

    engineer_name = engineer_info["name"]

    # 查该工程师是否有进行中的任务
    active_task = db_manager.get_engineer_active_task(engineer_name)

    if not active_task:
        # 没有进行中任务 → 当作新任务（工程师也可以报障）
        return None

    # 有进行中任务 → 检测是否"已解决"
    intent = detect_feedback_intent(text, "engineer")

    if intent == "resolved":
        return _handle_engineer_resolved(active_task, engineer_name)

    # 不是"已解决"关键词 → 当作补充信息，不改状态
    return {
        "is_feedback": True,
        "reply": f"收到您的补充信息。任务 {active_task['task_no']} 仍在处理中，完成后请回复「已解决」。",
    }


def _handle_engineer_resolved(task: dict, engineer_name: str) -> dict:
    """工程师回复"已解决" → 标记 resolved + 通知用户"""
    # 更新任务状态
    db_manager.update_task_status(task["id"], "resolved")
    db_manager.create_feedback(task["id"], "resolved", engineer_name)

    # 通知提交人（如果有钉钉 ID）
    _notify_submitter(task, engineer_name)

    reply = f"✅ 任务 {task['task_no']} 已标记为已解决，已通知提交人 {task['submitted_by']}。"
    return {"is_feedback": True, "reply": reply}


def _notify_submitter(task: dict, engineer_name: str):
    """通知提交人任务已解决"""
    submitter_id = task.get("submitter_id", "")
    if not submitter_id:
        return  # API 提交的任务无钉钉 ID，无法私聊通知

    try:
        from .graph import _send_dingtalk_direct_message

        title = f"✅ 任务 {task['task_no']} 已处理完成"
        text = f"""## ✅ 您的任务已处理完成

> 任务编号：**{task["task_no"]}**
> 任务：{task["title"]}
> 处理人：**{engineer_name}**

您的运维问题已由工程师 {engineer_name} 处理完成。
如仍有问题，请回复说明。"""
        _send_dingtalk_direct_message(submitter_id, title, text)
    except Exception as e:
        print(f"[feedback] 通知提交人失败：{e}")


# ==================== 用户消息处理 ====================


def _handle_user_message(sender_nick: str, sender_id: str, text: str) -> Optional[dict]:
    """处理普通用户发送的消息"""
    # 查用户是否有 active 任务（方案A：只追踪最近一个）
    active_task = db_manager.get_user_active_task(sender_id)

    if not active_task:
        # 没有 active 任务 → 新任务
        return None

    # 有 active 任务 → 检测反馈意图
    intent = detect_feedback_intent(text, "user")

    if intent is None:
        # 不是反馈关键词 → 当作追问（第一阶段：走新任务流程）
        return None

    if intent == "resolved":
        return _handle_user_resolved(active_task, sender_nick)

    # intent == "unresolved"
    return _handle_user_unresolved(active_task, sender_nick)


def _handle_user_resolved(task: dict, sender_nick: str) -> dict:
    """用户反馈"已解决" → 标记 resolved"""
    db_manager.update_task_status(task["id"], "resolved")
    db_manager.create_feedback(task["id"], "resolved", sender_nick)

    reply = f"好的，已为您关闭任务 {task['task_no']}，感谢反馈！👍"
    return {"is_feedback": True, "reply": reply}


def _handle_user_unresolved(task: dict, sender_nick: str) -> Optional[dict]:
    """用户反馈"未解决" → 根据当前状态处理"""
    current_status = task["status"]

    if current_status == "auto_answered":
        # 自动回答未解决 → 升级分配工程师
        return _handle_escalation(task, sender_nick)

    elif current_status == "assigned":
        # 已分配工程师但未解决 → 重新催办
        return _handle_re_escalation(task, sender_nick)

    # 其他状态（如 resolved），当作新任务
    return None


def _handle_escalation(task: dict, sender_nick: str) -> dict:
    """
    simple 任务升级：auto_answered -> assigned
    调用 assign_engineer() 分配工程师，通知工程师，回复用户。
    """
    from .graph import assign_engineer
    from .models import Task as TaskModel

    # 构造 Task 对象供 assign_engineer 使用
    task_obj = TaskModel(
        title=task["title"],
        description=task["description"],
        submitted_by=task["submitted_by"],
    )

    # 调用负载均衡算法分配工程师
    engineer_name, reason = assign_engineer(task_obj)

    if not engineer_name:
        reply = "❌ 自动回答未能解决您的问题，但当前暂无可用工程师，请联系 IT 主管。"
        return {"is_feedback": True, "reply": reply}

    # 更新任务：绑定工程师 + 状态改 assigned
    db_manager.assign_engineer_to_task(task["id"], engineer_name)
    db_manager.create_feedback(task["id"], "unresolved", sender_nick)

    # 通知工程师
    try:
        from .graph import _notify_engineer

        _notify_engineer(engineer_name, task_obj)
    except Exception as e:
        print(f"[feedback] 升级通知工程师失败：{e}")

    reply = (
        f"自动回答未能解决您的问题，已为您转给工程师 **{engineer_name}**，请稍候。\n"
        f"（任务编号：{task['task_no']}）"
    )
    return {"is_feedback": True, "reply": reply}


def _handle_re_escalation(task: dict, sender_nick: str) -> dict:
    """
    assigned 状态下用户再次反馈未解决 → 重新催办工程师（状态不变）
    """
    engineer_name = task.get("assigned_engineer", "")
    db_manager.create_feedback(task["id"], "unresolved", sender_nick)

    # 重新通知工程师
    try:
        from . import db_manager as _dbm
        from .graph import _send_dingtalk_direct_message

        engineer = _dbm.get_engineer_by_name(engineer_name)
        dingtalk_user_id = engineer.get("dingtalk_user_id", "") if engineer else ""

        if dingtalk_user_id:
            title = f"⏰ 用户催办：任务 {task['task_no']}"
            text = f"""## ⏰ 用户反馈任务仍未解决

> 任务编号：**{task["task_no"]}**
> 任务：{task["title"]}
> 提交人：{task["submitted_by"]}

用户反馈该问题仍未解决，请尽快跟进处理。
处理完成后请回复「已解决」。"""
            _send_dingtalk_direct_message(dingtalk_user_id, title, text)
    except Exception as e:
        print(f"[feedback] 催办通知失败：{e}")

    reply = (
        f"已再次通知工程师 **{engineer_name}**，请稍候。\n"
        f"（任务编号：{task['task_no']}）"
    )
    return {"is_feedback": True, "reply": reply}
