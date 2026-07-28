"""移动 app 文件目录监听器 — 对齐 LvTongPro::JsonFolderWatcher

工作流（对齐 Qt 平台）：
  1. 移动 app 拍照 / 扫码 → 生成 JSON 文件到 APP_PHOTO_DIR 目录
  2. 图片文件在 {json文件目录}/{json文件名}/ 子目录
  3. 监听器发现新 JSON 文件时：
     a. 读 JSON 内容
     b. 解析图片文件名（carHeadImg / carBodyImg / carTailImg / goodsImg1/2/3 / passCodeImg / vehicleLicenseImg1/2 等）
     c. 复制图片到 uploads 目录
     d. 解析 passcodeQRCodeInfo 14 字段
     e. 推 WS 事件给前端（mobile_upload + mobile_passcode）
  4. 前端 WS 订阅：更新 captureThumbs / licensePaths / form.phone / form.goods_type / passcode_* 字段

JSON 格式（对齐 LvTongPhotoHttp2/JsonUtil.CaptureImgEntity）：
{
  "carHeadImg": "carHeadImg_xxx.jpeg",
  "carBodyImg": "carBodyImg_xxx.jpeg",
  "carTailImg": "carTailImg_xxx.jpeg",
  "goodsImg1/2/3": "goodsImgN_xxx.jpeg",
  "evidenceImg_Optional": "evidenceImg_Optional_xxx.jpeg",
  "goodsImg_Chase": "goodsImg_Chase_xxx.jpeg",
  "evidenceImg_Chase": "evidenceImg_Chase_xxx.jpeg",
  "passCodeImg": "passCodeImg_xxx.jpeg",
  "vehicleLicenseImg1/2": "vehicleLicenseImgN_xxx.jpeg",
  "passcodeQRCodeInfo": "vehicleId|enStationId|exStationId|enWeight|exWeight|mediaType|transactionId|passId|exTime|transPayType|fee|payFee|vehicleSign|provinceCount",
  "phone": "11位手机号",
  "plate_number_gc": "...",
  "goods_type": "..."
}
"""
import json
import logging
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger('mobile_watcher')

# 监听目录：环境变量 MOCK_PHOTO_DIR 覆盖，默认 D:/soft/FtpLvTong（对齐 Qt APP_PHOTO_DIR）
APP_PHOTO_DIR = Path(os.environ.get('MOCK_PHOTO_DIR', 'D:/soft/FtpLvTong'))

# 图片存到后端 uploads 目录（前端可通过 /api/image?path= 访问）
UPLOAD_DIR = Path(os.environ.get('MOBILE_UPLOAD_DIR', './uploads/mobile'))


def _ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def _store_image(imgs_dir: Path, filename: str, category: str, session_id: str) -> Optional[str]:
    """把 imgs_dir/filename 复制到 uploads/<session_id>/<category>/filename

    返回前端可访问的 URL（相对路径 /uploads/mobile/...）
    """
    if not filename:
        return None
    src = imgs_dir / filename
    if not src.is_file():
        logger.warning(f'[WATCHER] 图片不存在: {src}')
        return None

    dest_dir = UPLOAD_DIR / session_id / category
    _ensure_dir(dest_dir)
    dest = dest_dir / filename
    try:
        shutil.copy2(src, dest)
    except Exception as e:
        logger.warning(f'[WATCHER] 复制图片失败 {src} -> {dest}: {e}')
        return None
    # 返回前端可用的路径（绝对路径，前端通过 /api/image?path= 访问）
    return str(dest.resolve())


def _generate_qrcode(content: str, session_id: str) -> Optional[str]:
    """对齐 Qt QZXing::encodeData — 用 passcodeQRCodeInfo 14 字段字符串生成 QR 码图

    保存到 uploads/<session_id>/passcode/passCodeImg.png
    返回绝对路径

    使用 segno（纯 Python，无 PIL 依赖），输出 PNG（segno 不支持 JPEG）。
    """
    if not content:
        return None
    try:
        import segno
        qr = segno.make(content, error='m')
        dest_dir = UPLOAD_DIR / session_id / 'passcode'
        _ensure_dir(dest_dir)
        dest = dest_dir / 'passCodeImg.png'
        qr.save(str(dest), scale=8, border=4)
        logger.info(f'[WATCHER] QR 码生成: {dest}')
        return str(dest.resolve())
    except Exception as e:
        logger.warning(f'[WATCHER] QR 码生成失败: {e}')
        return None


