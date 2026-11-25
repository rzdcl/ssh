#!/usr/bin/env python3
"""
RZDCL-cli —— 给 AI / 脚本用的命令行版凭证台。

和 GUI 共用同一份 hosts.json 与同一把免密密钥，所以：
  AI 在 shell 里读凭证 → 直接跑命令 / 列目录 / 传文件，不用再问用户要密码。

常用：
  RZDCL-cli.exe list                      # 列出全部设备（不显密码）
  RZDCL-cli.exe list --json               # 机器可读，含密码
  RZDCL-cli.exe get yunfan-0-165          # 单台详情
  RZDCL-cli.exe run <别名> --cmd "uname -a"
  RZDCL-cli.exe ls <别名> /home/yunfan
  RZDCL-cli.exe cat <别名> /etc/hostname
  RZDCL-cli.exe download <别名> /remote/path 本地路径
  RZDCL-cli.exe upload <别名> 本地路径 /remote/path
  RZDCL-cli.exe setup-key <别名>          # 写入公钥，之后免密
"""

import argparse
import json
import posixpath
import sys
from pathlib import Path

from rzdcl_core import (
    APP_NAME, config_path_default, load_entries, normalize_entry,
    SshKey, exec_command, ensure_key_login, sftp_connect, list_dir, human_size,
)


def _fix_stdio():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


def _load(config):
    path = Path(config).expanduser() if config else config_path_default()
    entries = [normalize_entry(e) for e in load_entries(path)]
    return path, entries


def _find(entries, needle):
    """按别名（优先）或 IP / 备注匹配，命中多台时报错列出候选。"""
    n = (needle or "").strip()
    if not n:
        raise SystemExit("请给出设备别名，例如 yunfan-0-165")
    exact = [e for e in entries if e["alias"] == n]
    if len(exact) == 1:
        return exact[0]
    loose = [e for e in entries
             if n.lower() in e["alias"].lower()
             or n == e["host"]
             or n in (e.get("note") or "")]
    if len(loose) == 1:
        return loose[0]
    if not loose:
        raise SystemExit(f"找不到设备「{n}」。先用 list 看一下别名。")
    lines = "\n".join(f"  {e['alias']}  {e['user']}@{e['host']}:{e['port']}" for e in loose)
    raise SystemExit(f"「{n}」匹配到多台，请用完整别名：\n{lines}")


def _key_for(entry):
    """该设备已配免密就带上密钥，否则返回 None（走密码）。"""
    key = SshKey()
    if entry.get("key") and key.exists():
        key.harden()          # 顺手修权限，ssh / scp 才认这把私钥
        return key
    return None


def _expand_remote(sftp, path):
    """支持 ~ 和 ~/xxx 写法（SFTP 不会自己展开）。"""
    if path == "~" or path.startswith("~/"):
        home = sftp.normalize(".")
        rest = path[2:] if path.startswith("~/") else ""
        return posixpath.normpath(posixpath.join(home, rest)) if rest else home
    return path


