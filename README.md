# ccw — Claude Code Wrapper

English | [日本語](README.ja.md)

`ccw` runs [Claude Code](https://github.com/anthropics/claude-code) headlessly (`claude -p`) and keeps a long task going across the 5-hour session limit. When the limit message appears, `ccw` works out the reset time, waits for it, and resumes the same session with `claude --continue -p "<resume prompt>"`.

- One Python file, standard library only. No `pip install`, no build step.
- Works on Windows 11, WSL2 and macOS.
- Safe to run in several Git worktrees at once. A lock file allows only one `ccw` per directory.

## Requirements

- Python 3.8 or newer (a version that still gets security fixes is recommended)
- Claude Code CLI (`claude`) on `PATH`

## Install

Clone the repository and put it on your `PATH`.

```sh
git clone https://github.com/withnic/ccw.git
```

- **macOS / WSL / Linux:** run `ccw` directly (it has a `#!/usr/bin/env python3` shebang), or symlink it into a directory on your `PATH`.
- **Windows (PowerShell / Command Prompt):** add the clone directory to `PATH`. `ccw.cmd` runs `ccw` with `python`.

## Usage

```text
ccw [ccw-options] "<initial prompt>"              [-- claude-options...]
ccw [ccw-options] (--stdin | --prompt-file PATH)  [-- claude-options...]
```

```sh
# Simple run
ccw "Make all tests pass"

# Pass options to claude. They are used on the first run AND on every resume,
# so a resumed session keeps its model and permissions.
ccw "Make all tests pass" -- --model opus --permission-mode acceptEdits --allowedTools "Bash(git:*)" Edit

# Keep the prompt out of the process list and shell history
ccw --prompt-file task.md
cat task.md | ccw --stdin

# Check arguments and the lock without starting claude
ccw --dry-run "Make all tests pass"
```

| Option | Default | Meaning |
|---|---|---|
| `--resume-prompt TEXT` | `作業を再開してください` | Prompt sent when resuming |
| `--max-retries INT` | `5` | Maximum number of limit wait cycles |
| `--fallback-wait INT` | `18000` | Seconds to wait if the reset time cannot be parsed |
| `--buffer-sec INT` | `15` | Extra seconds added after the reset time |
| `--stdin` | | Read the initial prompt from stdin |
| `--prompt-file PATH` | | Read the initial prompt from a UTF-8 file |
| `--dry-run` | | Parse arguments and test the lock only |

Give the prompt to `ccw`, not after `--`. `-p`/`--print`, `-c`/`--continue` and `-r`/`--resume` are managed by `ccw` and are rejected after `--`.

### Exit codes

| Code | Meaning |
|---|---|
| `0` | Finished normally |
| `1` | Invalid arguments, another `ccw` is running in this directory, or `claude` was not found |
| `2` | Still hitting the limit after `--max-retries` waits |
| `130` | Interrupted (Ctrl+C, Ctrl+Break, SIGTERM, or the terminal was closed) |
| other | `claude`'s own exit code |

## How it works

1. Starts `claude <claude-options> -p "<prompt>"` and streams its output to your terminal.
2. If the last lines contain a session limit message (`5-hour limit`, `usage limit reached`, `hit your limit`, ...), it reads the reset time (`resets 3pm`, `at 15:00`, `in 2 hours 15 minutes`, ...). If no time is found, it waits `--fallback-wait` seconds.
3. Waits until then plus `--buffer-sec`, printing the remaining time. The wait follows the wall clock, so it stays correct if the PC is suspended.
4. Resumes with `claude <claude-options> --continue -p "<resume prompt>"` and repeats from step 2.

`ccw`'s own messages go to stderr with a `[ccw]` prefix.

### Output encoding on Windows

When output goes to a pipe or a file (including Git Bash, whose terminal is a pipe), `ccw` writes UTF-8 so that Japanese text and emoji are not garbled or lost. The Windows console itself is unaffected. To choose another encoding, set `PYTHONIOENCODING`.

PowerShell decodes piped output with `[Console]::OutputEncoding` (often the legacy code page). Before piping, e.g. `ccw ... | Tee-Object log.txt`, run:

```powershell
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
```

## Security notes

- `claude` is always started without a shell (`shell=False`). On Windows, `ccw` never runs npm's `claude.cmd` shim, because `cmd.exe` would re-parse the prompt. It starts the real `claude.exe` instead. If that cannot be found, set `CCW_CLAUDE_CMD` to a JSON array, e.g. `["C:\\path\\to\\claude.exe"]`.
- Common secrets in claude's output (`sk-ant-...`, AWS keys, `Bearer ...`, GitHub tokens, private keys, ...) are masked before they reach your terminal.
- With `--stdin` / `--prompt-file`, the prompt is piped to claude's stdin and never appears in any process's argument list.
- The lock file `.ccw.lock` is created exclusively, with `0600` permissions on POSIX. `ccw` refuses symlinks and only removes its own lock. The lock is removed on Ctrl+C and when the terminal window is closed; a lock left behind by a crash is cleaned up on the next start. Add `.ccw.lock` to your global gitignore (`~/.config/git/ignore`).

## Development

```sh
python3 -m unittest -v test_ccw.py        # tests (use `python` on Windows)
python3 -m py_compile ccw test_ccw.py     # syntax check
uvx ruff check ccw test_ccw.py            # lint (rules in ruff.toml)
```

CI runs ruff and the tests on Linux and Windows (plus macOS on `main`) with Python 3.8 and the latest release.

## License

[MIT](LICENSE)
