#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
git-ssh-wrapper —— 同一台机器、同一 OS 帐号下，按 (host_name, user.email)
选择不同 SSH key 的 git SSH 包装器。同时适配 Windows 和 Linux。

工作原理
--------
通过 git 的配置（不是 OS 环境变量）对本 OS 帐号全局生效：

    Linux:
        git config --global core.sshCommand "python3 /abs/path/git_ssh_wrapper.py"
    Windows:
        git config --global core.sshCommand "python C:/path/to/git_ssh_wrapper.py"

git 只对 SSH 类 remote（git@host:owner/repo.git 或 ssh://git@host/owner/repo.git）
的 push / pull / fetch 等远端操作调用 core.sshCommand；https、本地路径等其他
协议根本不会调用本脚本 —— 即“其余情况空操作”由 git 自身保证。脚本内部另有
兜底：参数无法解析时原样透传给真正的 ssh，不做任何干预。

git 调用本脚本时的参数形如：
    [-p <port>] [-o ...] git@<host_name> "git-upload-pack '/<owner>/<repo>.git'"
脚本从中解析 <host_name>，并读取当前代码仓库内生效的 user.email
（git config user.email，仓库本地配置优先于全局配置；clone 时无仓库本地配置，
自动退化为全局配置）。二元组 (<host_name>, user.email) 在 YAML 配置中唯一确定
一个 <sshKey>，然后执行：
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


def die(message: str, code: int = 1) -> "SystemExit":
    print(f"git-ssh-wrapper: 错误: {message}", file=sys.stderr)
    raise SystemExit(code)


def warn(message: str) -> None:
    print(f"git-ssh-wrapper: 警告: {message}", file=sys.stderr)


def parse_ssh_host(argv: list[str]) -> str | None:
    """从 git 传给 ssh 命令的参数中解析 host_name。

    兼容 git@host:owner/repo.git 与 ssh://git@host/owner/repo.git 两种 URL
    形态（git 均已转换为对 ssh 的调用，后者带端口时会以 -p 传入）。
    参数结构不符合预期时返回 None（调用方应原样透传，即“空操作”）。
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
    # 形如 git@github.com；兼容 IPv6 的 [::1] 写法
    host = target.rsplit("@", 1)[-1].strip("[]")
    return host or None


def get_effective_user_email() -> str | None:
    """读取当前代码仓库内生效的 user.email（本地配置优先于全局配置）。

    未配置或无法调用 git 时返回 None。
    """
    git_bin = None
    for name in (["git.exe", "git"] if os.name == "nt" else ["git"]):
        git_bin = shutil.which(name)
        if git_bin:
            break
    if not git_bin:
        warn("未在 PATH 中找到 git，无法读取 user.email")
        return None
    try:
        result = subprocess.run(
            [git_bin, "config", "--get", "user.email"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        warn(f"调用 git config 读取 user.email 失败: {exc}")
        return None
    if result.returncode != 0:
        return None
    email = result.stdout.strip()
    return email or None


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


def select_entry(entries: list, host: str, email: str) -> dict | None:
    """按 (host, user.email) 查找唯一条目；多条匹配视为配置错误。

    host 不区分大小写（DNS 约定），email 按配置原样精确匹配。
    """
    matches = [
        e
        for e in entries
        if isinstance(e, dict)
        and str(e.get("host", "")).lower() == host.lower()
        and str(e.get("email", "")).strip() == email
    ]
    if len(matches) > 1:
        die(
            f"配置中存在多条匹配 (host={host}, email={email}) 的条目，"
            "无法唯一确定 sshKey，请修正配置文件"
        )
    return matches[0] if matches else None


def resolve_key_path(key_spec: str, config_dir: Path) -> Path:
    """解析 key 路径：

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

    # 参数无法识别为 git 的 ssh 调用时，原样透传（空操作）
    host = parse_ssh_host(orig_argv)
    if host is None:
        warn("无法解析调用参数，原样透传给 ssh，不做任何处理")
        return run_ssh(ssh_bin, [], orig_argv)

    config = load_config(CONFIG_PATH)
    default_key = config.get("default_key")

    email = get_effective_user_email()
    if email is None:
        if default_key:
            warn("未配置 user.email，回退使用 default_key")
            key_spec = str(default_key)
        else:
            die(
                "当前仓库及全局均未配置 user.email，无法确定使用哪个 sshKey。"
                "请先执行: git config user.email \"you@example.com\""
            )
    else:
        entries = config.get("keys") or []
        if not isinstance(entries, list):
            die("配置文件中的 keys 字段必须是列表")

        entry = select_entry(entries, host, email)

        if entry is None:
            # 配置中无该 (host, email) 的映射：回退到 default_key（若配置），
            # 否则原样透传，交给 ssh 自身的 ~/.ssh/config 与 agent 处理
            if not default_key:
                warn(
                    f"配置中未找到 (host={host}, email={email}) 的映射，"
                    "按 ssh 默认行为透传"
                )
                return run_ssh(ssh_bin, [], orig_argv)
            key_spec = str(default_key)
        else:
            key_spec = str(entry.get("ssh_key", ""))
            if not key_spec:
                die(f"配置条目 (host={host}, email={email}) 缺少 ssh_key 字段")

    key_path = resolve_key_path(key_spec, CONFIG_PATH.parent)
    key_opts = ["-i", str(key_path), "-o", "IdentitiesOnly=yes"]
    return run_ssh(ssh_bin, key_opts, orig_argv)


if __name__ == "__main__":
    sys.exit(main())
