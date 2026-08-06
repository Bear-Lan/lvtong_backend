"""LiveDetect 子进程入口：拉流 + YOLO + 区域 + 告警 + 写 latest.jpg/status.json。

必须在独立解释器中运行（无 gevent monkey-patch），否则 torch 会拖死 Flask。
由 app.services.live_detect 通过 subprocess 拉起。
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path


def _tcp_reachable(host: str, port: int = 554, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Windows 下 replace 可能因读者占用失败，带重试。"""
    tmp = path.with_name(f'{path.name}.{os.getpid()}.{time.time_ns()}.tmp')
    tmp.write_bytes(data)
    last_err: Exception | None = None
    for _ in range(15):
        try:
            os.replace(tmp, path)
            return
        except PermissionError as e:
            last_err = e
            time.sleep(0.01)
        except OSError as e:
            last_err = e
            time.sleep(0.01)
    try:
        path.write_bytes(data)
    except Exception:
        if last_err:
            raise last_err
        raise
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def _atomic_write_text(path: Path, text: str) -> None:
    _atomic_write_bytes(path, text.encode('utf-8'))


# 进程内状态缓存：避免每帧读写 status.json 引发 Windows 文件锁冲突
_status_cache: dict = {
    'connection': 'starting',
    'resolution': '--',
    'display_fps': 0.0,
    'detection_fps': 0.0,
    'device': '--',
    'model': '--',
    'error': None,
}
_status_last_flush = 0.0
_STATUS_FLUSH_INTERVAL = 0.5


def _write_status(path: Path, force: bool = False, **kwargs) -> None:
    global _status_last_flush
    _status_cache.update(kwargs)
    _status_cache['ts'] = time.time()
    now = time.time()
    # connection/error 变化立即落盘；其余节流
    urgent = force or ('connection' in kwargs) or ('error' in kwargs)
    if not urgent and (now - _status_last_flush) < _STATUS_FLUSH_INTERVAL:
        return
    try:
        _atomic_write_text(path, json.dumps(_status_cache, ensure_ascii=False))
        _status_last_flush = now
    except Exception as e:
        # 写状态失败不中断拉流
        print(f'[LiveWorker] status 写入失败: {e}', flush=True)


def _load_zones(zones_path: Path) -> dict:
    data = {
        'enter_seconds': 0.5,
        'exit_seconds': 0.2,
        'spatial_dedup_seconds': 5.0,
        'zones': [],
    }
    if not zones_path.is_file():
        return data
    try:
        raw = json.loads(zones_path.read_text(encoding='utf-8'))
        if isinstance(raw, dict):
            data.update({k: raw[k] for k in data if k in raw})
            if isinstance(raw.get('zones'), list):
                data['zones'] = raw['zones']
    except Exception as e:
        print(f'[LiveWorker] zones 读取失败: {e}', flush=True)
    zones = []
    for z in data['zones']:
        if not isinstance(z, dict):
            continue
        pts = z.get('points') or []
        if len(pts) < 3:
            continue
        kind = z.get('kind') or 'warning'
        if kind not in ('warning', 'danger'):
            kind = 'warning'
        zones.append({
            'id': str(z.get('id') or uuid.uuid4()),
            'name': str(z.get('name') or ''),
            'kind': kind,
            'points': [
                {'x': float(p.get('x', 0)), 'y': float(p.get('y', 0))}
                for p in pts if isinstance(p, dict)
            ],
            'visible': bool(z.get('visible', True)),
        })
    data['zones'] = zones
    return data


@dataclass
class DetectedBox:
    cls_id: int
    track_id: int
    x1: float
    y1: float
    x2: float
    y2: float
    conf: float
    kind: str = 'unknown'


_PERSON_CLS = 0
_VEHICLE_CLS = {2, 5, 7}
_DETECT_CLASSES = [0, 2, 5, 7]
_MAX_WIDTH = 1280
_JPEG_QUALITY = 85


