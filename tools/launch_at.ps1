# 定时启动器：到 LaunchAt 时刻先跑预检，通过后启动 damai_mode0_K80.py；
# 脚本结束后导出手机 logcat 供复盘。全过程写入 artifacts\launch_<Tag>.log。
# 用法：
#   powershell -File tools\launch_at.ps1 -LaunchAt "2026-10-14 12:58:00" -SaleTime "2026-10-14 13:00:00" -Tag 1300
param(
    [datetime]$LaunchAt,
    [string]$SaleTime,
    [string]$Tag
)
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$py = Join-Path $root '.venv-phone\Scripts\python.exe'
$adb = Join-Path $root 'platform-tools\adb.exe'
$serialArgs = @(); if ($env:DAMAI_DEVICE_SERIAL) { $serialArgs = @('-s', $env:DAMAI_DEVICE_SERIAL) }
$log = Join-Path $root "artifacts\launch_$Tag.log"
$env:PYTHONIOENCODING = 'utf-8'

function Write-Log([string]$msg) {
    "{0}  {1}" -f (Get-Date -Format 'HH:mm:ss.fff'), $msg | Add-Content -Path $log -Encoding utf8
}

Write-Log "launcher started, waiting until $($LaunchAt.ToString('HH:mm:ss')) (sale $SaleTime)"
while ((Get-Date) -lt $LaunchAt) { Start-Sleep -Milliseconds 500 }

Write-Log 'running precheck'
& $py tools\precheck_K80.py --skip-time *>&1 | ForEach-Object { Write-Log "  $_" }
if ($LASTEXITCODE -ne 0) {
    Write-Log "PRECHECK FAILED (exit $LASTEXITCODE) - grab script NOT started"
    exit 1
}

$out = Join-Path $root "artifacts\result_20261014_$Tag.json"
$err = Join-Path $root "artifacts\result_20261014_$Tag.err.log"
$p = Start-Process -FilePath $py -ArgumentList 'scripts\damai_mode0_K80.py', "`"$SaleTime`"" `
    -RedirectStandardOutput $out -RedirectStandardError $err -WindowStyle Hidden -PassThru
Write-Log "grab script started pid=$($p.Id)"

while (-not $p.HasExited) {
    $focus = (& $adb @serialArgs shell "dumpsys window | grep mCurrentFocus" 2>&1) -join ' '
    Write-Log ("focus=" + ($focus -replace '.*u0 ', '' -replace '\}.*', '').Trim())
    Start-Sleep -Seconds 2
}
Write-Log "grab script exited code=$($p.ExitCode)"

& $adb @serialArgs logcat -d -v time 2>$null | Out-File -Encoding utf8 (Join-Path $root "artifacts\logcat_$Tag.txt")
Write-Log 'logcat dumped, launcher done'
