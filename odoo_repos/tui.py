"""Textual front end. No logic of its own: every action calls the function behind the matching CLI command."""

from pathlib import Path
from typing import ClassVar

from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.coordinate import Coordinate
from textual.screen import Screen
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, Select, Static, TabbedContent, TabPane

from odoo_repos import index, ops, timer
from odoo_repos.config import Config, Error

COLUMNS = ("", "Repository", "Version", "Branch", "State", "+/-", "Last fetch", "Last run")
TABS = {
    "repos": {"toggle_mark", "update", "fetch", "refresh", "remove"},
    "add": {"toggle_mark", "add"},
    "timer": {"timer_enable", "timer_disable", "timer_every"},
}


class SetupScreen(Screen):
    """Shown without a config: the same choices as `init`."""

    def __init__(self, path: Path):
        super().__init__()
        self.path = path

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical():
            yield Label(f"No config at {self.path}. Create one:")
            yield Input(placeholder="Version, e.g. 18.0", id="version")
            yield Select([(p, p) for p in index.PRESETS], value="odoo-minimal", allow_blank=False, id="preset")
            yield Input(placeholder="Modules or org/repo, space separated (optional)", id="targets")
            yield Button("Create", id="create", variant="primary")
        yield Footer()

    def on_button_pressed(self) -> None:
        version = self.query_one("#version", Input).value.strip()
        preset = str(self.query_one("#preset", Select).value)
        targets = self.query_one("#targets", Input).value.split()
        self.create(version, preset, targets)

    @work(thread=True)
    def create(self, version: str, preset: str, targets: list[str]) -> None:
        try:
            cfg, messages, problems = index.init_config(self.path, version, preset, targets)
        except Error as e:
            self.app.call_from_thread(self.notify, str(e), severity="error")
            return
        cfg.save()
        for line in [*messages, *problems]:
            self.app.call_from_thread(self.notify, line, severity="error" if line in problems else "information")
        self.app.call_from_thread(self.dismiss, cfg)


