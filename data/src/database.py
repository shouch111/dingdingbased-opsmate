"""
数据库连接管理 + ORM 模型定义（PostgreSQL + pgvector 版）。

统一关系型数据 + 向量数据到一个 PostgreSQL 数据库：
- engineers / tasks / feedbacks / memories：关系型数据
- knowledge_docs / memories.embedding：向量数据（pgvector）
"""

import os
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    text,
)
from sqlalchemy.orm import Mapped, declarative_base, mapped_column, sessionmaker

# -------------------- 环境变量加载 --------------------

DATA_DIR = Path(__file__).parent.parent
_env_paths = [
    DATA_DIR / ".env",
    DATA_DIR.parent / ".env",
]
for _p in _env_paths:
    if _p.exists():
        load_dotenv(_p)
        print(f"[database] 已加载环境变量：{_p}")
        break
else:
    load_dotenv()

# -------------------- 数据库连接 --------------------

PG_HOST = os.getenv("PG_HOST", "localhost")
PG_PORT = os.getenv("PG_PORT", "5432")
PG_USER = os.getenv("PG_USER", "postgres")
PG_PASSWORD = os.getenv("PG_PASSWORD", "")
PG_DATABASE = os.getenv("PG_DATABASE", "ops_agent")

DATABASE_URL = (
    f"postgresql+psycopg2://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:{PG_PORT}/{PG_DATABASE}"
)

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_recycle=3600,
    echo=False,
)

SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
Base = declarative_base()

# text2vec-base-chinese 输出 768 维向量
EMBEDDING_DIM = 768


# -------------------- ORM 模型 --------------------


class Engineer(Base):
    """IT 工程师"""

    __tablename__ = "engineers"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(
        String(100), nullable=False, unique=True, comment="姓名"
    )
    skills: Mapped[list] = mapped_column(JSON, nullable=False, comment="技能标签列表")
    mobile: Mapped[str] = mapped_column(String(20), default="", comment="手机号")
    dingtalk_user_id: Mapped[str] = mapped_column(
        String(64), default="", comment="钉钉 UserID"
    )
    available: Mapped[bool] = mapped_column(Boolean, default=True, comment="是否在岗")
    created_at: Mapped[datetime] = mapped_column(default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(
        default=datetime.now, onupdate=datetime.now
    )


class Task(Base):
    """运维任务"""

    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    task_no: Mapped[str] = mapped_column(
        String(20), nullable=False, unique=True, comment="任务编号 T1001"
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False, comment="标题")
    description: Mapped[str] = mapped_column(Text, nullable=False, comment="描述")
    submitted_by: Mapped[str] = mapped_column(
        String(100), nullable=False, comment="提交人姓名"
    )
    submitter_id: Mapped[str] = mapped_column(
        String(64), default="", comment="提交人钉钉 ID"
    )
    difficulty: Mapped[str] = mapped_column(
        Enum("easy", "hard"), nullable=False, comment="难度"
    )
    status: Mapped[str] = mapped_column(
        Enum("auto_answered", "assigned", "resolved"),
        nullable=False,
        comment="任务状态",
    )
    assigned_engineer: Mapped[str] = mapped_column(
        String(100), default="", comment="分配工程师姓名"
    )
    final_response: Mapped[str | None] = mapped_column(Text, comment="给用户的回答")
    created_at: Mapped[datetime] = mapped_column(default=datetime.now)
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="解决时间"
    )
    intent: Mapped[str] = mapped_column(String(50), default="", comment="意图")
    complexity: Mapped[str] = mapped_column(String(20), default="", comment="复杂度")
    model_used: Mapped[str] = mapped_column(
        String(50), default="", comment="使用的AI模型"
    )
    raw_content: Mapped[str | None] = mapped_column(Text, comment="原始消息（脱敏后）")


class Feedback(Base):
    """任务反馈记录"""

    __tablename__ = "feedbacks"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    feedback_type: Mapped[str] = mapped_column(
        Enum("resolved", "unresolved"), nullable=False, comment="反馈类型"
    )
    feedback_by: Mapped[str] = mapped_column(
        String(100), nullable=False, comment="反馈人"
    )
    created_at: Mapped[datetime] = mapped_column(default=datetime.now)


class Memory(Base):
    """交互记忆（含向量，pgvector）"""

    __tablename__ = "memories"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    task_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="关联任务ID"
    )
    summary: Mapped[str] = mapped_column(
        String(500), nullable=False, comment="query+answer 摘要"
    )
    intent: Mapped[str] = mapped_column(String(50), default="", comment="意图")
    complexity: Mapped[str] = mapped_column(String(20), default="", comment="复杂度")
    model_used: Mapped[str] = mapped_column(
        String(50), default="", comment="使用的模型"
    )
    embedding: Mapped[list | None] = mapped_column(
        Vector(EMBEDDING_DIM), comment="摘要向量"
    )
    created_at: Mapped[datetime] = mapped_column(default=datetime.now)


class KnowledgeDoc(Base):
    """知识库文档分块（含向量，pgvector）"""

    __tablename__ = "knowledge_docs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(
        String(200), nullable=False, comment="来源文件路径"
    )
    chunk_index: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="分块序号"
    )
    content: Mapped[str] = mapped_column(Text, nullable=False, comment="分块文本")
    embedding: Mapped[list | None] = mapped_column(
        Vector(EMBEDDING_DIM), comment="分块向量"
    )
    file_hash: Mapped[str] = mapped_column(
        String(64), default="", comment="来源文件MD5"
    )
    created_at: Mapped[datetime] = mapped_column(default=datetime.now)


# -------------------- 初始化函数 --------------------


def create_database_if_not_exists():
    """连接 PostgreSQL 服务器，如果目标数据库不存在则创建。"""
    import psycopg2

    try:
        conn = psycopg2.connect(
            host=PG_HOST,
            port=int(PG_PORT),
            user=PG_USER,
            password=PG_PASSWORD,
            database="postgres",
        )
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (PG_DATABASE,))
        if not cur.fetchone():
            cur.execute(f'CREATE DATABASE "{PG_DATABASE}"')
            print(f"[database] ✅ 数据库 `{PG_DATABASE}` 已创建")
        else:
            print(f"[database] ✅ 数据库 `{PG_DATABASE}` 已存在")
        cur.close()
        conn.close()
    except Exception as e:
        print(f"[database] ❌ 创建数据库失败：{e}")
        raise


def init_db():
    """建表 + 安装 pgvector 扩展。"""
    # 安装 pgvector 扩展
    with engine.connect() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        conn.commit()
    # 建表
    Base.metadata.create_all(engine)
    print("[database] ✅ 数据库表已就绪（含 pgvector）")


def get_db():
    """FastAPI 依赖注入用"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
