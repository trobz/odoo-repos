from typer.testing import CliRunner

from odoo_repos.main import app


def test_hello():
    result = CliRunner().invoke(app, ["World"])
    assert result.exit_code == 0
    assert "Hello World" in result.stdout
