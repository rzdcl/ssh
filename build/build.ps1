# 重新打包 RZDCL.exe + RZDCL-cli.exe（需官方安装版 Python 3.11，已装 pyinstaller / paramiko / pillow）
# 用法：在 PowerShell 里执行  .\build\build.ps1

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$py = "C:\Users\12518\AppData\Local\Programs\Python\Python311\python.exe"

if (-not (Test-Path $py)) { throw "找不到 Python 3.11：$py" }

# 1) 图标素材（只有在图标脚本存在时才重生成，正常打包可跳过）
if ((Test-Path "$root\assets\rzdcl_raw.png") -and (Test-Path "$PSScriptRoot\make_icon_dark2.py")) {
    & $py "$PSScriptRoot\make_icon_dark2.py"
    if (Test-Path "$root\assets\rzdcl_dark2.png") {
        Copy-Item "$root\assets\rzdcl_dark2.png" "$root\assets\rzdcl.png" -Force
        Copy-Item "$root\assets\rzdcl_dark2.ico" "$root\assets\rzdcl.ico" -Force
    }
}

# 2) 关掉正在运行的旧版，避免文件占用
Get-Process RZDCL, RZDCL-cli -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 1

Push-Location $root

# 3) 图形界面版
& $py -m PyInstaller --onefile --windowed --noconfirm --clean --name RZDCL `
    --paths "$root\src" `
    --add-data "$root\assets\rzdcl.png;assets" `
    --add-data "$root\assets\rzdcl.ico;assets" `
    --icon "$root\assets\rzdcl.ico" `
    --version-file "$root\build\version_info.txt" `
    --distpath "$root\dist" --workpath "$root\build\pyi" --specpath "$root\build" `
    "$root\src\rzdcl_console.py"

# 4) 命令行版（给 AI / 脚本用）
& $py -m PyInstaller --onefile --console --noconfirm --clean --name RZDCL-cli `
    --paths "$root\src" `
    --icon "$root\assets\rzdcl.ico" `
    --version-file "$root\build\version_info.txt" `
    --distpath "$root\dist" --workpath "$root\build\pyi_cli" --specpath "$root\build" `
    "$root\src\rzdcl_cli.py"

Pop-Location

Copy-Item "$root\dist\RZDCL.exe" "$root\RZDCL.exe" -Force
Copy-Item "$root\dist\RZDCL-cli.exe" "$root\RZDCL-cli.exe" -Force

# 5) 自检
$g = Start-Process -FilePath "$root\RZDCL.exe" -ArgumentList @("--selftest") -PassThru -Wait
$c = Start-Process -FilePath "$root\RZDCL-cli.exe" -ArgumentList @("list") -PassThru -Wait -NoNewWindow
Write-Host ("打包完成：GUI 自检 {0}，CLI list {1}" -f $g.ExitCode, $c.ExitCode)
