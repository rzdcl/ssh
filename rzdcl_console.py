#!/usr/bin/env python3
"""
RZDCL —— 机器人 SSH 凭证台（上位机）

功能：
  * 集中保存各机器人 / 开发板 / 服务器的 SSH 凭证（别名、IP、端口、账号、密码、备注）
  * 左侧设备列表双击（或回车）直接打开内嵌 SSH 终端，自动用保存的密码登录
  * 也可一键打开 Windows 原生 ssh 外部终端窗口（密码自动复制到剪贴板）
  * 新增 / 编辑 / 删除 / 连通性测试 / 复制密码

凭证库查找顺序：--config > 环境变量 ROBOT_VAULT > exe 同级 hosts.json >
%APPDATA%\\RZDCL\\hosts.json > 源码模式下的 ../vault/hosts.json
"""

import argparse
import hashlib
import json
import os
import posixpath
import queue
import random
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from rzdcl_core import (  # noqa: E402
    APP_NAME, APP_SUB, strip_ansi, resource_path, app_dir,
    config_path_default, load_entries, save_entries, normalize_entry,
    SshKey, SshSession, ensure_key_login, key_login_ok, install_public_key,
    external_ssh_args, open_external_terminal, exec_command,
    sftp_connect, list_dir, human_size, keys_dir, ssh_cli_key_ok, list_shell_cwd,
)

def sftp_expand(sftp, path):
    """把路径里的 ~ / ~/xxx 展开成远端真实路径（SFTP 不会自己展开）。"""
    p = (path or "").strip()
    if p == "~" or p.startswith("~/"):
        home = sftp.normalize(".")
        rest = p[2:] if p.startswith("~/") else ""
        return posixpath.normpath(posixpath.join(home, rest)) if rest else home
    return p


# ---------------------------------------------------------------- 外观常量
C = {
    "bg": "#F4F6FB",
    "panel": "#FFFFFF",
    "header": "#0B1220",
    "header_2": "#111C33",
    "header_sub": "#94A3C4",
    "accent": "#2563EB",
    "accent_dark": "#1D4ED8",
    "text": "#1F2937",
    "muted": "#6B7280",
    "line": "#E2E8F0",
    "ok": "#16A34A",
    "warn": "#D97706",
    "err": "#DC2626",
    "term_bg": "#0B1220",
    "term_fg": "#D8E2F5",
}

FAMILY = "Microsoft YaHei UI" if os.name == "nt" else "Noto Sans CJK SC"
MONO_FAMILY = "Consolas" if os.name == "nt" else "DejaVu Sans Mono"

FONT = (FAMILY, 10)
FONT_S = (FAMILY, 9)
FONT_B = (FAMILY, 10, "bold")
FONT_TITLE = (FAMILY, 16, "bold")
MONO = (MONO_FAMILY, 10)
MONO_S = (MONO_FAMILY, 9)


# ---------------------------------------------------------------- 跨平台杂项
def open_local(path):
    """用系统默认程序打开本地文件。"""
    p = str(path)
    if os.name == "nt":
        os.startfile(p)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", p])
    else:
        subprocess.Popen(["xdg-open", p])


def reveal_path(path):
    """在文件管理器里定位（Windows 选中文件，Linux 打开所在目录）。"""
    p = Path(path)
    if os.name == "nt":
        if p.is_file():
            subprocess.Popen(["explorer", "/select,", str(p)])
        else:
            subprocess.Popen(["explorer", str(p)])
    else:
        target = p if p.is_dir() else p.parent
        subprocess.Popen(["xdg-open", str(target)])


def pick_ui_font(root):
    """挑一个系统里真实存在的中文字体，避免 Linux 上字形变方块。"""
    global FAMILY, MONO_FAMILY, FONT, FONT_S, FONT_B, FONT_TITLE, MONO, MONO_S
    try:
        import tkinter.font as tkfont
        fams = set(tkfont.families(root))
    except Exception:  # noqa: BLE001
        return
    for cand in (FAMILY, "Noto Sans CJK SC", "Source Han Sans CN", "Noto Sans SC",
                 "WenQuanYi Micro Hei", "WenQuanYi Zen Hei", "Droid Sans Fallback",
                 "Segoe UI", "DejaVu Sans"):
        if cand in fams:
            FAMILY = cand
            break
    for cand in (MONO_FAMILY, "DejaVu Sans Mono", "Liberation Mono", "Ubuntu Mono",
                 "Noto Sans Mono", "Consolas", "Courier New"):
        if cand in fams:
            MONO_FAMILY = cand
            break
    FONT = (FAMILY, 10)
    FONT_S = (FAMILY, 9)
    FONT_B = (FAMILY, 10, "bold")
    FONT_TITLE = (FAMILY, 16, "bold")
    MONO = (MONO_FAMILY, 10)
    MONO_S = (MONO_FAMILY, 9)


# ---------------------------------------------------------------- 通用 UI
def flat_button(parent, text, command, kind="normal", width=None, **kw):
    """统一样式的扁平按钮。"""
    palette = {
        "primary": (C["accent"], "#FFFFFF", C["accent_dark"]),
        "normal": ("#EEF2F9", C["text"], "#E2E8F0"),
        "ghost": ("#FFFFFF", C["muted"], "#F1F5F9"),
        "dark": ("#1B2942", "#C9D7F0", "#24365A"),
        "danger": ("#FEF2F2", C["err"], "#FEE2E2"),
    }
    bg, fg, active = palette.get(kind, palette["normal"])
    btn = tk.Button(
        parent, text=text, command=command,
        bg=bg, fg=fg, activebackground=active, activeforeground=fg,
        relief="flat", bd=0, cursor="hand2", font=FONT,
        padx=12, pady=6, highlightthickness=0,
    )
    if width:
        btn.configure(width=width)
    btn.update_idletasks()
    return btn


# ---------------------------------------------------------------- 编辑弹窗
class EntryDialog:
    FIELDS = [
        ("alias", "别名 *", "例如 yunfan-0-165"),
        ("host", "IP / 域名 *", "192.168.0.165"),
        ("port", "端口", "22"),
        ("user", "账号 *", "yunfan"),
        ("password", "密码", "留空则连接时再输入"),
        ("device", "设备类型", "Jetson / RK3588 / 云服务器 …"),
        ("tags", "标签", "lan, jetson, cloud（逗号分隔）"),
        ("note", "备注", ""),
    ]

    def __init__(self, root, existing=None):
        self.result = None
        self.top = tk.Toplevel(root)
        self.top.title("编辑会话" if existing else "新增会话")
        self.top.configure(bg=C["panel"])
        self.top.resizable(False, False)
        self.top.transient(root)
        self.top.grab_set()
        self.top.attributes("-topmost", True)

        wrap = tk.Frame(self.top, bg=C["panel"])
        wrap.pack(fill=tk.BOTH, expand=True, padx=22, pady=18)
        tk.Label(wrap, text="填写设备信息", bg=C["panel"], fg=C["text"],
                 font=FONT_TITLE).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 14))

        self.vars = {}
        for i, (key, label, hint) in enumerate(self.FIELDS, start=1):
            r = 1 + (i - 1) * 2
            tk.Label(wrap, text=label, bg=C["panel"], fg=C["text"], font=FONT).grid(
                row=r, column=0, sticky="w", pady=(8, 2))
            var = tk.StringVar()
            entry = ttk.Entry(wrap, textvariable=var, width=34)
            entry.grid(row=r, column=1, sticky="ew", padx=(16, 0), pady=(8, 0), ipady=3)
            if hint:
                tk.Label(wrap, text=hint, bg=C["panel"], fg="#9CA3AF",
                         font=(FAMILY, 8)).grid(
                    row=r + 1, column=1, sticky="w", padx=(18, 0), pady=(0, 2))
            self.vars[key] = var
            if key == "alias":
                self.first = entry

        if existing:
            for key, var in self.vars.items():
                val = existing.get(key, "")
                if isinstance(val, list):
                    val = ", ".join(val)
                var.set("" if val is None else str(val))
        else:
            self.vars["port"].set("22")

        bottom = tk.Frame(self.top, bg=C["panel"])
        bottom.pack(fill=tk.X, padx=22, pady=(0, 18))
        flat_button(bottom, "保存", self.on_save, kind="primary").pack(side=tk.LEFT)
        flat_button(bottom, "取消", self.top.destroy).pack(side=tk.LEFT, padx=8)

        self.top.bind("<Return>", lambda _e: self.on_save())
        self.top.bind("<Escape>", lambda _e: self.top.destroy())
        self.top.update_idletasks()
        # 居中到父窗口
        x = root.winfo_rootx() + (root.winfo_width() - self.top.winfo_width()) // 2
        y = root.winfo_rooty() + (root.winfo_height() - self.top.winfo_height()) // 3
        self.top.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        self.first.focus_set()

    def on_save(self):
        v = {k: var.get().strip() for k, var in self.vars.items()}
        if not v["alias"] or not v["host"] or not v["user"]:
            messagebox.showwarning("提示", "别名、IP、账号为必填项", parent=self.top)
            return
        try:
            port = int(v["port"] or 22)
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            messagebox.showwarning("提示", "端口需为 1-65535 的数字", parent=self.top)
            return
        self.result = {
            "alias": v["alias"], "host": v["host"], "port": port,
            "user": v["user"], "password": v["password"],
            "device": v["device"], "note": v["note"],
            "tags": [t.strip() for t in v["tags"].split(",") if t.strip()],
        }
        self.top.destroy()


