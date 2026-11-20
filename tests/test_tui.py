import asyncio

from textual.widgets import DataTable, Input, TabbedContent

from odoo_repos import timer
from odoo_repos.config import Config
from odoo_repos.tui import OdooReposApp


async def settle(app, pilot):
    for _ in range(6):
        await app.workers.wait_for_complete()
        await pilot.pause()


def test_tui(github, code, module_index, monkeypatch):
    github.repo("odoo", "odoo")
    github.repo("oca", "web")
    github.repo("oca", "sale-workflow")
    module_index(
        "18.0",
        {
            "base": {"repo": "odoo/odoo"},
            "sale_order_type": {"repo": "oca/sale-workflow", "summary": "Sale order types", "all_depends": ["base"]},
        },
    )
    calls = []
    monkeypatch.setattr(timer, "enable", lambda config, every: calls.append(("enable", every)) or ["timer enabled"])
    monkeypatch.setattr(timer, "status", lambda: {"enabled": bool(calls)})
    monkeypatch.setattr("odoo_repos.index.repo_list", lambda version: [("oca", "web"), ("oca", "sale-workflow")])
    config = code / "odoo-repos.toml"

    async def scenario():
        app = OdooReposApp(config)
        async with app.run_test(size=(160, 50)) as pilot:
            # Setup screen: same choices as init.
            app.screen.query_one("#version", Input).value = "18.0"
            app.screen.query_one("#targets", Input).value = "oca/web"
            await pilot.click("#create")
            await settle(app, pilot)
            assert (code / "odoo/odoo/18.0/.git").exists()
            assert (code / "oca/web/18.0/.git").exists()
            table = app.query_one("#table", DataTable)
            assert table.row_count == 2

            # Update a row.
            github.commit("oca", "web", "18.0")
            table.focus()
            table.move_cursor(row=[str(table.get_row_at(i)[1]) for i in range(2)].index("oca/web"))
            await pilot.press("u")
            await settle(app, pilot)
            assert app.results["oca/web/18.0"].action == "updated"

            # Add a module from the index.
            app.query_one("#tabs", TabbedContent).active = "add"
            await pilot.pause()
            app.query_one("#query", Input).value = "sale"
            await settle(app, pilot)
            app.query_one("#modules", DataTable).focus()
            await pilot.press("space")
            assert "oca/sale-workflow: sale_order_type" in str(app.query_one("#preview").render())
            await pilot.press("a")
            await settle(app, pilot)
            assert ("oca", "sale-workflow") in Config.load(config).repos()
            assert (code / "oca/sale-workflow/18.0/.git").exists()

            # Timer toggle.
            app.query_one("#tabs", TabbedContent).active = "timer"
            await pilot.pause()
            await pilot.press("n", "e")
            await settle(app, pilot)
            assert calls == [("enable", "daily")]

    asyncio.run(scenario())
