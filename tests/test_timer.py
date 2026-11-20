import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from odoo_repos import timer
from odoo_repos.config import Error
from odoo_repos.main import app


def test_units(tmp_path):
    exe = Path(sys.executable).parent / "odoo-repos"
    files = timer.units(exe, tmp_path / "my code" / "odoo-repos.toml", "6h")
    assert f'ExecStart={exe} update --config "{tmp_path}/my code/odoo-repos.toml"' in files["odoo-repos.service"]
    assert "OnCalendar=00/6:00" in files["odoo-repos.timer"]
    for name, text in files.items():
        (tmp_path / name).write_text(text)
    if not shutil.which("systemd-analyze"):
        pytest.skip("systemd-analyze not available")
    paths = [str(tmp_path / name) for name in files]
    proc = subprocess.run(["systemd-analyze", "--user", "verify", *paths], capture_output=True, text=True)  # noqa: S603, S607
    assert proc.returncode == 0, proc.stderr


def test_every_values_parse():
    if not shutil.which("systemd-analyze"):
        pytest.skip("systemd-analyze not available")
    for calendar in timer.EVERY.values():
        assert subprocess.run(["systemd-analyze", "calendar", calendar], capture_output=True).returncode == 0  # noqa: S603, S607
    with pytest.raises(Error):
        timer.units(Path("/x"), Path("/y"), "weekly")


def test_refuses_uvx(tmp_path, monkeypatch):
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "cache/uv"))
    monkeypatch.setattr(sys, "argv", [str(tmp_path / "cache/uv/archive-v0/abc/bin/odoo-repos")])
    with pytest.raises(Error, match="uv tool install"):
        timer.executable()


def test_without_systemd_exits_2(monkeypatch):
    monkeypatch.setattr(timer, "has_systemd", lambda: False)
    result = CliRunner().invoke(app, ["timer", "disable"])
    assert result.exit_code == 2
    assert "systemd is not available" in result.output
