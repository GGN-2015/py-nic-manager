"""PyQt6 GUI for Py NIC Manager."""

from __future__ import annotations

import sys
import time
import traceback
from collections.abc import Callable
from pathlib import Path

from PyQt6.QtCore import QObject, QRunnable, Qt, QThreadPool, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .api import NetworkManager, adapter_sort_key, nat_sort_key, route_sort_key
from .backends import BackendError
from .models import AdapterInfo, AddressInfo, CommandResult, NatRule, NetworkSnapshot, OperationPlan, RouteInfo, VirtualAdapterInfo
from .ui_tables import route_cell_text, route_table_columns
from .validation import validate_ip, validate_prefix


SORT_ROLE = Qt.ItemDataRole.UserRole
INDEX_ROLE = Qt.ItemDataRole.UserRole.value + 1
KEY_ROLE = Qt.ItemDataRole.UserRole.value + 2


class WorkerSignals(QObject):
    succeeded = pyqtSignal(object)
    failed = pyqtSignal(object, str)


class Worker(QRunnable):
    def __init__(self, func: Callable[[], object]) -> None:
        super().__init__()
        self.func = func
        self.signals = WorkerSignals()

    def run(self) -> None:
        try:
            self.signals.succeeded.emit(self.func())
        except Exception as exc:  # pragma: no cover - exercised through GUI callbacks
            self.signals.failed.emit(exc, traceback.format_exc())


class SortableTableItem(QTableWidgetItem):
    def __lt__(self, other: QTableWidgetItem) -> bool:
        left = self.data(SORT_ROLE)
        right = other.data(SORT_ROLE)
        try:
            return left < right
        except TypeError:
            return str(left) < str(right)


class BusyOverlay(QWidget):
    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("busyOverlay")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.hide()

        self._started_at: float | None = None
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._update_elapsed)

        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        panel = QFrame(self)
        panel.setObjectName("busyPanel")
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(24, 20, 24, 20)
        panel_layout.setSpacing(8)

        title = QLabel("Working")
        title.setObjectName("busyTitle")
        self.message_label = QLabel("")
        self.message_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.elapsed_label = QLabel("Elapsed: 0s")
        self.elapsed_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        progress = QProgressBar()
        progress.setRange(0, 0)
        progress.setTextVisible(False)
        progress.setFixedWidth(280)

        panel_layout.addWidget(title, alignment=Qt.AlignmentFlag.AlignCenter)
        panel_layout.addWidget(self.message_label, alignment=Qt.AlignmentFlag.AlignCenter)
        panel_layout.addWidget(self.elapsed_label, alignment=Qt.AlignmentFlag.AlignCenter)
        panel_layout.addWidget(progress)
        layout.addWidget(panel)

    def show_busy(self, message: str) -> None:
        self._started_at = time.monotonic()
        self.message_label.setText(message)
        self.elapsed_label.setText("Elapsed: 0s")
        self.setGeometry(self.parentWidget().rect())
        self.show()
        self.raise_()
        self.setFocus()
        self._timer.start()

    def hide_busy(self) -> None:
        self._timer.stop()
        self._started_at = None
        self.hide()

    def _update_elapsed(self) -> None:
        if self._started_at is None:
            return
        elapsed = int(time.monotonic() - self._started_at)
        self.elapsed_label.setText(f"Elapsed: {format_elapsed_time(elapsed)}")


