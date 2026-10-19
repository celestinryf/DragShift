"""macOS integration (only imported on darwin).

* Trigger: NSEvent.pressedMouseButtons / modifierFlags give the *global* mouse and
  modifier state, with no Accessibility or Input Monitoring permission needed.
* Capture: CGWindowListCreateImage can capture "everything below window N", which keeps
  Finder's drag picture and our own windows out of the glass (live refresh like Windows'
  capture exclusion). Seeing other apps' windows needs the Screen Recording permission;
  without it macOS returns just the wallpaper, which still makes a pleasant glass.
* Start at login: a LaunchAgent plist.
"""
from __future__ import annotations

import os
import plistlib
import sys
from pathlib import Path

try:
    import AppKit  # pyobjc-framework-Cocoa
    import Quartz  # pyobjc-framework-Quartz
    OK = True
except Exception:  # pragma: no cover
    OK = False

# NSEventModifierFlags
SHIFT, CONTROL, OPTION, COMMAND = 1 << 17, 1 << 18, 1 << 19, 1 << 20
KEY_FLAGS = {"shift": SHIFT, "ctrl": CONTROL, "alt": OPTION, "cmd": COMMAND}


# ---------------------------------------------------------------- input state
def left_button_down() -> bool:
    return bool(OK and AppKit.NSEvent.pressedMouseButtons() & 1)


def key_down(name: str) -> bool:
    return bool(OK and AppKit.NSEvent.modifierFlags() & KEY_FLAGS.get(name, SHIFT))


# virtual key codes (Carbon kVK_*) for the wheel's keyboard control; "enter" is Return or keypad Enter
KEY_CODES = {"left": (123,), "right": (124,), "down": (125,), "up": (126,), "enter": (36, 76),
             "escape": (53,), "tab": (48,)}


def nav_down(name: str) -> bool:
    """Is this navigation key held right now? CGEventSourceKeyState reads the global keyboard
    state without Accessibility or Input Monitoring, so it works while Finder owns a drag."""
    if not OK:
        return False
    try:
        return any(Quartz.CGEventSourceKeyState(Quartz.kCGEventSourceStateCombinedSessionState, code)
                   for code in KEY_CODES.get(name, ()))
    except Exception:
        return False


def escape_down() -> bool:
    return nav_down("escape")


def key_repeat() -> tuple[float, float]:
    """The user's key repeat (delay, interval) in seconds, from Keyboard settings."""
    try:
        return float(AppKit.NSEvent.keyRepeatDelay()), float(AppKit.NSEvent.keyRepeatInterval())
    except Exception:
        return 0.4, 0.08


# ---------------------------------------------------------------- pasteboards
def pasteboard_paths(pboard) -> list[str]:
    """Local file paths on a pasteboard (Finder writes file reference URLs, so resolve them)."""
    try:
        urls = pboard.readObjectsForClasses_options_(
            [AppKit.NSURL], {AppKit.NSPasteboardURLReadingFileURLsOnlyKey: True}) or []
        paths = [u.filePathURL().path() if u.isFileReferenceURL() else u.path() for u in urls]
        return [str(p) for p in paths if p]
    except Exception:
        return []


def _drag_pasteboard():
    return AppKit.NSPasteboard.pasteboardWithName_(AppKit.NSPasteboardNameDrag)


def drag_change_count() -> int:
    """Bumped by every new drag. The drag pasteboard keeps the last drag's files after it ends,
    so callers compare this with a count taken at mouse-down to know the contents are fresh."""
    if not OK:
        return -1
    try:
        return int(_drag_pasteboard().changeCount())
    except Exception:
        return -1


def drag_paths() -> list[str]:
    """Files on the drag pasteboard: readable mid-drag with no permission prompt."""
    return pasteboard_paths(_drag_pasteboard()) if OK else []


# ---------------------------------------------------------------- app behaviour
def become_accessory():
    """Menu-bar app: no Dock icon (the bundle also sets LSUIElement)."""
    if OK:
        AppKit.NSApplication.sharedApplication().setActivationPolicy_(
            AppKit.NSApplicationActivationPolicyAccessory)


def activate():
    """Bring our windows to the front (accessory apps aren't activated automatically)."""
    if OK:
        AppKit.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)


def frontmost_app():
    """The app in front right now (to hand focus back after a keyboard-driven menu), or None
    when it's DragShift itself."""
    if not OK:
        return None
    try:
        app = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
        return None if app is None or app.processIdentifier() == os.getpid() else app
    except Exception:
        return None