# ---------------------------------------------------------------- 子命令
def cmd_list(args):
    path, entries = _load(args.config)
    if args.json:
        payload = {
            "config": str(path),
            "keys_dir": str(SshKey().dir),
            "hosts": entries if args.show_password else
            [{k: v for k, v in e.items() if k != "password"} for e in entries],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    print(f"凭证库：{path}   共 {len(entries)} 台")
    print(f"密钥目录：{SshKey().dir}")
    print()
    width = max((len(e["alias"]) for e in entries), default=8)
    for e in entries:
        pwd = ("密码✓" if e.get("password") else "密码✗") if args.show_password else ""
        pwd_txt = ""
        if args.show_password:
            pwd_txt = f"{e['password'] or '(空)'}"
        flags = "免密" if e.get("key") else "    "
        print(f"{e['alias']:<{width}}  {e['user']}@{e['host']}:{e['port']}  "
              f"{flags}  {e.get('device') or ''}  {e.get('note') or ''}"
              f"{('  ' + pwd_txt) if args.show_password else ''}")
        if not args.show_password:
            continue
    return 0


def cmd_get(args):
    _, entries = _load(args.config)
    e = _find(entries, args.target)
    if args.json:
        print(json.dumps(e, ensure_ascii=False, indent=2))
        return 0
    key = SshKey()
    print(f"别名：{e['alias']}")
    print(f"地址：{e['host']}:{e['port']}")
    print(f"账号：{e['user']}")
    print(f"密码：{e['password'] or '(未保存)'}")
    print(f"免密：{'已配置' if e.get('key') else '未配置'}")
    print(f"设备：{e.get('device') or '-'}")
    print(f"标签：{', '.join(e.get('tags') or []) or '-'}")
    print(f"备注：{e.get('note') or '-'}")
    print(f"私钥：{key.private}")
    return 0


def cmd_run(args):
    _, entries = _load(args.config)
    e = _find(entries, args.target)
    code, out, err = exec_command(e, args.cmd, _key_for(e), timeout=args.timeout)
    if out:
        print(out, end="" if out.endswith("\n") else "\n")
    if err:
        print(err, end="" if err.endswith("\n") else "\n", file=sys.stderr)
    return code


def cmd_ls(args):
    _, entries = _load(args.config)
    e = _find(entries, args.target)
    client, sftp = sftp_connect(e, _key_for(e))
    try:
        sftp.chdir(_expand_remote(sftp, args.path))
        remote = sftp.normalize(".")
        rows = list_dir(sftp, ".")
    finally:
        try:
            sftp.close()
            client.close()
        except Exception:  # noqa: BLE001
            pass
    print(f"{remote}")
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    for r in rows:
        kind = "d" if r["is_dir"] else ("l" if r["is_link"] else "-")
        size = "" if r["is_dir"] else human_size(r["size"])
        print(f"{kind} {r['mode']:<6} {size:>9}  {r['mtime']}  {r['name']}")
    return 0


def cmd_cat(args):
    _, entries = _load(args.config)
    e = _find(entries, args.target)
    client, sftp = sftp_connect(e, _key_for(e))
    try:
        with sftp.open(_expand_remote(sftp, args.remote), "r") as fh:
            data = fh.read()
    finally:
        try:
            sftp.close()
            client.close()
        except Exception:  # noqa: BLE001
            pass
    sys.stdout.write(data.decode("utf-8", "replace"))
    return 0


def cmd_download(args):
    _, entries = _load(args.config)
    e = _find(entries, args.target)
    local = Path(args.local).expanduser()
    local.parent.mkdir(parents=True, exist_ok=True)
    client, sftp = sftp_connect(e, _key_for(e))
    try:
        sftp.get(_expand_remote(sftp, args.remote), str(local))
    finally:
        try:
            sftp.close()
            client.close()
        except Exception:  # noqa: BLE001
            pass
    print(f"已下载 {args.remote} → {local}  ({human_size(local.stat().st_size)})")
    return 0


def cmd_upload(args):
    _, entries = _load(args.config)
    e = _find(entries, args.target)
    local = Path(args.local).expanduser()
    if not local.exists():
        raise SystemExit(f"本地文件不存在：{local}")
    client, sftp = sftp_connect(e, _key_for(e))
    try:
        sftp.put(str(local), _expand_remote(sftp, args.remote))
    finally:
        try:
            sftp.close()
            client.close()
        except Exception:  # noqa: BLE001
            pass
    print(f"已上传 {local} ({human_size(local.stat().st_size)}) → {args.remote}")
    return 0


def cmd_setup_key(args):
    _, entries = _load(args.config)
    e = _find(entries, args.target)
    state, msg = ensure_key_login(e, SshKey())
    print(f"{state}: {msg}")
    return 0 if state in ("ok", "installed") else 2


def cmd_keys(args):
    key = SshKey()
    key.ensure()
    print(f"目录：{key.dir}")
    print(f"私钥：{key.private}")
    print(f"公钥：{key.public}")
    print()
    print(key.pub_text())
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog="RZDCL-cli", description=f"{APP_NAME} 命令行凭证台（给 AI / 脚本用）")
    p.add_argument("--config", help="凭证库 hosts.json 路径（默认自动查找）")
    sub = p.add_subparsers(dest="action")

    sp = sub.add_parser("list", help="列出全部设备")
    sp.add_argument("--json", action="store_true", help="JSON 输出")
    sp.add_argument("--show-password", action="store_true", help="带上密码")
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("get", help="查看单台设备凭证")
    sp.add_argument("target")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_get)

    sp = sub.add_parser("run", help="在设备上执行一条命令")
    sp.add_argument("target")
    sp.add_argument("--cmd", required=True, help="要执行的远端命令")
    sp.add_argument("--timeout", type=int, default=60)
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("ls", help="列远端目录")
    sp.add_argument("target")
    sp.add_argument("path", nargs="?", default=".")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_ls)

    sp = sub.add_parser("cat", help="打印远端文本文件")
    sp.add_argument("target")
    sp.add_argument("remote")
    sp.set_defaults(func=cmd_cat)

    sp = sub.add_parser("download", help="从设备下载文件")
    sp.add_argument("target")
    sp.add_argument("remote")
    sp.add_argument("local")
    sp.set_defaults(func=cmd_download)

    sp = sub.add_parser("upload", help="上传文件到设备")
    sp.add_argument("target")
    sp.add_argument("local")
    sp.add_argument("remote")
    sp.set_defaults(func=cmd_upload)

    sp = sub.add_parser("setup-key", help="给设备写入本机公钥（免密）")
    sp.add_argument("target")
    sp.set_defaults(func=cmd_setup_key)

    sp = sub.add_parser("keys", help="显示本机密钥路径与公钥")
    sp.set_defaults(func=cmd_keys)
    return p


def main(argv=None):
    _fix_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    try:
        return args.func(args)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"错误：{exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
