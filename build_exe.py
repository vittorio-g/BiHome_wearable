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
    # Add Viewer/ to the module search path so lsl_viewer can be imported
    "--paths", os.path.join(HERE, "Viewer"),
    "--hidden-import", "lsl_viewer",
    # Native DLLs / data files that need explicit collection
    "--collect-all", "pylsl",
    "--collect-all", "pyqtgraph",
    "--collect-all", "numpy",         # numpy 2.x + PyInstaller 6.x needs this
    "--collect-all", "bleak",
    "--collect-submodules", "brainflow",
    # Exclude heavy unused packages — pyqtgraph tries to import matplotlib
    # for optional features, pulling in a broken matplotlib-vs-numpy combo.
    "--exclude-module", "matplotlib",
    "--exclude-module", "tkinter",
    "--exclude-module", "PyQt6",
    "--exclude-module", "PySide2",
    "--exclude-module", "PySide6",
    "--exclude-module", "scipy",
    "--exclude-module", "pandas",
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
