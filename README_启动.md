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
| 摄像头网络 | 可达 | 实时检测默认用 `camera4`（约 `192.168.88.103`） |

数据库账号可用环境变量覆盖：`DB_HOST` / `DB_PORT` / `DB_NAME` / `DB_USER` / `DB_PASSWORD`。

---

## 2. 后端启动（`lvtong-backend`）

打开终端，进入后端目录：

```bash
cd e:\bibl\lvtong-backend
```

### 2.1 首次安装依赖（按顺序）

```bash
# ① 装项目依赖（含 Flask、gevent、opencv、psycopg2、ultralytics、lapx、av 等）
pip install -r requirements.txt

# ② 单独装 CPU 版 torch（YOLO 需要；不要写进 requirements，避免拉错平台版本）
pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
```

有 NVIDIA GPU 时，不要用上面的 CPU 命令，到 https://pytorch.org 选对应 CUDA 版本再装。

> `7ed9abe` 之后新增、已写入 `requirements.txt` 的包：`ultralytics`、`lapx`、`av`。  
> `torch` / `torchvision` 需按上面第 ② 步单独安装。

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

**注意：实时视频检测（YOLO）不会随 Flask 自动启动**，需要手动点一下（见第 4 节）。

关后端：在该终端按 `Ctrl+C`。

---

## 3. 前端启动（`lvtong-ui`）

另开一个终端：

```bash
cd e:\bibl\lvtong-ui

# 首次
npm install

# 启动开发服务器
npm run dev
```

> `cb479cf` 之后 `package.json` 无新增依赖，正常 `npm install` 即可。

浏览器打开：**http://localhost:5173**

用管理员账号登录（账号密码请向管理员索取）。

---

## 4. 实时视频检测（YOLO）启动

后端起来后，检测服务**默认不跑**。需要画面时：

1. 登录进入主页  
2. 找到「实时视频」面板右上角的 **↻** 按钮  
3. 点一下（未启动时会调用启动接口；已启动则是重连）  
4. 等几秒，出现画面和检测框即成功  

也可用接口（需已登录拿到 token）：

```http
POST /api/live/ensure
Authorization: Bearer <token>
```

关掉检测：关掉后端（`Ctrl+C`）时会自动停止检测子进程。

---

## 5. 端口速查

| 端口 | 服务 |
|------|------|
| 5173 | 前端 Vite |
| 8080 | 后端 HTTP |
| 8082 | 后端 HTTPS |
| 8889 | MediaMTX（WebRTC / WHEP） |

这些端口不要被其它程序占用。

---

## 6. 常见问题

| 现象 | 处理 |
|------|------|
| 登录 502 / 请求失败 | 等几秒让后端完全起来再登录；确认 `python main.py` 仍在跑 |
| 实时视频提示「检测未启动」 | 点右上角 **↻** 启动 |
| 实时视频灰屏 / 花屏 | 再点一次 **↻** 重连；确认摄像头 IP 通、账号密码正确 |
| 摄像头连不上 | 查数据库 `devices` 表里 `device_id=camera4` 的 `ip_address` / `username` / `password` |
| 端口被占用 | 关掉占用 8080 / 8082 / 5173 / 8889 的其它程序后再启动 |
| 缺 torch / YOLO 报错 | 确认已执行第 2.1 节第 ② 步安装 torch |

---

## 7. 最短启动清单（已装过依赖时）

```bash
# 终端 1 — 后端
cd e:\bibl\lvtong-backend
python main.py

# 终端 2 — 前端
cd e:\bibl\lvtong-ui
npm run dev
```

浏览器打开 http://localhost:5173 → 登录 → 实时视频点 **↻**。
