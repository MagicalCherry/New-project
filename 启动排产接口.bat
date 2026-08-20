@echo off
chcp 936 >nul
title 排产 Agent 接口
cd /d "%~dp0"

echo ============================================
echo   排产 Agent 接口启动
echo ============================================
echo.
echo [1/3] 检查依赖...
python -c "import fastapi, uvicorn" >nul 2>&1
if errorlevel 1 (
    echo   首次运行，正在安装依赖（稍候）...
    python -m pip install fastapi uvicorn
)
echo [2/3] 启动排产服务...
start "" python src\api.py
timeout /t 4 /nobreak >nul
echo [3/3] 打开操作页面 http://localhost:8000/docs ...
start "" http://localhost:8000/docs
echo.
echo 排产服务已启动，网页即是操作界面。关闭 "python src\api.py" 窗口 = 停止服务
