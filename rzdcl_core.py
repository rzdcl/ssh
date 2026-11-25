#!/usr/bin/env python3
"""
RZDCL 核心逻辑：凭证库、SSH 免密密钥、会话、SFTP 文件传输。

GUI（rzdcl_console.py）和命令行版（rzdcl_cli.py）共用这一份实现，
保证「桌面上能连、AI 也能连」用的是同一套凭证和同一把密钥。
"""

import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

APP_NAME = "RZDCL"
APP_SUB = "机器人 SSH 凭证台"

ANSI_RE = re.compile(
    r"\x1b\][^\x07\x1b]*(\x07|\x1b\\)"      # OSC
    r"|\x1b\[[0-9;?]*[ -/]*[@-~]"           # CSI
    r"|\x1b[()#][A-Za-z0-9]"                # 字符集切换
    r"|\x1b[=>]"                            # 单字符指令
    r"|\x08"                                # 退格
)


def strip_ansi(text):
    return ANSI_RE.sub("", text).replace("\r", "")


# ---------------------------------------------------------------- 路径
def resource_path(rel):
    """兼容 PyInstaller onefile 的资源查找。"""
    bases = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        bases.append(Path(meipass))
    if getattr(sys, "frozen", False):
        bases.append(Path(sys.executable).resolve().parent)
    bases.append(Path(__file__).resolve().parent.parent)
    bases.append(Path(__file__).resolve().parent)
    for base in bases:
        p = base / rel
        if p.exists():
            return p
    return None


