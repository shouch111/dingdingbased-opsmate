"""
工具函数 -- 知识库检索 + 工程师加载。

知识库使用 PostgreSQL + pgvector（不再依赖 ChromaDB）。
支持增量热更新：增删改 data/knowledge/*.md 文件后自动同步。
"""

import hashlib
import json
import logging
import time
from pathlib import Path

from dotenv import load_dotenv
from langchain_community.document_loaders import TextLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent
_env_paths = [DATA_DIR / ".env", DATA_DIR.parent / ".env"]
for _p in _env_paths:
    if _p.exists():
        load_dotenv(_p)
        logger.info("已加载环境变量：%s", _p)
        break
else:
    load_dotenv()

# -------------------- 知识库相关（PostgreSQL + pgvector） --------------------

_SYNC_COOLDOWN = 5.0
_last_sync_time = 0.0


def _get_file_hash(filepath: Path) -> str:
    """计算文件内容的 MD5 哈希，用于变更检测。"""
    return hashlib.md5(filepath.read_bytes()).hexdigest()


def _chunk_file(filepath: Path) -> list[str]:
    """将 .md 文件分块，返回文本列表。"""
    loader = TextLoader(str(filepath), encoding="utf-8")
    docs = loader.load()
    splitter = RecursiveCharacterTextSplitter(chunk_size=800, chunk_overlap=100)
    return [chunk.page_content for chunk in splitter.split_documents(docs)]


def _add_file_to_db(filepath: Path, rel_path: str, file_hash: str):
    """将文件分块、向量化后存入 knowledge_docs 表。"""
    from . import db_manager
    from .embedding import compute_embedding

    chunks = _chunk_file(filepath)
    for i, content in enumerate(chunks):
        embedding = compute_embedding(content)
        db_manager.add_knowledge_chunk(
            source=rel_path,
            chunk_index=i,
            content=content,
            embedding=embedding,
            file_hash=file_hash,
        )


def sync_knowledge(force: bool = False):
    """
    增量同步知识库 -- 热更新的核心函数。

    比较 data/knowledge/*.md 文件哈希与 DB 中存储的哈希：
    - 新增文件 -> 分块 + 向量化 + 存入 knowledge_docs 表
    - 修改文件 -> 删旧分块 + 加新分块
    - 删除文件 -> 从 knowledge_docs 表移除
    """
    from . import db_manager

    knowledge_dir = DATA_DIR / "knowledge"
    if not knowledge_dir.exists():
        return

    current_index: dict[str, str] = {}
    for md_file in knowledge_dir.glob("**/*.md"):
        rel_path = str(md_file.relative_to(knowledge_dir))
        current_index[rel_path] = _get_file_hash(md_file)

    if force:
        old_index = {}
        for source in list(db_manager.get_knowledge_file_hashes().keys()):
            db_manager.delete_knowledge_by_source(source)
    else:
        old_index = db_manager.get_knowledge_file_hashes()

    old_set = set(old_index.keys())
    new_set = set(current_index.keys())

    added = new_set - old_set
    deleted = old_set - new_set
    modified = {f for f in new_set & old_set if current_index[f] != old_index[f]}

    if not added and not deleted and not modified:
        return

    for rel_path in deleted:
        logger.debug("删除知识：%s", rel_path)
        db_manager.delete_knowledge_by_source(rel_path)

    for rel_path in modified:
        logger.debug("更新知识：%s", rel_path)
        db_manager.delete_knowledge_by_source(rel_path)
        _add_file_to_db(knowledge_dir / rel_path, rel_path, current_index[rel_path])

    for rel_path in added:
        logger.debug("新增知识：%s", rel_path)
        _add_file_to_db(knowledge_dir / rel_path, rel_path, current_index[rel_path])

    total = len(added) + len(deleted) + len(modified)
    logger.info(
        "增量同步完成：+%s/-%s/~%s（共 %s 个文件变更）",
        len(added), len(deleted), len(modified), total,
    )


def force_rebuild_knowledge():
    """强制全量重建知识库向量索引。"""
    logger.info("强制全量重建知识库...")
    sync_knowledge(force=True)


def retrieve_knowledge(query: str, top_k: int = 3) -> str:
    """
    从知识库检索与 query 最相关的 top_k 篇文档，拼接成一段上下文返回。
    每次调用时自动检测知识库文件变更，并增量同步（有 5 秒冷却时间）。
    """
    global _last_sync_time

    now = time.time()
    if now - _last_sync_time > _SYNC_COOLDOWN:
        try:
            sync_knowledge()
        except Exception:
            logger.exception("知识库同步失败")
        _last_sync_time = now

    from . import db_manager
    from .embedding import compute_embedding

    query_embedding = compute_embedding(query)
    if not query_embedding:
        return "（向量计算失败，无法检索）"

    docs = db_manager.search_knowledge_by_vector(query_embedding, top_k=top_k)
    if not docs:
        return "（知识库中未找到相关内容）"

    return "\n\n---\n\n".join(doc["content"] for doc in docs)


# -------------------- 工程师相关 --------------------


def load_engineers() -> list[dict]:
    """加载工程师列表（从数据库，DB 不可用时降级读 JSON）。"""
    try:
        from . import db_manager

        engineers = db_manager.load_engineers_from_db()
        if engineers:
            # 批量计算负载（1 条 SQL 替代 N 条，消除 N+1）
            load_map = db_manager.count_active_tasks_batch()
            for e in engineers:
                e["current_load"] = load_map.get(e["name"], 0)
            logger.info("从 DB 加载 %s 位工程师", len(engineers))
            return engineers
        logger.info("DB 工程师表为空，尝试读取 engineers.json")
    except Exception as e:
        logger.warning("DB 加载失败，降级读 engineers.json：%s", e)

    return _load_engineers_from_json()


def _load_engineers_from_json() -> list[dict]:
    """降级方案：从 engineers.json 加载。"""
    file_path = DATA_DIR / "engineers.json"
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            engineers = json.load(f)
        logger.info("从 JSON 加载 %s 位工程师", len(engineers))
        return engineers
    except (FileNotFoundError, json.JSONDecodeError):
        logger.exception("engineers.json 也不可用")
        return []


def count_active_tasks(engineer_name: str) -> int:
    """动态计算工程师当前活跃任务数。"""
    try:
        from . import db_manager

        return db_manager.count_active_tasks(engineer_name)
    except Exception:
        return 0
