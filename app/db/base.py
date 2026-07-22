"""数据库基类 — 提供连接和行转字典的通用方法

SQLAlchemy 2.x 要求所有执行都通过 Connection 对象。
自动连接（无 conn 参数时）内部创建短连接，用完释放。
显式传 conn 参数时复用已有连接（用于事务内多次查询）。
"""
from contextlib import contextmanager
from sqlalchemy import text, Connection
from app.db.engine import engine


class BaseRepo:

    @staticmethod
    @contextmanager
    def _tx():
        """事务上下文管理器 — 写操作专用

        with self._tx() as conn:
            self._exec("UPDATE ...", {...}, conn=conn)
            self._one("SELECT ...", {...}, conn=conn)
        # 正常退出自动 commit，异常自动 rollback
        """
        with engine.connect() as conn:
            with conn.begin():
                yield conn

    # ---- 内部：执行 SQL 的核心 ----

    @staticmethod
    def _execute(sql: str, params: dict | None = None, *, conn: Connection | None):
        """在指定连接上执行 SQL，返回 CursorResult"""
        t = text(sql)
        if conn is not None:
            return conn.execute(t, params or {})
        with engine.connect() as c:
            return c.execute(t, params or {})

    # ---- 公共查询方法 ----

    @classmethod
    def _rows(cls, sql: str, params: dict | None = None,
              *, conn: Connection | None = None) -> list[dict]:
        """多行查询 → list[dict]"""
        result = cls._execute(sql, params, conn=conn)
        cols = result.keys()
        return [dict(zip(cols, row)) for row in result.fetchall()]

    @classmethod
    def _one(cls, sql: str, params: dict | None = None,
             *, conn: Connection | None = None) -> dict | None:
        """单行查询 → dict | None"""
        result = cls._execute(sql, params, conn=conn)
        row = result.fetchone()
        if row is None:
            return None
        return dict(zip(result.keys(), row))

    @classmethod
    def _scalar(cls, sql: str, params: dict | None = None,
                *, conn: Connection | None = None):
        """标量查询 → 单个值（如 COUNT）"""
        result = cls._execute(sql, params, conn=conn)
        return result.scalar()

    @classmethod
    def _exec(cls, sql: str, params: dict | None = None,
              *, conn: Connection | None = None) -> int:
        """写操作 → 影响行数"""
        result = cls._execute(sql, params, conn=conn)
        return result.rowcount
