"""`odoo-repos.toml`: read, validate, expand to checkouts, write back with tomlkit."""

import re
from dataclasses import dataclass
from pathlib import Path

import tomlkit
from tomlkit.exceptions import ParseError
from tomlkit.items import Array

DEFAULT_CONFIG = Path("~/code/odoo-repos.toml").expanduser()
DEFAULT_JOBS = 16

NAME_RE = re.compile(r"^[a-zA-Z0-9._-]+$")
VERSION_RE = re.compile(r"^(?:\d+\.\d+|master)$")


class Error(Exception):
    """User-facing error: printed without a traceback, exit code 2."""


def check_name(name: str) -> str:
    """Validate an org, repository or module name before it becomes a path or URL."""
    if not NAME_RE.match(name) or name in (".", ".."):
        raise Error(f"invalid name: {name!r}")
    return name


def check_version(version: str) -> str:
    if not VERSION_RE.match(version):
        raise Error(f"invalid version: {version!r}")
    return version


def split_repo(target: str) -> tuple[str, str]:
    """`OCA/web` -> `("oca", "web")`. Org names are lowercase everywhere."""
    org, sep, repo = target.partition("/")
    if not sep:
        raise Error(f"expected ORG/REPO, got {target!r}")
    return check_name(org.lower()), check_name(repo)


@dataclass(frozen=True)
class Checkout:
    org: str
    repo: str
    version: str
    root: Path

    @property
    def key(self) -> str:
        return f"{self.org}/{self.repo}"

    @property
    def name(self) -> str:
        return f"{self.org}/{self.repo}/{self.version}"

    @property
    def path(self) -> Path:
        return self.root / self.org / self.repo / self.version

    @property
    def base(self) -> Path:
        """Bare base shared by the worktrees of one repository."""
        return self.root / self.org / self.repo / ".bare"

    def exists(self) -> bool:
        return (self.path / ".git").exists()


class Config:
    def __init__(self, path: Path, doc: tomlkit.TOMLDocument):
        self.path = path
        self.doc = doc
        self.root = path.parent
        self.versions = [check_version(str(v)) for v in doc.get("versions", [])]
        self.jobs = int(doc.get("jobs", DEFAULT_JOBS))
        self.worktree = {"/".join(split_repo(str(k))) for k in doc.get("worktree", [])}
        self.transport = {
            k if k == "default" else "/".join(split_repo(k)): str(v)
            for k, v in ((str(k), v) for k, v in doc.get("transport", {}).items())
        }
        self.remotes = {
            "/".join(split_repo(str(k))): {check_name(str(n)): str(u) for n, u in v.items()}
            for k, v in doc.get("remotes", {}).items()
        }
        for value in self.transport.values():
            if value not in ("https", "ssh"):
                raise Error(f"{path}: transport must be 'https' or 'ssh', got {value!r}")
        self.repos()  # validate early

    @classmethod
    def load(cls, path: Path) -> "Config":
        if not path.exists():
            raise Error(f"no config at {path}: run 'odoo-repos init VERSION' first")
        try:
            return cls(path, tomlkit.parse(path.read_text()))
        except ParseError as e:
            raise Error(f"{path}: {e}") from e

    @classmethod
    def new(cls, path: Path, version: str) -> "Config":
        doc = tomlkit.document()
        doc["versions"] = [check_version(version)]
        doc["jobs"] = DEFAULT_JOBS
        doc["worktree"] = ["odoo/odoo", "odoo/enterprise"]
        doc["repos"] = tomlkit.table()
        transport = tomlkit.table()
        transport["default"] = "https"
        transport["odoo/enterprise"] = "ssh"
        doc["transport"] = transport
        return cls(path, doc)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(tomlkit.dumps(self.doc))

    def repos(self) -> dict[tuple[str, str], list[str] | None]:
        """`(org, repo)` -> explicit versions, or None for every version of the config."""
        result: dict[tuple[str, str], list[str] | None] = {}
        for org, names in self.doc.get("repos", {}).items():
            for entry in names:
                if isinstance(entry, str):
                    result[split_repo(f"{org}/{entry}")] = None
                else:
                    name, versions = entry
                    result[split_repo(f"{org}/{name}")] = [check_version(str(v)) for v in versions]
        return result

    def checkouts(self, filters: list[str] | None = None) -> list[Checkout]:
        wanted = {f.lower() for f in filters or []}
        known = {name.lower() for org, repo in self.repos() for name in (repo, f"{org}/{repo}")}
        if unknown := sorted(wanted - known):
            raise Error(f"not in config: {', '.join(unknown)}")
        return [
            Checkout(org, repo, version, self.root)
            for (org, repo), versions in self.repos().items()
            if not wanted or repo.lower() in wanted or f"{org}/{repo}".lower() in wanted
            for version in (self.versions if versions is None else versions)
        ]

    def url(self, org: str, repo: str) -> str:
        transport = self.transport.get(f"{org}/{repo}", self.transport.get("default", "https"))
        if transport == "ssh":
            return f"git@github.com:{org}/{repo}.git"
        return f"https://github.com/{org}/{repo}.git"

    def _entries(self, org: str) -> Array:
        repos = self.doc.setdefault("repos", tomlkit.table())
        if org not in repos:
            repos[org] = tomlkit.array()
        return repos[org]

    def _find(self, org: str, repo: str) -> int | None:
        for i, entry in enumerate(self.doc.get("repos", {}).get(org, [])):
            if (entry if isinstance(entry, str) else entry[0]) == repo:
                return i
        return None

    def add_repo(self, org: str, repo: str, versions: list[str] | None = None) -> bool:
        """Add a repository, or versions to it. Returns False when nothing changed."""
        current = self.repos().get((org, repo), [])
        if current is None:
            return False
        if versions is None or set(versions) | set(current) == set(self.versions):
            new = None
        else:
            new = sorted(set(current) | set(versions))
            if new == current:
                return False
        entries = self._entries(org)
        i = self._find(org, repo)
        value = repo if new is None else [repo, new]
        if i is None:
            entries.append(value)
        else:
            entries[i] = value
        return True

    def remove_repo(self, org: str, repo: str, versions: list[str] | None = None) -> bool:
        repos = self.repos()
        if (org, repo) not in repos:
            return False
        entries = self._entries(org)
        i = self._find(org, repo)
        assert i is not None  # noqa: S101
        left = [] if versions is None else [v for v in (repos[(org, repo)] or self.versions) if v not in versions]
        if left:
            entries[i] = [repo, left]
        else:
            del entries[i]
        return True
