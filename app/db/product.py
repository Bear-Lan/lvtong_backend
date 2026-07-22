"""农产品 agricultural_products"""
from app.db.base import BaseRepo


class DBProduct(BaseRepo):

    def getAllProducts(self) -> list[dict]:
        return self._rows(
            "SELECT * FROM agricultural_products ORDER BY variety_name_pinyin"
        )

    def getProductsCount(self, product_type='', variety_name='',
                         variety_name_pinyin='', aliases='') -> int:
        sql, params = self._append_filters(
            "SELECT COUNT(*) FROM agricultural_products WHERE 1=1",
            product_type, variety_name, variety_name_pinyin, aliases
        )
        return self._scalar(sql, params)

    def getProductsWithFilter(self, product_type='', variety_name='',
                              variety_name_pinyin='', aliases='',
                              page=1, page_size=50) -> list[dict]:
        sql, params = self._append_filters(
            "SELECT * FROM agricultural_products WHERE 1=1",
            product_type, variety_name, variety_name_pinyin, aliases
        )
        sql += " ORDER BY id LIMIT :limit OFFSET :offset"
        params['limit'] = page_size
        params['offset'] = (page - 1) * page_size
        return self._rows(sql, params)

    def getProductByCode(self, product_code: str) -> dict | None:
        return self._one(
            "SELECT * FROM agricultural_products WHERE product_code = :code",
            {'code': product_code},
        )

    def getProductByName(self, product_name: str) -> list[dict]:
        return self._rows(
            "SELECT * FROM agricultural_products WHERE variety_name = :name",
            {'name': product_name},
        )

    def getVarietyNameByProductCode(self, product_code: str) -> str:
        row = self._one(
            "SELECT variety_name FROM agricultural_products WHERE product_code = :code",
            {'code': product_code},
        )
        return row['variety_name'] if row else ''

    @staticmethod
    def _append_filters(sql: str, product_type: str, variety_name: str,
                        pinyin: str, aliases: str) -> tuple[str, dict]:
        params: dict = {}
        if product_type:
            sql += " AND product_type = :ptype"
            params['ptype'] = product_type
        if variety_name:
            sql += " AND variety_name LIKE :vname"
            params['vname'] = f'%{variety_name}%'
        if pinyin:
            sql += " AND variety_name_pinyin LIKE :pinyin"
            params['pinyin'] = f'%{pinyin.lower()}%'
        if aliases:
            sql += " AND aliases::text LIKE :aliases"
            params['aliases'] = f'%{aliases}%'
        return sql, params
