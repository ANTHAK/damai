@echo off
chcp 65001 >nul
cd /d "%~dp0"
setlocal

if not exist ".venv-phone\Scripts\python.exe" (
    echo [FAIL] 找不到 .venv-phone，请先安装环境
    pause
    exit /b 1
)

set "SALE_TIME=%~1"
if "%SALE_TIME%"=="" (
    set /p "SALE_TIME=请输入开售时间 (格式 YYYY-MM-DD HH:MM:SS，例如 2026-10-14 11:30:00): "
)
if "%SALE_TIME%"=="" (
    echo [FAIL] 未输入开售时间
    pause
    exit /b 1
)

echo.
echo ===== 第 1 步：预检 =====
".venv-phone\Scripts\python.exe" tools\precheck_K80.py
if errorlevel 1 (
    echo.
    echo [中止] 预检存在 FAIL，未启动抢票脚本
    pause
    exit /b 1
)

echo.
echo ===== 第 2 步：启动抢票 开售时间 %SALE_TIME% =====
echo 开售前 2 秒开始自动点击。运行期间不要碰手机，也不要关闭本窗口。
echo 跳到支付宝收银台后脚本会自动停止，请自己核对金额后付款。
echo.
if not exist artifacts mkdir artifacts
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set "STAMP=%%i"
set "PYTHONIOENCODING=utf-8"
".venv-phone\Scripts\python.exe" scripts\damai_mode0_K80.py "%SALE_TIME%" > "artifacts\result_%STAMP%.json"
set CODE=%ERRORLEVEL%
type "artifacts\result_%STAMP%.json"
echo.
echo 结果已保存到 artifacts\result_%STAMP%.json （退出码 %CODE%）
pause
exit /b %CODE%
