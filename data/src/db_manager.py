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
            staff_id=data.get("staff_id") or None,  # 空串转 None，避免唯一约束冲突
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


def get_engineer_by_staff_id(staff_id: str) -> Optional[dict]:
    """按工号查工程师（唯一识别，用于身份直连）"""
    if not staff_id:
        return None
    session = _get_session()
    try:
        e = session.query(Engineer).filter(Engineer.staff_id == staff_id).first()
        return _engineer_to_dict(e) if e else None
    finally:
        session.close()


def get_engineer_by_mobile(mobile: str) -> Optional[dict]:
    """按手机号查工程师（用于身份辅助匹配）"""
    if not mobile:
        return None
    session = _get_session()
    try:
        e = session.query(Engineer).filter(Engineer.mobile == mobile).first()
        return _engineer_to_dict(e) if e else None
    finally:
        session.close()


def get_engineers_by_name(name: str) -> list[dict]:
    """按姓名查工程师（可能多条，用于同名场景的候选集）"""
    if not name:
        return []
    session = _get_session()
    try:
        rows = session.query(Engineer).filter(Engineer.name == name).all()
        return [_engineer_to_dict(e) for e in rows]
    finally:
        session.close()


def update_engineer_binding(
    engineer_id: int,
    staff_id: Optional[str] = None,
    dingtalk_user_id: Optional[str] = None,
) -> bool:
    """
    回填工程师的工号 / 钉钉 UserId（仅当原值为空时回填，不覆盖已有值）。
    返回是否发生了更新。
    """
    session = _get_session()
    try:
        e = session.query(Engineer).filter(Engineer.id == engineer_id).first()
        if not e:
            return False
        updated = False
        if staff_id and not e.staff_id:
            e.staff_id = staff_id
            updated = True
        if dingtalk_user_id and not e.dingtalk_user_id:
            e.dingtalk_user_id = dingtalk_user_id
            updated = True
        if updated:
            session.commit()
        return updated
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
        "id": e.id,
        "name": e.name,
        "staff_id": e.staff_id or "",
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
    intent: str = "",
    complexity: str = "",
    model_used: str = "",
    raw_content: str = "",
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
            intent=intent,
            complexity=complexity,
            model_used=model_used,
            raw_content=raw_content,
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
    绑定工程师到任务（用于 simple 升级为 assigned 时）。
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


def count_active_tasks_batch() -> dict[str, int]:
    """
    一条 SQL 批量计算所有工程师的活跃任务数。
    返回 {engineer_name: count}，未出现的工程师不在字典中（调用方默认 0）。
    替代逐人调 count_active_tasks 的 N+1 模式。
    """
    session = _get_session()
    try:
        rows = (
            session.query(
                Task.assigned_engineer,
                func.count(Task.id).label("cnt"),
            )
            .filter(
                Task.assigned_engineer != "",
                Task.status == "assigned",
            )
            .group_by(Task.assigned_engineer)
            .all()
        )
        return {row[0]: row[1] for row in rows}
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
        "intent": getattr(t, "intent", "") or "",
        "complexity": getattr(t, "complexity", "") or "",
        "model_used": getattr(t, "model_used", "") or "",
        "raw_content": getattr(t, "raw_content", "") or "",
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


# ==================== 提醒相关（重新提醒功能） ====================


# 系统提醒记录的 feedback_by 前缀，格式：系统提醒|工程师名
REMINDER_PREFIX = "系统提醒"


def _reminder_key(engineer_name: str) -> str:
    """生成提醒记录的 feedback_by 标识"""
    return f"{REMINDER_PREFIX}|{engineer_name}"


def get_assigned_tasks() -> list[dict]:
    """查询所有 assigned 状态的任务（供调度器扫描超时）"""
    session = _get_session()
    try:
        tasks = (
            session.query(Task)
            .filter(Task.status == "assigned")
            .order_by(Task.created_at)
            .all()
        )
        return [_task_to_dict(t) for t in tasks]
    finally:
        session.close()


def count_reminders(task_id: int, engineer_name: str) -> int:
    """统计某工程师在某任务上已被提醒的次数"""
    session = _get_session()
    try:
        return (
            session.query(Feedback)
            .filter(
                Feedback.task_id == task_id,
                Feedback.feedback_by == _reminder_key(engineer_name),
            )
            .count()
        )
    finally:
        session.close()


