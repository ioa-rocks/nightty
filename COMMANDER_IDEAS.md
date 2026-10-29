# Commander ergonomics ideas

Working ideas for Nightty. No implementation decisions are locked in here.

## Three independent choices

- **Location:** what the left or right pane is browsing. A location can be a
  directory, an existing mount, or eventually a remote connection or archive.
- **Presentation:** how a pane shows its current location or selection: Files,
  Gallery, or a compact Info/Preview panel. Changing presentation should retain
  the pane's location and the other pane's copy destination.
- **Workspace:** Shell, Commander, or Project. Ctrl+O and Ctrl+P switch context;
  Ctrl+I switches the active pane between Files and Gallery. The location
  chooser should not take over those keys.

## Location chooser

- Alt+F1 opens the left location chooser; Alt+F2 opens the right one. Keep
  Alt+1/Alt+2 as reliable equivalents on desktops that reserve function keys.
- Put a typeable path or URI at the top, followed by Home, filesystem roots,
  mounted volumes, saved locations, favourites, and recent folders. Search the
  chooser by typing; Enter opens the highlighted location in the target pane.
- Show remote locations with their protocol and host, connection state, and
  whether they are writable. Keep **Connect to server** separate from opening
  an already mounted location. SFTP is the most useful first remote target;
  other protocols can follow the backends available on the host.
- Start by surfacing existing mounts. Direct URI browsing needs a location
  model beyond local `Path` objects so that listing and file operations have
  consistent behavior on remote filesystems.
- Let the same chooser work when the right pane is displaying Info/Preview:
  picking a location updates its saved folder and copy destination.

## Working locations without another tab row

- A top-level Nightty tab already owns a shell and both pane paths. Let users
  save a named left/right location pair and restore it in the current workspace
  tab. Show pairs in the existing location chooser as `source → destination`.
- Keep a short Back/Forward history for each pane. Recent folders can remain a
  broader list; history should return to the exact path just left.
- Offer **Open in other pane** for a selected folder, and allow a copy
  destination to be pinned with a visible path indicator. A pinned destination
  should not change through accidental pane navigation.
- If a saved path is unavailable, mark that in the chooser without probing
  remote filesystems while building the popup.
- If top-level tabs become crowded, offer a searchable list of open workspaces
  from the existing tab bar instead of adding another row of pane tabs.

## Pane header

Compact layout now in use:

`[n] [list] [☆ /current/path ⌕]`

- The sort character shows the key and direction: lowercase for ascending,
  uppercase for descending. Click it to choose name, extension, size, modified,
  or created time; repeating a sort reverses direction. Each pane retains its
  choice when switching views or tabs.
- The monochrome list/grid symbol opens Files, Gallery, and Info choices. The
  star floats inside the editable path, opens locations and favourites, and
  fills when the current folder is saved. The magnifier at the other end of
  the path opens Find Files. The controls stay flat until hovered or focused.
- Each popup points to the control that opened it. Keep the menu focused while
  moving through its choices; Left and Right move through Sort, View, and
  Locations/Favourites in that order, shifting the popup to each control.
- Keep Alt+S as keyboard-only quick goto within the current directory. The
  magnifier opens Find Files across a tree or index. A later quick goto action
  could mark all matching files without adding another pane-header button.

## Discoverability and keyboard path

- Show the current workspace mode in the top bar as a compact **Shell ▾**,
  **Commander ▾**, or **Project ▾** button. Its menu lists all three modes and
  their shortcuts. Terminal tabs remain separate from the mode selector.
- The pane header controls provide visible mouse access. A pane-controls
  shortcut such as Ctrl+F1 for left or Ctrl+F2 for right could focus the
  corresponding header with **Location** selected. Left/Right would move among
  Sort, Location, and View; Down or Enter would open the current menu; Escape
  would return focus to the file list. Alt+F1/Alt+F2 can continue to open
  Location directly when the desktop delivers those keys.
- Show shortcuts beside menu actions so the interface teaches them during
  ordinary use. Highlight the active mode, view, location, and sort choice.

## Info and preview

- Keep Ctrl+5 as the quick companion pane. Give it **Preview** and **Info**
  views for the selected item; F3 can remain the full viewer. The inactive
  pane's folder remains the copy destination while this companion is visible.
- Info for a file: full path, type, size, modified time, permissions, owner,
  symlink target when relevant, and filesystem or mount. For a folder, show
  its direct entry count, mount and free space; calculate recursive size only
  when requested, with progress and cancellation.
- When nothing is selected, show useful information about the active folder
  and its filesystem rather than an empty panel. Keep live updates tied to the
  active selection without moving keyboard focus away from the file list.

## Later workflow ideas

- Keep search results as reusable sets of real paths from multiple folders.
  Removing an item from a set must be distinct from deleting the real file.
- Add cancellable file content search while retaining fast filename search.
- Grow F3 into a viewer with text search, wrap, hex view, and next/previous
  file; grow F4 into an embedded text editor with save, undo, and change-on-disk
  warning.
- Add selection by extension or pattern, named panel filters, and a comparison
  of the two panes that marks missing or changed files.
- Make archives browsable as read-only locations first, then consider explicit
  update operations. A user command menu and searchable file history can come
  after the core pane workflow feels settled.
