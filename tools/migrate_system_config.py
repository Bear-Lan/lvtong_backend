"""本机gcis参数表初始化及中文说明升级，不启用设备、不重启服务。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import DB_CONFIG
from app.db.engine import engine
from app.services.system_config import SystemConfigStore

if __name__ == '__main__':
    if DB_CONFIG['host'] not in ('127.0.0.1', 'localhost', '::1') or DB_CONFIG['dbname'] != 'gcis':
        raise SystemExit('只允许迁移本机gcis')
    store = SystemConfigStore(engine)
    store.initialize()
    print(f'本机config表已初始化并更新中文说明，共{len(store.rows())}项；未发设备命令、未重启任何服务')