def reactivate(app):
    """Give focus back to an app returned by frontmost_app (Finder, after Escape)."""
    if OK and app is not None:
        try:
            app.activateWithOptions_(0)
        except Exception:
            pass


# ---------------------------------------------------------------- Services menu
# Info.plist (DragShift.spec) declares an NSServices entry with NSMessage "openFiles": Finder lists
# it under right-click > Services (or Quick Actions), and System Settings > Keyboard > Keyboard
# Shortcuts > Services lets the user give it a shortcut. No Accessibility permission involved.
SERVICE_MESSAGE = "openFiles"
_SERVICES_CLASS = None
_SERVICES = None             # the provider object; AppKit doesn't retain it for us
_ON_SERVICE_FILES = None


def _services_class():
    global _SERVICES_CLASS
    if _SERVICES_CLASS is None:
        import objc

        class DragShiftServices(AppKit.NSObject):
            @objc.typedSelector(b"v@:@@o^@")
            def openFiles_userData_error_(self, pboard, user_data, error):
                paths = pasteboard_paths(pboard)
                if paths and _ON_SERVICE_FILES is not None:
                    try:
                        _ON_SERVICE_FILES(paths)
                    except Exception:
                        import traceback
                        traceback.print_exc()
                return None

        _SERVICES_CLASS = DragShiftServices
    return _SERVICES_CLASS


def register_services(on_files) -> bool:
    """Receive files from DragShift's Services menu item: on_files(paths) runs on the main thread."""
    global _SERVICES, _ON_SERVICE_FILES
    if not OK:
        return False
    try:
        _ON_SERVICE_FILES = on_files
        if _SERVICES is None:
            _SERVICES = _services_class().alloc().init()
            AppKit.NSApplication.sharedApplication().setServicesProvider_(_SERVICES)
            AppKit.NSUpdateDynamicServices()   # a freshly installed copy shows up without logging out
        return True
    except Exception:
        return False


def _ns_view(widget):
    """The NSView behind a Qt top-level widget (winId() is an NSView*, but only on Qt's
    Cocoa platform: with "offscreen" or "minimal" it's not a pointer, and using it crashes)."""
    import objc
    from PySide6.QtGui import QGuiApplication
    app = QGuiApplication.instance()
    if app is None or app.platformName() != "cocoa":
        raise RuntimeError("not running on Qt's Cocoa platform")
    return objc.objc_object(c_void_p=int(widget.winId()))


def window_number(widget) -> int:
    """CGWindowID of a Qt top-level widget."""
    if not OK:
        return 0
    try:
        win = _ns_view(widget).window()
        return int(win.windowNumber()) if win is not None else 0
    except Exception:
        return 0


# ---------------------------------------------------------------- appearance
def is_dark() -> bool:
    """True when the app's effective appearance is Dark Aqua (follows System Settings live)."""
    if not OK:
        return False
    try:
        ap = AppKit.NSApplication.sharedApplication().effectiveAppearance()
        best = ap.bestMatchFromAppearancesWithNames_([AppKit.NSAppearanceNameAqua, AppKit.NSAppearanceNameDarkAqua])
        return best == AppKit.NSAppearanceNameDarkAqua
    except Exception:
        return False


def ns_color(selector: str, dark: bool) -> tuple[int, int, int, int] | None:
    """A semantic NSColor (e.g. "labelColor") resolved for light or dark, as sRGB 0-255."""
    if not OK:
        return None
    try:
        name = AppKit.NSAppearanceNameDarkAqua if dark else AppKit.NSAppearanceNameAqua
        previous = AppKit.NSAppearance.currentAppearance()
        AppKit.NSAppearance.setCurrentAppearance_(AppKit.NSAppearance.appearanceNamed_(name))
        try:
            c = getattr(AppKit.NSColor, selector)().colorUsingColorSpace_(AppKit.NSColorSpace.sRGBColorSpace())
        finally:
            AppKit.NSAppearance.setCurrentAppearance_(previous)
        if c is None:
            return None
        return (round(c.redComponent() * 255), round(c.greenComponent() * 255),
                round(c.blueComponent() * 255), round(c.alphaComponent() * 255))
    except Exception:
        return None


