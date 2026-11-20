import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from odoo_repos import ops
from odoo_repos.main import app
from odoo_repos.ops import git


class GitHub:
    """Fake GitHub: `https://github.com/<org>/<repo>.git` resolves to local bare repositories."""

    def __init__(self, root: Path):
        self.root = root

    def work(self, org: str, repo: str) -> Path:
        return self.root / "_work" / org / repo

    def repo(self, org: str, repo: str, versions=("18.0",)) -> None:
        work = self.work(org, repo)
        work.mkdir(parents=True)
        git("init", "-q", cwd=work)
        for version in versions:
            git("checkout", "-q", "--orphan", version, cwd=work)
            (work / "README").write_text(f"{repo} {version}\n")
            (work / "other").write_text("other\n")
            git("add", ".", cwd=work)
            git("commit", "-qm", f"init {version}", cwd=work)
        bare = self.root / org / f"{repo}.git"
        git("clone", "-q", "--bare", str(work), str(bare))

    def commit(self, org: str, repo: str, version: str, file: str = "README", text: str = "upstream\n") -> str:
        work = self.work(org, repo)
        git("checkout", "-q", version, cwd=work)
        (work / file).write_text(text)
        git("commit", "-qam", f"change {file}", cwd=work)
        git("push", "-q", str(self.root / org / f"{repo}.git"), version, cwd=work)
        return git("rev-parse", "HEAD", cwd=work).stdout.strip()


@pytest.fixture
def github(tmp_path, monkeypatch):
    root = tmp_path / "github"
    settings = {
        f"url.file://{root}/.insteadOf": "https://github.com/",
        "uploadpack.allowFilter": "true",
        "user.name": "Test",
        "user.email": "test@example.com",
        "advice.detachedHead": "false",
    }
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_COUNT", str(len(settings)))
    for i, (key, value) in enumerate(settings.items()):
        monkeypatch.setenv(f"GIT_CONFIG_KEY_{i}", key)
        monkeypatch.setenv(f"GIT_CONFIG_VALUE_{i}", value)
    return GitHub(root)


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    """Keep tests away from the real cache, lock, units and GitHub token."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(ops, "github_token", lambda: None)


@pytest.fixture
def code(tmp_path) -> Path:
    path = tmp_path / "code"
    path.mkdir()
    return path


@pytest.fixture
def run(code):
    """Invoke the CLI with the temporary config."""

    def invoke(*args: str):
        return CliRunner().invoke(app, [*args, "--config", str(code / "odoo-repos.toml")])

    return invoke


@pytest.fixture
def module_index(tmp_path, monkeypatch):
    """Serve `<version>.json` files from disk through ODOO_REPOS_INDEX_URL (file://)."""
    directory = tmp_path / "index"
    directory.mkdir()

    def write(version: str, modules: dict) -> None:
        data = {"version": version, "generated_at": "2099-01-01T00:00:00Z", "modules": modules}
        (directory / f"{version}.json").write_text(json.dumps(data))

    monkeypatch.setenv("ODOO_REPOS_INDEX_URL", f"file://{directory}/{{version}}.json")
    return write
