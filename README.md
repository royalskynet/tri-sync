# tri-sync

Per-file three-way sync between a folder in a GitHub-backed git repo and the same folder in an **unrelated** git repo on another machine. Python 3.9+ standard library and git only.

[中文說明](#中文說明)

## When to use it

You have the same set of notes (e.g. an LLM wiki) in two places that both get written to:

- **LOCAL**: a clone of a shared GitHub repo. The folder may be a subdirectory (`notes/research/`). LOCAL is the only side that pushes.
- **REMOTE**: another machine, reached by a command such as `ssh user@host`. Its repo has its own history (its root is the folder itself), and it may not be allowed to push.

Because the two histories share no commit, `git pull`, `git subtree` and git-sync-style tools do not apply. `rsync --delete` overwrites one side's edits and, on macOS, can delete files whose names differ only in Unicode normalisation (NFC vs NFD). tri-sync compares file by file against the last state both sides agreed on.

## Rules per run

1. Pull LOCAL (`git pull --rebase --autostash`). Skip the round if either side has uncommitted changes inside the synced folders.
2. Scan both sides. Every path is NFC-normalised. Files ignored by `.gitignore` are skipped.
3. Compare each file with the baseline:
   - changed on one side only → copy that side's version (or delete it on the other side)
   - changed on both sides, file matches `TRISYNC_UNION` (default `log.md`) → merge entries; an entry starts with `## [YYYY-MM-DD]`. Existing entries stay byte-identical; new entries are inserted by date
   - changed on both sides, any other file → **conflict**. Neither side is touched, the baseline is not advanced, a notification is sent
4. Mass-deletion guards: abort before writing if more than `MAX_DEL_FILES` deletions are planned; after staging, abort and restore both sides if `git diff --numstat` deletes more than `MAX_DEL_FILES` files or `MAX_DEL_LINES` lines.
5. Commit on both sides, push LOCAL, check both sides match, save the new baseline (both commits + per-file sha list).

Files outside `TRISYNC_SCOPE` are never read or written.

## Setup

```sh
export TRISYNC_LOCAL_ROOT="$HOME/vault/notes/research"   # folder inside the GitHub clone
export TRISYNC_REMOTE_CMD="ssh me@other-mac"             # runs a shell command on REMOTE
export TRISYNC_REMOTE_ROOT="~/research"                  # REMOTE repo root
export TRISYNC_SCOPE="wiki,raw"                          # subfolders to sync

# 1. one-time: record a commit at which both sides were identical
python3 tri_sync.py init-base --rev <local-commit> --remote-commit <remote-commit>
# 2. preview
python3 tri_sync.py run --dry-run
# 3. sync, then check
python3 tri_sync.py run
python3 tri_sync.py verify
```

`verify` reads LOCAL's working tree, the `@{upstream}` tree (what is on GitHub) and REMOTE, and reports any file whose sha differs. Exit code 0 means all three match.

Schedule `run` with launchd or cron (e.g. every 30 minutes). It prints only to its log; it calls `TRISYNC_NOTIFY` only on error, conflict, guard trip, or no successful sync for `STALE_HOURS`, at most once per issue per 6 hours.

### Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `TRISYNC_LOCAL_ROOT` | `.` | synced folder inside the LOCAL clone |
| `TRISYNC_REMOTE_CMD` | — | command prefix that runs a shell command on REMOTE |
| `TRISYNC_REMOTE_ROOT` | — | REMOTE repo root (expanded on REMOTE) |
| `TRISYNC_REMOTE_PYTHON` | `python3` | Python on REMOTE |
| `TRISYNC_REMOTE_SCRIPT` | `~/.local/share/tri-sync/tri_sync.py` | where this file is copied on REMOTE each run |
| `TRISYNC_SCOPE` | `wiki,raw` | comma-separated subfolders to sync |
| `TRISYNC_UNION` | `log.md` | comma-separated glob patterns merged by entry |
| `TRISYNC_STATE` | `~/.local/state/tri-sync` | baseline and alert state |
| `TRISYNC_NOTIFY` | — | alert command; title as argument, body on stdin |
| `TRISYNC_POST_CMD` | — | shell command after LOCAL received changes (e.g. reindex) |
| `TRISYNC_PUSH` | `1` | `0` = commit LOCAL but do not push |
| `TRISYNC_MAX_DEL_FILES` | `10` | deletion guard, files |
| `TRISYNC_MAX_DEL_LINES` | `1500` | deletion guard, lines |
| `TRISYNC_STALE_HOURS` | `12` | alert if no successful sync for this long |
| `TRISYNC_LOCAL_NAME` / `TRISYNC_REMOTE_NAME` | `local` / `remote` | labels in logs |

## Limits

- Two writers plus one hub. More machines need more baselines; not supported.
- Conflicts are reported, not resolved. Fix the file on one side and commit; the next run picks it up.
- REMOTE must have git and Python 3.9+. Nothing else is installed.
- Renames are seen as delete + add.

## Tests

```sh
python3 -m unittest test_tri_sync
```

Covers one-side edits, both-side `log.md` merge, NFD file names, deletion, conflicts, both guards, uncommitted-change skip, ignored files, and the remote transport path.

## Alternatives considered

- **unison**: true two-way file sync with an archive of the last state. It still needs a wrapper for the git commit/push on each side and has no entry-level log merge.
- **git-sync / git-auto-sync**: both sides must share one history and push.
- **git subtree**: requires a shared history to split/merge.

---

## 中文說明

把 GitHub repo 裡的某個資料夾，跟另一台機器上**歷史不相干**的 git repo 做逐檔三方同步。只用 Python 3.9+ 標準函式庫和 git。

### 適用情境

同一批筆記（例如 LLM wiki）在兩處都會被寫入：

- **LOCAL**：GitHub repo 的 clone，同步的資料夾可以是子目錄。只有這邊 push。
- **REMOTE**：另一台機器，用 `ssh user@host` 之類的指令連過去。它的 repo 有自己的歷史，根目錄就是該資料夾，可以不允許 push。

兩邊沒有共同 commit，所以 `git pull`、`git subtree`、git-sync 這類工具用不上。`rsync --delete` 會蓋掉另一邊的修改；在 macOS 上，檔名只差 Unicode 正規化（NFC／NFD）的檔還可能被誤刪。tri-sync 以兩邊上次一致的狀態為基準，逐檔比對。

### 每輪規則

1. LOCAL 先 `git pull --rebase --autostash`。任一邊同步範圍內有未 commit 的改動，這輪跳過。
2. 掃描兩邊，路徑一律轉 NFC，`.gitignore` 忽略的檔不算。
3. 每個檔跟基準比：
   - 只有一邊改 → 採那邊（含刪除）
   - 兩邊都改、檔名符合 `TRISYNC_UNION`（預設 `log.md`）→ 條目取聯集。條目以 `## [YYYY-MM-DD]` 開頭；原有條目一個位元組都不動，新條目按日期插入
   - 兩邊都改其他檔 → **衝突**：兩邊都不動、基準不前進、發通知
4. 防大量刪除：寫入前計畫刪除超過 `MAX_DEL_FILES` 個檔就中止；stage 後 `git diff --numstat` 刪除超過 `MAX_DEL_FILES` 檔或 `MAX_DEL_LINES` 行，兩邊還原後中止。
5. 兩邊 commit、LOCAL push、確認兩邊一致，存新基準（兩邊 commit＋逐檔 sha 清單）。

`TRISYNC_SCOPE` 以外的檔不讀也不寫。

### 安裝與使用

見上方 [Setup](#setup)。流程：`init-base` 記下兩邊一致的 commit → `run --dry-run` 預覽 → `run` → `verify`。`verify` 比對 LOCAL 工作目錄、GitHub 上的樹（`@{upstream}`）和 REMOTE，exit 0 代表三方一致。

用 launchd 或 cron 定時跑 `run`（例如每 30 分鐘）。平常只寫 log；出錯、衝突、閘被觸發、或超過 `STALE_HOURS` 沒同步成功時才呼叫 `TRISYNC_NOTIFY`，同一件事 6 小時內只通知一次。環境變數見上表。

### 限制

- 只支援兩個寫入端加一個匯合點。
- 衝突只回報不自動解。在其中一邊改好並 commit，下一輪就會同步。
- REMOTE 需要 git 和 Python 3.9+，不需安裝其他東西。
- 改名視為刪除＋新增。

### 測試

`python3 -m unittest test_tri_sync`，涵蓋單邊改、雙邊改 log.md、NFD 檔名、刪除、衝突、兩道刪除閘、未 commit 跳過、gitignore 檔、遠端傳輸路徑。

## License

MIT