def accessibility() -> tuple[bool, bool]:
    """(Reduce motion, Reduce transparency) from System Settings > Accessibility > Display."""
    if not OK:
        return False, False
    try:
        ws = AppKit.NSWorkspace.sharedWorkspace()
        return (bool(ws.accessibilityDisplayShouldReduceMotion()),
                bool(ws.accessibilityDisplayShouldReduceTransparency()))
    except Exception:
        return False, False


# ---------------------------------------------------------------- SF Symbols
_WEIGHTS = {"ultralight": "NSFontWeightUltraLight", "thin": "NSFontWeightThin", "light": "NSFontWeightLight",
            "regular": "NSFontWeightRegular", "medium": "NSFontWeightMedium", "semibold": "NSFontWeightSemibold",
            "bold": "NSFontWeightBold"}
_SCALES = {"small": 1, "medium": 2, "large": 3}   # NSImageSymbolScale


def symbol_png(name: str, point_size: float, weight: str = "regular", scale: str = "medium",
               dpr: float = 2.0) -> tuple[bytes, float, float] | None:
    """Render an SF Symbol (black template) to PNG bytes at `dpr` pixels per point.
    Returns (png, width_pt, height_pt), or None if the symbol doesn't exist on this macOS.
    Symbols come from the OS at runtime; nothing is bundled (their licence forbids that)."""
    if not OK:
        return None
    try:
        img = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, None)
        if img is None:
            return None
        cfg = AppKit.NSImageSymbolConfiguration.configurationWithPointSize_weight_scale_(
            float(point_size), getattr(AppKit, _WEIGHTS.get(weight, "NSFontWeightRegular")), _SCALES.get(scale, 2))
        img = img.imageWithSymbolConfiguration_(cfg) or img
        size = img.size()
        w, h = max(1, int(round(size.width * dpr))), max(1, int(round(size.height * dpr)))
        rep = AppKit.NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
            None, w, h, 8, 4, True, False, AppKit.NSDeviceRGBColorSpace, 0, 0)
        rep.setSize_(size)
        ctx = AppKit.NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
        AppKit.NSGraphicsContext.saveGraphicsState()
        try:
            AppKit.NSGraphicsContext.setCurrentContext_(ctx)
            img.drawInRect_fromRect_operation_fraction_(AppKit.NSMakeRect(0, 0, size.width, size.height),
                                                        AppKit.NSZeroRect, AppKit.NSCompositingOperationSourceOver, 1.0)
            ctx.flushGraphics()
        finally:
            AppKit.NSGraphicsContext.restoreGraphicsState()
        data = rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, None)
        return bytes(data), float(size.width), float(size.height)
    except Exception:
        return None


# ---------------------------------------------------------------- materials and window chrome
MATERIALS = {"titlebar": 3, "menu": 5, "popover": 6, "sidebar": 7, "headerView": 10, "sheet": 11,
             "windowBackground": 12, "hudWindow": 13, "contentBackground": 18, "underWindowBackground": 21}
_VIBRANCY: dict[int, object] = {}   # winId -> NSVisualEffectView


def _rounded_mask(radius: float):
    """A stretchable rounded-rect mask image: Apple's way to round a visual-effect view."""
    r = float(radius)

    def draw(rect):
        AppKit.NSColor.blackColor().set()
        AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(rect, r, r).fill()
        return True
    img = AppKit.NSImage.imageWithSize_flipped_drawingHandler_(AppKit.NSMakeSize(2 * r + 1, 2 * r + 1), False, draw)
    img.setCapInsets_(AppKit.NSEdgeInsetsMake(r, r, r, r))
    img.setResizingMode_(AppKit.NSImageResizingModeStretch)
    return img


def _png_mask(png: bytes, w: float, h: float):
    img = AppKit.NSImage.alloc().initWithData_(AppKit.NSData.dataWithBytes_length_(png, len(png)))
    img.setSize_(AppKit.NSMakeSize(w, h))
    return img


