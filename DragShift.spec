# PyInstaller spec. ONEFILE=1 -> portable single exe; otherwise a folder for the installer.
import os
from PyInstaller.utils.hooks import collect_all, collect_data_files

onefile = os.environ.get("ONEFILE") == "1"
heif_datas, heif_bins, heif_hidden = collect_all("pillow_heif")
# python-pptx and python-docx load their blank templates from package data at run time
office_datas = collect_data_files("pptx") + collect_data_files("docx")
# They find them as "<subpackage>/../templates", which only resolves if the subpackage folder exists
# on disk. Frozen code lives in the PYZ, so ship each subpackage's __init__.py to create the folder.
for _pkg, _sub in (("pptx", "oxml"), ("pptx", "shapes"), ("docx", "parts")):
    office_datas += [(src, dst) for src, dst in collect_data_files(_pkg, include_py_files=True)
                     if dst.replace("\\", "/") == f"{_pkg}/{_sub}" and src.endswith("__init__.py")]

# ffmpeg/ffprobe fetched by packaging/ffmpeg/fetch.py (CI). They land in an "ffmpeg" folder that
# media.find_tool checks first; source runs use the ffmpeg on PATH instead. The portable onefile
# exe leaves them out: it would unpack ~270 MB to a temp folder on every launch.
FFMPEG_DIR = "vendor/ffmpeg"
ffmpeg_bins, ffmpeg_datas = [], []
if os.path.isdir(FFMPEG_DIR) and not onefile:
    for name in sorted(os.listdir(FFMPEG_DIR)):
        path = os.path.join(FFMPEG_DIR, name)
        if name.lower().endswith((".txt", ".md")) or name.startswith("COPYING"):
            ffmpeg_datas.append((path, "ffmpeg"))
        elif name.startswith(("ffmpeg", "ffprobe")):
            ffmpeg_bins.append((path, "ffmpeg"))

a = Analysis(
    ["run.py"],
    datas=[("dragshift/assets", "dragshift/assets")] + heif_datas + ffmpeg_datas + office_datas,
    binaries=heif_bins + ffmpeg_bins,
    # QtSvg rasterises SVG input (svg.py); zxingcpp reads QR codes (qr.py)
    hiddenimports=["win32com.client", "PySide6.QtNetwork", "PySide6.QtSvg", "zxingcpp", "AppKit", "Quartz",
                   "objc"] + heif_hidden,
    excludes=["tkinter", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtQml",
              "PySide6.QtQuick", "PySide6.Qt3DCore", "PySide6.QtMultimedia", "PySide6.QtCharts",
              "PySide6.QtDataVisualization", "PySide6.QtPdf", "pandas", "yaml", "PySide6.QtSql", "PySide6.QtTest",
              "PySide6.QtOpenGL", "PySide6.QtSvgWidgets", "PySide6.QtDesigner", "matplotlib", "scipy"],
)

DROP = ("Qt6Quick", "Qt6Qml", "Qt6Pdf", "Qt6WebEngine", "Qt63D", "Qt6Multimedia", "Qt6Charts",
        "Qt6VirtualKeyboard", "Qt6Designer", "Qt6ShaderTools", "Qt6Sql", "Qt6Test", "opengl32sw",
        "Qt6OpenGL", "Qt6DataVisualization", "Qt6Positioning", "Qt6Sensors", "Qt6SerialPort",
        "Qt6Bluetooth", "Qt6Nfc", "Qt6RemoteObjects", "Qt6Scxml", "Qt6SpatialAudio", "Qt6TextToSpeech",
        "Qt6WebChannel", "Qt6WebSockets", "qtquick", "qml",
        "libgtk-3", "libgdk-3", "libqgtk3", "libatk-")
a.binaries = [b for b in a.binaries if not any(d.lower() in b[0].lower() for d in DROP)]
a.datas = [d for d in a.datas if "translations" not in d[0].replace("\\", "/")]

pyz = PYZ(a.pure)
import sys
IS_MAC = sys.platform == "darwin"
common = dict(name="DragShift", console=False, upx=False)
IS_LINUX = sys.platform.startswith("linux")
if IS_MAC:
    common.update(icon="dragshift/assets/dragshift.icns")
elif IS_LINUX:
    common.update(name="dragshift")       # icon comes from the .desktop file / hicolor theme
else:
    common.update(icon="dragshift/assets/dragshift.ico", version="installer/version_info.txt")

if IS_MAC:
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, argv_emulation=False, **common)
    coll = COLLECT(exe, a.binaries, a.datas, name="DragShift", upx=False)
    app = BUNDLE(
        coll,
        name="DragShift.app",
        icon="dragshift/assets/dragshift.icns",
        bundle_identifier="com.dragshift.app",
        info_plist={
            "CFBundleName": "DragShift",
            "CFBundleDisplayName": "DragShift",
            "CFBundleShortVersionString": "1.1.0",
            "CFBundleVersion": "1.1.0",
            "LSUIElement": True,                 # menu-bar app, no Dock icon
            "LSMinimumSystemVersion": "11.0",
            "NSHighResolutionCapable": True,
            "NSRequiresAquaSystemAppearance": False,
            # show up in Finder's "Open With" for any file, and accept drops on the app icon
            "CFBundleDocumentTypes": [{
                "CFBundleTypeName": "Any file",
                "CFBundleTypeRole": "Viewer",
                "LSHandlerRank": "Alternate",
                "LSItemContentTypes": ["public.item", "public.content", "public.data"],
            }],
            # Finder > right-click > Services > "Open in DragShift" opens the wheel at the pointer
            # (platform_mac.register_services). Give it a shortcut in System Settings > Keyboard >
            # Keyboard Shortcuts > Services to open the wheel from the keyboard; no Accessibility
            # permission needed. NSRequiredContext makes it enabled by default.
            "NSServices": [{
                "NSMenuItem": {"default": "Open in DragShift"},
                "NSMessage": "openFiles",
                "NSPortName": "DragShift",
                "NSRequiredContext": {},
                "NSSendFileTypes": ["public.item"],
            }],
        },
    )
elif IS_LINUX:
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, **common)
    coll = COLLECT(exe, a.binaries, a.datas, name="dragshift", upx=False)  # no strip: breaks OpenBLAS with Debian 12 binutils
elif onefile:
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], **common)
else:
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, **common)
    coll = COLLECT(exe, a.binaries, a.datas, name="DragShift", upx=False)
