#!/usr/bin/env python3
"""tri-sync: per-file three-way sync between a folder in a local git repo and the same
folder in an independent git repo on another machine.

Typical setup ("three parties"): the LOCAL side is a clone of a shared GitHub repo and is
the only side that pushes; the REMOTE side is another machine whose repo has its own,
unrelated history and may not push. Each run:

  1. pull LOCAL (rebase --autostash); skip the round if either side has uncommitted
     changes inside the synced folders
  2. scan both sides, NFC-normalising every path (macOS may store NFD names)
  3. compare every file against the last agreed baseline:
       only one side changed      -> copy that side's version (or delete)
       both changed, union file   -> merge dated log entries (union, sorted by date)
       both changed, other file   -> conflict: touch neither side, keep the old baseline
  4. guard: abort before writing if too many deletions are planned; after staging,
     abort and restore both sides if `git diff --numstat` deletes too much
  5. commit on both sides, push LOCAL, verify both sides match, save new baseline

No rsync --delete, no third-party dependencies (Python 3.9+ stdlib + git).
The remote side runs this same file (copied over each run) via `tri_sync.py side`.

Usage:
  tri_sync.py run [--dry-run]     one sync round (schedule it with launchd/cron)
  tri_sync.py verify              compare LOCAL, LOCAL's upstream tree and REMOTE file by file
  tri_sync.py init-base --rev REV [--remote-commit SHA]
                                  create the first baseline from a commit both sides agree on
Configuration: environment variables, see README.md.
"""
import base64
import fnmatch
import hashlib
import json
import os
import shlex
import subprocess
import sys
import time
import unicodedata

SKIP_NAMES = (".DS_Store",)
SENTINEL = "@@TRISYNC@@"


def _env(name, default=None):
    return os.environ.get("TRISYNC_" + name, default)


def load_cfg():
    return {
        "local_root": os.path.expanduser(_env("LOCAL_ROOT", ".")),
        "remote_root": _env("REMOTE_ROOT", ""),          # expanded on the remote host
        "remote_cmd": _env("REMOTE_CMD", ""),            # e.g. "ssh user@host"
        "remote_python": _env("REMOTE_PYTHON", "python3"),
        "remote_script": _env("REMOTE_SCRIPT", "~/.local/share/tri-sync/tri_sync.py"),
        "scope": [s for s in _env("SCOPE", "wiki,raw").split(",") if s],
        "union": [s for s in _env("UNION", "log.md").split(",") if s],
        "state": os.path.expanduser(_env("STATE", "~/.local/state/tri-sync")),
        "notify": _env("NOTIFY", ""),                    # command; title as arg, body on stdin
        "post_cmd": _env("POST_CMD", ""),                # shell command after LOCAL received changes
        "push": _env("PUSH", "1") == "1",
        "max_del_files": int(_env("MAX_DEL_FILES", "10")),
        "max_del_lines": int(_env("MAX_DEL_LINES", "1500")),
        "stale_hours": float(_env("STALE_HOURS", "12")),
        "local_name": _env("LOCAL_NAME", "local"),
        "remote_name": _env("REMOTE_NAME", "remote"),
    }


def nfc(s):
    return unicodedata.normalize("NFC", s)


def sha1(b):
    return hashlib.sha1(b).hexdigest()


def git(root, *args, check=True):
    r = subprocess.run(["git", *args], cwd=root, capture_output=True)
    if check and r.returncode != 0:
        raise RuntimeError("git %s failed in %s: %s" % (" ".join(args[:3]), root,
                           r.stderr.decode(errors="replace").strip()[-500:]))
    return r.stdout.decode(errors="replace")


# ---------------------------------------------------------------- one side

