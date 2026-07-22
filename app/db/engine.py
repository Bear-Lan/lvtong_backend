"""SQLAlchemy 引擎

- psycopg2 + QueuePool（pool_size=20, max_overflow=5）
- pool_pre_ping 每次借出前检查连接有效性
- 所有 DB 类通过 app.db.base.BaseRepo 统一使用此引擎
"""
from sqlalchemy import create_engine, MetaData, Table
from config import DB_CONFIG

DATABASE_URL = (
    f"postgresql+psycopg2://{DB_CONFIG['user']}:{DB_CONFIG['password']}"
    f"@{DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['dbname']}"
)

engine = create_engine(
    DATABASE_URL,
    pool_size=20,
    max_overflow=5,
    pool_pre_ping=True,
    # echo=False,  # SQL 调试日志，生产环境务必关闭
)

meta = MetaData()


def table(name: str) -> Table:
    """延迟反射：按需从数据库读取真实列定义"""
    return Table(name, meta, autoload_with=engine, extend_existing=True)
