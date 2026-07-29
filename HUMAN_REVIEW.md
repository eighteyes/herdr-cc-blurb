# HUMAN_REVIEW

## cc-blurb Herdr plugin — 2026-07-29

Session: `692bbd77-5c7c-494a-ba77-138ab07c3376`
Commit: `9d16a15` on `main`

Mirrors an agent pane's OSC 0/2 terminal title into its Herdr pane label, so the
title appears in the session navigator (`prefix+g`), which renders pane labels
rather than terminal titles.

### Files

    herdr-plugin.toml        manifest: startup hook + five actions
    bin/blurb-daemon.py      subscription daemon and CLI subcommands
    config.example.toml      documented defaults, copy to the plugin config dir

### Verification

- [ ] Plugin is linked and enabled

```shell
herdr plugin list
```

- [ ] Daemon is running and owns at least the current Claude pane

```shell
python3 /Users/god/projects/ai-jank/herdr-cc-blurb/bin/blurb-daemon.py status
```

- [ ] Pane label matches the live terminal title for every Claude pane

```shell
herdr pane list | jq -r '.result.panes[] | select(.agent=="claude") | [.pane_id, .label, .terminal_title_stripped] | @tsv'
```

- [ ] Label tracks a title change. Run this, wait for the agent's title to change
      as it starts new work, then run it again and confirm the label moved.

```shell
herdr pane list | jq -r '.result.panes[] | select(.agent=="claude") | "\(.pane_id)\tlabel=\(.label)\ttitle=\(.terminal_title_stripped)"'
```

- [ ] Session navigator shows the title. Press `prefix+g` and confirm the Claude
      pane row reads the agent's current blurb.

- [ ] Manual labels are not clobbered. Set one by hand, wait for a title change,
      confirm the manual label survives, then release it.

```shell
herdr pane rename w1:p1 "manual label"
```

```shell
python3 /Users/god/projects/ai-jank/herdr-cc-blurb/bin/blurb-daemon.py resync
```

```shell
herdr pane get w1:p1 | jq -r '.result.pane.label'
```

- [ ] Teardown restores default pane names

```shell
python3 /Users/god/projects/ai-jank/herdr-cc-blurb/bin/blurb-daemon.py clear
```

```shell
python3 /Users/god/projects/ai-jank/herdr-cc-blurb/bin/blurb-daemon.py stop
```

### Notes and open risks

- The Herdr socket API serves one request per connection and closes it. Only
  `events.subscribe` connections stay open. Commands must therefore open a fresh
  socket per call; multiplexing raises `BrokenPipeError` on the second request.
- `pane.rename` does not emit `pane.updated` and does not bump the pane
  `revision`, so label writes cannot feed the daemon's own subscription. This is
  observed behaviour, not documented, and would need rechecking after a Herdr
  upgrade — if it changes, the daemon can loop.
- Valid `[[events]] on = "..."` names are not documented. The plugin avoids them
  by holding its own subscription from a `[[startup]]` process.
- `[[startup]]` runs after the server restores a session. After `plugin link` or
  `plugin enable` with the server already up, start the daemon explicitly with
  `herdr plugin action invoke eighteyes.cc-blurb.start`.
- The daemon exits after `reconnect_attempts` consecutive socket failures rather
  than retrying indefinitely, on the assumption that a vanished socket means the
  server is gone and a new server will re-run the startup hook.
- Labels persist in `~/.local/state/herdr/plugins/eighteyes.cc-blurb/owned.json`.
  Deleting that file makes the daemon treat existing labels as human-set and
  stop updating them until `claim_foreign_labels` is enabled or labels cleared.