class LocalSide:
    """One writer. root = directory holding the scope folders (may be a git subdirectory)."""

    def __init__(self, root, scope, name="local"):
        self.root = os.path.expanduser(root)
        self.scope = list(scope)
        self.name = name
        self._real = None

    def _scan(self):
        real = {}
        for d in self.scope:
            for dp, dns, fs in os.walk(os.path.join(self.root, d)):
                dns[:] = [x for x in dns if not x.startswith(".git")]
                for f in fs:
                    p = os.path.join(dp, f)
                    if f in SKIP_NAMES or os.path.islink(p):  # symlinks are not synced
                        continue
                    rel = nfc(os.path.relpath(p, self.root))
                    if rel in real:
                        raise RuntimeError("%s has two files with the same NFC name: %s" % (self.name, rel))
                    real[rel] = p
        # files ignored by .gitignore are machine-local and never synced
        if real:
            ign = subprocess.run(["git", "check-ignore", "-z", "--stdin"], cwd=self.root,
                                 input="\0".join(real.values()).encode(), capture_output=True)
            if ign.returncode not in (0, 1):
                raise RuntimeError("git check-ignore failed: %s" % ign.stderr.decode(errors="replace"))
            ignored = {nfc(os.path.relpath(x, self.root)) for x in ign.stdout.decode().split("\0") if x}
            real = {k: v for k, v in real.items() if k not in ignored}
        self._real = real
        return real

    def status(self):
        dirty = git(self.root, "status", "--porcelain", "--", *self.scope).splitlines()
        return {"dirty": dirty[:20], "head": git(self.root, "rev-parse", "HEAD").strip()}

    def manifest(self):
        out = {}
        for rel, p in self._scan().items():
            with open(p, "rb") as fh:
                out[rel] = sha1(fh.read())
        return out

    def fetch(self, paths):
        real = self._real or self._scan()
        out = {}
        for rel in paths:
            with open(real[rel], "rb") as fh:
                out[rel] = base64.b64encode(fh.read()).decode()
        return out

    def apply(self, writes, deletes):
        """writes={nfc_path: b64}. Existing files keep their on-disk name (maybe NFD) and are
        replaced atomically. Returns the files that were newly created."""
        real = self._scan()
        created = []
        for rel, b64 in writes.items():
            p = real.get(rel) or os.path.join(self.root, rel)
            if rel not in real:
                created.append(rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            tmp = p + ".trisync.tmp"
            with open(tmp, "wb") as fh:
                fh.write(base64.b64decode(b64))
            os.replace(tmp, p)
        for rel in deletes:
            p = real.get(rel)
            if p is None:
                continue
            os.remove(p)
            d = os.path.dirname(p)
            while d != self.root and os.path.isdir(d) and not os.listdir(d):
                os.rmdir(d)
                d = os.path.dirname(d)
        self._real = None
        return {"created": created}

    def stage_guard(self, max_files, max_lines):
        git(self.root, "add", "-A", "--", *self.scope)
        del_files = len([x for x in git(self.root, "diff", "--cached", "--name-only",
                                        "--diff-filter=D", "--", *self.scope).splitlines() if x])
        del_lines = changed = 0
        for line in git(self.root, "diff", "--cached", "--numstat", "--", *self.scope).splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            changed += 1
            if parts[1].isdigit():
                del_lines += int(parts[1])
        ok = del_files <= max_files and del_lines <= max_lines
        return {"ok": ok, "del_files": del_files, "del_lines": del_lines, "changed": changed}

    def restore(self, created):
        """Reset the scope to HEAD (it was clean before the run) and remove files created this run."""
        git(self.root, "reset", "-q", "--", *self.scope, check=False)
        git(self.root, "checkout", "-q", "HEAD", "--", *self.scope, check=False)
        real = self._scan()
        for rel in created:
            if rel in real:
                os.remove(real[rel])
        return {"restored": True}

    def commit(self, msg):
        if not git(self.root, "diff", "--cached", "--name-only", "--", *self.scope).strip():
            return {"head": git(self.root, "rev-parse", "HEAD").strip(), "committed": False}
        git(self.root, "commit", "-q", "-m", msg, "--", *self.scope)
        return {"head": git(self.root, "rev-parse", "HEAD").strip(), "committed": True}


def side_main(root, scope, sub):
    """Remote entry point: JSON request on stdin -> LocalSide call -> SENTINEL + JSON on stdout
    (the sentinel skips anything a login shell prints first)."""
    s = LocalSide(root, scope.split(","), "remote")
    req = json.loads(sys.stdin.read() or "{}")
    try:
        if sub == "status":
            res = s.status()
        elif sub == "manifest":
            res = s.manifest()
        elif sub == "fetch":
            res = s.fetch(req["paths"])
        elif sub == "apply":
            res = s.apply(req["writes"], req["deletes"])
        elif sub == "stage_guard":
            res = s.stage_guard(req["max_files"], req["max_lines"])
        elif sub == "restore":
            res = s.restore(req["created"])
        elif sub == "commit":
            res = s.commit(req["msg"])
        else:
            raise SystemExit("unknown side command " + sub)
        out = {"ok": True, "res": res}
    except Exception as e:
        out = {"ok": False, "err": "%s: %s" % (type(e).__name__, e)}
    sys.stdout.write("\n" + SENTINEL + json.dumps(out, ensure_ascii=False) + "\n")


class RemoteUnreachable(RuntimeError):
    pass


class RemoteSide:
    """Proxy for the remote writer: each method is one `<remote_cmd> '<command>'` call."""

    def __init__(self, cmd, root, scope, python="python3",
                 script="~/.local/share/tri-sync/tri_sync.py", name="remote"):
        self.cmd = shlex.split(cmd)
        self.root = root
        self.scope = list(scope)
        self.python = python
        self.script = script
        self.name = name

    def install(self):
        with open(os.path.abspath(__file__), "rb") as fh:
            src = fh.read()
        self._run("mkdir -p %s && cat > %s" % (os.path.dirname(self.script), self.script), src)

    def _run(self, remote_cmd, data=b""):
        try:
            r = subprocess.run(self.cmd + [remote_cmd], input=data, capture_output=True, timeout=900)
        except subprocess.TimeoutExpired:
            raise RemoteUnreachable("remote command timed out")
        if r.returncode != 0:
            raise RemoteUnreachable("remote command exit %d: %s" % (
                r.returncode, r.stderr.decode(errors="replace").strip()[-300:]))
        return r.stdout.decode(errors="replace")

    def _call(self, sub, **req):
        out = self._run("%s -I %s side %s %s %s" % (self.python, self.script, self.root,
                                                   ",".join(self.scope), sub),
                        json.dumps(req).encode())
        if SENTINEL not in out:
            raise RuntimeError("remote %s returned no result: %s" % (sub, out[-300:]))
        resp = json.loads(out.rsplit(SENTINEL, 1)[1])
        if not resp["ok"]:
            raise RuntimeError("remote %s failed: %s" % (sub, resp["err"]))
        return resp["res"]

    def status(self):
        return self._call("status")

    def manifest(self):
        return self._call("manifest")

    def fetch(self, paths):
        return self._call("fetch", paths=paths)

    def apply(self, writes, deletes):
        return self._call("apply", writes=writes, deletes=deletes)

    def stage_guard(self, max_files, max_lines):
        return self._call("stage_guard", max_files=max_files, max_lines=max_lines)

    def restore(self, created):
        return self._call("restore", created=created)

    def commit(self, msg):
        return self._call("commit", msg=msg)


# ---------------------------------------------------------------- merge logic (pure)

def is_union(path, patterns):
    return any(fnmatch.fnmatch(os.path.basename(path), p) for p in patterns)


def classify(B, A, R, union=("log.md",)):
    """B=baseline, A=local, R=remote, each {nfc_path: sha1}. Returns the action per path."""
    plan = {"to_remote": [], "to_local": [], "del_remote": [], "del_local": [], "union": [],
            "conflict": [], "same": []}
    for k in sorted(set(B) | set(A) | set(R)):
        b, a, r = B.get(k), A.get(k), R.get(k)
        if a == r:
            if a != b:
                plan["same"].append(k)  # both made the same change; only the baseline moves
            continue
        if a == b:  # only remote changed
            plan["del_local" if r is None else "to_local"].append(k)
        elif r == b:  # only local changed
            plan["del_remote" if a is None else "to_remote"].append(k)
        elif is_union(k, union) and a is not None and r is not None:
            plan["union"].append(k)
        else:
            plan["conflict"].append(k)
    return plan


def split_log(text):
    """Dated log -> (preamble, [entries]); an entry starts with a line `## [YYYY-MM-DD]`."""
    pre, entries, cur = [], [], None
    for ln in text.splitlines(keepends=True):
        if ln.startswith("## ["):
            if cur is not None:
                entries.append("".join(cur))
            cur = [ln]
        elif cur is None:
            pre.append(ln)
        else:
            cur.append(ln)
    if cur is not None:
        entries.append("".join(cur))
    return "".join(pre), entries


def _key(e):
    return e.rstrip()


def _date(e):
    return e[4:14]


def merge_log(base, local, remote):
    """Entry-level three-way union. LOCAL's order is the backbone and its entries stay
    byte-identical; baseline entries removed by either side are dropped; entries only in
    REMOTE are inserted by date (newest first: before the first older entry, after
    same-day entries). If both sides edited the same entry, both versions are kept."""
    _, be = split_log(base or "")
    lp, le = split_log(local)
    rp, re_ = split_log(remote)
    bk = {_key(e) for e in be}
    lk = {_key(e) for e in le}
    rk = {_key(e) for e in re_}
    removed = {k for k in bk if k not in lk or k not in rk}
    out = [e for e in le if _key(e) not in removed]
    seen = {_key(e) for e in out}
    for e in re_:
        k = _key(e)
        if k in seen or k in removed:
            continue
        pos = len(out)
        for i, x in enumerate(out):
            if _date(x) < _date(e):
                pos = i
                break
        if pos > 0 and not out[pos - 1].endswith("\n"):
            out[pos - 1] += "\n"
        e = e if e.endswith("\n") else e + "\n"
        if pos < len(out) and not e.endswith("\n\n"):
            e += "\n"
        out.insert(pos, e)
        seen.add(k)
    return (lp if lp.strip() else rp) + "".join(out)


# ---------------------------------------------------------------- state

def load_state(sd):
    p = os.path.join(sd, "base.json")
    if not os.path.exists(p):
        return None
    with open(p) as fh:
        return json.load(fh)


def write_state(sd, st):
    os.makedirs(sd, exist_ok=True)
    p = os.path.join(sd, "base.json")
    with open(p + ".tmp", "w") as fh:
        json.dump(st, fh, ensure_ascii=False, indent=0, sort_keys=True)
    os.replace(p + ".tmp", p)


def save_state(sd, st):
    write_state(sd, st)
    with open(os.path.join(sd, "history.jsonl"), "a") as fh:
        fh.write(json.dumps({k: st.get(k) for k in ("ts", "local_commit", "remote_commit", "files_n",
                                                     "conflicts", "summary")},
                            ensure_ascii=False) + "\n")


def put_blob(sd, data):
    s = sha1(data)
    p = os.path.join(sd, "blobs", s)
    if not os.path.exists(p):
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as fh:
            fh.write(data)
    return s


def get_blob(sd, sha):
    p = os.path.join(sd, "blobs", sha or "-")
    if sha and os.path.exists(p):
        with open(p, "rb") as fh:
            return fh.read()
    return None


# ---------------------------------------------------------------- main flow

class Abort(Exception):
    pass


def sync(a, r, sd, cfg, dry_run=False, push=None, log=print):
    """a = LOCAL side (LocalSide), r = REMOTE side (LocalSide or RemoteSide)."""
    st = load_state(sd)
    if st is None:
        raise Abort("no baseline at %s/base.json; run init-base first" % sd)
    B = st["base"]
    union = cfg.get("union", ["log.md"])
    for s in (a, r):
        dirty = s.status()["dirty"]
        if dirty:
            raise Abort("SKIP %s has uncommitted changes in scope (%d, e.g. %s)" % (s.name, len(dirty), dirty[0]))
    A, R = a.manifest(), r.manifest()
    plan = classify(B, A, R, union)
    summary = {k: len(v) for k, v in plan.items() if v}
    log("plan " + json.dumps(summary, ensure_ascii=False))
    for k in ("conflict", "union", "del_remote", "del_local"):
        for p in plan[k][:20]:
            log("  %s %s" % (k, p))
    if max(len(plan["del_remote"]), len(plan["del_local"])) > cfg["max_del_files"]:
        raise Abort("GUARD too many planned deletions (remote %d, local %d, limit %d); nothing written"
                    % (len(plan["del_remote"]), len(plan["del_local"]), cfg["max_del_files"]))
    if dry_run:
        return {"plan": summary, "dry_run": True}

    from_r = r.fetch(plan["to_local"] + plan["union"])
    from_a = a.fetch(plan["to_remote"] + plan["union"])
    w_a = {k: from_r[k] for k in plan["to_local"]}
    w_r = {k: from_a[k] for k in plan["to_remote"]}
    for k in plan["union"]:  # the merged result goes to both sides
        base_txt = get_blob(sd, B.get(k))
        merged = merge_log(base_txt.decode() if base_txt else None,
                           base64.b64decode(from_a[k]).decode(),
                           base64.b64decode(from_r[k]).decode()).encode()
        w_a[k] = w_r[k] = base64.b64encode(merged).decode()

    created = {"a": [], "r": []}
    try:
        if w_a or plan["del_local"]:
            created["a"] = a.apply(w_a, plan["del_local"])["created"]
        if w_r or plan["del_remote"]:
            created["r"] = r.apply(w_r, plan["del_remote"])["created"]
        ga = a.stage_guard(cfg["max_del_files"], cfg["max_del_lines"])
        gr = r.stage_guard(cfg["max_del_files"], cfg["max_del_lines"])
        log("numstat %s %s | %s %s" % (a.name, ga, r.name, gr))
        if not (ga["ok"] and gr["ok"]):
            raise Abort("GUARD git diff --numstat deletes too much (%s %s | %s %s, limit %d files / %d lines);"
                        " both sides restored" % (a.name, ga, r.name, gr, cfg["max_del_files"], cfg["max_del_lines"]))
    except Exception:
        a.restore(created["a"])
        r.restore(created["r"])
        raise

    tag = time.strftime("%Y-%m-%d %H:%M")
    rc = r.commit("sync: three-way sync with %s %s" % (a.name, tag))
    ac = a.commit("sync: three-way sync with %s %s (%s %s)" % (r.name, tag, r.name, rc["head"][:7]))
    if push:
        push()

    A2, R2 = a.manifest(), r.manifest()
    conflicts = set(plan["conflict"])
    diff = sorted(k for k in set(A2) | set(R2) if A2.get(k) != R2.get(k) and k not in conflicts)
    if diff:
        raise Abort("VERIFY %d files still differ after sync: %s" % (len(diff), diff[:5]))
    newB = {k: v for k, v in A2.items() if k not in conflicts}
    for k in conflicts:  # the baseline does not move for conflicts
        if k in B:
            newB[k] = B[k]
    for k in A2:
        if is_union(k, union):
            put_blob(sd, base64.b64decode(a.fetch([k])[k]))
    st = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "local_commit": ac["head"], "remote_commit": rc["head"],
          "base": newB, "local_files": A2, "remote_files": R2, "files_n": len(A2),
          "conflicts": sorted(conflicts), "summary": summary,
          "last_ok": time.time(), "alerts": st.get("alerts", {})}
    save_state(sd, st)
    return {"plan": summary, "local": ac, "remote": rc, "files": len(A2), "conflicts": sorted(conflicts)}