def get_last_reminder_time(task_id: int, engineer_name: str) -> Optional[datetime]:
    """获取某工程师在某任务上的最后一次提醒时间，无记录返回 None"""
    session = _get_session()
    try:
        result = (
            session.query(func.max(Feedback.created_at))
            .filter(
                Feedback.task_id == task_id,
                Feedback.feedback_by == _reminder_key(engineer_name),
            )
            .scalar()
        )
        return result
    finally:
        session.close()


def create_reminder(task_id: int, engineer_name: str) -> dict:
    """记录一次系统提醒（复用 feedbacks 表）"""
    return create_feedback(task_id, "unresolved", _reminder_key(engineer_name))


# ==================== 记忆相关（PostgreSQL + pgvector） ====================


def create_memory(
    task_id: int | None,
    summary: str,
    intent: str = "",
    complexity: str = "",
    model_used: str = "",
    embedding: list | None = None,
) -> dict:
    """存储一条交互记忆（含向量）"""
    from .database import Memory

    session = _get_session()
    try:
        mem = Memory(
            task_id=task_id,
            summary=summary,
            intent=intent,
            complexity=complexity,
            model_used=model_used,
            embedding=embedding,
        )
        _commit_and_refresh(session, mem)
        return {
            "id": mem.id,
            "task_id": mem.task_id,
            "summary": mem.summary,
        }
    finally:
        session.close()


def list_memories(limit: int = 20) -> list[dict]:
    """查询最近的记忆记录"""
    from .database import Memory

    session = _get_session()
    try:
        mems = (
            session.query(Memory).order_by(Memory.created_at.desc()).limit(limit).all()
        )
        return [
            {
                "id": m.id,
                "task_id": m.task_id,
                "summary": m.summary,
                "intent": m.intent,
                "complexity": m.complexity,
                "created_at": m.created_at.strftime("%Y-%m-%d %H:%M:%S")
                if m.created_at
                else "",
            }
            for m in mems
        ]
    finally:
        session.close()


def search_memories_by_vector(query_embedding: list, top_k: int = 3) -> list[dict]:
    """向量检索记忆（pgvector cosine distance）"""
    from .database import Memory

    if not query_embedding:
        return []
    session = _get_session()
    try:
        mems = (
            session.query(Memory)
            .filter(Memory.embedding.isnot(None))
            .order_by(Memory.embedding.cosine_distance(query_embedding))
            .limit(top_k)
            .all()
        )
        return [
            {
                "id": m.id,
                "task_id": m.task_id,
                "summary": m.summary,
            }
            for m in mems
        ]
    finally:
        session.close()


# ==================== 知识库相关（PostgreSQL + pgvector） ====================


def add_knowledge_chunk(
    source: str, chunk_index: int, content: str, embedding: list, file_hash: str
) -> dict:
    """存储一个知识库分块"""
    from .database import KnowledgeDoc

    session = _get_session()
    try:
        doc = KnowledgeDoc(
            source=source,
            chunk_index=chunk_index,
            content=content,
            embedding=embedding,
            file_hash=file_hash,
        )
        _commit_and_refresh(session, doc)
        return {"id": doc.id, "source": doc.source}
    finally:
        session.close()


def delete_knowledge_by_source(source: str):
    """删除指定来源文件的所有分块"""
    from .database import KnowledgeDoc

    session = _get_session()
    try:
        session.query(KnowledgeDoc).filter(KnowledgeDoc.source == source).delete()
        session.commit()
    finally:
        session.close()


def get_knowledge_file_hashes() -> dict[str, str]:
    """获取知识库中所有文件的哈希（用于增量同步）"""
    from .database import KnowledgeDoc

    session = _get_session()
    try:
        rows = (
            session.query(KnowledgeDoc.source, KnowledgeDoc.file_hash)
            .distinct(KnowledgeDoc.source)
            .all()
        )
        return {row[0]: row[1] for row in rows}
    finally:
        session.close()


def search_knowledge_by_vector(query_embedding: list, top_k: int = 3) -> list[dict]:
    """向量检索知识库（pgvector cosine distance）"""
    from .database import KnowledgeDoc

    if not query_embedding:
        return []
    session = _get_session()
    try:
        docs = (
            session.query(KnowledgeDoc)
            .filter(KnowledgeDoc.embedding.isnot(None))
            .order_by(KnowledgeDoc.embedding.cosine_distance(query_embedding))
            .limit(top_k)
            .all()
        )
        return [
            {
                "id": d.id,
                "source": d.source,
                "content": d.content,
            }
            for d in docs
        ]
    finally:
        session.close()


def count_knowledge_chunks() -> int:
    """知识库分块总数"""
    from .database import KnowledgeDoc

    session = _get_session()
    try:
        return session.query(KnowledgeDoc).count()
    finally:
        session.close()