def classify_road_users(boxes: list[DetectedBox]) -> list[DetectedBox]:
    vehicles = [b for b in boxes if b.cls_id in _VEHICLE_CLS]
    persons = [b for b in boxes if b.cls_id == _PERSON_CLS]
    others = [b for b in boxes if b.cls_id not in _VEHICLE_CLS and b.cls_id != _PERSON_CLS]
    for v in vehicles:
        v.kind = 'vehicle'
    for p in persons:
        best_area = float('inf')
        inside = False
        for v in vehicles:
            if p.x1 >= v.x1 and p.y1 >= v.y1 and p.x2 <= v.x2 and p.y2 <= v.y2:
                area = (v.x2 - v.x1) * (v.y2 - v.y1)
                if area < best_area:
                    best_area = area
                    inside = True
        p.kind = 'in_vehicle' if inside else 'pedestrian'
    return vehicles + persons + others


def _point_in_polygon(px: float, py: float, poly: list[tuple[float, float]]) -> bool:
    n = len(poly)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if ((yi > py) != (yj > py)) and (
            px < (xj - xi) * (py - yi) / (yj - yi + 1e-12) + xi
        ):
            inside = not inside
        j = i
    return inside


def update_zone_engine(boxes, zones, frame_w, frame_h, now, enter_s, exit_s, states):
    pedestrians = [b for b in boxes if b.kind == 'pedestrian']
    active_ids = set()
    for z in zones:
        if not z.get('visible', True):
            continue
        zid = z['id']
        poly = [(float(p['x']), float(p['y'])) for p in z['points']]
        if len(poly) < 3:
            continue
        contacting = False
        for b in pedestrians:
            fx = ((b.x1 + b.x2) / 2.0) / max(frame_w, 1)
            fy = b.y2 / max(frame_h, 1)
            if _point_in_polygon(fx, fy, poly):
                contacting = True
                break
        st = states.get(zid) or {'active': False, 'contact_since': None, 'leave_since': None}
        if contacting:
            st['leave_since'] = None
            if st['contact_since'] is None:
                st['contact_since'] = now
            if not st['active'] and (now - st['contact_since']) >= enter_s:
                st['active'] = True
        else:
            st['contact_since'] = None
            if st['active']:
                if st['leave_since'] is None:
                    st['leave_since'] = now
                elif (now - st['leave_since']) >= exit_s:
                    st['active'] = False
                    st['leave_since'] = None
            else:
                st['leave_since'] = None
        states[zid] = st
        if st['active']:
            active_ids.add(zid)
    return active_ids