# ---------------------------------------------------------------- local git, notify

def local_pull(root):
    if git(root, "rev-parse", "--abbrev-ref", "@{u}", check=False).strip():
        git(root, "pull", "-q", "--rebase", "--autostash")


def local_push(root):
    if not git(root, "rev-parse", "--abbrev-ref", "@{u}", check=False).strip():
        return
    if git(root, "rev-list", "--count", "@{u}..HEAD").strip() != "0":
        if subprocess.run(["git", "push", "-q"], cwd=root, capture_output=True).returncode != 0:
            local_pull(root)
            git(root, "push", "-q")


def notify(cfg, sd, title, body, key):
    """Send at most once per key per 6 hours; without TRISYNC_NOTIFY only print."""
    print("NOTIFY %s: %s" % (title, body.splitlines()[0] if body else ""))
    if not cfg["notify"]:
        return
    st = load_state(sd) or {}
    alerts = st.get("alerts", {})
    now = time.time()
    if now - alerts.get(key, 0) < 6 * 3600:
        return
    try:
        subprocess.run(shlex.split(cfg["notify"]) + [title], input=body.encode(),
                       capture_output=True, timeout=60)
    except Exception as e:
        print("notify failed: %s" % e)
    if st:
        alerts[key] = now
        st["alerts"] = alerts
        write_state(sd, st)


