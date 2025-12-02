"""
alignment_viewer.py
-------------------------------

Real-time alignment and bending visualization tool for ASTM E1012 compliance.
Acquires strain-gauge data from a ME-Systeme GSV-8 device via the gsv86lib
(serial communication, streaming mode with StartTransmission) and visualizes
bending strain on two orthogonal planes with color-coded alignment classes.

Features:
- Live data capture (8 channels, streaming via gsv86lib.StartTransmission)
- Axial and bending strain computation (see axial_bending.py)
- Color-coded ASTM E1012 alignment classes (Class 1 … Out of class)
- High-performance live visualization using PyQtGraph
- Optional auto-scaling of bending radius
- Robust device initialization and safe shutdown

This is the gsv86lib streaming variant, analogous to the DLL-based version
(https://github.com/me-systeme/GSV-8_AlignmentProbe) using
GSV86startTX / GSV86readMultiple.

Author: <Name/Firma>
"""

import sys
import time
import signal

from pathlib import Path

import numpy as np
import yaml

from PyQt6 import QtWidgets, QtCore, QtGui
import pyqtgraph as pg
import pyqtgraph.exporters

# gsv86 library from https://github.com/me-systeme/gsv86lib
from gsv86lib import gsv86

pg.setConfigOption('background', 'w')   # white background
pg.setConfigOption('foreground', 'k')   # black lines/text

from axial_bending import axial_bending

# -----------------------------
# Configuration loading (external + embedded fallback)
# -----------------------------
CONFIG_FILENAME = "alignment_config.yaml"


def get_base_dirs():
    """
    Returns:
    - exe_dir: directory where the EXE (or script) is located
    - bundle_dir: directory where PyInstaller unpacks embedded files
                  (for normal Python execution: same as exe_dir)
    """
    if getattr(sys, "frozen", False):
        # Running inside a PyInstaller onefile EXE
        exe_dir = Path(sys.argv[0]).resolve().parent
        bundle_dir = Path(sys._MEIPASS)
    else:
        # Running as normal Python script
        exe_dir = Path(__file__).resolve().parent
        bundle_dir = exe_dir

    return exe_dir, bundle_dir


