"""为本机gcis新增点云顶部/侧面图字段；现场由容器发布流程执行同一SQL。"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import DB_CONFIG
from app.db.engine import engine

if __name__ == '__main__':
    if DB_CONFIG['host'] not in ('127.0.0.1', 'localhost', '::1') or DB_CONFIG['dbname'] != 'gcis':
        raise SystemExit('本脚本只允许修改本机gcis数据库')
    connection = engine.raw_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute((Path(__file__).resolve().parents[1] / 'migrations/20261114_radar_3d_views.sql').read_text(encoding='utf8'))
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    print('本机点云顶部、侧面图片字段迁移完成')
