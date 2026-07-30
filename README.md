# cc-blurb

A [Herdr](https://herdr.dev) plugin that mirrors an agent pane's terminal title
into its Herdr pane label.

Claude Code, and most coding agents, report their current activity through the
OSC 0/2 window title — "Refactoring the auth module", "Running tests". Herdr's
session navigator (`prefix+g`) renders pane *labels*, not terminal titles, so
that activity does not appear there. This plugin copies one into the other.

## Install

```shell
herdr plugin install eighteyes/herdr-cc-blurb
```

The daemon starts automatically whenever the Herdr server restores a session.
After a fresh install on a running server, start it once by hand:

```shell
herdr plugin action invoke eighteyes.cc-blurb.start
```

For local development, `herdr plugin link /path/to/herdr-cc-blurb` instead.

## Showing the label in the sidebar

The session navigator picks up pane labels with no further setup. The agent
sidebar does not: its rows are templated, and the default template does not
include a label row. Add one in `~/.config/herdr/config.toml`, where the `pane`
token renders the pane label:

```toml
[ui.sidebar.agents.rows_by_agent]
claude = [["state_icon", "pane"]]
```

```shell
herdr server reload-config
```

`{cwd_hint}` renders the pane's folder only when it differs from the workspace
name, so a single row stays readable without repeating what the space row above
it already says. Use `{cwd_base}` instead to prefix the folder unconditionally.

The sidebar also offers a `terminal_title_stripped` token that shows the raw
title directly, without this plugin. Use that instead if the navigator does not
matter to you and no template is wanted — it is the simpler option for the
sidebar alone.

## What it does

A `[[startup]]` hook launches a daemon that holds one `pane.updated`
subscription. For each pane whose detected agent matches the configured list,
it renames the pane to the stripped terminal title, coalescing rapid title
changes over a short debounce window.

Two behaviours are worth knowing before installing:

- **Labels set by hand are not overwritten.** If a pane carries a label the
  plugin did not write, it is left alone. Set `claim_foreign_labels = true` to
  change this.
- **The last meaningful title is retained.** Agents reset their title to a
  generic string such as `Claude Code` when idle. Rather than reverting the
  label, the plugin keeps the previous one. Set `retain_on_ignore = false` to
  clear the label on idle instead.

## Named sessions

Each named Herdr session (`herdr --session <name>`) runs its own server on its
own socket, and one daemon serves one server. Daemon state — pidfile, owned
labels, log — is therefore keyed per socket under `instances/<server>` in the
plugin state directory, because pane ids such as `w1:p1` repeat across servers.

The `[[startup]]` hook fires per server, so each session starts its own daemon
on restore. To start one in a session that is already running:

```shell
HERDR_SOCKET_PATH=~/.config/herdr/sessions/<name>/herdr.sock \
  herdr plugin action invoke eighteyes.cc-blurb.start
```

The same prefix works for `status`, `resync`, and `clear` when inspecting a
session other than the current one.

## Configuration

Optional. Copy `config.example.toml` to `config.toml` in the directory printed
by:

```shell
herdr plugin config-dir eighteyes.cc-blurb
```

Every key is optional; omitted keys keep their default.

| key | default | effect |
| --- | --- | --- |
| `agents` | `["claude"]` | Agent ids to label. Empty list means all detected agents. |
| `ignore_titles` | `["Claude Code", "Codex", "opencode"]` | Exact titles treated as carrying no information. |
| `ignore_patterns` | shell-prompt regexes | Titles matching these are ignored. |
| `retain_on_ignore` | `true` | Keep the last meaningful label instead of clearing. |
| `claim_foreign_labels` | `false` | Adopt panes already carrying a label. |
| `clear_on_agent_exit` | `false` | Drop the label when the agent exits. |
| `template` | `"{cwd_hint}{title}"` | Label template. Tokens: `{title}` `{agent}` `{status}` `{cwd_base}` `{cwd_hint}`. |
| `cwd_hint_separator` | `" · "` | Appended to `{cwd_hint}` when it is non-empty. |
| `max_length` | `64` | Truncate longer labels. `0` disables. |
| `debounce_seconds` | `0.35` | Coalesce rapid title changes. |
| `reconnect_attempts` | `5` | Socket reconnects before the daemon exits. |

## Actions

Invoke with `herdr plugin action invoke eighteyes.cc-blurb.<id>`.

| action | effect |
| --- | --- |
| `start` | Start the daemon if it is not already running. |
| `stop` | Stop the daemon. Labels already applied remain. |
| `status` | Print daemon state, resolved paths, and owned labels. |
| `resync` | Relabel every current pane once, without the daemon. |
| `clear` | Remove every label this plugin set. |

The same subcommands are available directly:

```shell
python3 bin/blurb-daemon.py status
```

## Requirements

Herdr 0.7.0 or newer, and Python 3.11 or newer. No third-party packages.
Linux and macOS; the daemon uses `fork` and Unix domain sockets.

## Notes on Herdr behaviour

Two undocumented behaviours of the Herdr socket API are load-bearing here, and
would need rechecking after a Herdr upgrade:

- The socket API serves one request per connection and then closes it. Only
  `events.subscribe` connections stay open, so each command opens a fresh
  socket.
- `pane.rename` does not emit `pane.updated` and does not bump the pane
  revision, so the daemon's own writes cannot feed back into its subscription.
  If this changes, the daemon could loop.
