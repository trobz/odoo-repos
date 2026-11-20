import json

from odoo_repos import index

MODULES = {
    "base": {"repo": "odoo/odoo"},
    "web": {"repo": "odoo/odoo"},
    "web_responsive": {"repo": "oca/web", "summary": "Responsive web client", "all_depends": ["base", "web"]},
    "sale_order_type": {"repo": "OCA/sale-workflow", "all_depends": ["base", "web_responsive"]},
    "broken": {"repo": "oca/web", "all_depends": ["private_thing"]},
    "web_studio": {"repo": "odoo/enterprise"},
    "../evil": {"repo": "oca/web"},
    "evil": {"repo": "../etc"},
}


def test_resolve(module_index):
    module_index("18.0", MODULES)
    idx = index.load_index("18.0")
    assert "../evil" not in idx and "evil" not in idx

    repos, problems = index.resolve(["sale_order_type"], idx)
    assert repos == {
        "oca/sale-workflow": ["sale_order_type"],
        "odoo/odoo": ["sale_order_type"],
        "oca/web": ["sale_order_type"],
    }
    assert not problems

    repos, _ = index.resolve(["web_studio"], idx)
    assert set(repos) == {"odoo/enterprise", "odoo/odoo"}

    _, problems = index.resolve(["broken", "nope"], idx)
    assert problems == ["broken: depends on unknown module 'private_thing'", "module 'nope' is not in the index"]


def test_add_module_writes_repositories(code, run, module_index):
    module_index("18.0", MODULES)
    module_index("17.0", {"base": {"repo": "odoo/odoo"}})
    (code / "odoo-repos.toml").write_text('versions = ["18.0", "17.0"]\n[repos]\nodoo = ["odoo"]\n')
    result = run("add", "web_responsive", "--no-clone")
    assert result.exit_code == 2  # absent from 17.0
    assert "oca/web: web_responsive" in result.output
    assert "17.0: module 'web_responsive' is not in the index" in result.output
    assert 'oca = [["web", ["18.0"]]]' in (code / "odoo-repos.toml").read_text()


def test_search(module_index, code, run):
    module_index("18.0", MODULES)
    result = run("search", "responsive", "-v", "18.0")
    assert result.stdout.splitlines() == ["web_responsive  oca/web  Responsive web client"]


def test_offline_uses_cache(module_index, tmp_path, monkeypatch, capsys):
    module_index("18.0", MODULES)
    index.load_index("18.0")
    (tmp_path / "index" / "18.0.json").unlink()
    assert "web_responsive" in index.load_index("18.0")
    assert "using cache" in capsys.readouterr().err


def test_stale_index_warns(module_index, tmp_path, capsys):
    data = {"version": "18.0", "generated_at": "2020-01-01T00:00:00Z", "modules": {}}
    (tmp_path / "index" / "18.0.json").write_text(json.dumps(data))
    index.load_index("18.0")
    assert "days old" in capsys.readouterr().err


def test_malformed_index(module_index, tmp_path, capsys):
    import pytest

    from odoo_repos.config import Error

    (tmp_path / "index" / "18.0.json").write_text('{"generated_at": "yesterday", "modules": {}}')
    assert index.load_index("18.0") == {}
    assert "no valid generated_at" in capsys.readouterr().err
    (tmp_path / "index" / "17.0.json").write_text("[1, 2]")
    with pytest.raises(Error, match="not a module index"):
        index.load_index("17.0")
