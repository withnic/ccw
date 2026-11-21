# ccw — Claude Code Wrapper

[English](README.md) | 日本語

`ccw` は [Claude Code](https://github.com/anthropics/claude-code) をヘッドレス（`claude -p`）で動かし、5 時間のセッション上限をまたいで長い作業を続けさせるツールです。上限のメッセージが出たら、解除される時刻を読み取ってその時刻まで待ち、`claude --continue -p "<再開プロンプト>"` で同じセッションを再開します。

- Python のファイル 1 つだけで動きます。使うのは標準ライブラリのみで、`pip install` もビルドもいりません。
- Windows 11、WSL2、macOS で動きます。
- 複数の Git worktree で同時に動かせます。同じフォルダでは、ロックファイルで 1 つしか起動できないようにしています。

## 必要なもの

- Python 3.8 以上（セキュリティ修正が続いている版を推奨）
- Claude Code の CLI（`claude`）に PATH が通っていること

## インストール

リポジトリを clone して、PATH に追加します。

```sh
git clone https://github.com/withnic/ccw.git
```

- **macOS / WSL / Linux:** `ccw` をそのまま実行できます（`#!/usr/bin/env python3` の shebang 付き）。PATH の通ったフォルダにシンボリックリンクを置いても使えます。
- **Windows（PowerShell / コマンドプロンプト）:** clone したフォルダを PATH に追加します。`ccw.cmd` が `python` で `ccw` を起動します。

## 使い方

```text
ccw [ccw-options] "<最初のプロンプト>"            [-- claude-options...]
ccw [ccw-options] (--stdin | --prompt-file PATH)  [-- claude-options...]
```

```sh
# いちばん簡単な使い方
ccw "テストを全部通して"

# claude にオプションを渡す。最初の実行だけでなく再開のたびにも付くので、
# 再開後もモデルや権限の設定が引き継がれる
ccw "テストを全部通して" -- --model opus --permission-mode acceptEdits --allowedTools "Bash(git:*)" Edit

# プロンプトをプロセス一覧やシェルの履歴に残さない
ccw --prompt-file task.md
cat task.md | ccw --stdin

# claude を起動せず、引数とロックだけ確かめる
ccw --dry-run "テストを全部通して"
```

| オプション | 既定値 | 意味 |
|---|---|---|
| `--resume-prompt TEXT` | `作業を再開してください` | 再開するときに送るプロンプト |
| `--max-retries INT` | `5` | 上限を待つ回数の上限 |
| `--fallback-wait INT` | `18000` | 解除時刻が読み取れなかったときに待つ秒数 |
| `--buffer-sec INT` | `15` | 解除時刻に足す、余裕の秒数 |
| `--stdin` | | 最初のプロンプトを標準入力から読む |
| `--prompt-file PATH` | | 最初のプロンプトを UTF-8 のファイルから読む |
| `--dry-run` | | 引数の解析とロックの確認だけをする |

プロンプトは `--` の後ろではなく、`ccw` に渡してください。`-p`/`--print`、`-c`/`--continue`、`-r`/`--resume` は `ccw` が付けるので、`--` の後ろに書くとエラーになります。

### 終了コード

| コード | 意味 |
|---|---|
| `0` | 正常に終わった |
| `1` | 引数が正しくない、同じフォルダで別の `ccw` が動いている、または `claude` が見つからない |
| `2` | `--max-retries` 回待っても上限に当たり続けた |
| `130` | 中断された（Ctrl+C、Ctrl+Break、SIGTERM、ターミナルを閉じた） |
| その他 | `claude` 自身の終了コード |

## 動きの流れ

1. `claude <claude-options> -p "<プロンプト>"` を起動し、出力をそのまま画面に流します。
2. 出力の最後の数行にセッション上限のメッセージ（`5-hour limit`、`usage limit reached`、`hit your limit` など）があれば、解除時刻（`resets 3pm`、`at 15:00`、`in 2 hours 15 minutes` など）を読み取ります。時刻が見つからなければ、`--fallback-wait` 秒待ちます。
3. その時刻に `--buffer-sec` を足した時刻まで、残り時間を表示しながら待ちます。待ち時間は実際の時計で計るので、途中で PC がスリープしてもずれません。
4. `claude <claude-options> --continue -p "<再開プロンプト>"` で再開し、2 に戻ります。

`ccw` 自身のメッセージは、`[ccw]` を先頭に付けて標準エラー出力に出します。

### Windows での出力の文字コード

出力先がパイプやファイルのとき（Git Bash も、画面への出力はパイプ扱いになります）、`ccw` は UTF-8 で書き出します。日本語や絵文字が化けたり消えたりしないようにするためです。Windows のコンソールに直接出すときは、これまでどおりです。別の文字コードにしたいときは、`PYTHONIOENCODING` を指定してください。

PowerShell は、パイプで受け取った出力を `[Console]::OutputEncoding`（多くの場合 cp932）で読みます。`ccw ... | Tee-Object log.txt` のようにパイプを使う前に、次を実行してください。

```powershell
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
```

## セキュリティ

- `claude` は、必ずシェルを通さずに起動します（`shell=False`）。Windows では npm が入れる `claude.cmd` を実行しません。`.cmd` を経由すると、`cmd.exe` がプロンプトを解釈し直してしまうからです。代わりに本体の `claude.exe` を直接起動します。見つからないときは、`CCW_CLAUDE_CMD` に JSON の配列で指定してください（例: `["C:\\path\\to\\claude.exe"]`）。
- claude の出力に含まれる代表的な秘密情報（`sk-ant-...`、AWS のキー、`Bearer ...`、GitHub のトークン、秘密鍵など）は、画面に出る前に伏せ字にします。
- `--stdin` / `--prompt-file` を使うと、プロンプトは claude の標準入力に流すので、どのプロセスの引数にも現れません。
- ロックファイル `.ccw.lock` は排他的に作成し、POSIX では権限を `0600` にします。シンボリックリンクは拒否し、自分で作ったロックだけを消します。Ctrl+C で止めたときやターミナルを閉じたときにもロックを消します。異常終了で残ったロックは、次に起動したときに片付けます。`.ccw.lock` はグローバルの gitignore（`~/.config/git/ignore`）に追加しておいてください。

## 任せきりで動かすとき

`ccw` は claude を `-p` で動かすので、「実行していいですか？」に答える人がいません。**設定で許可していない操作は、聞かれずに拒否されます。** claude はそれを避けて進めるか、そこで止まります。何をさせてよいかを、先に決めておいてください。

- **作業に必要なものを許可する。** ファイルを読む・編集する、テストを実行する、`git add` / `git commit` あたりが基本です。Claude Code の設定（`~/.claude/settings.json` か、プロジェクトの `.claude/settings.json`）に書くか、その回だけ `--` の後ろで許可します。
  ```sh
  ccw "..." -- --allowedTools "Bash(python3 -m unittest *)" "Bash(git commit *)"
  ```
- **`git push` は禁止のままにする。** 終わったらコミットを確かめて、自分で push してください。
- **permissions はサンドボックスではない。** 規則はコマンドの文字列で判定します。「テストを実行してよい」（`npm run` や `make` なども同じ）を許可すると、claude はテストのファイルに書いた、どんなコードでも実行できます。`Bash(rm -rf *)` のような禁止も、`rm -fr` やスクリプトを使えば簡単にすり抜けられます。
- **できれば、Claude Code のサンドボックスを使う（推奨）。** claude が実行するシェルのコマンドと、そこから起動したものすべてを、OS のレベルで閉じ込めます。書き込めるのは作業中のフォルダと temp だけ、通信できるのは許可したドメインだけになります。
  - 使えるのは macOS、Linux、WSL2 です。**Windows では使えない**ので、任せきりの作業は WSL2 で動かしてください。
  - Linux / WSL2 では、先に `bubblewrap` と `socat` を入れます（例: `sudo apt-get install bubblewrap socat`）。そのあと `claude` の中で `/sandbox` を開いて確かめます。
  - `"allowUnsandboxedCommands": false`（囲いの外でやり直させない）と `"failIfUnavailable": true`（囲いが使えないときは、囲いなしで動かさずに起動を止める）にしておきます。
  - 通信は、最初はすべて閉じています。パッケージのリポジトリなど、作業に必要なものだけ `sandbox.network.allowedDomains` に足します。
- **設定と hook を守る。** `~/.claude/settings.json` と git の hook に、`Edit(...)` の禁止を入れておきます。claude が自分の制限をゆるめられないようにするためです。`Edit` の規則は、ファイルを書き換えるすべての道具に効きます。`Write(...)` の規則は、ファイルには効きません。

[`examples/claude-settings.json`](examples/claude-settings.json) は、これらをまとめた設定の見本です。必要なところを自分の設定に写して、使いながら調整してください。

## 開発

```sh
python3 -m unittest -v test_ccw.py        # テスト（Windows では python）
python3 -m py_compile ccw test_ccw.py     # 構文チェック
uvx ruff check ccw test_ccw.py            # lint（ルールは ruff.toml）
```

CI では ruff とテストを、Linux と Windows（`main` では macOS も）で、Python 3.8 と最新版の両方で動かしています。

## ライセンス

[MIT](LICENSE)
