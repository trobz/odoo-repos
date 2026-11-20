"""Module index (published by module-tracker) and repository lists (`trobz/odoo-addons-repos`).

Both are network data: every name read from them is validated before it becomes a path or URL.
"""

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import tomlkit

from odoo_repos.config import Config, Error, check_name, check_version, split_repo

INDEX_URL = "https://trobz.github.io/odoo-addons-repos/modules/{version}.json"
REPO_LIST_URL = "https://raw.githubusercontent.com/trobz/odoo-addons-repos/main/all_repos_{version}.toml"
STALE_DAYS = 3
PRESETS = ("odoo-minimal", "oca-contributor")
ENTERPRISE = "odoo/enterprise"


def warn(message: str) -> None:
    print(f"warning: {message}", file=sys.stderr)


def cache_dir() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "odoo-repos"


def _age(seconds: float) -> str:
    hours = int(seconds // 3600)
    return f"{hours // 24}d" if hours >= 24 else f"{hours}h"


def fetch_cached(url: str, name: str) -> str:
    """Download `url`, revalidated with If-None-Match. Offline, fall back to the cache and say how old it is."""
    cache = cache_dir() / name
    etag_file = cache.with_name(name + ".etag")
    request = urllib.request.Request(url)
    if cache.exists() and etag_file.exists():
        request.add_header("If-None-Match", etag_file.read_text().strip())
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode()
            etag = response.headers.get("ETag")
    except urllib.error.HTTPError as e:
        if e.code == 304:
            cache.touch()
            return cache.read_text()
        if not cache.exists():
            raise Error(f"{url}: HTTP {e.code}") from e
        warn(f"{url}: HTTP {e.code}, using cache from {_age(datetime.now().timestamp() - cache.stat().st_mtime)} ago")
        return cache.read_text()
    except OSError as e:
        if not cache.exists():
            raise Error(f"{url}: {e}") from e
        warn(f"{url} unreachable, using cache from {_age(datetime.now().timestamp() - cache.stat().st_mtime)} ago")
        return cache.read_text()
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(body)
    if etag:
        etag_file.write_text(etag)
    return body


def load_index(version: str) -> dict[str, dict]:
    """`module` -> entry, for one version. Entries with an invalid name or repo are skipped."""
    url = os.environ.get("ODOO_REPOS_INDEX_URL", INDEX_URL).format(version=version)
    try:
        data = json.loads(fetch_cached(url, f"modules-{version}.json"))
    except json.JSONDecodeError as e:
        raise Error(f"{url}: invalid JSON: {e}") from e
    if not isinstance(data, dict) or not isinstance(data.get("modules", {}), dict):
        raise Error(f"{url}: not a module index")
    try:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(str(data["generated_at"]).replace("Z", "+00:00"))
        if age.days >= STALE_DAYS:
            warn(f"module index for {version} is {age.days} days old")
    except (KeyError, ValueError, TypeError):
        warn(f"module index for {version} has no valid generated_at")
    modules = {}
    for name, entry in data.get("modules", {}).items():
        try:
            check_name(name)
            entry["repo"] = "/".join(split_repo(entry["repo"]))
        except (Error, KeyError, TypeError, AttributeError):
            warn(f"module index {version}: skipping invalid entry {name!r}")
            continue
        modules[name] = entry
    return modules


def resolve(modules: list[str], index: dict[str, dict]) -> tuple[dict[str, list[str]], list[str]]:
    """Modules -> `{repo: [modules that need it]}`, plus problems found on the way.

    A dependency missing from the index is reported, not guessed.
    """
    repos: dict[str, list[str]] = {}
    problems = []

    def need(repo: str, module: str) -> None:
        repos.setdefault(repo, [])
        if module not in repos[repo]:
            repos[repo].append(module)

    for module in modules:
        entry = index.get(module)
        if entry is None:
            problems.append(f"module {module!r} is not in the index")
            continue
        need(entry["repo"], module)
        if entry["repo"] == ENTERPRISE:
            need("odoo/odoo", module)
        for dep in entry.get("all_depends", []):
            if dep in index:
                need(index[dep]["repo"], module)
            else:
                problems.append(f"{module}: depends on unknown module {dep!r}")
    return repos, problems


def search(query: str, index: dict[str, dict]) -> list[tuple[str, dict]]:
    q = query.lower()
    return sorted((n, e) for n, e in index.items() if q in n.lower() or q in e.get("summary", "").lower())


def repo_list(version: str) -> list[tuple[str, str]]:
    """Repositories of `all_repos_<version>.toml`, as `(org, repo)`."""
    url = REPO_LIST_URL.format(version=version)
    doc = tomlkit.parse(fetch_cached(url, f"all_repos_{version}.toml"))
    result = []
    for org, names in doc.get("repos", {}).items():
        for entry in names:
            name = entry if isinstance(entry, str) else entry[0]
            try:
                result.append(split_repo(f"{org}/{name}"))
            except Error:
                warn(f"{url}: skipping invalid repository {org}/{name}")
    return result


def preset_repos(preset: str, version: str) -> list[tuple[str, str]]:
    if preset == "odoo-minimal":
        return [("odoo", "odoo")]
    if preset == "oca-contributor":
        return repo_list(version)
    raise Error(f"unknown preset {preset!r}, expected one of {', '.join(PRESETS)}")


def add_targets(
    cfg: Config, targets: list[str], versions: list[str] | None = None
) -> tuple[list[tuple[str, str]], list[str], list[str]]:
    """Resolve module names and `org/repo` targets, write the missing repositories to `cfg` (not saved).

    Returns `(added, messages, problems)`; messages say which repository is added for which module.
    """
    versions = [check_version(v) for v in versions] if versions else cfg.versions
    need: dict[tuple[str, str], tuple[set[str], list[str]]] = {}
    for target in targets:
        if "/" in target:
            need.setdefault(split_repo(target), (set(), []))[0].update(versions)
    modules = [check_name(t) for t in targets if "/" not in t]
    problems = []
    for version in versions if modules else []:
        repos, found = resolve(modules, load_index(version))
        problems += [f"{version}: {p}" for p in found]
        for repo, by in repos.items():
            vs, reasons = need.setdefault(split_repo(repo), (set(), []))
            vs.add(version)
            reasons += [m for m in by if m not in reasons]
    added, messages = [], []
    for (org, repo), (vs, reasons) in need.items():
        changed = cfg.add_repo(org, repo, sorted(vs))
        messages.append(f"{org}/{repo}: {', '.join(reasons) or 'requested'}{'' if changed else ' (already in config)'}")
        if changed:
            added.append((org, repo))
    return added, messages, problems


def init_config(path: Path, version: str, preset: str, targets: list[str]) -> tuple[Config, list[str], list[str]]:
    """New config for one version from a preset plus targets (not saved). Refuses an existing file."""
    if path.exists():
        raise Error(f"{path} already exists: use 'odoo-repos add'")
    cfg = Config.new(path, version)
    for org, name in preset_repos(preset, version):
        cfg.add_repo(org, name)
    _, messages, problems = add_targets(cfg, targets)
    return cfg, messages, problems
