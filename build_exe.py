"""Build BiHome Wearable.exe with PyInstaller.

Run:
    pip install pyinstaller
    python build_exe.py

Output: dist/BiHome Wearable.exe
The .exe is self-contained — it bundles PyQt5, pylsl, bleak, brainflow,
numpy, pyqtgraph, the Montserrat fonts, and the bihome.ico icon.

It spawns Viewer/lsl_viewer.py as a subprocess at runtime, so both
BiHome_wearable.py and Viewer/lsl_viewer.py must be visible from the
exe's folder.  We therefore use --onedir mode (not --onefile) and
ship both scripts alongside the executable plus the Viewer/, LabRecorder/
folders and required resources.
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# Verify PyInstaller is installed
try:
    import PyInstaller  # noqa
except ImportError:
    print("PyInstaller not installed. Run: pip install pyinstaller")
    sys.exit(1)

# Verify icon exists
icon_path = os.path.join(HERE, "Viewer", "bihome.ico")
if not os.path.isfile(icon_path):
    print(f"Icon missing: {icon_path}")
    sys.exit(1)

args = [
    "pyinstaller",
    "--noconfirm",
    "--clean",
    "--name", "BiHome Wearable",
    "--icon", icon_path,
    "--windowed",                          # no console window on launch
    "--onedir",                            # bundle dependencies in a folder
    # Embed the viewer script + Viewer assets so subprocess can find them
    "--add-data", f"{os.path.join(HERE, 'Viewer')}{os.pathsep}Viewer",
    "--add-data", f"{os.path.join(HERE, 'LabRecorder')}{os.pathsep}LabRecorder",
    # pylsl ships native DLLs in site-packages; PyInstaller usually picks
    # them up automatically via hooks.
    "--collect-all", "pylsl",
    "--collect-all", "pyqtgraph",
    "BiHome_wearable.py",
]
print("Running:", " ".join(args))
rc = subprocess.call(args)
if rc == 0:
    exe = os.path.join(HERE, "dist", "BiHome Wearable", "BiHome Wearable.exe")
    print(f"\nDone. Executable: {exe}")
    print("You can pin this .exe to the taskbar or make a Desktop shortcut.")
else:
    print(f"\nBuild failed with code {rc}.")
    sys.exit(rc)
