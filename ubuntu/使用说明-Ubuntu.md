# RZDCL · 机器人 SSH 凭证台（Ubuntu / Linux 版）

单文件可执行，不用装 Python。两个程序：`RZDCL`（图形界面）、`RZDCL-cli`（命令行，给 AI 或脚本用）。

## 运行环境

- Ubuntu 22.04 及以上（x86_64）。编译时用的 Python 3.10，运行时需要 glibc ≥ 2.35。
- 图形界面需要桌面环境（X11 或 Wayland 下的 XWayland 都可以）。
- 纯服务器没有桌面时，可用 `xvfb-run -a ./RZDCL` 做自检，但正常使用要图形终端。

## 装好就能跑

```bash
chmod +x RZDCL RZDCL-cli
./RZDCL                     # 图形界面
./RZDCL-cli --config hosts.json list    # 命令行
```

凭证库查找顺序：`--config` 参数 → 环境变量 `ROBOT_VAULT` → 可执行文件同级的 `hosts.json` → `~/.config/RZDCL/hosts.json`。
把 Windows 上那份 `hosts.json` 直接拷到 `RZDCL` 旁边就能用，格式完全一样。

第一次启动会在旁边生成 `keys/id_ed25519`（私钥自动 `chmod 600`），并写进对方机器的 `~/.ssh/authorized_keys` 实现免密。

## 和 Windows 版的差别

| 项目 | Windows | Ubuntu |
| --- | --- | --- |
| 免密私钥权限 | `icacls` 收紧 ACL | `chmod 600` |
| 外部终端 | `ssh.exe` 新开控制台窗口 | 自动挑 `gnome-terminal` / `konsole` / `xfce4-terminal` / `tilix` / `x-terminal-emulator` / `xterm` |
| 打开远端文件 | 系统默认程序（`os.startfile`） | `xdg-open` |
| 定位凭证库 | 资源管理器选中 | `xdg-open` 打开所在目录 |
| 图标 | `.ico` 多尺寸 | 沿用同一套字标（窗口图标走 PNG） |

内部终端、SFTP 文件管理、上传/下载/回传/删除、跟随终端目录、`>_` 快捷键、操作记录全部一致。

## 免密排错（Linux 一侧最常见两个坑）

1. `Permission denied (publickey)`：对方 `/etc/ssh/sshd_config` 里 `PubkeyAuthentication no`。改成 `yes` 后 `sudo systemctl restart ssh`。
2. 私钥权限：Linux 上 ssh 也要求私钥只能自己读。程序会自动 `chmod 600`；自己手动复制密钥时记得也改一下。

## 中文字体

界面会自动在系统字体里挑一个可用的中文字体（Noto Sans CJK / 文泉驿 / 思源等）。如果全是方块：

```bash
sudo apt install -y fonts-noto-cjk
```

## 打包命令（自己重编时用）

```bash
python3 -m pip install --user paramiko pyinstaller
python3 -m PyInstaller --onefile --name RZDCL --add-data "assets:assets" src/rzdcl_console.py
python3 -m PyInstaller --onefile --name RZDCL-cli src/rzdcl_cli.py
```

源码和 Windows 版共用一套 `src/`，平台差异都在代码里按 `os.name` 分支处理，没有分叉维护。