def load_config_or_exit():
    """
    1) Try external 'alignment_config.yaml' next to the EXE.
    2) If not found, try embedded default (PyInstaller bundle).
    3) If neither exists or loading fails, print error and exit.
    """
    exe_dir, bundle_dir = get_base_dirs()

    external_path = exe_dir / CONFIG_FILENAME
    internal_path = bundle_dir / CONFIG_FILENAME

    if external_path.exists():
        path = external_path
        source = f"external file ({external_path})"
    elif internal_path.exists():
        path = internal_path
        source = f"internal default (embedded: {internal_path.name})"
    else:
        sys.stderr.write(
            "Configuration file not found.\n"
            f"Searched locations:\n- {external_path}\n- {internal_path}\n"
        )
        sys.exit(1)

    try:
        with path.open("r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except Exception as e:
        sys.stderr.write(
            f"Failed to load configuration file:\n{path}\n\nError:\n{e}\n"
        )
        sys.exit(1)

    return cfg, source

# Load config once at import time
CONFIG, CONFIG_SOURCE = load_config_or_exit()

# Device configuration
DEVICE_CFG = CONFIG["device"]

SAMPLE_FREQUENCY = float(DEVICE_CFG.get("sample_frequency", 50.0))
BAUDRATE = int(DEVICE_CFG.get("baudrate", 115200))

# Prefer explicit serial_port; fall back to COM{com_port}
SERIAL_PORT = DEVICE_CFG.get("serial_port")
COM_PORT_NUM = DEVICE_CFG.get("com_port")

if SERIAL_PORT:
    PORT = str(SERIAL_PORT)
elif COM_PORT_NUM is not None:
    PORT = f"COM{int(COM_PORT_NUM)}"
else:
    raise KeyError(
        "device.serial_port or device.com_port must be defined in alignment_config.yaml"
    )


# Channels
SECTION_MAP = CONFIG["channels"]["section_map"]
CHANNELS = sorted({ch for sec in SECTION_MAP.values() for ch in sec.values()})

# View / GUI
AUTO_SCALE = bool(CONFIG["view"]["auto_scale"])
R_FIXED = float(CONFIG["view"]["fixed_radius"])
REFRESH_MS = int(CONFIG["view"]["refresh_ms"])
MULT_FRAMES = int(CONFIG["view"]["mult_frames"])

# Alignment classes
_ALIGN_CFG = CONFIG["alignment_classes"]
ALIGNMENT_CLASSES_AXIAL_SMALL = [
    (c["name"], float(c["eps_b_mag"]), tuple(c["color"]))
    for c in _ALIGN_CFG["classes_axial_strain_small"]
]

ALIGNMENT_CLASSES_AXIAL_BIG = [
    (c["name"], float(c["max_percent"]), tuple(c["color"]))
    for c in _ALIGN_CFG["classes_axial_strain_big"]
]

_oc = _ALIGN_CFG["out_of_class"]
ALIGNMENT_OUT_OF_CLASS = (_oc["name"], tuple(_oc["color"]))


def classify_alignment(value: float, eps_ax: float):
    """
    Return (class_name, (r,g,b)) for a given magnitude of
    bending strain or percent_bending.
    """
    if eps_ax < 1000:
        for name, limit, rgb in ALIGNMENT_CLASSES_AXIAL_SMALL:
            if value <= limit:
                return name, rgb
    else:
        for name, limit, rgb in ALIGNMENT_CLASSES_AXIAL_BIG:
            if value <= limit:
                return name, rgb
    name, rgb = ALIGNMENT_OUT_OF_CLASS
    return name, rgb

# -----------------------------
# GSV-8 device via gsv86lib
# -----------------------------
def init_device() -> gsv86:
    """
    Create and configure the GSV-8 device via gsv86lib.

    We follow the pattern from example_record.py:
        dev = gsv86("/dev/ttyACM0", 230400)
        measurement = dev.ReadValue()
        print(measurement.getChannel1())
    """
    print(f"Connecting to GSV-8 via gsv86lib on {PORT} @ {BAUDRATE} baud ...")
    dev = gsv86(PORT, BAUDRATE)

    # Optional: set data rate if supported by the device
    try:
        dev.writeDataRate(SAMPLE_FREQUENCY)
        print(f"Requested device data rate: {SAMPLE_FREQUENCY:.3f} Hz")
    except Exception as e:
        print(f"Warning: writeDataRate({SAMPLE_FREQUENCY}) failed: {e}")

    # Start continuous transmission (analog to GSV86startTX)
    try:
        dev.StartTransmission()
        print("StartTransmission() called – device is now streaming.")
    except Exception as e:
        print(f"Error: StartTransmission() failed: {e}")
        # you may choose to exit here if streaming is mandatory
        # sys.exit(1)

    # small delay to allow buffer fill (optional)
    time.sleep(0.05)
    print("Device initialized via gsv86lib.")

    return dev

# -----------------------------
# GUI – PyQtGraph
# -----------------------------
class BendingView(QtWidgets.QWidget):
    def __init__(self, device: gsv86, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Bending vectors – planes A & B")

        self._dev = device  # gsv86 device instance

        # --- view settings (runtime adjustable) ---
        self.auto_scale = AUTO_SCALE
        self.r_fixed = R_FIXED
        self.refresh_ms = REFRESH_MS
        self.mult_frames = MULT_FRAMES

        self._last_vals = {ch: 0.0 for ch in CHANNELS}  # last complete 8-channel frame
        self._empty_reads = 0  # counter for “no values available”
        self._last_vecA = (0.0, 0.0)
        self._last_vecB = (0.0, 0.0)

        # Shortcuts for quitting
        QtGui.QShortcut(QtGui.QKeySequence("Esc"), self, activated=self.close)
        QtGui.QShortcut(QtGui.QKeySequence("Ctrl+Q"), self, activated=self.close)

        # -------------------------------------------------
        # Main layout: [ Left: Info + Controls | Right: Plots ]
        # -------------------------------------------------
        main_layout = QtWidgets.QHBoxLayout(self)
        main_layout.setContentsMargins(8, 8, 8, 8)
        main_layout.setSpacing(8)

        # ============================
        # Left column: info + controls
        # ============================
        left_widget = QtWidgets.QWidget()
        left_widget.setMinimumWidth(220)
        left_widget.setMaximumWidth(280)
        left_layout = QtWidgets.QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(10)

        # ---- Plane A info box ----
        boxA = QtWidgets.QGroupBox("Plane A")
        boxA_layout = QtWidgets.QVBoxLayout(boxA)
        boxA_layout.setContentsMargins(8, 8, 8, 8)
        boxA_layout.setSpacing(4)

        self.infoA = QtWidgets.QLabel()
        self.infoA.setAlignment(QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignTop)
        self.infoA.setStyleSheet(
            "font-family: Consolas, 'Courier New', monospace; font-size: 10pt;"
        )
        self.infoA.setMinimumWidth(200)
        self.infoA.setWordWrap(True)
        boxA_layout.addWidget(self.infoA)

        # ---- Plane B info box ----
        boxB = QtWidgets.QGroupBox("Plane B")
        boxB_layout = QtWidgets.QVBoxLayout(boxB)
        boxB_layout.setContentsMargins(8, 8, 8, 8)
        boxB_layout.setSpacing(4)

        self.infoB = QtWidgets.QLabel()
        self.infoB.setAlignment(QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignTop)
        self.infoB.setStyleSheet(
            "font-family: Consolas, 'Courier New', monospace; font-size: 10pt;"
        )
        self.infoB.setMinimumWidth(200)
        self.infoB.setWordWrap(True)
        boxB_layout.addWidget(self.infoB)

        # ---- Controls box (buttons) ----
        boxControls = QtWidgets.QGroupBox("Controls")
        controls_layout = QtWidgets.QVBoxLayout(boxControls)
        controls_layout.setContentsMargins(8, 8, 8, 8)
        controls_layout.setSpacing(6)

        self.btn_save = QtWidgets.QPushButton("Save PNG")
        self.btn_settings = QtWidgets.QPushButton("View settings")
        self.btn_save.setMinimumHeight(26)
        self.btn_settings.setMinimumHeight(26)

        controls_layout.addWidget(self.btn_save)
        controls_layout.addWidget(self.btn_settings)

        # assemble left column
        left_layout.addWidget(boxA)
        left_layout.addWidget(boxB)
        left_layout.addWidget(boxControls)
        left_layout.addStretch(1)

        main_layout.addWidget(left_widget, stretch=0)  # schmale Spalte

        # Connect buttons
        self.btn_save.clicked.connect(self._save_png)
        self.btn_settings.clicked.connect(self._open_view_settings_dialog)

        # ============================
        # Right side: plot area
        # ============================
        right_widget = QtWidgets.QWidget()
        right_layout = QtWidgets.QVBoxLayout(right_widget)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(4)

        pg.setConfigOptions(antialias=True)
        self.glw = pg.GraphicsLayoutWidget()
        self.glw.setMinimumWidth(1000)
        right_layout.addWidget(self.glw, 1)

        main_layout.addWidget(right_widget, stretch=1)  # breiter Plotbereich

        # Two plots (A/B) in the center
        self.axA = self._make_polar_plot("Plane A")
        self.axB = self._make_polar_plot("Plane B")

        # Points (instead of vectors)
        self.pointA = pg.ScatterPlotItem(size=9)
        self.pointB = pg.ScatterPlotItem(size=9)
        self.axA.addItem(self.pointA)
        self.axB.addItem(self.pointB)

        # Circle items + scaling
        self.rmin = 1e-6
        self.rA = self.r_fixed if not self.auto_scale else 1.0
        self.rB = self.r_fixed if not self.auto_scale else 1.0
        self.circleA = self._add_circle(self.axA, self.rA)
        self.circleB = self._add_circle(self.axB, self.rB)

        # Initial axis scaling and (optional) text positions
        self._update_view_limits()

        # Timer (GUI thread) – calls update_view
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.update_view)
        self.timer.start(self.refresh_ms)

        # Throttle console output
        self._last_print = 0.0

    # -----------------------------
    # UI helper methods
    # -----------------------------
    def _save_png(self):
        # generate file name
        ts = time.strftime("%Y%m%d_%H%M%S")
        filename = f"alignment_view_{ts}.png"

        # Grab the entire window (including plots + info panels)
        pixmap = self.grab()
        ok = pixmap.save(filename)

        if ok:
            QtWidgets.QMessageBox.information(self, "Saved", f"PNG saved:\n{filename}")
        else:
            QtWidgets.QMessageBox.warning(self, "Error", "Could not save PNG.")

    def _open_view_settings_dialog(self):
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("View settings")

        form = QtWidgets.QFormLayout(dlg)

        # Auto-scale
        chk_auto = QtWidgets.QCheckBox()
        chk_auto.setChecked(self.auto_scale)
        form.addRow("Auto scale", chk_auto)

        # Fixed radius
        spin_radius = QtWidgets.QDoubleSpinBox()
        spin_radius.setDecimals(3)
        spin_radius.setMinimum(0.0001)
        spin_radius.setMaximum(1e9)
        spin_radius.setValue(self.r_fixed)
        form.addRow("Fixed radius", spin_radius)

        # Refresh interval
        spin_refresh = QtWidgets.QSpinBox()
        spin_refresh.setMinimum(10)
        spin_refresh.setMaximum(5000)
        spin_refresh.setSingleStep(10)
        spin_refresh.setValue(self.refresh_ms)
        form.addRow("Refresh [ms]", spin_refresh)

        # OK / Cancel buttons
        btn_box = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel,
            parent=dlg,
        )
        form.addRow(btn_box)

        btn_box.accepted.connect(dlg.accept)
        btn_box.rejected.connect(dlg.reject)

        if dlg.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            # Apply settings
            self.apply_view_settings(
                auto_scale=chk_auto.isChecked(),
                r_fixed=spin_radius.value(),
                refresh_ms=spin_refresh.value(),
            )

    def apply_view_settings(self, auto_scale: bool, r_fixed: float, refresh_ms: int):
        self.auto_scale = auto_scale
        self.r_fixed = r_fixed
        self.refresh_ms = refresh_ms

        # Update timer interval
        self.timer.setInterval(self.refresh_ms)

        # Recompute radii / axes
        self._update_view_limits()

    def _make_polar_plot(self, title: str):
        p = self.glw.addPlot(title=title)
        self.glw.nextColumn()
        p.setAspectLocked(True)               # keep circle round
        p.showGrid(x=True, y=True, alpha=0.2)
        p.setMouseEnabled(x=False, y=False)   # no zoom/pan by mouse
        p.hideButtons()                       # hide auto-range button
        # Cross axes
        p.addLine(x=0, pen=pg.mkPen(width=1))
        p.addLine(y=0, pen=pg.mkPen(width=1))
        return p

    def _add_circle(self, plot, radius: float):
        # Circle as GraphicsEllipseItem
        item = QtWidgets.QGraphicsEllipseItem(-radius, -radius, 2*radius, 2*radius)
        item.setPen(pg.mkPen(width=2))
        plot.addItem(item)
        return item

    def _set_circle_radius(self, item, radius: float):
        item.setRect(-radius, -radius, 2*radius, 2*radius)

    def _apply_limits(self, plot, r: float):
        # add a small margin so the circle is not on the border
        margin_factor = 1.05  # 5% extra space
        lim = r * margin_factor
        plot.setXRange(-lim, lim, padding=0.0)
        plot.setYRange(-lim, lim, padding=0.0)

    def _update_view_limits(self):
        """
        Keep circle radius and axis limits in sync for both plots,
        depending on auto_scale / fixed radius.
        """
        if self.auto_scale:
            # make sure radius is at least rmin
            rA = max(self.rA, self.rmin)
            rB = max(self.rB, self.rmin)
        else:
            # fixed scaling: both plots use r_fixed
            rA = rB = max(self.r_fixed, self.rmin)
            self.rA = rA
            self.rB = rB

        # apply circle geometry
        self._set_circle_radius(self.circleA, rA)
        self._set_circle_radius(self.circleB, rB)

        # apply visible ranges
        self._apply_limits(self.axA, rA)
        self._apply_limits(self.axB, rB)

        self._place_info_texts()

    def _place_info_texts(self):
        # If you want text inside the circles, you could position it here
        rA = self.rA if self.auto_scale else self.r_fixed
        rB = self.rB if self.auto_scale else self.r_fixed
        # Example (currently commented out because no TextItems are used):
        # self.txtA.setPos(-rA, rA)
        # self.txtB.setPos(-rB, rB)

    # -----------------------------
    # Data acquisition via gsv86lib
    # -----------------------------
    def _read_values(self):
        """
        Read all newly received measurement frames using ReadMultiple() and return
        a single consolidated channel dictionary.

        Behavior:
        - Fetches all frames accumulated since the previous call (up to mult_frames).
        - Uses only the most recent frame for live display (minimal latency).
        - Maps gsv86lib keys "channel0".."channel7" to CHANNELS 1..8.
        - Falls back to the last valid values if no new frame is available.
        - Preserves robustness against unexpected frame structure.

        Returns
        -------
        dict : {channel_number (int): value (float)}
            Latest complete measurement for all configured channels.
        """
        try:
            frames = self._dev.ReadMultiple(max_count=self.mult_frames)
        except Exception as e:
            self._empty_reads += 1
            # On error, return last valid values
            print(f"ReadMultiple() error: {e}")
            return dict(self._last_vals)

        if frames is None:
            self._empty_reads += 1
            return dict(self._last_vals)

        # Use only the most recent frame
        last_frame = frames[-1]

        try:
            _ts, values, inputOverload, sixAxisError = last_frame
        except ValueError:
            print("Unexpected frame format in ReadMultiple()")
            return dict(self._last_vals)
        
        if not isinstance(values, dict):
            return dict(self._last_vals)
        
        vals = {}
        try:
            for ch in CHANNELS:
                key = f"channel{ch-1}"  # GSV-8 uses zero-based channel keys
                if key in values:
                    vals[ch] = float(values[key])
                else:
                    vals[ch] = self._last_vals.get(ch, 0.0)
        except Exception as e:
            self._empty_reads += 1
            print(f"Error extracting channels from measurement: {e}")
            return dict(self._last_vals)
        
        self._empty_reads = 0
        self._last_vals = dict(vals)
        return vals

    def _compute_sections(self, vals):
        def section_vals(sec_key):
            m = SECTION_MAP[sec_key]
            return axial_bending(vals[m["e0"]], vals[m["e90"]], vals[m["e180"]], vals[m["e270"]])
        return section_vals("A"), section_vals("B")

    # -----------------------------
    # Main update loop
    # -----------------------------
    def update_view(self):
        try:
            vals = self._read_values()
            resA, resB = self._compute_sections(vals)

            # Helper:
            def _finite(v):
                return np.isfinite(v)

            bxA, byA = resA["eps_bx"], resA["eps_by"]
            bxB, byB = resB["eps_bx"], resB["eps_by"]

            if _finite(bxA) and _finite(byA):
                self._last_vecA = (bxA, byA)
            else:
                bxA, byA = self._last_vecA

            if _finite(bxB) and _finite(byB):
                self._last_vecB = (bxB, byB)
            else:
                bxB, byB = self._last_vecB

            # Alignment class and color
            if resA["eps_ax"] < 1000:
                clsA, colorA = classify_alignment(resA["eps_b_mag"],resA["eps_ax"])
            else:
                clsA, colorA = classify_alignment(resA["percent_bending"],resA["eps_ax"])

            if resB["eps_ax"] < 1000:
                clsB, colorB = classify_alignment(resB["eps_b_mag"], resB["eps_ax"])
            else:
                clsB, colorB = classify_alignment(resB["percent_bending"], resB["eps_ax"])


            brushA = pg.mkBrush(colorA)
            penA   = pg.mkPen(colorA, width=1)
            brushB = pg.mkBrush(colorB)
            penB   = pg.mkPen(colorB, width=1)

            # Set points – colored by alignment class
            self.pointA.setData([bxA], [byA], brush=brushA, pen=penA)
            self.pointB.setData([bxB], [byB], brush=brushB, pen=penB)

            self.infoA.setText(
                f"class = {clsA}\n"
                f"axial strain = {resA['eps_ax']:.6g}\n"
                f"bending mom = {resA['eps_b_mag']:.6g}\n"
                f"phi = {np.degrees(resA['phi']):.1f}°\n"
                f"%bending = {resA['percent_bending']:.2f}%"
            )

            self.infoB.setText(
                f"class = {clsB}\n"
                f"axial strain = {resB['eps_ax']:.6g}\n"
                f"bending mom = {resB['eps_b_mag']:.6g}\n"
                f"phi = {np.degrees(resB['phi']):.1f}°\n"
                f"%bending = {resB['percent_bending']:.2f}%"
            )


            if self.auto_scale:
                # dynamic auto scaling (smooth)
                targetA = max(resA["eps_b_mag"] * 1.2, self.rmin)
                targetB = max(resB["eps_b_mag"] * 1.2, self.rmin)
                # shrink slowly, grow fast
                self.rA = max(self.rA * 0.95, targetA)
                self.rB = max(self.rB * 0.95, targetB)

            # in both cases (auto + fixed) at the end:
            self._update_view_limits()

            # (optional) throttled console output
            now = time.perf_counter()
            if now - self._last_print > 0.5:
                self._last_print = now
                # print(f"A: |eps_b|={resA['eps_b_mag']:.3g}, phi={np.degrees(resA['phi']):.1f}°, %b={resA['percent_bending']:.2f}%   "
                #       f"B: |eps_b|={resB['eps_b_mag']:.3g}, phi={np.degrees(resB['phi']):.1f}°, %b={resB['percent_bending']:.2f}%")

        except Exception as e:
            # Show error in a dialog (optional) and stop UI updates
            QtWidgets.QMessageBox.critical(self, "Error in update", str(e))
            self.timer.stop()

    def closeEvent(self, event):
        # Cleanly release device
        print("Window closed by user.")
        QtWidgets.QApplication.quit()
        super().closeEvent(event)

# -----------------------------
# main
# -----------------------------
def main():
    dev = init_device()
    app = QtWidgets.QApplication(sys.argv)

    # (1) Ctrl+C in the terminal → quit app
    signal.signal(signal.SIGINT, lambda *args: app.quit())

    # (2) Heartbeat so Python processes SIGINT
    heartbeat = QtCore.QTimer()
    heartbeat.start(200)
    heartbeat.timeout.connect(lambda: None)

    def cleanup():
        # follow the vendor examples: just drop the reference
        nonlocal dev
        try:
            dev.StopTransmission()
            print("StopTransmission() called.")
        except Exception as e:
            print(f"Note: StopTransmission() reported: {e}")
        dev = None
        print("Cleaned up. Done.")

    app.aboutToQuit.connect(cleanup)

    w = BendingView(dev)
    w.resize(960, 520)
    w.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()