class PasswordDialog:
    """连接时临时输入密码，可选保存回凭证库。"""

    def __init__(self, root, entry):
        self.value = None
        self.save = False
        self.top = tk.Toplevel(root)
        self.top.title("输入密码")
        self.top.configure(bg=C["panel"])
        self.top.resizable(False, False)
        self.top.transient(root)
        self.top.grab_set()
        self.top.attributes("-topmost", True)

        wrap = tk.Frame(self.top, bg=C["panel"])
        wrap.pack(fill=tk.BOTH, expand=True, padx=24, pady=18)
        tk.Label(wrap, text=f"{entry['alias']}", bg=C["panel"], fg=C["text"],
                 font=FONT_TITLE).pack(anchor="w")
        tk.Label(wrap, text=f"{entry['user']}@{entry['host']}:{entry['port']} 未保存密码",
                 bg=C["panel"], fg=C["muted"], font=FONT_S).pack(anchor="w", pady=(0, 12))

        self.var = tk.StringVar()
        ent = ttk.Entry(wrap, textvariable=self.var, show="*", width=32)
        ent.pack(fill=tk.X, ipady=4)
        self.save_var = tk.BooleanVar(value=True)
        tk.Checkbutton(wrap, text="保存到凭证库（下次免输入）", variable=self.save_var,
                       bg=C["panel"], fg=C["muted"], font=FONT_S, activebackground=C["panel"],
                       highlightthickness=0, selectcolor=C["panel"]).pack(anchor="w", pady=8)

        btns = tk.Frame(wrap, bg=C["panel"])
        btns.pack(fill=tk.X, pady=(6, 0))
        flat_button(btns, "连接", self.on_ok, kind="primary").pack(side=tk.LEFT)
        flat_button(btns, "取消", self.top.destroy).pack(side=tk.LEFT, padx=8)

        self.top.bind("<Return>", lambda _e: self.on_ok())
        self.top.bind("<Escape>", lambda _e: self.top.destroy())
        self.top.update_idletasks()
        x = root.winfo_rootx() + (root.winfo_width() - self.top.winfo_width()) // 2
        y = root.winfo_rooty() + (root.winfo_height() - self.top.winfo_height()) // 3
        self.top.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        ent.focus_set()

    def on_ok(self):
        pwd = self.var.get()
        if not pwd:
            messagebox.showwarning("提示", "密码不能为空", parent=self.top)
            return
        self.value = pwd
        self.save = bool(self.save_var.get())
        self.top.destroy()


# ---------------------------------------------------------------- 内嵌终端
class TerminalWindow:
    """内嵌 SSH 终端：命令一行行发，输出流式回显。"""

    QUICK = [
        ("uname -a", "uname -a"),
        ("主机名", "hostname && whoami"),
        ("目录", "pwd && ls -al"),
        ("磁盘", "df -h"),
        ("内存", "free -h"),
        ("网卡", "ip -br a"),
        ("GPU", "nvidia-smi"),
        ("温度", "cat /sys/class/thermal/thermal_zone*/temp 2>/dev/null"),
    ]

    def __init__(self, master, entry, password=None, key=None, on_external=None, on_files=None):
        self.entry_data = entry
        self.password = password
        self.key = key
        self.on_external = on_external
        self.on_files = on_files
        self.master_ref = master
        self.session = None
        self.cwd = None
        self.busy = False
        self.cancel_event = threading.Event()
        self.queue = queue.Queue()
        self.history = []
        self.hist_idx = 0
        self.closed = False
        self.prompt = f"{entry['user']}@{entry['alias']}"

        self.top = tk.Toplevel(master)
        self.top.title(f"RZDCL 终端 — {entry['alias']}")
        self.top.configure(bg=C["term_bg"])
        self.top.geometry("980x640")
        self.top.minsize(720, 420)
        self._apply_icon()
        self._build()
        self.top.protocol("WM_DELETE_WINDOW", self.close)
        self.top.bind("<Control-l>", lambda _e: self.clear())
        self.top.after(60, self._pump)
        self._append(f"正在连接 {entry['user']}@{entry['host']}:{entry['port']} …\n", "sys")
        threading.Thread(target=self._connect_worker, daemon=True).start()

    # -- 外观
    def _apply_icon(self):
        ico = resource_path("assets/rzdcl.ico")
        if ico:
            try:
                if os.name == "nt":
                    self.top.iconbitmap(str(ico))
            except tk.TclError:
                pass

    def _build(self):
        head = tk.Frame(self.top, bg=C["term_bg"])
        head.pack(fill=tk.X, padx=16, pady=(12, 8))

        self.dot = tk.Label(head, text="●", bg=C["term_bg"], fg=C["warn"], font=(MONO_FAMILY, 12))
        self.dot.pack(side=tk.LEFT)
        self.title_lbl = tk.Label(
            head, text=f"{self.entry_data['alias']}   {self.entry_data['user']}@{self.entry_data['host']}:{self.entry_data['port']}",
            bg=C["term_bg"], fg="#E5EDFB", font=FONT_B)
        self.title_lbl.pack(side=tk.LEFT, padx=(6, 0))

        right = tk.Frame(head, bg=C["term_bg"])
        right.pack(side=tk.RIGHT)
        for text, cmd in (("文件管理", self.open_files), ("外部终端", self.open_external),
                          ("复制输出", self.copy_all), ("清屏", self.clear),
                          ("重连", self.reconnect), ("断开", self.close)):
            flat_button(right, text, cmd, kind="dark").pack(side=tk.LEFT, padx=(6, 0))

        self.status = tk.Label(self.top, text="连接中…", bg=C["term_bg"], fg=C["header_sub"],
                               font=FONT_S, anchor="w")
        self.status.pack(fill=tk.X, padx=18, pady=(0, 6))

        body = tk.Frame(self.top, bg=C["term_bg"])
        body.pack(fill=tk.BOTH, expand=True, padx=16, pady=(0, 8))

        self.text = tk.Text(body, bg="#08101F", fg=C["term_fg"], insertbackground=C["term_fg"],
                            font=MONO, relief="flat", bd=0, wrap="char",
                            padx=12, pady=10, spacing1=1, spacing3=2, state="disabled")
        scroll = tk.Scrollbar(body, orient="vertical", command=self.text.yview,
                               bg="#22304A", troughcolor="#0E1728", activebackground="#2F4468",
                               relief="flat", bd=0, highlightthickness=0, width=10)
        self.text.configure(yscrollcommand=scroll.set)
        self.text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)

        self.text.tag_configure("sys", foreground="#8B9CC0")
        self.text.tag_configure("cmd", foreground="#4CC9F0")
        self.text.tag_configure("ok", foreground="#4ADE80")
        self.text.tag_configure("err", foreground="#F87171")
        self.text.tag_configure("out", foreground=C["term_fg"])

        quick = tk.Frame(self.top, bg=C["term_bg"])
        quick.pack(fill=tk.X, padx=16)
        tk.Label(quick, text="常用：", bg=C["term_bg"], fg=C["header_sub"], font=FONT_S).pack(side=tk.LEFT)
        for label, cmd in self.QUICK:
            b = tk.Button(quick, text=label, command=lambda c=cmd: self.send_command(c),
                          bg="#152139", fg="#B9C8E4", activebackground="#1E2E4D",
                          activeforeground="#FFFFFF", relief="flat", bd=0, cursor="hand2",
                          font=FONT_S, padx=9, pady=4, highlightthickness=0)
            b.pack(side=tk.LEFT, padx=(4, 0), pady=4)

        inp = tk.Frame(self.top, bg=C["term_bg"])
        inp.pack(fill=tk.X, padx=16, pady=(4, 14))
        tk.Label(inp, text="$", bg=C["term_bg"], fg=C["accent"], font=(MONO_FAMILY, 12, "bold")).pack(side=tk.LEFT, padx=(0, 6))
        self.entry = tk.Entry(inp, bg="#0B1220", fg="#EAF2FF", insertbackground="#EAF2FF",
                              disabledbackground="#0B1220", disabledforeground="#5D6E8C",
                              relief="flat", bd=0, font=MONO, highlightthickness=1,
                              highlightbackground="#1E2E4D", highlightcolor="#2563EB")
        self.entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=6)
        self.entry.bind("<Return>", lambda _e: self.send_command())
        self.entry.bind("<Up>", self._hist_up)
        self.entry.bind("<Down>", self._hist_down)
        flat_button(inp, "发送", self.send_command, kind="primary").pack(side=tk.LEFT, padx=(8, 0))
        flat_button(inp, "中断", self.interrupt, kind="danger").pack(side=tk.LEFT, padx=(6, 0))
        self.entry.configure(state="disabled")

    # -- 连接
    def _connect_worker(self):
        try:
            sess = SshSession(self.entry_data["host"], self.entry_data["port"],
                              self.entry_data["user"], password=self.password,
                              key=self.key)
            banner = sess.connect()
            self.session = sess
            try:
                # 先问一次当前目录，文件管理窗口就能直接落在终端所在的目录
                sess.run("pwd", max_seconds=10)
                self.cwd = sess.cwd
            except Exception:  # noqa: BLE001
                pass
            self.queue.put(("connected", banner))
        except Exception as exc:  # noqa: BLE001
            self.queue.put(("connect_error", str(exc)))

    def _pump(self):
        """主线程轮询队列，保证 UI 操作都在主线程。"""
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "connected":
                    self._on_connected(payload)
                elif kind == "connect_error":
                    self._on_connect_error(payload)
                elif kind == "line":
                    self._append(payload + "\n", "out")
                elif kind == "cmd_done":
                    self._on_cmd_done(*payload)
                elif kind == "cmd_error":
                    self._on_cmd_error(payload)
        except queue.Empty:
            pass
        if not self.closed:
            self.top.after(60, self._pump)

    def _on_connected(self, banner):
        self.dot.configure(fg=C["ok"])
        where = f"{self.entry_data['host']}:{self.entry_data['port']}"
        if self.cwd:
            where += f" · {self.cwd}"
        self.status.configure(text=f"已连接 · {where} · 输入命令后回车")
        if banner:
            self._append(banner.strip() + "\n\n", "sys")
        self._append("已登录，可以开始输入命令。\n", "ok")
        self.entry.configure(state="normal")
        self.entry.focus_set()

    def _on_connect_error(self, err):
        self.dot.configure(fg=C["err"])
        self.status.configure(text="连接失败")
        self._append(f"✗ 连接失败：{err}\n", "err")
        self._append("可通过右上角「外部终端」用系统 ssh 再试，或检查设备是否在线、密码是否正确。\n", "sys")

    def reconnect(self):
        if self.busy:
            self.status.configure(text="命令执行中，先点「中断」再重连")
            return
        try:
            if self.session:
                self.session.close()
        except Exception:  # noqa: BLE001
            pass
        self.session = None
        self.dot.configure(fg=C["warn"])
        self.status.configure(text="重新连接中…")
        self.entry.configure(state="disabled")
        self._append("\n--- 重新连接 ---\n", "sys")
        threading.Thread(target=self._connect_worker, daemon=True).start()

    # -- 输出
    def _append(self, text, tag="out"):
        self.text.configure(state="normal")
        self.text.insert("end", text, tag)
        # 限行，避免超长
        lines = int(self.text.index("end-1c").split(".")[0])
        if lines > 4000:
            self.text.delete("1.0", f"{lines - 3000}.0")
        self.text.see("end")
        self.text.configure(state="disabled")

    def clear(self):
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")

    def copy_all(self):
        data = self.text.get("1.0", "end-1c")
        self.top.clipboard_clear()
        self.top.clipboard_append(data)
        self.status.configure(text="输出已复制到剪贴板")

    # -- 命令
    def send_command(self, command=None):
        if self.busy:
            self.status.configure(text="上一条命令还在执行，请稍候或点「中断」")
            return
        if not self.session or not self.session.alive:
            self.status.configure(text="尚未连接")
            return
        cmd = (command if command is not None else self.entry.get()).strip()
        if not cmd:
            return
        if command is None:
            self.history.append(cmd)
            self.hist_idx = len(self.history)
            self.entry.delete(0, "end")
        self.busy = True
        self.status.configure(text=f"执行中：{cmd[:60]}")
        self._append(f"\n{self.prompt}:~$ {cmd}\n", "cmd")
        threading.Thread(target=self._cmd_worker, args=(cmd,), daemon=True).start()

    def _cmd_worker(self, cmd):
        try:
            code, _ = self.session.run(
                cmd, cancel_event=self.cancel_event,
                on_line=lambda line: self.queue.put(("line", line)),
            )
            self.queue.put(("cmd_done", (code, cmd)))
        except Exception as exc:  # noqa: BLE001
            self.queue.put(("cmd_error", str(exc)))

    def _on_cmd_done(self, code, cmd):
        self.busy = False
        if self.session and self.session.cwd:
            self.cwd = self.session.cwd
        if code != 0:
            self._append(f"[退出码 {code}]\n", "err")
        where = f" · {self.cwd}" if self.cwd else ""
        self.status.configure(text=f"就绪{where} · 输入命令后回车")
        self.entry.configure(state="normal")
        self.entry.focus_set()

    def _on_cmd_error(self, err):
        self.busy = False
        self._append(f"✗ {err}\n", "err")
        self.status.configure(text="会话异常")
        self.dot.configure(fg=C["err"])
        self.entry.configure(state="disabled")

    def interrupt(self):
        if self.busy:
            self.cancel_event.set()
            self.status.configure(text="已发送 Ctrl+C")

    def _hist_up(self, _e=None):
        if self.history:
            self.hist_idx = max(0, self.hist_idx - 1)
            self.entry.delete(0, "end")
            self.entry.insert(0, self.history[self.hist_idx])
        return "break"

    def _hist_down(self, _e=None):
        if self.history:
            self.hist_idx = min(len(self.history), self.hist_idx + 1)
            self.entry.delete(0, "end")
            if self.hist_idx < len(self.history):
                self.entry.insert(0, self.history[self.hist_idx])
        return "break"

    # -- 外部终端（由主窗口统一负责免密准备）
    def open_external(self):
        if callable(self.on_external):
            self.on_external(self.entry_data, self.top)
            return
        try:
            open_external_terminal(self.entry_data, self.key)
            self.status.configure(text="已打开系统 ssh 窗口")
        except FileNotFoundError:
            messagebox.showerror("外部终端", "系统未找到 ssh 命令，请先安装 OpenSSH 客户端", parent=self.top)

    def open_files(self):
        """从终端打开文件管理，落在终端当前目录，并跟随终端 cd。"""
        cwd = self.cwd or (self.session.cwd if self.session else None)
        if callable(self.on_files):
            self.on_files(self.entry_data, cwd, lambda: self.cwd)
            return
        FileBrowserWindow(self.master_ref, self.entry_data, self.password, self.key,
                          initial_path=cwd, follow_getter=lambda: self.cwd)

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.cancel_event.set()
        try:
            if self.session:
                self.session.close()
        finally:
            self.top.destroy()


