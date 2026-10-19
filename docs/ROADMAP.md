# DragShift roadmap: closing the gap with Tangerine

This fork's goal: make DragShift a free, local, cross-platform equal of
[Tangerine](https://tangerineformac.com) (Leap Studio, $29.99 one-time on the Mac App Store,
Apple silicon only), and make it look and feel like a native Apple app.

This file is the single source of truth for what is left to do. Every agent or person working on
the fork reads it first and updates the status tables when they finish something. Keep it
current-state: change rows in place, don't add a changelog.

## Ground rules

- **Local only.** No network calls, no telemetry, no update checks. Files never leave the machine.
- **Never touch the original.** Every result is a new file beside the source (`util.sibling`),
  written atomically (`convert.atomic_save`). Never overwrite or delete a user's file.
- **Cross-platform stays working.** macOS is the design target, but Windows and Linux builds must
  keep passing. Platform-specific code goes in `platform_mac.py` / `platform_linux.py` or behind
  `sys.platform` checks, like the existing code.
- **Subprocesses take argument lists, never `shell=True`.** File names are untrusted input.
- **Tests for every change.** Unit tests in `tests/` (pytest, `QT_QPA_PLATFORM=offscreen`).
  Generate fixtures in the test (for example, a 1-second ffmpeg `testsrc` clip), don't commit
  large binary fixtures.
- **Match the surrounding code.** Same naming, comment density and idioms as neighbouring files.
- **Don't copy Tangerine's name, icon, wording or artwork.** Match features and quality, not branding.
- **Errors users can read.** Raise `util.UserError` with a plain sentence for anything the user
  can fix (missing codec, password-protected file, and so on).

## Reference: what Tangerine does

Source: tangerineformac.com, its press kit, and the App Store listing (v2.0.1, October 2026).

- **Gesture:** start dragging a file, then tap a modifier. Shift = convert wheel, Command = tools
  wheel, Control = actions wheel. The wheel stays open after the key is released until the file
  is dropped or the action is cancelled. Keys can be remapped in Settings.
- **Keyboard:** Enter on a selected Finder file opens the format wheel. Arrow keys move, Enter
  applies, Escape cancels, Command/Control switch wheels.
- **Scale:** 275 conversion paths, 26 file tools, 6 file actions.
- **Formats:**
  - Images: JPG, PNG, WebP, HEIC, TIFF, AVIF, BMP; SVG input only; PDF export.
  - Audio: MP3, M4A, WAV, FLAC, OGG, Opus, AIFF, WMA.
  - Video: MP4, MOV, MKV, WebM, AVI, WMV, GIF; MP3 export.
  - PDF: to DOCX, Pages, PPTX, XLSX, JPG, PNG, EPUB, TXT, MD.
  - Documents: DOCX, Pages, Markdown, TXT, EPUB; RTF, RTFD and text-clipping input; HTML export.
  - Slides: PPT/PPTX to PDF, JPG, PNG; PDF to PPTX.
  - Spreadsheets: XLSX, ODS, CSV, TSV; XLS input; PDF export.
  - Subtitles: SRT, VTT; TXT export.
  - Archives: ZIP, TAR, GZIP; RAR input only.
- **Tools (examples):** compress (including by percentage), crop, trim, split, merge, metadata
  removal, redaction, annotation, QR reading, PDF page reorder, video crop, change speed, cut,
  extract audio, audio cleanup.
- **Actions:** AirDrop, Float (pin the file in a small floating window to drag later), Copy,
  Move, Copy Path, open in Terminal. Work on folders and mixed selections.
- **Settings:** remappable keys, conversion quality.

## Current state of this fork

