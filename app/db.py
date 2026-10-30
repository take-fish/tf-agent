"""数据库：审计库 fm_agent（MySQL）。

单实例试点用同步 SQLAlchemy + 短事务；异步端点里通过 anyio.to_thread 调用。

两条约束来自《FishMatrix 数据库账号体系》：

1. 本服务**不建表**。业务容器用的是 `fm_agent_app`（只有 DML 权限），
   建表由 `python -m app.migrate` 以 `fm_agent_migrate` 身份完成。
2. 数据库不可用要降级：会话不落库、审计丢失，但对话功能不受影响，
   并且必须在日志里说清楚是"没配 DSN"还是"没跑迁移"，否则很难排查。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.models import Base

logger = logging.getLogger(__name__)

REQUIRED_TABLES = frozenset(Base.metadata.tables.keys())

_engine = None
_session_factory: sessionmaker | None = None
_db_ok: bool | None = None


def init_db() -> bool:
    """建连接池并校验表结构（不建表）。返回是否可用。"""
    global _engine, _session_factory, _db_ok
    if _session_factory is not None:
        return bool(_db_ok)

    dsn = settings.mysql_dsn
    if not dsn:
        _db_ok = False
        logger.warning("未配置 FM_AGENT_MYSQL_DSN，会话与审计降级（对话功能不受影响）")
        return False

    try:
        engine = create_engine(
            dsn,
            pool_pre_ping=True,
            pool_recycle=1800,
            pool_size=5,
            max_overflow=5,
            future=True,
        )
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        missing = REQUIRED_TABLES - set(inspect(engine).get_table_names())
        if missing:
            engine.dispose()
            _db_ok = False
            logger.error(
                "审计库缺少表 %s：请先以 fm_agent_migrate 身份执行 `python -m app.migrate`；会话与审计降级",
                ", ".join(sorted(missing)),
            )
            return False

        _engine = engine
        _session_factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
        _db_ok = True
        logger.info("审计库就绪：%s", mask_dsn(dsn))
    except Exception as exc:  # noqa: BLE001 - 数据库不可用不应阻止服务启动
        _db_ok = False
        logger.warning("审计库不可用，会话与审计降级：%s", exc)
    return bool(_db_ok)


def db_available() -> bool:
    if _session_factory is None:
        init_db()
    return bool(_db_ok)


def reset_for_tests() -> None:
    """仅测试使用：丢弃已建立的连接池。"""
    global _engine, _session_factory, _db_ok
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None
    _db_ok = None


def mask_dsn(dsn: str) -> str:
    """日志里只保留用户名和主机，密码一律替换。"""
    if "@" in dsn and "//" in dsn:
        head, tail = dsn.split("//", 1)
        creds, host = tail.split("@", 1)
        user = creds.split(":", 1)[0]
        return f"{head}//{user}:***@{host}"
    return dsn


@contextmanager
def session_scope() -> Iterator[Session]:
    if _session_factory is None:
        init_db()
    if _session_factory is None:
        raise RuntimeError("审计库不可用")
    db = _session_factory()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
