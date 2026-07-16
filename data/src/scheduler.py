"""
定时提醒调度器 -- 监控超时未解决的任务，自动提醒工程师或转派。

工作原理：
1. APScheduler 在后台线程每 1 分钟扫描一次 assigned 状态的任务
2. 对每个任务检查距上次提醒是否已超过 REMINDER_INTERVAL_MINUTES（默认 30 分钟）
3. 未超上限 -> 钉钉私聊提醒工程师
4. 达到 REMINDER_MAX_COUNT（默认 3 次）-> 转派给其他工程师（排除当前）
5. 无其他人可转 -> 继续提醒当前工程师 + 通知 IT 群

提醒记录复用 feedbacks 表，feedback_by = "系统提醒|{工程师名}"。
工程师转派后新工程师的提醒计数自然为 0（按名区分），无需额外重置。
"""

import logging
import os
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# -------------------- 环境变量加载 --------------------

DATA_DIR = Path(__file__).parent.parent
_env_paths = [
    DATA_DIR / ".env",
    DATA_DIR.parent / ".env",
]
for _p in _env_paths:
    if _p.exists():
        load_dotenv(_p)
        break
else:
    load_dotenv()

# 提醒间隔（分钟），默认 30
REMINDER_INTERVAL_MINUTES = int(os.getenv("REMINDER_INTERVAL_MINUTES", "30"))
# 最大提醒次数，达到后转派，默认 3
REMINDER_MAX_COUNT = int(os.getenv("REMINDER_MAX_COUNT", "3"))
# 调度器扫描间隔（分钟），固定 1 分钟
CHECK_INTERVAL_MINUTES = 1


# ==================== 核心逻辑 ====================


def check_overdue_tasks():
    """
    扫描所有 assigned 状态的任务，对超时未解决的发送提醒或转派。
    由 APScheduler 定时调用。
    """
    try:
        from . import db_manager

        tasks = db_manager.get_assigned_tasks()
        if not tasks:
            return

        logger.info("扫描到 %d 个进行中任务", len(tasks))

        for task in tasks:
            try:
                _check_single_task(task)
            except Exception:
                logger.exception("任务 %s 检查失败", task.get('task_no', '?'))
    except Exception:
        logger.exception("扫描异常")


def _check_single_task(task: dict):
    """检查单个任务是否需要提醒或转派"""
    from . import db_manager

    task_id = task["id"]
    engineer_name = task.get("assigned_engineer", "")

    if not engineer_name:
        return  # 无分配工程师，跳过

    now = datetime.now()

    # 查已提醒次数
    reminder_count = db_manager.count_reminders(task_id, engineer_name)

    # 查上次提醒时间，无记录则用任务创建时间
    last_reminder = db_manager.get_last_reminder_time(task_id, engineer_name)
    if last_reminder:
        reference_time = last_reminder
    else:
        # 解析任务创建时间
        try:
            reference_time = datetime.strptime(task["created_at"], "%Y-%m-%d %H:%M:%S")
        except (ValueError, KeyError):
            reference_time = now  # 解析失败则立即提醒

    # 未到提醒间隔，跳过
    elapsed_minutes = (now - reference_time).total_seconds() / 60
    if elapsed_minutes < REMINDER_INTERVAL_MINUTES:
        return

    # 达到最大提醒次数 -> 尝试转派
    if reminder_count >= REMINDER_MAX_COUNT:
        _try_reassign(task, engineer_name)
        return

    # 正常提醒
    _send_reminder(task, engineer_name, reminder_count + 1)
    db_manager.create_reminder(task_id, engineer_name)


# ==================== 提醒发送 ====================


def _send_reminder(task: dict, engineer_name: str, count: int):
    """给工程师发送钉钉私聊提醒"""
    from . import db_manager

    engineer = db_manager.get_engineer_by_name(engineer_name)
    if not engineer:
        logger.info("工程师 %s 不存在，跳过提醒", engineer_name)
        return

    dingtalk_user_id = engineer.get("dingtalk_user_id", "")
    if not dingtalk_user_id:
        logger.info("工程师 %s 无钉钉 ID，跳过私聊提醒", engineer_name)
        return

    task_no = task["task_no"]
    title = f"⏰ 提醒：任务 {task_no} 尚未解决（第 {count}/{REMINDER_MAX_COUNT} 次）"
    text = f"""## ⏰ 任务提醒

> 任务编号：**{task_no}**
> 任务：{task["title"]}
> 提交人：{task["submitted_by"]}
> 已分配时间：{task["created_at"]}

该任务已分配给您但尚未解决，请尽快处理。
处理完成后请回复「已解决」。

---
⚠️ 第 {count} 次提醒，共 {REMINDER_MAX_COUNT} 次。
达到上限后将自动转派给其他工程师。"""

    try:
        from .graph import _send_dingtalk_direct_message

        _send_dingtalk_direct_message(dingtalk_user_id, title, text)
        logger.info("已提醒 %s（任务 %s，第 %d 次）", engineer_name, task_no, count)
    except Exception:
        logger.exception("提醒发送失败")


