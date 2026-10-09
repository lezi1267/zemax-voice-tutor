# 建桌面快捷方式「光学设计助教」(控制台最小化,对话窗照常弹出)。
# 用法:右键本文件 → 用 PowerShell 运行。
$desktop = [Environment]::GetFolderPath('Desktop')
$sh = (New-Object -ComObject WScript.Shell).CreateShortcut("$desktop\光学设计助教.lnk")
$sh.TargetPath = Join-Path $PSScriptRoot 'start.bat'
$sh.WorkingDirectory = $PSScriptRoot
$sh.WindowStyle = 7          # 最小化
$sh.Description = 'Zemax 光学设计语音助教'
$sh.Save()
Write-Host "已建快捷方式: $desktop\光学设计助教.lnk"