class PlanDialog(QDialog):
    def __init__(self, parent: QWidget, plan: OperationPlan) -> None:
        super().__init__(parent)
        self.setWindowTitle("Review Command Plan")
        self.setMinimumSize(720, 460)
        self.confirmed = False

        layout = QVBoxLayout(self)
        title = QLabel(plan.title)
        title.setObjectName("dialogTitle")
        layout.addWidget(title)

        text = QPlainTextEdit()
        text.setReadOnly(True)
        text.setPlainText(plan.as_text())
        layout.addWidget(text, 1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        run = QPushButton("Run Commands")
        run.setObjectName("primaryButton")
        cancel.clicked.connect(self.reject)
        run.clicked.connect(self._accept_plan)
        buttons.addWidget(cancel)
        buttons.addWidget(run)
        layout.addLayout(buttons)

    def _accept_plan(self) -> None:
        self.confirmed = True
        self.accept()


class NetworkManagerQtWindow(QMainWindow):
    def __init__(self, *, auto_refresh: bool = True) -> None:
        super().__init__()
        self.setWindowTitle("Py NIC Manager")
        self.resize(1220, 760)
        self.setMinimumSize(1040, 640)

        self.manager = NetworkManager()
        self.adapters: list[AdapterInfo] = []
        self.routes: list[RouteInfo] = []
        self.nat_rules: list[NatRule] = []
        self.virtual_adapters: list[VirtualAdapterInfo] = []
        self.global_forwarding_enabled: bool | None = None
        self.imported_snapshot: NetworkSnapshot | None = None
        self._admin_only_widgets: list[QWidget] = []
        self._last_suggested_loopback_value = _default_loopback_value(self.manager.backend_name)
        self._last_suggested_virtual_value = _default_virtual_adapter_value(self.manager.backend_name)
        self._busy_depth = 0
        self._thread_pool = QThreadPool.globalInstance()
        self._workers: set[Worker] = set()
        self._active_plan: OperationPlan | None = None
        self._route_columns = route_table_columns(self.manager.backend_name)

        self._build_layout()
        self._set_mutating_controls_state()
        if auto_refresh:
            self.refresh_all()

    def _build_layout(self) -> None:
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)

        layout = QVBoxLayout(root)
        layout.setContentsMargins(14, 12, 14, 10)
        layout.setSpacing(10)

        top_bar = QFrame()
        top_bar.setObjectName("topBar")
        top_layout = QHBoxLayout(top_bar)
        top_layout.setContentsMargins(14, 10, 14, 10)
        top_layout.setSpacing(12)

        title = QLabel("Py NIC Manager")
        title.setObjectName("appTitle")
        self.admin_label = QLabel(self._admin_text())
        self.admin_label.setObjectName("goodText" if self.manager.is_admin else "dangerText")
        self.global_forwarding_label = QLabel("Global IPv4 Forwarding: Unknown")
        self.global_forwarding_check = QCheckBox("Enable global IPv4 forwarding")
        self.apply_global_forwarding_button = QPushButton("Apply Global Forwarding")
        self.apply_global_forwarding_button.clicked.connect(self.apply_global_forwarding)
        self._admin_only_widgets.extend([self.global_forwarding_check, self.apply_global_forwarding_button])
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.refresh_all)

        top_layout.addWidget(title)
        top_layout.addWidget(self.admin_label, 1)
        top_layout.addWidget(self.global_forwarding_label)
        top_layout.addWidget(self.global_forwarding_check)
        top_layout.addWidget(self.apply_global_forwarding_button)
        top_layout.addWidget(self.refresh_button)
        layout.addWidget(top_bar)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        layout.addWidget(self.tabs, 1)

        self._build_adapters_tab()
        self._build_routes_tab()
        self._build_nat_tab()
        self._build_config_tab()
        self._build_log_tab()

        self.statusBar().showMessage("Ready")
        self.busy_overlay = BusyOverlay(root)

    def _build_adapters_tab(self) -> None:
        tab = QWidget()
        layout = QHBoxLayout(tab)
        layout.setContentsMargins(0, 10, 0, 0)
        layout.setSpacing(12)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        layout.addWidget(splitter)

        self.adapter_table = QTableWidget(0, 12)
        self.adapter_table.setHorizontalHeaderLabels(
            [
                "Adapter",
                "Index",
                "Status",
                "Admin",
                "IP Forwarding",
                "ICS Compatible",
                "IPv4",
                "MAC",
                "Gateway",
                "DNS",
                "NIC Nature",
                "Type",
            ]
        )
        self._configure_table(self.adapter_table)
        self.adapter_table.horizontalHeader().setSortIndicator(1, Qt.SortOrder.AscendingOrder)
        self.adapter_table.setColumnWidth(0, 190)
        self.adapter_table.setColumnWidth(1, 70)
        self.adapter_table.setColumnWidth(2, 90)
        self.adapter_table.setColumnWidth(3, 85)
        self.adapter_table.setColumnWidth(4, 115)
        self.adapter_table.setColumnWidth(5, 120)
        self.adapter_table.setColumnWidth(6, 170)
        self.adapter_table.setColumnWidth(7, 145)
        self.adapter_table.setColumnWidth(8, 140)
        self.adapter_table.setColumnWidth(9, 210)
        self.adapter_table.setColumnWidth(10, 165)
        self.adapter_table.setColumnWidth(11, 95)
        self.adapter_table.itemSelectionChanged.connect(self._on_adapter_select)
        splitter.addWidget(self.adapter_table)

        panel = self._side_panel()
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(14, 14, 14, 14)
        panel_layout.setSpacing(10)

        panel_layout.addWidget(self._section_label("Adapter Settings"))
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        form.setFormAlignment(Qt.AlignmentFlag.AlignTop)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(8)
        panel_layout.addLayout(form)

        self.adapter_name_edit = self._line_edit(readonly=True)
        self.adapter_mac_edit = self._line_edit(admin_required=True)
        self.adapter_ip_edit = self._line_edit(admin_required=True)
        self.adapter_prefix_edit = self._line_edit(admin_required=True)
        self.adapter_gateway_edit = self._line_edit(admin_required=True)
        self.adapter_dns_edit = self._line_edit(admin_required=True)
        self.adapter_dhcp_check = QCheckBox("Use DHCP for IPv4")
        self.adapter_forwarding_check = QCheckBox("Enable IPv4 router forwarding")
        self._admin_only_widgets.extend([self.adapter_dhcp_check, self.adapter_forwarding_check])

        form.addRow("Name", self.adapter_name_edit)
        form.addRow("MAC address", self.adapter_mac_edit)
        form.addRow("IPv4 address", self.adapter_ip_edit)
        form.addRow("Prefix length", self.adapter_prefix_edit)
        form.addRow("Gateway", self.adapter_gateway_edit)
        form.addRow("DNS servers", self.adapter_dns_edit)
        form.addRow("", self.adapter_dhcp_check)
        form.addRow("", self.adapter_forwarding_check)

        self.apply_adapter_button = QPushButton("Apply Adapter Changes")
        self.apply_adapter_button.setObjectName("primaryButton")
        self.apply_adapter_button.clicked.connect(self.apply_selected_adapter)
        self.apply_forwarding_button = QPushButton("Apply Forwarding")
        self.apply_forwarding_button.clicked.connect(self.apply_selected_adapter_forwarding)
        self.enable_adapter_button = QPushButton("Enable Selected Adapter")
        self.enable_adapter_button.clicked.connect(lambda: self.set_selected_adapter_admin(True))
        self.disable_adapter_button = QPushButton("Disable Selected Adapter")
        self.disable_adapter_button.clicked.connect(lambda: self.set_selected_adapter_admin(False))
        self._admin_only_widgets.extend([
            self.apply_adapter_button,
            self.apply_forwarding_button,
            self.enable_adapter_button,
            self.disable_adapter_button,
        ])
        panel_layout.addWidget(self.apply_adapter_button)
        panel_layout.addWidget(self.apply_forwarding_button)
        panel_layout.addWidget(self.enable_adapter_button)
        panel_layout.addWidget(self.disable_adapter_button)

        panel_layout.addSpacing(4)
        panel_layout.addWidget(self._separator())
        panel_layout.addWidget(self._section_label("Loopback"))

        loopback_form = QFormLayout()
        loopback_form.setHorizontalSpacing(10)
        self.loopback_name_edit = self._line_edit(self._last_suggested_loopback_value, admin_required=True)
        loopback_form.addRow("Name or alias/address", self.loopback_name_edit)
        panel_layout.addLayout(loopback_form)

        self.create_loopback_button = QPushButton("Create Loopback")
        self.create_loopback_button.clicked.connect(self.create_loopback)
        self.delete_loopback_button = QPushButton("Delete Selected Loopback")
        self.delete_loopback_button.clicked.connect(self.delete_selected_loopback)
        self._admin_only_widgets.extend([self.create_loopback_button, self.delete_loopback_button])
        panel_layout.addWidget(self.create_loopback_button)
        panel_layout.addWidget(self.delete_loopback_button)

        panel_layout.addSpacing(4)
        panel_layout.addWidget(self._separator())
        panel_layout.addWidget(self._section_label("Virtual NIC"))

        virtual_form = QFormLayout()
        virtual_form.setHorizontalSpacing(10)
        self.virtual_name_edit = self._line_edit(self._last_suggested_virtual_value, admin_required=True)
        self.virtual_address_edit = self._line_edit("192.168.56.1/24", admin_required=True)
        virtual_form.addRow("Name", self.virtual_name_edit)
        virtual_form.addRow("IPv4 CIDR", self.virtual_address_edit)
        panel_layout.addLayout(virtual_form)

        self.create_virtual_button = QPushButton("Create Virtual NIC")
        self.create_virtual_button.clicked.connect(self.create_virtual_adapter)
        self.delete_virtual_button = QPushButton("Delete Selected Virtual NIC")
        self.delete_virtual_button.clicked.connect(self.delete_selected_virtual_adapter)
        self._admin_only_widgets.extend([self.create_virtual_button, self.delete_virtual_button])
        panel_layout.addWidget(self.create_virtual_button)
        panel_layout.addWidget(self.delete_virtual_button)
        panel_layout.addStretch(1)

        splitter.addWidget(self._scroll_panel(panel))
        splitter.setSizes([820, 340])
        self.tabs.addTab(tab, "Adapters")

    def _build_routes_tab(self) -> None:
        tab = QWidget()
        layout = QHBoxLayout(tab)
        layout.setContentsMargins(0, 10, 0, 0)
        layout.setSpacing(12)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        layout.addWidget(splitter)

        self.route_table = QTableWidget(0, len(self._route_columns))
        self.route_table.setHorizontalHeaderLabels([column.label for column in self._route_columns])
        self._configure_table(self.route_table)
        self.route_table.horizontalHeader().setSortIndicator(0, Qt.SortOrder.AscendingOrder)
        for index, column in enumerate(self._route_columns):
            self.route_table.setColumnWidth(index, column.width)
        self.route_table.itemSelectionChanged.connect(self._on_route_select)
        splitter.addWidget(self.route_table)

        panel = self._side_panel()
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(14, 14, 14, 14)
        panel_layout.setSpacing(10)
        panel_layout.addWidget(self._section_label("Route Editor"))

        form = QFormLayout()
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(8)
        self.route_destination_edit = self._line_edit(admin_required=True)
        self.route_gateway_edit = self._line_edit(admin_required=True)
        self.route_interface_edit = self._line_edit(admin_required=True)
        self.route_metric_edit = self._line_edit(admin_required=True)
        form.addRow("Destination", self.route_destination_edit)
        form.addRow("Gateway", self.route_gateway_edit)
        form.addRow("Interface", self.route_interface_edit)
        form.addRow("Route metric", self.route_metric_edit)
        panel_layout.addLayout(form)

        self.add_route_button = QPushButton("Add Route")
        self.add_route_button.setObjectName("primaryButton")
        self.add_route_button.clicked.connect(self.add_route)
        self.update_route_button = QPushButton("Update Selected Route")
        self.update_route_button.clicked.connect(self.update_selected_route)
        self.delete_route_button = QPushButton("Delete Selected Route")
        self.delete_route_button.clicked.connect(self.delete_selected_route)
        self._admin_only_widgets.extend([self.add_route_button, self.update_route_button, self.delete_route_button])
        panel_layout.addWidget(self.add_route_button)
        panel_layout.addWidget(self.update_route_button)
        panel_layout.addWidget(self.delete_route_button)
        panel_layout.addStretch(1)

        splitter.addWidget(self._scroll_panel(panel))
        splitter.setSizes([820, 340])
        self.tabs.addTab(tab, "Routes")

    def _build_nat_tab(self) -> None:
        tab = QWidget()
        layout = QHBoxLayout(tab)
        layout.setContentsMargins(0, 10, 0, 0)
        layout.setSpacing(12)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        layout.addWidget(splitter)

        self.nat_table = QTableWidget(0, 6)
        self.nat_table.setHorizontalHeaderLabels(
            ["Name", "Source CIDR", "Outbound Interface", "Enabled", "Persistent", "Managed"]
        )
        self._configure_table(self.nat_table)
        self.nat_table.horizontalHeader().setSortIndicator(0, Qt.SortOrder.AscendingOrder)
        self.nat_table.setColumnWidth(0, 170)
        self.nat_table.setColumnWidth(1, 150)
        self.nat_table.setColumnWidth(2, 180)
        self.nat_table.setColumnWidth(3, 85)
        self.nat_table.setColumnWidth(4, 95)
        self.nat_table.setColumnWidth(5, 90)
        self.nat_table.itemSelectionChanged.connect(self._on_nat_select)
        splitter.addWidget(self.nat_table)

        panel = self._side_panel()
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(14, 14, 14, 14)
        panel_layout.setSpacing(10)
        panel_layout.addWidget(self._section_label("NAT Rule Editor"))

        form = QFormLayout()
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(8)
        self.nat_name_edit = self._line_edit("py-nat0", admin_required=True)
        self.nat_source_edit = self._line_edit("192.168.0.0/24", admin_required=True)
        self.nat_outbound_edit = self._line_edit(admin_required=True)
        self.nat_enabled_check = QCheckBox("Enable NAT rule")
        self.nat_enabled_check.setChecked(True)
        self._admin_only_widgets.append(self.nat_enabled_check)
        form.addRow("Name", self.nat_name_edit)
        form.addRow("Source CIDR", self.nat_source_edit)
        form.addRow("Outbound Interface", self.nat_outbound_edit)
        form.addRow("", self.nat_enabled_check)
        panel_layout.addLayout(form)

        self.add_nat_button = QPushButton("Add NAT Rule")
        self.add_nat_button.setObjectName("primaryButton")
        self.add_nat_button.clicked.connect(self.add_nat_rule)
        self.update_nat_button = QPushButton("Update Selected NAT Rule")
        self.update_nat_button.clicked.connect(self.update_selected_nat_rule)
        self.delete_nat_button = QPushButton("Delete Selected NAT Rule")
        self.delete_nat_button.clicked.connect(self.delete_selected_nat_rule)
        self._admin_only_widgets.extend([self.add_nat_button, self.update_nat_button, self.delete_nat_button])
        panel_layout.addWidget(self.add_nat_button)
        panel_layout.addWidget(self.update_nat_button)
        panel_layout.addWidget(self.delete_nat_button)
        panel_layout.addStretch(1)

        splitter.addWidget(self._scroll_panel(panel))
        splitter.setSizes([820, 340])
        self.tabs.addTab(tab, "NAT")

    def _build_config_tab(self) -> None:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 10, 0, 0)
        layout.setSpacing(10)

        buttons = QHBoxLayout()
        self.export_button = QPushButton("Export Current Configuration")
        self.export_button.clicked.connect(self.export_current_configuration)
        self.import_button = QPushButton("Import Configuration")
        self.import_button.clicked.connect(self.import_configuration_file)
        self.apply_snapshot_button = QPushButton("Apply Imported Configuration")
        self.apply_snapshot_button.setObjectName("primaryButton")
        self.apply_snapshot_button.clicked.connect(self.apply_imported_configuration)
        self._admin_only_widgets.append(self.apply_snapshot_button)
        buttons.addWidget(self.export_button)
        buttons.addWidget(self.import_button)
        buttons.addWidget(self.apply_snapshot_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self.config_text = QPlainTextEdit()
        self.config_text.setReadOnly(True)
        self.config_text.setPlainText("No configuration file imported.")
        layout.addWidget(self.config_text, 1)
        self.tabs.addTab(tab, "Configuration")

    def _build_log_tab(self) -> None:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 10, 0, 0)
        self.log_text = QPlainTextEdit()
        self.log_text.setReadOnly(True)
        layout.addWidget(self.log_text)
        self.tabs.addTab(tab, "Log")

    def _configure_table(self, table: QTableWidget) -> None:
        table.setObjectName("dataTable")
        table.setAlternatingRowColors(True)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setStretchLastSection(True)
        table.horizontalHeader().setSectionsClickable(True)
        table.horizontalHeader().setSortIndicatorShown(True)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        table.setSortingEnabled(True)

    def _side_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("sidePanel")
        panel.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        return panel

    def _scroll_panel(self, widget: QWidget) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setObjectName("sideScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(widget)
        return scroll

    def _section_label(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("sectionLabel")
        return label

    def _separator(self) -> QFrame:
        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.HLine)
        separator.setObjectName("separator")
        return separator

    def _line_edit(
        self,
        value: str = "",
        *,
        readonly: bool = False,
        admin_required: bool = False,
    ) -> QLineEdit:
        edit = QLineEdit(value)
        edit.setReadOnly(readonly)
        if admin_required:
            self._admin_only_widgets.append(edit)
        return edit

    def _admin_text(self) -> str:
        if self.manager.is_admin:
            return f"{self.manager.backend_name} backend - administrator access is active."
        return (
            f"{self.manager.backend_name} backend - read-only mode. Restart this app as "
            "Administrator/root to change adapters, routes, or loopback devices."
        )

    def _set_mutating_controls_state(self) -> None:
        enabled = self.manager.is_admin
        for widget in self._admin_only_widgets:
            widget.setEnabled(enabled)
            if not enabled:
                widget.setToolTip("Restart Py NIC Manager as Administrator/root to change network settings.")

    def refresh_all(self) -> None:
        self.statusBar().showMessage("Loading adapters and routes...")
        self._run_background(
            lambda: self.manager.get_snapshot(concurrent=True),
            self._on_network_state_loaded,
            "Loading adapters and routes...",
        )

    def _on_network_state_loaded(self, snapshot: object) -> None:
        state = snapshot
        if not isinstance(state, NetworkSnapshot):
            raise TypeError("Network state loader did not return a snapshot.")
        self.adapters = state.adapters
        self.routes = state.routes
        self.nat_rules = state.nat_rules
        self.virtual_adapters = state.virtual_adapters
        self.global_forwarding_enabled = state.global_forwarding_enabled
        self._refresh_global_forwarding_controls()
        self._refresh_loopback_suggestion()
        self._refresh_virtual_suggestion()
        self._populate_adapters()
        self._populate_routes()
        self._populate_nat_rules()
        self.statusBar().showMessage(
            f"Loaded {len(self.adapters)} adapters, {len(self.routes)} routes, "
            f"{len(self.nat_rules)} NAT rules, and {len(self.virtual_adapters)} virtual NICs."
        )
        self._log(f"Refreshed state from the {self.manager.backend_name} backend.")

    def _populate_adapters(self) -> None:
        selected = self._selected_adapter()
        selected_key = selected.id if selected else ""
        header = self.adapter_table.horizontalHeader()
        section = header.sortIndicatorSection() if header.sortIndicatorSection() >= 0 else 1
        order = header.sortIndicatorOrder()

        self.adapter_table.setSortingEnabled(False)
        self.adapter_table.setRowCount(len(self.adapters))
        for row, adapter in enumerate(self.adapters):
            ipv4 = _first_ipv4(adapter)
            values = [
                adapter.name,
                str(row),
                adapter.status,
                _format_admin_enabled(adapter.admin_enabled),
                _format_forwarding(adapter.forwarding_enabled),
                _format_ics_compatible(adapter),
                _format_address(ipv4),
                adapter.mac,
                ", ".join(adapter.gateways),
                ", ".join(adapter.dns_servers),
                adapter.nature,
                _adapter_kind(adapter),
            ]
            sort_columns = [
                "name",
                "index",
                "status",
                "admin",
                "forwarding",
                "ics",
                "ipv4",
                "mac",
                "gateway",
                "dns",
                "nature",
                "kind",
            ]
            for column, value in enumerate(values):
                item = _table_item(
                    value,
                    adapter_sort_key(adapter, sort_by=sort_columns[column], index=row),
                    index=row,
                    key=adapter.id,
                )
                self.adapter_table.setItem(row, column, item)
        self.adapter_table.setSortingEnabled(True)
        self.adapter_table.sortItems(section, order)
        self._select_table_key(self.adapter_table, selected_key)
        if self.adapter_table.currentRow() < 0 and self.adapters:
            self.adapter_table.selectRow(0)

    def _populate_routes(self) -> None:
        selected = self._selected_route()
        selected_key = _route_key(selected) if selected else ""
        header = self.route_table.horizontalHeader()
        section = header.sortIndicatorSection() if header.sortIndicatorSection() >= 0 else 0
        order = header.sortIndicatorOrder()

        self.route_table.setSortingEnabled(False)
        self.route_table.setRowCount(len(self.routes))
        sort_columns = [column.key for column in self._route_columns]
        for row, route in enumerate(self.routes):
            values = [route_cell_text(route, column.key) for column in self._route_columns]
            for column, value in enumerate(values):
                item = _table_item(
                    value,
                    route_sort_key(route, sort_by=sort_columns[column]),
                    index=row,
                    key=_route_key(route),
                )
                self.route_table.setItem(row, column, item)
        self.route_table.setSortingEnabled(True)
        self.route_table.sortItems(section, order)
        self._select_table_key(self.route_table, selected_key)
        if self.route_table.currentRow() < 0 and self.routes:
            self.route_table.selectRow(0)

    def _populate_nat_rules(self) -> None:
        selected = self._selected_nat_rule()
        selected_key = selected.name if selected else ""
        header = self.nat_table.horizontalHeader()
        section = header.sortIndicatorSection() if header.sortIndicatorSection() >= 0 else 0
        order = header.sortIndicatorOrder()

        self.nat_table.setSortingEnabled(False)
        self.nat_table.setRowCount(len(self.nat_rules))
        sort_columns = ["name", "source_cidr", "outbound_interface", "enabled", "persistent", "managed"]
        for row, rule in enumerate(self.nat_rules):
            values = [
                rule.name,
                rule.source_cidr,
                rule.outbound_interface,
                _format_bool(rule.enabled),
                _format_bool(rule.persistent),
                _format_bool(rule.managed),
            ]
            for column, value in enumerate(values):
                item = _table_item(
                    value,
                    nat_sort_key(rule, sort_by=sort_columns[column]),
                    index=row,
                    key=rule.name,
                )
                self.nat_table.setItem(row, column, item)
        self.nat_table.setSortingEnabled(True)
        self.nat_table.sortItems(section, order)
        self._select_table_key(self.nat_table, selected_key)
        if self.nat_table.currentRow() < 0 and self.nat_rules:
            self.nat_table.selectRow(0)

    def _select_table_key(self, table: QTableWidget, key: str) -> None:
        if not key:
            return
        for row in range(table.rowCount()):
            item = table.item(row, 0)
            if item and item.data(KEY_ROLE) == key:
                table.selectRow(row)
                return

    def _on_adapter_select(self) -> None:
        adapter = self._selected_adapter()
        if adapter is None:
            return
        ipv4 = _first_ipv4(adapter)
        self.adapter_name_edit.setText(adapter.name)
        self.adapter_mac_edit.setText(adapter.mac)
        self.adapter_ip_edit.setText(ipv4.address if ipv4 else "")
        self.adapter_prefix_edit.setText("" if not ipv4 or ipv4.prefix_length is None else str(ipv4.prefix_length))
        self.adapter_gateway_edit.setText(adapter.gateways[0] if adapter.gateways else "")
        self.adapter_dns_edit.setText(", ".join(adapter.dns_servers))
        self.adapter_dhcp_check.setChecked(bool(adapter.dhcp_enabled))
        self.adapter_forwarding_check.setChecked(True if adapter.forwarding_enabled is None else adapter.forwarding_enabled)
        if adapter.is_loopback and not self.loopback_name_edit.text().strip():
            self.loopback_name_edit.setText(adapter.name)
        if adapter.is_virtual:
            self.virtual_name_edit.setText(adapter.name)
            if ipv4:
                self.virtual_address_edit.setText(_format_address(ipv4))

    def _on_route_select(self) -> None:
        route = self._selected_route()
        if route is None:
            return
        self.route_destination_edit.setText(route.destination)
        self.route_gateway_edit.setText(route.gateway)
        self.route_interface_edit.setText(route.interface)
        self.route_metric_edit.setText("" if route.metric is None else str(route.metric))

    def _on_nat_select(self) -> None:
        rule = self._selected_nat_rule()
        if rule is None:
            return
        self.nat_name_edit.setText(rule.name)
        self.nat_source_edit.setText(rule.source_cidr)
        self.nat_outbound_edit.setText(rule.outbound_interface)
        self.nat_enabled_check.setChecked(rule.enabled)

    def apply_selected_adapter(self) -> None:
        adapter = self._selected_adapter()
        if adapter is None:
            self._info("No Adapter Selected", "Select an adapter first.")
            return
        try:
            plan = self.manager.plan_update_adapter(
                adapter,
                address=_address_from_fields(self.adapter_ip_edit.text(), self.adapter_prefix_edit.text()),
                gateway=self.adapter_gateway_edit.text(),
                dns_servers=self.adapter_dns_edit.text(),
                mac=self.adapter_mac_edit.text().strip(),
                dhcp_enabled=self.adapter_dhcp_check.isChecked(),
            )
        except (ValueError, BackendError, LookupError) as exc:
            self._error("Invalid Adapter Settings", str(exc))
            return
        self._confirm_and_run(plan)

    def apply_selected_adapter_forwarding(self) -> None:
        adapter = self._selected_adapter()
        if adapter is None:
            self._info("No Adapter Selected", "Select an adapter first.")
            return
        try:
            plan = self.manager.plan_set_adapter_forwarding(adapter, self.adapter_forwarding_check.isChecked())
        except (BackendError, LookupError, ValueError) as exc:
            self._error("Forwarding Error", str(exc))
            return
        self._confirm_and_run(plan)

    def set_selected_adapter_admin(self, enabled: bool) -> None:
        adapter = self._selected_adapter()
        if adapter is None:
            self._info("No Adapter Selected", "Select an adapter first.")
            return
        try:
            plan = self.manager.plan_set_adapter_admin(adapter, enabled)
        except (BackendError, LookupError, ValueError) as exc:
            self._error("Adapter State Error", str(exc))
            return
        self._confirm_and_run(plan)

    def apply_global_forwarding(self) -> None:
        try:
            plan = self.manager.plan_set_global_forwarding(self.global_forwarding_check.isChecked())
        except (BackendError, ValueError) as exc:
            self._error("Forwarding Error", str(exc))
            return
        self._confirm_and_run(plan)

    def create_loopback(self) -> None:
        name = self.loopback_name_edit.text().strip()
        if not name:
            self._info("Loopback Name Required", "Enter a loopback adapter name or alias.")
            return
        try:
            plan = self.manager.plan_create_loopback(name)
        except (BackendError, ValueError) as exc:
            self._error("Loopback Error", str(exc))
            return
        self._confirm_and_run(plan)

    def delete_selected_loopback(self) -> None:
        adapter = self._selected_adapter()
        if adapter is None:
            self._info("No Adapter Selected", "Select a loopback adapter first.")
            return
        try:
            plan = self.manager.plan_delete_loopback(adapter)
        except (BackendError, LookupError, ValueError) as exc:
            self._error("Loopback Error", str(exc))
            return
        self._confirm_and_run(plan)

    def create_virtual_adapter(self) -> None:
        name = self.virtual_name_edit.text().strip()
        if not name:
            self._info("Virtual NIC Name Required", "Enter a virtual NIC name.")
            return
        try:
            plan = self.manager.plan_create_virtual_adapter(
                name,
                address=_address_from_text(self.virtual_address_edit.text().strip() or "192.168.56.1/24"),
            )
        except (BackendError, ValueError) as exc:
            self._error("Virtual NIC Error", str(exc))
            return
        self._confirm_and_run(plan)

    def delete_selected_virtual_adapter(self) -> None:
        adapter = self._selected_virtual_adapter()
        if adapter is None:
            self._info("No Virtual NIC Selected", "Select a virtual NIC first.")
            return
        try:
            plan = self.manager.backend.plan_virtual_adapter_delete(adapter)
        except BackendError as exc:
            self._error("Virtual NIC Error", str(exc))
            return
        self._confirm_and_run(plan)

    def add_route(self) -> None:
        try:
            plan = self.manager.plan_add_route(
                self.route_destination_edit.text(),
                gateway=self.route_gateway_edit.text(),
                interface=self.route_interface_edit.text(),
                metric=self.route_metric_edit.text().strip(),
            )
        except (ValueError, BackendError) as exc:
            self._error("Invalid Route", str(exc))
            return
        self._confirm_and_run(plan)

    def update_selected_route(self) -> None:
        old_route = self._selected_route()
        if old_route is None:
            self._info("No Route Selected", "Select a route first.")
            return
        try:
            plan = self.manager.plan_update_route(
                old_route,
                self.route_destination_edit.text(),
                gateway=self.route_gateway_edit.text(),
                interface=self.route_interface_edit.text(),
                metric=self.route_metric_edit.text().strip(),
            )
        except (ValueError, BackendError) as exc:
            self._error("Invalid Route", str(exc))
            return
        self._confirm_and_run(plan)

    def delete_selected_route(self) -> None:
        route = self._selected_route()
        if route is None:
            self._info("No Route Selected", "Select a route first.")
            return
        try:
            plan = self.manager.backend.plan_route_delete(route)
        except BackendError as exc:
            self._error("Route Error", str(exc))
            return
        self._confirm_and_run(plan)

    def add_nat_rule(self) -> None:
        try:
            plan = self.manager.plan_create_nat_rule(
                self.nat_name_edit.text(),
                self.nat_source_edit.text(),
                outbound_interface=self.nat_outbound_edit.text(),
                enabled=self.nat_enabled_check.isChecked(),
            )
        except (ValueError, BackendError) as exc:
            self._error("Invalid NAT Rule", str(exc))
            return
        self._confirm_and_run(plan)

    def update_selected_nat_rule(self) -> None:
        old_rule = self._selected_nat_rule()
        if old_rule is None:
            self._info("No NAT Rule Selected", "Select a NAT rule first.")
            return
        try:
            plan = self.manager.plan_update_nat_rule(
                old_rule,
                self.nat_name_edit.text(),
                self.nat_source_edit.text(),
                outbound_interface=self.nat_outbound_edit.text(),
                enabled=self.nat_enabled_check.isChecked(),
            )
        except (ValueError, BackendError, LookupError) as exc:
            self._error("Invalid NAT Rule", str(exc))
            return
        self._confirm_and_run(plan)

    def delete_selected_nat_rule(self) -> None:
        rule = self._selected_nat_rule()
        if rule is None:
            self._info("No NAT Rule Selected", "Select a NAT rule first.")
            return
        try:
            plan = self.manager.plan_delete_nat_rule(rule)
        except (BackendError, LookupError) as exc:
            self._error("NAT Error", str(exc))
            return
        self._confirm_and_run(plan)

    def export_current_configuration(self) -> None:
        path, _filter = QFileDialog.getSaveFileName(
            self,
            "Export Network Configuration",
            "",
            "JSON files (*.json);;All files (*)",
        )
        if not path:
            return
        if not Path(path).suffix:
            path = f"{path}.json"
        self.statusBar().showMessage("Exporting configuration snapshot...")
        self._run_background(
            lambda: self._export_configuration_to_path(path),
            self._on_configuration_exported,
            "Exporting configuration snapshot...",
        )

    def import_configuration_file(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self,
            "Import Network Configuration",
            "",
            "JSON files (*.json);;All files (*)",
        )
        if not path:
            return
        self.statusBar().showMessage("Importing configuration snapshot...")
        self._run_background(
            lambda: (path, self.manager.import_snapshot(path)),
            self._on_configuration_imported,
            "Importing configuration snapshot...",
        )

    def _on_configuration_imported(self, payload: object) -> None:
        path, snapshot = payload
        if not isinstance(snapshot, NetworkSnapshot):
            raise TypeError("Imported configuration did not return a snapshot.")
        self.imported_snapshot = snapshot
        self.config_text.setPlainText(
            f"Imported: {path}\n"
            f"Captured at: {snapshot.captured_at}\n"
            f"Source platform: {snapshot.platform}\n"
            f"Global IPv4 forwarding: {_format_forwarding(snapshot.global_forwarding_enabled)}\n"
            f"Adapters: {len(snapshot.adapters)}\n"
            f"Routes: {len(snapshot.routes)}\n"
            f"NAT rules: {len(snapshot.nat_rules)}\n\n"
            "Use Apply Imported Configuration to preview and apply this snapshot."
        )
        self.statusBar().showMessage("Imported configuration snapshot.")

    def apply_imported_configuration(self) -> None:
        if self.imported_snapshot is None:
            self._info("No Snapshot Imported", "Import a configuration file first.")
            return
        allow_mismatch = False
        if self.imported_snapshot.platform and self.imported_snapshot.platform != self.manager.backend_name:
            answer = QMessageBox.question(
                self,
                "Platform Mismatch",
                "This snapshot was captured on "
                f"{self.imported_snapshot.platform}, but this system is using "
                f"the {self.manager.backend_name} backend. Continue with best-effort apply?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            allow_mismatch = True
        self.statusBar().showMessage("Preparing imported configuration plan...")
        self._run_background(
            lambda: self.manager.plan_apply_snapshot(
                self.imported_snapshot,
                allow_platform_mismatch=allow_mismatch,
            ),
            self._confirm_and_run,
            "Preparing imported configuration plan...",
        )

    def _export_configuration_to_path(self, path: str) -> str:
        snapshot = NetworkSnapshot(
            platform=self.manager.backend_name,
            adapters=self.adapters or self.manager.list_adapters(),
            routes=self.routes or self.manager.list_routes(),
            nat_rules=self.nat_rules or self.manager.list_nat_rules(),
            virtual_adapters=self.virtual_adapters or self.manager.list_virtual_adapters(),
            global_forwarding_enabled=(
                self.global_forwarding_enabled
                if self.global_forwarding_enabled is not None
                else self.manager.get_global_forwarding_enabled()
            ),
        )
        self.manager.export_snapshot(path, snapshot)
        return path

    def _on_configuration_exported(self, path: object) -> None:
        self.statusBar().showMessage(f"Exported configuration to {path}")
        self._log(f"Exported configuration to {path}")

    def _confirm_and_run(self, plan_object: object) -> None:
        if not isinstance(plan_object, OperationPlan):
            raise TypeError("Expected an operation plan.")
        plan = plan_object
        if not self.manager.is_admin:
            self._warning(
                "Administrator Access Required",
                "This action changes system network settings. Restart Py NIC Manager "
                "as Administrator/root and try again.",
            )
            return
        if not plan.commands:
            notes = "\n".join(plan.notes) if plan.notes else "No system commands were generated."
            self._info("Nothing to Apply", notes)
            return

        dialog = PlanDialog(self, plan)
        if dialog.exec() != QDialog.DialogCode.Accepted or not dialog.confirmed:
            return
        self.statusBar().showMessage("Running network command plan...")
        self._active_plan = plan
        self._run_background(
            lambda: self.manager.run_plan(plan),
            self._on_plan_finished,
            "Running network command plan...",
        )

    def _on_plan_finished(self, results_object: object) -> None:
        results = list(results_object)
        failures: list[CommandResult] = []
        should_refresh = True
        for result in results:
            if isinstance(result, CommandResult):
                self._log(result.summary())
                if not result.ok:
                    failures.append(result)
        if failures:
            self._error("Command Failed", "\n\n".join(result.error_message() for result in failures[:3]))
            self.statusBar().showMessage(f"{len(failures)} command(s) failed.")
        else:
            self.statusBar().showMessage("Network command plan completed.")
            self._info("Done", "The network command plan completed.")
            if self._active_plan and self._active_plan.restart_required:
                should_refresh = not self._ask_restart_now()
        self._active_plan = None
        if should_refresh:
            self.refresh_all()

    def _ask_restart_now(self) -> bool:
        message = QMessageBox(self)
        message.setIcon(QMessageBox.Icon.Question)
        message.setWindowTitle("Restart Required")
        message.setText("This setting may require a restart to take effect. Restart now?")
        restart_button = message.addButton("Restart Now", QMessageBox.ButtonRole.AcceptRole)
        message.addButton("Later", QMessageBox.ButtonRole.RejectRole)
        message.exec()
        if message.clickedButton() == restart_button:
            self.statusBar().showMessage("Restarting system...")
            self._run_background(
                self.manager.backend.restart_system,
                self._on_restart_command_finished,
                "Restarting system...",
            )
            return True
        return False

    def _on_restart_command_finished(self, result: object) -> None:
        if isinstance(result, CommandResult):
            self._log(result.summary())
            if not result.ok:
                self._error("Restart Failed", result.summary())
                self.statusBar().showMessage("Restart command failed.")

    def _run_background(self, func: Callable[[], object], callback: Callable[[object], None], message: str) -> None:
        self._begin_busy(message)
        worker = Worker(func)
        self._workers.add(worker)
        worker.signals.succeeded.connect(lambda result, item=worker: self._finish_worker(callback, result, item))
        worker.signals.failed.connect(lambda exc, details, item=worker: self._worker_failed(exc, details, item))
        self._thread_pool.start(worker)

    def _finish_worker(self, callback: Callable[[object], None], result: object, worker: Worker) -> None:
        self._workers.discard(worker)
        self._end_busy()
        try:
            callback(result)
        except Exception as exc:
            self._log(str(exc))
            self._error("Operation Failed", str(exc))

    def _worker_failed(self, exc: object, details: str, worker: Worker) -> None:
        self._workers.discard(worker)
        self._end_busy()
        self.statusBar().showMessage("Operation failed.")
        self._log(details or str(exc))
        self._error("Operation Failed", str(exc))

    def _begin_busy(self, message: str) -> None:
        self._busy_depth += 1
        self.statusBar().showMessage(message)
        if self._busy_depth == 1:
            self.busy_overlay.show_busy(message)
        else:
            self.busy_overlay.message_label.setText(message)

    def _end_busy(self) -> None:
        if self._busy_depth > 0:
            self._busy_depth -= 1
        if self._busy_depth == 0:
            self.busy_overlay.hide_busy()

    def _selected_adapter(self) -> AdapterInfo | None:
        row = self.adapter_table.currentRow()
        if row < 0:
            return None
        item = self.adapter_table.item(row, 0)
        if item is None:
            return None
        index = item.data(INDEX_ROLE)
        if index is None:
            return None
        try:
            return self.adapters[int(index)]
        except (IndexError, ValueError):
            return None

    def _selected_route(self) -> RouteInfo | None:
        row = self.route_table.currentRow()
        if row < 0:
            return None
        item = self.route_table.item(row, 0)
        if item is None:
            return None
        index = item.data(INDEX_ROLE)
        if index is None:
            return None
        try:
            return self.routes[int(index)]
        except (IndexError, ValueError):
            return None

    def _selected_nat_rule(self) -> NatRule | None:
        row = self.nat_table.currentRow()
        if row < 0:
            return None
        item = self.nat_table.item(row, 0)
        if item is None:
            return None
        index = item.data(INDEX_ROLE)
        if index is None:
            return None
        try:
            return self.nat_rules[int(index)]
        except (IndexError, ValueError):
            return None

    def _selected_virtual_adapter(self) -> VirtualAdapterInfo | None:
        selected_adapter = self._selected_adapter()
        if selected_adapter is None:
            return None
        selected_name = selected_adapter.name.lower()
        for adapter in self.virtual_adapters:
            if adapter.name.lower() == selected_name or adapter.backend_id.lower() == selected_adapter.id.lower():
                return adapter
        if selected_adapter.is_virtual:
            ipv4 = _first_ipv4(selected_adapter)
            address = _format_address(ipv4)
            return VirtualAdapterInfo(
                name=selected_adapter.name,
                kind=selected_adapter.virtual_kind or "virtual",
                status=selected_adapter.status,
                address=address,
                source_cidr=_source_cidr_from_text(address),
                backend_id=selected_adapter.id,
                ics_compatible=selected_adapter.ics_compatible,
                ics_note=selected_adapter.ics_note,
            )
        return None

    def _refresh_loopback_suggestion(self) -> None:
        current = self.loopback_name_edit.text().strip()
        if current and current != self._last_suggested_loopback_value:
            return
        suggestion = self.manager.suggest_loopback_value(self.adapters)
        self._last_suggested_loopback_value = suggestion
        self.loopback_name_edit.setText(suggestion)

    def _refresh_virtual_suggestion(self) -> None:
        current = self.virtual_name_edit.text().strip()
        if current and current != self._last_suggested_virtual_value:
            return
        suggestion = self.manager.suggest_virtual_adapter_value(self.adapters)
        self._last_suggested_virtual_value = suggestion
        self.virtual_name_edit.setText(suggestion)

    def _refresh_global_forwarding_controls(self) -> None:
        self.global_forwarding_label.setText(
            f"Global IPv4 Forwarding: {_format_forwarding(self.global_forwarding_enabled)}"
        )
        self.global_forwarding_check.setChecked(bool(self.global_forwarding_enabled))

    def _log(self, message: str) -> None:
        self.log_text.appendPlainText(message.rstrip() + "\n")

    def _info(self, title: str, message: str) -> None:
        QMessageBox.information(self, title, message)

    def _warning(self, title: str, message: str) -> None:
        QMessageBox.warning(self, title, message)

    def _error(self, title: str, message: str) -> None:
        QMessageBox.critical(self, title, message)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt method name
        super().resizeEvent(event)
        if hasattr(self, "busy_overlay"):
            self.busy_overlay.setGeometry(self.centralWidget().rect())


def _table_item(text: str, sort_key: tuple, *, index: int, key: str) -> SortableTableItem:
    item = SortableTableItem(text)
    item.setData(SORT_ROLE, sort_key)
    item.setData(INDEX_ROLE, index)
    item.setData(KEY_ROLE, key)
    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
    return item


def _address_from_fields(ip_value: str, prefix_value: str) -> AddressInfo | None:
    address = ip_value.strip()
    if not address:
        return None
    prefix_text = prefix_value.strip() or "24"
    return AddressInfo(address=validate_ip(address), prefix_length=validate_prefix(prefix_text), family="ipv4")


def _address_from_text(value: str) -> AddressInfo | None:
    text = value.strip()
    if not text:
        return None
    if "/" in text:
        import ipaddress

        interface = ipaddress.ip_interface(text)
        return AddressInfo(str(interface.ip), int(interface.network.prefixlen), f"ipv{interface.ip.version}")
    return AddressInfo(address=validate_ip(text), prefix_length=24, family="ipv4")


def _first_ipv4(adapter: AdapterInfo) -> AddressInfo | None:
    return next((item for item in adapter.addresses if item.family.lower() == "ipv4"), None)


def _format_address(address: AddressInfo | None) -> str:
    if address is None:
        return ""
    if address.prefix_length is None:
        return address.address
    return f"{address.address}/{address.prefix_length}"


def _source_cidr_from_text(value: str) -> str:
    text = value.strip()
    if not text or "/" not in text:
        return ""
    try:
        import ipaddress

        return str(ipaddress.ip_interface(text).network)
    except ValueError:
        return ""


def _adapter_kind(adapter: AdapterInfo) -> str:
    if adapter.is_loopback:
        return "Loopback"
    if adapter.is_virtual:
        return f"Virtual ({adapter.virtual_kind})" if adapter.virtual_kind else "Virtual"
    return "Physical"


def _format_forwarding(value: bool | None) -> str:
    if value is None:
        return "Unknown"
    return "Enabled" if value else "Disabled"


def _format_admin_enabled(value: bool | None) -> str:
    if value is None:
        return "Unknown"
    return "Enabled" if value else "Disabled"


def _format_ics_compatible(adapter: AdapterInfo) -> str:
    if adapter.ics_compatible is True:
        return "Yes"
    if adapter.ics_compatible is False:
        return "No"
    return "Unknown" if adapter.is_virtual or adapter.is_loopback else "N/A"


def _format_bool(value: bool) -> str:
    return "Yes" if value else "No"


def _route_key(route: RouteInfo | None) -> str:
    if route is None:
        return ""
    return "|".join(
        [
            route.destination.strip().lower(),
            route.gateway.strip().lower(),
            route.interface.strip().lower(),
            "" if route.metric is None else str(route.metric),
            "" if route.interface_metric is None else str(route.interface_metric),
            "" if route.effective_metric is None else str(route.effective_metric),
        ]
    )


def _default_loopback_value(backend_name: str) -> str:
    if backend_name in {"macOS", "POSIX"}:
        return "127.0.0.2/32"
    return "py-loopback0"


def _default_virtual_adapter_value(backend_name: str) -> str:
    if backend_name in {"macOS", "POSIX"}:
        return "bridge0"
    return "py-virtual0"


def format_elapsed_time(seconds: int | float) -> str:
    total_seconds = max(0, int(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {seconds:02d}s"
    if minutes:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"


def apply_auto_theme(app: QApplication) -> None:
    dark = system_prefers_dark(app)
    app.setStyle("Fusion")
    app.setPalette(_palette(dark))
    app.setStyleSheet(_stylesheet(dark))


def system_prefers_dark(app: QApplication) -> bool:
    try:
        scheme = app.styleHints().colorScheme()
        if hasattr(Qt, "ColorScheme"):
            if scheme == Qt.ColorScheme.Dark:
                return True
            if scheme == Qt.ColorScheme.Light:
                return False
    except AttributeError:
        pass
    window_color = app.palette().color(QPalette.ColorRole.Window)
    return window_color.lightness() < 128


def _palette(dark: bool) -> QPalette:
    palette = QPalette()
    if dark:
        colors = {
            QPalette.ColorRole.Window: QColor("#111827"),
            QPalette.ColorRole.WindowText: QColor("#e5e7eb"),
            QPalette.ColorRole.Base: QColor("#0f172a"),
            QPalette.ColorRole.AlternateBase: QColor("#162033"),
            QPalette.ColorRole.ToolTipBase: QColor("#1f2937"),
            QPalette.ColorRole.ToolTipText: QColor("#f9fafb"),
            QPalette.ColorRole.Text: QColor("#e5e7eb"),
            QPalette.ColorRole.Button: QColor("#1f2937"),
            QPalette.ColorRole.ButtonText: QColor("#f9fafb"),
            QPalette.ColorRole.Highlight: QColor("#2563eb"),
            QPalette.ColorRole.HighlightedText: QColor("#ffffff"),
        }
    else:
        colors = {
            QPalette.ColorRole.Window: QColor("#f6f8fb"),
            QPalette.ColorRole.WindowText: QColor("#111827"),
            QPalette.ColorRole.Base: QColor("#ffffff"),
            QPalette.ColorRole.AlternateBase: QColor("#f1f5f9"),
            QPalette.ColorRole.ToolTipBase: QColor("#ffffff"),
            QPalette.ColorRole.ToolTipText: QColor("#111827"),
            QPalette.ColorRole.Text: QColor("#111827"),
            QPalette.ColorRole.Button: QColor("#ffffff"),
            QPalette.ColorRole.ButtonText: QColor("#111827"),
            QPalette.ColorRole.Highlight: QColor("#2563eb"),
            QPalette.ColorRole.HighlightedText: QColor("#ffffff"),
        }
    for role, color in colors.items():
        palette.setColor(role, color)
    return palette


def _stylesheet(dark: bool) -> str:
    if dark:
        return """
QWidget#root { background: #111827; color: #e5e7eb; }
QFrame#topBar, QFrame#sidePanel {
  background: #182235; border: 1px solid #2d3a52; border-radius: 6px;
}
QLabel#appTitle { font-size: 18px; font-weight: 700; }
QLabel#sectionLabel, QLabel#dialogTitle, QLabel#busyTitle { font-size: 14px; font-weight: 700; }
QLabel#goodText { color: #86efac; }
QLabel#dangerText { color: #fca5a5; }
QTabWidget::pane { border: 1px solid #2d3a52; border-radius: 6px; top: -1px; }
QTabBar::tab {
  background: #182235; color: #cbd5e1; padding: 8px 14px;
  border: 1px solid #2d3a52; border-bottom: none;
}
QTabBar::tab:selected { background: #0f172a; color: #ffffff; }
QTableWidget, QPlainTextEdit, QLineEdit {
  background: #0f172a; color: #e5e7eb; border: 1px solid #2d3a52; border-radius: 6px;
}
QTableWidget::item { padding: 4px; }
QTableWidget::item:selected { background: #1d4ed8; color: #ffffff; }
QHeaderView::section {
  background: #182235; color: #e5e7eb; padding: 7px; border: none; border-right: 1px solid #2d3a52;
}
QPushButton {
  background: #253247; color: #f8fafc; border: 1px solid #3a4a64; border-radius: 6px; padding: 7px 12px;
}
QPushButton:hover { background: #30405a; }
QPushButton:disabled, QLineEdit:disabled, QCheckBox:disabled { color: #64748b; background: #172033; }
QPushButton#primaryButton { background: #2563eb; border-color: #2563eb; color: #ffffff; }
QPushButton#primaryButton:hover { background: #1d4ed8; }
QScrollArea#sideScroll { background: transparent; }
QFrame#separator { color: #2d3a52; }
QWidget#busyOverlay { background: rgba(15, 23, 42, 178); }
QFrame#busyPanel { background: #182235; border: 1px solid #3a4a64; border-radius: 8px; }
QProgressBar { border: 1px solid #3a4a64; border-radius: 4px; background: #0f172a; height: 8px; }
QProgressBar::chunk { background: #38bdf8; border-radius: 4px; }
"""
    return """
QWidget#root { background: #f6f8fb; color: #111827; }
QFrame#topBar, QFrame#sidePanel {
  background: #ffffff; border: 1px solid #d8e0ea; border-radius: 6px;
}
QLabel#appTitle { font-size: 18px; font-weight: 700; }
QLabel#sectionLabel, QLabel#dialogTitle, QLabel#busyTitle { font-size: 14px; font-weight: 700; }
QLabel#goodText { color: #15803d; }
QLabel#dangerText { color: #b91c1c; }
QTabWidget::pane { border: 1px solid #d8e0ea; border-radius: 6px; top: -1px; }
QTabBar::tab {
  background: #edf2f7; color: #334155; padding: 8px 14px;
  border: 1px solid #d8e0ea; border-bottom: none;
}
QTabBar::tab:selected { background: #ffffff; color: #111827; }
QTableWidget, QPlainTextEdit, QLineEdit {
  background: #ffffff; color: #111827; border: 1px solid #d8e0ea; border-radius: 6px;
}
QTableWidget::item { padding: 4px; }
QTableWidget::item:selected { background: #2563eb; color: #ffffff; }
QHeaderView::section {
  background: #eef3f8; color: #111827; padding: 7px; border: none; border-right: 1px solid #d8e0ea;
}
QPushButton {
  background: #ffffff; color: #111827; border: 1px solid #cbd5e1; border-radius: 6px; padding: 7px 12px;
}
QPushButton:hover { background: #f1f5f9; }
QPushButton:disabled, QLineEdit:disabled, QCheckBox:disabled { color: #94a3b8; background: #f1f5f9; }
QPushButton#primaryButton { background: #2563eb; border-color: #2563eb; color: #ffffff; }
QPushButton#primaryButton:hover { background: #1d4ed8; }
QScrollArea#sideScroll { background: transparent; }
QFrame#separator { color: #d8e0ea; }
QWidget#busyOverlay { background: rgba(226, 232, 240, 178); }
QFrame#busyPanel { background: #ffffff; border: 1px solid #cbd5e1; border-radius: 8px; }
QProgressBar { border: 1px solid #cbd5e1; border-radius: 4px; background: #e2e8f0; height: 8px; }
QProgressBar::chunk { background: #0ea5e9; border-radius: 4px; }
"""


def main() -> None:
    app = QApplication(sys.argv)
    apply_auto_theme(app)
    try:
        app.styleHints().colorSchemeChanged.connect(lambda _scheme: apply_auto_theme(app))
    except AttributeError:
        pass
    window = NetworkManagerQtWindow()
    window.show()
    sys.exit(app.exec())
