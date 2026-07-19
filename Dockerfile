# 绿通快检系统 - 后端 Docker 镜像
FROM ubuntu:24.04

# 替换 apt 源为清华镜像（加速国内构建）
RUN sed -i 's|http://archive.ubuntu.com|https://mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list && \
    sed -i 's|http://security.ubuntu.com|https://mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list

# 安装系统依赖
RUN apt-get update && apt-get install -y \
    gfortran g++ make vim nano unzip curl \
    inetutils-ping openssl libssl-dev libaio1t64

# 安装 Python 运行时
RUN apt-get update && \
    apt-get install -y python3 python3-venv && \
    rm -rf /var/lib/apt/lists/*

# 设置工作目录
WORKDIR /app

# 复制项目文件
COPY . .

# 创建虚拟环境并安装依赖
RUN python3 -m venv /myenv
RUN /myenv/bin/pip install --no-cache-dir -r requirements.txt \
    -i https://pypi.tuna.tsinghua.edu.cn/simple

# 配置环境
RUN echo "ulimit -s unlimited" >> /root/.bashrc

# 暴露端口
EXPOSE 8080

# 环境变量
ENV PATH="/myenv/bin:$PATH"

# 启动
CMD ["./startup.sh"]