| Area | Status |
|---|---|
| Images: convert (PNG, JPG, WEBP, HEIC, TIFF, AVIF, BMP, PDF, ICO, GIF) | Done (upstream) |
| Image tools: compress, resize, crop, edit, annotate, redact, add background, metadata | Done (upstream) |
| PDF: to JPG, PNG, DOCX, TXT; compress, split, rotate, merge, protect, extract images, metadata | Done (upstream) |
| DOCX to PDF/TXT; text to PDF/DOCX | Done (upstream) |
| Hub dwell flips Convert ⇄ Tools | Fixed in this fork: 1.5 s dwell (`radial.HUB_DWELL_MS`), was 0.42 s and re-fired continuously |
| Video and audio | **Mostly done**: convert, tools (compress, trim, crop, speed, extract audio, mute, frame, rotate; audio compress, trim, speed, normalize, strip tags), Trim/Crop/Speed/Frame windows, progress + cancel, bundled ffmpeg |
| Apple Pages-style design pass | **Mostly done** (workstream 2): design tokens, Pages-style inspector/canvas/toolbar, native windows, banner toasts, SF Symbols |
| Separate key per wheel (Shift/Command/Control) + Actions wheel | **Done** (workstream 3). A wheel's key only counts when pressed after the mouse button (`trigger._stale`), so a normal ⌘-drag in Finder stays a drag. Picking a key another wheel uses swaps the two (Windows/Linux have exactly one key per wheel, so refusing meant no key could change). Live AirDrop, Float drag-out and Windows/Linux Terminal still need hands-on checks |
| Office, ebook and text formats | **Done** (workstream 4): spreadsheets, slides (Keynote when installed), EPUB, Markdown, HTML, RTF, Pages, PDF to XLSX/PPTX/EPUB/MD |
| Keyboard control in the wheel + Finder Services entry | **Done** (workstream 6). The Services entry opens the wheel in a built app (checked with `NSPerformService`); a keyboard shortcut bound to it hasn't been tried |
| Archives and subtitles | **Done** (workstream 5): ZIP/TAR/TAR.GZ from any selection, safe extract (ZIP, TAR, GZ/BZ2/XZ, RAR via bsdtar), archive-to-archive; SRT ⇄ VTT, ASS/SSA in, TXT out |
| SVG input, QR reading, PDF page reorder, compress by percentage | **Done** (workstream 7) |
| Local Mac build (`pyinstaller DragShift.spec`) | Builds (227 MB incl. our LGPL ffmpeg) and passes `--selftest` with Homebrew off PATH (frozen output goes to `~/Library/Logs/DragShift/log.txt`). Fixed: python-pptx/python-docx template paths broke in the frozen app (PDF to PPTX crashed); self-test now also covers SVG, QR, archives and subtitles |
| Hands-on pass, real Finder drags (built app, macOS 15) | **Passed** 9/9 with posted mouse/key events: Shift opens Convert and a drop converts PNG to JPG; ⌘ held before the click opens nothing; ⌘ after the drag starts opens Tools; Esc closes; resting in the hub flips rings after 1.5 s (not at 0.8 s); MP4 to MOV via the bundled ffmpeg; Actions Zip and Float. Not run: AirDrop (would show the share sheet to nearby devices), Float drag-out, Keynote/Pages export (not installed) |
| Mac ffmpeg is LGPL | **Done**: `packaging/ffmpeg/build_mac.py` builds ffmpeg 9.0.2 + LAME, Opus, Ogg, Vorbis, libvpx from pinned, hash-checked sources, LGPL v3 only, static, macOS 11+, linked only to system libraries; H.264 via VideoToolbox. CI builds it per runner with a cache. Replaces Riedl's GPL build |
| Linux | Full suite and self-test pass in Docker (Debian 12, amd64 and arm64, source and PyInstaller build) after fixes: `QFont.Tag` on PySide6 < 6.8.3, OpenH264 bitrate mode for compress-by-percent, tests that assumed macOS keys. Real X11 drags not checked |
| Review fixes | Tar hard-link zip bomb, EPUB repeated-spine memory blowup, `=` cells becoming XLSX formulas, Markdown/HTML/SVG pulling in files from outside the source folder, inconsistent DOCX errors (now one helper, detects password protection), numbered Word lists, very long names, right Windows key, IPC payload checks + user-only socket. Plus DRY clean-up (`util.plural`, `save_bytes`/`save_text`, one control-char regex, one encrypted-OLE check) |
| Test-only deps split out | `requirements-dev.txt` (pytest, xlwt, qrcode); the app ships only `requirements.txt` |
| Next | Run CI once (new Mac ffmpeg build, Intel runner untested), Windows check, macOS 26 Liquid Glass (`NSGlassEffectView`, needs a macOS 26 machine), AirDrop and Float drag-out by hand, design: the wheel's glass takes on the wallpaper's colour strongly (purple wallpaper gives a purple wheel), which is further from Pages' neutral popovers than it should be |

