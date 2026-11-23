import functools
import json
import sys
from pathlib import Path
from typing import Annotated

import typer

from odoo_repos import index, ops, timer
from odoo_repos.config import DEFAULT_CONFIG, Config, Error, check_version, split_repo

app = typer.Typer(no_args_is_help=False, add_completion=False)
timer_app = typer.Typer(help="Manage the systemd user timer that runs 'update'.")
app.add_typer(timer_app, name="timer")

ConfigOpt = Annotated[Path, typer.Option("--config", "-c", help="Config file; its folder is the code root.")]
FilterOpt = Annotated[list[str] | None, typer.Option("--filter", "-f", help="Repository name, repeatable.")]
JobsOpt = Annotated[int | None, typer.Option("--jobs", "-j", help="Parallel git processes [default: config or 16].")]
VersionsOpt = Annotated[list[str] | None, typer.Option("--version", "-v", help="Odoo version, repeatable.")]
JsonOpt = Annotated[bool, typer.Option("--json", help="One JSON object per line.")]
DryRunOpt = Annotated[bool, typer.Option("--dry-run", help="Print what would be done.")]

LABELS = {"cloned": "CLONE", "skipped": "SKIP", "noted": "NOTE", "failed": "FAIL"}


def handle_errors(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Error as e:
            print(f"error: {e}", file=sys.stderr)
            raise typer.Exit(2) from None
        except ops.Locked as e:
            print(f"{e}, nothing to do", file=sys.stderr)
            raise typer.Exit(0) from None

    return wrapper


def command(name: str | None = None):
    return lambda fn: app.command(name)(handle_errors(fn))


def printer(as_json: bool):
    def on_result(result: ops.Result) -> None:
        if as_json:
            print(json.dumps(result.to_dict()), flush=True)
        elif result.action in LABELS:
            print(f"{LABELS[result.action]} {result.checkout.name}: {result.reason or result.branch}", flush=True)

    return on_result


def finish(results: list[ops.Result], as_json: bool = False) -> None:
    print(ops.summary(results), file=sys.stderr if as_json else sys.stdout)
    raise typer.Exit(ops.exit_code(results))


def report(messages: list[str], problems: list[str]) -> None:
    for message in messages:
        print(message)
    for problem in problems:
        print(f"error: {problem}", file=sys.stderr)


def clone_added(cfg: Config, added: list[tuple[str, str]], jobs: int | None) -> list[ops.Result]:
    checkouts = [c for c in cfg.checkouts() if (c.org, c.repo) in added]
    with ops.lock():
        return ops.clone(cfg, checkouts, jobs or cfg.jobs, printer(False))


@app.callback(invoke_without_command=True)
def root(ctx: typer.Context, config: ConfigOpt = DEFAULT_CONFIG):
    """Clone and keep Odoo/OCA repositories up to date. Without a command: the TUI in a terminal."""
    defaults = {"config": config}
    ctx.default_map = dict.fromkeys(("init", "add", "remove", "search", "list", "clone", "update", "status"), defaults)
    ctx.default_map["timer"] = {"enable": defaults}
    if ctx.invoked_subcommand is None:
        if sys.stdin.isatty() and sys.stdout.isatty():
            from odoo_repos.tui import OdooReposApp

            OdooReposApp(config).run()
        else:
            print(ctx.get_help())


@command()
def init(
    version: str,
    preset: Annotated[str, typer.Option(help=f"One of: {', '.join(index.PRESETS)}.")] = "odoo-minimal",
    module: Annotated[list[str] | None, typer.Option("--module", help="Module name, repeatable.")] = None,
    repo: Annotated[list[str] | None, typer.Option("--repo", help="ORG/REPO, repeatable.")] = None,
    no_clone: Annotated[bool, typer.Option("--no-clone")] = False,
    config: ConfigOpt = DEFAULT_CONFIG,
):
    """Write a config for one version, then clone."""
    if any("/" not in r for r in repo or []):
        raise Error("--repo expects ORG/REPO")
    if any("/" in m for m in module or []):
        raise Error("--module expects a module name, use --repo for ORG/REPO")
    cfg, messages, problems = index.init_config(config, version, preset, [*(repo or []), *(module or [])])
    report(messages, problems)
    cfg.save()
    print(f"wrote {config}")
    results = [] if no_clone else clone_added(cfg, list(cfg.repos()), None)
    if results:
        print(ops.summary(results))
    if sys.stdin.isatty() and timer.has_systemd() and typer.confirm("Enable the update timer (weekdays)?"):
        for note in timer.enable(config):
            print(note)
    raise typer.Exit(2 if problems else ops.exit_code(results))


@command()
def add(
    targets: Annotated[list[str], typer.Argument(help="Module names or ORG/REPO.")],
    version: VersionsOpt = None,
    no_clone: Annotated[bool, typer.Option("--no-clone")] = False,
    jobs: JobsOpt = None,
    config: ConfigOpt = DEFAULT_CONFIG,
):
    """Add modules (resolved to repositories, dependencies included) or repositories."""
    cfg = Config.load(config)
    added, messages, problems = index.add_targets(cfg, targets, version)
    report(messages, problems)
    if added:
        cfg.save()
    results = clone_added(cfg, added, jobs) if added and not no_clone else []
    if results:
        print(ops.summary(results))
    raise typer.Exit(2 if problems else ops.exit_code(results))


@command()
def remove(
    repos: Annotated[list[str], typer.Argument(help="ORG/REPO.")],
    version: VersionsOpt = None,
    config: ConfigOpt = DEFAULT_CONFIG,
):
    """Take repositories out of the config. Clones stay on disk."""
    cfg = Config.load(config)
    versions = [check_version(v) for v in version] if version else None
    changed = False
    for target in repos:
        org, name = split_repo(target)
        if cfg.remove_repo(org, name, versions):
            changed = True
            print(f"removed {org}/{name}" + (f" {', '.join(versions)}" if versions else ""))
        else:
            print(f"{org}/{name}: not in config")
    if changed:
        cfg.save()


@command()
def search(query: str, version: VersionsOpt = None, config: ConfigOpt = DEFAULT_CONFIG):
    """Look up modules in the index by technical name and summary."""
    versions = [check_version(v) for v in version] if version else None
    if not versions:
        if not config.exists():
            raise Error("no config: pass --version")
        versions = Config.load(config).versions
    for v in versions:
        for name, entry in index.search(query, index.load_index(v)):
            prefix = f"{v}  " if len(versions) > 1 else ""
            print(f"{prefix}{name}  {entry['repo']}  {entry.get('summary', '')}".rstrip())


@command("list")
def list_(filter_: FilterOpt = None, as_json: JsonOpt = False, config: ConfigOpt = DEFAULT_CONFIG):
    """Repositories of the config, one line per version. No git call."""
    for c in Config.load(config).checkouts(filter_):
        if as_json:
            row = {"path": c.name, "org": c.org, "repo": c.repo, "version": c.version}
            print(json.dumps(row | {"dir": str(c.path), "exists": c.exists()}))
        else:
            print(f"{c.name}  {c.path}  {'ok' if c.exists() else 'missing'}")


@command()
def clone(
    filter_: FilterOpt = None, jobs: JobsOpt = None, dry_run: DryRunOpt = False, config: ConfigOpt = DEFAULT_CONFIG
):
    """Create missing checkouts, touch nothing else."""
    cfg = Config.load(config)
    checkouts = cfg.checkouts(filter_)
    if dry_run:
        for c in checkouts:
            if not c.exists():
                print(f"would clone {c.name} from {cfg.url(c.org, c.repo)}")
        return
    with ops.lock():
        finish(ops.clone(cfg, checkouts, jobs or cfg.jobs, printer(False)))


@command()
def update(
    filter_: FilterOpt = None,
    jobs: JobsOpt = None,
    fetch_only: Annotated[bool, typer.Option("--fetch-only", help="Fetch, never touch working trees.")] = False,
    as_json: JsonOpt = False,
    dry_run: DryRunOpt = False,
    config: ConfigOpt = DEFAULT_CONFIG,
):
    """Clone missing checkouts, fetch, then fast-forward. Local work is never touched."""
    cfg = Config.load(config)
    checkouts = cfg.checkouts(filter_)
    jobs = jobs or cfg.jobs
    token = ops.github_token()
    if dry_run:
        present = [c for c in checkouts if c.exists()]
        for c in checkouts:
            if not c.exists():
                print(f"would clone {c.name}")
        groups = ops.group_by_repo(present, jobs)
        for gitdir in ops.stale_groups(groups, token):
            print(f"would fetch {groups[gitdir][0].key} ({', '.join(c.version for c in groups[gitdir])})")
        if not fetch_only:
            for row in ops.statuses(present, jobs):
                if row.get("behind"):
                    print(f"would fast-forward {row['path']} ({row['behind']} behind origin)")
        return
    with ops.lock():
        finish(ops.update(cfg, checkouts, jobs, fetch_only, token, printer(as_json)), as_json)


@command()
def status(
    filter_: FilterOpt = None,
    as_json: JsonOpt = False,
    remote: Annotated[bool, typer.Option("--remote", help="Ask GitHub whether origin moved.")] = False,
    jobs: JobsOpt = None,
    config: ConfigOpt = DEFAULT_CONFIG,
):
    """Branch, dirty state and ahead/behind per checkout, relative to the last fetch."""
    cfg = Config.load(config)
    if remote and not ops.github_token():
        print("warning: no GitHub token (GITHUB_TOKEN or gh auth login), --remote ignored", file=sys.stderr)
    for row in ops.statuses(cfg.checkouts(filter_), jobs or cfg.jobs, remote):
        if as_json:
            print(json.dumps(row))
        elif not row["exists"]:
            print(f"{row['path']}  missing")
        else:
            parts = [row["path"], row["branch"] or "(detached)", "dirty" if row["dirty"] else "clean"]
            if row["ahead"] is not None:
                parts.append(f"+{row['ahead']} -{row['behind']}")
            if row.get("remote_changed"):
                parts.append("remote moved")
            print("  ".join(parts))


@timer_app.command("enable")
@handle_errors
def timer_enable(
    every: Annotated[str, typer.Option(help=f"One of: {', '.join(timer.EVERY)}.")] = "weekdays",
    config: ConfigOpt = DEFAULT_CONFIG,
):
    """Write the user units and start the timer."""
    Config.load(config)
    for note in timer.enable(config, every):
        print(note)


@timer_app.command("disable")
@handle_errors
def timer_disable():
    """Stop the timer. Unit files stay."""
    timer.disable()
    print("timer disabled")


@timer_app.command("status")
@handle_errors
def timer_status(as_json: JsonOpt = False):
    """Enabled state, frequency, next and last run."""
    info = timer.status()
    if as_json:
        print(json.dumps(info))
    else:
        for key, value in info.items():
            print(f"{key}: {value}")


if __name__ == "__main__":
    app()
