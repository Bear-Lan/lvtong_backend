@echo off
chcp 65001 >nul
echo.
echo ============================================
echo   绿通快检系统 - 后端 API 服务
echo ============================================
echo.

:: 激活虚拟环境
if exist "%~dp0venv\Scripts\activate.bat" (
    call "%~dp0venv\Scripts\activate.bat"
    echo [√] 虚拟环境已激活
) else (
    echo [×] 未找到 venv，请先创建: python -m venv venv
    pause
    exit /b 1
)

:: 检查依赖
python -c "import flask" 2>nul
if errorlevel 1 (
    echo [!] 检测到依赖未安装，正在安装...
    pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
)

echo [√] 启动 Flask API (端口: 8080) ...
echo.
echo   接口地址: http://localhost:8080/api/health
echo   退出: Ctrl+C
echo ============================================
echo.

python main.py
pause