## Workstreams

Ordered by priority. Each one should land as its own branch (`feature/<name>`) and PR into the
fork's `main`.

### 1. Video and audio (`feature/video-audio`): partly done

**Done:** `dragshift/media.py` (only module that runs ffmpeg/ffprobe; paths passed as
`file:/abs/path`; temp file + atomic rename), `video`/`audio` categories (`.ts` only counts as
video when it really is an MPEG transport stream, so TypeScript files aren't misread), all
conversions below, the non-window tools, percentage progress on the card, cancel by closing the
card, `packaging/ffmpeg/fetch.py` (pinned + SHA-256-checked downloads), 66+ tests that check
outputs with ffprobe. Codecs: libx264 CRF 20 (CRF 28 for Compress) when the ffmpeg found has it
(source runs with Homebrew), otherwise VideoToolbox (bundled Mac build), Media Foundation or OpenH264, VP9+Opus for WebM, MPEG-4 Part 2+MP3 for AVI, GIF palette pass at 640 px / 15 fps,
60 s cap.

**Windows (done):** `dragshift/media_windows.py`. Trim (video and audio), Crop (video), Speed
(video and audio) and Frame open from the Tools ring (`app.MEDIA_TOOLS` maps a name to a one-click
function or a window class). Trim/Crop/Frame share a canvas with a QuickTime-style timeline
(filmstrip for video, ffmpeg waveform for sound, yellow trim handles, play head, arrow keys step
one frame). Preview frames come from `media.preview_frame` / `media.waveform` (stdout, never a
file) on worker threads, debounced and cached by frame number. Trim has Start/End fields and
Precise (re-encode) or Fast (stream copy, `media.trim(fast=True)`). Crop reuses `tools.CropView`
and `tools.CropInspector`, works in upright pixels for rotated clips and rounds to even sizes.
Speed has presets plus a logarithmic slider and Keep pitch (`media.speed(keep_pitch=False)` uses
`asetrate`). Frame became a window (pick the frame, then Save Frame) instead of saving the middle
frame. Tests: `tests/test_media_windows.py`.

**Left:** compress to a target size (by percentage is done, workstream 7), audio metadata view. In the windows: no playback,
non-square pixels (anamorphic video) aren't corrected in the crop preview (`media.probe` doesn't
read the sample aspect ratio), and `media.probe` (ffprobe) still runs on the UI thread when a
window opens. CI changes haven't run yet.

Original plan:

The largest gap. Needs ffmpeg.

- **ffmpeg:** ship it with the app, and fall back to an `ffmpeg` on PATH for source runs.
  - Pick builds per platform (macOS arm64, macOS x86_64, Windows x64, Linux amd64) and fetch them
    in CI (`.github/workflows/build.yml`), pinned by version and SHA-256. Add them to
    `DragShift.spec` as binaries.
  - Licensing: prefer LGPL builds. If a GPL build is used, it ships as a separate executable
    called through `subprocess`, with its license text bundled and a note in the README. Record
    the decision here.
  - Write one helper module (`dragshift/media.py`) that finds ffmpeg/ffprobe, probes files
    (duration, streams, codecs), and runs ffmpeg with progress parsing and cancellation. No
    other module calls ffmpeg directly.
- **Categories:** `video` and `audio` in `util.category`.
- **Conversions:**
  - Video to MP4, MOV, MKV, WebM, AVI, GIF, plus audio-only exports (MP3, M4A, WAV).
  - Audio to MP3, M4A, WAV, FLAC, OGG, Opus, AIFF.
  - Pick sensible codecs per container (H.264/AAC for MP4/MOV, VP9/Opus for WebM, and so on);
    stream-copy when the codecs already fit the target container.
  - GIF export uses a palette pass (`palettegen`/`paletteuse`), capped width and fps.
- **Tools (convert ring stays first; tools ring second):**
  - Video: Compress (by percentage or target size), Trim, Crop, Speed, Extract audio, Mute,
    Frame grab (still image at the current position), Rotate.
  - Audio: Compress/bitrate, Trim, Speed, Normalize volume, Metadata (view/strip tags).
  - Trim and Crop need a tool window with a scrubber/preview (reuse `tools.ToolWindow`).
