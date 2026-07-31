# MediaMTX — 主页实时视频（WHEP）

海康 RTSP → 本目录 MediaMTX → WHEP → 前端主页 `<video>`。

## 首次准备（只需一次）

```powershell
cd E:\bibl\lvtong-backend
.\deploy\mediamtx\download.ps1
```

摄像头一般改数据库 `devices`（默认 `id=20` / camera1），Flask 启动时会同步到 `mediamtx.local.yml` 的 `cam1.source`。  
环境变量：`LIVE_VIDEO_DEVICE_PK`（默认 20）、`LIVE_VIDEO_STREAM_TYPE`（1 主码流 / 2 子码流）。

## 日常使用

```powershell
cd E:\bibl\lvtong-backend
python main.py
```

Flask 默认自动拉起 MediaMTX（`MEDIAMTX_AUTO_START=true`）。  
再开前端 Vite，主页「实时视频」走 `/mtx/cam1/whep`。

关闭自动启动：

```text
set MEDIAMTX_AUTO_START=false
```

手动单独跑流媒体：

```powershell
.\deploy\mediamtx\start.ps1
```

## 端口

| 服务 | 端口 |
|------|------|
| MediaMTX WHEP | 8889 |
| MediaMTX API | 9997 |
| Flask | 8080 |
| Vite（代理 /mtx） | 5173 |

## 框图

主页裁切预览叠在 `<video>` 之上（z-index），删框后仍见直播。