# ==================== 转派逻辑 ====================


def _try_reassign(task: dict, current_engineer: str):
    """
    达到最大提醒次数，尝试转派给其他工程师。
    排除当前工程师；若无可转派对象则继续提醒 + 通知 IT 群。
    """
    from . import db_manager
    from .graph import _notify_engineer, assign_engineer
    from .models import Task as TaskModel

    task_id = task["id"]
    task_no = task["task_no"]

    # 构造 Task 对象供 assign_engineer 使用
    task_obj = TaskModel(
        title=task["title"],
        description=task["description"],
        submitted_by=task["submitted_by"],
    )

    # 尝试转派（排除当前工程师）
    new_engineer, reason = assign_engineer(task_obj, exclude_name=current_engineer)

    if new_engineer:
        # 转派成功
        db_manager.assign_engineer_to_task(task_id, new_engineer)
        logger.info("任务 %s 转派：%s -> %s", task_no, current_engineer, new_engineer)

        # 通知新工程师（钉钉私聊 + 群简报）
        try:
            _notify_engineer(new_engineer, task_obj)
        except Exception:
            logger.exception("转派通知失败")

        # 通知提交人任务已转派
        _notify_submitter_reassign(task, current_engineer, new_engineer)
    else:
        # 无其他人可转派 -> 继续提醒当前工程师 + 通知 IT 群
        logger.warning(
            "任务 %s 已达提醒上限，工程师 %s 未响应且无其他人可转派",
            task_no, current_engineer,
        )
        _send_reminder(task, current_engineer, REMINDER_MAX_COUNT + 1)
        db_manager.create_reminder(task_id, current_engineer)
        _notify_group_no_reassign(task, current_engineer)


def _notify_submitter_reassign(task: dict, old_engineer: str, new_engineer: str):
    """通知提交人任务已转派"""
    submitter_id = task.get("submitter_id", "")
    if not submitter_id:
        return

    try:
        from .graph import _send_dingtalk_direct_message

        title = f"🔄 任务 {task['task_no']} 已转派"
        text = f"""## 🔄 您的任务已转派

> 任务编号：**{task["task_no"]}**
> 任务：{task["title"]}
> 原工程师：{old_engineer}（未响应）
> 新工程师：**{new_engineer}**

因原工程师长时间未响应，已自动转派给 {new_engineer} 处理。"""
        _send_dingtalk_direct_message(submitter_id, title, text)
    except Exception:
        logger.exception("通知提交人转派失败")


def _notify_group_no_reassign(task: dict, engineer_name: str):
    """通知 IT 群：任务超时且无人可转派"""
    import requests

    dingtalk_url = os.getenv("DINGTALK_WEBHOOK", "")
    wechat_url = os.getenv("WECHAT_WEBHOOK", "")

    task_no = task["task_no"]
    message = (
        f"⚠️ 任务 {task_no} 已提醒 {REMINDER_MAX_COUNT} 次未响应，"
        f"工程师 {engineer_name} 未处理且无其他工程师可转派，请人工介入。"
    )

    if dingtalk_url:
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": "⚠️ 任务超时需人工介入",
                "text": f"## ⚠️ 任务超时\n\n{message}",
            },
        }
        try:
            requests.post(dingtalk_url, json=payload, timeout=5)
        except Exception:
            logger.exception("钉钉群通知失败")

    if wechat_url and not dingtalk_url:
        payload = {
            "msgtype": "markdown",
            "markdown": {"content": f"## ⚠️ 任务超时\n\n{message}"},
        }
        try:
            requests.post(wechat_url, json=payload, timeout=5)
        except Exception:
            logger.exception("企微群通知失败")


# ==================== 调度器启动 ====================


_scheduler = None


def start_scheduler():
    """启动后台调度器（在 FastAPI 启动时调用）"""
    global _scheduler

    if _scheduler is not None:
        logger.info("调度器已在运行，跳过")
        return

    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.interval import IntervalTrigger

        _scheduler = BackgroundScheduler(daemon=True)
        _scheduler.add_job(
            check_overdue_tasks,
            trigger=IntervalTrigger(minutes=CHECK_INTERVAL_MINUTES),
            id="check_overdue_tasks",
            replace_existing=True,
        )
        _scheduler.start()
        logger.info(
            "定时提醒已启动（间隔 %d 分钟，上限 %d 次）",
            REMINDER_INTERVAL_MINUTES, REMINDER_MAX_COUNT,
        )
    except Exception:
        logger.exception("调度器启动失败")


def stop_scheduler():
    """停止调度器（可选，进程退出时自动关闭）"""
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("调度器已停止")