def app_dir():
    """exe 所在目录；源码模式下是项目根目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def config_path_default():
    """凭证库：优先便携模式（exe 同级 / 源码 vault），否则用户目录。"""
    env = os.environ.get("ROBOT_VAULT")
    if env:
        return Path(env).expanduser()

    beside = app_dir() / "hosts.json"
    if beside.exists():
        return beside
    if not getattr(sys, "frozen", False):
        dev = Path(__file__).resolve().parent.parent / "vault" / "hosts.json"
        if dev.exists():
            return dev

    roaming = Path(os.environ.get("APPDATA", Path.home())) / APP_NAME / "hosts.json"
    if roaming.exists():
        return roaming
    try:
        beside.parent.mkdir(parents=True, exist_ok=True)
        return beside
    except OSError:
        roaming.parent.mkdir(parents=True, exist_ok=True)
        return roaming


def load_entries(config_path):
    p = Path(config_path)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"凭证库无法读取：{p}\n{exc}") from exc
    hosts = data.get("hosts", [])
    return hosts if isinstance(hosts, list) else []


def save_entries(config_path, entries):
    p = Path(config_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps({"version": 1, "hosts": entries}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def normalize_entry(raw):
    e = dict(raw)
    e.setdefault("alias", "")
    e.setdefault("host", "")
    e.setdefault("port", 22)
    e.setdefault("user", "")
    e.setdefault("password", "")
    e.setdefault("device", "")
    e.setdefault("note", "")
    e.setdefault("key", False)
    tags = e.get("tags", [])
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    e["tags"] = tags or []
    try:
        e["port"] = int(e["port"] or 22)
    except (TypeError, ValueError):
        e["port"] = 22
    return e


# ---------------------------------------------------------------- 免密密钥
def keys_dir():
    """密钥目录：优先 exe 同级 keys，不可写时落到 %APPDATA%\\RZDCL\\keys。"""
    for base in (app_dir() / "keys",
                 Path(os.environ.get("APPDATA", Path.home())) / APP_NAME / "keys"):
        try:
            base.mkdir(parents=True, exist_ok=True)
            probe = base / ".writable"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return base
        except OSError:
            continue
    home = Path.home() / ".rzdcl" / "keys"
    home.mkdir(parents=True, exist_ok=True)
    return home


def _ssh_keygen():
    exe = shutil.which("ssh-keygen")
    if exe:
        return exe
    for cand in (r"C:\Windows\System32\OpenSSH\ssh-keygen.exe", "/usr/bin/ssh-keygen"):
        if Path(cand).exists():
            return cand
    return None


class SshKey:
    """本机免密密钥（ed25519），GUI / CLI 共用一个。"""

    def __init__(self, directory=None):
        self.dir = Path(directory) if directory else keys_dir()
        self.private = self.dir / "id_ed25519"
        self.public = Path(str(self.private) + ".pub")
        self._pkey = None

    def exists(self):
        return self.private.exists() and self.public.exists()

    def harden(self):
        """收紧私钥权限。

        Windows 的 ssh.exe 会拒绝权限过宽的私钥（“Bad permissions / UNPROTECTED
        PRIVATE KEY FILE”），而我们这个 exe 放在用户目录里，目录继承下来的 ACL
        常常带着别的组（甚至沙箱用户）。这里去掉继承、只留当前用户。
        """
        path = self.private
        if not path.exists():
            return False
        try:
            if os.name != "nt":
                os.chmod(path, 0o600)
                return True
            user = os.environ.get("USERNAME") or ""
            domain = os.environ.get("USERDOMAIN") or ""
            who = f"{domain}\\{user}" if domain else user
            icacls = shutil.which("icacls") or r"C:\Windows\System32\icacls.exe"
            subprocess.run(
                [icacls, str(path), "/inheritance:r", "/grant:r", f"{who}:(F)"],
                check=True, capture_output=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return True
        except Exception:  # noqa: BLE001
            return False

    def ensure(self):
        if self.exists():
            self.harden()
            return True
        exe = _ssh_keygen()
        if not exe:
            raise RuntimeError("系统缺少 ssh-keygen，无法生成免密密钥（Windows 可在“可选功能”里安装 OpenSSH 客户端）")
        self.dir.mkdir(parents=True, exist_ok=True)
        for p in (self.private, self.public):
            if p.exists():
                p.unlink()
        cmd = [exe, "-t", "ed25519", "-f", str(self.private), "-N", "", "-C", "RZDCL"]
        subprocess.run(cmd, check=True, capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.harden()
        return self.exists()

    def pub_text(self):
        if not self.exists():
            self.ensure()
        return self.public.read_text(encoding="utf-8").strip()

    def pkey(self):
        import paramiko
        if self._pkey is None:
            self._pkey = paramiko.Ed25519Key.from_private_key_file(str(self.private))
        return self._pkey

    def live_check(self):
        """本机密钥是否可用（能读出来就算）。"""
        try:
            self.pkey()
            return True
        except Exception:  # noqa: BLE001
            return False


def _connect_client(host, port, user, password=None, key=None, timeout=10):
    """建立 SSHClient：有密钥优先密钥，失败再用密码。"""
    import paramiko

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    common = dict(timeout=timeout, banner_timeout=timeout, auth_timeout=timeout,
                  look_for_keys=False, allow_agent=False)
    last_err = None
    if key is not None and getattr(key, "exists", lambda: False)():
        try:
            client.connect(host, port=int(port), username=user, pkey=key.pkey(), **common)
            return client
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    if password:
        try:
            client.connect(host, port=int(port), username=user, password=password, **common)
            return client
        except Exception as exc:  # noqa: BLE001
            last_err = exc
    if last_err is not None:
        raise last_err
    raise RuntimeError("没有可用的登录方式（既没有密钥也没有密码）")


def key_login_ok(host, port, user, key, timeout=5):
    client = None
    try:
        client = _connect_client(host, port, user, None, key, timeout=timeout)
        return True
    except Exception:  # noqa: BLE001
        return False
    finally:
        try:
            if client:
                client.close()
        except Exception:  # noqa: BLE001
            pass


def install_public_key(host, port, user, password, pub_text, timeout=8):
    """把公钥追加到远端 ~/.ssh/authorized_keys（已存在则跳过，改动前留备份）。"""
    import paramiko  # noqa: F401  (SFTP 依赖 paramiko 在此进程中被导入)

    client = _connect_client(host, port, user, password, None, timeout=timeout)
    try:
        sftp = client.open_sftp()
        home = sftp.normalize(".") or "."
        ssh_dir = posixpath.join(home, ".ssh")
        try:
            sftp.stat(ssh_dir)
        except IOError:
            sftp.mkdir(ssh_dir, 0o700)
        try:
            sftp.chmod(ssh_dir, 0o700)
        except Exception:  # noqa: BLE001
            pass

        auth = posixpath.join(ssh_dir, "authorized_keys")
        try:
            with sftp.open(auth, "r") as fh:
                existing = fh.read().decode("utf-8", "replace")
        except IOError:
            existing = ""

        body = pub_text.split()[1]
        for line in existing.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1] == body and not line.lstrip().startswith("#"):
                return "已存在"

        if existing:
            try:
                with sftp.open(auth + ".rzdcl.bak", "w") as fh:
                    fh.write(existing.encode("utf-8"))
            except Exception:  # noqa: BLE001
                pass

        payload = existing + ("" if (not existing or existing.endswith("\n")) else "\n") + pub_text + "\n"
        with sftp.open(auth, "w") as fh:
            fh.write(payload.encode("utf-8"))
        try:
            sftp.chmod(auth, 0o600)
        except Exception:  # noqa: BLE001
            pass
        return "已写入"
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass


def _ssh_exe():
    exe = shutil.which("ssh")
    if exe:
        return exe
    for cand in (r"C:\Windows\System32\OpenSSH\ssh.exe", "/usr/bin/ssh"):
        if Path(cand).exists():
            return cand
    return None


def ssh_cli_key_ok(entry, key, connect_timeout=6):
    """用系统 ssh.exe 实测一次密钥登录（paramiko 通过 ≠ ssh.exe 能用）。

    Windows 的 ssh.exe 会检查私钥 ACL，权限过宽就直接忽略密钥并回落到密码，
    所以免密到底成没成，必须由 ssh.exe 自己验一遍。返回 (ok, detail)。
    """
    exe = _ssh_exe()
    if not exe:
        return None, "系统没有 ssh 命令"
    args = [exe, "-i", str(key.private), "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"ConnectTimeout={connect_timeout}",
            "-p", str(entry["port"]), f"{entry['user']}@{entry['host']}", "exit"]
    try:
        res = subprocess.run(args, capture_output=True, timeout=connect_timeout + 10,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
    text = (res.stderr or b"").decode("utf-8", "replace").strip()
    return res.returncode == 0, text


def ensure_key_login(entry, key=None, allow_install=True):
    """确保某台设备可以密钥免密登录。

    返回 (state, message)：
      ok         免密已经可用
      installed  本次刚写入公钥，之后免密
      no-password 该设备没存密码，无法自动配置
      failed      失败（消息给出原因）
    成功后会在 entry 上打 key=True，调用方可选择持久化。
    """
    key = key or SshKey()
    try:
        key.ensure()
    except Exception as exc:  # noqa: BLE001
        return "failed", str(exc)

    if key_login_ok(entry["host"], entry["port"], entry["user"], key):
        key.harden()
        ok, detail = ssh_cli_key_ok(entry, key)
        if ok is False:
            return "failed", f"paramiko 能登录，但系统 ssh 用不了这把密钥：{detail or '原因不明'}"
        entry["key"] = True
        return "ok", "免密登录可用"

    if not allow_install:
        return "failed", "免密未配置"
    if not entry.get("password"):
        return "no-password", "这台设备没有保存密码，无法自动写入公钥"
    try:
        install_public_key(entry["host"], entry["port"], entry["user"],
                           entry["password"], key.pub_text())
    except Exception as exc:  # noqa: BLE001
        return "failed", f"写入公钥失败：{exc}"

    if key_login_ok(entry["host"], entry["port"], entry["user"], key):
        key.harden()
        ok, detail = ssh_cli_key_ok(entry, key)
        if ok is False:
            key.harden()
            ok, detail = ssh_cli_key_ok(entry, key)
        if ok is False:
            return "failed", f"公钥已写入，但系统 ssh 拒绝用这把密钥登录：{detail or '原因不明'}"
        entry["key"] = True
        return "installed", "已写入公钥，之后免密登录"
    return "failed", "公钥已写入，但密钥登录仍失败（服务器可能禁用了 PubkeyAuthentication）"


def external_ssh_args(entry, key=None):
    """拼出系统 ssh 命令（免密窗口用）。"""
    args = ["ssh", "-p", str(entry["port"]),
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ServerAliveInterval=30"]
    if key is not None and key.exists():
        args += ["-i", str(key.private), "-o", "IdentitiesOnly=yes"]
    args.append(f"{entry['user']}@{entry['host']}")
    return args


def _shell_quote(s):
    return "'" + str(s).replace("'", "'\\''") + "'"


def open_external_terminal(entry, key=None):
    """新开一个终端窗口跑 ssh（原生终端，vim/top 都能用）。

    Windows 用 CREATE_NEW_CONSOLE 直接起 ssh.exe；Linux/macOS 找一个终端模拟器
    再把 ssh 命令塞进去。
    """
    if key is not None and key.exists():
        key.harden()          # 私钥权限必须只对自己可读
    args = external_ssh_args(entry, key)
    if os.name == "nt":
        return subprocess.Popen(
            args,
            creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
        )
    cmd = " ".join(_shell_quote(a) for a in args)
    shell = os.environ.get("SHELL") or "/bin/bash"
    for name, argv in (
        ("gnome-terminal", lambda exe: [exe, "--", shell, "-c", cmd]),
        ("konsole", lambda exe: [exe, "-e", shell, "-c", cmd]),
        ("xfce4-terminal", lambda exe: [exe, "-e", f"{shell} -c {cmd}"]),
        ("mate-terminal", lambda exe: [exe, "-e", f"{shell} -c {cmd}"]),
        ("tilix", lambda exe: [exe, "-e", f"{shell} -c {cmd}"]),
        ("x-terminal-emulator", lambda exe: [exe, "-e", shell, "-c", cmd]),
        ("xterm", lambda exe: [exe, "-e", shell, "-c", cmd]),
    ):
        exe = shutil.which(name)
        if not exe:
            continue
        try:
            return subprocess.Popen(argv(exe))
        except Exception:  # noqa: BLE001
            continue
    raise RuntimeError("系统里没找到可用的终端模拟器（可安装 xterm 或 gnome-terminal）")


# ---------------------------------------------------------------- 交互 shell
class SshSession:
    """持久 shell 会话：发一行命令，读回完整输出（内嵌终端用）。"""

    def __init__(self, host, port, user, password=None, key=None):
        self.host, self.port = host, int(port)
        self.user, self.password, self.key = user, password, key
        self.client = None
        self.chan = None
        self.cwd = None          # 命令执行后同步到的远端工作目录

    def connect(self, timeout=12, banner_seconds=0.9):
        client = _connect_client(self.host, self.port, self.user,
                                 self.password, self.key, timeout=timeout)
        try:
            chan = client.invoke_shell(term="xterm-256color", width=160, height=48)
        except Exception:
            client.close()
            raise
        chan.settimeout(0.25)
        self.client, self.chan = client, chan

        banner, deadline = "", time.time() + banner_seconds
        while time.time() < deadline:
            try:
                data = chan.recv(65536)
            except Exception:  # noqa: BLE001  (socket.timeout 也走这里)
                continue
            if not data:
                break
            banner += data.decode("utf-8", "replace")
        return strip_ansi(banner).strip()

    def close(self):
        for obj in (self.chan, self.client):
            try:
                if obj is not None:
                    obj.close()
            except Exception:  # noqa: BLE001
                pass
        self.chan = self.client = None

    @property
    def alive(self):
        return bool(self.chan and self.client and self.chan.active)

    def run(self, command, cancel_event=None, on_line=None, max_seconds=900, echo_filter=True):
        import socket as _socket

        chan = self.chan
        if not self.alive:
            raise ConnectionError("SSH 会话已断开")

        marker = "RZDCL_END_%d" % int(time.time() * 1000 % 9999999)
        chan.send(command + "\n")
        time.sleep(0.02)
        # 顺带把远端当前目录 $PWD 带回来，文件管理窗口可以跟着终端走
        chan.send("printf '\\n%s:%%s|%%s\\n' \"$?\" \"$PWD\"\n" % marker)

        out_lines = []
        pending = ""
        echo_dropped = not echo_filter
        deadline = time.time() + max_seconds
        exit_code = None
        cancelled = False

        while True:
            if time.time() > deadline:
                raise TimeoutError("命令执行超时，可点「中断」结束当前任务")
            if cancel_event is not None and cancel_event.is_set():
                cancel_event.clear()
                cancelled = True
                try:
                    chan.send("\x03")
                except Exception:
                    raise ConnectionError("SSH 会话已断开")

            try:
                chunk = chan.recv(65536)
            except _socket.timeout:
                continue
            except Exception as exc:  # noqa: BLE001
                raise ConnectionError(f"SSH 会话异常：{exc}") from exc

            if not chunk:
                if not chan.active:
                    raise ConnectionError("SSH 连接已被远端关闭")
                time.sleep(0.02)
                continue

            pending += chunk.decode("utf-8", "replace")
            pending = pending.replace("\r\n", "\n")
            while "\n" in pending:
                line, pending = pending.split("\n", 1)
                line = strip_ansi(line)
                if marker in line:
                    m = re.search(re.escape(marker) + r":(\d+)(?:\|(.*))?", line)
                    if m:
                        exit_code = int(m.group(1))
                        pwd = (m.group(2) or "").strip()
                        if pwd:
                            self.cwd = pwd
                        pending = ""
                        break
                    continue
                text = line.rstrip("\n")
                stripped = text.strip()
                if not echo_dropped:
                    if stripped == command.strip() or (
                        stripped.endswith(command.strip())
                        and ("$" in stripped or "#" in stripped)
                        and len(stripped) <= len(command.strip()) + 100
                    ):
                        echo_dropped = True
                        continue
                out_lines.append(text)
                if on_line:
                    on_line(text)
            if exit_code is not None:
                break

        if pending:
            tail = strip_ansi(pending)
            if marker not in tail:
                out_lines.append(tail)
                if on_line:
                    on_line(tail)
        if cancelled:
            out_lines.append("")
            out_lines.append("^C  已发送中断信号")
        return exit_code if exit_code is not None else 130, "\n".join(out_lines)


# ---------------------------------------------------------------- 一次性命令 / 文件
def exec_command(entry, command, key=None, timeout=60):
    """执行一条远端命令，返回 (exit_code, stdout, stderr)。AI / CLI 用。"""
    client = _connect_client(entry["host"], entry["port"], entry["user"],
                             entry.get("password"), key, timeout=10)
    try:
        _, stdout, stderr = client.exec_command(command, timeout=timeout, get_pty=False)
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        code = stdout.channel.recv_exit_status()
        return code, out, err
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass


def sftp_connect(entry, key=None, timeout=10):
    """返回 (client, sftp)，调用方负责 close。"""
    client = _connect_client(entry["host"], entry["port"], entry["user"],
                             entry.get("password"), key, timeout=timeout)
    return client, client.open_sftp()


def list_dir(sftp, path="."):
    """列目录：返回 [{name, is_dir, size, mode, mtime}]，目录在前。"""
    attrs = sftp.listdir_attr(path)
    rows = []
    for a in attrs:
        mode = a.st_mode or 0
        rows.append({
            "name": a.filename,
            "is_dir": (mode & 0o170000) == 0o040000,
            "is_link": (mode & 0o170000) == 0o120000,
            "size": a.st_size or 0,
            "mode": oct(mode & 0o7777),
            "mtime": time.strftime("%Y-%m-%d %H:%M", time.localtime(a.st_mtime or 0)),
        })
    rows.sort(key=lambda r: (not r["is_dir"], r["name"].lower()))
    return rows


_SHELL_LIST_SCRIPT = r'''for p in /proc/[0-9]*; do
  [ -r "$p/comm" ] || continue
  comm=$(cat "$p/comm" 2>/dev/null) || continue
  case "$comm" in bash|zsh|sh|fish|dash|ksh) ;; *) continue ;; esac
  tty=$(ps -o tty= -p "${p#/proc/}" 2>/dev/null | tr -d ' ')
  [ -z "$tty" ] && continue
  [ "$tty" = "?" ] && continue
  rt=$(cut -d' ' -f1 "$p/schedstat" 2>/dev/null)
  [ -z "$rt" ] && rt=0
  cwd=$(readlink "$p/cwd" 2>/dev/null) || continue
  echo "${p#/proc/}|$cwd|$rt"
done
'''


def list_shell_cwd(entry, key=None, client=None, timeout=8):
    """列出远端所有带 tty 的交互式 shell：pid、cwd、累计 CPU 时间（ns）。

    这些 shell 既包括本程序内嵌终端的，也包括系统 ssh 窗口里那些。谁“最近动过”
    由调用方比较两次采样里 CPU 时间的增量判断，比看 tty idle 靠谱得多。
    """
    try:
        if client is not None:
            _, out, _ = client.exec_command(_SHELL_LIST_SCRIPT, timeout=timeout)
            text = out.read().decode("utf-8", "replace")
        else:
            _, text, _ = exec_command(entry, _SHELL_LIST_SCRIPT, key, timeout=timeout)
    except Exception:  # noqa: BLE001
        return []
    rows = []
    for line in text.splitlines():
        parts = line.strip().split("|")
        if len(parts) != 3:
            continue
        pid, cwd, rt = parts
        if not cwd.startswith("/"):
            continue
        try:
            rows.append({"pid": pid, "cwd": cwd, "runtime": int(rt)})
        except ValueError:
            continue
    return rows


def human_size(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.0f} B"