def add_vibrancy(widget, material: str = "windowBackground", radius: float = 0.0,
                 mask: tuple[bytes, float, float] | None = None, follow_active: bool = True) -> bool:
    """Put a real NSVisualEffectView behind a translucent Qt window (live blur of what's
    behind it, tinted for light/dark, and solid when "Reduce transparency" is on).
    `radius` rounds it; `mask` = (png, w, h) cuts it to any shape (e.g. a popover arrow).
    Calling again updates the existing view. Returns False when it can't be done."""
    if not OK:
        return False
    try:
        view = _ns_view(widget)
        frame = view.superview()
        if frame is None:
            return False
        vev = _VIBRANCY.get(int(widget.winId()))
        if vev is None:
            vev = AppKit.NSVisualEffectView.alloc().initWithFrame_(frame.bounds())
            vev.setAutoresizingMask_(AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable)
            vev.setBlendingMode_(AppKit.NSVisualEffectBlendingModeBehindWindow)
            frame.addSubview_positioned_relativeTo_(vev, AppKit.NSWindowBelow, view)
            _VIBRANCY[int(widget.winId())] = vev
            widget.destroyed.connect(lambda *_, k=int(widget.winId()): _VIBRANCY.pop(k, None))
        vev.setMaterial_(MATERIALS.get(material, 12))
        vev.setState_(AppKit.NSVisualEffectStateFollowsWindowActiveState if follow_active
                      else AppKit.NSVisualEffectStateActive)
        if mask is not None:
            vev.setMaskImage_(_png_mask(*mask))
        elif radius > 0:
            vev.setMaskImage_(_rounded_mask(radius))
        win = view.window()
        if win is not None and not (win.styleMask() & AppKit.NSWindowStyleMaskTitled):
            win.setHasShadow_(True)     # borderless: shadow follows the masked shape
            win.invalidateShadow()
        return True
    except Exception:
        return False


def native_chrome(widget, toolbar: bool = False, subtitle: str = "", show_title: bool = True) -> bool:
    """Real traffic lights over full-height Qt content. With `toolbar`, the title bar grows
    to a unified toolbar band (like Pages) whose free area Qt fills with its own buttons."""
    if not OK:
        return False
    try:
        win = _ns_view(widget).window()
        if win is None:
            return False
        win.setStyleMask_(win.styleMask() | AppKit.NSWindowStyleMaskFullSizeContentView)
        win.setTitlebarAppearsTransparent_(True)
        if toolbar and win.toolbar() is None:
            delegate = _delegate_class().alloc().init()
            _TOOLBARS[int(widget.winId())] = delegate
            widget.destroyed.connect(lambda *_, k=int(widget.winId()): _TOOLBARS.pop(k, None))
            tb = AppKit.NSToolbar.alloc().initWithIdentifier_(f"DragShiftToolbar{int(widget.winId())}")
            tb.setAllowsUserCustomization_(False)
            tb.setDisplayMode_(AppKit.NSToolbarDisplayModeIconAndLabel)
            tb.setDelegate_(delegate)
            win.setToolbar_(tb)
            win.setToolbarStyle_(3)          # NSWindowToolbarStyleUnified
        if subtitle:
            win.setSubtitle_(subtitle)
        if not show_title:
            win.setTitleVisibility_(AppKit.NSWindowTitleHidden)
        return True
    except Exception:
        return False


def set_subtitle(widget, text: str):
    if OK:
        try:
            win = _ns_view(widget).window()
            if win is not None:
                win.setSubtitle_(text)
        except Exception:
            pass


# ---------------------------------------------------------------- native toolbar
# Qt widgets can't sit in the title bar (AppKit takes those clicks), so toolbar buttons are
# real NSToolbar items: SF Symbols with labels under them, Liquid Glass on macOS 26.
SELECT_ONE, SELECT_ANY, MOMENTARY = 0, 1, 2       # NSToolbarItemGroupSelectionMode
_DELEGATE_CLASS = None
_TOOLBARS: dict[int, object] = {}                  # winId -> delegate (keeps it alive)


def _delegate_class():
    global _DELEGATE_CLASS
    if _DELEGATE_CLASS is None:
        import objc

        class DragShiftToolbarDelegate(AppKit.NSObject):
            def init(self):
                self = objc.super(DragShiftToolbarDelegate, self).init()
                if self is not None:
                    self.items, self.order, self.callbacks = {}, [], {}
                return self

            def toolbarAllowedItemIdentifiers_(self, toolbar):
                return [AppKit.NSToolbarFlexibleSpaceItemIdentifier] + list(self.order)

            def toolbarDefaultItemIdentifiers_(self, toolbar):
                return [AppKit.NSToolbarFlexibleSpaceItemIdentifier] + list(self.order)

            def toolbar_itemForItemIdentifier_willBeInsertedIntoToolbar_(self, toolbar, ident, flag):
                return self.items.get(str(ident))

            def groupClicked_(self, sender):
                cb = self.callbacks.get(str(sender.itemIdentifier()))
                if cb is not None:
                    cb(int(sender.selectedIndex()))

        _DELEGATE_CLASS = DragShiftToolbarDelegate
    return _DELEGATE_CLASS


