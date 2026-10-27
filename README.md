# Nightty — native Surface prototype

A keyboard-first graphical workspace for people who live in Midnight Commander
and command-line tools. This is a new independent Surface component, with the
Aethera workspace vision as orientation and Second Brain as an optional agent
backend. The existing browser AgentConsole remains independent.

## First slice

- Each top-bar tab owns a persistent shell and starts in Shell view. Keyboard
  shortcuts switch that tab to MC-style files, Gallery, or Project; switching
  tabs restores its view and browser folders. Each tab shows its current folder,
  truncated in the header with the full path in a tooltip. File panes are
  created on first use, and Gallery controls and thumbnails are created only
  when requested. Shell tabs do not scan folders for unopened browser views.
  Ctrl+PageUp / Ctrl+PageDown cycle tabs. The compact ⇄ button opens background
  transfers. Exiting a shell closes its tab.
- Files mode: two independent directory panes in a compact Midnight Commander
  style, each with two filename columns and no permanent sidebar. The panes
  share the width evenly by default; dragging the divider keeps the chosen
  proportion when the window is resized. Entries fill
  the left column to the bottom of the pane, then continue at the top of the
  right column; Down and Up follow that order, while Left and Right continue
  through columns on later screenfuls. At the outer edges, Left goes to the first
  entry and Right to the last. Scrolling keeps complete rows aligned and shifts
  the visible range so the last screen fills both columns when enough files exist.
  File rows use a fixed height based on the configured font, including for names
  rendered with Japanese or other fallback fonts.
  A double line
  separates panes and a single line separates filename columns. Clicking blank
  pane space focuses
  the nearest entry. Each pane's
  small locations menu contains favourites and recent folders. Ctrl+5 turns
  the right pane into a large preview of the selected file on the left; the
  right folder remains the copy destination. A fixed slot before each name shows
  `/` for directories, `@` for symbolic links, `*` for executables, or a space.
  Going to a parent selects the
  directory just left. Large folders render their first screen before later rows;
  slow directory scans show partial results while the rest arrive.
  Quick navigation and explicit background copying remain available without
  replacing existing destinations.
- Ctrl+I switches the active pane between Files and Gallery; the other pane
  keeps its own view. Tab changes the active pane without changing either view.
  Compact horizontal cards fill the left column before the right and show a
  thumbnail, text excerpt or file type
  icon, plus filename, type, size and image dimensions or modification time. The
  parent directory appears as a large `..` instead of a folder card. Select
  several items and use F5 to copy them to the other pane. F3 expands one file
  with type, size, modified time, permissions, location and image dimensions; Escape
  returns from that single-file preview to the gallery or file list. Left and
  Right follow the same sliding columns and screenfuls as the file list. Gallery
  scrolling lands on complete card rows, and the pane hides any partial row below
  the last complete card. Resizing recalculates each column's height while
  keeping the selected card.
- Project mode: the selected folder's inherited context, readable documents,
  images and linked work, using the full window.
- A real embedded VTE terminal in every tab. Files and Gallery share an MC-style
  command line: typing from either browser pane enters it. Enter runs a command
  in that tab's shell from the active pane's folder, and a simple `cd` changes
  that pane's folder. Commands show the shell while they run. At a shell prompt,
  Ctrl+O switches between shell and the tab's previous view. While another program
  owns the terminal, Ctrl+O goes to that program, including through SSH.
  Browser navigation itself never injects commands into a running shell or
  agent. Typing `exit` in the final shell tab quits Nightty. Hiding the window
  preserves terminal processes. Ctrl+click opens
  terminal URLs and simple file paths with the desktop's default application.
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

Uses the system Python and installed PyGObject, GTK 4, VTE 3.91 and GdkPixbuf.
The VTE 3.91 introspection namespace is the GTK 4 binding. No Python environment
or dependency download is required on the development host. BIV also uses GTK 4;
its viewing logic and service interfaces are future reuse candidates, not
embedded by this slice.
PDF previews use `pdftocairo` when it is installed; other file views continue
to work without it.

The Files pane still uses `Gtk.IconView`, and agent selectors use
`Gtk.ComboBoxText`. GTK 4 supports these widgets but deprecated them in 4.10;
replacing them is follow-up work for GTK 5 readiness.

```sh
./run.sh --left /path/to/work --right /path/to/another/folder
./run.sh /path/to/work
```

`./run.sh` starts Nightty maximized by passing `--maximized`; direct
`python3 -m workspace` launches at the default size unless given that option.

Colors, font sizes and file name weight live in [appearance.json](appearance.json).
The terminal shares the file list's family, pixel size and weight. The default
font is DejaVu Sans Mono at 26 pixels, regular weight, close to the font resolved
from the operator's Kitty `monospace` setting at 20 points on a 96 DPI display.
The palette matches the operator's MC skin through Kitty's Solarized colors:
directories use Kitty's normal white (`#eee8d5`), unknown files are gray, and
recognized types get distinct colors. The embedded terminal uses the same
16-color palette, with separate normal text and background colors matching
Kitty. Edit the JSON and restart Nightty, or pass
`--appearance /path/to/appearance.json` to try another palette.

State (favourites, recent folders and last directories) lives under
`~/.local/state/surface-workspace/`, or the explicit `--state` directory. Project
files never receive private UI state. Launches begin in Shell view; views and
folder positions stay with their tabs while the app is running. An optional
global shortcut can run the installed launcher to show/hide the existing window.
The existing state location is retained so settings survive the rename.

