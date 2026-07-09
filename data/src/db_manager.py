"""
数据库 CRUD 操作封装。

所有上层代码（graph.py / feedback.py / dingtalk_stream.py / main.py）
统一通过本模块操作数据库，不直接接触 ORM Session 细节。
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from .database import Engineer, Feedback, SessionLocal, Task

# ==================== 会话管理 ====================


def _get_session() -> Session:
    """获取一个数据库会话（调用方负责 close，或用 _auto_commit 上下文）"""
    return SessionLocal()


def _commit_and_refresh(session: Session, obj):
    """提交并刷新对象，失败时回滚"""
    session.add(obj)
    session.commit()
    session.refresh(obj)
    return obj


# ==================== 工程师相关 ====================


def count_engineers() -> int:
    """工程师总数（迁移时判断表是否为空）"""
    session = _get_session()
    try:
        return session.query(Engineer).count()
    finally:
        session.close()


def create_engineer(data: dict) -> dict:
    """
    新增工程师。
    data 格式与原 engineers.json 一致：{name, skills, mobile, dingtalk_user_id, available}
    返回插入后的字典（含 id）。
    """
    session = _get_session()
    try:
        engineer = Engineer(
            name=data["name"],
            skills=data.get("skills", []),
            mobile=data.get("mobile", ""),
            dingtalk_user_id=data.get("dingtalk_user_id", ""),
            available=data.get("available", True),
        )
        _commit_and_refresh(session, engineer)
        return _engineer_to_dict(engineer)
    finally:
        session.close()


def load_engineers_from_db() -> list[dict]:
    """
    加载全部工程师（返回 list[dict]，与原 load_engineers() 接口兼容）。
    """
    session = _get_session()
    try:
        engineers = session.query(Engineer).order_by(Engineer.id).all()
        return [_engineer_to_dict(e) for e in engineers]
    finally:
        session.close()


def get_engineer_by_name(name: str) -> Optional[dict]:
    """按姓名查工程师"""
    session = _get_session()
    try:
        e = session.query(Engineer).filter(Engineer.name == name).first()
        return _engineer_to_dict(e) if e else None
    finally:
        session.close()


def save_engineer_dingtalk_id(name: str, dingtalk_user_id: str) -> bool:
    """
    更新工程师的钉钉 UserID（替代原 _auto_fill_engineer_id 写 JSON 的逻辑）。
    返回是否更新成功。
    """
    session = _get_session()
    try:
        e = session.query(Engineer).filter(Engineer.name == name).first()
        if not e:
            return False
        if e.dingtalk_user_id:  # 已有值则不覆盖
            return False
        e.dingtalk_user_id = dingtalk_user_id
        session.commit()
        print(f"[db_manager] 🔗 已绑定 {name} → dingtalk_user_id={dingtalk_user_id}")
        return True
    finally:
        session.close()


def update_engineer_availability(name: str, available: bool) -> bool:
    """更新工程师在岗状态"""
    session = _get_session()
    try:
        e = session.query(Engineer).filter(Engineer.name == name).first()
        if not e:
            return False
        e.available = available
        session.commit()
        return True
    finally:
        session.close()


def _engineer_to_dict(e: Engineer) -> dict:
    """ORM 对象转字典（与原 engineers.json 格式一致）"""
    return {
        "name": e.name,
        "skills": e.skills if e.skills else [],
        "mobile": e.mobile or "",
        "dingtalk_user_id": e.dingtalk_user_id or "",
        "current_load": 0,  # 占位，由 count_active_tasks 动态填充
        "available": e.available if e.available is not None else True,
    }


# ==================== 任务相关 ====================


def generate_task_no() -> str:
    """
    生成任务编号 T1001。
    策略：取当前最大 id + 1，补零至 4 位。首次（空表）返回 T1001。
    用 DB 自增 ID 保证唯一，避免并发冲突。
    """
    session = _get_session()
    try:
        max_id = session.query(func.max(Task.id)).scalar()
        next_id = (max_id or 0) + 1
        return f"T{next_id:04d}"
    finally:
        session.close()


def create_task(
    title: str,
    description: str,
    submitted_by: str,
    submitter_id: str,
    difficulty: str,
    status: str,
    assigned_engineer: str = "",
    final_response: str = "",
) -> dict:
    """
    创建任务并返回字典。
    status: 'auto_answered' | 'assigned' | 'resolved'
    """
    session = _get_session()
    try:
        # 生成编号（基于自增 id，先插入拿 id 再回填 task_no）
        task = Task(
            task_no="TEMP",  # 临时占位，提交后用真实 id 更新
            title=title,
            description=description,
            submitted_by=submitted_by,
            submitter_id=submitter_id,
            difficulty=difficulty,
            status=status,
            assigned_engineer=assigned_engineer,
            final_response=final_response,
        )
        _commit_and_refresh(session, task)

        # 用真实 id 生成编号
        task.task_no = f"T{task.id:04d}"
        session.commit()
        session.refresh(task)
        return _task_to_dict(task)
    finally:
        session.close()


def get_task_by_id(task_id: int) -> Optional[dict]:
    """按主键查任务"""
    session = _get_session()
    try:
        task = session.query(Task).filter(Task.id == task_id).first()
        return _task_to_dict(task) if task else None
    finally:
        session.close()


def get_task_by_no(task_no: str) -> Optional[dict]:
    """按任务编号查任务"""
    session = _get_session()
    try:
        task = session.query(Task).filter(Task.task_no == task_no).first()
        return _task_to_dict(task) if task else None
    finally:
        session.close()


def get_user_active_task(submitter_id: str) -> Optional[dict]:
    """
    查用户最近一条未关闭任务（方案 A：只追踪最近一个）。
    active = status IN ('auto_answered', 'assigned')
    """
    if not submitter_id:
        return None
    session = _get_session()
    try:
        task = (
            session.query(Task)
            .filter(
                Task.submitter_id == submitter_id,
                Task.status.in_(["auto_answered", "assigned"]),
            )
            .order_by(Task.created_at.desc())
            .first()
        )
        return _task_to_dict(task) if task else None
    finally:
        session.close()


def get_engineer_active_task(engineer_name: str) -> Optional[dict]:
    """
    查工程师最近一条进行中任务。
    active = status = 'assigned'
    """
    if not engineer_name:
        return None
    session = _get_session()
    try:
        task = (
            session.query(Task)
            .filter(
                Task.assigned_engineer == engineer_name,
                Task.status == "assigned",
            )
            .order_by(Task.created_at.desc())
            .first()
        )
        return _task_to_dict(task) if task else None
    finally:
        session.close()


def update_task_status(task_id: int, status: str) -> Optional[dict]:
    """
    更新任务状态。
    当状态变为 resolved 时，自动写入 resolved_at。
    """
    session = _get_session()
    try:
        task = session.query(Task).filter(Task.id == task_id).first()
        if not task:
            return None
        task.status = status
        if status == "resolved" and not task.resolved_at:
            task.resolved_at = datetime.now()
        session.commit()
        session.refresh(task)
        return _task_to_dict(task)
    finally:
        session.close()


def assign_engineer_to_task(task_id: int, engineer_name: str) -> Optional[dict]:
    """
    绑定工程师到任务（用于 easy 升级为 assigned 时）。
    同时把状态改为 assigned。
    """
    session = _get_session()
    try:
        task = session.query(Task).filter(Task.id == task_id).first()
        if not task:
            return None
        task.assigned_engineer = engineer_name
        task.status = "assigned"
        session.commit()
        session.refresh(task)
        return _task_to_dict(task)
    finally:
        session.close()


def update_task_response(task_id: int, final_response: str) -> Optional[dict]:
    """更新任务的最终回答内容"""
    session = _get_session()
    try:
        task = session.query(Task).filter(Task.id == task_id).first()
        if not task:
            return None
        task.final_response = final_response
        session.commit()
        session.refresh(task)
        return _task_to_dict(task)
    finally:
        session.close()


def count_active_tasks(engineer_name: str) -> int:
    """
    动态计算工程师当前活跃任务数（current_load）。
    active = status = 'assigned'
    """
    session = _get_session()
    try:
        return (
            session.query(Task)
            .filter(
                Task.assigned_engineer == engineer_name,
                Task.status == "assigned",
            )
            .count()
        )
    finally:
        session.close()


def list_recent_tasks(limit: int = 20) -> list[dict]:
    """查询最近任务列表（供 API /tasks 用）"""
    session = _get_session()
    try:
        tasks = session.query(Task).order_by(Task.created_at.desc()).limit(limit).all()
        return [_task_to_dict(t) for t in tasks]
    finally:
        session.close()


def _task_to_dict(t: Task) -> dict:
    """ORM 对象转字典"""
    return {
        "id": t.id,
        "task_no": t.task_no,
        "title": t.title,
        "description": t.description,
        "submitted_by": t.submitted_by,
        "submitter_id": t.submitter_id or "",
        "difficulty": t.difficulty,
        "status": t.status,
        "assigned_engineer": t.assigned_engineer or "",
        "final_response": t.final_response or "",
        "created_at": t.created_at.strftime("%Y-%m-%d %H:%M:%S")
        if t.created_at
        else "",
        "resolved_at": t.resolved_at.strftime("%Y-%m-%d %H:%M:%S")
        if t.resolved_at
        else "",
    }


# ==================== 反馈相关 ====================


def create_feedback(task_id: int, feedback_type: str, feedback_by: str) -> dict:
    """
    记录一条反馈。
    feedback_type: 'resolved' | 'unresolved'
    """
    session = _get_session()
    try:
        fb = Feedback(
            task_id=task_id,
            feedback_type=feedback_type,
            feedback_by=feedback_by,
        )
        _commit_and_refresh(session, fb)
        return {
            "id": fb.id,
            "task_id": fb.task_id,
            "feedback_type": fb.feedback_type,
            "feedback_by": fb.feedback_by,
            "created_at": fb.created_at.strftime("%Y-%m-%d %H:%M:%S")
            if fb.created_at
            else "",
        }
    finally:
        session.close()


def list_feedbacks(task_id: int) -> list[dict]:
    """查某任务的全部反馈记录"""
    session = _get_session()
    try:
        fbs = (
            session.query(Feedback)
            .filter(Feedback.task_id == task_id)
            .order_by(Feedback.created_at)
            .all()
        )
        return [
            {
                "id": f.id,
                "feedback_type": f.feedback_type,
                "feedback_by": f.feedback_by,
                "created_at": f.created_at.strftime("%Y-%m-%d %H:%M:%S")
                if f.created_at
                else "",
            }
            for f in fbs
        ]
    finally:
        session.close()