class OdooReposApp(App):
    TITLE = "odoo-repos"
    CSS = """
    #add-bar { height: auto; }
    #query { width: 1fr; }
    #add-version { width: 20; }
    #preview { height: auto; max-height: 10; }
    """
    BINDINGS: ClassVar = [
        ("q", "quit", "Quit"),
        ("1", "tab('repos')", "Repositories"),
        ("2", "tab('add')", "Add"),
        ("3", "tab('timer')", "Timer"),
        ("space", "toggle_mark", "Mark"),
        ("u", "update", "Update"),
        ("f", "fetch", "Fetch only"),
        ("r", "refresh", "Refresh (GitHub)"),
        ("d", "remove", "Remove"),
        ("a", "add", "Add selected"),
        ("e", "timer_enable", "Enable"),
        ("x", "timer_disable", "Disable"),
        ("n", "timer_every", "Frequency"),
    ]

    def __init__(self, config: Path):
        super().__init__()
        self.config_path = config
        self.cfg: Config | None = None
        self.rows: dict[str, dict] = {}
        self.results: dict[str, ops.Result] = {}
        self.marked: set[str] = set()
        self.picked: set[str] = set()
        self.indexes: dict[str, dict] = {}
        self.every = "weekdays"

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(initial="repos", id="tabs"):
            with TabPane("Repositories", id="repos"):
                yield Input(placeholder="Filter by text, version or state", id="filter")
                yield DataTable(id="table", cursor_type="row")
            with TabPane("Add", id="add"):
                with Horizontal(id="add-bar"):
                    yield Input(placeholder="Search modules or repositories, enter to pick", id="query")
                    yield Select([], allow_blank=True, id="add-version")
                with TabbedContent(id="add-tabs"):
                    with TabPane("Modules", id="add-modules"):
                        yield DataTable(id="modules", cursor_type="row")
                    with TabPane("Repositories", id="add-repos"):
                        yield DataTable(id="repolist", cursor_type="row")
                yield Static(id="preview")
            with TabPane("Timer", id="timer"):
                yield Static(id="timer-info")
        yield Footer()

    def on_mount(self) -> None:
        for table_id, columns in (
            ("#table", COLUMNS[1:]),
            ("#modules", ("Module", "Repository", "Summary")),
            ("#repolist", ("Repository",)),
        ):
            table = self.query_one(table_id, DataTable)
            table.add_column("", width=1)  # mark column, empty until a row is marked
            table.add_columns(*columns)
        if self.config_path.exists():
            self.loaded(Config.load(self.config_path))
        else:
            self.push_screen(SetupScreen(self.config_path), self.created)

    def created(self, cfg: Config | None) -> None:
        if cfg:
            self.loaded(cfg)
            self.run_update(cfg.checkouts(), fetch_only=False)

    def loaded(self, cfg: Config) -> None:
        self.cfg = cfg
        select = self.query_one("#add-version", Select)
        select.set_options([(v, v) for v in cfg.versions])
        if cfg.versions:
            select.value = cfg.versions[0]
        self.load_status(remote=False)

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if not any(action in actions for actions in TABS.values()):
            return True
        tabs = self.screen.query("#tabs")
        return bool(tabs) and action in TABS.get(str(tabs.first(TabbedContent).active), ())

    def action_tab(self, tab: str) -> None:
        self.query_one("#tabs", TabbedContent).active = tab

    # Repositories

    @work(thread=True, exclusive=True, group="status")
    def load_status(self, remote: bool, checkouts: list | None = None) -> None:
        assert self.cfg  # noqa: S101
        rows = ops.statuses(checkouts or self.cfg.checkouts(), self.cfg.jobs, remote)
        self.call_from_thread(self.fill, rows, checkouts is None)

    def fill(self, rows: list[dict], replace: bool) -> None:
        if replace:
            self.rows = {}
        self.rows |= {row["path"]: row for row in rows}
        self.render_table()

    def cells(self, row: dict) -> tuple:
        result = self.results.get(row["path"])
        last = f"{result.action}: {result.reason}".rstrip(": ") if result else ""
        if not row["exists"]:
            return ("", f"{row['org']}/{row['repo']}", row["version"], "", "missing", "", "", last)
        state = "dirty" if row["dirty"] else "clean"
        if row.get("remote_changed"):
            state += ", remote moved"
        ahead = f"+{row['ahead']} -{row['behind']}" if row["ahead"] is not None else ""
        mark = "*" if row["path"] in self.marked else ""
        branch = row["branch"] or "(detached)"
        return (
            mark,
            f"{row['org']}/{row['repo']}",
            row["version"],
            branch,
            state,
            ahead,
            row["last_fetch"] or "",
            last,
        )

    def render_table(self) -> None:
        table = self.query_one("#table", DataTable)
        text = self.query_one("#filter", Input).value.lower()
        table.clear()
        for path, row in self.rows.items():
            cells = self.cells(row)
            if text in " ".join(map(str, cells)).lower():
                table.add_row(*cells, key=path)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "filter":
            self.render_table()
        elif event.input.id == "query":
            self.search()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "query":
            modules = self.query_one("#add-tabs", TabbedContent).active == "add-modules"
            self.query_one("#modules" if modules else "#repolist", DataTable).focus()
        elif event.input.id == "filter":
            self.query_one("#table", DataTable).focus()

    def cursor_key(self, table_id: str) -> str | None:
        table = self.query_one(table_id, DataTable)
        if not table.row_count:
            return None
        return table.coordinate_to_cell_key(Coordinate(table.cursor_row, 0)).row_key.value

    def selected(self) -> list:
        assert self.cfg  # noqa: S101
        names = self.marked or {self.cursor_key("#table")}
        return [c for c in self.cfg.checkouts() if c.name in names]

    def action_toggle_mark(self) -> None:
        tab = self.query_one("#tabs", TabbedContent).active
        if tab == "repos":
            marks, table_id = self.marked, "#table"
        else:
            marks = self.picked
            modules = self.query_one("#add-tabs", TabbedContent).active == "add-modules"
            table_id = "#modules" if modules else "#repolist"
        key = self.cursor_key(table_id)
        if key:
            marks.symmetric_difference_update({key})
            table = self.query_one(table_id, DataTable)
            table.update_cell_at(Coordinate(table.cursor_row, 0), "*" if key in marks else "")
        if tab == "add":
            self.preview()

    def action_update(self) -> None:
        self.run_update(self.selected(), fetch_only=False)

    def action_fetch(self) -> None:
        self.run_update(self.selected(), fetch_only=True)

    def action_refresh(self) -> None:
        self.load_status(remote=True)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "table":
            result = self.results.get(str(event.row_key.value))
            if result:
                self.notify(f"{result.checkout.name}: {result.action} {result.reason}", timeout=10)

    def action_remove(self) -> None:
        assert self.cfg  # noqa: S101
        key = self.cursor_key("#table")
        if key:
            org, repo, _ = key.split("/")
            self.cfg.remove_repo(org, repo)
            self.cfg.save()
            self.notify(f"removed {org}/{repo} from the config, clones stay on disk")
            self.loaded(Config.load(self.config_path))

    @work(thread=True, exclusive=True, group="git")
    def run_update(self, checkouts: list, fetch_only: bool) -> None:
        assert self.cfg  # noqa: S101
        cfg = self.cfg

        def on_result(result: ops.Result) -> None:
            self.call_from_thread(self.set_result, result)

        try:
            with ops.lock():
                results = ops.update(cfg, checkouts, cfg.jobs, fetch_only, ops.github_token(), on_result)
        except ops.Locked as e:
            self.call_from_thread(self.notify, f"{e}: the timer may be running", severity="warning")
            return
        self.call_from_thread(self.notify, ops.summary(results))
        self.call_from_thread(self.load_status, False, checkouts)

    def set_result(self, result: ops.Result) -> None:
        self.results[result.checkout.name] = result
        self.marked.discard(result.checkout.name)
        self.render_table()

    # Add

    def version(self) -> str | None:
        value = self.query_one("#add-version", Select).value
        return value if isinstance(value, str) else None

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "add-version":
            self.picked.clear()
            self.search()

    @work(thread=True, exclusive=True, group="search")
    def search(self) -> None:
        version = self.version()
        if not version:
            return
        query = self.query_one("#query", Input).value
        try:
            if version not in self.indexes:
                self.indexes[version] = index.load_index(version)
            modules = index.search(query, self.indexes[version]) if query else []
        except Error as e:
            modules = []
            self.call_from_thread(self.notify, str(e), severity="error")
        try:
            repos = [f"{o}/{r}" for o, r in index.repo_list(version) if query.lower() in f"{o}/{r}"]
        except Error:
            repos = []
        self.call_from_thread(self.fill_add, modules, repos)

    def fill_add(self, modules: list, repos: list[str]) -> None:
        table = self.query_one("#modules", DataTable)
        table.clear()
        for name, entry in modules:
            table.add_row("*" if name in self.picked else "", name, entry["repo"], entry.get("summary", ""), key=name)
        table = self.query_one("#repolist", DataTable)
        table.clear()
        for repo in repos:
            table.add_row("*" if repo in self.picked else "", repo, key=repo)

    def preview(self) -> None:
        """Repositories the picked modules pull in, and why, before anything is written."""
        version = self.version()
        modules = [p for p in self.picked if "/" not in p]
        repos, problems = index.resolve(modules, self.indexes.get(version or "", {}))
        for repo in (p for p in self.picked if "/" in p):
            repos.setdefault(repo, [])
        assert self.cfg  # noqa: S101
        present = {f"{o}/{r}" for o, r in self.cfg.repos()}
        lines = [
            f"{r}: {', '.join(by) or 'requested'}" + (" (in config)" if r in present else "") for r, by in repos.items()
        ]
        self.query_one("#preview", Static).update(
            "\n".join(lines + problems) or "Mark modules or repositories with space"
        )

    def action_add(self) -> None:
        assert self.cfg  # noqa: S101
        if not self.picked:
            return
        version = self.version()
        try:
            added, messages, problems = index.add_targets(self.cfg, sorted(self.picked), [version] if version else None)
        except Error as e:
            self.notify(str(e), severity="error")
            return
        self.cfg.save()
        self.notify("\n".join(messages + problems), severity="error" if problems else "information")
        self.picked.clear()
        self.preview()
        self.loaded(Config.load(self.config_path))
        assert self.cfg  # noqa: S101
        self.run_update([c for c in self.cfg.checkouts() if (c.org, c.repo) in added], fetch_only=False)

    # Timer

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        self.refresh_bindings()
        if event.pane.id == "add":
            self.query_one("#query", Input).focus()
        elif event.pane.id == "repos":
            self.query_one("#table", DataTable).focus()
        elif event.pane.id == "timer":
            self.load_timer()

    @work(thread=True, exclusive=True, group="timer")
    def load_timer(self) -> None:
        try:
            info = timer.status()
            text = "\n".join(f"{k}: {v}" for k, v in info.items())
            if isinstance(info.get("every"), str) and info["every"] in timer.EVERY:
                self.every = info["every"]
        except Error as e:
            text = str(e)
        self.call_from_thread(self.query_one("#timer-info", Static).update, text)

    @work(thread=True, group="timer")
    def timer_call(self, enable: bool) -> None:
        try:
            notes = timer.enable(self.config_path, self.every) if enable else (timer.disable() or ["timer disabled"])
            self.call_from_thread(self.notify, "\n".join(notes))
        except Error as e:
            self.call_from_thread(self.notify, str(e), severity="error")
        self.call_from_thread(self.load_timer)

    def action_timer_enable(self) -> None:
        self.timer_call(True)

    def action_timer_disable(self) -> None:
        self.timer_call(False)

    def action_timer_every(self) -> None:
        keys = list(timer.EVERY)
        self.every = keys[(keys.index(self.every) + 1) % len(keys)]
        self.notify(f"frequency: {self.every} ({timer.EVERY[self.every]}), press e to apply")
