"""Clone, fetch, fast-forward and inspect checkouts. git is always a subprocess with an argument list."""

import contextlib
import fcntl
import functools
import json
import os
import shutil
import subprocess
import tempfile
import urllib.request
from collections.abc import Callable, Generator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from odoo_repos.config import Checkout, Config

ACTIONS = ("cloned", "updated", "unchanged", "skipped", "noted", "failed")
GRAPHQL_URL = "https://api.github.com/graphql"
GRAPHQL_BATCH = 50


class GitError(Exception):
    pass


class Locked(Exception):
    pass


@dataclass
class Result:
    checkout: Checkout
    action: str
    reason: str = ""
    branch: str = ""
    head: str = ""

    def to_dict(self) -> dict:
        c = self.checkout
        return {
            "path": c.name,
            "org": c.org,
            "repo": c.repo,
            "version": c.version,
            "branch": self.branch,
            "action": self.action,
            "reason": self.reason,
            "head": self.head,
        }


@functools.cache
def _user_ssh_command() -> bool:
    proc = subprocess.run(["git", "config", "core.sshCommand"], capture_output=True)  # noqa: S607
    return proc.returncode == 0


def git(*args: str, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    if not _user_ssh_command():
        env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")  # never wait on a passphrase prompt
    proc = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True)  # noqa: S603, S607
    if check and proc.returncode:
        raise GitError(_message(proc.stderr))
    return proc


def _message(stderr: str) -> str:
    lines = [ln.strip() for ln in stderr.splitlines() if ln.strip()]
    useful = [ln for ln in lines if not ln.startswith(("Cloning into", "hint:", "From "))]
    return (useful or lines or ["git failed"])[0]


def _out(*args: str, cwd: Path) -> str:
    return git(*args, cwd=cwd, check=False).stdout.strip()


@contextlib.contextmanager
def lock() -> Generator[None]:
    """One run at a time, whether it comes from the timer, the TUI or a terminal."""
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        path = Path(runtime) / "odoo-repos.lock"
    else:
        path = Path(tempfile.gettempdir()) / f"odoo-repos-{os.getuid()}.lock"
    with path.open("w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Locked(f"another odoo-repos run holds {path}") from None
        yield


def _pool_map(fn: Callable, items: list, jobs: int) -> list:
    if not items:
        return []
    with ThreadPoolExecutor(max(1, jobs)) as pool:
        return list(pool.map(fn, items))


# Clone


def _add_remotes(cfg: Config, c: Checkout, gitdir: Path) -> None:
    existing = set(_out("remote", cwd=gitdir).split())
    for name, url in cfg.remotes.get(c.key, {}).items():
        if name not in existing:
            git("remote", "add", name, url, cwd=gitdir)


def _clone_one(cfg: Config, c: Checkout) -> Result:
    if c.path.exists() and any(c.path.iterdir()):
        return Result(c, "failed", f"{c.path} exists and is not a git checkout")
    try:
        git("clone", "--depth", "1", "-b", c.version, cfg.url(c.org, c.repo), str(c.path))
        _add_remotes(cfg, c, c.path)
    except BaseException as e:
        # An interrupted clone leaves nothing behind, so the next run clones again.
        shutil.rmtree(c.path, ignore_errors=True)
        if not isinstance(e, GitError):
            raise
        return Result(c, "failed", f"clone failed: {e}")
    return Result(c, "cloned", branch=c.version, head=_out("rev-parse", "HEAD", cwd=c.path))


def _ensure_base(cfg: Config, c: Checkout) -> None:
    base = c.base
    if not (base / "HEAD").exists():
        try:
            git("clone", "--bare", "--filter=blob:none", cfg.url(c.org, c.repo), str(base))
            git("config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*", cwd=base)
            git("fetch", "origin", cwd=base)
        except BaseException:
            shutil.rmtree(base, ignore_errors=True)
            raise
    _add_remotes(cfg, c, base)


def _add_worktree(c: Checkout) -> Result:
    base, v = c.base, c.version
    try:
        git("fetch", "origin", v, cwd=base)
        if git("show-ref", "--verify", "--quiet", f"refs/heads/{v}", cwd=base, check=False).returncode == 0:
            git("fetch", ".", f"origin/{v}:{v}", cwd=base, check=False)  # catch up a stale branch, never force
            git("worktree", "add", str(c.path), v, cwd=base)
            git("branch", f"--set-upstream-to=origin/{v}", v, cwd=c.path, check=False)
        else:
            git("worktree", "add", "--track", "-b", v, str(c.path), f"origin/{v}", cwd=base)
    except BaseException as e:
        shutil.rmtree(c.path, ignore_errors=True)
        git("worktree", "prune", cwd=base, check=False)
        if not isinstance(e, GitError):
            raise
        return Result(c, "failed", f"worktree failed: {e}")
    return Result(c, "cloned", branch=v, head=_out("rev-parse", "HEAD", cwd=c.path))


def _clone_group(cfg: Config, group: list[Checkout]) -> list[Result]:
    first = group[0]
    if first.key not in cfg.worktree:
        return [_clone_one(cfg, first)]
    try:
        _ensure_base(cfg, first)
    except GitError as e:
        return [Result(c, "failed", f"clone failed: {e}") for c in group]
    return [_add_worktree(c) for c in group]


def clone(cfg: Config, checkouts: list[Checkout], jobs: int, on_result: Callable | None = None) -> list[Result]:
    """Create missing checkouts. Worktrees of one repository share one task, so one base."""
    groups: dict[str, list[Checkout]] = {}
    for c in checkouts:
        if not c.exists():
            groups.setdefault(c.key if c.key in cfg.worktree else c.name, []).append(c)

    def task(group: list[Checkout]) -> list[Result]:
        results = _clone_group(cfg, group)
        for r in results:
            on_result and on_result(r)
        return results

    return [r for rs in _pool_map(task, list(groups.values()), jobs) for r in rs]


# Change detection


def github_token() -> str | None:
    if token := os.environ.get("GITHUB_TOKEN"):
        return token
    if shutil.which("gh"):
        proc = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=10)  # noqa: S607
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    return None


