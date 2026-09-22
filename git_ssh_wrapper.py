#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
git-ssh-wrapper —— 同一台机器、同一 OS 帐号下，按 (host_name, user_or_group)
选择不同 SSH key 的 git SSH 包装器。同时适配 Windows 和 Linux。

工作原理
--------
通过 git 的配置（不是 OS 环境变量）对本 OS 帐号全局生效：

    Linux:
        git config --global core.sshCommand "python3 /abs/path/git_ssh_wrapper.py"
    Windows:
        git config --global core.sshCommand "python C:/path/to/git_ssh_wrapper.py"

git 只对 SSH 类 remote（git@host:user/repo.git 或 ssh://git@host/user/repo.git）
的 push / pull / fetch 等远端操作调用 core.sshCommand；https、本地路径等其他
协议根本不会调用本脚本 —— 即需求 1 所说的“其余情况空操作”由 git 自身保证。
脚本内部另有兜底：参数无法解析时原样透传给真正的 ssh，不做任何干预。

git 调用本脚本时的参数形如：
    [-p <port>] [-o ...] git@<host_name> "git-upload-pack '/<user_or_group>/<repo_name>.git'"
脚本从中解析 <host_name> 与 <user_or_group>，在 YAML 配置中查找唯一对应的
<sshKey>，然后执行：
    ssh -i <sshKey> -o IdentitiesOnly=yes <原始参数...>

依赖
----
    pip install pyyaml

配置文件
--------
默认位置：~/.ssh/git-ssh-wrapper.yaml （Windows 上即 C:/Users/<你>/.ssh/）
格式见 git-ssh-wrapper.example.yaml。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # 延迟到真正需要读配置时再报错
    yaml = None

CONFIG_PATH = Path.home() / ".ssh" / "git-ssh-wrapper.yaml"

# Windows 控制台默认 GBK，避免中文提示输出乱码
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

# ssh 选项中“带一个独立参数值”的选项字母（用于跳过参数找到 host）
_SSH_OPTS_WITH_VALUE = set("bcDEeFIiJLlmOopQRSWw")

# 匹配 git 通过 ssh 发送的远端命令，例如：
#   git-upload-pack '/user_or_group/repo.git'
#   git-receive-pack 'user_or_group/repo.git'
_REMOTE_CMD_RE = re.compile(
    r"git-(?:upload|receive)-pack\s+"
    r"(?:'(?P<sq>[^']*)'|\"(?P<dq>[^\"]*)\"|(?P<plain>\S+))"
)


def die(message: str, code: int = 1) -> "SystemExit":
    print(f"git-ssh-wrapper: 错误: {message}", file=sys.stderr)
    raise SystemExit(code)


def warn(message: str) -> None:
    print(f"git-ssh-wrapper: 警告: {message}", file=sys.stderr)


def parse_ssh_argv(argv: list[str]) -> tuple[str, str | None] | None:
    """解析 git 传给 ssh 命令的参数。

    返回 (host_name, user_or_group)；参数结构不符合预期时返回 None
    （调用方应原样透传给真正的 ssh，即“空操作”）。
    """
    i = 0
    n = len(argv)
    # 跳过 ssh 自身的选项，找到第一个非选项参数：[user@]host
    while i < n:
        arg = argv[i]
        if arg == "--":
            i += 1
            break
        if arg.startswith("-") and len(arg) >= 2:
            opt = arg[1]
            if opt in _SSH_OPTS_WITH_VALUE:
                # -p 22（分离形式）或 -p22（连写形式）
                i += 2 if len(arg) == 2 else 1
            else:
                i += 1  # 无值开关选项（可能捆绑，如 -vT）
            continue
        break

    if i >= n:
        return None

    target = argv[i]
    command_args = argv[i + 1 :]

    # 形如 git@github.com；兼容 IPv6 的 [::1] 写法
    host = target.rsplit("@", 1)[-1].strip("[]")
    if not host:
        return None

    # 从远端命令中提取仓库路径，取第一段作为 user_or_group。
    # 对应两种 URL 形态：
    #   git@host:user_or_group/repo.git        -> 'user_or_group/repo.git'
    #   ssh://git@host/user_or_group/repo.git  -> '/user_or_group/repo.git'
    user_or_group = None
    match = _REMOTE_CMD_RE.search(" ".join(command_args))
    if match:
        repo_path = (match.group("sq") or match.group("dq") or match.group("plain") or "")
        repo_path = repo_path.lstrip("/")
        if repo_path:
            user_or_group = repo_path.split("/", 1)[0]

    return host, user_or_group


