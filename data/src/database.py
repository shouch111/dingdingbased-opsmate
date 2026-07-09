"""
数据库连接管理 + ORM 模型定义。

引入第一阶段：任务持久化、工程师名单迁移到 DB。
"""

import os
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
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
)
from sqlalchemy.orm import Mapped, declarative_base, mapped_column, sessionmaker

# -------------------- 环境变量加载 --------------------

DATA_DIR = Path(__file__).parent.parent  # data/
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
    print("[database] 未找到 .env 文件，使用环境变量或默认值")

# -------------------- 数据库连接 --------------------

MYSQL_HOST = os.getenv("MYSQL_HOST", "localhost")
MYSQL_PORT = os.getenv("MYSQL_PORT", "3306")
MYSQL_USER = os.getenv("MYSQL_USER", "root")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "")
MYSQL_DATABASE = os.getenv("MYSQL_DATABASE", "ops_agent")

DATABASE_URL = (
    f"mysql+pymysql://{MYSQL_USER}:{MYSQL_PASSWORD}"
    f"@{MYSQL_HOST}:{MYSQL_PORT}/{MYSQL_DATABASE}?charset=utf8mb4"
)

# pool_pre_ping: 连接前检测是否存活，避免"连接已断开"错误
# pool_recycle:  每 3600s 回收连接，防止 MySQL 8h 超时
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_recycle=3600,
    echo=False,  # 调试时可改为 True 打印 SQL
)

SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
Base = declarative_base()


# -------------------- ORM 模型 --------------------


class Engineer(Base):
    """IT 工程师（从 engineers.json 迁移）"""

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
        String(64), default="", comment="提交人钉钉 ID（反馈追踪用）"
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
    final_response: Mapped[str | None] = mapped_column(
        Text, comment="给用户的回答/分配信息"
    )
    created_at: Mapped[datetime] = mapped_column(default=datetime.now)
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="解决时间"
    )


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


# -------------------- 初始化函数 --------------------


def create_database_if_not_exists():
    """
    连接 MySQL 服务器（不指定库），如果目标数据库不存在则创建。
    在 init_db() 之前调用，避免用户手动建库。
    """
    import pymysql

    try:
        conn = pymysql.connect(
            host=MYSQL_HOST,
            port=int(MYSQL_PORT),
            user=MYSQL_USER,
            password=MYSQL_PASSWORD,
            charset="utf8mb4",
        )
        cur = conn.cursor()
        cur.execute(
            f"CREATE DATABASE IF NOT EXISTS `{MYSQL_DATABASE}` "
            f"DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci"
        )
        conn.commit()
        cur.close()
        conn.close()
        print(f"[database] ✅ 数据库 `{MYSQL_DATABASE}` 已就绪")
    except Exception as e:
        print(f"[database] ❌ 创建数据库失败：{e}")
        raise


def init_db():
    """建表（已存在的表不会重建）。调用前需确保数据库已存在。"""
    Base.metadata.create_all(engine)
    print("[database] ✅ 数据库表已就绪")


def get_db():
    """FastAPI 依赖注入用：获取数据库会话"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
