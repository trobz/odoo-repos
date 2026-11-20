import pytest

from odoo_repos.config import Config, Error, check_name

CONFIG = """\
# my clones
versions = ["18.0", "17.0"]

[repos]
odoo = ["odoo"]  # community
oca = [
  "web",  # keep me
  ["server-ux", ["18.0"]],
]
"""


def test_add_remove_keep_comments(code, run):
    path = code / "odoo-repos.toml"
    path.write_text(CONFIG)
    assert run("add", "OCA/sale-workflow", "--no-clone").exit_code == 0
    assert run("add", "oca/server-ux", "-v", "17.0", "--no-clone").exit_code == 0
    assert run("add", "oca/queue", "-v", "16.0", "--no-clone").exit_code == 0
    assert run("remove", "odoo/odoo").exit_code == 0
    assert run("remove", "oca/web", "-v", "17.0").exit_code == 0
    text = path.read_text()
    assert "# my clones" in text and "# keep me" in text
    repos = Config.load(path).repos()
    assert repos == {
        ("oca", "web"): ["18.0"],
        ("oca", "server-ux"): None,
        ("oca", "sale-workflow"): None,
        ("oca", "queue"): ["16.0"],
    }


def test_add_existing_repo_is_noop(code, run):
    (code / "odoo-repos.toml").write_text(CONFIG)
    result = run("add", "oca/web", "--no-clone")
    assert "oca/web: requested (already in config)" in result.output
    assert (code / "odoo-repos.toml").read_text() == CONFIG


@pytest.mark.parametrize("name", ["..", ".", "a/b", "a b", ""])
def test_unsafe_names_rejected(name):
    with pytest.raises(Error):
        check_name(name)


def test_invalid_config_exits_2(code, run):
    (code / "odoo-repos.toml").write_text('versions = ["18"]\n')
    result = run("list")
    assert result.exit_code == 2
    assert "invalid version" in result.output


def test_unknown_filter_exits_2(code, run):
    (code / "odoo-repos.toml").write_text(CONFIG)
    assert run("list", "-f", "OCA/Web", "-f", "server-ux").exit_code == 0
    result = run("list", "-f", "web", "-f", "nope")
    assert result.exit_code == 2
    assert "not in config: nope" in result.output


def test_init_refuses_existing_config(code, run):
    (code / "odoo-repos.toml").write_text(CONFIG)
    result = run("init", "18.0", "--no-clone")
    assert result.exit_code == 2
    assert "use 'odoo-repos add'" in result.output


def test_root_config_option_reaches_subcommands(code):
    from typer.testing import CliRunner

    from odoo_repos.main import app

    (code / "odoo-repos.toml").write_text(CONFIG)
    result = CliRunner().invoke(app, ["-c", str(code / "odoo-repos.toml"), "list"])
    assert result.exit_code == 0, result.output
    assert "oca/web/18.0" in result.output


def test_transport_keys_lowercase_org_only(code):
    path = code / "odoo-repos.toml"
    path.write_text('versions = ["18.0"]\n[transport]\ndefault = "https"\n"OCA/Web" = "ssh"\n')
    cfg = Config.load(path)
    assert cfg.url("oca", "Web") == "git@github.com:oca/Web.git"
    assert cfg.url("oca", "web") == "https://github.com/oca/web.git"