- **Progress:** long jobs show progress in the existing toast/spinner and can be cancelled.
- **Tests:** generate short clips with `ffmpeg -f lavfi testsrc / sine` inside tests; skip
  cleanly when ffmpeg is missing. Check output streams with ffprobe, not just file existence.

### 2. Apple-native design pass (`feature/apple-design`): mostly done

Goal: **match Apple Pages** on macOS (current, macOS 26 Tahoe era with Liquid Glass), so it
looks like Apple's Pages team built it. Windows/Linux keep a close approximation from the
same tokens. Follow Apple's Human Interface Guidelines (macOS) underneath.

**Done:**

- `dragshift/design.py` is the single source of truth: system font (`.AppleSystemUIFont` on
  macOS, Segoe UI Variable on Windows), Apple text styles with real weights, semantic colours read
  live from `NSColor` (light/dark and the user's accent, with Apple's published values as the
  fallback), radii, 4/8-pt spacing, control heights, a damped-spring easing curve, Reduce motion /
  Reduce transparency (cached, 2 s), SF Symbols rendered from AppKit to cached tinted pixmaps
  (text glyph fallback), and the window stylesheets. `design.theme().changed` fires on a live
  light/dark or accent switch; windows restyle themselves.
- `dragshift/controls.py`: Pages pieces Qt lacks: capsule segmented control (keeps the
  `QButtonGroup` API), inspector with segmented tabs, small bold group headers, disclosure
  triangles and hairlines, icon-above-label toolbar buttons in Liquid Glass capsules, colour
  well with a swatch popover, popover with an arrow, macOS spoke spinner, focus rings.
- Windows on macOS are real titled windows: traffic lights, native corners and shadow,
  `NSVisualEffectView` window material, title + subtitle (file name and size) in the title bar,
  and a real `NSToolbar` (unified style, SF Symbol items with labels) driven by the Qt
  `Toolbar` model. Qt widgets can't live in the title bar (AppKit takes those clicks), which is
  why the toolbar is native. Standard controls are drawn by Qt's macOS style (real AppKit
  buttons, pop-ups, sliders, steppers); the default button is the accent colour.
- Tool windows use Pages' layout: canvas left (grey canvas, image with a soft shadow),
  fixed-width Format-style inspector right with small controls, Cancel / primary at the bottom;
  windows without a canvas use the inspector full width. Settings uses the classic Pages
  Settings form (right-aligned labels); Welcome uses Apple's welcome-sheet layout.
- Toasts look like macOS notification banners: real popover material on macOS, top-right under
  the menu bar, headline title, leading thumbnail or SF Symbol, hover close button, glass
  capsule actions ("Show in Finder" on macOS).
- Radial menu keeps the liquid glass but drops the painted rainbow; selected petal uses the
  accent colour like a selected menu item; SF Symbols on tool petals; SF text styles; spring
  pop-in (fade only with Reduce motion); VoiceOver name updates as the pointer moves.
- Menu-bar icon was already a monochrome template image; the tray menu is a native `NSMenu`.
- Tests: `tests/test_design.py` (fallbacks off macOS, colour validity and contrast, motion and
  transparency helpers, SF Symbol fallback, theme change, controls, and a paint smoke test of
  every window, toast state and the radial menu).

**Left:**

- macOS 26: try `NSGlassEffectView` for toasts, popovers and the radial menu instead of
  `NSVisualEffectView` / the painted glass (not available on the macOS 15 dev machine, so untested).
- AppKit logs "NSWindow warning: adding an unknown subview" once per window because the
  effect view goes into the window's frame view. Harmless; the clean fix is a container
  content view, which Qt doesn't support well.
- Hold-to-compare in Edit became a toggle (native toolbar items only report clicks).
- No sliding animation on the segmented control's selection yet.
- Windows/Linux: check the glass windows on real hardware (only checked offscreen on macOS).
- The tray menu has no SF Symbol item icons (macOS 26 menus show them).

### 3. Separate keys per wheel + Actions wheel (`feature/actions-wheel`): done, needs hands-on checks

**Done:**