Install after validation with `python3 install.py --shortcut F12`. It adds a
`nightty` command, application entry and an optional global GNOME shortcut,
preserving other custom shortcuts and refusing an existing launcher from another
application. The launcher uses `assets/nightty-icon-constellation.png`; the
previous icon remains at `assets/nightty-icon.png`. Running `nightty .`
opens the caller's directory. F12 show/hide
is not an anchored Wayland drop-down; the window remains compositor-managed.
The application is single-instance: `./run.sh` presents the existing window,
while the installed `nightty` shortcut toggles it. The running process keeps
the code loaded when it started. After changing source, quit with Ctrl+Q before
running `./run.sh` again; F12 only hides the process.

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

The tab's MC/Project view change is explicit, and preserves both directory positions.
Project mode enhances the same files rather than replacing navigation with a
separate data store. Full task/pocket permissions and inheritance remain future
Second Brain work.

## Copying

In Files, Insert or Ctrl+T marks a file and advances the cursor; Shift+Up/Down
marks the current file and moves in that direction. Right-click toggles a mark
without moving. A line closes each file list. Each pane's two-row footer shows one
selected file's details or the selected count and size, then that pane's folder,
entry count and available filesystem space. The command line below uses a
`user@host:path$` prompt. F5
copies marked entries, or the current entry when nothing is marked. Gallery
uses its own selection. Copies run in a background worker. They
preserve symbolic links and refuse existing destinations. Each regular file is
written to a temporary sibling and published without overwriting another name.
Directory transfers can leave already completed entries on cancellation/failure;
the transfer panel reports that state. Cancellation is cooperative. Transfer
progress is desktop operation state, separate from project tasks. No delete or
move operation is provided in this first prototype; MC is available in a terminal.

## Controls

- Ctrl+PageUp / Ctrl+PageDown: move between workspace tabs; Ctrl+Shift+T: new
  shell tab. All tabs have the same shell controls.
- Ctrl+O: switch the current tab between its shell and previous view at the
  shell prompt. During a foreground program, it goes to that program instead;
  Ctrl+Shift+O always sends a literal Ctrl+O to the terminal.
- Ctrl+Enter or Shift+Enter sends a newline (the same byte as Ctrl+J) to the
  terminal, including Codex's composer. In Codex, Ctrl+V requests an image
  from the local clipboard.
  Ctrl+Shift+V, Shift+Insert, or the terminal's Paste menu pastes text, or
  sends Ctrl+V to a foreground program when the clipboard contains an image.
  Image attachment depends on that program's clipboard support and access to
  the local clipboard. Ctrl+click opens terminal links and simple file paths.
- In native views, Ctrl+P opens Project (press again for Shell); Ctrl+I
  switches Gallery and Files. Terminal programs keep their own Ctrl+P and Ctrl+I.
- Ctrl+1 / Ctrl+2 / Ctrl+3 / Ctrl+4 select Files / Project / Shell / Gallery
  from native views or an idle shell. Foreground terminal programs receive
  their own keys.
- Escape closes an expanded single-file preview or inline search; it leaves
  Gallery, Files and Project open. F12 hides the window.
- Type in a Files or Gallery pane to use the command line; Enter runs the command
  and opens its output, while `cd`, `cd ..`, `cd -`, and `cd /path` change the
  active pane. Backspace removes the last command character while focus is on a
  browser pane.
  Up / Down in the command line recall commands; Escape returns to the pane.
- Ctrl+5: toggle a large quick view in the right pane while browsing the left
- Alt+1 / Alt+2: locations menu for the left / right pane (favourites and
  recent folders); Alt+F1 / Alt+F2 also work if the desktop passes them through
  (GNOME normally reserves Alt+F2 for Run Command)
- Alt+S or the small magnifier beside a path: inline filename search in the
  active pane; Escape closes it. The GTK floating type-ahead box is disabled.
- Tab / Shift+Tab: switch browser panes and keep keyboard focus in the selected
  pane; Enter: enter a directory or open a file with GNOME's
  default application; F3: embedded preview; Home / End: first / last item
- Insert / Ctrl+T: mark or unmark the cursor file and move down;
  Shift+Up / Shift+Down: mark or unmark and move that way; right-click: toggle
  a mark without moving. F5 copies marked files, or the cursor file.
- Alt+Up: parent; Ctrl+L: edit the active path; Ctrl+H: hidden files
- Ctrl+D: star or unstar the active directory
- Locations menu beside either path: choose a favourite or recent folder, or
  star/unstar that pane's current folder
- F3: inspect selected file; F4: open with the default application; F5:
  background copy in Files or Gallery (the confirmation defaults to Yes)
- ⇄ in the top bar: transfer progress and cancellation
- Escape: leave Inspect or inline search; F12: hide; Ctrl+Q: explicit quit

## Verification

`python3 -m unittest discover -s tests -v` verifies metadata inheritance,
navigation state, copy cancellation/no-clobber, symlink preservation and the
loopback-only agent client. `./run.sh --smoke --state /tmp/workspace-smoke-state`
uses disposable files and fake agents, exercises native navigation, preview,
copying and terminal output, and saves screenshots under `/tmp/`.

## License

Nightty's code, documentation, configuration and bundled artwork are licensed
under the Mozilla Public License, version 2.0. See [LICENSE](LICENSE).
