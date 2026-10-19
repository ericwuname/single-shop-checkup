@echo off
REM M7 WP-5（docs/08 §8.1）：Windows 一键启动。
REM 用法：
REM   scripts\start_server.bat          默认绑 127.0.0.1（给 Cloudflare Tunnel 用，不直接暴露局域网）
REM   scripts\start_server.bat lan      绑 0.0.0.0（同一 Wi-Fi 下手机直接访问，试用用）
REM
REM 本脚本**不含任何密钥**。访问码等配置一律放 .env（见 .env.example）。

setlocal
cd /d "%~dp0.."

set PY=C:\Users\87465\.workbuddy\binaries\python\envs\single-shop\Scripts\python.exe
if not exist "%PY%" set PY=python

REM ---- 读取 .env（只认 KEY=VALUE，忽略注释与空行）----
if exist .env (
  for /f "usebackq eol=# tokens=1,* delims==" %%A in (".env") do set "%%A=%%B"
)

REM ---- 前端构建：dist 不存在或源码比 dist 新时重建 ----
if not exist web\dist\index.html (
  echo [start] 没有前端构建产物，先构建一次...
  pushd web && call npm run build && popd
)

REM ---- 绑定地址 ----
set HOST=127.0.0.1
if /i "%~1"=="lan" set HOST=0.0.0.0

echo [start] 启动单店体检 API：http://%HOST%:8000
echo [start] 本机健康检查： http://127.0.0.1:8000/api/health
"%PY%" -m uvicorn server.main:app --host %HOST% --port 8000

endlocal
