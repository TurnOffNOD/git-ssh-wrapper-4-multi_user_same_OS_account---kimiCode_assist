# AGENTS.md

## Project status

This project implements **git-ssh-wrapper**: a Python SSH wrapper for git that lets
multiple people share one OS account on one machine while each uses their own git
identity and SSH key for remote operations (push / pull / fetch / clone over SSH).

## Layout

- `git_ssh_wrapper.py` — the wrapper script. Installed globally for the OS account
  via git config (not OS environment variables):
  - Linux: `git config --global core.sshCommand "python3 /abs/path/git_ssh_wrapper.py"`
  - Windows: `git config --global core.sshCommand "python C:/path/to/git_ssh_wrapper.py"`
- `git-ssh-wrapper.example.yaml` — annotated sample mapping config. Real config
  lives at `~/.ssh/git-ssh-wrapper.yaml` (i.e. `C:/Users/<user>/.ssh/` on Windows).

## How it works

1. git invokes `core.sshCommand` only for SSH remotes (`git@host:owner/repo.git`
   or `ssh://git@host/owner/repo.git`); all other protocols never reach the script.
   Unrecognizable arguments are passed through to real `ssh` unchanged (no-op).
2. The script parses the ssh-style argv git passes in
   (`[-p port] [-o ...] git@<host> "git-upload-pack '<owner>/<repo>.git'"`) to
   extract `host` (handles `-p port`, `-o ...`, bundled flags, `user@host`, IPv6).
3. The identity is the `user.email` effective in the current repository, read via
   `git config --get user.email` (repo-local overrides global; during clone there
   is no local config so the global value applies).
4. `(host, user.email)` uniquely selects an `ssh_key` from the YAML config;
   duplicate matches are a config error. Host match is case-insensitive, email
   match is exact. `default_key` (optional) is the fallback when there is no
   match or no email is configured; without it, no-match falls back to
   transparent pass-through and missing-email is a hard error.
5. Key spec rules: bare filename → look under `~/.ssh/`, error if missing;
   absolute path or path containing separators → resolve as given (relative to
   the config file's directory), error if missing.
6. The real ssh binary is located via PATH (guarding against resolving to the
   script itself) and executed as a subprocess with
   `-i <key> -o IdentitiesOnly=yes` prepended; the exit code is propagated.

## Dependencies and testing

- Python 3.8+ (Windows & Linux), plus `pip install pyyaml`.
- No test suite yet. Smoke-test parsing with:
  `python -c "from git_ssh_wrapper import parse_ssh_host; print(parse_ssh_host(['git@github.com', \"git-upload-pack '/alice/repo.git'\"]))"`
- End-to-end check: configure a mapping and a repo-local `user.email`, then run
  `git ls-remote git@<host>:<owner>/<repo>.git` with `GIT_TRACE=1` and confirm the
  wrapper injects the expected `-i` key.

## Conventions

- Source file is UTF-8 with Chinese user-facing messages (the wrapper's users are
  Chinese-speaking); stdout/stderr are reconfigured to UTF-8 for Windows consoles.
- Match, don't break: unparseable argv or a missing mapping must fall back to
  transparent pass-through (or `default_key`) rather than aborting with an
  unclear error; a missing `user.email` with no `default_key` is the one hard
  error, since identity cannot be determined at all.
