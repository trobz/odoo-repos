# odoo-repos

Clone and keep Odoo/OCA repositories up to date, with worktree support, a TUI and a systemd timer.

Pick modules or repositories and a version; `odoo-repos` clones what they need into
`~/code/<org>/<repo>/<version>` (the layout module-tracker reads), keeps the clones
current, and never touches local work: anything that cannot fast-forward is skipped
and reported.

## Installation

```bash
uv tool install odoo-repos
```

Requires Python 3.10+, git 2.31+, and systemd for the timer.

The tool installs as `odoo-repos`, with the short name `ors`.

## Usage

```bash
odoo-repos init 18.0 --module web_responsive --module sale_order_type
odoo-repos add oca/server-ux -v 17.0   # a repository, or module names
odoo-repos update -j 32                # clone missing, fetch, fast-forward
odoo-repos update --fetch-only --json  # never touches working trees
odoo-repos status --remote             # branch, dirty, ahead/behind
odoo-repos timer enable --every weekdays
odoo-repos                             # TUI
```

Configuration lives in `~/code/odoo-repos.toml`.

Start from the example config, then edit it:

```bash
mkdir -p ~/code
# from a clone of this repository
test -e ~/code/odoo-repos.toml || cp odoo-repos.example.toml ~/code/odoo-repos.toml
# or straight from GitHub
test -e ~/code/odoo-repos.toml || curl -fsSL -o ~/code/odoo-repos.toml \
  https://raw.githubusercontent.com/trobz/odoo-repos/main/odoo-repos.example.toml
ors list     # check the result, then: ors clone
```

Both commands keep a config that is already there. `ors init VERSION` is the
other way to start: it writes a minimal config for you.

A fuller example:

```toml
versions = ["18.0", "17.0"]
jobs = 16
worktree = ["odoo/odoo", "odoo/enterprise"]  # bare blobless base + one worktree per version

[repos]
odoo = ["odoo", "enterprise"]
oca = ["web", "sale-workflow", ["server-ux", ["18.0"]]]

[transport]
default = "https"
"odoo/enterprise" = "ssh"

[remotes]
"odoo/odoo" = { ocb = "https://github.com/oca/ocb.git" }
```

Every key is described in [docs/configuration.md](docs/configuration.md).

Exit codes: `0` all good, `1` something skipped because of local work, `2` a clone or
fetch failed, the config is invalid, or a module is not in the index.

With a GitHub token (`GITHUB_TOKEN` or `gh auth login`), `update` asks GitHub which
branches moved and only fetches those.

Modules are resolved through the module index published at
`https://trobz.github.io/odoo-addons-repos/modules/<version>.json`
(override with `ODOO_REPOS_INDEX_URL`, a template with `{version}`).
