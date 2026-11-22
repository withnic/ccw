# CLAUDE.md — ccw (Claude Code Wrapper)

## Project Overview
`ccw` is a zero-dependency, single-file Python CLI wrapper for Claude Code (`claude`). It runs `claude -p` headlessly, monitors the output for 5-hour session limit messages, calculates the reset timestamp, sleeps until the reset time, and automatically resumes with `claude <claude-options> --continue -p "<resume_prompt>"`. Options given after `--` are passed to claude on the initial run and on every resume.

Interactive mode (`-i` / `--interactive`, POSIX only) instead runs claude's normal TUI inside a pseudo-terminal that `ccw` proxies transparently (raw stdin, output, `SIGWINCH` window size). On a limit hit the same claude process is kept open; after the reset time `ccw` writes `resume_prompt` and then `"\r"` to the PTY master, once per limit hit. On native Windows (`os.name == "nt"`) `-i` prints an error pointing to WSL2 and exits `1`.

## Files
- `ccw`: the whole program (no extension, `#!/usr/bin/env python3`).
- `ccw.cmd`: 2-line shim so native Windows shells can run `ccw`.
- `test_ccw.py`: unit tests and mock end-to-end tests (`unittest` only). Loads `ccw` via `importlib.machinery.SourceFileLoader`; set `CCW_CLAUDE_CMD` (JSON array) or pass `claude_cmd=` to `main()` to use a mock claude.

## Development & Verification Commands
On Windows use `python` instead of `python3` (`python3` is often the Microsoft Store stub).
- Run tests: `python3 -m unittest -v test_ccw.py`
- Verify syntax: `python3 -m py_compile ccw test_ccw.py`
- Lint (config in `ruff.toml`; `ccw` has no extension so pass it explicitly): `uvx ruff check --fix ccw test_ccw.py`
- POSIX-only tests (lock permissions, symlinks, PTY / interactive mode) are skipped on Windows; run the suite under WSL too when touching lock handling or interactive mode.

## Strict Constraints
1. **Zero External Dependencies:** Python 3.8+ standard library only (3.8 is the tested floor; users are recommended a security-supported version), with `from __future__ import annotations`. Do not use 3.9+ APIs (`str.removeprefix`, `zoneinfo`, ...). Never add `requirements.txt` or `pyproject.toml` dependencies.
2. **Single-File Distribution:** All runtime logic must remain inside the single executable script `ccw`.
3. **Line Endings & Encoding:** Always preserve LF (`\n`) line endings and UTF-8 (no BOM) encoding across all files, especially `ccw`.
4. **Worktree Isolation:** Enforce single-instance execution per working directory via `.ccw.lock` PID checking.
5. **No Shell:** Never use `shell=True`. On Windows never execute a `.cmd`/`.bat` with user-controlled arguments (cmd.exe re-parses them); resolve the npm shim to `claude.exe` / `node cli.js` instead.
6. **Lock File Safety:** Create `.ccw.lock` with `O_CREAT | O_EXCL` (`0o600` on POSIX), refuse symlinks, and only remove a lock whose `pid` is our own.
7. **Secrets:** Redact secrets (`sk-ant-...`, `AWS_SECRET_ACCESS_KEY=...`, `Bearer ...`, PEM keys, ...) before echoing child output. Prompts from `--stdin` / `--prompt-file` are piped to claude's stdin, never put in argv. In interactive mode redaction is width-preserving (`*` of the same length) and only touches text between escape sequences, so TUI cursor control is never altered.
8. **Exit Codes:** `0` success, `1` usage/lock/claude-not-found/`-i` on native Windows, `2` max retries exceeded, `130` interrupted, otherwise claude's own exit code.
9. **Terminal Safety (interactive):** Put the parent terminal into raw mode only inside a `try` whose `finally` restores the saved `termios` attributes. Import `pty` / `termios` / `tty` / `fcntl` lazily so `ccw` still loads on Windows. Limit detection runs on ANSI-stripped text; the screen always gets the (redacted) raw stream.

## Committing
`core.filemode` is `false` on Windows; add the executable bit explicitly: `git add --chmod=+x ccw`.
