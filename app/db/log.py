"""操作日志数据库操作层

参考 Qt LogDatabase 实现。
"""
from app.db.pool import DBPool


class DBLog(DBPool):
    """操作日志读写"""

    def addLog(self, level: str, message: str, category: str = '',
               operator: str = '', device_id: str = '') -> bool:
        conn = self.getConn()
        try:
            with conn:
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO operation_logs (level, message, category, operator, device_id) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (level, message, category, operator, device_id)
                )
                return True
        except Exception:
            return False
        finally:
            self.releaseConn(conn)

    def getLogs(self, level='', category='', operator='',
                start_time=None, end_time=None,
                page=1, page_size=50) -> list[dict]:
        sql = "SELECT * FROM operation_logs WHERE 1=1"
        params = []

        if level:
            sql += " AND level = %s"
            params.append(level)
        if category:
            sql += " AND category = %s"
            params.append(category)
        if operator:
            sql += " AND operator LIKE %s"
            params.append(f"%{operator}%")
        if start_time:
            sql += " AND timestamp >= %s"
            params.append(start_time)
        if end_time:
            sql += " AND timestamp <= %s"
            params.append(end_time)

        sql += " ORDER BY timestamp DESC LIMIT %s OFFSET %s"
        params.append(page_size)
        params.append((page - 1) * page_size)

        conn = self.getConn()
        try:
            with conn:
                cursor = conn.cursor()
                cursor.execute(sql, params)
                return [self._row_to_dict(r, cursor) for r in cursor.fetchall()]
        finally:
            self.releaseConn(conn)

    def getLogsCount(self, level='', category='', operator='',
                     start_time=None, end_time=None) -> int:
        sql = "SELECT COUNT(*) FROM operation_logs WHERE 1=1"
        params = []

        if level:
            sql += " AND level = %s"
            params.append(level)
        if category:
            sql += " AND category = %s"
            params.append(category)
        if operator:
            sql += " AND operator LIKE %s"
            params.append(f"%{operator}%")
        if start_time:
            sql += " AND timestamp >= %s"
            params.append(start_time)
        if end_time:
            sql += " AND timestamp <= %s"
            params.append(end_time)

        conn = self.getConn()
        try:
            with conn:
                cursor = conn.cursor()
                cursor.execute(sql, params)
                return cursor.fetchone()[0]
        finally:
            self.releaseConn(conn)

    @staticmethod
    def _row_to_dict(row, cursor) -> dict:
        cols = [desc[0] for desc in cursor.description]
        d = dict(zip(cols, row))
        if d.get('timestamp'):
            d['timestamp'] = d['timestamp'].isoformat()
        return d