# ---------------------------------------------------------------- 文件管理
class FileBrowserWindow:
    """SFTP 文件管理：浏览机器人本体目录、上传下载、直接打开远端文件。"""

    def __init__(self, master, entry, password=None, key=None,
                 initial_path=None, follow_getter=None):
        self.entry_data = entry
        self.password, self.key = password, key
        self.client = self.sftp = None
        self.cwd = initial_path or "."
        self.initial_path = initial_path
        self.follow_getter = follow_getter      # 传进来就说明是从终端开的，可跟随
        self._last_terminal_cwd = initial_path
        self._shell_samples = {}        # pid -> 上次采到的 CPU 时间
        self._followed_cwd = None       # 当前认定的“终端目录”
        self.rows = []
        self.busy = False
        self.closed = False
        self.queue = queue.Queue()
        self.local_cache = {}          # 远端路径 -> 本地副本路径
        self.tmp_root = Path(tempfile.gettempdir()) / "RZDCL" / entry["alias"]
        self._last_cb = 0.0

        self.top = tk.Toplevel(master)
        self.top.title(f"RZDCL 文件 — {entry['alias']}")
        self.top.configure(bg=C["bg"])
        self.top.geometry("960x620")
        self.top.minsize(760, 460)
        ico = resource_path("assets/rzdcl.ico")
        if ico:
            try:
                if os.name == "nt":
                    self.top.iconbitmap(str(ico))
            except tk.TclError:
                pass
        self._build()
        self.top.protocol("WM_DELETE_WINDOW", self.close)
        self.top.after(60, self._pump)
        if self.follow_getter:
            self.top.after(1200, self._poll_follow)
        else:
            self.top.after(1500, self._poll_follow)
        threading.Thread(target=self._connect_worker, daemon=True).start()

    def _build(self):
        head = tk.Frame(self.top, bg=C["header"], height=58)
        head.pack(fill=tk.X)
        head.pack_propagate(False)
        self.dot = tk.Label(head, text="●", bg=C["header"], fg=C["warn"], font=(MONO_FAMILY, 12))
        self.dot.pack(side=tk.LEFT, padx=(16, 0), pady=16)
        self.title_lbl = tk.Label(
            head, text=f"{self.entry_data['alias']}   文件",
            bg=C["header"], fg="#EEF4FF", font=(FAMILY, 13, "bold"))
        self.title_lbl.pack(side=tk.LEFT, padx=(8, 0))
        self.addr_lbl = tk.Label(head, text=f"{self.entry_data['user']}@{self.entry_data['host']}",
                                 bg=C["header"], fg=C["header_sub"], font=FONT_S)
        self.addr_lbl.pack(side=tk.LEFT, padx=12)

        right = tk.Frame(head, bg=C["header"])
        right.pack(side=tk.RIGHT, padx=14, pady=11)
        for text, cmd in (("打开", self.open_selected), ("上传", self.upload),
                          ("下载", self.download), ("刷新", lambda: self.load_dir()),
                          ("断开", self.close)):
            flat_button(right, text, cmd, kind="dark").pack(side=tk.LEFT, padx=(6, 0))

        bar = tk.Frame(self.top, bg=C["bg"])
        bar.pack(fill=tk.X, padx=14, pady=(12, 8))
        flat_button(bar, "↑ 上级", self.go_up).pack(side=tk.LEFT)
        flat_button(bar, "主目录", self.go_home).pack(side=tk.LEFT, padx=(6, 0))
        self.path_var = tk.StringVar(value=self.cwd)
        self.path_entry = ttk.Entry(bar, textvariable=self.path_var, font=MONO_S)
        self.path_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=8, ipady=4)
        self.path_entry.bind("<Return>", self._goto_typed)
        self.path_entry.bind("<Escape>", lambda _e: self.path_var.set(self.cwd))
        self.path_entry.bind("<FocusIn>", lambda _e: self.path_entry.selection_range(0, "end"))
        self.follow_var = tk.BooleanVar(value=True)
        tk.Checkbutton(bar, text="跟随终端", variable=self.follow_var,
                       bg=C["bg"], fg=C["text"], activebackground=C["bg"],
                       activeforeground=C["text"], selectcolor="#FFFFFF",
                       font=FONT_S, bd=0, highlightthickness=0).pack(side=tk.LEFT, padx=(0, 6))
        flat_button(bar, "复制路径", self.copy_path).pack(side=tk.LEFT)

        card = tk.Frame(self.top, bg=C["panel"], highlightthickness=1, highlightbackground=C["line"])
        card.pack(fill=tk.BOTH, expand=True, padx=14, pady=(0, 8))
        self.tree = ttk.Treeview(card, columns=("size", "mtime", "mode"), show="tree headings",
                                 selectmode="extended")
        self.tree.heading("#0", text="名称")
        self.tree.heading("size", text="大小")
        self.tree.heading("mtime", text="修改时间")
        self.tree.heading("mode", text="权限")
        self.tree.column("#0", width=420, anchor="w")
        self.tree.column("size", width=100, anchor="e")
        self.tree.column("mtime", width=150, anchor="w")
        self.tree.column("mode", width=90, anchor="w")
        ys = ttk.Scrollbar(card, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ys.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        ys.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.tag_configure("dir", foreground="#1D4ED8")
        self.tree.bind("<Double-1>", self._on_double)
        self.tree.bind("<Return>", lambda _e: self._open_selected())

        self.menu = tk.Menu(self.top, tearoff=0, bg=C["panel"], fg=C["text"], font=FONT_S)
        self.menu.add_command(label="打开文件（下载到本地并启动）", command=self.open_selected)
        self.menu.add_command(label="下载到…", command=self.download)
        self.menu.add_command(label="用本地副本回传覆盖", command=self.sync_back)
        self.menu.add_separator()
        self.menu.add_command(label="上传文件…", command=self.upload)
        self.menu.add_command(label="上传文件夹到当前目录…", command=self.upload_folder)
        self.menu.add_separator()
        self.menu.add_command(label="复制远程路径", command=self.copy_path)
        self.menu.add_command(label="删除（仅文件）", command=self.delete_selected)
        self.menu.add_command(label="刷新", command=lambda: self.load_dir())
        self.tree.bind("<Button-3>", self._on_right)

        bottom = tk.Frame(self.top, bg=C["bg"])
        bottom.pack(fill=tk.X, padx=16, pady=(0, 10))
        self.status = tk.Label(bottom, text="正在连接…", bg=C["bg"], fg=C["muted"],
                               font=FONT_S, anchor="w")
        self.status.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.pct = tk.Label(bottom, text="", bg=C["bg"], fg=C["muted"], font=FONT_S,
                            width=18, anchor="e")
        self.pct.pack(side=tk.RIGHT)
        self.pbar = ttk.Progressbar(bottom, mode="determinate", length=150)
        self.pbar.pack(side=tk.RIGHT, padx=(8, 6))

    # -- 队列
    def _pump(self):
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "ready":
                    self.dot.configure(fg=C["ok"])
                    # 没指定起始目录时，落到“现在终端在哪”而不是永远 home
                    cur = self._current_terminal_cwd()
                    self.cwd = self.initial_path or cur or payload or "."
                    self._last_terminal_cwd = cur or self.cwd
                    self.path_var.set(self.cwd)
                    self.status.configure(text="已连接 · 双击目录进入，双击文件直接打开")
                    self.load_dir()
                elif kind == "listed":
                    self._fill(payload)
                elif kind == "status":
                    self.status.configure(text=payload)
                elif kind == "progress":
                    self._on_progress(*payload)
                elif kind == "progress_done":
                    self.pbar.configure(value=0)
                    self.pct.configure(text="")
                elif kind == "open_local":
                    self._open_local(payload)
                elif kind == "reload":
                    self.load_dir()
                elif kind == "error":
                    self.dot.configure(fg=C["err"])
                    self.status.configure(text=payload)
                    self.pbar.configure(value=0)
                    self.pct.configure(text="")
                elif kind == "connect_error":
                    self.dot.configure(fg=C["err"])
                    self.status.configure(text=payload)
                    messagebox.showerror("文件管理", payload, parent=self.top)
        except queue.Empty:
            pass
        if not self.closed:
            self.top.after(60, self._pump)

    def _connect_worker(self):
        try:
            client, sftp = sftp_connect(self.entry_data, self.key)
        except Exception as exc:  # noqa: BLE001
            self.queue.put(("connect_error", f"连接失败：{exc}"))
            return
        self.client, self.sftp = client, sftp
        try:
            home = sftp.normalize(".")
        except Exception:  # noqa: BLE001
            home = "."
        self.queue.put(("ready", home))

    # -- 进度
    def _on_progress(self, done, total):
        total = total or 0
        if total <= 0:
            self.pbar.configure(value=0)
            self.pct.configure(text=human_size(done))
            return
        pct = min(100, int(done * 100 / total))
        self.pbar.configure(value=pct)
        self.pct.configure(text=f"{pct}%  {human_size(done)}/{human_size(total)}")

    def _cb(self, done, total):
        now = time.time()
        if (total or 0) > done and now - self._last_cb < 0.12:
            return
        self._last_cb = now
        self.queue.put(("progress", (done, total)))

    # -- 目录
    def load_dir(self, path=None):
        if self.sftp is None:
            return
        if path is not None:
            self.cwd = path
        self.busy = True
        self.queue.put(("status", f"正在读取 {self.cwd} …"))
        threading.Thread(target=self._list_worker, daemon=True).start()

    def _list_worker(self):
        try:
            rows = list_dir(self.sftp, self.cwd)
            self.queue.put(("listed", rows))
        except Exception as exc:  # noqa: BLE001
            self.queue.put(("error", f"读取目录失败：{exc}"))
        finally:
            self.busy = False

    def _fill(self, rows):
        self.rows = rows
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        for i, r in enumerate(rows):
            self.tree.insert("", "end", iid=str(i),
                             text=r["name"] + ("/" if r["is_dir"] else ""),
                             values=("—" if r["is_dir"] else human_size(r["size"]),
                                     r["mtime"], r["mode"]),
                             tags=("dir",) if r["is_dir"] else ())
        self.path_var.set(self.cwd)
        self.status.configure(text=f"{self.cwd} · 共 {len(rows)} 项（Ctrl/Shift 可多选）")

    def _selected(self):
        sel = self.tree.selection()
        if not sel:
            return None
        idx = int(sel[0])
        return self.rows[idx] if 0 <= idx < len(self.rows) else None

    def _selected_files(self):
        items = []
        for iid in self.tree.selection():
            idx = int(iid)
            if 0 <= idx < len(self.rows) and not self.rows[idx]["is_dir"]:
                items.append(self.rows[idx])
        return items

    def _remote_of(self, item):
        return posixpath.normpath(posixpath.join(self.cwd, item["name"]))

    def _on_double(self, _event):
        self._open_selected()

    def _open_selected(self):
        item = self._selected()
        if not item:
            return
        if item["is_dir"]:
            self.load_dir(self._remote_of(item))
        else:
            self.open_selected()

    def _on_right(self, event):
        iid = self.tree.identify_row(event.y)
        if iid:
            if iid not in self.tree.selection():
                self.tree.selection_set(iid)
            self.menu.tk_popup(event.x_root, event.y_root)

    def go_up(self):
        if self.cwd in (".", "/"):
            return
        self._manual_nav()
        self.load_dir(posixpath.dirname(self.cwd) or "/")

    def go_home(self):
        if self.sftp is None:
            return
        self._manual_nav()
        try:
            home = self.sftp.normalize(".")
        except Exception:  # noqa: BLE001
            home = "."
        self.load_dir(home)

    def copy_path(self):
        item = self._selected()
        path = self._remote_of(item) if item else self.cwd
        self.top.clipboard_clear()
        self.top.clipboard_append(posixpath.normpath(path))
        self.status.configure(text=f"已复制：{posixpath.normpath(path)}")

    # -- 路径输入 / 跟随终端
    def _goto_typed(self, _event=None):
        if self.sftp is None:
            return "break"
        text = self.path_var.get().strip()
        if not text:
            return "break"
        try:
            target = sftp_expand(self.sftp, text)
        except Exception:  # noqa: BLE001
            target = text
        self._manual_nav()
        self.load_dir(target)
        return "break"

    def _manual_nav(self):
        """手动跳目录时把“终端上次目录”对齐，避免立刻被跟随逻辑拽回去。"""
        cur = self._current_terminal_cwd()
        if cur:
            self._last_terminal_cwd = cur

    def _current_terminal_cwd(self):
        """远端最近“动过”的那个 shell 在哪个目录。

        每 1.5 秒采样一次各 shell 进程的累计 CPU 时间，谁涨了就说明刚有人在那敲
        命令，取它的 /proc/pid/cwd。内嵌终端和系统 ssh 开的窗口都能覆盖到，
        而且不需要改远端任何配置。
        """
        rows = []
        if self.client is not None:
            try:
                rows = list_shell_cwd(self.entry_data, client=self.client)
            except Exception:  # noqa: BLE001
                rows = []
        moved = []
        for r in rows:
            prev = self._shell_samples.get(r["pid"])
            self._shell_samples[r["pid"]] = r["runtime"]
            if prev is not None and r["runtime"] > prev:
                moved.append((r["runtime"] - prev, r["cwd"]))
        if moved:
            self._followed_cwd = max(moved)[1]
        elif self._followed_cwd is None and rows:
            self._followed_cwd = max(rows, key=lambda r: r["runtime"])["cwd"]
        if not self._followed_cwd and self.follow_getter:
            try:
                self._followed_cwd = self.follow_getter()
            except Exception:  # noqa: BLE001
                pass
        return self._followed_cwd

    def _poll_follow(self):
        if self.closed:
            return
        if (getattr(self, "follow_var", None) is not None and self.follow_var.get()
                and self.sftp is not None and not self.busy):
            cur = self._current_terminal_cwd()
            if cur and cur != self._last_terminal_cwd:
                self._last_terminal_cwd = cur
                self.status.configure(text=f"终端切到 {cur}，跟随跳转…")
                self.load_dir(cur)
            elif cur:
                self._last_terminal_cwd = cur
        if not self.closed:
            self.top.after(1500, self._poll_follow)

    # -- 打开文件（下载到本地缓存后交给系统默认程序）
    def _cache_path(self, remote):
        name = posixpath.basename(remote) or "file"
        tag = hashlib.md5(remote.encode("utf-8")).hexdigest()[:8]
        self.tmp_root.mkdir(parents=True, exist_ok=True)
        return str(self.tmp_root / f"{tag}_{name}")

    def open_selected(self):
        if self.sftp is None:
            messagebox.showinfo("提示", "还没连上，请稍等连接完成", parent=self.top)
            return
        items = self._selected_files()
        if not items:
            messagebox.showinfo("提示", "请先选中文件（目录请双击进入）", parent=self.top)
            return
        threading.Thread(target=self._open_worker, args=(items,), daemon=True).start()

    def _open_worker(self, items):
        for item in items:
            remote = self._remote_of(item)
            try:
                local = self.local_cache.get(remote) or self._cache_path(remote)
                self.queue.put(("status", f"正在把 {item['name']} 取到本地 …"))
                self.queue.put(("progress", (0, item["size"])))
                self.sftp.get(remote, local, callback=self._cb)
                self.local_cache[remote] = local
                self.queue.put(("open_local", local))
            except Exception as exc:  # noqa: BLE001
                self.queue.put(("error", f"{item['name']} 打开失败：{exc}"))
        self.queue.put(("progress_done", None))

    def _open_local(self, local):
        try:
            open_local(local)
            self.status.configure(text=f"已用系统默认程序打开：{local}")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("打开文件", f"打不开：{local}\n{exc}", parent=self.top)

    # -- 上传
    def upload(self):
        if self.sftp is None:
            messagebox.showinfo("提示", "还没连上，请稍等连接完成", parent=self.top)
            return
        files = filedialog.askopenfilenames(parent=self.top, title="选择要上传的文件（可多选）")
        if not files:
            return
        threading.Thread(target=self._upload_many, args=(list(files), self.cwd), daemon=True).start()

    def _upload_many(self, files, remote_dir):
        ok = 0
        for path in files:
            local = Path(path)
            remote = posixpath.join(remote_dir, local.name)
            self.queue.put(("status", f"正在上传 {local.name} …"))
            self.queue.put(("progress", (0, local.stat().st_size)))
            try:
                self.sftp.put(str(local), remote, callback=self._cb)
            except Exception as exc:  # noqa: BLE001
                self.queue.put(("error", f"{local.name} 上传失败：{exc}"))
                continue
            self.local_cache[remote] = str(local)
            ok += 1
        self.queue.put(("progress_done", None))
        self.queue.put(("status", f"上传完成 {ok}/{len(files)} → {remote_dir}"))
        self.queue.put(("reload", None))

    def upload_folder(self):
        if self.sftp is None:
            messagebox.showinfo("提示", "还没连上，请稍等连接完成", parent=self.top)
            return
        folder = filedialog.askdirectory(parent=self.top, title="选择要上传的文件夹")
        if not folder:
            return
        threading.Thread(target=self._upload_tree, args=(folder, self.cwd), daemon=True).start()

    def _ensure_remote_dir(self, rdir):
        if not rdir or rdir == ".":
            return
        parts = [p for p in rdir.split("/") if p and p != "."]
        cur = "/" if rdir.startswith("/") else ""
        for p in parts:
            cur = posixpath.join(cur, p) if cur else p
            try:
                self.sftp.stat(cur)
            except IOError:
                try:
                    self.sftp.mkdir(cur)
                except Exception:  # noqa: BLE001
                    pass

    def _upload_tree(self, local_root, remote_dir):
        local_root = Path(local_root)
        base = posixpath.join(remote_dir, local_root.name)
        self.queue.put(("status", f"正在上传文件夹 {local_root.name} …"))
        self._ensure_remote_dir(base)
        count = 0
        for root, _dirs, files in os.walk(local_root):
            rel = Path(root).relative_to(local_root).as_posix()
            rdir = base if rel == "." else posixpath.join(base, rel)
            self._ensure_remote_dir(rdir)
            for name in files:
                lp = Path(root) / name
                rp = posixpath.join(rdir, name)
                self.queue.put(("status", f"正在上传 {name} …"))
                try:
                    self.queue.put(("progress", (0, lp.stat().st_size)))
                    self.sftp.put(str(lp), rp, callback=self._cb)
                    self.local_cache[rp] = str(lp)
                    count += 1
                except Exception as exc:  # noqa: BLE001
                    self.queue.put(("error", f"{name} 上传失败：{exc}"))
        self.queue.put(("progress_done", None))
        self.queue.put(("status", f"文件夹上传完成：{count} 个文件 → {base}"))
        self.queue.put(("reload", None))

    # -- 下载
    def download(self):
        if self.sftp is None:
            messagebox.showinfo("提示", "还没连上，请稍等连接完成", parent=self.top)
            return
        items = self._selected_files()
        if not items:
            messagebox.showinfo("提示", "请先选中文件（可按住 Ctrl / Shift 多选）", parent=self.top)
            return
        if len(items) == 1:
            local = filedialog.asksaveasfilename(parent=self.top, title="另存为",
                                                 initialfile=items[0]["name"])
            if not local:
                return
            targets = [(items[0], local)]
        else:
            folder = filedialog.askdirectory(parent=self.top, title="下载到哪个文件夹")
            if not folder:
                return
            targets = [(it, str(Path(folder) / it["name"])) for it in items]
        threading.Thread(target=self._download_many, args=(targets,), daemon=True).start()

    def _download_many(self, targets):
        ok = 0
        for item, local in targets:
            remote = self._remote_of(item)
            self.queue.put(("status", f"正在下载 {item['name']} …"))
            self.queue.put(("progress", (0, item["size"])))
            try:
                Path(local).parent.mkdir(parents=True, exist_ok=True)
                self.sftp.get(remote, local, callback=self._cb)
            except Exception as exc:  # noqa: BLE001
                self.queue.put(("error", f"{item['name']} 下载失败：{exc}"))
                continue
            self.local_cache[remote] = local
            ok += 1
        self.queue.put(("progress_done", None))
        self.queue.put(("status", f"下载完成 {ok}/{len(targets)}"))

    # -- 回传
    def sync_back(self):
        items = self._selected_files()
        if len(items) != 1:
            messagebox.showinfo("提示", "请只选中一个文件", parent=self.top)
            return
        remote = self._remote_of(items[0])
        local = self.local_cache.get(remote)
        if not local or not Path(local).exists():
            messagebox.showinfo(
                "提示",
                "本机还没有这个文件的副本。\n先点「打开文件」或「下载」，改完再回传。",
                parent=self.top,
            )
            return
        if not messagebox.askyesno(
            "回传覆盖",
            f"用本地副本覆盖远端？\n\n远端：{remote}\n本地：{local}",
            parent=self.top,
        ):
            return
        threading.Thread(target=self._sync_back_worker,
                         args=(local, remote, items[0]["size"]), daemon=True).start()

    def _sync_back_worker(self, local, remote, size):
        self.queue.put(("status", f"正在回传 {posixpath.basename(remote)} …"))
        if size and size <= 8 * 1024 * 1024:
            try:
                with self.sftp.open(remote, "r") as fh:
                    old = fh.read()
                with self.sftp.open(remote + ".rzdcl.bak", "w") as fh:
                    fh.write(old)
            except Exception:  # noqa: BLE001
                pass
        try:
            self.queue.put(("progress", (0, Path(local).stat().st_size)))
            self.sftp.put(local, remote, callback=self._cb)
            self.queue.put(("status", f"已回传：{remote}（远端旧版存为 .rzdcl.bak）"))
            self.queue.put(("reload", None))
        except Exception as exc:  # noqa: BLE001
            self.queue.put(("error", f"回传失败：{exc}"))
        self.queue.put(("progress_done", None))

    # -- 删除
    def delete_selected(self):
        items = self._selected_files()
        if not items:
            messagebox.showinfo("提示", "只支持删除文件，目录请到终端里处理", parent=self.top)
            return
        names = "、".join(it["name"] for it in items)
        if not messagebox.askyesno("确认删除", f"删除远端文件？\n\n{names}", parent=self.top):
            return
        threading.Thread(target=self._delete_worker,
                         args=([self._remote_of(it) for it in items],), daemon=True).start()

    def _delete_worker(self, remotes):
        ok = 0
        for remote in remotes:
            try:
                self.sftp.remove(remote)
                ok += 1
            except Exception as exc:  # noqa: BLE001
                self.queue.put(("error", f"删除失败 {remote}：{exc}"))
        self.queue.put(("status", f"已删除 {ok}/{len(remotes)} 个文件"))
        self.queue.put(("reload", None))

    def close(self):
        if self.closed:
            return
        self.closed = True
        for obj in (self.sftp, self.client):
            try:
                if obj is not None:
                    obj.close()
            except Exception:  # noqa: BLE001
                pass
        self.top.destroy()


# ---------------------------------------------------------------- 主窗口
class RZDCLApp(tk.Tk):
    def __init__(self, config_path):
        super().__init__()
        self.config_path = Path(config_path)
        self.entries = []
        self.status_cache = {}
        self.row_map = {}
        self.current_alias = None
        self.ui_queue = queue.Queue()
        self.key = SshKey()
        self.key.harden()          # 旧密钥顺手修权限，否则 ssh.exe 会拒绝加载
        self._logo_imgs = []

        self.title(f"{APP_NAME} · {APP_SUB}")
        self.configure(bg=C["bg"])
        self.geometry("1080x760")
        self.minsize(960, 660)
        pick_ui_font(self)
        self._apply_icon()
        self._setup_style()
        self._build_header()
        self._build_body()
        self._build_footer()
        self.reload_devices()
        self.log(f"已加载凭证库（{len(self.entries)} 台设备）：{self.config_path}")
        self.after(120, self._pump_ui)

    # -- 外观
    def _apply_icon(self):
        ico = resource_path("assets/rzdcl.ico")
        if ico:
            try:
                if os.name == "nt":
                    self.iconbitmap(str(ico))
            except tk.TclError:
                pass
        png = resource_path("assets/rzdcl_mark.png") or resource_path("assets/rzdcl.png")
        if png:
            try:
                img = tk.PhotoImage(file=str(png))
                factor = max(img.width() // 46, 1)
                self.logo_img = img.subsample(max(factor, 1))
                self._logo_imgs.append(self.logo_img)
                self.iconphoto(True, img)
            except tk.TclError:
                self.logo_img = None
        else:
            self.logo_img = None

    def _setup_style(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Treeview", background=C["panel"], fieldbackground=C["panel"],
                        rowheight=32, borderwidth=0, font=FONT)
        style.configure("Treeview.Heading", background="#EEF2F9", foreground="#475569",
                        font=(FAMILY, 9, "bold"), relief="flat", borderwidth=0)
        style.map("Treeview", background=[("selected", "#DBEAFE")],
                  foreground=[("selected", C["text"])])
        style.configure("TScrollbar", background="#D7DEEA", troughcolor=C["panel"],
                        borderwidth=0, arrowsize=12)

    def _build_header(self):
        head = tk.Frame(self, bg=C["header"], height=84)
        head.pack(fill=tk.X)
        head.pack_propagate(False)

        left = tk.Frame(head, bg=C["header"])
        left.pack(side=tk.LEFT, padx=(18, 0), pady=12)
        if getattr(self, "logo_img", None) is not None:
            lbl = tk.Label(left, image=self.logo_img, bg=C["header"], bd=0)
            lbl.pack(side=tk.LEFT)
            self._logo_imgs.append(self.logo_img)
        else:
            tk.Label(left, text="R", bg=C["accent"], fg="#FFFFFF", font=("Segoe UI", 22, "bold"),
                     width=2, height=1).pack(side=tk.LEFT)
        texts = tk.Frame(left, bg=C["header"])
        texts.pack(side=tk.LEFT, padx=12)
        tk.Label(texts, text=APP_NAME, bg=C["header"], fg="#FFFFFF",
                 font=("Segoe UI", 20, "bold")).pack(anchor="w")
        tk.Label(texts, text=APP_SUB, bg=C["header"], fg=C["header_sub"], font=FONT_S).pack(anchor="w")

        right = tk.Frame(head, bg=C["header"])
        right.pack(side=tk.RIGHT, padx=18, pady=16)
        self.clock = tk.Label(right, text="", bg=C["header"], fg=C["header_sub"], font=FONT_S)
        self.clock.pack(side=tk.RIGHT, padx=(10, 0))
        flat_button(right, "打开凭证库", self.open_config, kind="dark").pack(side=tk.RIGHT)
        self._tick_clock()

    def _tick_clock(self):
        self.clock.configure(text=time.strftime("%Y-%m-%d  %H:%M:%S"))
        self.after(1000, self._tick_clock)

    def _pump_ui(self):
        try:
            while True:
                kind, *payload = self.ui_queue.get_nowait()
                if kind == "test_done":
                    self._test_done(*payload)
                elif kind == "key_result":
                    self._key_result(*payload)
                elif kind == "external_result":
                    self._external_result(*payload)
        except queue.Empty:
            pass
        try:
            self.after(120, self._pump_ui)
        except tk.TclError:
            pass

    def _build_body(self):
        body = tk.Frame(self, bg=C["bg"])
        body.pack(fill=tk.BOTH, expand=True, padx=16, pady=(14, 8))

        # 左：设备列表
        left = tk.Frame(body, bg=C["panel"], highlightthickness=1, highlightbackground=C["line"])
        left.pack(side=tk.LEFT, fill=tk.Y)
        left.configure(width=380)
        left.pack_propagate(False)

        tools = tk.Frame(left, bg=C["panel"])
        tools.pack(fill=tk.X, padx=14, pady=(14, 6))
        tk.Label(tools, text="设备列表", bg=C["panel"], fg=C["text"], font=FONT_B).pack(side=tk.LEFT)
        self.count_lbl = tk.Label(tools, text="", bg=C["panel"], fg=C["muted"], font=FONT_S)
        self.count_lbl.pack(side=tk.RIGHT)
        flat_button(tools, "刷新", self.reload_devices, kind="ghost").pack(side=tk.RIGHT, padx=(0, 8))

        search_box = tk.Frame(left, bg="#F8FAFE", highlightthickness=1, highlightbackground=C["line"])
        search_box.pack(fill=tk.X, padx=14, pady=(0, 8))
        tk.Label(search_box, text="筛选", bg="#F8FAFE", fg=C["muted"], font=FONT_S).pack(side=tk.LEFT, padx=(8, 4))
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self.populate_tree())
        search = tk.Entry(search_box, textvariable=self.search_var, bg="#F8FAFE", fg=C["text"],
                          relief="flat", bd=0, font=FONT_S, highlightthickness=0)
        search.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=5, padx=(0, 6))

        tree_wrap = tk.Frame(left, bg=C["panel"])
        tree_wrap.pack(fill=tk.BOTH, expand=True, padx=14)
        self.dev_tree = ttk.Treeview(tree_wrap, columns=("alias", "host", "user"),
                                     show="headings", selectmode="browse")
        self.dev_tree.heading("alias", text="别名")
        self.dev_tree.heading("host", text="地址")
        self.dev_tree.heading("user", text="账号")
        self.dev_tree.column("alias", width=136, anchor="w")
        self.dev_tree.column("host", width=130, anchor="w")
        self.dev_tree.column("user", width=80, anchor="w")
        scroll = ttk.Scrollbar(tree_wrap, orient="vertical", command=self.dev_tree.yview)
        self.dev_tree.configure(yscrollcommand=scroll.set)
        self.dev_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.dev_tree.bind("<<TreeviewSelect>>", lambda _e: self.show_detail())
        self.dev_tree.bind("<Double-1>", self._on_tree_double)
        self.dev_tree.bind("<Return>", lambda _e: self.open_terminal())
        self.dev_tree.tag_configure("offline", foreground="#94A3B8")
        self.dev_tree.tag_configure("online", foreground=C["ok"])

        # 右键菜单
        self.menu = tk.Menu(self, tearoff=0, bg=C["panel"], fg=C["text"], font=FONT_S)
        self.menu.add_command(label="打开终端", command=self.open_terminal)
        self.menu.add_command(label="外部终端（免密）", command=self.open_external)
        self.menu.add_command(label="文件管理（SFTP）", command=self.open_files)
        self.menu.add_separator()
        self.menu.add_command(label="配置免密登录", command=self.setup_key)
        self.menu.add_command(label="编辑", command=self.edit_entry)
        self.menu.add_command(label="测试连接", command=self.test_entry)
        self.menu.add_command(label="复制密码", command=self.copy_password)
        self.menu.add_separator()
        self.menu.add_command(label="删除", command=self.delete_entry)
        self.dev_tree.bind("<Button-3>", self._on_right_click)

        row = tk.Frame(left, bg=C["panel"])
        row.pack(fill=tk.X, padx=14, pady=12)
        flat_button(row, "＋ 新增", self.add_entry, kind="primary").pack(side=tk.LEFT, expand=True, fill=tk.X)
        flat_button(row, "删除", self.delete_entry, kind="danger").pack(side=tk.LEFT, padx=(8, 0))

        # 右：详情
        right = tk.Frame(body, bg=C["bg"])
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(14, 0))

        card = tk.Frame(right, bg=C["panel"], highlightthickness=1, highlightbackground=C["line"])
        card.pack(fill=tk.BOTH, expand=True)

        top = tk.Frame(card, bg=C["panel"])
        top.pack(fill=tk.X, padx=22, pady=(16, 2))
        self.detail_alias = tk.Label(top, text="未选择设备", bg=C["panel"], fg=C["text"], font=FONT_TITLE)
        self.detail_alias.pack(side=tk.LEFT)
        self.detail_state = tk.Label(top, text="", bg=C["panel"], fg=C["muted"], font=FONT_S)
        self.detail_state.pack(side=tk.RIGHT)

        self.detail_sub = tk.Label(card, text="在左侧选中一台设备，回车即可直连", bg=C["panel"],
                                   fg=C["muted"], font=FONT_S)
        self.detail_sub.pack(anchor="w", padx=22, pady=(0, 10))

        info = tk.Frame(card, bg=C["panel"])
        info.pack(fill=tk.X, padx=22)
        self.info_vars = {}
        for i, (key, label) in enumerate((("host", "地址"), ("user", "账号"), ("password", "密码"),
                                          ("auth", "免密"), ("device", "设备"), ("tags", "标签"),
                                          ("note", "备注"))):
            tk.Label(info, text=label, bg=C["panel"], fg=C["muted"], font=FONT_S,
                     width=6, anchor="w").grid(row=i, column=0, sticky="w", pady=2)
            var = tk.StringVar(value="—")
            tk.Label(info, textvariable=var, bg=C["panel"], fg=C["text"], font=FONT,
                     anchor="w", justify="left", wraplength=430).grid(row=i, column=1, sticky="w", pady=2)
            self.info_vars[key] = var

        actions = tk.Frame(card, bg=C["panel"])
        actions.pack(fill=tk.X, padx=22, pady=(14, 4))
        flat_button(actions, "▶  打开终端（回车）", self.open_terminal, kind="primary").pack(side=tk.LEFT)
        flat_button(actions, "外部终端（免密）", self.open_external).pack(side=tk.LEFT, padx=8)
        flat_button(actions, "文件管理", self.open_files).pack(side=tk.LEFT)

        actions2 = tk.Frame(card, bg=C["panel"])
        actions2.pack(fill=tk.X, padx=22, pady=(4, 6))
        flat_button(actions2, "配置免密", self.setup_key).pack(side=tk.LEFT)
        flat_button(actions2, "编辑", self.edit_entry).pack(side=tk.LEFT, padx=8)
        flat_button(actions2, "测试连接", self.test_entry).pack(side=tk.LEFT)
        flat_button(actions2, "复制密码", self.copy_password).pack(side=tk.LEFT, padx=8)

        log_wrap = tk.Frame(card, bg=C["panel"])
        log_wrap.pack(fill=tk.BOTH, expand=True, padx=22, pady=(16, 0))
        tk.Label(log_wrap, text="操作记录", bg=C["panel"], fg=C["text"], font=FONT_B).pack(anchor="w", pady=(0, 6))
        self.log_box = tk.Text(log_wrap, height=4, bg="#F8FAFE", fg="#64748B", font=MONO_S,
                               relief="flat", bd=0, wrap="char", padx=10, pady=8,
                               highlightthickness=1, highlightbackground=C["line"], state="disabled")
        self.log_box.pack(fill=tk.BOTH, expand=True)
        self.log_box.tag_configure("time", foreground="#94A3B8")

        self.tip = tk.Label(
            card, bg=C["panel"], fg="#94A3B8", font=(FAMILY, 9),
            justify="left", anchor="w", wraplength=580,
            text=("双击设备直连内嵌终端。「外部终端」用系统 ssh 开原生窗口，配好免密后不用再输密码。\n"
                  "「文件管理」可浏览机器人本体目录、上传下载；AI 侧可用 RZDCL-cli.exe 走同一套凭证。"))
        self.tip.pack(side=tk.BOTTOM, anchor="w", padx=22, pady=(4, 12))

    def _build_footer(self):
        bar = tk.Frame(self, bg=C["bg"], height=28)
        bar.pack(fill=tk.X, padx=18, pady=(0, 8))
        self.footer = tk.Label(bar, text="", bg=C["bg"], fg=C["muted"], font=FONT_S, anchor="w")
        self.footer.pack(side=tk.LEFT)
        self.footer_path = tk.Label(bar, text=str(self.config_path), bg=C["bg"], fg="#9AA6BC", font=(MONO_FAMILY, 9))
        self.footer_path.pack(side=tk.RIGHT)

    # -- 数据
    def log(self, msg):
        if not hasattr(self, "log_box"):
            return
        now = time.strftime("%H:%M:%S")
        self.log_box.configure(state="normal")
        self.log_box.insert("end", now + "  ", "time")
        self.log_box.insert("end", msg + "\n")
        if int(self.log_box.index("end-1c").split(".")[0]) > 80:
            self.log_box.delete("1.0", "17.0")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def reload_devices(self):
        try:
            self.entries = [normalize_entry(e) for e in load_entries(self.config_path)]
        except RuntimeError as exc:
            messagebox.showerror("凭证库", str(exc), parent=self)
            self.entries = []
        self.populate_tree()
        self.show_detail()
        self.footer.configure(text=f"共 {len(self.entries)} 台设备 · 双击直连")

    def populate_tree(self):
        keyword = self.search_var.get().strip().lower() if hasattr(self, "search_var") else ""
        keep = getattr(self, "current_alias", None)
        for iid in self.dev_tree.get_children():
            self.dev_tree.delete(iid)
        self.row_map = {}
        shown = 0
        for idx, e in enumerate(self.entries):
            hay = " ".join([e["alias"], e["host"], e["user"], e.get("device", ""),
                            ",".join(e.get("tags", [])), e.get("note", "")]).lower()
            if keyword and keyword not in hay:
                continue
            state = self.status_cache.get(e["alias"])
            tag = "online" if state is True else ("offline" if state is False else "")
            iid = f"r{idx}"
            self.dev_tree.insert("", "end", iid=iid,
                                 values=(e["alias"], f"{e['host']}:{e['port']}", e["user"]),
                                 tags=(tag,) if tag else ())
            self.row_map[iid] = e["alias"]
            if keep and e["alias"] == keep:
                self.dev_tree.selection_set(iid)
            shown += 1
        if hasattr(self, "count_lbl"):
            self.count_lbl.configure(text=f"{shown} / {len(self.entries)}")
        if not self.dev_tree.selection():
            rows = self.dev_tree.get_children()
            if rows:
                self.dev_tree.selection_set(rows[0])
                self.dev_tree.focus(rows[0])

    def selected_entry(self):
        sel = self.dev_tree.selection()
        if not sel:
            return None
        alias = self.row_map.get(sel[0])
        if alias is None:
            return None
        for e in self.entries:
            if e["alias"] == alias:
                return e
        return None

    def show_detail(self):
        e = self.selected_entry()
        self.current_alias = e["alias"] if e else None
        if not e:
            self.detail_alias.configure(text="未选择设备")
            self.detail_sub.configure(text="在左侧选中一台设备，回车即可直连")
            for var in self.info_vars.values():
                var.set("—")
            self.detail_state.configure(text="")
            return
        self.detail_alias.configure(text=e["alias"])
        self.detail_sub.configure(text=f"{e['user']}@{e['host']}:{e['port']}")
        self.info_vars["host"].set(f"{e['host']}:{e['port']}")
        self.info_vars["user"].set(e["user"])
        self.info_vars["password"].set("已保存（连接时自动使用）" if e.get("password") else "未设置（连接时输入）")
        self.info_vars["auth"].set("已配置（外部终端免密直连）" if e.get("key") else "未配置（点「配置免密」一键写入公钥）")
        self.info_vars["device"].set(e.get("device") or "—")
        self.info_vars["tags"].set("、".join(e.get("tags", [])) or "—")
        self.info_vars["note"].set(e.get("note") or "—")
        state = self.status_cache.get(e["alias"])
        self.detail_state.configure(
            text="● TCP 可达" if state is True else ("● 连接失败" if state is False else ""),
            fg=C["ok"] if state is True else C["err"],
        )

    def _save(self):
        save_entries(self.config_path, self.entries)
        self.reload_devices()

    def _on_tree_double(self, _event):
        self.open_terminal()

    def _on_right_click(self, event):
        iid = self.dev_tree.identify_row(event.y)
        if iid:
            self.dev_tree.selection_set(iid)
            self.show_detail()
            self.menu.tk_popup(event.x_root, event.y_root)

    # -- 动作
    def add_entry(self):
        dlg = EntryDialog(self)
        self.wait_window(dlg.top)
        if not dlg.result:
            return
        if any(e["alias"] == dlg.result["alias"] for e in self.entries):
            if not messagebox.askyesno("别名重复", f"「{dlg.result['alias']}」已存在，是否覆盖？", parent=self):
                return
            self.entries = [e for e in self.entries if e["alias"] != dlg.result["alias"]]
        self.entries.append(normalize_entry(dlg.result))
        self._save()
        self.footer.configure(text=f"已保存 {dlg.result['alias']}")
        self.log(f"新增会话 {dlg.result['alias']} → {dlg.result['host']}:{dlg.result['port']}")

    def edit_entry(self):
        e = self.selected_entry()
        if not e:
            messagebox.showinfo("提示", "请先在左侧选中一台设备", parent=self)
            return
        dlg = EntryDialog(self, e)
        self.wait_window(dlg.top)
        if not dlg.result:
            return
        idx = next((i for i, x in enumerate(self.entries) if x["alias"] == e["alias"]), None)
        if idx is None:
            return
        self.entries[idx] = normalize_entry(dlg.result)
        self.entries[idx]["key"] = bool(e.get("key"))
        self.status_cache.pop(dlg.result["alias"], None)
        self.current_alias = self.entries[idx]["alias"]
        self._save()
        for iid, alias in self.row_map.items():
            if alias == self.entries[idx]["alias"]:
                self.dev_tree.selection_set(iid)
                break
        self.show_detail()
        self.log(f"已更新会话 {self.entries[idx]['alias']}")

    def delete_entry(self):
        e = self.selected_entry()
        if not e:
            messagebox.showinfo("提示", "请先在左侧选中一台设备", parent=self)
            return
        if not messagebox.askyesno("确认删除", f"删除会话「{e['alias']}」？\n（只删本地凭证，不影响设备）", parent=self):
            return
        self.entries = [x for x in self.entries if x["alias"] != e["alias"]]
        self._save()
        self.log(f"已删除会话 {e['alias']}")

    def test_entry(self):
        e = self.selected_entry()
        if not e:
            messagebox.showinfo("提示", "请先在左侧选中一台设备", parent=self)
            return
        self.footer.configure(text=f"正在测试 {e['host']}:{e['port']} …")

        def worker():
            try:
                with socket.create_connection((e["host"], e["port"]), timeout=4):
                    ok = True
                err = ""
            except OSError as exc:
                ok, err = False, str(exc)
            self.ui_queue.put(("test_done", e, ok, err))

        threading.Thread(target=worker, daemon=True).start()

    def _test_done(self, entry, ok, err):
        self.status_cache[entry["alias"]] = ok
        self.populate_tree()
        if self.selected_entry() and self.selected_entry()["alias"] == entry["alias"]:
            self.show_detail()
        if ok:
            self.footer.configure(text=f"{entry['alias']} TCP 可达 {entry['host']}:{entry['port']}")
            self.log(f"测试 {entry['alias']} → TCP 可达 {entry['host']}:{entry['port']}")
        else:
            self.footer.configure(text=f"{entry['alias']} 连接失败")
            self.log(f"测试 {entry['alias']} → 失败 {err}")
            messagebox.showerror("测试连接", f"❌ {entry['host']}:{entry['port']}\n{err}", parent=self)

    def copy_password(self):
        e = self.selected_entry()
        if not e:
            return
        if not e.get("password"):
            messagebox.showinfo("提示", "这台设备未保存密码", parent=self)
            return
        self.clipboard_clear()
        self.clipboard_append(e["password"])
        self.footer.configure(text=f"已复制 {e['alias']} 的密码")
        self.log(f"复制 {e['alias']} 的密码到剪贴板")

    def _persist(self, note=None):
        try:
            save_entries(self.config_path, self.entries)
        except OSError as exc:
            messagebox.showwarning("保存失败", str(exc), parent=self)
            return
        if note:
            self.footer.configure(text=note)
            self.log(note)

    def _ask_password(self, entry):
        """取密码：已存则直接用，否则弹窗输入（可勾选保存）。取消返回 None。"""
        if entry.get("password"):
            return entry["password"]
        dlg = PasswordDialog(self, entry)
        self.wait_window(dlg.top)
        if not dlg.value:
            return None
        if dlg.save:
            entry["password"] = dlg.value
            self._persist(f"已为 {entry['alias']} 保存密码")
        return dlg.value

    def open_terminal(self):
        e = self.selected_entry()
        if not e:
            messagebox.showinfo("提示", "请先在左侧选中一台设备", parent=self)
            return
        password = e.get("password", "")
        if not password and not e.get("key"):
            password = self._ask_password(e)
            if password is None:
                return
        TerminalWindow(self, e, password, key=self.key,
                       on_external=self.open_external, on_files=self.open_files)
        self.log(f"打开终端 {e['alias']}（{e['user']}@{e['host']}:{e['port']}）")

    # -- 免密 / 外部终端
    def _open_external_window(self, entry):
        try:
            open_external_terminal(entry, self.key)
            self.footer.configure(text=f"已打开 {entry['alias']} 外部终端（免密直连）")
            self.log(f"打开外部终端 {entry['alias']}（免密，无需输密码）")
        except FileNotFoundError:
            messagebox.showerror("外部终端", "系统未找到 ssh 命令，请先安装 OpenSSH 客户端", parent=self)

    def _launch_external(self, entry):
        """先让 ssh.exe 自己验证一次密钥，通过了再开窗，免得开出来还是要密码。"""
        self.footer.configure(text=f"正在校验 {entry['alias']} 免密…")

        def worker():
            try:
                ok, detail = ssh_cli_key_ok(entry, self.key)
            except Exception as exc:  # noqa: BLE001
                ok, detail = False, str(exc)
            self.ui_queue.put(("external_result", entry, ok, detail))

        threading.Thread(target=worker, daemon=True).start()

    def _external_result(self, entry, ok, detail):
        if ok is False:
            self.log(f"{entry['alias']} 免密校验未通过：{detail}")
            self.footer.configure(text=f"{entry['alias']} 免密校验未通过")
            ask = messagebox.askyesno(
                "免密不可用",
                f"{entry['alias']} 的密钥登录没通过系统 ssh 校验：\n\n{detail[:400]}\n\n"
                "要不要改用普通 ssh 窗口（需要手动输一次密码）？\n"
                "（也可点「配置免密」重新写一次公钥）",
                parent=self,
            )
            if ask:
                self._fallback_external(entry, detail)
            return
        self._open_external_window(entry)

    def _fallback_external(self, entry, reason):
        """免密没配成：退回原生 ssh 窗口 + 密码进剪贴板，并把原因说清楚。"""
        if entry.get("password"):
            self.clipboard_clear()
            self.clipboard_append(entry["password"])
        try:
            open_external_terminal(entry, None)
        except FileNotFoundError:
            messagebox.showerror("外部终端", "系统未找到 ssh 命令，请先安装客户端", parent=self)
            return
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("外部终端", f"打不开终端窗口：{exc}", parent=self)
            return
        self.footer.configure(text=f"{entry['alias']} 免密未生效，已开普通 ssh 窗口")
        self.log(f"免密未生效（{reason}），回退普通 ssh + 剪贴板密码")
        messagebox.showwarning(
            "免密未生效",
            f"{entry['alias']}：{reason}\n\n已打开普通 ssh 窗口，密码已复制到剪贴板，粘贴即可。\n"
            "如果一直配不上，请在机器人上确认 /etc/ssh/sshd_config 里 PubkeyAuthentication 是 yes"
            "（改完 sudo systemctl restart ssh）。",
            parent=self,
        )

    def _key_worker(self, entry, then_open, notify):
        try:
            state, msg = ensure_key_login(entry, self.key)
        except Exception as exc:  # noqa: BLE001
            state, msg = "failed", str(exc)
        self.ui_queue.put(("key_result", entry, state, msg, then_open, notify))

    def _key_result(self, entry, state, msg, then_open=False, notify=True):
        if state in ("ok", "installed"):
            entry["key"] = True
            self._persist(f"{entry['alias']} 免密就绪：{msg}")
            if self.selected_entry() and self.selected_entry()["alias"] == entry["alias"]:
                self.show_detail()
            if then_open:
                self._launch_external(entry)
            elif notify and state == "installed":
                messagebox.showinfo(
                    "免密已配置",
                    f"{entry['alias']}：{msg}\n之后「外部终端（免密）」不会再要求输密码。",
                    parent=self,
                )
            return

        self.footer.configure(text=f"{entry['alias']} 免密配置失败")
        self.log(f"{entry['alias']} 免密失败：{msg}")
        if then_open:
            self._fallback_external(entry, msg)
        elif notify:
            messagebox.showwarning(
                "免密未配置",
                f"{entry['alias']}：{msg}\n\n可以在「编辑」里补上密码后重试，"
                "或确认目标机允许公钥登录（PubkeyAuthentication yes）。",
                parent=self,
            )

    def open_external(self, entry=None, _parent=None):
        e = entry or self.selected_entry()
        if not e:
            messagebox.showinfo("提示", "请先在左侧选中一台设备", parent=self)
            return
        if e.get("key"):
            self._launch_external(e)
            return
        password = self._ask_password(e)
        if password is None:
            return
        self.footer.configure(text=f"正在为 {e['alias']} 配置免密登录…")
        self.log(f"配置免密 {e['alias']}（首次写入 ~/.ssh/authorized_keys）")
        threading.Thread(target=self._key_worker, args=(e, True, False), daemon=True).start()

    def setup_key(self):
        e = self.selected_entry()
        if not e:
            messagebox.showinfo("提示", "请先在左侧选中一台设备", parent=self)
            return
        if not e.get("password") and not e.get("key"):
            messagebox.showinfo(
                "需要密码",
                "配置免密要用一次密码登录、把本机公钥写进机器人的 ~/.ssh/authorized_keys。\n"
                "这台设备还没存密码，请先点「编辑」补上密码，或直接打开终端输入一次。",
                parent=self,
            )
            return
        self.footer.configure(text=f"正在配置 {e['alias']} 免密…")
        self.log(f"配置免密 {e['alias']}")
        threading.Thread(target=self._key_worker, args=(e, False, True), daemon=True).start()

    def open_files(self, entry=None, initial_path=None, follow=None):
        e = entry or self.selected_entry()
        if not e:
            messagebox.showinfo("提示", "请先在左侧选中一台设备", parent=self)
            return
        password = e.get("password", "")
        if not password and not e.get("key"):
            password = self._ask_password(e)
            if password is None:
                return
        FileBrowserWindow(self, e, password, self.key,
                          initial_path=initial_path, follow_getter=follow)
        where = f" · 起始目录 {initial_path}" if initial_path else ""
        self.log(f"打开文件管理 {e['alias']}（SFTP）{where}")

    def open_config(self):
        p = Path(self.config_path)
        if p.exists():
            reveal_path(p)
        else:
            reveal_path(p.parent)


def main():
    parser = argparse.ArgumentParser(description=f"{APP_NAME} {APP_SUB}")
    parser.add_argument("--config", help="凭证库 hosts.json 路径")
    parser.add_argument("--selftest", action="store_true", help="仅构建界面并退出，用于打包自检")
    args = parser.parse_args()

    config = Path(args.config).expanduser() if args.config else config_path_default()
    app = RZDCLApp(config)
    if args.selftest:
        app.withdraw()
        app.update_idletasks()
        app.update()
        app.after(50, app.destroy)
        app.mainloop()
        print("SELFTEST OK ->", config)
        return
    app.mainloop()


if __name__ == "__main__":
    main()