def _parse_passcode(code_str: str) -> dict:
    """解析 passcodeQRCodeInfo 14 字段（对齐 Qt PassCodeUtil::GetPassCodeInfoByCodeStr）

    格式：vehicleId|enStationId|exStationId|enWeight|exWeight|mediaType|transactionId|passId|exTime|transPayType|fee|payFee|vehicleSign|provinceCount
    """
    out: dict[str, Any] = {
        'valid': False,
        'vehicleId': '',
        'vehicleDisplayId': '',
        'vehicleColorName': '',
        'enStationId': '',
        'exStationId': '',
        'enWeight': '',
        'exWeight': '',
        'mediaTypeId': 0,
        'mediaTypeStr': '',
        'transactionId': '',
        'passId': '',
        'exTime': '',
        'transPayTypeId': 0,
        'transPayTypeStr': '',
        'fee': '',
        'payFee': '',
        'vehicleSignId': 0,
        'vehicleSignStr': '',
        'provinceCount': '',
    }
    if not code_str:
        return out

    raw = code_str.split('|')
    if len(raw) < 14:
        return out

    parts = [p.strip() for p in raw[:14]]
    vehicle_id = parts[0]

    # 去掉 _数字 后缀（vehicleColorName）— 对齐 Qt 正则 `_[0-9]+$`
    import re
    display_id = re.sub(r'_[0-9]+$', '', vehicle_id)

    out.update({
        'valid': True,
        'vehicleId': vehicle_id,
        'vehicleDisplayId': display_id,
        'enStationId': parts[1],
        'exStationId': parts[2],
        'enWeight': parts[3],
        'exWeight': parts[4],
        'mediaTypeId': int(parts[5] or 0),
        'transactionId': parts[6],
        'passId': parts[7],
        'exTime': parts[8],
        'transPayTypeId': int(parts[9] or 0),
        'fee': parts[10],  # 元（前端再除以 100）
        'payFee': parts[11],
        'vehicleSignId': int(parts[12] or 0, 16) if parts[12] else 0,
        'provinceCount': parts[13],
    })

    # 颜色名（vehicleId 最后一位）
    if vehicle_id:
        out['vehicleColorName'] = vehicle_id[-1]
    return out