def make_sides(cfg):
    if not cfg["remote_cmd"] or not cfg["remote_root"]:
        raise SystemExit("set TRISYNC_REMOTE_CMD and TRISYNC_REMOTE_ROOT")
    a = LocalSide(cfg["local_root"], cfg["scope"], cfg["local_name"])
    r = RemoteSide(cfg["remote_cmd"], cfg["remote_root"], cfg["scope"], cfg["remote_python"],
                   cfg["remote_script"], cfg["remote_name"])
    return a, r


def cmd_run(cfg, dry_run):
    sd = cfg["state"]
    os.makedirs(sd, exist_ok=True)
    lock = os.path.join(sd, "lock")
    try:
        os.mkdir(lock)
    except FileExistsError:
        if time.time() - os.path.getmtime(lock) < 7200:
            print("another round is running; skipped")
            return 0
        os.rmdir(lock)
        os.mkdir(lock)
    print("== %s run%s" % (time.strftime("%F %T"), " --dry-run" if dry_run else ""))
    rc = 0
    stale = lambda: time.time() - (load_state(sd) or {}).get("last_ok", time.time()) > cfg["stale_hours"] * 3600
    try:
        root = cfg["local_root"]
        if not dry_run:
            local_pull(root)
            local_push(root)  # retry a push that failed last round
        a, r = make_sides(cfg)
        r.install()
        res = sync(a, r, sd, cfg, dry_run=dry_run,
                   push=(lambda: local_push(root)) if cfg["push"] and not dry_run else None)
        print("OK " + json.dumps(res, ensure_ascii=False))
        if cfg["post_cmd"] and not dry_run and any(res["plan"].get(k) for k in ("to_local", "union", "del_local")):
            p = subprocess.run(cfg["post_cmd"], shell=True, capture_output=True, timeout=1800)
            print("post_cmd exit %d" % p.returncode)
        if res.get("conflicts"):
            notify(cfg, sd, "tri-sync: conflicts",
                   "changed on both sides, left untouched:\n" + "\n".join(res["conflicts"][:30]),
                   "conflict:" + sha1("\n".join(res["conflicts"]).encode()))
    except Abort as e:
        msg = str(e)
        print(msg)
        if msg.startswith("SKIP"):
            if stale():
                notify(cfg, sd, "tri-sync: no successful sync for %g hours" % cfg["stale_hours"], msg, "stale")
        else:
            rc = 2
            notify(cfg, sd, "tri-sync: aborted", msg, "abort:" + msg[:40])
    except Exception as e:
        msg = "%s: %s" % (type(e).__name__, e)
        print("ERROR " + msg)
        # remote asleep/offline or another process holding the git lock: transient
        transient = isinstance(e, RemoteUnreachable) or "index.lock" in msg
        if not transient or stale():
            notify(cfg, sd, "tri-sync: failed", msg, "transient" if transient else "error:" + msg[:40])
        rc = 0 if transient else 1
    finally:
        os.rmdir(lock)
    return rc


