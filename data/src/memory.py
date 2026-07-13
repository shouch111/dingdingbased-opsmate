"""
记忆管理 -- 交互记忆的向量存储与检索（PostgreSQL + pgvector）。

与知识库共用一个 PostgreSQL 数据库，memories 表含 embedding 列。
每次交互后由后处理层调用 save_memory 存储，AI 处理层调用 search_memory 检索。
"""

from . import db_manager
from .config import MEMORY_ENABLED, MEMORY_SEARCH_TOP_K


def save_memory(
    summary: str, task_id: int | None = None, metadata: dict | None = None
) -> bool:
    """
    存储一条交互记忆（计算向量 + 存入 memories 表）。
    返回是否存储成功。
    """
    if not MEMORY_ENABLED:
        return False

    if not summary or not summary.strip():
        return False

    try:
        from .embedding import compute_embedding

        embedding = compute_embedding(summary)

        intent = metadata.get("intent", "") if metadata else ""
        complexity = metadata.get("complexity", "") if metadata else ""

        db_manager.create_memory(
            task_id=task_id,
            summary=summary,
            intent=intent,
            complexity=complexity,
            embedding=embedding,
        )
        print(f"[memory] ✅ 记忆已存储：{summary[:50]}...")
        return True
    except Exception as e:
        print(f"[memory] ❌ 存储失败：{e}")
        return False


def search_memory(query: str, top_k: int | None = None) -> str:
    """
    检索与 query 最相关的历史交互记忆。
    返回拼接的记忆文本，无结果返回空字符串。
    """
    if not MEMORY_ENABLED:
        return ""

    if not query or not query.strip():
        return ""

    try:
        from .embedding import compute_embedding

        query_embedding = compute_embedding(query)
        if not query_embedding:
            return ""

        k = top_k or MEMORY_SEARCH_TOP_K
        mems = db_manager.search_memories_by_vector(query_embedding, top_k=k)

        if not mems:
            return ""

        results = [f"- {m['summary']}" for m in mems]
        memory_text = "\n".join(results)
        print(f"[memory] 🔍 检索到 {len(mems)} 条相关记忆")
        return memory_text
    except Exception as e:
        print(f"[memory] ❌ 检索失败：{e}")
        return ""


def get_memory_count() -> int:
    """获取当前记忆总数"""
    if not MEMORY_ENABLED:
        return 0
    try:
        from . import db_manager as _dbm
        from .database import Memory

        session = _dbm._get_session()
        try:
            return session.query(Memory).count()
        finally:
            session.close()
    except Exception:
        return 0