def toolbar_add_group(widget, ident: str, items: list[tuple[str, str]], mode: int, on_click) -> bool:
    """Append a group of toolbar items: items = [(sf_symbol, label)], on_click(index)."""
    if not OK:
        return False
    try:
        delegate = _TOOLBARS.get(int(widget.winId()))
        win = _ns_view(widget).window()
        if delegate is None or win is None or win.toolbar() is None:
            return False
        images = []
        for sym, label in items:
            img = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(sym, label)
            images.append(img if img is not None else AppKit.NSImage.alloc().initWithSize_((16, 16)))
        group = AppKit.NSToolbarItemGroup.groupWithItemIdentifier_images_selectionMode_labels_target_action_(
            ident, images, mode, [label for _, label in items], delegate, "groupClicked:")
        for sub, (_, label) in zip(group.subitems(), items):
            sub.setToolTip_(label)
        delegate.items[ident] = group
        delegate.order.append(ident)
        delegate.callbacks[ident] = on_click
        tb = win.toolbar()
        tb.insertItemWithItemIdentifier_atIndex_(ident, len(tb.items()))
        return True
    except Exception:
        return False


def _group(widget, ident: str):
    delegate = _TOOLBARS.get(int(widget.winId())) if OK else None
    return delegate.items.get(ident) if delegate is not None else None


def toolbar_set_selected(widget, ident: str, index: int, on: bool):
    g = _group(widget, ident)
    if g is not None:
        g.setSelected_atIndex_(bool(on), index)


def toolbar_set_label(widget, ident: str, index: int, label: str):
    g = _group(widget, ident)
    if g is not None:
        subs = g.subitems()
        if 0 <= index < len(subs):
            subs[index].setLabel_(label)


def toolbar_set_enabled(widget, ident: str, index: int, on: bool):
    g = _group(widget, ident)
    if g is not None:
        subs = g.subitems()
        if 0 <= index < len(subs):
            subs[index].setEnabled_(bool(on))


def titlebar_height(widget) -> int:
    """Height of the title bar / unified toolbar band in points (0 if unknown)."""
    if not OK:
        return 0
    try:
        win = _ns_view(widget).window()
        return int(round(win.frame().size.height - win.contentLayoutRect().size.height))
    except Exception:
        return 0


# ---------------------------------------------------------------- screen capture
LAST_SWEEP: list[str] = []


def drag_window_ids(cursor: tuple[int, int] | None = None) -> list[int]:
    """The drag picture under the cursor: windows at the dragging level, or any small
    window of another app sitting right on the cursor above normal windows."""
    LAST_SWEEP.clear()
    if not OK:
        return []
    try:
        level = Quartz.CGWindowLevelForKey(Quartz.kCGDraggingWindowLevelKey)
        infos = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID) or []
        me = os.getpid()
        ids = []
        for i in infos:
            layer = int(i.get("kCGWindowLayer", 0))
            b = i.get("kCGWindowBounds", {}) or {}
            x, y, w, h = (float(b.get(k, 0)) for k in ("X", "Y", "Width", "Height"))
            near = cursor is not None and x - 40 <= cursor[0] <= x + w + 40 and y - 40 <= cursor[1] <= y + h + 40
            if near:
                LAST_SWEEP.append(f"{i.get('kCGWindowOwnerName')} layer={layer} {int(w)}x{int(h)}")
            if int(i.get("kCGWindowOwnerPID", 0)) == me:
                continue
            if layer >= level or (near and layer > 0 and w < 700 and h < 700):
                ids.append(int(i["kCGWindowNumber"]))
        return ids
    except Exception as e:
        LAST_SWEEP.append(f"error {e}")
        return []


