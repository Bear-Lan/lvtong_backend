# 绿通快检系统 — 启动说明

给第一次接触本项目的人看。按下面步骤做，就能把前后端跑起来。

---

## 1. 环境准备

先确认本机已安装：

| 软件 | 要求 | 说明 |
|------|------|------|
| Python | 3.11+（3.14 也可） | 后端 |
| Node.js | 18+ | 前端 |
| PostgreSQL | 已建库 | 默认库名 `gcis`，用户 `postgres`，密码 `123456`，地址 `127.0.0.1:5432` |
| 摄像头网络 | 可达 | 实时=`cam_lane`(103)；预约对讲=`cam_booking`(101)；球机=`cam_ptz`(104) |

数据库账号可用环境变量覆盖：`DB_HOST` / `DB_PORT` / `DB_NAME` / `DB_USER` / `DB_PASSWORD`。

---

## 2. 后端启动（`flask_backend`）

打开终端，进入后端目录：

```bash
cd e:\code_product\gcms_src\flask_backend
```

### 2.1 首次安装依赖

```bash
pip install -r requirements.txt
```

### 2.2 确认数据库

PostgreSQL 中已有库 `gcis`，且表结构已导入（含 `devices` 等）。

### 2.3 启动

```bash
python main.py
```

看到类似输出即成功：

```
[SERVER] HTTP  启动 http://0.0.0.0:8080
[SERVER] HTTPS 启动 https://0.0.0.0:8082
[MediaMTX] 已启动 ...
```

关后端：在该终端按 `Ctrl+C`。

---

## 3. 前端启动（`vue_frontend`）

另开一个终端：

```bash
cd e:\code_product\gcms_src\vue_frontend

# 首次
npm install

# 启动开发服务器
npm run dev
```

浏览器打开：**http://localhost:5173**

用管理员账号登录（账号密码请向管理员索取）。

主页「实时视频」走 MediaMTX WHEP（默认 `/mtx/cam1/whep`），无需额外启动检测进程。

---

## 4. 端口速查

| 端口 | 服务 |
|------|------|
| 5173 | 前端 Vite |
| 8080 | 后端 HTTP |
| 8082 | 后端 HTTPS |
| 8889 | MediaMTX（WebRTC / WHEP） |

这些端口不要被其它程序占用。

---

## 5. 常见问题

| 现象 | 处理 |
|------|------|
| 登录 502 / 请求失败 | 等几秒让后端完全起来再登录；确认 `python main.py` 仍在跑 |
| 实时视频灰屏 / 花屏 | 确认 MediaMTX 已启动、摄像头 IP 通、账号密码正确；检查浏览器控制台 WHEP 报错 |
| 摄像头连不上 | 查数据库 `devices` 表里对应 `device_id` 的 `ip_address` / `username` / `password` |
| 端口被占用 | 关掉占用 8080 / 8082 / 5173 / 8889 的其它程序后再启动 |

---

## 6. 最短启动清单（已装过依赖时）

```bash
# 终端 1 — 后端
cd e:\code_product\gcms_src\flask_backend
python main.py

# 终端 2 — 前端
cd e:\code_product\gcms_src\vue_frontend
npm run dev
```

浏览器打开 http://localhost:5173 → 登录即可。