def tree_manifest(repo, rev, prefix, scope, sd=None, union=()):
    """Per-file content sha1 of <prefix>/<scope> at a git commit (not git blob ids)."""
    pre = prefix.rstrip("/") + "/" if prefix not in ("", ".") else ""
    ls = git(repo, "ls-tree", "-r", "-z", "--full-tree", rev, "--", *[pre + d for d in scope])
    out = {}
    for item in ls.split("\0"):
        if not item:
            continue
        meta, path = item.split("\t", 1)
        if meta.split()[1] != "blob" or os.path.basename(path) in SKIP_NAMES:
            continue
        data = subprocess.run(["git", "cat-file", "blob", meta.split()[2]], cwd=repo,
                              capture_output=True, check=True).stdout
        rel = nfc(path[len(pre):])
        out[rel] = sha1(data)
        if sd and is_union(rel, union):
            put_blob(sd, data)
    return out


def repo_prefix(root):
    top = git(root, "rev-parse", "--show-toplevel").strip()
    return top, os.path.relpath(os.path.realpath(root), os.path.realpath(top))


def cmd_verify(cfg):
    """File-by-file comparison of LOCAL, LOCAL's upstream tree, and REMOTE."""
    root = cfg["local_root"]
    a, r = make_sides(cfg)
    r.install()
    top, prefix = repo_prefix(root)
    sides = {a.name: a.manifest()}
    if git(root, "rev-parse", "--abbrev-ref", "@{u}", check=False).strip():
        git(root, "fetch", "-q")
        sides["upstream"] = tree_manifest(top, "@{u}", prefix, cfg["scope"])
    sides[r.name] = r.manifest()
    keys = set().union(*sides.values())
    diff = sorted(k for k in keys if len({s.get(k) for s in sides.values()}) > 1)
    heads = ["%s %s" % (a.name, git(root, "rev-parse", "--short", "HEAD").strip())]
    if "upstream" in sides:
        heads.append("upstream %s" % git(root, "rev-parse", "--short", "@{u}").strip())
    heads.append("%s %s" % (r.name, r.status()["head"][:7]))
    print(" | ".join(heads))
    for n, s in sides.items():
        print("%-9s files %d | manifest %s" % (n, len(s), sha1(json.dumps(s, sort_keys=True).encode())[:12]))
    print("mismatched files: %d" % len(diff))
    for k in diff[:20]:
        print("  diff %s %s" % (k, " ".join("%s=%s" % (n, (s.get(k) or "-")[:8]) for n, s in sides.items())))
    return 1 if diff else 0


