#!/bin/bash
# 绿通快检系统 - 后端启动脚本

# 激活虚拟环境（优先级：Docker > 本地 venv > 本地 myenv）
if [ -d "../myenv" ]; then
    source ../myenv/bin/activate
elif [ -d "./venv" ]; then
    source ./venv/Scripts/activate   # Windows Git Bash
elif [ -d "./venv/bin" ]; then
    source ./venv/bin/activate       # Linux/Mac
fi

# 取消栈大小限制
ulimit -s unlimited

# 安装依赖（如果需要）
# pip install -r requirements.txt

# 启动服务
# 开发模式：直接运行 Flask
python main.py

# 生产模式：使用 gunicorn + gevent（支持 WebSocket）
# gunicorn -k gevent -w 1 --bind 0.0.0.0:8080 main:flask_app
