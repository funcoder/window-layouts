# Window Layouts

An Omarchy shell plugin that saves every open window, on every workspace, as a
named template and restores it in one click.

- **Save**: open the bar panel, type a name (e.g. `Client work`) and press
  Enter. The plugin records each window's app, how to relaunch it, its
  workspace and monitor, its tiling position, and whether it's floating,
  fullscreen or pinned. For a terminal it also records what was running in it,
  so the window you left `claude` in comes back running `claude`.
- **Restore**: click a template. Windows that are already open are moved back
  into place. Missing apps are launched. Windows that aren't in the template
  are closed, and each workspace's tiling is rebuilt to match.
- **After a reboot**: a `Last session` snapshot is refreshed every few minutes,
  so after logging in you can click it to get everything back. The snapshot
  from the login before that is kept as `Previous session`.
- **At login**: set `startupTemplate` to restore a template automatically once
  per login.

The panel follows the active Omarchy theme: it uses the shell's own panel, row,
button and confirm-dialog components, fonts and colors.

## Requirements

- [Omarchy](https://omarchy.org) 4 (the Quickshell-based shell with plugins) on
  Hyprland. Python 3 is included with Omarchy. There are no other dependencies.

## Install

```sh
omarchy plugin add https://github.com/funcoder/window-layouts.git
omarchy plugin enable funcoder.window-layouts left
```

Update with `omarchy plugin update funcoder.window-layouts`.

## Remove

```sh
omarchy plugin remove funcoder.window-layouts
rm -rf ~/.config/omarchy/window-layouts ~/.local/state/omarchy/window-layouts   # optional: saved templates and snapshots
```

Also delete any key bindings you added for it from `~/.config/hypr/bindings.lua`.

## Settings

Edit them from the bar settings panel, or inline on the widget's entry in
`~/.config/omarchy/shell.json`:

| Key | Default | Meaning |
| --- | --- | --- |
| `closeOthers` | `true` | Close windows that aren't part of the template |
| `launchTimeout` | `45` | Seconds to wait for launched apps to open a window |
| `autosave` | `true` | Keep the `Last session` snapshot up to date |
| `autosaveMinutes` | `5` | Snapshot interval |
| `startupTemplate` | `""` | Template to restore automatically at login |

## Keybindings and scripting

The panel exposes IPC methods, and the helper works on its own:

```bash
omarchy-shell funcoder.window-layouts toggle
omarchy-shell funcoder.window-layouts apply "Client work"

~/.config/omarchy/plugins/funcoder.window-layouts/layouts.py list
~/.config/omarchy/plugins/funcoder.window-layouts/layouts.py save "Client work"
~/.config/omarchy/plugins/funcoder.window-layouts/layouts.py apply "Client work" --keep-others
~/.config/omarchy/plugins/funcoder.window-layouts/layouts.py apply "Mail" --workspaces 3
```

Example binding in `~/.config/hypr/bindings.lua`:

```lua
o.bind("SUPER + ALT + 1", "Restore client work",
  "~/.config/omarchy/plugins/funcoder.window-layouts/layouts.py apply 'Client work' --notify")
```

## Templates

Templates are plain JSON in `~/.config/omarchy/window-layouts/`. Each window
has a `launch` command (an argv array). If an app is relaunched the wrong way,
edit that array. For example, point it at a `.desktop` id such as
`["spotify.desktop"]`.

A terminal's `launch` ends with the command that was running in it. Delete that
tail to have the window come back at a plain prompt, or edit it to run
something else. Only a command that has been running for at least 20 seconds is
recorded, so a snapshot doesn't capture whatever was typed a moment earlier;
`save`/`autosave` take `--no-commands` to skip recording them altogether.

A command line can carry a password or an API token as an argument, and a
template outlives the window it came from. Any command that looks like it
carries a credential — a `--password`/`--token` style option, a URL with a
token or embedded login, or an argument shaped like a known key — is not
recorded at all: that window comes back at a plain prompt, or from its
`.desktop` entry, instead. Templates are kept as `0600` files in a `0700`
directory, and they are read back with their size, shape and lengths checked
before a restore acts on them.

How launch commands are worked out:

| Window | Relaunched with |
| --- | --- |
| Omarchy web apps (`chrome-<site>-Default`) | `omarchy-launch-webapp <url>` (URL taken from the matching `.desktop` file when there is one) |
| Web apps with their own profile (`--user-data-dir`) | `omarchy-launch-webapp <url> --user-data-dir=… --class=…` |
| Browser windows | `<browser> --new-window` |
| foot / alacritty / kitty / ghostty | same command, with the shell's current directory, plus whatever was running in the window: `foot -D <dir> bash -c 'claude; exec bash'` |
| Flatpak / AppImage | `flatpak run <id>` / the AppImage path |
| Everything else | the process command line, falling back to its `.desktop` entry |

## Limitations

- Tiling is rebuilt for the dwindle layout. On other layouts, windows still
  land on the right workspace but the arrangement may differ.
- Tabbed groups come back as separate tiles.
- Apps that restore their own windows (e.g. a browser reopening a session) can
  open extra windows. Those are left alone.
- A window that asks before closing (unsaved work) stays open.

## Development

`./deploy-local.sh` copies a checkout into `~/.config/omarchy/plugins` and
validates it. Re-run it after editing. Changes to `layouts.py` apply
immediately. The shell's hot-reload can keep a stale compiled `Panel.qml`, so
after changing the QML run `omarchy restart shell`.
