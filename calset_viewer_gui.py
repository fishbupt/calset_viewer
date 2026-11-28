#!/usr/bin/env python3
"""
Keysight PNA / PNA-X Calibration File (*.cal) PyQt6 GUI Inspector

Features:
- Open via File dialog, menu, toolbar, or drag-and-drop
- Hierarchical tree view of all 7 reverse-engineered .cal sections with live search
- Interactive Magnitude/Phase (or Re/Im) dual-subplot curve viewer (matplotlib) + data table
- Copy table selections directly to Excel/clipboard (Ctrl+C)
- Export selected item to CSV or export full .cal contents to JSON + CSV
"""

from __future__ import annotations

import csv
import logging
import math
import platform
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TYPE_CHECKING, Any, List, Optional, Tuple

from PyQt6.QtCore import PYQT_VERSION_STR, QT_VERSION_STR, QStandardPaths, Qt, QUrl
from PyQt6.QtGui import (
    QAction,
    QCloseEvent,
    QDesktopServices,
    QDragEnterEvent,
    QDropEvent,
    QKeySequence,
)
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDockWidget,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QStatusBar,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QToolBar,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from calset_operations import (
    DERIVED_FORMAT,
    compare_terms,
    deembed_calset,
    fixture_calibration_ports,
    named_calset_groups,
    read_calset_document,
    read_fixture,
    write_calset_document,
)

if TYPE_CHECKING:
    from parse_pna_cal import ComplexTrace, ParsedCalFile

try:
    from matplotlib.backends.backend_qtagg import (
        FigureCanvasQTAgg,
        NavigationToolbar2QT,
    )
    from matplotlib.figure import Figure

    HAS_MATPLOTLIB = True
except ImportError:
    HAS_MATPLOTLIB = False


FMT_DB_PHASE = "Mag(dB) / Phase(°)"
FMT_RE_IM = "Re / Im"
FMT_LIN_PHASE = "Mag(Lin) / Phase(°)"
LOGGER = logging.getLogger("calset_viewer")
CALSET_FILTER = "校准文件 (*.cal *.calset.json);;Keysight CalSet (*.cal);;Viewer 去嵌结果 (*.calset.json)"


class LogPanelHandler(logging.Handler):
    def __init__(self, output: QPlainTextEdit) -> None:
        super().__init__()
        self.output = output

    def emit(self, record: logging.LogRecord) -> None:
        self.output.appendPlainText(self.format(record))


def complex_to_mag_db_phase(re_v: float, im_v: float) -> Tuple[float, float, float]:
    """Return (mag_linear, mag_db, phase_deg) for a complex sample."""
    mag = math.hypot(re_v, im_v)
    mag_db = 20.0 * math.log10(mag) if mag > 1e-30 else -600.0
    phase_deg = math.degrees(math.atan2(im_v, re_v))
    return mag, mag_db, phase_deg


class CopyableTableWidget(QTableWidget):
    """Read-only QTableWidget that supports Ctrl+C TSV copying to clipboard."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setAlternatingRowColors(True)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.verticalHeader().setDefaultSectionSize(22)

    def copy_selection_to_clipboard(self) -> None:
        ranges = self.selectedRanges()
        if not ranges:
            return
        r = ranges[0]
        lines: List[str] = []
        for row in range(r.topRow(), r.bottomRow() + 1):
            cells: List[str] = []
            for col in range(r.leftColumn(), r.rightColumn() + 1):
                item = self.item(row, col)
                cells.append(item.text() if item else "")
            lines.append("\t".join(cells))
        QApplication.clipboard().setText("\n".join(lines))

    def keyPressEvent(self, event) -> None:  # type: ignore[override]
        if event.matches(QKeySequence.StandardKey.Copy):
            self.copy_selection_to_clipboard()
            return
        super().keyPressEvent(event)


class TracePlotWidget(QWidget):
    """Dual-subplot matplotlib canvas for Magnitude/Phase or Real/Imag display."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        if HAS_MATPLOTLIB:
            self.figure = Figure(figsize=(7, 4.2), dpi=100, facecolor="#ffffff")
            self.canvas = FigureCanvasQTAgg(self.figure)
            self.nav_toolbar = NavigationToolbar2QT(self.canvas, self)
            layout.addWidget(self.nav_toolbar)
            layout.addWidget(self.canvas, 1)
        else:
            self.figure = None
            self.canvas = None
            notice = QLabel(
                "未检测到 matplotlib，曲线视图已禁用。\n"
                "如需启用交互式曲线图，请运行：pip install matplotlib"
            )
            notice.setAlignment(Qt.AlignmentFlag.AlignCenter)
            notice.setStyleSheet("color: #666666; font-size: 13px; padding: 24px;")
            layout.addWidget(notice, 1)

    def plot_complex_series(
        self,
        title: str,
        x_vals: List[float],
        x_label: str,
        points: List[Tuple[float, float]],
        display_format: str,
    ) -> None:
        if not HAS_MATPLOTLIB or self.figure is None or self.canvas is None:
            return

        self.figure.clear()
        ax1 = self.figure.add_subplot(2, 1, 1)
        ax2 = self.figure.add_subplot(2, 1, 2, sharex=ax1)

        if display_format == FMT_RE_IM:
            y1 = [p[0] for p in points]
            y2 = [p[1] for p in points]
            y1_label, y2_label = "Real", "Imag"
            l1, l2 = "Real", "Imag"
        elif display_format == FMT_LIN_PHASE:
            vals = [complex_to_mag_db_phase(p[0], p[1]) for p in points]
            y1 = [v[0] for v in vals]
            y2 = [v[2] for v in vals]
            y1_label, y2_label = "Mag (Lin)", "Phase (deg)"
            l1, l2 = "Mag", "Phase"
        else:
            vals = [complex_to_mag_db_phase(p[0], p[1]) for p in points]
            y1 = [v[1] for v in vals]
            y2 = [v[2] for v in vals]
            y1_label, y2_label = "Mag (dB)", "Phase (deg)"
            l1, l2 = "Mag", "Phase"

        ax1.plot(x_vals, y1, color="#1f77b4", linewidth=1.4, label=l1)
        ax1.set_ylabel(y1_label, fontsize=9)
        ax1.grid(True, linestyle="--", alpha=0.45)
        ax1.legend(loc="upper right", fontsize=8)
        ax1.tick_params(labelsize=8)

        ax2.plot(x_vals, y2, color="#e67e22", linewidth=1.4, label=l2)
        ax2.set_ylabel(y2_label, fontsize=9)
        ax2.set_xlabel(x_label, fontsize=9)
        ax2.grid(True, linestyle="--", alpha=0.45)
        ax2.legend(loc="upper right", fontsize=8)
        ax2.tick_params(labelsize=8)
        if "Phase" in y2_label:
            ax2.set_ylim(-185, 185)
            ax2.set_yticks([-180, -90, 0, 90, 180])

        self.figure.subplots_adjust(
            left=0.10, right=0.97, top=0.95, bottom=0.12, hspace=0.28
        )
        self.canvas.draw_idle()


    def plot_comparison(
        self,
        x_vals: List[float],
        x_label: str,
        current: List[Tuple[float, float]],
        reference: List[Tuple[float, float]],
        *,
        phase: bool,
    ) -> None:
        if self.figure is None or self.canvas is None:
            return

        def plot_value(real: float, imag: float) -> float:
            magnitude = math.hypot(real, imag)
            if magnitude == 0.0:
                return math.nan
            if phase:
                return math.degrees(math.atan2(imag, real))
            return 20.0 * math.log10(magnitude)

        current_values = [plot_value(*point) for point in current]
        reference_values = [plot_value(*point) for point in reference]
        differences = [a - b for a, b in zip(current_values, reference_values)]
        if phase:
            differences = [(value + 180.0) % 360.0 - 180.0 for value in differences]

        self.figure.clear()
        grid = self.figure.add_gridspec(2, 1, height_ratios=(2, 1), hspace=0.12)
        overlay = self.figure.add_subplot(grid[0])
        delta = self.figure.add_subplot(grid[1], sharex=overlay)
        overlay.tick_params(labelbottom=False)
        marker = "." if len(x_vals) == 1 else None
        overlay.plot(
            x_vals, current_values, color="#2563EB", linewidth=1.4,
            label="Current", marker=marker,
        )
        overlay.plot(
            x_vals, reference_values, "--", color="#D97706", linewidth=1.4,
            label="Reference", marker=marker,
        )
        overlay.legend(loc="upper right", fontsize=8)
        delta.axhline(0, color="#94A3B8", linewidth=0.8, zorder=0)
        delta.plot(x_vals, differences, color="#475569", linewidth=1.3, marker=marker)
        if not any(math.isfinite(value) for value in differences):
            delta.text(
                0.5, 0.5, "No valid differences", transform=delta.transAxes,
                ha="center", va="center", color="#64748B", fontsize=9,
                bbox={"facecolor": "white", "edgecolor": "none"},
            )

        quantity = "Phase (°)" if phase else "Magnitude (dB)"
        overlay.set_ylabel(quantity, fontsize=9)
        delta.set_ylabel(f"Δ {quantity}", fontsize=9)
        delta.set_xlabel(x_label, fontsize=9)
        for axes in (overlay, delta):
            axes.grid(True, color="#E7ECF3")
            axes.tick_params(labelsize=8)
        self.figure.subplots_adjust(left=0.12, right=0.97, top=0.95, bottom=0.16)
        self.canvas.draw_idle()


