@echo off
REM Build script for Alignment Viewer EXE using PyInstaller (gsv86lib version)

echo Installing dependencies (if needed)...

REM Install PyInstaller + required libs
pip install pyinstaller pyqt6 pyqtgraph numpy pyyaml

REM Install gsv86lib directly from GitHub (not a PyPI package!)
pip install --upgrade git+https://github.com/me-systeme/gsv86lib.git

echo.
echo Building AlignmentViewer_GSV86LIB.exe...

pyinstaller ^
  --onefile ^
  --noconsole ^
  --name AlignmentViewer_GSV86LIB ^
  --add-data "alignment_config.yaml;." ^
  alignment_viewer.py

echo.
echo Build finished.
echo You can find AlignmentViewer_GSV86LIB.exe in the "dist" folder.
pause