def render_frame(frame, boxes, zones, active_ids):
    import cv2
    import numpy as np

    out = frame.copy()
    h, w = out.shape[:2]
    for z in zones:
        if not z.get('visible', True):
            continue
        pts = np.array(
            [[int(p['x'] * w), int(p['y'] * h)] for p in z['points']],
            dtype=np.int32,
        )
        if len(pts) < 3:
            continue
        color = (0, 0, 255) if z.get('kind') == 'danger' else (0, 220, 255)
        if z['id'] in active_ids:
            overlay = out.copy()
            cv2.fillPoly(overlay, [pts], color)
            cv2.addWeighted(overlay, 0.25, out, 0.75, 0, out)
        cv2.polylines(out, [pts], True, color, 2)
        name = z.get('name') or z['id']
        cv2.putText(
            out, name, (pts[0][0], max(20, pts[0][1] - 6)),
            cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 2, cv2.LINE_AA,
        )

    style = {
        'pedestrian': ((0, 200, 0), 'ped'),
        'in_vehicle': ((0, 140, 255), 'in-veh'),
        'vehicle': ((255, 140, 0), 'veh'),
    }
    for b in boxes:
        color, label = style.get(b.kind, ((200, 200, 200), b.kind))
        x1, y1, x2, y2 = int(b.x1), int(b.y1), int(b.x2), int(b.y2)
        cv2.rectangle(out, (x1, y1), (x2, y2), color, 2)
        text = f'{label}#{b.track_id} {b.conf:.2f}'
        cv2.putText(
            out, text, (x1, max(16, y1 - 4)),
            cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1, cv2.LINE_AA,
        )

    if w > _MAX_WIDTH:
        scale = _MAX_WIDTH / float(w)
        out = cv2.resize(out, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    return out


def encode_jpeg(frame, quality=_JPEG_QUALITY):
    import cv2
    ok, buf = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
    return buf.tobytes() if ok else None


def _trigger_alarm(zone, box, frame, now, dedup_s, last_pos, alarms_dir, alarms_log):
    import cv2

    zid = zone['id']
    fx = (box.x1 + box.x2) / 2.0
    fy = box.y2
    last = last_pos.get(zid)
    if last is not None:
        lx, ly, lts = last
        if (now - lts) < dedup_s and ((fx - lx) ** 2 + (fy - ly) ** 2) ** 0.5 < 80:
            return None
    last_pos[zid] = (fx, fy, now)

    fname = (
        f"{time.strftime('%Y%m%d_%H%M%S')}_"
        f"{zone.get('name') or zid}_"
        f"person{box.track_id}_{uuid.uuid4().hex[:6]}.jpg"
    )
    for ch in '<>:"/\\|?*':
        fname = fname.replace(ch, '_')
    out_path = alarms_dir / fname
    try:
        cv2.imwrite(str(out_path), frame, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
    except Exception as e:
        print(f'[LiveWorker] 告警截图失败: {e}', flush=True)
        return None

    if os.environ.get('LIVE_DETECT_BEEP', 'true').strip().lower() in ('1', 'true', 'yes', 'on'):
        try:
            import winsound
            winsound.Beep(1000, 200)
        except Exception:
            pass

    rec = {
        'id': uuid.uuid4().hex,
        'zone_id': zid,
        'zone_name': zone.get('name') or '',
        'kind': zone.get('kind'),
        'track_id': box.track_id,
        'ts': now,
        'time': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now)),
        'file': fname,
        'rel_path': f'live_alarms/{fname}',
    }
    try:
        with alarms_log.open('a', encoding='utf-8') as f:
            f.write(json.dumps(rec, ensure_ascii=False) + '\n')
    except Exception as e:
        print(f'[LiveWorker] 告警日志失败: {e}', flush=True)
    print(f'[LiveWorker] 红区告警 zone={rec["zone_name"]} file={fname}', flush=True)
    return rec


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--rtsp', required=True)
    parser.add_argument('--model', required=True)
    parser.add_argument('--zones', required=True)
    parser.add_argument('--out-dir', required=True)
    parser.add_argument('--alarms-dir', required=True)
    parser.add_argument('--stop-file', required=True)
    parser.add_argument('--device-id', default='')
    parser.add_argument('--device-ip', default='')
    args = parser.parse_args(argv)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    alarms_dir = Path(args.alarms_dir)
    alarms_dir.mkdir(parents=True, exist_ok=True)
    status_path = out_dir / 'status.json'
    jpeg0_path = out_dir / 'latest.0.jpg'
    jpeg1_path = out_dir / 'latest.1.jpg'
    slot_path = out_dir / 'current.slot'
    jpeg_legacy = out_dir / 'latest.jpg'
    jpeg_slot = 0
    alarms_log = out_dir / 'alarms.jsonl'
    stop_file = Path(args.stop_file)
    zones_path = Path(args.zones)
    model_path = Path(args.model)

    os.environ.setdefault('OMP_NUM_THREADS', '1')
    os.environ.setdefault('MKL_NUM_THREADS', '1')
    os.environ.setdefault('OPENBLAS_NUM_THREADS', '1')
    os.environ.setdefault('NUMEXPR_NUM_THREADS', '1')
    os.environ.setdefault('KMP_DUPLICATE_LIB_OK', 'TRUE')
    os.environ.setdefault('OPENCV_FFMPEG_CAPTURE_OPTIONS', 'rtsp_transport;tcp')
    # 禁止 ultralytics 在线拉取依赖（会卡死 worker）
    os.environ.setdefault('YOLO_OFFLINE', '1')
    os.environ.setdefault('ULTRALYTICS_OFFLINE', '1')

    _write_status(
        status_path,
        connection='starting',
        device_id=args.device_id,
        device_ip=args.device_ip,
        error=None,
    )

    import cv2
    import numpy as np

    model = None
    device = 'cpu'
    try:
        import torch
        try:
            torch.set_num_threads(1)
        except Exception:
            pass
        from ultralytics import YOLO
        if model_path.is_file():
            device = 'cuda:0' if torch.cuda.is_available() else 'cpu'
            print(f'[LiveWorker] 加载模型 {model_path.name} device={device}', flush=True)
            model = YOLO(str(model_path))
            _write_status(status_path, device=device, model=model_path.name, error=None)
        else:
            _write_status(status_path, error=f'模型不存在: {model_path}', model='--')
    except Exception as e:
        print(f'[LiveWorker] 模型加载失败: {e}', flush=True)
        _write_status(status_path, error=f'模型加载失败: {e}', model='--', device='cpu')
        model = None

    host = ''
    try:
        after = args.rtsp.split('@', 1)[-1]
        host = after.split(':', 1)[0].split('/', 1)[0]
    except Exception:
        pass

    zone_states: dict = {}
    last_alarm_pos: dict = {}
    zones_mtime = 0.0
    zcfg = _load_zones(zones_path)
    backoff = 0.5
    disp_times: deque = deque(maxlen=120)
    det_times: deque = deque(maxlen=120)

    while not stop_file.exists():
        if host and not _tcp_reachable(host, 554, 2.0):
            _write_status(status_path, connection='source_failed', error=f'摄像头 {host}:554 不可达')
            time.sleep(backoff)
            backoff = min(backoff * 2, 8.0)
            continue

        _write_status(status_path, connection='connecting', error=None)
        container = None
        try:
            try:
                import av
                container = av.open(
                    args.rtsp,
                    options={
                        'rtsp_transport': 'tcp',
                        'buffer_size': '1024000',
                        'max_delay': '0',
                        'fflags': 'nobuffer',
                        'flags': 'low_delay',
                        'stimeout': '5000000',
                    },
                )
            except Exception as e:
                _write_status(status_path, connection='source_failed', error=f'RTSP 打开失败: {e}')
                time.sleep(backoff)
                backoff = min(backoff * 2, 8.0)
                continue

            video_streams = container.streams.video
            if not video_streams:
                _write_status(status_path, connection='source_failed', error='RTSP 无视频流')
                time.sleep(backoff)
                backoff = min(backoff * 2, 8.0)
                continue
            video_stream = video_streams[0]

            _write_status(status_path, connection='connected', error=None)
            backoff = 0.5
            fail_reads = 0
            # PyAV：ffmpeg 内部丢弃不完整/撕裂帧，只 yield 完整帧，从根上消除灰屏/宏块
            for avframe in container.decode(video_stream):
                if stop_file.exists():
                    break
                try:
                    frame = avframe.to_ndarray(format='bgr24')
                except Exception:
                    fail_reads += 1
                    if fail_reads >= 30:
                        _write_status(status_path, connection='reconnecting', error='解码失败，重连中')
                        break
                    continue
                fail_reads = 0
                now = time.time()
                h, w = frame.shape[:2]
                _write_status(status_path, resolution=f'{w}x{h}')

                try:
                    mtime = zones_path.stat().st_mtime
                except OSError:
                    mtime = 0.0
                if mtime != zones_mtime:
                    zcfg = _load_zones(zones_path)
                    zones_mtime = mtime

                zones = zcfg['zones']
                enter_s = float(zcfg['enter_seconds'])
                exit_s = float(zcfg['exit_seconds'])
                dedup_s = float(zcfg['spatial_dedup_seconds'])

                boxes: list[DetectedBox] = []
                if model is not None:
                    try:
                        # 优先 ByteTrack；缺 lap 时降级 predict，避免 AutoUpdate 卡死
                        try:
                            results = model.track(
                                frame,
                                persist=True,
                                tracker='bytetrack.yaml',
                                conf=0.4,
                                imgsz=640,
                                classes=_DETECT_CLASSES,
                                device=device,
                                verbose=False,
                            )
                        except Exception as track_err:
                            print(f'[LiveWorker] track 失败，降级 predict: {track_err}', flush=True)
                            results = model.predict(
                                frame,
                                conf=0.4,
                                imgsz=640,
                                classes=_DETECT_CLASSES,
                                device=device,
                                verbose=False,
                            )
                        det_times.append(time.time())
                        if results:
                            r0 = results[0]
                            if r0.boxes is not None and len(r0.boxes) > 0:
                                xyxy = r0.boxes.xyxy.cpu().numpy()
                                cls = r0.boxes.cls.cpu().numpy().astype(int)
                                conf = r0.boxes.conf.cpu().numpy()
                                if getattr(r0.boxes, 'id', None) is not None:
                                    ids = r0.boxes.id.cpu().numpy().astype(int)
                                else:
                                    ids = np.arange(len(xyxy))
                                for i in range(len(xyxy)):
                                    boxes.append(DetectedBox(
                                        cls_id=int(cls[i]),
                                        track_id=int(ids[i]),
                                        x1=float(xyxy[i][0]),
                                        y1=float(xyxy[i][1]),
                                        x2=float(xyxy[i][2]),
                                        y2=float(xyxy[i][3]),
                                        conf=float(conf[i]),
                                    ))
                        if len(det_times) >= 2:
                            span = det_times[-1] - det_times[0]
                            if span > 0:
                                _write_status(
                                    status_path,
                                    detection_fps=round((len(det_times) - 1) / span, 1),
                                    error=None,
                                )
                    except Exception as e:
                        _write_status(status_path, error=f'检测失败: {e}')
                        boxes = []

                boxes = classify_road_users(boxes)
                active_ids = update_zone_engine(
                    boxes, zones, w, h, now, enter_s, exit_s, zone_states,
                )

                for z in zones:
                    if z.get('kind') != 'danger' or z['id'] not in active_ids:
                        continue
                    for b in boxes:
                        if b.kind != 'pedestrian':
                            continue
                        poly = [(float(p['x']), float(p['y'])) for p in z['points']]
                        fx = ((b.x1 + b.x2) / 2.0) / max(w, 1)
                        fy = b.y2 / max(h, 1)
                        if _point_in_polygon(fx, fy, poly):
                            _trigger_alarm(
                                z, b, frame, now, dedup_s,
                                last_alarm_pos, alarms_dir, alarms_log,
                            )

                annotated = render_frame(frame, boxes, zones, active_ids)
                jpeg = encode_jpeg(annotated)
                if jpeg:
                    # 双缓冲：写入非当前槽，再切换 slot 指针，避免读者读到半文件
                    next_slot = 1 - jpeg_slot
                    target = jpeg0_path if next_slot == 0 else jpeg1_path
                    try:
                        _atomic_write_bytes(target, jpeg)
                        _atomic_write_text(slot_path, str(next_slot))
                        # 兼容旧读者
                        try:
                            _atomic_write_bytes(jpeg_legacy, jpeg)
                        except Exception:
                            pass
                        jpeg_slot = next_slot
                    except Exception as e:
                        print(f'[LiveWorker] 写帧失败: {e}', flush=True)
                    disp_times.append(time.time())
                    if len(disp_times) >= 2:
                        span = disp_times[-1] - disp_times[0]
                        if span > 0:
                            _write_status(
                                status_path,
                                display_fps=round((len(disp_times) - 1) / span, 1),
                            )
        except Exception as e:
            msg = str(e).lower()
            if '401' in msg or 'auth' in msg or 'unauthorized' in msg:
                _write_status(status_path, connection='authentication_failed', error='RTSP 认证失败')
            else:
                _write_status(status_path, connection='reconnecting', error=f'拉流异常: {e}')
            time.sleep(backoff)
            backoff = min(backoff * 2, 8.0)
        finally:
            if container is not None:
                try:
                    container.close()
                except Exception:
                    pass

        if stop_file.exists():
            break
        _write_status(status_path, connection='reconnecting')
        time.sleep(backoff)
        backoff = min(backoff * 2, 8.0)

    _write_status(status_path, connection='stopped')
    print('[LiveWorker] 已退出', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
