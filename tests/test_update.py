import json

from odoo_repos import ops
from odoo_repos.config import Config
from odoo_repos.ops import git


def write_config(code, text):
    (code / "odoo-repos.toml").write_text(text)


def head(path, ref="HEAD"):
    return git("rev-parse", ref, cwd=path).stdout.strip()


def test_clone_version_matrix_lowercases_org(github, code, run):
    github.repo("oca", "web", ["17.0", "18.0"])
    github.repo("oca", "server-ux", ["17.0"])
    write_config(code, 'versions = ["18.0", "17.0"]\n[repos]\nOCA = ["web", ["server-ux", ["17.0"]]]\n')
    result = run("clone")
    assert result.exit_code == 0, result.output
    assert "3 checkouts: 3 cloned" in result.output
    for path in ("oca/web/18.0", "oca/web/17.0", "oca/server-ux/17.0"):
        assert (code / path / ".git").exists()
    assert not (code / "oca/server-ux/18.0").exists()
    assert run("clone").output.strip() == "0 checkouts: nothing to do"


def test_update_never_loses_local_work(github, code, run):
    for repo in ("dirty-other", "dirty-same", "local-commit", "feature"):
        github.repo("oca", repo)
    write_config(code, 'versions = ["18.0"]\n[repos]\noca = ["dirty-other", "dirty-same", "local-commit", "feature"]\n')
    assert run("clone").exit_code == 0

    (code / "oca/dirty-other/18.0/other").write_text("mine\n")
    (code / "oca/dirty-same/18.0/README").write_text("mine\n")
    local = code / "oca/local-commit/18.0"
    (local / "other").write_text("committed\n")
    git("commit", "-qam", "local", cwd=local)
    local_head = head(local)
    feature = code / "oca/feature/18.0"
    git("checkout", "-qb", "feat", cwd=feature)
    for repo in ("dirty-other", "dirty-same", "local-commit", "feature"):
        upstream = github.commit("oca", repo, "18.0")

    result = run("update", "--json")
    assert result.exit_code == 1, result.output
    rows = {r["repo"]: r for r in map(json.loads, result.stdout.splitlines())}
    assert set(rows["feature"]) == {"path", "org", "repo", "version", "branch", "action", "reason", "head"}

    assert rows["dirty-other"]["action"] == "updated"
    assert (code / "oca/dirty-other/18.0/other").read_text() == "mine\n"
    assert rows["dirty-same"] == rows["dirty-same"] | {"action": "skipped", "reason": "local changes"}
    assert (code / "oca/dirty-same/18.0/README").read_text() == "mine\n"
    assert rows["local-commit"] == rows["local-commit"] | {"action": "skipped", "reason": "local commits"}
    assert head(local) == local_head
    assert rows["feature"] == rows["feature"] | {"action": "noted", "reason": "on 'feat', 18.0 moved forward"}
    assert head(feature, "refs/heads/18.0") == upstream
    assert git("symbolic-ref", "--short", "HEAD", cwd=feature).stdout.strip() == "feat"


def test_fetch_only_touches_no_working_tree(github, code, run):
    github.repo("oca", "web")
    write_config(code, 'versions = ["18.0"]\n[repos]\noca = ["web"]\n')
    run("clone")
    checkout = code / "oca/web/18.0"
    before = head(checkout)
    upstream = github.commit("oca", "web", "18.0")
    result = run("update", "--fetch-only")
    assert result.exit_code == 0, result.output
    assert head(checkout) == before
    assert head(checkout, "origin/18.0") == upstream
    assert "would fast-forward oca/web/18.0 (1 behind origin)" in run("update", "--dry-run").output
    status = json.loads(run("status", "--json").stdout)
    assert status | {"branch": "18.0", "dirty": False, "ahead": 0, "behind": 1} == status


