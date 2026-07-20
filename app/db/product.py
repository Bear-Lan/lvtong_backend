"""农产品数据库操作层

参考 Qt ProductDatabase 实现。
"""
from app.db.pool import DBPool


class DBProduct(DBPool):
    """农产品 CRUD"""

    def getAllProducts(self) -> list[dict]:
        conn = self.getConn()
        try:
            with conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT * FROM agricultural_products ORDER BY variety_name_pinyin"
                )
                return [self._row_to_dict(r, cursor) for r in cursor.fetchall()]
        finally:
            self.releaseConn(conn)

    def getProductsCount(self, product_type='', variety_name='',
                         variety_name_pinyin='', aliases='') -> int:
        sql = "SELECT COUNT(*) FROM agricultural_products WHERE 1=1"
        params = []
        sql, params = self._append_filters(sql, params, product_type, variety_name,
                                           variety_name_pinyin, aliases)

        conn = self.getConn()
        try:
            with conn:
                cursor = conn.cursor()
                cursor.execute(sql, params)
                return cursor.fetchone()[0]
        finally:
            self.releaseConn(conn)

    def getProductsWithFilter(self, product_type='', variety_name='',
                              variety_name_pinyin='', aliases='',
                              page=1, page_size=50) -> list[dict]:
        sql = "SELECT * FROM agricultural_products WHERE 1=1"
        params = []
        sql, params = self._append_filters(sql, params, product_type, variety_name,
                                           variety_name_pinyin, aliases)
        sql += " ORDER BY variety_name_pinyin LIMIT %s OFFSET %s"
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

    def getProductByCode(self, product_code: str) -> dict | None:
        conn = self.getConn()
        try:
            with conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT * FROM agricultural_products WHERE product_code = %s",
                    (product_code,)
                )
                row = cursor.fetchone()
                return self._row_to_dict(row, cursor) if row else None
        finally:
            self.releaseConn(conn)

    def getProductByName(self, product_name: str) -> list[dict]:
        """按品种名称模糊查询"""
        conn = self.getConn()
        try:
            with conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT * FROM agricultural_products WHERE variety_name = %s",
                    (product_name,)
                )
                return [self._row_to_dict(r, cursor) for r in cursor.fetchall()]
        finally:
            self.releaseConn(conn)

    def getVarietyNameByProductCode(self, product_code: str) -> str:
        conn = self.getConn()
        try:
            with conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT variety_name FROM agricultural_products WHERE product_code = %s",
                    (product_code,)
                )
                row = cursor.fetchone()
                return row[0] if row else ''
        finally:
            self.releaseConn(conn)

    @staticmethod
    def _append_filters(sql, params, product_type, variety_name, pinyin, aliases):
        if product_type:
            sql += " AND product_type = %s"
            params.append(product_type)
        if variety_name:
            sql += " AND variety_name LIKE %s"
            params.append(f"%{variety_name}%")
        if pinyin:
            sql += " AND variety_name_pinyin LIKE %s"
            params.append(f"%{pinyin.lower()}%")
        if aliases:
            sql += " AND aliases::text LIKE %s"
            params.append(f"%{aliases}%")
        return sql, params

    @staticmethod
    def _row_to_dict(row, cursor) -> dict:
        cols = [desc[0] for desc in cursor.description]
        d = dict(zip(cols, row))
        for ts in ('created_time', 'updated_time'):
            if d.get(ts):
                d[ts] = d[ts].isoformat()
        return d
