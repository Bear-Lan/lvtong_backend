"""操作日志 operation_logs"""
from app.db.base import BaseRepo


class DBLog(BaseRepo):

    def addLog(self, level: str, message: str, category: str = '',
               operator: str = '', device_id: str = '') -> bool:
        try:
            with self._tx() as conn:
                self._exec(
                    "INSERT INTO operation_logs (level, message, category, "
                    "operator, device_id) VALUES (:lv, :msg, :cat, :op, :dev)",
                    {'lv': level, 'msg': message, 'cat': category,
                     'op': operator, 'dev': device_id},
                    conn=conn,
                )
            return True
        except Exception:
            return False

    def getLogs(self, level='', category='', operator='',
                start_time=None, end_time=None,
                page=1, page_size=50) -> list[dict]:
        sql, params = _build_log_query(level, category, operator,
                                       start_time, end_time)
        sql += " ORDER BY timestamp DESC LIMIT :limit OFFSET :offset"
        params['limit'] = page_size
        params['offset'] = (page - 1) * page_size

        rows = self._rows(sql, params)
        for d in rows:
            if d.get('timestamp') and hasattr(d['timestamp'], 'isoformat'):
                d['timestamp'] = d['timestamp'].isoformat()
        return rows

    def getLogsCount(self, level='', category='', operator='',
                     start_time=None, end_time=None) -> int:
        sql, params = _build_log_query(level, category, operator,
                                       start_time, end_time)
        return self._scalar(
            sql.replace("SELECT *", "SELECT COUNT(*)"),
            params,
        )


def _build_log_query(level='', category='', operator='',
                     start_time=None, end_time=None):
    sql = "SELECT * FROM operation_logs WHERE 1=1"
    params: dict = {}
    if level:
        sql += " AND level = :level"
        params['level'] = level
    if category:
        sql += " AND category = :cat"
        params['cat'] = category
    if operator:
        sql += " AND operator LIKE :op"
        params['op'] = f'%{operator}%'
    if start_time:
        sql += " AND timestamp >= :st"
        params['st'] = start_time
    if end_time:
        sql += " AND timestamp <= :et"
        params['et'] = end_time
    return sql, params
