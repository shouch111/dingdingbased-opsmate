"""
数据库连接管理 + ORM 模型定义（PostgreSQL + pgvector 版）。

统一关系型数据 + 向量数据到一个 PostgreSQL 数据库：
- engineers / tasks / feedbacks / memories：关系型数据
- knowledge_docs / memories.embedding：向量数据（pgvector）
"""

import logging
import os
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus

from dotenv import load_dotenv
from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    text,
)
from sqlalchemy.orm import Mapped, declarative_base, mapped_column, sessionmaker

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
        logger.info("已加载环境变量：%s", _p)
        break
else:
    load_dotenv()

# -------------------- 数据库连接 --------------------

PG_HOST = os.getenv("PG_HOST", "localhost")
PG_PORT = os.getenv("PG_PORT", "5432")
PG_USER = os.getenv("PG_USER", "postgres")
PG_PASSWORD = os.getenv("PG_PASSWORD", "")
PG_DATABASE = os.getenv("PG_DATABASE", "ops_agent")

# 密码用 URL 编码，避免 @:/ 等特殊字符破坏连接字符串
DATABASE_URL = (
    f"postgresql+psycopg2://{PG_USER}:{quote_plus(PG_PASSWORD)}@{PG_HOST}:{PG_PORT}/{PG_DATABASE}"
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
        String(100), nullable=False, comment="姓名（允许同名，靠 staff_id 唯一识别）"
    )
    staff_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        unique=True,
        comment="员工工号（钉钉 staffId，唯一识别）",
    )
    skills: Mapped[list] = mapped_column(JSON, nullable=False, comment="技能标签列表")
    mobile: Mapped[str] = mapped_column(String(20), default="", comment="手机号")
    dingtalk_user_id: Mapped[str] = mapped_column(
        String(64), default="", comment="钉钉 UserId（用于发私聊消息）"
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
        String(20), nullable=False, comment="难度（simple/medium/hard，与 complexity 取值统一）"
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, comment="任务状态"
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
        String(20), nullable=False, comment="反馈类型"
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
        # 用 DSN 字符串连接（避免密码含中文/特殊字符时的编码问题）
        dsn = (
            f"host={PG_HOST} port={int(PG_PORT)} "
            f"user={PG_USER} password={PG_PASSWORD} "
            f"dbname=postgres"
        )
        conn = psycopg2.connect(dsn)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (PG_DATABASE,))
        if not cur.fetchone():
            cur.execute(f'CREATE DATABASE "{PG_DATABASE}"')
            logger.info("数据库 %s 已创建", PG_DATABASE)
        else:
            logger.info("数据库 %s 已存在", PG_DATABASE)
        cur.close()
        conn.close()
    except Exception as e:
        logger.error("创建数据库失败：%s", e)
        raise


def init_db():
    """建表 + 安装 pgvector 扩展 + 创建索引。"""
    # 安装 pgvector 扩展
    with engine.connect() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        conn.commit()
    # 建表
    Base.metadata.create_all(engine)
    # 创建索引（幂等）
    with engine.connect() as conn:
        # 复合索引：加速按提交人/工程师查活跃任务的查询
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_tasks_submitter_status "
                "ON tasks (submitter_id, status)"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_tasks_engineer_status "
                "ON tasks (assigned_engineer, status)"
            )
        )
        # 向量索引：HNSW 近似最近邻，加速知识库和记忆的向量检索
        # knowledge_docs.embedding 和 memories.embedding 的 cosine_distance 查询
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_knowledge_embedding "
                "ON knowledge_docs USING hnsw (embedding vector_cosine_ops)"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_memories_embedding "
                "ON memories USING hnsw (embedding vector_cosine_ops)"
            )
        )
        conn.commit()
    logger.info("数据库表已就绪（含 pgvector + 复合索引 + HNSW 向量索引）")


def migrate_engineers_schema():
    """
    engineers 表结构增量迁移（幂等，安全可重复执行）：
    - 新增 staff_id 列（工号）
    - 移除 name 的 unique 约束（允许同名）
    - 为 staff_id 建立唯一约束（PostgreSQL 允许多个 NULL，未绑定的工程师不冲突）

    适用于已存在的旧表平滑升级；全新表由 create_all 直接建好，本函数跳过实际改动。
    """
    with engine.connect() as conn:
        # 1. 新增 staff_id 列
        conn.execute(
            text("ALTER TABLE engineers ADD COLUMN IF NOT EXISTS staff_id VARCHAR(64)")
        )
        # 2. 移除 name 上的 unique 约束（自动查找约束名，兼容不同命名）
        conn.execute(
            text(
                "DO $$ "
                "DECLARE c text; "
                "BEGIN "
                "  SELECT conname INTO c FROM pg_constraint "
                "  WHERE conrelid='engineers'::regclass AND contype='u' "
                "    AND pg_get_constraintdef(oid) LIKE '%name%'; "
                "  IF c IS NOT NULL THEN "
                "    EXECUTE format('ALTER TABLE engineers DROP CONSTRAINT %I', c); "
                "  END IF; "
                "END $$;"
            )
        )
        # 3. staff_id 唯一约束（若不存在则创建；多个 NULL 不冲突）
        conn.execute(
            text(
                "DO $$ "
                "BEGIN "
                "  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ux_engineers_staff_id') THEN "
                "    ALTER TABLE engineers ADD CONSTRAINT ux_engineers_staff_id UNIQUE (staff_id); "
                "  END IF; "
                "END $$;"
            )
        )
        conn.commit()
        logger.info("engineers 表结构迁移完成（staff_id + name 去 unique）")


def migrate_difficulty_values():
    """
    tasks.difficulty 值迁移（幂等，安全可重复执行）：
    将旧值 'easy' 更新为 'simple'，'hard' 不变。
    适用于 v2.7.0 升级（difficulty 枚举从 easy/hard 统一为 simple/medium/hard）。
    """
    try:
        with engine.connect() as conn:
            result = conn.execute(
                text("UPDATE tasks SET difficulty = 'simple' WHERE difficulty = 'easy'")
            )
            conn.commit()
            if result.rowcount > 0:
                logger.info(
                    "difficulty 值迁移完成：%s 条 easy -> simple", result.rowcount
                )
    except Exception as e:
        logger.warning("difficulty 值迁移跳过（表可能不存在）：%s", e)


def get_db():
    """FastAPI 依赖注入用"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
