@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv-phone\Scripts\python.exe" (
    echo [FAIL] 找不到 .venv-phone，请先在项目根目录执行：
    echo        python -m venv .venv-phone
    echo        .venv-phone\Scripts\python.exe -m pip install -r requirements-phone.txt
    pause
    exit /b 1
)
".venv-phone\Scripts\python.exe" tools\precheck_K80.py %*
set CODE=%ERRORLEVEL%
pause
exit /b %CODE%