class FixtureDialog(QDialog):
    def __init__(self, parent: QWidget, ports: list[int], directory: Path) -> None:
        super().__init__(parent)
        self.setWindowTitle("夹具去嵌 CalSet")
        self.resize(680, 190)
        self.directory = directory
        self.inputs = {}
        layout = QFormLayout(self)
        layout.addRow(QLabel("为一个或两个端口选择 .s2p。留空的端口按理想直通处理。"))
        for port in ports:
            filename = QLineEdit(self)
            filename.setPlaceholderText("选择夹具文件")
            browse = QPushButton("浏览…", self)
            browse.clicked.connect(lambda checked=False, field=filename: self.browse(field))
            direction = QComboBox(self)
            direction.addItems(["文件端口 1 朝 VNA", "文件端口 2 朝 VNA"])
            row = QHBoxLayout()
            row.addWidget(filename, 1)
            row.addWidget(browse)
            row.addWidget(direction)
            layout.addRow(f"VNA 端口 {port}", row)
            self.inputs[port] = (filename, direction)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("计算并保存")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def browse(self, field: QLineEdit) -> None:
        name, _ = QFileDialog.getOpenFileName(
            self, "选择 S2P 夹具", str(self.directory), "Touchstone (*.s2p);;所有文件 (*)"
        )
        if name:
            field.setText(name)
            self.directory = Path(name).parent

    def accept(self) -> None:
        if not any(field.text().strip() for field, _ in self.inputs.values()):
            QMessageBox.warning(self, "缺少夹具", "请至少选择一个端口的 .s2p 文件。")
            return
        super().accept()


class CalSetComparisonDialog(QDialog):
    HEADERS = ["误差项", "比较状态", "比较点数", "最大 |Δ|", "RMS |Δ|", "峰值位置"]

    def __init__(
        self, parent: QWidget, left_name: str, right_name: str,
        left: List[ComplexTrace], right: List[ComplexTrace],
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("比较 CalSet")
        self.resize(960, 720)
        self.left = {trace.key.casefold(): trace for trace in left}
        self.right = {trace.key.casefold(): trace for trace in right}
        self.left_name = left_name
        self.right_name = right_name
        layout = QVBoxLayout(self)
        description = QLabel(f"当前  {left_name}\n参考  {right_name}\nΔ = 当前 − 参考", self)
        description.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(description)
        self.point_order = QCheckBox("一侧缺少频率轴时按点序比较", self)
        self.point_order.setToolTip("仅在确认两份 CalSet 使用相同扫频时启用。")
        layout.addWidget(self.point_order)
        self.table = QTableWidget(0, len(self.HEADERS), self)
        self.table.setHorizontalHeaderLabels(self.HEADERS)
        for column in (3, 4):
            self.table.horizontalHeaderItem(column).setToolTip("按复数残差的模统计。")
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        layout.addWidget(self.table, 1)
        self.info = QLabel(self)
        self.info.setTextFormat(Qt.TextFormat.PlainText)
        self.plot = TracePlotWidget(self)
        self.view = QComboBox(self)
        self.view.addItem("幅度（dB）", False)
        self.view.addItem("相位（°）", True)
        view_row = QHBoxLayout()
        view_row.addWidget(self.info, 1)
        view_row.addWidget(self.view)
        layout.addLayout(view_row)
        layout.addWidget(self.plot, 2)
        actions = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, parent=self)
        export = actions.addButton("导出比较 CSV", QDialogButtonBox.ButtonRole.ActionRole)
        export.clicked.connect(self.export_csv)
        actions.rejected.connect(self.reject)
        layout.addWidget(actions)

        self.table.currentCellChanged.connect(self.show_trace)
        self.view.currentIndexChanged.connect(self.show_trace)
        self.point_order.toggled.connect(self.populate_comparison)
        self.populate_comparison()

    def populate_comparison(self) -> None:
        self.rows = compare_terms(
            list(self.left.values()), list(self.right.values()),
            allow_point_order=self.point_order.isChecked(),
        )
        self.table.setRowCount(len(self.rows))
        self.report = []
        for i, row in enumerate(self.rows):
            peak = ""
            if row.peak_frequency_hz is not None:
                peak = f"{row.peak_frequency_hz:.12g} Hz"
            elif row.peak_index is not None:
                peak = f"点索引 {row.peak_index}"
            values = [
                row.name, row.status, str(len(row.residuals)),
                "" if row.max_error is None else f"{row.max_error:.12g}",
                "" if row.rms_error is None else f"{row.rms_error:.12g}", peak,
            ]
            self.report.append(values)
            for col, value in enumerate(values):
                self.table.setItem(i, col, QTableWidgetItem(value))
        if self.rows:
            row_index = next((i for i, row in enumerate(self.rows) if row.residuals), 0)
            self.table.setCurrentCell(row_index, 0)
            self.table.selectRow(row_index)
            self.show_trace()

    def show_trace(self) -> None:
        index = self.table.currentRow()
        if index < 0:
            return
        row = self.rows[index]
        self.info.setText(f"{row.name} | {row.status}")
        self.plot.setVisible(bool(row.residuals))
        if not row.residuals:
            return
        axis = row.frequencies_hz
        if axis is not None and all(a < b for a, b in zip(axis, axis[1:])):
            x_vals, x_label = [hz / 1e9 for hz in axis], "Frequency (GHz)"
        else:
            x_vals, x_label = list(range(len(row.residuals))), "Point Index"
        key = row.name.casefold()
        self.plot.plot_comparison(
            x_vals, x_label, self.left[key].points, self.right[key].points,
            phase=self.view.currentData(),
        )

    def export_csv(self) -> None:
        filename, _ = QFileDialog.getSaveFileName(
            self, "导出比较结果", "calset_comparison.csv", "CSV (*.csv)"
        )
        if filename:
            try:
                with open(filename, "w", newline="", encoding="utf-8-sig") as output:
                    writer = csv.writer(output)
                    writer.writerow(["当前 CalSet", "参考 CalSet", *self.HEADERS])
                    writer.writerows([self.left_name, self.right_name, *row] for row in self.report)
            except OSError as exc:
                QMessageBox.critical(self, "导出失败", str(exc))


