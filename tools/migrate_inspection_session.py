"""本轮仅授权迁移本机gcis，拒绝远程数据库。"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import DB_CONFIG, IMAGE_STORAGE_ROOT
from app.db.engine import engine
from app.services.inspection_session import InspectionSessionStore

if __name__ == '__main__':
    if DB_CONFIG['host'] not in ('127.0.0.1', 'localhost', '::1') or DB_CONFIG['dbname'] != 'gcis':
        raise SystemExit('本脚本只允许修改本机gcis数据库')
    connection = engine.raw_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute((Path(__file__).resolve().parents[1] / 'migrations/20261113_inspection_session_radar.sql').read_text(encoding='utf8'))
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    InspectionSessionStore(engine, IMAGE_STORAGE_ROOT).initialize()
    print('本机检测同步与雷达图片字段迁移完成')
