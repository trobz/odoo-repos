"""systemd user timer that runs `odoo-repos update`."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

from odoo_repos.config import Error

UNIT = "odoo-repos"
EVERY = {"weekdays": "Mon..Fri 08:00", "daily": "*-*-* 08:00", "6h": "00/6:00", "1h": "hourly"}
DESCRIPTION = "Update Odoo/OCA clones (odoo-repos)"


def unit_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd" / "user"


def has_systemd() -> bool:
    return bool(shutil.which("systemctl")) and Path("/run/systemd/system").is_dir()


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), capture_output=True, text=True)  # noqa: S603


def _systemctl(*args: str) -> None:
    proc = _run("systemctl", "--user", *args)
    if proc.returncode:
        raise Error(f"systemctl --user {' '.join(args)}: {proc.stderr.strip()}")


def executable() -> Path:
    """Absolute path of the running `odoo-repos`, refused when it lives in uv's cache (uvx)."""
    exe = Path(shutil.which(sys.argv[0]) or sys.argv[0]).absolute()
    uv_cache = Path(
        os.environ.get("UV_CACHE_DIR") or Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "uv"
    )
    if exe.is_relative_to(uv_cache):
        raise Error(f"{exe} is in uv's cache and will not last: run 'uv tool install odoo-repos' first")
    return exe


def _quote(value: str) -> str:
    return f'"{value}"' if any(ch.isspace() for ch in value) else value


def units(exe: Path, config: Path, every: str) -> dict[str, str]:
    if every not in EVERY:
        raise Error(f"--every must be one of {', '.join(EVERY)}")
    service = (
        f"[Unit]\nDescription={DESCRIPTION}\n\n"
        f"[Service]\nType=oneshot\nExecStart={_quote(str(exe))} update --config {_quote(str(config))}\n"
    )
    timer = (
        f"[Unit]\nDescription={DESCRIPTION}\n\n"
        f"[Timer]\nOnCalendar={EVERY[every]}\nPersistent=true\nRandomizedDelaySec=10min\n\n"
        "[Install]\nWantedBy=timers.target\n"
    )
    return {f"{UNIT}.service": service, f"{UNIT}.timer": timer}


def _require_systemd() -> None:
    if not has_systemd():
        raise Error("systemd is not available: the timer needs a systemd user session")


def enable(config: Path, every: str = "weekdays") -> list[str]:
    """Write and start the units. Returns notes for the user."""
    _require_systemd()
    directory = unit_dir()
    directory.mkdir(parents=True, exist_ok=True)
    for name, text in units(executable(), config.absolute(), every).items():
        (directory / name).write_text(text)
    _systemctl("daemon-reload")
    _systemctl("enable", "--now", f"{UNIT}.timer")
    notes = [f"timer enabled: {EVERY[every]}"]
    user = os.environ.get("USER", "")
    if "Linger=no" in _run("loginctl", "show-user", user, "-p", "Linger").stdout:
        # Linger is a choice about the machine, not about clones: suggest it, never run it.
        notes.append("linger is off: the timer only runs while you are logged in. Enable with: loginctl enable-linger")
    return notes


def disable() -> None:
    _require_systemd()
    _systemctl("disable", "--now", f"{UNIT}.timer")


def status() -> dict:
    _require_systemd()
    props = {}
    for unit, keys in (
        (f"{UNIT}.timer", "UnitFileState,ActiveState,NextElapseUSecRealtime,LastTriggerUSec"),
        (f"{UNIT}.service", "ExecMainStatus,ExecMainExitTimestamp"),
    ):
        for line in _run("systemctl", "--user", "show", unit, "-p", keys).stdout.splitlines():
            key, _, value = line.partition("=")
            props[key] = value
    timer_file = unit_dir() / f"{UNIT}.timer"
    calendar = (
        next(
            (ln.split("=", 1)[1] for ln in timer_file.read_text().splitlines() if ln.startswith("OnCalendar=")),
            None,
        )
        if timer_file.exists()
        else None
    )
    journal = _run("journalctl", "--user", "-u", f"{UNIT}.service", "-n", "1", "-o", "cat", "--no-pager")
    return {
        "enabled": props.get("UnitFileState") == "enabled",
        "active": props.get("ActiveState") == "active",
        "every": next((k for k, v in EVERY.items() if v == calendar), calendar),
        "next_run": props.get("NextElapseUSecRealtime") or None,
        "last_run": props.get("LastTriggerUSec") or None,
        "last_exit": int(props["ExecMainStatus"]) if props.get("ExecMainExitTimestamp") else None,
        "last_summary": journal.stdout.strip() or None,
    }