def test_worktree_repo_and_extra_remote(github, code, run):
    github.repo("odoo", "odoo", ["17.0", "18.0"])
    github.repo("oca", "ocb", ["16.0", "17.0", "18.0"])
    write_config(
        code,
        'versions = ["18.0", "17.0"]\nworktree = ["odoo/odoo"]\n[repos]\nodoo = ["odoo"]\n'
        '[remotes]\n"odoo/odoo" = { ocb = "https://github.com/oca/ocb.git" }\n',
    )
    result = run("update")
    assert result.exit_code == 0, result.output
    base = code / "odoo/odoo/.bare"
    assert (base / "HEAD").exists()
    for version in ("17.0", "18.0"):
        assert (code / f"odoo/odoo/{version}/.git").is_file()  # a worktree, not a clone

    upstream = github.commit("odoo", "odoo", "18.0")
    github.commit("oca", "ocb", "18.0")
    result = run("update", "--json")
    assert result.exit_code == 0, result.output
    actions = {r["version"]: r["action"] for r in map(json.loads, result.stdout.splitlines())}
    assert actions == {"18.0": "updated", "17.0": "unchanged"}
    assert head(code / "odoo/odoo/18.0") == upstream
    remote_refs = git("for-each-ref", "--format=%(refname)", "refs/remotes/ocb", cwd=base).stdout.split()
    assert sorted(remote_refs) == ["refs/remotes/ocb/17.0", "refs/remotes/ocb/18.0"]
    assert not git("for-each-ref", "refs/heads/ocb", cwd=base).stdout


def test_failed_clone_leaves_nothing_and_exits_2(github, code, run):
    write_config(code, 'versions = ["18.0"]\n[repos]\noca = ["missing"]\n')
    result = run("update")
    assert result.exit_code == 2
    assert "FAIL oca/missing/18.0: clone failed" in result.output
    assert not (code / "oca/missing/18.0").exists()


def test_lock_blocks_second_run(github, code, run):
    write_config(code, 'versions = ["18.0"]\n[repos]\n')
    with ops.lock():
        result = run("update")
    assert result.exit_code == 0
    assert "another odoo-repos run" in result.output


def test_list_without_git(github, code, run):
    github.repo("oca", "web")
    write_config(code, 'versions = ["18.0"]\n[repos]\noca = ["web", "missing"]\n')
    run("clone", "-f", "web")
    rows = [json.loads(line) for line in run("list", "--json").stdout.splitlines()]
    assert [(r["path"], r["exists"]) for r in rows] == [("oca/web/18.0", True), ("oca/missing/18.0", False)]


def test_change_detection(github, code, run, monkeypatch):
    github.repo("oca", "web")
    write_config(code, 'versions = ["18.0"]\n[repos]\noca = ["web"]\n')
    run("clone")
    cfg = Config.load(code / "odoo-repos.toml")
    groups = ops.group_by_repo(cfg.checkouts(), 2)
    local = head(code / "oca/web/18.0", "origin/18.0")
    assert ops.stale_groups(groups, None) == set(groups)
    monkeypatch.setattr(ops, "remote_heads", lambda token, cs: {"oca/web/18.0": local})
    assert ops.stale_groups(groups, "token") == set()
    monkeypatch.setattr(ops, "remote_heads", lambda token, cs: {"oca/web/18.0": "0" * 40})
    assert ops.stale_groups(groups, "token") == set(groups)
    monkeypatch.setattr(ops, "remote_heads", lambda token, cs: None)  # query failed: fetch everything
    assert ops.stale_groups(groups, "token") == set(groups)


def test_remote_heads_query(monkeypatch):
    sent = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self):
            return json.dumps({"data": {"r0": {"b0": {"target": {"oid": "abc"}}, "b1": None}}}).encode()

    def urlopen(request, timeout):
        sent["query"] = json.loads(request.data)["query"]
        return Response()

    monkeypatch.setattr(ops.urllib.request, "urlopen", urlopen)
    cfg = Config.new(__import__("pathlib").Path("/x/odoo-repos.toml"), "18.0")
    cfg.add_repo("oca", "web", ["17.0", "18.0"])
    heads = ops.remote_heads("t", [c for c in cfg.checkouts() if c.repo == "web"])
    assert heads == {"oca/web/17.0": "abc", "oca/web/18.0": None}
    assert 'repository(owner: "oca", name: "web")' in sent["query"]

    def boom(request, timeout):
        raise OSError("rate limited")

    monkeypatch.setattr(ops.urllib.request, "urlopen", boom)
    assert ops.remote_heads("t", cfg.checkouts()) is None


def test_feature_branch_with_local_commits_on_version_branch(github, code, run):
    github.repo("oca", "web")
    write_config(code, 'versions = ["18.0"]\n[repos]\noca = ["web"]\n')
    run("clone")
    checkout = code / "oca/web/18.0"
    (checkout / "other").write_text("mine\n")
    git("commit", "-qam", "local", cwd=checkout)
    git("checkout", "-qb", "feat", cwd=checkout)
    github.commit("oca", "web", "18.0")
    result = run("update")
    assert result.exit_code == 0, result.output
    assert "NOTE oca/web/18.0: on 'feat', 18.0 has local commits, not moved" in result.output