- Key per wheel: `settings.default_keys()` gives Shift = Convert, Command = Tools, Control =
  Actions on macOS. Windows and Linux use Shift, Ctrl, Alt (there is no Command key, and the
  Windows/Super key opens the Start menu or desktop overview when tapped, so it isn't offered).
  Settings has one pop-up per wheel; a key another wheel already uses is refused with a plain
  message and the pop-up snaps back (`settings.validate_keys`). Damaged prefs fall back to the
  defaults.
- Migration: the old single `trigger` setting becomes the Convert key on first start, and the
  wheel that would have shared it takes Convert's default key instead (`settings.migrate_trigger`).
- `trigger.py` reads every mapped key each tick. The key that opens the menu picks the starting
  wheel; pressing another wheel's key while open jumps straight to it (the same swap animation as
  `next_ring`); pressing the showing wheel's key again goes to the next ring, as before. The wheel
  stays open after the key is released until the drop or Escape. The hub dwell still cycles
  through every ring, and the hub hint names the next ring with its key (for example "⌘ Tools").
- Actions ring (`dragshift/actions.py`) for any dragged item: folders, unsupported files and
  mixed selections now get Actions instead of "Unsupported". Convert/Tools follow the first item
  DragShift can handle (folders never count, even one named `Photos.png`); the menu opens on the
  wanted wheel, or the first one the files have.
  - AirDrop: `NSSharingService` (`NSSharingServiceNameSendViaAirDrop`) with file URLs; hidden off macOS.
  - Float (`dragshift/floating.py`): small always-on-top panel with thumbnail or system icon and
    name, close button, Escape closes; many at once; position remembered per item set for the
    session. Dragging out offers Copy only, so the original never moves.
  - Copy / Move: native folder picker that remembers the last folder; never overwrites
    (`unique_path`), handles folders (`copytree`), refuses copying a folder into itself, turns
    permission errors into `UserError`. Copy's Undo moves the copies to the Trash (never the
    destination folder); Move's Undo moves everything back, beside anything new at the old path
    as "name (1)" rather than over it.
  - Copy Path: absolute paths, one per line, on the clipboard, with a banner.
  - Terminal: `open -a Terminal <dir>` on macOS; Windows Terminal (`wt -d .`) or `cmd /K` on
    Windows, and the first of x-terminal-emulator, gnome-terminal, konsole... on Linux, with the
    folder passed as the working directory so cmd.exe/wt never parse the name. No `shell=True`.
