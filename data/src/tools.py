import hashlib
import json
import os
import shutil
import time
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv
from langchain_chroma import Chroma
from langchain_community.document_loaders import DirectoryLoader, TextLoader
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter

# 优先从 data/ 目录加载 .env，若不存在则向上查找（支持从项目根目录启动）
DATA_DIR = Path(__file__).parent.parent
_env_paths = [
    DATA_DIR / ".env",
    DATA_DIR.parent / ".env",
]
for _p in _env_paths:
    if _p.exists():
        load_dotenv(_p)
        print(f"[tools] 已加载环境变量：{_p}")
        break
else:
    load_dotenv()
    print("[tools] 未找到 .env 文件，使用环境变量或默认值")

open_code_go_api = os.getenv("open_code_go_api")

# -------------------- 知识库相关（支持热更新） --------------------

_SYNC_COOLDOWN = 5.0       # 同步冷却时间（秒）
_last_sync_time = 0.0


def _make_embeddings():
    """创建 embedding 模型实例（统一入口）。"""
    return HuggingFaceEmbeddings(
        model_name="shibing624/text2vec-base-chinese",
        model_kwargs={"device": "cpu"},
        encode_kwargs={"normalize_embeddings": True},
    )


def _get_file_hash(filepath: Path) -> str:
    """计算文件内容的 MD5 哈希，用于变更检测。"""
    return hashlib.md5(filepath.read_bytes()).hexdigest()


def _load_file_index(index_path: Path) -> dict[str, str]:
    """加载文件索引：{相对路径: MD5哈希}。"""
    if index_path.exists():
        try:
            with open(index_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {}


def _save_file_index(index_path: Path, index: dict[str, str]):
    """保存文件索引到磁盘。"""
    index_path.parent.mkdir(parents=True, exist_ok=True)
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)


def _get_or_create_vectorstore():
    """加载已有向量库。如果库不存在则返回 None。"""
    vs_path = DATA_DIR / "chroma_db"
    embeddings = _make_embeddings()
    if vs_path.exists() and any(vs_path.iterdir()):
        return Chroma(
            persist_directory=str(vs_path),
            embedding_function=embeddings,
        )
    return None


def _delete_file_from_vs(vs: Chroma, rel_path: str):
    """从向量库中删除指定文件的所有 chunks。"""
    try:
        col = vs._collection
        results = col.get(where={"source": rel_path})
        if results and results.get("ids"):
            col.delete(ids=results["ids"])
    except Exception as e:
        print(f"[tools] 删除文件 chunks 失败（{rel_path}）：{e}")


def _add_file_to_vs(vs: Chroma, filepath: Path, rel_path: str, embeddings):
    """将单个 .md 文件分块后加入向量库，使用确定性 chunk ID。"""
    loader = TextLoader(str(filepath), encoding="utf-8")
    docs = loader.load()
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=800,
        chunk_overlap=100,
    )
    chunks = splitter.split_documents(docs)

    ids = []
    for i, chunk in enumerate(chunks):
        chunk.metadata["source"] = rel_path
        chunk_id = f"{rel_path}:chunk_{i}"
        ids.append(chunk_id)

    try:
        vs.add_documents(chunks, ids=ids)
    except Exception as e:
        print(f"[tools] 添加文件 chunks 失败（{rel_path}）：{e}")


def _full_rebuild(knowledge_dir: Path, vs_path: Path):
    """全量重建向量库并建立文件索引。"""
    embeddings = _make_embeddings()

    if vs_path.exists():
        shutil.rmtree(vs_path)
    vs_path.mkdir(parents=True, exist_ok=True)

    if not knowledge_dir.exists():
        print("[tools] 知识库目录不存在，创建空向量库")
        vs = Chroma(
            persist_directory=str(vs_path),
            embedding_function=embeddings,
        )
        _save_file_index(vs_path / "file_index.json", {})
        return vs

    loader = DirectoryLoader(
        str(knowledge_dir),
        glob="**/*.md",
        loader_cls=TextLoader,
        loader_kwargs={"encoding": "utf-8"},
    )
    docs = loader.load()
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=800,
        chunk_overlap=100,
    )
    chunks = splitter.split_documents(docs)

    # 按来源文件分组，分配确定性 chunk ID
    file_chunks: dict[str, list] = defaultdict(list)
    for chunk in chunks:
        source = chunk.metadata.get("source", "")
        try:
            rel_path = str(Path(source).relative_to(knowledge_dir))
        except ValueError:
            rel_path = source
        chunk.metadata["source"] = rel_path
        file_chunks[rel_path].append(chunk)

    all_chunks = []
    all_ids = []
    for rel_path, chks in file_chunks.items():
        for i, chk in enumerate(chks):
            all_chunks.append(chk)
            all_ids.append(f"{rel_path}:chunk_{i}")

    vs = Chroma.from_documents(
        all_chunks,
        embedding=embeddings,
        persist_directory=str(vs_path),
        ids=all_ids,
    )

    file_index = {}
    for md_file in knowledge_dir.glob("**/*.md"):
        rel_path = str(md_file.relative_to(knowledge_dir))
        file_index[rel_path] = _get_file_hash(md_file)
    _save_file_index(vs_path / "file_index.json", file_index)

    print(f"[tools] 全量重建完成，共索引 {len(file_index)} 个文件、{len(all_ids)} 个 chunk")
    return vs