def capture(x: int, y: int, w: int, h: int, below_window: int = 0):
    """Capture a screen rect (global points, top-left origin) as a PIL RGB image.
    below_window: capture only what is beneath that window (excludes it and anything above)."""
    if not OK:
        return None
    from PIL import Image
    rect = Quartz.CGRectMake(x, y, w, h)
    if below_window:
        opt, wid = Quartz.kCGWindowListOptionOnScreenBelowWindow, below_window
    else:
        loc = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
        drags = drag_window_ids((int(loc.x), int(loc.y)))
        if drags:
            # everything below the lowest drag window = no drag picture in the glass
            opt, wid = Quartz.kCGWindowListOptionOnScreenBelowWindow, min(drags)
        else:
            opt, wid = Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID
    try:
        img = Quartz.CGWindowListCreateImage(rect, opt, wid, Quartz.kCGWindowImageBestResolution)
    except Exception:
        return None
    if img is None:
        return None
    width, height = Quartz.CGImageGetWidth(img), Quartz.CGImageGetHeight(img)
    bpr = Quartz.CGImageGetBytesPerRow(img)
    data = Quartz.CGDataProviderCopyData(Quartz.CGImageGetDataProvider(img))
    im = Image.frombuffer("RGBA", (width, height), bytes(data), "raw", "BGRA", bpr, 1)
    return im.convert("RGB").resize((w, h), Image.LANCZOS)


def has_screen_recording() -> bool:
    try:
        return bool(Quartz.CGPreflightScreenCaptureAccess())
    except Exception:
        return True


def request_screen_recording() -> bool:
    try:
        return bool(Quartz.CGRequestScreenCaptureAccess())
    except Exception:
        return False


# ---------------------------------------------------------------- sharing
def airdrop(paths: list[str]) -> bool:
    """Open the AirDrop sheet for these files and folders. False when AirDrop can't take
    them (AirDrop off, no Wi-Fi/Bluetooth, or not supported on this Mac)."""
    if not OK:
        return False
    try:
        service = AppKit.NSSharingService.sharingServiceNamed_(AppKit.NSSharingServiceNameSendViaAirDrop)
        urls = [AppKit.NSURL.fileURLWithPath_(str(p)) for p in paths]
        if service is None or not service.canPerformWithItems_(urls):
            return False
        activate()   # the sheet belongs to us: a menu-bar app has to come forward first
        service.performWithItems_(urls)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- start at login
AGENT = Path.home() / "Library" / "LaunchAgents" / "com.dragshift.app.plist"


def _program() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--quiet"]
    return [sys.executable, "-m", "dragshift", "--quiet"]


def autostart_enabled() -> bool:
    return AGENT.exists()


def set_autostart(on: bool):
    if on:
        AGENT.parent.mkdir(parents=True, exist_ok=True)
        with open(AGENT, "wb") as f:
            plistlib.dump({"Label": "com.dragshift.app", "ProgramArguments": _program(),
                           "RunAtLoad": True, "ProcessType": "Interactive"}, f)
    elif AGENT.exists():
        AGENT.unlink()


# ---------------------------------------------------------------- Keynote / Pages / textutil
IWORK_APPS = ("Keynote", "Pages")
IWORK_TIMEOUT = 300  # seconds; a long deck takes a while, and a stuck dialog must not hang the job


def iwork_app(name: str) -> Path | None:
    """Where Keynote or Pages is installed, or None."""
    for base in (Path("/Applications"), Path.home() / "Applications"):
        app = base / f"{name}.app"
        if app.is_dir():
            return app
    return None


def _iwork_script(name: str) -> list[str]:
    """AppleScript lines that open argv[1] in Keynote/Pages and export argv[2] as PDF. The app
    name comes from IWORK_APPS (never from a file); file paths only ever arrive as arguments."""
    if name not in IWORK_APPS:
        raise ValueError(name)
    return ["on run argv",
            "set src to POSIX file (item 1 of argv)",
            "set dst to POSIX file (item 2 of argv)",
            f'tell application "{name}"',
            "set d to open src",
            "export d to dst as PDF",
            "close d saving no",
            "end tell",
            "end run"]


def iwork_export_pdf(name: str, src: Path, out: Path) -> bool:
    """Export `src` to the PDF `out` through Keynote or Pages. False if the app is missing,
    automation was denied, or the export failed, so the caller can fall back."""
    import subprocess
    if iwork_app(name) is None:
        return False
    cmd = ["osascript"]
    for line in _iwork_script(name):
        cmd += ["-e", line]
    cmd += ["--", str(Path(src).resolve()), str(Path(out).resolve())]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=IWORK_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0 and Path(out).is_file() and Path(out).stat().st_size > 0


def textutil_html(src: Path) -> str | None:
    """RTF/RTFD as HTML through macOS's textutil (keeps bold, italics, lists and tables)."""
    import subprocess
    try:
        r = subprocess.run(["/usr/bin/textutil", "-convert", "html", "-stdout", "--", str(Path(src).resolve())],
                           capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0 or not r.stdout:
        return None
    return r.stdout.decode("utf-8", "replace")