def _query_heads(token: str, batch: list[tuple[tuple[str, str], list[str]]]) -> dict[str, str | None] | None:
    # Names passed config validation (^[a-zA-Z0-9._-]+$), so they are safe to inline.
    parts = []
    for i, ((org, repo), versions) in enumerate(batch):
        refs = " ".join(
            f'b{j}: ref(qualifiedName: "refs/heads/{v}") {{ target {{ oid }} }}' for j, v in enumerate(versions)
        )
        parts.append(f'r{i}: repository(owner: "{org}", name: "{repo}") {{ {refs} }}')
    request = urllib.request.Request(
        GRAPHQL_URL,
        data=json.dumps({"query": "query { " + " ".join(parts) + " }"}).encode(),
        headers={"Authorization": f"bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.load(response).get("data") or {}
    except Exception:
        return None
    heads: dict[str, str | None] = {}
    for i, ((org, repo), versions) in enumerate(batch):
        node = data.get(f"r{i}") or {}
        for j, v in enumerate(versions):
            ref = node.get(f"b{j}") or {}
            heads[f"{org}/{repo}/{v}"] = (ref.get("target") or {}).get("oid")
    return heads


def remote_heads(token: str, checkouts: list[Checkout]) -> dict[str, str | None] | None:
    """Head of each `org/repo/version` branch on GitHub, one query per batch, batches in parallel. None on error."""
    by_repo: dict[tuple[str, str], list[str]] = {}
    for c in checkouts:
        by_repo.setdefault((c.org, c.repo), []).append(c.version)
    repos = list(by_repo.items())
    batches = [repos[i : i + GRAPHQL_BATCH] for i in range(0, len(repos), GRAPHQL_BATCH)]
    results = _pool_map(lambda batch: _query_heads(token, batch), batches, len(batches))
    if any(r is None for r in results):
        return None
    return {k: v for r in results for k, v in r.items()}


# Update


def _common_dir(c: Checkout) -> Path:
    return Path(_out("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=c.path))


def group_by_repo(checkouts: list[Checkout], jobs: int) -> dict[Path, list[Checkout]]:
    """Worktrees of one base share one fetch."""
    groups: dict[Path, list[Checkout]] = {}
    for c, gitdir in zip(checkouts, _pool_map(_common_dir, checkouts, jobs), strict=True):
        groups.setdefault(gitdir, []).append(c)
    return groups


def stale_groups(groups: dict[Path, list[Checkout]], token: str | None) -> set[Path]:
    """Groups whose origin moved on GitHub. Every group without a token or when the query fails."""
    if not token:
        return set(groups)
    heads = remote_heads(token, [c for cs in groups.values() for c in cs])
    if heads is None:
        return set(groups)
    return {
        gitdir
        for gitdir, cs in groups.items()
        if any(
            heads.get(c.name) is None
            or heads[c.name] != _out("rev-parse", "-q", "--verify", f"refs/remotes/origin/{c.version}", cwd=gitdir)
            for c in cs
        )
    }


def _fetch(cfg: Config, gitdir: Path, cs: list[Checkout], origin: bool) -> str | None:
    try:
        _add_remotes(cfg, cs[0], gitdir)
        if origin:
            git("fetch", "origin", cwd=gitdir)
        versions = sorted({c.version for c in cs})
        for name in cfg.remotes.get(cs[0].key, {}):
            git("fetch", name, *versions, cwd=gitdir)  # only the config's versions, never merged
    except GitError as e:
        return f"fetch failed: {e}"
    return None


def _skip_or_fail(c: Checkout, branch: str, stderr: str) -> Result:
    message = _message(stderr)
    if "overwritten" in stderr:
        return Result(c, "skipped", "local changes", branch)
    if "fast-forward" in stderr:
        return Result(c, "skipped", "local commits", branch)
    if "index.lock" in stderr:
        return Result(c, "skipped", "index.lock held by another git process", branch)
    return Result(c, "failed", message, branch)


def fast_forward(c: Checkout) -> Result:
    """Move the checkout to origin/<version> without ever losing local work."""
    p, v = c.path, c.version
    branch = _out("symbolic-ref", "--short", "-q", "HEAD", cwd=p)
    before = _out("rev-parse", "HEAD", cwd=p)
    if not branch:
        return Result(c, "noted", "detached HEAD", head=before)
    if branch == v:
        proc = git("merge", "--ff-only", "--no-autostash", f"origin/{v}", cwd=p, check=False)
        if proc.returncode:
            result = _skip_or_fail(c, branch, proc.stderr)
            result.head = before
            return result
        after = _out("rev-parse", "HEAD", cwd=p)
        return Result(c, "updated" if after != before else "unchanged", branch=branch, head=after)
    # On another branch: move the local version branch without touching the working tree.
    old = _out("rev-parse", "-q", "--verify", f"refs/heads/{v}", cwd=p)
    proc = git("fetch", ".", f"origin/{v}:{v}", cwd=p, check=False)
    reason = f"on '{branch}'"
    if "non-fast-forward" in proc.stderr:
        reason += f", {v} has local commits, not moved"
    elif proc.returncode:
        reason += f", {v} not moved: {_message(proc.stderr)}"
    elif _out("rev-parse", "-q", "--verify", f"refs/heads/{v}", cwd=p) != old:
        reason += f", {v} moved forward"
    return Result(c, "noted", reason, branch, before)


def update(
    cfg: Config,
    checkouts: list[Checkout],
    jobs: int,
    fetch_only: bool = False,
    token: str | None = None,
    on_result: Callable | None = None,
) -> list[Result]:
    done = {r.checkout: r for r in clone(cfg, checkouts, jobs, on_result)}
    present = [c for c in checkouts if c not in done]
    groups = group_by_repo(present, jobs)
    stale = stale_groups(groups, token)
    errors = dict(
        zip(
            groups,
            _pool_map(lambda g: _fetch(cfg, g, groups[g], g in stale), list(groups), jobs),
            strict=True,
        )
    )

    def finish(item: tuple[Path, Checkout]) -> Result:
        gitdir, c = item
        if errors[gitdir]:
            result = Result(c, "failed", errors[gitdir])
        elif fetch_only:
            result = Result(c, "unchanged", "fetched" if gitdir in stale else "")
        else:
            result = fast_forward(c)
        on_result and on_result(result)
        return result

    items = [(g, c) for g, cs in groups.items() for c in cs]
    done.update({r.checkout: r for r in _pool_map(finish, items, jobs)})
    return [done[c] for c in checkouts]


def exit_code(results: list[Result]) -> int:
    actions = {r.action for r in results}
    return 2 if "failed" in actions else 1 if "skipped" in actions else 0


def summary(results: list[Result]) -> str:
    labels = {
        "cloned": "cloned",
        "updated": "updated",
        "unchanged": "up to date",
        "skipped": "skipped",
        "noted": "noted",
        "failed": "failed",
    }
    counts = [f"{n} {labels[a]}" for a in ACTIONS if (n := sum(r.action == a for r in results))]
    return f"{len(results)} checkouts: " + (", ".join(counts) or "nothing to do")


# Status


def status(c: Checkout, remote_head: str | None = None) -> dict:
    row = {"path": c.name, "org": c.org, "repo": c.repo, "version": c.version, "exists": c.exists()}
    if not row["exists"]:
        return row
    p, v = c.path, c.version
    counts = _out("rev-list", "--left-right", "--count", f"HEAD...origin/{v}", cwd=p).split()
    gitdir = _common_dir(c)
    fetch_head = gitdir / "FETCH_HEAD"
    origin = _out("rev-parse", "-q", "--verify", f"refs/remotes/origin/{v}", cwd=p)
    row |= {
        "branch": _out("symbolic-ref", "--short", "-q", "HEAD", cwd=p),
        "dirty": bool(_out("status", "--porcelain", "--untracked-files=no", cwd=p)),
        "ahead": int(counts[0]) if counts else None,
        "behind": int(counts[1]) if counts else None,
        "head": _out("rev-parse", "HEAD", cwd=p),
        "last_fetch": (
            datetime.fromtimestamp(fetch_head.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")
            if fetch_head.exists()
            else None
        ),
    }
    if remote_head is not None:
        row["remote_changed"] = remote_head != origin
    return row


def statuses(checkouts: list[Checkout], jobs: int, remote: bool = False) -> list[dict]:
    heads = (remote_heads(token, checkouts) if (token := github_token()) else None) if remote else None
    return _pool_map(lambda c: status(c, (heads or {}).get(c.name)), checkouts, jobs)