def sync_knowledge(force: bool = False):
    """
    增量同步知识库 —— 热更新的核心函数。

    只处理变化的部分：
    - 新增文件 → 分块加入向量库
    - 修改文件 → 删除旧 chunks + 添加新 chunks
    - 删除文件 → 从向量库移除对应 chunks

    如果 force=True 或向量库/索引丢失，则执行全量重建。
    """
    vs_path = DATA_DIR / "chroma_db"
    knowledge_dir = DATA_DIR / "knowledge"
    index_path = vs_path / "file_index.json"

    if force or not vs_path.exists() or not any(vs_path.iterdir()) or not index_path.exists():
        return _full_rebuild(knowledge_dir, vs_path)

    vs = _get_or_create_vectorstore()
    if vs is None:
        return _full_rebuild(knowledge_dir, vs_path)

    old_index = _load_file_index(index_path)

    current_index: dict[str, str] = {}
    if knowledge_dir.exists():
        for md_file in knowledge_dir.glob("**/*.md"):
            rel_path = str(md_file.relative_to(knowledge_dir))
            current_index[rel_path] = _get_file_hash(md_file)

    old_set = set(old_index.keys())
    new_set = set(current_index.keys())

    added = new_set - old_set
    deleted = old_set - new_set
    modified = {
        f for f in new_set & old_set
        if current_index[f] != old_index[f]
    }

    if not added and not deleted and not modified:
        return vs

    embeddings = _make_embeddings()

    for rel_path in deleted:
        print(f"[tools] 🗑 删除知识：{rel_path}")
        _delete_file_from_vs(vs, rel_path)

    for rel_path in modified:
        print(f"[tools] ✏️ 更新知识：{rel_path}")
        _delete_file_from_vs(vs, rel_path)
        _add_file_to_vs(vs, knowledge_dir / rel_path, rel_path, embeddings)

    for rel_path in added:
        print(f"[tools] ➕ 新增知识：{rel_path}")
        _add_file_to_vs(vs, knowledge_dir / rel_path, rel_path, embeddings)

    _save_file_index(index_path, current_index)

    total = len(added) + len(deleted) + len(modified)
    print(f"[tools] 增量同步完成：+{len(added)}/-{len(deleted)}/~{len(modified)}（共 {total} 个文件变更）")
    return vs


def force_rebuild_knowledge():
    """强制全量重建知识库向量索引。"""
    print("[tools] 🔄 强制全量重建知识库...")
    return sync_knowledge(force=True)


def build_or_load_vectorstore():
    """
    【兼容旧接口】加载向量库，首次使用时自动构建。
    推荐使用 retrieve_knowledge() 进行日常查询，它会自动触发增量同步。
    """
    vs = _get_or_create_vectorstore()
    if vs is not None:
        return vs
    return _full_rebuild(DATA_DIR / "knowledge", DATA_DIR / "chroma_db")


def retrieve_knowledge(query: str, top_k: int = 3) -> str:
    """
    从知识库检索与 query 最相关的 top_k 篇文档，拼接成一段上下文返回。

    每次调用时自动检测知识库文件变更，并增量同步（有 5 秒冷却时间）。
    你只需正常增删改 data/knowledge/*.md 文件，系统会自动热更新！
    """
    global _last_sync_time

    now = time.time()
    if now - _last_sync_time > _SYNC_COOLDOWN:
        vs = sync_knowledge()
        _last_sync_time = now
    else:
        vs = _get_or_create_vectorstore()

    if vs is None:
        return "（知识库为空，请先添加知识文档到 data/knowledge/ 目录）"

    docs = vs.similarity_search(query, k=top_k)
    if not docs:
        return "（知识库中未找到相关内容）"
    return "\n\n---\n\n".join(doc.page_content for doc in docs)


# -------------------- 工程师相关 --------------------


def load_engineers() -> list[dict]:
    """从 data/engineers.json 加载工程师列表。"""
    file_path = DATA_DIR / "engineers.json"
    print(f"[tools] 正在加载工程师名单：{file_path}")
    # print(f"[tools] DATA_DIR = {DATA_DIR}")
    # print(f"[tools] 文件存在: {file_path.exists()}, 大小: {file_path.stat().st_size if file_path.exists() else 'N/A'} bytes")
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            engineers = json.load(f)
        print(
            f"[tools] 成功加载 {len(engineers)} 位工程师："
            f"{[e.get('name', '?') for e in engineers]}"
        )
        return engineers
    except FileNotFoundError:
        print(f"[tools] ❌ 找不到工程师名单文件：{file_path}")
        print(f"[tools]    DATA_DIR = {DATA_DIR}")
        print(f"[tools]    当前工作目录 = {os.getcwd()}")
        return []
    except json.JSONDecodeError as e:
        print(f"[tools] ❌ 工程师名单 JSON 格式错误：{e}")
        return []