class CalSetViewerWindow(QMainWindow):
    """Main window for Keysight PNA *.cal inspection."""

    def __init__(self, initial_file: Optional[Path] = None) -> None:
        super().__init__()
        self.setWindowTitle("Keysight PNA Cal File Inspector")
        self.resize(1280, 760)
        self.setAcceptDrops(True)

        self.current_path: Optional[Path] = None
        self.parsed: Optional[ParsedCalFile] = None
        self.active_series_payload: Optional[dict[str, Any]] = None

        self._build_menu_and_toolbar()
        self._build_central_ui()
        self._build_status_bar()
        self._build_import_log()

        if initial_file is not None:
            self.load_cal_file(initial_file)

    def _build_menu_and_toolbar(self) -> None:
        # Actions
        self.act_open = QAction("打开 CalSet", self)
        self.act_open.setShortcut(QKeySequence.StandardKey.Open)
        self.act_open.triggered.connect(self.on_open_dialog)

        self.act_reload = QAction("重新加载", self)
        self.act_reload.setShortcut(QKeySequence("F5"))
        self.act_reload.triggered.connect(self.on_reload)

        self.act_export_current = QAction("导出当前项", self)
        self.act_export_current.setShortcut(QKeySequence("Ctrl+E"))
        self.act_export_current.triggered.connect(self.on_export_current)

        self.act_export_all = QAction("导出全部", self)
        self.act_export_all.setShortcut(QKeySequence("Ctrl+Shift+E"))
        self.act_export_all.triggered.connect(self.on_export_all)

        self.act_exit = QAction("退出", self)
        self.act_exit.setShortcut(QKeySequence.StandardKey.Quit)
        self.act_exit.triggered.connect(self.close)

        self.act_copy = QAction("复制表格选区", self)
        self.act_copy.setShortcut(QKeySequence.StandardKey.Copy)
        self.act_copy.triggered.connect(self.on_copy_active_table)

        self.act_expand_all = QAction("展开全部目录", self)
        self.act_expand_all.triggered.connect(lambda: self.tree.expandAll())

        self.act_collapse_all = QAction("折叠全部目录", self)
        self.act_collapse_all.triggered.connect(lambda: self.tree.collapseAll())

        self.act_deembed = QAction("夹具去嵌 CalSet…", self)
        self.act_deembed.triggered.connect(self.on_deembed_calset)
        self.act_compare = QAction("比较 CalSet…", self)
        self.act_compare.triggered.connect(self.on_compare_calsets)

        self.act_about = QAction("关于", self)
        self.act_about.triggered.connect(self.on_about)

        # Menu Bar
        mb = self.menuBar()
        m_file = mb.addMenu("文件(&F)")
        m_file.addAction(self.act_open)
        m_file.addAction(self.act_reload)
        m_file.addSeparator()
        m_file.addAction(self.act_export_current)
        m_file.addAction(self.act_export_all)
        m_file.addSeparator()
        m_file.addAction(self.act_exit)

        m_edit = mb.addMenu("编辑(&E)")
        m_edit.addAction(self.act_copy)

        m_view = mb.addMenu("查看(&V)")
        m_view.addAction(self.act_expand_all)
        m_view.addAction(self.act_collapse_all)
        self.view_menu = m_view

        m_tools = mb.addMenu("工具(&T)")
        m_tools.addAction(self.act_deembed)
        m_tools.addAction(self.act_compare)

        m_help = mb.addMenu("帮助(&H)")
        m_help.addAction(self.act_about)

        # Toolbar
        tb = QToolBar("主工具栏", self)
        tb.setMovable(False)
        self.addToolBar(tb)
        tb.addAction(self.act_open)
        tb.addAction(self.act_export_current)
        tb.addAction(self.act_export_all)
        tb.addSeparator()

        fmt_label = QLabel(" 格式: ")
        tb.addWidget(fmt_label)
        self.format_combo = QComboBox(self)
        self.format_combo.addItems([FMT_DB_PHASE, FMT_RE_IM, FMT_LIN_PHASE])
        self.format_combo.currentTextChanged.connect(self.on_format_changed)
        tb.addWidget(self.format_combo)

    def _build_central_ui(self) -> None:
        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        splitter.setChildrenCollapsible(False)

        # Left Panel: Search + Tree
        left_panel = QWidget(self)
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(6, 6, 4, 6)
        left_layout.setSpacing(6)

        self.search_input = QLineEdit(self)
        self.search_input.setPlaceholderText("搜索数据项...")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.textChanged.connect(self.on_filter_tree)
        left_layout.addWidget(self.search_input)

        self.tree = QTreeWidget(self)
        self.tree.setHeaderHidden(True)
        self.tree.currentItemChanged.connect(self.on_tree_selection_changed)
        left_layout.addWidget(self.tree, 1)

        # Right Panel: Info Strip + Stacked Views
        right_panel = QWidget(self)
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(4, 6, 6, 6)
        right_layout.setSpacing(6)

        self.info_bar = QLabel("未加载文件 — 点击「打开 .cal」或将 .cal 文件拖入窗口")
        self.info_bar.setStyleSheet(
            "background-color: #f3f5f8; border: 1px solid #dcdfe6; "
            "border-radius: 3px; padding: 6px 10px; font-weight: 500; color: #2c3e50;"
        )
        right_layout.addWidget(self.info_bar)

        self.stack = QStackedWidget(self)

        # Page 0: Welcome
        welcome = QLabel(
            "Keysight PNA / PNA-X 校准文件浏览器\n\n"
            "• 点击左上角「打开 .cal」选择校准文件\n"
            "• 或直接将 *.cal 文件拖放到此窗口中"
        )
        welcome.setAlignment(Qt.AlignmentFlag.AlignCenter)
        welcome.setStyleSheet("color: #7f8c8d; font-size: 14px;")
        self.stack.addWidget(welcome)

        # Page 1: Generic Key-Value / Structured Table
        self.kv_table = CopyableTableWidget(self)
        self.stack.addWidget(self.kv_table)

        # Page 2: Plain Text View (CoreStimulus / Long metadata)
        self.text_view = QPlainTextEdit(self)
        self.text_view.setReadOnly(True)
        self.text_view.setStyleSheet("font-family: Consolas, Menlo, monospace; font-size: 12px;")
        self.stack.addWidget(self.text_view)

        # Page 3: Complex Trace / Error Term Inspector (Tabs: Plot+Table Split & Full Table)
        self.trace_tabs = QTabWidget(self)

        # Tab 1: Full Data Table
        self.full_trace_table = CopyableTableWidget(self)
        self.trace_tabs.addTab(self.full_trace_table, "数据表格")

        # Tab 2: Curve View (Top Plot + Bottom Compact Table, matching mockup)
        plot_tab = QWidget(self)
        plot_tab_layout = QVBoxLayout(plot_tab)
        plot_tab_layout.setContentsMargins(2, 2, 2, 2)

        v_splitter = QSplitter(Qt.Orientation.Vertical, self)
        self.plot_widget = TracePlotWidget(self)
        self.bottom_trace_table = CopyableTableWidget(self)
        v_splitter.addWidget(self.plot_widget)
        v_splitter.addWidget(self.bottom_trace_table)
        v_splitter.setSizes([420, 200])
        plot_tab_layout.addWidget(v_splitter)

        self.trace_tabs.addTab(plot_tab, "曲线视图")
        self.trace_tabs.setCurrentIndex(1)  # Default to Curve + Table split view

        self.stack.addWidget(self.trace_tabs)
        right_layout.addWidget(self.stack, 1)

        splitter.addWidget(left_panel)
        splitter.addWidget(right_panel)
        splitter.setSizes([350, 930])
        self.setCentralWidget(splitter)

    def _build_status_bar(self) -> None:
        self.status = QStatusBar(self)
        self.setStatusBar(self.status)
        self.status.showMessage("就绪")

    def _build_import_log(self) -> None:
        self.log_dock = QDockWidget("导入日志", self)
        self.log_dock.setAllowedAreas(Qt.DockWidgetArea.BottomDockWidgetArea)
        panel = QWidget(self.log_dock)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(6, 4, 6, 6)

        actions = QHBoxLayout()
        copy_button = QPushButton("复制日志", panel)
        copy_button.clicked.connect(self._copy_import_log)
        actions.addWidget(copy_button)
        open_button = QPushButton("打开日志目录", panel)
        actions.addWidget(open_button)
        actions.addStretch(1)
        layout.addLayout(actions)

        self.log_text = QPlainTextEdit(panel)
        self.log_text.setReadOnly(True)
        self.log_text.setMaximumBlockCount(2000)
        layout.addWidget(self.log_text)
        self.log_dock.setWidget(panel)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.log_dock)
        self.view_menu.addAction(self.log_dock.toggleViewAction())
        self.log_dock.hide()

        formatter = logging.Formatter(
            "%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"
        )
        panel_handler = LogPanelHandler(self.log_text)
        panel_handler.setFormatter(formatter)
        self._log_handlers: list[logging.Handler] = [panel_handler]
        LOGGER.setLevel(logging.DEBUG)
        LOGGER.propagate = False
        LOGGER.addHandler(panel_handler)

        data_dir = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.AppLocalDataLocation
        )
        log_dir = Path(data_dir) / "logs"
        open_button.setEnabled(False)
        try:
            if not data_dir:
                raise OSError("系统未提供应用数据目录")
            log_dir.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                log_dir / "calset-viewer.log",
                maxBytes=1024 * 1024,
                backupCount=3,
                encoding="utf-8",
            )
        except OSError as exc:
            LOGGER.warning("无法写入本地日志目录，仍可从界面复制日志。%s", exc)
        else:
            file_handler.setFormatter(formatter)
            LOGGER.addHandler(file_handler)
            self._log_handlers.append(file_handler)
            open_button.setToolTip(str(log_dir))
            log_url = QUrl.fromLocalFile(str(log_dir))
            open_button.clicked.connect(lambda: QDesktopServices.openUrl(log_url))
            open_button.setEnabled(True)

        LOGGER.info(
            "CalSet Viewer 启动 | %s | Python %s | PyQt %s | Qt %s",
            platform.system(),
            platform.python_version(),
            PYQT_VERSION_STR,
            QT_VERSION_STR,
        )

    def _copy_import_log(self) -> None:
        QApplication.clipboard().setText(self.log_text.toPlainText())
        self.status.showMessage("导入日志已复制", 4000)

    def closeEvent(self, event: QCloseEvent) -> None:
        super().closeEvent(event)
        if event.isAccepted():
            for handler in self._log_handlers:
                LOGGER.removeHandler(handler)
                handler.close()

    # ---------------- Drag & Drop ----------------

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                if url.toLocalFile().lower().endswith((".cal", ".calset.json")):
                    event.acceptProposedAction()
                    return
        event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:
        for url in event.mimeData().urls():
            path = Path(url.toLocalFile())
            if path.name.lower().endswith((".cal", ".calset.json")):
                self.load_cal_file(path)
                return

    # ---------------- File Loading ----------------

    def on_open_dialog(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "打开 CalSet",
            str(self.current_path.parent) if self.current_path else "",
            CALSET_FILTER,
        )
        if file_path:
            self.load_cal_file(Path(file_path))

    def on_reload(self) -> None:
        if self.current_path is not None:
            self.load_cal_file(self.current_path)

    def load_cal_file(self, path: Path) -> None:
        LOGGER.info("开始导入 %s", path.name)
        stage = "读取文件"
        try:
            raw = path.read_bytes()
            LOGGER.info("已读取 %s | %d 字节", path.name, len(raw))
            stage = "解析校准数据"
            parsed = read_calset_document(path, raw)
        except Exception as exc:
            LOGGER.exception("导入失败 | %s | 阶段：%s", path.name, stage)
            self.log_dock.show()
            message = str(exc)
            QMessageBox.critical(
                self,
                "导入失败",
                f"{path.name}\n{stage}失败：{message}\n\n可在底部导入日志中复制详细信息。",
            )
            return

        self.current_path = path
        self.parsed = parsed
        self.active_series_payload = None
        self.setWindowTitle(f"Keysight PNA Cal File Inspector - {path.name}")
        self._populate_tree(parsed)
        self._update_status_bar(path, parsed)
        matrix_term_count = len(parsed.error_term_set.error_terms) if parsed.error_term_set else 0
        named_term_count = len(parsed.named_error_terms) + sum(
            len(group.error_terms) for group in parsed.mapped_measurement_groups
        )
        LOGGER.info(
            "导入完成 | %s | 原始迹线 %d | 映射迹线 %d | 原始矩阵系数 %d | 具名误差项 %d | 重建误差项 %d",
            path.name,
            len(parsed.raw_measurements),
            len(parsed.mapped_measurements),
            matrix_term_count,
            named_term_count,
            len(parsed.reconstructed_error_terms),
        )

    def on_deembed_calset(self) -> None:
        if self.parsed is None or self.current_path is None:
            QMessageBox.information(self, "夹具去嵌", "请先打开常规两端口 CalSet。")
            return
        try:
            ports = fixture_calibration_ports(self.parsed)
        except ValueError as exc:
            QMessageBox.information(self, "夹具去嵌", str(exc))
            return

        dialog = FixtureDialog(self, [port for port, _ in ports], self.current_path.parent)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            fixtures = {
                port: read_fixture(Path(field.text().strip()), direction.currentIndex() == 1)
                for port, (field, direction) in dialog.inputs.items() if field.text().strip()
            }
            result = deembed_calset(self.parsed, self.current_path, fixtures)
            source_name = self.current_path.name.removesuffix(".calset.json")
            if self.current_path.suffix.lower() == ".cal":
                source_name = self.current_path.stem
            filename, _ = QFileDialog.getSaveFileName(
                self, "保存去嵌后的 CalSet",
                str(self.current_path.with_name(f"{source_name}_deembed.calset.json")),
                "CalSet Viewer (*.calset.json)",
            )
            if not filename:
                return
            path = Path(filename)
            if not path.name.lower().endswith(".calset.json"):
                path = path.with_name(f"{path.name}.calset.json")
                if path.exists():
                    answer = QMessageBox.question(
                        self, "覆盖去嵌结果", f"已存在 {path.name}，是否替换？",
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                        QMessageBox.StandardButton.No,
                    )
                    if answer != QMessageBox.StandardButton.Yes:
                        return
            if path.resolve() == self.current_path.resolve():
                raise ValueError("请选择与当前 CalSet 不同的文件名。")
            write_calset_document(path, result)
        except (OSError, ValueError, ArithmeticError) as exc:
            LOGGER.exception("夹具去嵌失败")
            QMessageBox.critical(self, "夹具去嵌失败", str(exc))
            return
        self.load_cal_file(path)
        self.status.showMessage(f"已保存去嵌 CalSet {path.name}", 5000)

    def on_compare_calsets(self) -> None:
        if self.parsed is None or self.current_path is None:
            QMessageBox.information(self, "比较 CalSet", "请先打开待比较的 CalSet。")
            return
        current = named_calset_groups(self.parsed)
        if not current:
            QMessageBox.information(self, "比较 CalSet", "当前文件缺少具名误差项。")
            return
        filename, _ = QFileDialog.getOpenFileName(
            self, "打开参考 CalSet", str(self.current_path.parent), CALSET_FILTER
        )
        if not filename:
            return
        path = Path(filename)
        try:
            reference = named_calset_groups(read_calset_document(path))
        except (OSError, ValueError, EOFError) as exc:
            LOGGER.exception("读取参考 CalSet 失败 | %s", path.name)
            QMessageBox.critical(self, "读取参考 CalSet 失败", str(exc))
            return
        if not reference:
            QMessageBox.information(self, "比较 CalSet", "参考文件缺少具名误差项。")
            return

        selections = []
        for filepath, groups in ((self.current_path, current), (path, reference)):
            index = 0
            if len(groups) > 1:
                options = [f"{i + 1}. {name}" for i, (name, _) in enumerate(groups)]
                option, accepted = QInputDialog.getItem(
                    self, "选择 CalSet", filepath.name, options, editable=False
                )
                if not accepted:
                    return
                index = options.index(option)
            name, traces = groups[index]
            selections.append((f"{filepath.name} | {name}", traces))
        (left_name, left), (right_name, right) = selections
        CalSetComparisonDialog(self, left_name, right_name, left, right).exec()

    def _update_status_bar(self, path: Path, parsed: ParsedCalFile) -> None:
        parts = [path.name, f"{parsed.file_size:,} B"]
        model = parsed.metadata.get("Model Number")
        if model:
            parts.append(str(model))
        if parsed.stimulus_header and parsed.stimulus_header.port_config:
            parts.append(parsed.stimulus_header.port_config)
        if parsed.error_term_set and parsed.error_term_set.freqs_ghz:
            fs = parsed.error_term_set.freqs_ghz
            parts.append(f"{len(fs)} pts")
            parts.append(f"{fs[0] * 1000:.4g} MHz - {fs[-1]:.4g} GHz")
        self.status.showMessage(" | ".join(parts))

    # ---------------- Tree Population & Search ----------------

    def _populate_tree(self, p: ParsedCalFile) -> None:
        self.tree.clear()
        self.search_input.clear()

        # 1. Section 1: Header & Schemas
        n_schemas = len(p.header.schemas)
        sec1 = QTreeWidgetItem(self.tree, [f"文件头与子系统版本 ({n_schemas})"])
        sec1.setData(0, Qt.ItemDataRole.UserRole, {"kind": "header"})
        schema_node = QTreeWidgetItem(sec1, [f"子系统 Schema 版本表 ({n_schemas})"])
        schema_node.setData(0, Qt.ItemDataRole.UserRole, {"kind": "schemas"})

        # 2. Section 3 & 5: Stimulus & Cal Settings
        sec35 = QTreeWidgetItem(self.tree, ["激励与校准设置 (Sec 3 & 5)"])
        sec35.setData(0, Qt.ItemDataRole.UserRole, {"kind": "stimulus_header"})
        if p.cal_settings:
            kits_node = QTreeWidgetItem(
                sec35, [f"端口校准件配置 ({len(p.cal_settings.port_kits)})"]
            )
            kits_node.setData(0, Qt.ItemDataRole.UserRole, {"kind": "port_kits"})
            if p.cal_settings.core_stimulus_text:
                core_node = QTreeWidgetItem(sec35, ["Std CoreStimulus 摘要"])
                core_node.setData(0, Qt.ItemDataRole.UserRole, {"kind": "core_stimulus"})

        # 3. Section 6: Metadata
        sec6 = QTreeWidgetItem(self.tree, [f"元数据属性 ({len(p.metadata)})"])
        sec6.setData(0, Qt.ItemDataRole.UserRole, {"kind": "metadata"})
        if "Cal Acquisition Information" in p.metadata:
            acq_node = QTreeWidgetItem(sec6, ["Cal Acquisition Information"])
            acq_node.setData(0, Qt.ItemDataRole.UserRole, {"kind": "cal_acq_text"})

        # 4. Section 2: Raw Measurements
        sec2 = QTreeWidgetItem(self.tree, [f"原始标准件测量 ({len(p.raw_measurements)})"])
        sec2.setData(0, Qt.ItemDataRole.UserRole, {"kind": "trace_list", "source": "raw"})
        for idx, tr in enumerate(p.raw_measurements):
            item = QTreeWidgetItem(sec2, [tr.key])
            item.setData(
                0,
                Qt.ItemDataRole.UserRole,
                {"kind": "trace", "source": "raw", "index": idx},
            )

        # 5. Section 4: Mapped Measurements
        sec4 = QTreeWidgetItem(self.tree, [f"映射标准件测量 ({len(p.mapped_measurements)})"])
        if p.mapped_measurement_groups:
            sec4.setData(0, Qt.ItemDataRole.UserRole, {"kind": "mapped_groups"})
            for group_index, group in enumerate(p.mapped_measurement_groups):
                parent = QTreeWidgetItem(sec4, [f"{group.calset.name} ({len(group.traces)})"])
                self._add_trace_nodes(parent, group.traces, group_index)
            sec4.setExpanded(True)
        else:
            self._add_trace_nodes(sec4, p.mapped_measurements)
            sec4.setExpanded(bool(p.mapped_measurements))

        named_term_count = sum(len(group.error_terms) for group in p.mapped_measurement_groups)
        if named_term_count:
            named_terms = QTreeWidgetItem(self.tree, [f"误差项 ({named_term_count})"])
            named_terms.setData(0, Qt.ItemDataRole.UserRole, {"kind": "diq_error_groups"})
            for group_index, group in enumerate(p.mapped_measurement_groups):
                if group.error_terms:
                    label = f"{group.calset.name} ({len(group.error_terms)})"
                    parent = QTreeWidgetItem(named_terms, [label])
                    self._add_trace_nodes(parent, group.error_terms, group_index, source="diq_error")
            named_terms.setExpanded(True)

        # 6. Section 7: Error Terms
        reconstructed_node = None
        if p.reconstructed_error_terms:
            group_label = "误差项（去嵌）" if p.header.magic == DERIVED_FORMAT else "误差项（重建）"
            label = f"{group_label} ({len(p.reconstructed_error_terms)})"
            reconstructed_node = QTreeWidgetItem(self.tree, [label])
            self._add_trace_nodes(
                reconstructed_node, p.reconstructed_error_terms, source="reconstructed_error"
            )
            reconstructed_node.setExpanded(True)

        if p.error_term_set:
            ets = p.error_term_set
            n_terms = len(ets.error_terms)
            n_pts = len(ets.freqs_ghz)
            sec7 = QTreeWidgetItem(self.tree, [f"误差项矩阵 ({n_terms} × {n_pts})"])
            sec7.setData(0, Qt.ItemDataRole.UserRole, {"kind": "error_overview"})

            freq_node = QTreeWidgetItem(sec7, [f"频率轴 ({n_pts} pts)"])
            freq_node.setData(0, Qt.ItemDataRole.UserRole, {"kind": "freq_axis"})

            for t_idx in range(n_terms):
                t_node = QTreeWidgetItem(sec7, [ets.term_label(t_idx)])
                t_node.setData(
                    0,
                    Qt.ItemDataRole.UserRole,
                    {"kind": "error_term", "index": t_idx},
                )
            sec7.setExpanded(reconstructed_node is None)
            if reconstructed_node is not None:
                self.tree.setCurrentItem(reconstructed_node.child(0))
            elif sec7.childCount() > 1:
                self.tree.setCurrentItem(sec7.child(1))
        elif reconstructed_node is not None:
            self.tree.setCurrentItem(reconstructed_node.child(0))
        elif p.named_error_terms:
            named_terms = QTreeWidgetItem(self.tree, [f"误差项 ({len(p.named_error_terms)})"])
            self._add_trace_nodes(named_terms, p.named_error_terms, source="named_error")
            named_terms.setExpanded(True)
            self.tree.setCurrentItem(named_terms.child(0))
        else:
            self.tree.setCurrentItem(sec1)

    def _add_trace_nodes(
        self, parent: QTreeWidgetItem, traces: List[ComplexTrace],
        group_index: Optional[int] = None, *, source: str = "mapped",
    ) -> None:
        context = {"source": source, "group_index": group_index}
        parent.setData(0, Qt.ItemDataRole.UserRole, {"kind": "trace_list", **context})
        for index, trace in enumerate(traces):
            node = QTreeWidgetItem(parent, [trace.key])
            node.setData(
                0, Qt.ItemDataRole.UserRole, {"kind": "trace", "index": index, **context}
            )

    def on_filter_tree(self, query: str) -> None:
        query = query.strip().lower()

        def filter_item(item: QTreeWidgetItem, ancestor_matches: bool) -> bool:
            matches = ancestor_matches or query in item.text(0).lower()
            visible_children = [
                filter_item(item.child(i), matches) for i in range(item.childCount())
            ]
            visible = matches or any(visible_children)
            item.setHidden(not visible)
            if query and any(visible_children):
                item.setExpanded(True)
            return visible

        for index in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(index)
            if item is not None:
                filter_item(item, False)

    # ---------------- Selection & Rendering ----------------

    def on_tree_selection_changed(
        self, current: Optional[QTreeWidgetItem], _prev: Optional[QTreeWidgetItem]
    ) -> None:
        if current is None or self.parsed is None:
            return
        meta = current.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(meta, dict):
            return

        kind = meta.get("kind")
        p = self.parsed
        self.active_series_payload = None

        if kind == "header":
            h = p.header
            rows = [
                ("Magic", h.magic),
                ("File Version", str(h.file_version)),
                ("Sub Version", str(h.sub_ver_a)),
                ("CalSet Name", h.calset_name),
                ("CalSet GUID", h.calset_guid),
                ("Timestamp (UTC)", h.timestamp_utc),
                ("Header Flags", f"{h.header_flags} (0x{h.header_flags:X})"),
                ("Subsystem Schemas", str(len(h.schemas))),
            ]
            if len(h.calset_records) > 1:
                rows.append(("CalSet 记录数", str(len(h.calset_records))))
                for index, record in enumerate(h.calset_records[1:], start=2):
                    rows.extend([
                        (f"CalSet {index} 名称", record.name),
                        (f"CalSet {index} GUID", record.guid),
                        (f"CalSet {index} 时间戳 (UTC)", record.timestamp_utc),
                        (f"CalSet {index} 标志", f"0x{record.flags:X}"),
                    ])
            self._show_kv_table(
                "Section 1 | File Header 摘要", ["字段", "值"], rows
            )

        elif kind == "schemas":
            rows = [(k, str(v)) for k, v in p.header.schemas.items()]
            self._show_kv_table(
                f"Section 1 | 子系统 Schema 版本表 ({len(rows)} 项)",
                ["子系统名称", "Schema Version"],
                rows,
            )

        elif kind == "stimulus_header":
            rows: List[Tuple[str, str]] = []
            if p.stimulus_header:
                sh = p.stimulus_header
                rows.extend(
                    [
                        ("Name", sh.name),
                        ("Alias", sh.alias),
                        ("Created By", sh.created_by),
                        ("Port Config", sh.port_config),
                        ("GUID", sh.calset_guid),
                        ("Timestamp (UTC)", sh.timestamp_utc),
                    ]
                )
                id_names = {
                    0: "Param[0] (IF BW Hz)",
                    1: "Param[1] (Start Freq Hz)",
                    2: "Param[2] (Stop Freq Hz)",
                    3: "Param[3] (Start Freq Hz)",
                    4: "Param[4] (Stop Freq Hz)",
                }
                for pid, val in sh.params.items():
                    rows.append((id_names.get(pid, f"Param[{pid}]"), f"{val:g}"))
            if p.cal_settings:
                rows.append(("Cal Settings Timestamp", p.cal_settings.timestamp_utc))
            self._show_kv_table(
                "Section 3 & 5 | 激励与校准配置摘要", ["参数", "值"], rows
            )

        elif kind == "port_kits" and p.cal_settings:
            rows_3 = [
                (str(k["port"]), str(k["connector"]), str(k["cal_kit"]))
                for k in p.cal_settings.port_kits
            ]
            self._show_kv_table(
                "Section 5 | 端口连接器与校准件配置",
                ["端口", "连接器类型", "校准件名称"],
                rows_3,
            )

        elif kind == "core_stimulus" and p.cal_settings:
            self.info_bar.setText("Section 5 | Std CoreStimulus 激励摘要文本")
            self.text_view.setPlainText(p.cal_settings.core_stimulus_text)
            self.stack.setCurrentIndex(2)

        elif kind == "metadata":
            rows = [(k, str(v)) for k, v in p.metadata.items()]
            self._show_kv_table(
                f"Section 6 | CalSet 元数据属性 ({len(rows)} 项)",
                ["属性键 (Key)", "值 (Value)"],
                rows,
            )

        elif kind == "cal_acq_text":
            self.info_bar.setText("Section 6 | Cal Acquisition Information")
            self.text_view.setPlainText(str(p.metadata.get("Cal Acquisition Information", "")))
            self.stack.setCurrentIndex(2)

        elif kind in ("mapped_groups", "diq_error_groups"):
            is_error_term = kind == "diq_error_groups"
            rows_3 = []
            for group in p.mapped_measurement_groups:
                count = len(group.error_terms if is_error_term else group.traces)
                if count:
                    rows_3.append((group.calset.name, group.calset.guid, str(count)))
            label = "DIQ | 误差项分组" if is_error_term else "DIQ | 映射测量分组"
            count_title = "误差项数" if is_error_term else "迹线数"
            self._show_kv_table(label, ["CalSet", "GUID", count_title], rows_3)

        elif kind in ("trace_list", "trace"):
            source = meta["source"]
            section_label = {
                "raw": "Section 2 | 原始标准件测量",
                "mapped": "Section 4 | 映射标准件测量",
                "diq_error": "DIQ | 误差项",
                "named_error": "具名误差项",
                "reconstructed_error": "误差项（去嵌）" if p.header.magic == DERIVED_FORMAT else "误差项（重建）",
            }[source]
            group_index = meta.get("group_index")
            group_name = ""
            if group_index is not None:
                group = p.mapped_measurement_groups[group_index]
                traces = group.error_terms if source == "diq_error" else group.traces
                group_name = group.calset.name
            elif source == "named_error":
                traces = p.named_error_terms
                group_name = p.header.calset_name
            elif source == "reconstructed_error":
                traces = p.reconstructed_error_terms
                group_name = p.header.calset_name
            else:
                traces = p.raw_measurements if source == "raw" else p.mapped_measurements

            if kind == "trace_list":
                rows_3 = [
                    (str(i), tr.key, f"{len(tr.points)} pts (elem_type={tr.elem_type})")
                    for i, tr in enumerate(traces)
                ]
                label = f"{section_label}列表"
                if group_name:
                    label = f"{label} | {group_name}"
                self._show_kv_table(label, ["#", "键名 (Key)", "数据规格"], rows_3)
            else:
                tr = traces[meta["index"]]
                freqs = None
                if tr.frequencies_hz is not None:
                    freqs = [frequency / 1e9 for frequency in tr.frequencies_hz]
                elif (
                    group_index is None
                    and p.error_term_set
                    and len(p.error_term_set.freqs_ghz) == len(tr.points)
                ):
                    freqs = p.error_term_set.freqs_ghz
                data_type = "ComplexFloat32" if tr.elem_type == 3 else "ComplexFloat64"
                self._show_complex_series(
                    title=f"{group_name} | {tr.key}" if group_name else tr.key,
                    dtype_label=f"{section_label} | {data_type}",
                    points=tr.points,
                    freqs_ghz=freqs,
                )

        elif kind == "error_overview" and p.error_term_set:
            ets = p.error_term_set
            rows = [
                ("Binary Offset", f"0x{ets.offset:X}"),
                ("Magic / Version", f"{ets.magic} (0x{ets.magic:X}) / v{ets.ver_a}.{ets.ver_b}"),
                ("Port IDs", ", ".join(map(str, ets.port_ids))),
                ("Port Z0 (Ohm)", ", ".join(f"{r:g}+{im:g}j" for r, im in ets.port_z0)),
                (
                    "Frequency Range",
                    f"{ets.freqs_ghz[0]:.6g} .. {ets.freqs_ghz[-1]:.6g} GHz ({len(ets.freqs_ghz)} pts)",
                ),
                ("Error Term Matrix", f"{len(ets.error_terms)} terms × {len(ets.freqs_ghz)} pts (ComplexFloat64)"),
            ]
            self._show_kv_table("Section 7 | ErrorTermSet 总览", ["属性", "值"], rows)

        elif kind == "freq_axis" and p.error_term_set:
            fs = p.error_term_set.freqs_ghz
            rows_3 = [(str(i), f"{f:.9g}", f"{f * 1000.0:.6g}") for i, f in enumerate(fs)]
            self._show_kv_table(
                f"Section 7 | 频率轴 ({len(fs)} pts)",
                ["Index", "Freq (GHz)", "Freq (MHz)"],
                rows_3,
            )

        elif kind == "error_term" and p.error_term_set:
            t_idx = int(meta["index"])
            ets = p.error_term_set
            self._show_complex_series(
                title=ets.term_label(t_idx),
                dtype_label=f"Section 7 | ComplexFloat64 | Offset: 0x{ets.offset:X}",
                points=ets.error_terms[t_idx],
                freqs_ghz=ets.freqs_ghz,
            )

    def _show_kv_table(
        self, title: str, headers: List[str], rows: List[Tuple[str, ...]]
    ) -> None:
        self.info_bar.setText(title)
        tbl = self.kv_table
        tbl.setSortingEnabled(False)
        tbl.clear()
        tbl.setColumnCount(len(headers))
        tbl.setRowCount(len(rows))
        tbl.setHorizontalHeaderLabels(headers)
        for r_idx, row in enumerate(rows):
            for c_idx, val in enumerate(row):
                tbl.setItem(r_idx, c_idx, QTableWidgetItem(val))
        tbl.horizontalHeader().setSectionResizeMode(
            len(headers) - 1, QHeaderView.ResizeMode.Stretch
        )
        for c in range(len(headers) - 1):
            tbl.resizeColumnToContents(c)
        tbl.setSortingEnabled(True)
        self.stack.setCurrentIndex(1)

    def _show_complex_series(
        self,
        title: str,
        dtype_label: str,
        points: List[Tuple[float, float]],
        freqs_ghz: Optional[List[float]],
    ) -> None:
        n_pts = len(points)
        if freqs_ghz:
            freq_str = f"{freqs_ghz[0]:.4g} - {freqs_ghz[-1]:.4g} GHz"
            if any(a >= b for a, b in zip(freqs_ghz, freqs_ghz[1:])):
                x_vals = [float(i) for i in range(n_pts)]
                x_label = "Point Index"
            else:
                x_vals = freqs_ghz
                x_label = "Frequency (GHz)"
        else:
            freq_str = f"0 - {n_pts - 1}"
            x_vals = [float(i) for i in range(n_pts)]
            x_label = "Point Index"

        self.info_bar.setText(f"{title} | {dtype_label} | {n_pts} pts | {freq_str}")
        self.active_series_payload = {
            "title": title,
            "x_vals": x_vals,
            "x_label": x_label,
            "points": points,
            "freqs_ghz": freqs_ghz,
        }
        self._populate_trace_tables(points, freqs_ghz)
        self.plot_widget.plot_complex_series(
            title, x_vals, x_label, points, self.format_combo.currentText()
        )
        self.stack.setCurrentIndex(3)

    def _populate_trace_tables(
        self, points: List[Tuple[float, float]], freqs_ghz: Optional[List[float]]
    ) -> None:
        headers = [
            "Index",
            "Freq (GHz)" if freqs_ghz else "Point",
            "Real",
            "Imag",
            "Mag (dB)",
            "Phase (°)",
        ]
        for tbl in (self.full_trace_table, self.bottom_trace_table):
            tbl.setSortingEnabled(False)
            tbl.clear()
            tbl.setColumnCount(len(headers))
            tbl.setRowCount(len(points))
            tbl.setHorizontalHeaderLabels(headers)
            for i, (re_v, im_v) in enumerate(points):
                _mag_lin, mag_db, phase_deg = complex_to_mag_db_phase(re_v, im_v)
                freq_txt = f"{freqs_ghz[i]:.6f}" if freqs_ghz else str(i)
                vals = [
                    str(i),
                    freq_txt,
                    f"{re_v:.6g}",
                    f"{im_v:.6g}",
                    f"{mag_db:.3f}",
                    f"{phase_deg:.2f}",
                ]
                for c_idx, txt in enumerate(vals):
                    item = QTableWidgetItem(txt)
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                    )
                    tbl.setItem(i, c_idx, item)
            tbl.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
            tbl.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)

    def on_format_changed(self, fmt_text: str) -> None:
        if self.active_series_payload is None:
            return
        d = self.active_series_payload
        self.plot_widget.plot_complex_series(
            d["title"], d["x_vals"], d["x_label"], d["points"], fmt_text
        )

    # ---------------- Copy & Export ----------------

    def on_copy_active_table(self) -> None:
        if self.log_text.hasFocus():
            self.log_text.copy()
            return
        idx = self.stack.currentIndex()
        if idx == 1:
            self.kv_table.copy_selection_to_clipboard()
        elif idx == 3:
            if self.trace_tabs.currentIndex() == 0:
                self.full_trace_table.copy_selection_to_clipboard()
            else:
                self.bottom_trace_table.copy_selection_to_clipboard()

    def on_export_current(self) -> None:
        if self.parsed is None:
            QMessageBox.information(self, "提示", "请先打开一个 CalSet。")
            return
        if self.active_series_payload is None:
            QMessageBox.information(
                self,
                "提示",
                "当前选中项不是迹线或误差项，请选中某条测量迹线或 Term 后再导出当前项，或使用「导出全部」。",
            )
            return

        d = self.active_series_payload
        safe_name = "".join(c if c.isalnum() or c in "._-" else "_" for c in d["title"])
        out_path, _ = QFileDialog.getSaveFileName(
            self,
            "导出当前项为 CSV",
            f"{safe_name}.csv",
            "CSV Files (*.csv)",
        )
        if not out_path:
            return

        freqs = d["freqs_ghz"]
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(
                ["index", "freq_ghz" if freqs else "point", "real", "imag", "mag_lin", "mag_db", "phase_deg"]
            )
            for i, (re_v, im_v) in enumerate(d["points"]):
                mag_lin, mag_db, phase_deg = complex_to_mag_db_phase(re_v, im_v)
                writer.writerow(
                    [
                        i,
                        f"{freqs[i]:.9g}" if freqs else i,
                        f"{re_v:.12g}",
                        f"{im_v:.12g}",
                        f"{mag_lin:.12g}",
                        f"{mag_db:.6f}",
                        f"{phase_deg:.6f}",
                    ]
                )
        self.status.showMessage(f"已导出: {out_path}", 5000)

    def on_export_all(self) -> None:
        if self.parsed is None:
            QMessageBox.information(self, "提示", "请先打开一个 CalSet。")
            return
        out_dir = QFileDialog.getExistingDirectory(self, "选择全量导出目录")
        if not out_dir:
            return
        try:
            from parse_pna_cal import export_results

            export_results(self.parsed, Path(out_dir))
        except Exception as exc:
            QMessageBox.critical(self, "导出失败", str(exc))
            return
        QMessageBox.information(
            self,
            "导出完成",
            f"已将 summary.json、测量迹线 CSV 与误差项 CSV 导出至:\n{out_dir}",
        )

    def on_about(self) -> None:
        QMessageBox.about(
            self,
            "关于 Keysight PNA Cal File Inspector",
            "Keysight PNA / PNA-X 校准文件 (*.cal) 可视化分析工具\n"
            "基于 PyQt6 + MFC CArchive 二进制格式解析。\n\n"
            "支持查看文件头、39 项子系统版本、激励配置、COM VARIANT 元数据、\n"
            "原始/映射标准件复数迹线以及误差项矩阵。",
        )


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("CalSetViewer")
    app.setStyle("Fusion")
    initial = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    win = CalSetViewerWindow(initial)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
