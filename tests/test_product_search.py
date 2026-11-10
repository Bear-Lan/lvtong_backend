"""农产品关键词、首字母大小写、计数/分页共用筛选；不连接数据库。"""
import unittest
from unittest.mock import patch
from flask import Flask
from app.api.dictionary import dict_api
from app.db.product import DBProduct
from app.extensions.auth import create_token


class ProductSearchTest(unittest.TestCase):
    def test_keyword_searches_name_initials_aliases_with_same_count_and_page_filter(self):
        db = DBProduct()
        with patch.object(db, '_scalar', return_value=3) as count, patch.object(db, '_rows', return_value=[]) as rows:
            self.assertEqual(db.getProductsCount('新鲜蔬菜', keyword=' bC '), 3)
            db.getProductsWithFilter('新鲜蔬菜', page=2, page_size=2, keyword=' bC ')
        count_sql, count_params = count.call_args.args
        rows_sql, rows_params = rows.call_args.args
        self.assertEqual(count_sql.split(' WHERE ')[1], rows_sql.split(' WHERE ')[1].split(' ORDER BY ')[0])
        self.assertEqual(count_params, {k: v for k, v in rows_params.items() if k not in ('limit', 'offset')})
        self.assertIn('(variety_name ILIKE :keyword OR variety_name_pinyin ILIKE :keyword OR aliases::text ILIKE :keyword)', rows_sql)
        self.assertEqual(rows_params, {'ptype': '新鲜蔬菜', 'keyword': '%bC%', 'limit': 2, 'offset': 2})

    def test_legacy_pinyin_filter_matches_uppercase_database_initials(self):
        sql, params = DBProduct._append_filters('SELECT 1 WHERE 1=1', '', '', ' bC ', '')
        self.assertIn('variety_name_pinyin ILIKE :pinyin', sql)
        self.assertEqual(params['pinyin'], '%bC%')

    def test_keyword_is_bound_as_data_instead_of_sql(self):
        keyword = "BC' OR 1=1 --"
        sql, params = DBProduct._append_filters('SELECT 1 WHERE 1=1', '', '', '', '', keyword)
        self.assertNotIn(keyword, sql)
        self.assertEqual(params['keyword'], '%' + keyword + '%')

    def test_api_separates_generic_keyword_from_explicit_name_and_preserves_filters(self):
        app = Flask(__name__);app.register_blueprint(dict_api)
        headers = {'Authorization': 'Bearer ' + create_token({'username': 'product-search-test'})}
        query = {'keyword': ' BC ', 'product_type': '新鲜蔬菜', 'pinyin': 'B', 'variety_name': '白菜', 'page': '2', 'page_size': '2'}
        with patch('app.api.dictionary.DBProduct') as repo:
            repo.return_value.getProductsCount.return_value = 3
            repo.return_value.getProductsWithFilter.return_value = [{'variety_name': '大白菜'}]
            result = app.test_client().get('/api/dict/products', query_string=query, headers=headers)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json['data']['total'], 3)
        repo.return_value.getProductsCount.assert_called_once_with('新鲜蔬菜', '白菜', 'B', keyword='BC')
        repo.return_value.getProductsWithFilter.assert_called_once_with('新鲜蔬菜', '白菜', 'B', page=2, page_size=2, keyword='BC')


if __name__ == '__main__': unittest.main()
