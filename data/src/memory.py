"""
记忆管理 -- 交互记忆的向量存储与检索。

与知识库（skill 文档）隔离，使用 ChromaDB 独立 collection。
每次交互后由后处理层调用 save_memory 存储，AI 处理层调用 search_memory 检索。
"""

import os
import uuid
from pathlib import Path

os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
os.environ.setdefault("HF_HUB_OFFLINE", "0")

from langchain_chroma import Chroma
from langchain_huggingface import HuggingFaceEmbeddings

from .config import MEMORY_DB_PATH, MEMORY_ENABLED, MEMORY_SEARCH_TOP_K

# -------------------- Embedding 模型（与知识库共用） --------------------

_embeddings = None
_vectorstore = None


def _get_embeddings():
    """获取 embedding 模型实例（单例）"""
    global _embeddings
    if _embeddings is None:
        _embeddings = HuggingFaceEmbeddings(
            model_name="shibing624/text2vec-base-chinese",
            model_kwargs={"device": "cpu"},
            encode_kwargs={"normalize_embeddings": True},
        )
    return _embeddings


def _get_vectorstore():
    """获取记忆向量库实例（单例）"""
    global _vectorstore
    if _vectorstore is None:
        Path(MEMORY_DB_PATH).mkdir(parents=True, exist_ok=True)
        _vectorstore = Chroma(
            collection_name="ops_memory",
            embedding_function=_get_embeddings(),
            persist_directory=MEMORY_DB_PATH,
        )
    return _vectorstore


# -------------------- 记忆存储与检索 --------------------


def save_memory(
    summary: str, task_id: int | None = None, metadata: dict | None = None
) -> str:
    """
    存储一条交互记忆到向量库。
    返回向量库中的 doc_id（供 DB 记录 embedding_id）。
    """
    if not MEMORY_ENABLED:
        return ""

    if not summary or not summary.strip():
        return ""

    try:
        vs = _get_vectorstore()
        doc_id = f"memory_{uuid.uuid4().hex[:12]}"

        meta = {"task_id": str(task_id) if task_id else ""}
        if metadata:
            meta.update(metadata)

        vs.add_texts(
            texts=[summary],
            metadatas=[meta],
            ids=[doc_id],
        )
        print(f"[memory] ✅ 记忆已存储：{summary[:50]}... (id={doc_id})")
        return doc_id
    except Exception as e:
        print(f"[memory] ❌ 存储失败：{e}")
        return ""


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
        vs = _get_vectorstore()
        k = top_k or MEMORY_SEARCH_TOP_K
        docs = vs.similarity_search(query, k=k)

        if not docs:
            return ""

        results = []
        for doc in docs:
            results.append(f"- {doc.page_content}")

        memory_text = "\n".join(results)
        print(f"[memory] 🔍 检索到 {len(docs)} 条相关记忆")
        return memory_text
    except Exception as e:
        print(f"[memory] ❌ 检索失败：{e}")
        return ""


def get_memory_count() -> int:
    """获取当前记忆总数"""
    if not MEMORY_ENABLED:
        return 0
    try:
        vs = _get_vectorstore()
        return vs._collection.count()
    except Exception:
        return 0
