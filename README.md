# Workspace — native Surface prototype

A keyboard-first graphical workspace for people who live in Midnight Commander
and command-line tools. This is a new independent Surface component, with the
Aethera workspace vision as orientation and Second Brain as an optional agent
backend. The existing browser AgentConsole remains independent.

## First slice

- Files mode: two independent directory panes, favourites, quick navigation and
  explicit background copying without replacing existing destinations.
- Project mode: the selected folder's inherited context, readable documents,
  images and linked work, using the full window.
- A real embedded VTE terminal with multiple tabs. Each tab starts in its chosen
  folder; changing directory in the browser never injects commands into a running
  shell or agent. Hiding the window preserves terminal processes.
- Native access to existing Second Brain agent profiles and sessions. Only an
  explicit Send starts inference. No folder text is silently sent to an agent.
- One application instance, with repeat invocation toggling visibility for a
  desktop shortcut. On Wayland the compositor controls placement; this initial
  window does not claim a shell-level anchored drop-down animation.

This is an operator desktop application. Its terminals and file operations have
the operator's host authority. Project discovery and metadata provide orientation,
not containment or new backend permissions. Existing native-agent grants remain
enforced by Second Brain. Metadata cannot supply executable commands or credentials.

## Run

Uses the system Python and installed PyGObject, GTK 3, VTE 2.91 and GdkPixbuf.
The installed VTE binding targets GTK 3; no Python environment or dependency
download is required on the development host. BIV uses GTK 4; its viewing logic
and service interfaces are future reuse candidates, not embedded by this slice.

```sh
./run.sh --left /path/to/work --right /path/to/another/folder
./run.sh /path/to/work
```

State (favourites, last directories and selected mode) lives under
`~/.local/state/surface-workspace/`, or the explicit `--state` directory. Project
files never receive private UI state. An optional global shortcut can run the
installed launcher to show/hide the existing window.

Install after validation with `python3 install.py --shortcut F12`. It adds a
`workspace` command, application entry and an optional global GNOME shortcut,
preserving other custom shortcuts and refusing an existing launcher from another
application. Running `workspace .` opens the caller's directory. F12 show/hide
is not an anchored Wayland drop-down; the window remains compositor-managed.

## Project recognition

Normal directories always work. Existing `README.md` files with `type: project`
frontmatter are recognized. An optional `.surface.json` supplies view metadata:

```json
{
  "type": "project",
  "title": "An article",
  "description": "What this work is for",
  "work": ["work/check-claim.md"],
  "agent": "japanese-cli"
}
```

The nearest marked ancestor is the current project. Nested projects appear in a
breadcrumb; descriptions, work references and the suggested agent are inherited
with their source location retained. Child metadata may replace the suggested
agent. Folder metadata only suggests a profile already exposed by the backend;
it cannot change scopes or mount the folder into that profile. Work references
open their original documents; this Surface creates no task database or lifecycle.

The Files/Project switch is explicit, and preserves both directory positions.
Project mode enhances the same files rather than replacing navigation with a
separate data store. Full task/pocket permissions and inheritance remain future
Second Brain work.

## Copying

F5 copies the selected entries to the other pane in a background worker. Copies
preserve symbolic links and refuse existing destinations. Each regular file is
written to a temporary sibling and published without overwriting another name.
Directory transfers can leave already completed entries on cancellation/failure;
the transfer panel reports that state. Cancellation is cooperative. Transfer
progress is desktop operation state, separate from project tasks. No delete or
move operation is provided in this first prototype; MC is available in a terminal.

## Controls

- Ctrl+1 / Ctrl+2: Files / Project
- Tab: switch file panes; Enter: enter a directory or preview a file
- Alt+Up: parent; Ctrl+L: edit the active path; Ctrl+H: hidden files
- Ctrl+D: favourite the active directory
- F3: preview; F4: open with the default application; F5: background copy
- Ctrl+grave: show/hide the terminal area; Ctrl+Shift+T: new terminal
- F12 or Escape outside the terminal: hide; Ctrl+Q: explicit quit

## Verification

`python3 -m unittest discover -s tests -v` verifies metadata inheritance,
navigation state, copy cancellation/no-clobber, symlink preservation and the
loopback-only agent client. `./run.sh --smoke --state /tmp/workspace-smoke-state`
uses disposable files and fake agents, exercises native navigation, preview,
copying and terminal output, and saves screenshots under `/tmp/`.