def cmd_init_base(cfg, rev, remote_commit):
    """First baseline from a commit whose scope content both sides agree on."""
    sd = cfg["state"]
    if load_state(sd):
        raise SystemExit("%s/base.json exists; not overwriting" % sd)
    top, prefix = repo_prefix(cfg["local_root"])
    base = tree_manifest(top, rev, prefix, cfg["scope"], sd, cfg["union"])
    st = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "local_commit": git(top, "rev-parse", rev).strip(),
          "remote_commit": remote_commit, "base": base, "files_n": len(base), "conflicts": [],
          "summary": {"init": len(base)}, "last_ok": time.time(), "alerts": {}}
    save_state(sd, st)
    print("baseline %d files, local_commit %s, remote_commit %s" % (len(base), st["local_commit"][:7], remote_commit))
    return 0


def main(argv):
    if len(argv) >= 5 and argv[1] == "side":
        side_main(argv[2], argv[3], argv[4])
        return 0
    cfg = load_cfg()
    if len(argv) >= 2 and argv[1] == "run":
        return cmd_run(cfg, "--dry-run" in argv)
    if len(argv) >= 2 and argv[1] == "verify":
        return cmd_verify(cfg)
    if len(argv) >= 2 and argv[1] == "init-base":
        opt = dict(zip(argv[2::2], argv[3::2]))
        return cmd_init_base(cfg, opt.get("--rev", "HEAD"), opt.get("--remote-commit", ""))
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
