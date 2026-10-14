# 只读监控：每 2 秒记录手机前台页面和抢票脚本进程状态，不点击手机。
# 用法：powershell -File tools\watch_run.ps1 -ScriptPid 11368 -Log artifacts\watch_1220.log -Until "2026-10-14 12:31:00"
param(
    [int]$ScriptPid,
    [string]$Log,
    [datetime]$Until
)
$adb = Join-Path $PSScriptRoot '..\platform-tools\adb.exe'
$serialArgs = @(); if ($env:DAMAI_DEVICE_SERIAL) { $serialArgs = @('-s', $env:DAMAI_DEVICE_SERIAL) }
while ((Get-Date) -lt $Until) {
    $alive = [bool](Get-Process -Id $ScriptPid -ErrorAction SilentlyContinue)
    $focus = (& $adb @serialArgs shell "dumpsys window | grep mCurrentFocus" 2>&1) -join ' '
    $focus = ($focus -replace '.*u0 ', '' -replace '\}.*', '').Trim()
    "{0}  script_alive={1}  focus={2}" -f (Get-Date -Format 'HH:mm:ss.fff'), $alive, $focus | Add-Content -Path $Log -Encoding utf8
    if (-not $alive) { "{0}  script exited" -f (Get-Date -Format 'HH:mm:ss.fff') | Add-Content -Path $Log -Encoding utf8; break }
    Start-Sleep -Seconds 2
}