def load_config(path: Path) -> dict:
    if yaml is None:
        die("缺少依赖 PyYAML，请先执行: pip install pyyaml")
    if not path.is_file():
        die(f"配置文件不存在: {path}")
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        die(f"配置文件 YAML 解析失败: {path}: {exc}")
    if not isinstance(data, dict):
        die(f"配置文件内容必须是 YAML 映射（字典）: {path}")
    return data


def select_entry(entries: list, host: str, user_or_group: str) -> dict | None:
    """按 (host, user_or_group) 查找唯一条目；多条匹配视为配置错误。"""
    matches = [
        e
        for e in entries
        if isinstance(e, dict)
        and str(e.get("host", "")).lower() == host.lower()
        and str(e.get("user_or_group", "")) == user_or_group
    ]
    if len(matches) > 1:
        die(
            f"配置中存在多条匹配 (host={host}, user_or_group={user_or_group}) 的条目，"
            "无法唯一确定 sshKey，请修正配置文件"
        )
    return matches[0] if matches else None


def resolve_key_path(key_spec: str, config_dir: Path) -> Path:
    """按需求 4 解析 key 路径：

    - 仅文件名（不含路径分隔符）：在 ~/.ssh/ 下查找，不存在则报错；
    - 完整路径（绝对路径或含分隔符的相对路径）：按给出的路径查找
      （相对路径相对于配置文件所在目录解析），不存在则报错。
    """
    expanded = os.path.expandvars(os.path.expanduser(key_spec))
    has_sep = ("/" in key_spec) or ("\\" in key_spec) or (os.sep in key_spec)

    if os.path.isabs(expanded):
        candidate = Path(expanded)
    elif has_sep:
        candidate = (config_dir / expanded).resolve()
    else:
        candidate = Path.home() / ".ssh" / expanded

    if not candidate.is_file():
        die(f"SSH 私钥文件不存在: {candidate}（配置项: {key_spec}）")
    return candidate


def find_real_ssh() -> str:
    """在 PATH 中找到真正的 ssh 可执行文件（防止解析到本脚本自身造成递归）。"""
    self_path = Path(__file__).resolve()
    for name in (["ssh.exe", "ssh"] if os.name == "nt" else ["ssh"]):
        found = shutil.which(name)
        if found and Path(found).resolve() != self_path:
            return found
    die("未在 PATH 中找到真正的 ssh 可执行文件")


def run_ssh(ssh_bin: str, extra_opts: list[str], orig_argv: list[str]) -> int:
    """以子进程方式执行真正的 ssh，并透传退出码（Windows / Linux 通用）。"""
    try:
        return subprocess.call([ssh_bin, *extra_opts, *orig_argv])
    except OSError as exc:
        die(f"执行 ssh 失败: {exc}")


def main() -> int:
    orig_argv = sys.argv[1:]
    ssh_bin = find_real_ssh()

    # 需求 1 的脚本侧兜底：参数无法识别为 git 的 ssh 调用时，原样透传（空操作）
    parsed = parse_ssh_argv(orig_argv)
    if parsed is None:
        warn("无法解析调用参数，原样透传给 ssh，不做任何处理")
        return run_ssh(ssh_bin, [], orig_argv)

    host, user_or_group = parsed
    if not user_or_group:
        warn(f"未能从远端命令中解析 user_or_group（host={host}），原样透传")
        return run_ssh(ssh_bin, [], orig_argv)

    config = load_config(CONFIG_PATH)
    entries = config.get("keys") or []
    if not isinstance(entries, list):
        die("配置文件中的 keys 字段必须是列表")

    entry = select_entry(entries, host, user_or_group)

    if entry is None:
        # 配置中无该 (host, user_or_group) 的映射：回退到 default_key（若配置），
        # 否则原样透传，交给 ssh 自身的 ~/.ssh/config 与 agent 处理
        default_key = config.get("default_key")
        if not default_key:
            warn(
                f"配置中未找到 (host={host}, user_or_group={user_or_group}) 的映射，"
                "按 ssh 默认行为透传"
            )
            return run_ssh(ssh_bin, [], orig_argv)
        key_spec = str(default_key)
    else:
        key_spec = str(entry.get("ssh_key", ""))
        if not key_spec:
            die(
                f"配置条目 (host={host}, user_or_group={user_or_group}) 缺少 ssh_key 字段"
            )

    key_path = resolve_key_path(key_spec, CONFIG_PATH.parent)
    key_opts = ["-i", str(key_path), "-o", "IdentitiesOnly=yes"]
    return run_ssh(ssh_bin, key_opts, orig_argv)


if __name__ == "__main__":
    sys.exit(main())
