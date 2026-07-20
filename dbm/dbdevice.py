"""设备数据库操作层

参考 Qt DeviceDatabase 实现。
"""
import json
from dbm.dbpool import DBPool


class DBDevice(DBPool):
    """设备 CRUD"""

    def getAllDevices(self) -> list[dict]:
        conn = self.getConn()
        try:
            with conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT * FROM devices ORDER BY device_type, device_name"
                )
                return [self._row_to_dict(r, cursor) for r in cursor.fetchall()]
        finally:
            self.releaseConn(conn)

    def getDevice(self, device_id: str) -> dict | None:
        conn = self.getConn()
        try:
            with conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT * FROM devices WHERE device_id = %s", (device_id,)
                )
                row = cursor.fetchone()
                return self._row_to_dict(row, cursor) if row else None
        finally:
            self.releaseConn(conn)

    def getDevicesByType(self, device_type: str) -> list[dict]:
        conn = self.getConn()
        try:
            with conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT * FROM devices WHERE device_type = %s", (device_type,)
                )
                return [self._row_to_dict(r, cursor) for r in cursor.fetchall()]
        finally:
            self.releaseConn(conn)

    def updateDeviceStatus(self, device_id: str, status: int) -> bool:
        conn = self.getConn()
        try:
            with conn:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE devices SET status = %s, updated_time = NOW() WHERE device_id = %s",
                    (status, device_id)
                )
                return cursor.rowcount > 0
        finally:
            self.releaseConn(conn)

    def updateLastConnectTime(self, device_id: str) -> bool:
        conn = self.getConn()
        try:
            with conn:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE devices SET last_connect_time = NOW() WHERE device_id = %s",
                    (device_id,)
                )
                return cursor.rowcount > 0
        finally:
            self.releaseConn(conn)

    def updateDeviceConfig(self, device_id: str, config: dict) -> bool:
        conn = self.getConn()
        try:
            with conn:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE devices SET config = %s, updated_time = NOW() WHERE device_id = %s",
                    (json.dumps(config, ensure_ascii=False), device_id)
                )
                return cursor.rowcount > 0
        finally:
            self.releaseConn(conn)

    @staticmethod
    def _row_to_dict(row, cursor) -> dict:
        cols = [desc[0] for desc in cursor.description]
        d = dict(zip(cols, row))
        for ts in ('last_connect_time', 'created_time', 'updated_time'):
            if d.get(ts):
                d[ts] = d[ts].isoformat()
        if isinstance(d.get('config'), str):
            try:
                d['config'] = json.loads(d['config'])
            except (json.JSONDecodeError, TypeError):
                d['config'] = {}
        return d