- Tests: `tests/test_actions.py` (keys, migration, validation, ring availability, Copy/Move/Undo
  edge cases, clipboard, terminal commands per platform, AirDrop gating, Float paint and drag
  MIME, the watcher's key switching) and wheel switching in `tests/test_radial.py`.

**Left:**

- Hands-on checks: the real AirDrop sheet, a Float drag into Finder/Mail, and the key switching
  during a live Finder drag were only covered by unit tests and offscreen/Cocoa renders.
  Terminal launching on Windows and Linux is only tested with a mocked `Popen`.
- Holding Command or Control when a drag starts also opens a wheel (same rule Shift always had),
  so a Command-drag in Finder now shows the Tools wheel under the cursor.
- A Move that fails halfway reports the error but offers no Undo for the items already moved
  (`actions.Moves` records them; the card just doesn't expose it yet). Cancelling the card
  doesn't stop a long Copy/Move.

### 4. Office, ebook and text formats (`feature/documents`): done

**Done:** `documents.py`, `sheets.py`, `slides.py`; categories sheet (.xlsx .xlsm .xls .ods .csv
.tsv; detected by content), slides (.pptx; .ppt via Keynote only), epub, markdown, rtf (.rtf
.rtfd), pages. Spreadsheets to XLSX/ODS/CSV/TSV/PDF/DOCX (one CSV per sheet); slides to
PDF/JPG/PNG (Keynote via `osascript` with paths as arguments when installed, built-in renderer
otherwise); PDF to PPTX/XLSX/EPUB/MD; EPUB in and out; Markdown to PDF/DOCX/HTML/EPUB/TXT;
RTF to TXT/DOCX/PDF/MD (formatting kept on macOS via textutil); Pages via Pages app or the
embedded preview. New deps (all MIT-compatible): openpyxl, xlrd, odfpy (Apache), python-pptx,
markdown-it-py, striprtf, lxml. Behaviour changes: .csv is a spreadsheet, .md has its own
category, HTML to PDF renders as a page, DOCX to PDF keeps pictures/lists/links.

**Left:** built-in slide renderer is approximate (no charts, rotation, shadows, real
gradients or SmartArt); Keynote/Pages export untested on a machine that has them; XLSX
formulas use last saved values; PyInstaller build with the new deps not yet run.

Original plan:

- Spreadsheets: XLSX, ODS, CSV, TSV, XLS input, PDF export (openpyxl, odfpy or pandas).
- Slides: PPTX to PDF/JPG/PNG; PDF to PPTX (python-pptx).
- EPUB, Markdown, HTML export; RTF input; Pages input where feasible.
- PDF to XLSX, PPTX, EPUB, MD.

### 5. Archives and subtitles (`feature/archives-subtitles`): done

**Done:**

- `dragshift/archives.py`, category `archive` (.zip .tar .tgz .gz .bz2 .xz .tbz2 .txz .rar;
  `archive_suffix` tells `.tar.gz` from `.gz`).
- Where it shows up: a selection nothing else converts (folders, unknown files, mixed folders and
  files) gets ZIP / TAR / TAR.GZ in the **Convert** ring (Tangerine lists archives under formats),
  making one archive of everything. **Zip** is also an Actions-ring entry, so any selection,
  convertible files included, can be zipped without crowding every Convert ring. An archive gets
  the other archive formats in Convert and **Extract Files** in Tools.
- Create: one item becomes "<name>.zip" (Finder style: "photo.jpg.zip"), several become
  "Archive.zip", never overwriting ("Archive (1).tar.gz", the double extension kept). Relative
  structure and empty folders kept, sorted order, two selected "a.txt" become "a.txt" and
  "a (1).txt", .DS_Store / __MACOSX / "._" resource forks skipped inside folders, symlinks stored
  as links (never followed), no user or group names in tars, the hidden temp file never archived
  into itself. Progress and cancel on the card.
- Extract: into a new folder beside the archive ("Photos/", unique); a single top folder isn't
  doubled; a single .gz/.bz2/.xz becomes the file beside it. Every entry is checked before
  anything is written: absolute paths, drive letters, "..", backslash tricks, NUL bytes and NTFS
  streams refuse the whole archive with a readable message; symlinks must stay inside (checked on
  the names, then again on the real path after each link is made, which catches chains like
  `a → .` + `b → a/..`); links are made last so no file is written through one; hard links are
  copied, never linked; devices and FIFOs skipped; setuid dropped. Zip bomb caps (200,000 items,
  32 GB, enforced on bytes really written) and a free-space check. Everything goes into a hidden
  temp folder that only becomes the result when it all worked. Encrypted zips (ZipCrypto or AES)
  get "password-protected" (no password prompt); names without the UTF-8 flag try UTF-8, then
  cp437.
- RAR (input only): libarchive's `bsdtar` (built into macOS and Windows 10+ as tar.exe,
  `libarchive-tools` on Linux, BSD licence) re-packs it as a tar stream (`@archive`, argument
  list, no shell), and that stream goes through the same checks; no unrar code. Clear message when
  bsdtar is missing.
- Archive to archive (ZIP ⇄ TAR ⇄ TAR.GZ, RAR/GZ to any) extracts safely to a temp folder and
  re-packs it, structure unchanged.
- `dragshift/subtitles.py`, category `subtitle` (.srt .vtt, .ass/.ssa input). SRT → VTT/TXT,
  VTT → SRT/TXT, ASS/SSA → SRT/VTT/TXT. BOM, CRLF/CR, UTF-16 and cp1252 read; output is UTF-8
  with LF. Hours past 99 kept; '.' or ',' and short fractions accepted; malformed cues skipped.
  <b>/<i>/<u> kept, VTT voice tags become "Name: ", other tags and entities cleaned, SRT `{\anN}`
  becomes VTT `line:`/`align:` settings, VTT settings dropped for SRT, stray `<`/`&` escaped in
  VTT. TXT is dialogue only with roll-up repeats removed. ASS `{\i1}` etc. become tags.
- Tests: `tests/test_archives.py`, `tests/test_subtitles.py` (round trips, every hostile case
  above, RAR from a byte-built fixture through the real bsdtar, exact subtitle output).

**Left:** no password prompt for encrypted archives; on Windows without the symlink privilege,
links inside archives are skipped; RAR on Windows/Linux depends on the system bsdtar having RAR
support (untested there); 7z isn't read.

### 6. Keyboard control (`feature/keyboard`): done, Services entry needs a hands-on check

**Done:**

- Keys: Left/Right move the highlight clockwise/counter-clockwise round the wheel (wrapping;
  from nothing, Right starts at the top petal and Left at the last). Up/Down and Tab switch
  wheels and keep the position (clamped to the new wheel's size), so the two axes mean two
  things: which petal, which wheel. Enter runs the highlighted petal (on the hub it shows the
  next wheel, like a click), Escape closes. A wheel's key still jumps to that wheel.
  `radial.step` is the navigation math; `RadialMenu.nav(name)` is the one entry point for both
  input paths below.
- During a drag the menu never has focus (`WindowDoesNotAcceptFocus`), so `trigger.py` polls
  the keys too: `platform_mac.nav_down` (`CGEventSourceKeyState`: arrows 123-126, Return 36,
  keypad Enter 76, Escape 53, Tab 48), `GetAsyncKeyState` on Windows, `XQueryKeymap` keysyms on
  Linux X11 (Wayland has no global key state, same gap as the trigger). `trigger.KeyRepeat`
  turns "held" into presses with the user's own key repeat on macOS (`NSEvent.keyRepeatDelay` /
  `keyRepeatInterval`), 0.4 s / 0.08 s elsewhere; late ticks fire once instead of bursting.
  Enter and Tab never repeat. Keys are only polled while a drag menu is open.
- Enter mid-drag: macOS's drag pasteboard (`NSPasteboardNameDrag`) is readable during the drag
  with no permission. The watcher stores its `changeCount` at mouse-down and only trusts it once
  it changes (the pasteboard keeps the last drag's files afterwards), and the menu loads the
  rings from it the moment it opens, so the keys work before the drag even reaches the window.
  Without it (Windows/Linux) the files come from the drag entering the menu, which it does as
  soon as the pointer moves. Enter runs the item at once with those files; the menu then stays
  up showing "Release to finish" and swallows the drop as a copy, so the file can't land on a
  folder underneath and move. Arrowing to a petal and letting go without moving the pointer
  runs that petal too (`RadialMenu.target`).
- Keyboard and pointer share one highlight: the same accent petal and spring, with the light
  gathered at the petal's centre. The pointer takes it back only after it really moves
  (`POINTER_SLOP`), because macOS repeats drag-move events while the mouse is still. VoiceOver's
  name follows the keyboard highlight.
- Click menus (tray, Open With, Services): the window takes focus (`_set_focusable`), the app is
  activated on macOS, and `keyPressEvent` maps Qt keys to the same `nav` names (Qt's macOS
  Command/Control swap handled in `radial.modifier_name`). Escape or a click outside hands focus
  back to the app that had it (Finder).
- Finder: an `NSServices` entry in `DragShift.spec` ("Open in DragShift", message `openFiles`,
  files and folders, enabled by default) and a provider registered at start-up
  (`platform_mac.register_services`) that hands the files to `queue_files`, which opens the
  wheel at the pointer. Users give it a shortcut in System Settings > Keyboard > Keyboard
  Shortcuts > Services (README). Chosen over reading Finder's selection with AppleScript, which
  needs an Automation prompt and still can't stop Finder from acting on the key, and over a
  plain Return, which needs Accessibility to take the key from Finder (where it renames).
- Fixed on the way: `design.spring_curve` returned a curve whose custom easing died with it, so
  the wheel's pop-in animation could segfault on start; curves are now cached.
- Tests: `tests/test_keyboard.py` (navigation math, ring switching, empty and one-item rings,
  key repeat with a fake clock, the watcher's polling, Enter/Escape in both modes, keyPressEvent,
  focus flags, VoiceOver names, pointer vs keyboard, drag pasteboard freshness, pasteboard and
  Services provider on macOS, the Info.plist entry).
- Checked live on macOS 15 (in-process, synthesized events): a real Cocoa drag shows its file on
  the drag pasteboard mid-drag with a new `changeCount`; polled Right, Right, Enter during the
  drag ran the second petal with the button still down, and the drop was swallowed as a copy;
  the focused click menu got real key events and Escape gave focus back to Finder.

**Left:**

- The Services entry and its shortcut are untested in a built, installed bundle (needs a
  PyInstaller build in /Applications). The mid-drag checks used a Qt drag source, not Finder;
  Finder's own drags write the same pasteboard (seen holding Finder's file reference URLs
  after a drag), but a hands-on Finder drag with the arrow keys is still to do.
- Keys pressed mid-drag also reach whatever app has focus (usually the drag source; what
  Finder does with arrows and Return during its own drag isn't checked yet). Swallowing them
  needs an event tap, which needs Accessibility.
- Windows/Linux have no early view of the dragged files, so the keys do nothing until the drag
  enters the menu (first pointer movement). Windows and Linux key polling is only unit tested.

### 7. Smaller gaps: done

**Done:**

- SVG input (`dragshift/svg.py`, category `svg`): QtSvg (part of PySide6, no cairo) renders to
  every image format; the long side is scaled into 1024 to 8192 px (SVG has no pixel size). PDF is
  drawn as vectors at the drawing's own size (text stays text). `Qt6Svg` is no longer dropped from
  the PyInstaller build. QtSvg is SVG Tiny 1.2: no CSS stylesheets, filters or masks.
- QR reading (`dragshift/qr.py`, image Tool **Read QR**): zxing-cpp (Apache-2.0) finds QR codes and
  other barcodes; a small Pages-style window shows each text with Copy, and Open Link only for
  http(s) addresses. Nothing ever opens by itself. No code found is a plain error card.
- PDF page reorder (`dragshift/reorder.py`, PDF Tool **Reorder**): thumbnail grid on the grey
  canvas (rendered a few at a time, so long PDFs open at once), drag to reorder, Move Earlier /
  Later (⌘← / ⌘→), Delete (⌫, never the last page), Reverse, Reset; saves "<name> Reordered.pdf".
- Compress by percentage: images got a third Compress mode (Percentage, reusing the target-size
  search per file); PDF, video and audio Compress open a small window (`dragshift/compress.py`) with
  Recommended (the old one-click settings, Enter applies) or Percentage. PDFs try image
  resolution/quality steps until they fit (or keep the smallest); video plans a bitrate from size
  and duration (single pass, usually within about 10%); audio scales the bitrate (32 to 320 kbps).
- Tests: `tests/test_extras.py` (raster sizes and pixels, vector PDF, QR decode of generated codes
  and link safety, page order output, percentage results, ring wiring and routing).

**Left:** the PyInstaller build with zxing-cpp and QtSvg hasn't been run; video percentage is
single-pass ABR, so very short or static clips can land under the aim.

## Known issues

- The filmstrip's single-frame ffmpeg calls check for cancel between frames, not during one.
- Keynote/Pages export temp names grow with the file name (very long names could fail there).
- The IPC handler reads the whole message with no size cap (local, user-only socket).
- A built Mac app logs "NSWindow warning: adding an unknown subview: NSVisualEffectView" once at
  start-up (`platform_mac.add_vibrancy` adds to the theme frame). Harmless today; AppKit may stop
  allowing it.
- The hub's text (size, next ring) sits under Finder's drag picture while a drag is over the hub,
  so it can't be read then.
- Old PySide6 (6.8.0, the newest on Debian 12 arm64) draws SVG text as outlines in PDFs.
- `fetch.py` has no `linux-arm64` entry (only an amd64 .deb ships today).

## Open decisions

- **ffmpeg on Windows/Linux.** BtbN 9.0.2 LGPL v3 builds add about 270 MB (a slim custom build
  like the Mac one would fix it), BtbN's OpenH264 isn't covered by Cisco's patent licence, and
  BtbN keeps builds about two years, so mirror them.
- Whether to sign/notarize the Mac build (needs an Apple Developer account, $99/year).
- Whether to keep upstream's Cash App tip link in this fork.
