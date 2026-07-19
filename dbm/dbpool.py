"""PostgreSQL 数据库连接池"""
import os
from psycopg2 import pool
from config import DB_CONFIG

# 修复中文 Windows 下 psycopg2 编码问题
os.environ.setdefault('PGCLIENTENCODING', 'UTF8')

_pg_pool = None


def _get_pool():
    """延迟初始化连接池（避免导入时就连接数据库）"""
    global _pg_pool
    if _pg_pool is None:
        _pg_pool = pool.SimpleConnectionPool(
            minconn=5,
            maxconn=20,
            **DB_CONFIG
        )
    return _pg_pool


class DBPool:
    """数据库连接池基类"""

    def getConn(self):
        return _get_pool().getconn()

    def releaseConn(self, conn):
        if conn:
            _get_pool().putconn(conn)
