"""设备 devices"""
import json
from app.db.base import BaseRepo


class DBDevice(BaseRepo):

    def getAllDevices(self) -> list[dict]:
        rows = self._rows(
            "SELECT * FROM devices ORDER BY device_type, device_name"
        )
        for d in rows:
            _normalize(d)
        return rows

    def getDevice(self, device_id: str) -> dict | None:
        d = self._one(
            "SELECT * FROM devices WHERE device_id = :id",
            {'id': device_id},
        )
        if d:
            _normalize(d)
        return d

    def getDevicesByType(self, device_type: str) -> list[dict]:
        rows = self._rows(
            "SELECT * FROM devices WHERE device_type = :type",
            {'type': device_type},
        )
        for d in rows:
            _normalize(d)
        return rows

    def updateDeviceStatus(self, device_id: str, status: int) -> bool:
        with self._tx() as conn:
            return self._exec(
                "UPDATE devices SET status=:st, updated_time=NOW() "
                "WHERE device_id = :id",
                {'st': status, 'id': device_id}, conn=conn,
            ) > 0

    def updateLastConnectTime(self, device_id: str) -> bool:
        with self._tx() as conn:
            return self._exec(
                "UPDATE devices SET last_connect_time=NOW() WHERE device_id = :id",
                {'id': device_id}, conn=conn,
            ) > 0

    def updateDeviceConfig(self, device_id: str, config: dict) -> bool:
        with self._tx() as conn:
            return self._exec(
                "UPDATE devices SET config=:cfg, updated_time=NOW() "
                "WHERE device_id = :id",
                {'cfg': json.dumps(config, ensure_ascii=False), 'id': device_id},
                conn=conn,
            ) > 0


def _normalize(d: dict):
    """后处理：时间转 isoformat，config 从 JSON 字符串解析为 dict"""
    for ts in ('last_connect_time', 'created_time', 'updated_time'):
        if d.get(ts) and hasattr(d[ts], 'isoformat'):
            d[ts] = d[ts].isoformat()
    if isinstance(d.get('config'), str):
        try:
            d['config'] = json.loads(d['config'])
        except (json.JSONDecodeError, TypeError):
            d['config'] = {}
