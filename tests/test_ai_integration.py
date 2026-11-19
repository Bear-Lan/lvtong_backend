"""AI契约、业务判型、推荐分页与裁剪/OCR回归。禁止真实网络。"""
import base64
import io
import unittest
from unittest.mock import Mock, patch

import cv2
import numpy as np
from flask import Flask
from sqlalchemy import create_engine, text

from app.services import ai_client, goods_detect, truck_real_detect, mask_loading_rate
from app.api.imaging import imaging_api
from app.api.file import file_api
from app.api.dictionary import dict_api
from app.extensions.auth import create_token
from app.db.product import DBProduct


class AIIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.net = patch('socket.socket', side_effect=AssertionError('禁止真实网络'))
        self.net.start(); self.addCleanup(self.net.stop)
        self.app = Flask(__name__)
        for bp in (imaging_api, file_api, dict_api): self.app.register_blueprint(bp)
        self.client = self.app.test_client()
        self.headers = {'Authorization': 'Bearer ' + create_token({'username': 'test', 'role': 0})}
        _, encoded = cv2.imencode('.png', np.full((40, 60, 3), 200, np.uint8))
        self.image = encoded.tobytes()

    def test_client_auth_tls_timeout_redirect_and_strict_business_code(self):
        cfg = {'base_url': 'https://example.test', 'username': 'user', 'password': 'secret', 'ca_bundle': 'public.pem', 'proxy': ''}
        with patch.object(ai_client, 'settings', return_value=cfg), patch.object(ai_client.requests, 'Session') as factory:
            session = factory.return_value.__enter__.return_value
            response = session.request.return_value
            response.status_code=200;response.headers={}
            response.json.return_value={'code':1,'data':[]}
            self.assertEqual(ai_client.request_json('/detect',files={})['code'],1)
            kwargs=session.request.call_args.kwargs
            self.assertEqual(kwargs['auth'], ('user','secret'))
            self.assertEqual(kwargs['verify'],'public.pem')
            self.assertFalse(kwargs['allow_redirects'])
            self.assertEqual(kwargs['timeout'],(15,60))
            for code in (0, '1', True, None):
                response.json.return_value={'code':code}
                with self.assertRaises(ai_client.AIError): ai_client.request_json('/detect',files={})
            response.status_code=401
            with self.assertRaisesRegex(ai_client.AIError,'HTTP 401'): ai_client.request_json('/detect',files={})

    def test_axles_use_old_plate_rules_and_container_mapping(self):
        for wheels,color,plate,expected in [(2,'0','鄂A12345','11'),(2,'4','鄂A12345','11'),(2,'1','鄂A12345','12'),(2,'','鄂A12345',''),(2,'0','',''),(3,'','','13'),(9,'','','16')]:
            self.assertEqual(truck_real_detect.wheel_count_to_truck_type(wheels,color,plate)[0],expected)
        with patch.object(truck_real_detect,'call_ai_truck_real',return_value={'code':1,'wheel_count':4,'cratetype':'4.1'}), patch('app.db.dictionary.DBDictionary'):
            result=truck_real_detect.detect_truck_and_container(self.image)
        self.assertEqual((result['vehicle_type'],result['vehicle_container_type']),('14','4.1'))

    def test_goods_class_id_string_deduplicates_and_orders_confidence(self):
        detections=[{'class_id':'001','score':0.4},{'class_id':'002','score':0.8},{'class_id':'001','score':0.9},{'class_id':'bad','label':'苹果','score':1},{'class_id':'002','score':float('nan')}]
        detections.append({'class_id':'pingguo','score':0.85})
        rows={'001':{'product_code':'001','variety_name':'香蕉'},'002':{'product_code':'002','variety_name':'苹果'}}
        with patch.object(ai_client,'request_json',return_value={'code':1,'data':{'detections':detections}}) as req, patch.object(DBProduct,'getProductByCode',side_effect=rows.get), patch.object(DBProduct,'getProductByName',return_value=[rows['002']]):
            goods=goods_detect.detect_product_real(self.image)['goods']
        self.assertEqual([(g['code'],g['score']) for g in goods],[('001',0.9),('002',0.85)])
        self.assertEqual(req.call_args.args[0],'/v1/goods/real')

    def test_recommendations_sort_before_paging_and_preserve_filter(self):
        engine=create_engine('sqlite://')
        self.addCleanup(engine.dispose)
        with engine.begin() as conn:
            conn.execute(text('CREATE TABLE agricultural_products (id INTEGER, product_code TEXT, product_type TEXT)'))
            for n in range(30):conn.execute(text('INSERT INTO agricultural_products VALUES (:id,:code,:type)'),{'id':n,'code':str(n),'type':'水果' if n%2 else '蔬菜'})
        def rows(sql,params):
            with engine.connect() as conn:return [dict(r) for r in conn.execute(text(sql),params).mappings()]
        with patch.object(DBProduct,'_rows',side_effect=rows):
            db=DBProduct()
            first=db.getProductsWithFilter(page=1,page_size=20,recommend_codes=['29','25'])
            second=db.getProductsWithFilter(page=2,page_size=20,recommend_codes=['29','25'])
            filtered=db.getProductsWithFilter(product_type='蔬菜',recommend_codes=['29','28'])
        self.assertEqual([p['product_code'] for p in first[:2]],['29','25'])
        self.assertEqual(len({p['product_code'] for p in first+second}),30)
        self.assertEqual(filtered[0]['product_code'],'28')
        self.assertTrue(all(p['product_type']=='蔬菜' for p in filtered))

    def test_crop_supports_two_images_and_requires_auth(self):
        def files():return {'crop_image1':(io.BytesIO(self.image),'main.png'),'crop_image2':(io.BytesIO(self.image),'hang.png')}
        self.assertEqual(self.client.post('/api/license/ai-crop',data=files()).status_code,401)
        crop={'format':'png','base64':base64.b64encode(self.image).decode()}
        with patch.object(ai_client,'request_json',return_value={'code':1,'crop_result_image1':crop,'crop_result_image2':crop}) as req:
            result=self.client.post('/api/license/ai-crop',data=files(),headers=self.headers)
        self.assertEqual(result.status_code,200)
        self.assertEqual(result.json['data']['mode'],'stitch')
        self.assertEqual(req.call_args.kwargs['data'],{'task':'truck_real'})
        raw=base64.b64decode(result.json['data']['imageDataUrl'].split(',')[1])
        self.assertEqual(cv2.imdecode(np.frombuffer(raw,np.uint8),1).shape[:2],(80,60))

    def test_ocr_fields_weight_hint_and_failed_upstream(self):
        fields=[{'key':k,'value':v} for k,v in [('号牌号码','鄂A12345'),('核定载质量','10000'),('整备质量','5000'),('总质量','16000')]]
        with patch.object(ai_client,'request_json',return_value={'code':1,'data':fields}):
            result=self.client.post('/api/imaging/ocr/driving-license',headers=self.headers,data={'image':(io.BytesIO(self.image),'main.png')})
        self.assertEqual(result.json['data']['weight_min'],13000)
        self.assertEqual(result.json['data']['weight_max'],16800)
        with patch.object(ai_client,'request_json',side_effect=ai_client.AIError('AI未识别成功')):
            result=self.client.post('/api/imaging/ocr/driving-license',headers=self.headers,data={'image':(io.BytesIO(self.image),'main.png')})
        self.assertEqual(result.status_code,502)
        self.assertNotEqual(result.json['code'],0)

    def test_mask_prefers_new_union_and_disabled_xray_never_calls_upstream(self):
        self.assertEqual(mask_loading_rate.extract_mask_b64({'mask_image':{'base64':'union'},'data':[{'mask':{'base64':'old'}}]})[0],'union')
        with patch.object(ai_client,'request_json',side_effect=AssertionError('不应调用')):
            result=self.client.post('/api/imaging/ai-goods-xray',headers=self.headers)
        self.assertEqual(result.status_code,409)

    def test_ai_source_returns_paired_original_and_expiry_is_explicit(self):
        from app.services import temp_image_cache as cache
        _, preview = cv2.imencode('.png',np.zeros((4,6,3),np.uint8))
        reference=cache.put_preview_with_original(self.image,preview.tobytes())
        response=self.client.get('/api/imaging/ai-source/'+reference.rsplit('/',1)[1],headers=self.headers)
        self.assertEqual(response.data,self.image)
        self.assertEqual(self.client.get('/api/imaging/ai-source/missing',headers=self.headers).status_code,404)

if __name__=='__main__': unittest.main()
