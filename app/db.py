"""数据库：审计库 fm_agent（MySQL）。

单实例试点用同步 SQLAlchemy + 短事务；异步端点里通过 anyio.to_thread 调用。
数据库不可用时降级：会话不落库、审计丢失，但对话功能不受影响。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.models import Base

logger = logging.getLogger(__name__)

_engine = None
_session_factory: sessionmaker | None = None
_db_ok: bool | None = None


def init_db() -> bool:
    """建连接池并建表（幂等）。返回是否可用。"""
    global _engine, _session_factory, _db_ok
    if _session_factory is not None:
        return bool(_db_ok)
    try:
        _engine = create_engine(
            settings.mysql_dsn,
            pool_pre_ping=True,
            pool_recycle=1800,
            pool_size=5,
            max_overflow=5,
            future=True,
        )
        Base.metadata.create_all(_engine)
        _session_factory = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
        _db_ok = True
        logger.info("审计库就绪：%s", _mask(settings.mysql_dsn))
    except Exception as exc:  # noqa: BLE001 - 数据库不可用不应阻止服务启动
        _db_ok = False
        logger.warning("审计库不可用，会话与审计降级：%s", exc)
    return bool(_db_ok)


def db_available() -> bool:
    if _session_factory is None:
        init_db()
    return bool(_db_ok)


def _mask(dsn: str) -> str:
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
