# Configuration

`odoo-repos` reads one TOML file, `~/code/odoo-repos.toml` by default. Point to
another one with `-c PATH` on any command (`ors -c ~/work/odoo-repos.toml update`).

The folder that holds the file is the **code root**: every checkout lives below it at
`<org>/<repo>/<version>`.

The format is the same as `tlc`, so an existing `tlc` config works as is.

`ors init`, `ors add` and `ors remove` write this file for you. You can also edit it
by hand. Those commands keep your comments and only rewrite the `[repos]` entries
they change.

## Start from the example

`odoo-repos.example.toml` at the root of this repository is a commented starting point.

```bash
mkdir -p ~/code
# from a clone of this repository
test -e ~/code/odoo-repos.toml || cp odoo-repos.example.toml ~/code/odoo-repos.toml
# or straight from GitHub
test -e ~/code/odoo-repos.toml || curl -fsSL -o ~/code/odoo-repos.toml \
  https://raw.githubusercontent.com/trobz/odoo-repos/main/odoo-repos.example.toml
ors list     # check the result, then: ors clone
```

Both commands keep a config that is already there.

## Full example

```toml
# Versions cloned for every repository that does not list its own.
versions = ["18.0", "17.0"]

# Parallel git processes. `-j N` on the command line wins.
jobs = 16

# Repositories cloned as one shared base plus one worktree per version.
worktree = ["odoo/odoo", "odoo/enterprise"]

[repos]
odoo = ["odoo", "enterprise"]
oca = [
  "web",                        # every version above
  "sale-workflow",
  ["server-ux", ["18.0"]],      # only 18.0
  ["queue", ["16.0", "17.0"]],  # versions outside `versions` are allowed
]

[transport]
default = "https"
"odoo/enterprise" = "ssh"

[remotes]
"odoo/odoo" = { ocb = "https://github.com/oca/ocb.git" }
```

With this file the code root holds, among others:

```text
~/code/odoo/odoo/.bare        shared base for odoo/odoo
~/code/odoo/odoo/18.0         worktree
~/code/odoo/odoo/17.0         worktree
~/code/oca/web/18.0
~/code/oca/web/17.0
~/code/oca/server-ux/18.0
~/code/oca/queue/16.0
~/code/oca/queue/17.0
```

## Keys

### `versions` (required)

A list of Odoo versions, each written `MAJOR.MINOR` (`"18.0"`) or `"master"`.

A repository written as a plain name in `[repos]` is cloned for each of these
versions. The version is also the branch name on GitHub.

### `jobs`

Number of git processes run in parallel by `clone`, `update` and `status`.
The default is `16`, and `-j N` overrides it for one run.

Use `32` or more for large configs such as the `oca-contributor` preset. Use less
on a slow link.

### `worktree`

A list of `org/repo` names. Each repository listed here is cloned once:
- as a bare, blobless base at `<org>/<repo>/.bare` (`git clone --bare --filter=blob:none`);
- then each version is a `git worktree add` from that base.

This saves both disk space and download time: the history is downloaded once, and
the blobs of a version are fetched only when that version is checked out. Use it for
large repositories that you need in several versions, typically `odoo/odoo` and
`odoo/enterprise`.

Every other repository is a shallow clone of its version branch
(`git clone --depth 1 -b <version>`).

The setting applies when a checkout is created. An existing shallow clone is not
converted. To switch a repository to worktree mode:
1. move its checkouts away;
2. add the repository to `worktree`;
3. run `ors clone` again.

### `[repos]` (required)

One key per GitHub organisation. Each value is a list of entries, and an entry is one
of:

- `"name"`: the repository, for every version in `versions`.
- `["name", ["17.0", "18.0"]]`: the repository, only for the listed versions.

Organisation names are case-insensitive and stored lowercase on disk: `OCA` and
`oca` both give `~/code/oca/...`. Repository names keep their case.

An organisation with an empty list (`odoo = []`) is valid and clones nothing.
`ors remove` leaves such a line behind so that the comments on it are kept.

### `[transport]`

How each repository is cloned:

| Value | URL used |
|---|---|
| `"https"` | `https://github.com/<org>/<repo>.git` |
| `"ssh"` | `git@github.com:<org>/<repo>.git` |

- `default` applies to every repository.
- An `"org/repo"` key overrides `default` for that repository.

Use `ssh` for private repositories such as `odoo/enterprise`, where you already
have a key on GitHub.

The transport is read when a checkout is created. To change it for an existing
checkout, use `git remote set-url origin ...`.

### `[remotes]`

Extra git remotes added to a repository next to `origin`:

```toml
[remotes]
"odoo/odoo" = { ocb = "https://github.com/oca/ocb.git" }
"oca/web" = { mine = "git@github.com:me/web.git" }
```

The remote is added at clone time and on every `update`, so new entries also reach
existing checkouts. `update` fetches only the branches named in `versions` from
these remotes, and never merges them. Each remote must have every one of those
branches, or the fetch for that repository fails.

## Defaults written by `ors init`

```toml
versions = ["18.0"]
jobs = 16
worktree = ["odoo/odoo", "odoo/enterprise"]

[repos]
odoo = ["odoo"]   # the odoo-minimal preset; oca-contributor lists every OCA repository

[transport]
default = "https"
"odoo/enterprise" = "ssh"
```

## Related environment variables

| Variable | Effect |
|---|---|
| `GITHUB_TOKEN` | Token used by `update` and `status --remote` to ask GitHub which branches moved. If unset, `gh auth token` is tried. Without a token, every repository is fetched. |
| `ODOO_REPOS_INDEX_URL` | Module index used by `ors add <module>` and `ors search`. It is a URL template with `{version}`, and `file://` works. |
| `XDG_CACHE_HOME` | Where the module index and repository lists are cached (`odoo-repos/` inside it). |
| `XDG_RUNTIME_DIR` | Where the lock file `odoo-repos.lock` lives. One run at a time per user. |

## Validation

The config is checked every time it is loaded. On an error the command prints the
problem and exits with code 2, without touching any checkout. A config is rejected
when:
- a version is not `MAJOR.MINOR` or `master`;
- an organisation or repository name has characters outside `a-z A-Z 0-9 . _ -`;
- a transport is neither `https` nor `ssh`;
- the file is not valid TOML.

`-f NAME` must name a repository that is in the config. Otherwise the command fails
with `not in config: NAME` and exit code 2.
