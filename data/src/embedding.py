"""
Embedding 服务 -- 共享 HuggingFace embedding 模型实例。

所有需要计算向量的模块（tools.py / memory.py）统一通过本模块获取 embedding。
text2vec-base-chinese 输出 768 维向量，与 database.py 的 EMBEDDING_DIM 一致。
"""

import logging
import os
from pathlib import Path

os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
os.environ.setdefault("HF_HUB_OFFLINE", "0")

from dotenv import load_dotenv
from langchain_huggingface import HuggingFaceEmbeddings

logger = logging.getLogger(__name__)

# 加载环境变量
DATA_DIR = Path(__file__).parent.parent
_env_paths = [DATA_DIR / ".env", DATA_DIR.parent / ".env"]
for _p in _env_paths:
    if _p.exists():
        load_dotenv(_p)
        break

_embeddings = None


def get_embeddings():
    """获取 embedding 模型实例（单例，避免重复加载）"""
    global _embeddings
    if _embeddings is None:
        logger.info("正在加载 text2vec-base-chinese 模型...")
        _embeddings = HuggingFaceEmbeddings(
            model_name="shibing624/text2vec-base-chinese",
            model_kwargs={"device": "cpu"},
            encode_kwargs={"normalize_embeddings": True},
        )
        logger.info("模型加载完成（768 维）")
    return _embeddings


def compute_embedding(text: str) -> list[float]:
    """计算文本的 embedding 向量"""
    if not text or not text.strip():
        return []
    return get_embeddings().embed_query(text)