class MobileJsonFolderWatcher:
    """监听 APP_PHOTO_DIR 目录，新 JSON 文件被创建时处理（对齐 Qt JsonFolderWatcher）"""

    def __init__(self, watch_dir: Path = APP_PHOTO_DIR, poll_interval: float = 1.0):
        self._watch_dir = watch_dir
        self._poll_interval = poll_interval
        self._seen: set[str] = set()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def start(self) -> None:
        if self._running:
            return
        if not self._watch_dir.is_dir():
            logger.warning(f'[WATCHER] 监听目录不存在: {self._watch_dir}（可由 MOCK_PHOTO_DIR 环境变量指定）')
            return
        self._running = True
        # 预扫描已存在的 JSON（不重复处理）
        self._prescan()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name='mobile-json-watcher'
        )
        self._thread.start()
        logger.info(f'[WATCHER] 移动 app 监听已启动: {self._watch_dir}')

    def stop(self) -> None:
        self._running = False

    def _prescan(self) -> None:
        try:
            for p in self._watch_dir.glob('*.json'):
                self._seen.add(str(p.resolve()))
        except Exception as e:
            logger.warning(f'[WATCHER] 预扫描失败: {e}')

    def _loop(self) -> None:
        while self._running:
            try:
                self._scan_once()
            except Exception as e:
                logger.warning(f'[WATCHER] 扫描异常: {e}')
            time.sleep(self._poll_interval)

    def _scan_once(self) -> None:
        for json_path in self._watch_dir.glob('*.json'):
            real = str(json_path.resolve())
            if real in self._seen:
                continue
            # 等待文件写入完成（避免读到半截）
            if not self._wait_stable(json_path):
                continue
            self._seen.add(real)
            self._handle_json(json_path)

    def _wait_stable(self, p: Path, retries: int = 3) -> bool:
        """简单检测：连续 2 次 size 一致认为写完"""
        try:
            s1 = p.stat().st_size
        except Exception:
            return False
        for _ in range(retries):
            time.sleep(0.1)
            try:
                s2 = p.stat().st_size
            except Exception:
                return False
            if s1 != s2:
                return False
        return True

    def _handle_json(self, json_path: Path) -> None:
        """读 JSON + 解析图片 + 解析 passcode + 推 WS"""
        try:
            content = json_path.read_text(encoding='utf-8')
        except Exception as e:
            logger.warning(f'[WATCHER] 读 JSON 失败 {json_path}: {e}')
            return

        try:
            doc = json.loads(content)
        except Exception as e:
            logger.warning(f'[WATCHER] 解析 JSON 失败 {json_path}: {e}')
            return

        if not isinstance(doc, dict):
            return

        # imgsDir = {jsonPath目录}/{jsonPath文件名（去.json）}/
        imgs_dir = json_path.parent / json_path.stem
        session_id = json_path.stem  # 用 JSON 文件名作为 session_id

        # 复制图片到 uploads 目录
        field_map = {
            'head_image_path': ('carHeadImg', 'head'),
            'body_image_path': ('carBodyImg', 'body'),
            'tail_image_path': ('carTailImg', 'tail'),
            'top_image_path': ('top', 'top'),  # 当前端没车顶图字段，先留着
            'passcode_image_path': ('passCodeImg', 'passcode'),
            'license_image_path1': ('vehicleLicenseImg1', 'license1'),
            'license_image_path2': ('vehicleLicenseImg2', 'license2'),
        }
        goods_list: list[str] = []
        for i in (1, 2, 3):
            f = doc.get(f'goodsImg{i}', '')
            if f:
                p = _store_image(imgs_dir, f, f'goods{i}', session_id)
                if p:
                    goods_list.append(p)
        evidences_list: list[str] = []
        for key, cat in [('evidenceImg_Optional', 'evidence_opt'),
                         ('goodsImg_Chase', 'goods_chase'),
                         ('evidenceImg_Chase', 'evidence_chase')]:
            f = doc.get(key, '')
            if f:
                p = _store_image(imgs_dir, f, cat, session_id)
                if p:
                    evidences_list.append(p)

        result: dict[str, Any] = {
            'sessionId': session_id,
            'plate_number_gc': doc.get('plate_number_gc', ''),
            'phone': doc.get('phone', ''),
            'goods_type': doc.get('goods_type', ''),
        }
        for k, (json_field, cat) in field_map.items():
            f = doc.get(json_field, '')
            if f:
                p = _store_image(imgs_dir, f, cat, session_id)
                if p:
                    result[k] = p
        if goods_list:
            result['goods_image_path'] = ','.join(goods_list)
        if evidences_list:
            result['evidences_image_path'] = ','.join(evidences_list)

        # 解析通行码 14 字段
        passcode = _parse_passcode(doc.get('passcodeQRCodeInfo', ''))
        if passcode['valid']:
            result['passcode'] = passcode
            # 生成 QR 码图（对齐 Qt QZXing::encodeData，保存到 passcode_image_path）
            qr_path = _generate_qrcode(doc.get('passcodeQRCodeInfo', ''), session_id)
            if qr_path:
                result['passcode_image_path'] = qr_path

        logger.info(f'[WATCHER] session={session_id} 收到 {len([k for k in result if k.endswith("_image_path")])} 张图片, '
                    f'passcode_valid={passcode["valid"]} qr_path={bool(result.get("passcode_image_path"))}')

        # 推 WS 给前端
        self._emit_ws(result)

    def _emit_ws(self, payload: dict) -> None:
        try:
            # 优先用模块级 socketio（生产从 app.websocket.handler 导入；测试可覆盖）
            sio = globals().get('socketio')
            if sio is None:
                from app.websocket.handler import socketio as sio
            # 推 mobile_upload：含图片路径
            sio.emit('message', {
                'type': 'mobile_upload',
                'timestamp': int(time.time() * 1000),
                'data': payload,
            })
            # 如果有通行码，再推一个独立事件
            if payload.get('passcode'):
                sio.emit('message', {
                    'type': 'mobile_passcode',
                    'timestamp': int(time.time() * 1000),
                    'data': payload['passcode'],
                })
        except Exception as e:
            logger.warning(f'[WATCHER] 推 WS 失败: {e}')


# 全局单例
_watcher: Optional[MobileJsonFolderWatcher] = None
_watcher_lock = threading.Lock()


def start_mobile_watcher() -> None:
    """启动移动 app 目录监听器（app 启动时调用）"""
    global _watcher
    with _watcher_lock:
        if _watcher is not None:
            return
        _watcher = MobileJsonFolderWatcher()
        _watcher.start()


def stop_mobile_watcher() -> None:
    global _watcher
    with _watcher_lock:
        if _watcher is not None:
            _watcher.stop()
            _watcher = None