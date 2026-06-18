from __future__ import annotations

import ipaddress
import queue
import threading
import time
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor
from tkinter import filedialog, messagebox, scrolledtext, ttk

from .admin import is_admin
from .backends import BackendError, BaseBackend, get_backend
from .io import export_snapshot, import_snapshot
from .models import AdapterInfo, AddressInfo, NatRule, NetworkSnapshot, OperationPlan, RouteInfo, VirtualAdapterInfo
from .tk_fonts import configure_tk_fonts
from .validation import parse_csv, validate_ip, validate_network, validate_prefix


class NetworkManagerApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Py NIC Manager")
        self.geometry("1120x720")
        self.minsize(980, 620)

        self.backend: BaseBackend = get_backend()
        self.is_admin = is_admin()
        self.adapters: list[AdapterInfo] = []
        self.routes: list[RouteInfo] = []
        self.nat_rules: list[NatRule] = []
        self.virtual_adapters: list[VirtualAdapterInfo] = []
        self.global_forwarding_enabled: bool | None = None
        self.imported_snapshot: NetworkSnapshot | None = None
        self._queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self._busy_depth = 0
        self._busy_message_var = tk.StringVar(value="")
        self._busy_elapsed_var = tk.StringVar(value="Elapsed: 0s")
        self._busy_started_at: float | None = None
        self._busy_elapsed_after_id: str | None = None
        self._admin_only_widgets: list[tk.Widget] = []
        self._last_suggested_loopback_value = _default_loopback_value(self.backend.name)
        self._last_suggested_virtual_value = _default_virtual_adapter_value(self.backend.name)
        self._adapter_sort_column = "index"
        self._adapter_sort_descending = False
        self._route_sort_column = "destination"
        self._route_sort_descending = False
        self._nat_sort_column = "name"
        self._nat_sort_descending = False
        self._optional_load_errors: list[str] = []
        self._active_plan: OperationPlan | None = None
        self.ui_font_family = configure_tk_fonts(self)
        self.ui_text_font = (self.ui_font_family, 10)
        self.ui_heading_font = (self.ui_font_family, 11, "bold")

        self._build_style()
        self._build_layout()
        self._set_mutating_controls_state()
        self._poll_queue()
        self.refresh_all()

    def _build_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Header.TLabel", font=self.ui_heading_font)
        style.configure("Danger.TLabel", foreground="#9b1c1c")
        style.configure("Good.TLabel", foreground="#166534")
        style.configure("Action.TButton", padding=(12, 6))
        style.configure("Treeview", font=self.ui_text_font, rowheight=24)
        style.configure("Treeview.Heading", font=self.ui_heading_font)

    def _build_layout(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        banner_frame = ttk.Frame(self, padding=(12, 10, 12, 6))
        banner_frame.grid(row=0, column=0, sticky="ew")
        banner_frame.columnconfigure(1, weight=1)

        status_style = "Good.TLabel" if self.is_admin else "Danger.TLabel"
        status_text = (
            f"{self.backend.name} backend - administrator access is active."
            if self.is_admin
            else (
                f"{self.backend.name} backend - read-only mode. Restart this app as "
                "Administrator/root to change adapters, routes, or loopback devices."
            )
        )
        self.global_forwarding_status_var = tk.StringVar(value="Global IPv4 Forwarding: Unknown")
        self.global_forwarding_var = tk.BooleanVar(value=False)
        ttk.Label(banner_frame, text="Py NIC Manager", style="Header.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(banner_frame, text=status_text, style=status_style).grid(row=0, column=1, sticky="w", padx=(16, 0))
        ttk.Button(banner_frame, text="Refresh", command=self.refresh_all).grid(row=0, column=4, sticky="e")
        ttk.Label(banner_frame, textvariable=self.global_forwarding_status_var).grid(
            row=1,
            column=0,
            columnspan=2,
            sticky="w",
            pady=(6, 0),
        )
        self.global_forwarding_check = ttk.Checkbutton(
            banner_frame,
            text="Enable global IPv4 forwarding",
            variable=self.global_forwarding_var,
        )
        self.global_forwarding_check.grid(row=1, column=2, sticky="e", padx=(12, 8), pady=(6, 0))
        self._admin_only_widgets.append(self.global_forwarding_check)
        self.apply_global_forwarding_button = ttk.Button(
            banner_frame,
            text="Apply Global Forwarding",
            command=self.apply_global_forwarding,
        )
        self.apply_global_forwarding_button.grid(row=1, column=3, sticky="e", padx=(0, 8), pady=(6, 0))
        self._admin_only_widgets.append(self.apply_global_forwarding_button)

        self.notebook = ttk.Notebook(self)
        self.notebook.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 8))

        self.adapters_tab = ttk.Frame(self.notebook, padding=10)
        self.routes_tab = ttk.Frame(self.notebook, padding=10)
        self.nat_tab = ttk.Frame(self.notebook, padding=10)
        self.config_tab = ttk.Frame(self.notebook, padding=10)
        self.log_tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(self.adapters_tab, text="Adapters")
        self.notebook.add(self.routes_tab, text="Routes")
        self.notebook.add(self.nat_tab, text="NAT")
        self.notebook.add(self.config_tab, text="Configuration")
        self.notebook.add(self.log_tab, text="Log")

        self._build_adapters_tab()
        self._build_routes_tab()
        self._build_nat_tab()
        self._build_config_tab()
        self._build_log_tab()

        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(self, textvariable=self.status_var, anchor="w", padding=(12, 5)).grid(row=2, column=0, sticky="ew")

        self.busy_overlay = tk.Frame(self, bg="#d8d8d8", cursor="watch")
        self.busy_overlay.grid(row=0, column=0, rowspan=3, sticky="nsew")
        self.busy_overlay.grid_remove()
        self.busy_overlay.bind("<Button>", lambda _event: "break")
        self.busy_overlay.bind("<ButtonRelease>", lambda _event: "break")
        self.busy_overlay.bind("<Key>", lambda _event: "break")
        self.busy_overlay.bind("<Motion>", lambda _event: "break")
        busy_panel = ttk.Frame(self.busy_overlay, padding=18)
        busy_panel.place(relx=0.5, rely=0.5, anchor="center")
        ttk.Label(busy_panel, text="Working", style="Header.TLabel").grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(busy_panel, textvariable=self._busy_message_var).grid(row=1, column=0, sticky="ew", pady=(0, 6))
        ttk.Label(busy_panel, textvariable=self._busy_elapsed_var).grid(row=2, column=0, sticky="ew", pady=(0, 10))
        self.busy_progress = ttk.Progressbar(busy_panel, mode="indeterminate", length=260)
        self.busy_progress.grid(row=3, column=0, sticky="ew")

    def _build_split_tab(self, tab: ttk.Frame) -> tuple[ttk.Panedwindow, ttk.Frame, ttk.Frame]:
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(0, weight=1)
        paned = ttk.Panedwindow(tab, orient=tk.HORIZONTAL)
        paned.grid(row=0, column=0, sticky="nsew")
        table_frame = ttk.Frame(paned, width=720)
        panel = ttk.Frame(paned, padding=(12, 0, 0, 0), width=320)
        paned.add(table_frame, weight=3)
        paned.add(panel, weight=1)
        return paned, table_frame, panel

    def _grid_scrollable_tree(self, parent: ttk.Frame, tree: ttk.Treeview) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)
        tree.grid(row=0, column=0, sticky="nsew")
        y_scroll = ttk.Scrollbar(parent, orient="vertical", command=tree.yview)
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll = ttk.Scrollbar(parent, orient="horizontal", command=tree.xview)
        x_scroll.grid(row=1, column=0, sticky="ew")
        tree.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)

    def _build_adapters_tab(self) -> None:
        self.adapters_paned, table_frame, panel = self._build_split_tab(self.adapters_tab)

        columns = ("index", "status", "forwarding", "ipv4", "mac", "gateway", "dns", "kind")
        self.adapter_tree = ttk.Treeview(
            table_frame,
            columns=columns,
            show="tree headings",
            selectmode="browse",
        )
        self._set_adapter_heading("#0", "Adapter", "name")
        self._set_adapter_heading("index", "Index", "index")
        self._set_adapter_heading("status", "Status", "status")
        self._set_adapter_heading("forwarding", "IP Forwarding", "forwarding")
        self._set_adapter_heading("ipv4", "IPv4", "ipv4")
        self._set_adapter_heading("mac", "MAC", "mac")
        self._set_adapter_heading("gateway", "Gateway", "gateway")
        self._set_adapter_heading("dns", "DNS", "dns")
        self._set_adapter_heading("kind", "Type", "kind")
        self.adapter_tree.column("#0", width=190, minwidth=160)
        self.adapter_tree.column("index", width=70, anchor="center")
        self.adapter_tree.column("status", width=90, anchor="center")
        self.adapter_tree.column("forwarding", width=105, anchor="center")
        self.adapter_tree.column("ipv4", width=170)
        self.adapter_tree.column("mac", width=145)
        self.adapter_tree.column("gateway", width=140)
        self.adapter_tree.column("dns", width=190)
        self.adapter_tree.column("kind", width=90, anchor="center")
        self.adapter_tree.bind("<<TreeviewSelect>>", self._on_adapter_select)
        self._grid_scrollable_tree(table_frame, self.adapter_tree)
        panel.columnconfigure(1, weight=1)

        ttk.Label(panel, text="Adapter Settings", style="Header.TLabel").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))

        self.adapter_name_var = tk.StringVar()
        self.adapter_mac_var = tk.StringVar()
        self.adapter_ip_var = tk.StringVar()
        self.adapter_prefix_var = tk.StringVar()
        self.adapter_gateway_var = tk.StringVar()
        self.adapter_dns_var = tk.StringVar()
        self.adapter_dhcp_var = tk.BooleanVar(value=False)
        self.adapter_forwarding_var = tk.BooleanVar(value=True)

        self._labeled_entry(panel, "Name", self.adapter_name_var, 1, readonly=True)
        self._labeled_entry(panel, "MAC address", self.adapter_mac_var, 2, admin_required=True)
        self._labeled_entry(panel, "IPv4 address", self.adapter_ip_var, 3, admin_required=True)
        self._labeled_entry(panel, "Prefix length", self.adapter_prefix_var, 4, admin_required=True)
        self._labeled_entry(panel, "Gateway", self.adapter_gateway_var, 5, admin_required=True)
        self._labeled_entry(panel, "DNS servers", self.adapter_dns_var, 6, admin_required=True)
        self.adapter_dhcp_check = ttk.Checkbutton(panel, text="Use DHCP for IPv4", variable=self.adapter_dhcp_var)
        self.adapter_dhcp_check.grid(row=7, column=0, columnspan=2, sticky="w", pady=(4, 10))
        self._admin_only_widgets.append(self.adapter_dhcp_check)
        self.adapter_forwarding_check = ttk.Checkbutton(
            panel,
            text="Enable IPv4 router forwarding",
            variable=self.adapter_forwarding_var,
        )
        self.adapter_forwarding_check.grid(row=8, column=0, columnspan=2, sticky="w", pady=(0, 10))
        self._admin_only_widgets.append(self.adapter_forwarding_check)

        self.apply_adapter_button = ttk.Button(
            panel,
            text="Apply Adapter Changes",
            style="Action.TButton",
            command=self.apply_selected_adapter,
        )
        self.apply_adapter_button.grid(row=9, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        self._admin_only_widgets.append(self.apply_adapter_button)
        self.apply_forwarding_button = ttk.Button(
            panel,
            text="Apply Forwarding",
            command=self.apply_selected_adapter_forwarding,
        )
        self.apply_forwarding_button.grid(row=10, column=0, columnspan=2, sticky="ew", pady=(0, 12))
        self._admin_only_widgets.append(self.apply_forwarding_button)

        ttk.Separator(panel).grid(row=11, column=0, columnspan=2, sticky="ew", pady=8)
        ttk.Label(panel, text="Loopback", style="Header.TLabel").grid(row=12, column=0, columnspan=2, sticky="w", pady=(0, 8))
        self.loopback_name_var = tk.StringVar(value=self._last_suggested_loopback_value)
        self._labeled_entry(panel, "Name or alias/address", self.loopback_name_var, 13, admin_required=True)
        self.create_loopback_button = ttk.Button(
            panel,
            text="Create Loopback",
            command=self.create_loopback,
        )
        self.create_loopback_button.grid(row=14, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        self._admin_only_widgets.append(self.create_loopback_button)
        self.delete_loopback_button = ttk.Button(
            panel,
            text="Delete Selected Loopback",
            command=self.delete_selected_loopback,
        )
        self.delete_loopback_button.grid(row=15, column=0, columnspan=2, sticky="ew")
        self._admin_only_widgets.append(self.delete_loopback_button)

        ttk.Separator(panel).grid(row=16, column=0, columnspan=2, sticky="ew", pady=8)
        ttk.Label(panel, text="Virtual NIC", style="Header.TLabel").grid(row=17, column=0, columnspan=2, sticky="w", pady=(0, 8))
        self.virtual_name_var = tk.StringVar(value=self._last_suggested_virtual_value)
        self.virtual_address_var = tk.StringVar(value="192.168.56.1/24")
        self._labeled_entry(panel, "Name", self.virtual_name_var, 18, admin_required=True)
        self._labeled_entry(panel, "IPv4 CIDR", self.virtual_address_var, 19, admin_required=True)
        self.create_virtual_button = ttk.Button(
            panel,
            text="Create Virtual NIC",
            command=self.create_virtual_adapter,
        )
        self.create_virtual_button.grid(row=20, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        self._admin_only_widgets.append(self.create_virtual_button)
        self.delete_virtual_button = ttk.Button(
            panel,
            text="Delete Selected Virtual NIC",
            command=self.delete_selected_virtual_adapter,
        )
        self.delete_virtual_button.grid(row=21, column=0, columnspan=2, sticky="ew")
        self._admin_only_widgets.append(self.delete_virtual_button)

    def _build_routes_tab(self) -> None:
        self.routes_paned, table_frame, panel = self._build_split_tab(self.routes_tab)

        columns = ("gateway", "interface", "route_metric", "interface_metric", "effective_metric", "protocol", "table")
        self.route_tree = ttk.Treeview(
            table_frame,
            columns=columns,
            show="tree headings",
            selectmode="browse",
        )
        self._set_route_heading("#0", "Destination", "destination")
        self._set_route_heading("gateway", "Gateway", "gateway")
        self._set_route_heading("interface", "Interface", "interface")
        self._set_route_heading("route_metric", "Route Metric", "route_metric")
        self._set_route_heading("interface_metric", "Interface Metric", "interface_metric")
        self._set_route_heading("effective_metric", "Effective Metric", "effective_metric")
        self._set_route_heading("protocol", "Protocol", "protocol")
        self._set_route_heading("table", "Table", "table")
        self.route_tree.column("#0", width=190, minwidth=150)
        self.route_tree.column("gateway", width=135)
        self.route_tree.column("interface", width=150)
        self.route_tree.column("route_metric", width=105, anchor="center")
        self.route_tree.column("interface_metric", width=120, anchor="center")
        self.route_tree.column("effective_metric", width=120, anchor="center")
        self.route_tree.column("protocol", width=95)
        self.route_tree.column("table", width=75)
        self.route_tree.bind("<<TreeviewSelect>>", self._on_route_select)
        self._grid_scrollable_tree(table_frame, self.route_tree)
        panel.columnconfigure(1, weight=1)

        ttk.Label(panel, text="Route Editor", style="Header.TLabel").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))
        self.route_destination_var = tk.StringVar(value="0.0.0.0/0")
        self.route_gateway_var = tk.StringVar()
        self.route_interface_var = tk.StringVar()
        self.route_metric_var = tk.StringVar()

        self._labeled_entry(panel, "Destination", self.route_destination_var, 1, admin_required=True)
        self._labeled_entry(panel, "Gateway", self.route_gateway_var, 2, admin_required=True)
        self._labeled_entry(panel, "Interface", self.route_interface_var, 3, admin_required=True)
        self._labeled_entry(panel, "Route metric", self.route_metric_var, 4, admin_required=True)

        self.add_route_button = ttk.Button(panel, text="Add Route", command=self.add_route)
        self.add_route_button.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(8, 6))
        self._admin_only_widgets.append(self.add_route_button)
        self.update_route_button = ttk.Button(panel, text="Update Selected Route", command=self.update_selected_route)
        self.update_route_button.grid(row=6, column=0, columnspan=2, sticky="ew", pady=6)
        self._admin_only_widgets.append(self.update_route_button)
        self.delete_route_button = ttk.Button(panel, text="Delete Selected Route", command=self.delete_selected_route)
        self.delete_route_button.grid(row=7, column=0, columnspan=2, sticky="ew", pady=6)
        self._admin_only_widgets.append(self.delete_route_button)

    def _build_nat_tab(self) -> None:
        self.nat_paned, table_frame, panel = self._build_split_tab(self.nat_tab)

        columns = ("source_cidr", "outbound_interface", "enabled", "persistent", "managed")
        self.nat_tree = ttk.Treeview(
            table_frame,
            columns=columns,
            show="tree headings",
            selectmode="browse",
        )
        self._set_nat_heading("#0", "Name", "name")
        self._set_nat_heading("source_cidr", "Source CIDR", "source_cidr")
        self._set_nat_heading("outbound_interface", "Outbound Interface", "outbound_interface")
        self._set_nat_heading("enabled", "Enabled", "enabled")
        self._set_nat_heading("persistent", "Persistent", "persistent")
        self._set_nat_heading("managed", "Managed", "managed")
        self.nat_tree.column("#0", width=170, minwidth=130)
        self.nat_tree.column("source_cidr", width=150)
        self.nat_tree.column("outbound_interface", width=170)
        self.nat_tree.column("enabled", width=80, anchor="center")
        self.nat_tree.column("persistent", width=90, anchor="center")
        self.nat_tree.column("managed", width=90, anchor="center")
        self.nat_tree.bind("<<TreeviewSelect>>", self._on_nat_select)
        self._grid_scrollable_tree(table_frame, self.nat_tree)
        panel.columnconfigure(1, weight=1)

        ttk.Label(panel, text="NAT Rule Editor", style="Header.TLabel").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))
        self.nat_name_var = tk.StringVar(value="py-nat0")
        self.nat_source_var = tk.StringVar(value="192.168.0.0/24")
        self.nat_outbound_var = tk.StringVar()
        self.nat_enabled_var = tk.BooleanVar(value=True)

        self._labeled_entry(panel, "Name", self.nat_name_var, 1, admin_required=True)
        self._labeled_entry(panel, "Source CIDR", self.nat_source_var, 2, admin_required=True)
        self._labeled_entry(panel, "Outbound Interface", self.nat_outbound_var, 3, admin_required=True)
        self.nat_enabled_check = ttk.Checkbutton(panel, text="Enable NAT rule", variable=self.nat_enabled_var)
        self.nat_enabled_check.grid(row=4, column=0, columnspan=2, sticky="w", pady=(4, 10))
        self._admin_only_widgets.append(self.nat_enabled_check)

        self.add_nat_button = ttk.Button(panel, text="Add NAT Rule", command=self.add_nat_rule)
        self.add_nat_button.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(8, 6))
        self._admin_only_widgets.append(self.add_nat_button)
        self.update_nat_button = ttk.Button(panel, text="Update Selected NAT Rule", command=self.update_selected_nat_rule)
        self.update_nat_button.grid(row=6, column=0, columnspan=2, sticky="ew", pady=6)
        self._admin_only_widgets.append(self.update_nat_button)
        self.delete_nat_button = ttk.Button(panel, text="Delete Selected NAT Rule", command=self.delete_selected_nat_rule)
        self.delete_nat_button.grid(row=7, column=0, columnspan=2, sticky="ew", pady=6)
        self._admin_only_widgets.append(self.delete_nat_button)

    def _build_config_tab(self) -> None:
        self.config_tab.columnconfigure(0, weight=1)
        self.config_tab.rowconfigure(2, weight=1)

        ttk.Label(self.config_tab, text="Configuration Snapshots", style="Header.TLabel").grid(row=0, column=0, sticky="w")
        buttons = ttk.Frame(self.config_tab)
        buttons.grid(row=1, column=0, sticky="ew", pady=10)
        buttons.columnconfigure(3, weight=1)

        ttk.Button(buttons, text="Export Current Configuration", command=self.export_current_configuration).grid(row=0, column=0, padx=(0, 8))
        ttk.Button(buttons, text="Import Configuration File", command=self.import_configuration_file).grid(row=0, column=1, padx=8)
        self.apply_config_button = ttk.Button(buttons, text="Apply Imported Configuration", command=self.apply_imported_configuration)
        self.apply_config_button.grid(row=0, column=2, padx=8)
        self._admin_only_widgets.append(self.apply_config_button)

        self.config_text = scrolledtext.ScrolledText(
            self.config_tab,
            font=self.ui_text_font,
            height=12,
            wrap="word",
        )
        self.config_text.grid(row=2, column=0, sticky="nsew")
        self.config_text.insert(
            "1.0",
            "Export saves the current adapters and route table as JSON.\n"
            "Import loads a saved snapshot and can apply it after a command preview.",
        )
        self.config_text.configure(state="disabled")

    def _build_log_tab(self) -> None:
        self.log_tab.columnconfigure(0, weight=1)
        self.log_tab.rowconfigure(0, weight=1)
        self.log_text = scrolledtext.ScrolledText(self.log_tab, font=self.ui_text_font, wrap="word")
        self.log_text.grid(row=0, column=0, sticky="nsew")
        self.log_text.configure(state="disabled")

    def _labeled_entry(
        self,
        parent: ttk.Frame,
        label: str,
        variable: tk.StringVar,
        row: int,
        *,
        readonly: bool = False,
        admin_required: bool = False,
    ) -> ttk.Entry:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=4)
        entry = ttk.Entry(parent, textvariable=variable, state="readonly" if readonly else "normal")
        entry.grid(row=row, column=1, sticky="ew", pady=4)
        if admin_required:
            self._admin_only_widgets.append(entry)
        return entry

    def _set_mutating_controls_state(self) -> None:
        state = "normal" if self.is_admin else "disabled"
        for widget in self._admin_only_widgets:
            widget.configure(state=state)

    def refresh_all(self) -> None:
        self.status_var.set("Loading adapters and routes...")
        self._run_background(
            self._load_network_state,
            self._on_network_state_loaded,
            busy_message="Loading adapters and routes...",
        )

    def _load_network_state(
        self,
    ) -> tuple[list[AdapterInfo], list[RouteInfo], list[NatRule], list[VirtualAdapterInfo], bool | None]:
        with ThreadPoolExecutor(max_workers=5) as executor:
            adapters_future = executor.submit(self.backend.list_adapters)
            routes_future = executor.submit(self.backend.list_routes)
            nat_future = executor.submit(self.backend.list_nat_rules)
            list_virtual_adapters = getattr(self.backend, "list_virtual_adapters", lambda: [])
            virtual_future = executor.submit(list_virtual_adapters)
            global_forwarding_future = executor.submit(self.backend.get_global_forwarding_enabled)
            optional_errors: list[str] = []
            nat_rules = _future_result_or(nat_future, [], "NAT rules", optional_errors)
            virtual_adapters = _future_result_or(virtual_future, [], "Virtual adapters", optional_errors)
            global_forwarding = _future_result_or(
                global_forwarding_future,
                None,
                "Global forwarding state",
                optional_errors,
            )
            self._optional_load_errors = optional_errors
            return (
                adapters_future.result(),
                routes_future.result(),
                nat_rules,
                virtual_adapters,
                global_forwarding,
            )

    def _on_network_state_loaded(
        self,
        payload: tuple[list[AdapterInfo], list[RouteInfo], list[NatRule], list[VirtualAdapterInfo], bool | None],
    ) -> None:
        self.adapters, self.routes, self.nat_rules, self.virtual_adapters, self.global_forwarding_enabled = payload
        self._refresh_global_forwarding_controls()
        self._refresh_loopback_suggestion()
        self._refresh_virtual_suggestion()
        self._populate_adapters()
        self._populate_routes()
        self._populate_nat_rules()
        status = (
            f"Loaded {len(self.adapters)} adapters, {len(self.routes)} routes, "
            f"{len(self.nat_rules)} NAT rules, and {len(self.virtual_adapters)} virtual NICs."
        )
        if self._optional_load_errors:
            status += " Some optional status items are unavailable; see the log."
            for message in self._optional_load_errors:
                self._log(message)
        self.status_var.set(status)
        self._log(f"Refreshed state from the {self.backend.name} backend.")

    def _populate_adapters(self) -> None:
        selected = self._selected_adapter_id()
        self.adapter_tree.delete(*self.adapter_tree.get_children())
        self._refresh_adapter_headings()
        for index, adapter in self._sorted_adapter_items():
            ipv4 = _first_ipv4(adapter)
            iid = str(index)
            self.adapter_tree.insert(
                "",
                "end",
                iid=iid,
                text=adapter.name,
                values=(
                    index,
                    adapter.status,
                    _format_forwarding(adapter.forwarding_enabled),
                    _format_address(ipv4),
                    adapter.mac,
                    ", ".join(adapter.gateways),
                    ", ".join(adapter.dns_servers),
                    _adapter_kind(adapter),
                ),
            )
        if selected is not None and selected in self.adapter_tree.get_children():
            self.adapter_tree.selection_set(selected)

    def _set_adapter_heading(self, column_id: str, label: str, sort_column: str) -> None:
        self.adapter_tree.heading(
            column_id,
            text=label,
            command=lambda column=sort_column: self._sort_adapters_by(column),
        )

    def _refresh_adapter_headings(self) -> None:
        labels = {
            "name": ("#0", "Adapter"),
            "index": ("index", "Index"),
            "status": ("status", "Status"),
            "forwarding": ("forwarding", "IP Forwarding"),
            "ipv4": ("ipv4", "IPv4"),
            "mac": ("mac", "MAC"),
            "gateway": ("gateway", "Gateway"),
            "dns": ("dns", "DNS"),
            "kind": ("kind", "Type"),
        }
        for sort_column, (column_id, label) in labels.items():
            indicator = ""
            if sort_column == self._adapter_sort_column:
                indicator = " v" if self._adapter_sort_descending else " ^"
            self._set_adapter_heading(column_id, label + indicator, sort_column)

    def _sort_adapters_by(self, column: str) -> None:
        if self._adapter_sort_column == column:
            self._adapter_sort_descending = not self._adapter_sort_descending
        else:
            self._adapter_sort_column = column
            self._adapter_sort_descending = False
        self._populate_adapters()

    def _sorted_adapter_items(self) -> list[tuple[int, AdapterInfo]]:
        items = list(enumerate(self.adapters))
        return sorted(
            items,
            key=lambda item: self._adapter_sort_key(item[0], item[1]),
            reverse=self._adapter_sort_descending,
        )

    def _adapter_sort_key(self, index: int, adapter: AdapterInfo) -> tuple[int, str]:
        column = self._adapter_sort_column
        ipv4 = _first_ipv4(adapter)
        values = {
            "name": adapter.name,
            "index": str(index),
            "status": adapter.status,
            "forwarding": _format_forwarding(adapter.forwarding_enabled),
            "ipv4": _format_address(ipv4),
            "mac": adapter.mac,
            "gateway": ", ".join(adapter.gateways),
            "dns": ", ".join(adapter.dns_servers),
            "kind": _adapter_kind(adapter),
        }
        if column == "index":
            return (0, values["index"].zfill(8))
        value = values.get(column, adapter.name).strip().lower()
        return (0 if value else 1, value)

    def _populate_routes(self) -> None:
        selected = self._selected_route_id()
        self.route_tree.delete(*self.route_tree.get_children())
        self._refresh_route_headings()
        for index, route in self._sorted_route_items():
            iid = str(index)
            self.route_tree.insert(
                "",
                "end",
                iid=iid,
                text=route.destination,
                values=(
                    route.gateway,
                    route.interface,
                    "" if route.metric is None else str(route.metric),
                    "" if route.interface_metric is None else str(route.interface_metric),
                    "" if route.effective_metric is None else str(route.effective_metric),
                    route.protocol,
                    route.table,
                ),
            )
        if selected is not None and selected in self.route_tree.get_children():
            self.route_tree.selection_set(selected)

    def _set_route_heading(self, column_id: str, label: str, sort_column: str) -> None:
        self.route_tree.heading(
            column_id,
            text=label,
            command=lambda column=sort_column: self._sort_routes_by(column),
        )

    def _refresh_route_headings(self) -> None:
        labels = {
            "destination": ("#0", "Destination"),
            "gateway": ("gateway", "Gateway"),
            "interface": ("interface", "Interface"),
            "route_metric": ("route_metric", "Route Metric"),
            "interface_metric": ("interface_metric", "Interface Metric"),
            "effective_metric": ("effective_metric", "Effective Metric"),
            "protocol": ("protocol", "Protocol"),
            "table": ("table", "Table"),
        }
        for sort_column, (column_id, label) in labels.items():
            indicator = ""
            if sort_column == self._route_sort_column:
                indicator = " v" if self._route_sort_descending else " ^"
            self._set_route_heading(column_id, label + indicator, sort_column)

    def _sort_routes_by(self, column: str) -> None:
        if self._route_sort_column == column:
            self._route_sort_descending = not self._route_sort_descending
        else:
            self._route_sort_column = column
            self._route_sort_descending = False
        self._populate_routes()

    def _sorted_route_items(self) -> list[tuple[int, RouteInfo]]:
        items = list(enumerate(self.routes))
        return sorted(
            items,
            key=lambda item: route_sort_key(item[1], self._route_sort_column),
            reverse=self._route_sort_descending,
        )

    def _populate_nat_rules(self) -> None:
        selected = self._selected_nat_id()
        self.nat_tree.delete(*self.nat_tree.get_children())
        self._refresh_nat_headings()
        for index, rule in self._sorted_nat_items():
            iid = str(index)
            self.nat_tree.insert(
                "",
                "end",
                iid=iid,
                text=rule.name,
                values=(
                    rule.source_cidr,
                    rule.outbound_interface,
                    _format_bool(rule.enabled),
                    _format_bool(rule.persistent),
                    _format_bool(rule.managed),
                ),
            )
        if selected is not None and selected in self.nat_tree.get_children():
            self.nat_tree.selection_set(selected)

    def _set_nat_heading(self, column_id: str, label: str, sort_column: str) -> None:
        self.nat_tree.heading(
            column_id,
            text=label,
            command=lambda column=sort_column: self._sort_nat_by(column),
        )

    def _refresh_nat_headings(self) -> None:
        labels = {
            "name": ("#0", "Name"),
            "source_cidr": ("source_cidr", "Source CIDR"),
            "outbound_interface": ("outbound_interface", "Outbound Interface"),
            "enabled": ("enabled", "Enabled"),
            "persistent": ("persistent", "Persistent"),
            "managed": ("managed", "Managed"),
        }
        for sort_column, (column_id, label) in labels.items():
            indicator = ""
            if sort_column == self._nat_sort_column:
                indicator = " v" if self._nat_sort_descending else " ^"
            self._set_nat_heading(column_id, label + indicator, sort_column)

    def _sort_nat_by(self, column: str) -> None:
        if self._nat_sort_column == column:
            self._nat_sort_descending = not self._nat_sort_descending
        else:
            self._nat_sort_column = column
            self._nat_sort_descending = False
        self._populate_nat_rules()

    def _sorted_nat_items(self) -> list[tuple[int, NatRule]]:
        items = list(enumerate(self.nat_rules))
        return sorted(
            items,
            key=lambda item: nat_sort_key(item[1], self._nat_sort_column),
            reverse=self._nat_sort_descending,
        )

    def _on_adapter_select(self, _event: tk.Event | None = None) -> None:
        adapter = self._selected_adapter()
        if adapter is None:
            return
        ipv4 = _first_ipv4(adapter)
        self.adapter_name_var.set(adapter.name)
        self.adapter_mac_var.set(adapter.mac)
        self.adapter_ip_var.set(ipv4.address if ipv4 else "")
        self.adapter_prefix_var.set("" if not ipv4 or ipv4.prefix_length is None else str(ipv4.prefix_length))
        self.adapter_gateway_var.set(adapter.gateways[0] if adapter.gateways else "")
        self.adapter_dns_var.set(", ".join(adapter.dns_servers))
        self.adapter_dhcp_var.set(bool(adapter.dhcp_enabled))
        self.adapter_forwarding_var.set(True if adapter.forwarding_enabled is None else adapter.forwarding_enabled)
        if adapter.is_loopback and not self.loopback_name_var.get().strip():
            self.loopback_name_var.set(adapter.name)
        if adapter.is_virtual:
            self.virtual_name_var.set(adapter.name)
            if ipv4:
                self.virtual_address_var.set(_format_address(ipv4))

    def _on_route_select(self, _event: tk.Event | None = None) -> None:
        route = self._selected_route()
        if route is None:
            return
        self.route_destination_var.set(route.destination)
        self.route_gateway_var.set(route.gateway)
        self.route_interface_var.set(route.interface)
        self.route_metric_var.set("" if route.metric is None else str(route.metric))

    def _on_nat_select(self, _event: tk.Event | None = None) -> None:
        rule = self._selected_nat_rule()
        if rule is None:
            return
        self.nat_name_var.set(rule.name)
        self.nat_source_var.set(rule.source_cidr)
        self.nat_outbound_var.set(rule.outbound_interface)
        self.nat_enabled_var.set(rule.enabled)

    def apply_selected_adapter(self) -> None:
        adapter = self._selected_adapter()
        if adapter is None:
            messagebox.showinfo("No Adapter Selected", "Select an adapter first.")
            return
        try:
            address = self._adapter_address_from_form()
            gateway = validate_ip(self.adapter_gateway_var.get(), allow_empty=True)
            dns_servers = [validate_ip(item) for item in parse_csv(self.adapter_dns_var.get())]
            plan = self.backend.plan_adapter_update(
                adapter,
                address,
                gateway,
                dns_servers,
                self.adapter_mac_var.get().strip(),
                self.adapter_dhcp_var.get(),
            )
        except (ValueError, BackendError) as exc:
            messagebox.showerror("Invalid Adapter Settings", str(exc))
            return
        self._confirm_and_run(plan)

    def apply_selected_adapter_forwarding(self) -> None:
        adapter = self._selected_adapter()
        if adapter is None:
            messagebox.showinfo("No Adapter Selected", "Select an adapter first.")
            return
        try:
            plan = self.backend.plan_adapter_forwarding_update(adapter, self.adapter_forwarding_var.get())
        except BackendError as exc:
            messagebox.showerror("Forwarding Error", str(exc))
            return
        self._confirm_and_run(plan)

    def apply_global_forwarding(self) -> None:
        try:
            plan = self.backend.plan_global_forwarding_update(self.global_forwarding_var.get())
        except BackendError as exc:
            messagebox.showerror("Forwarding Error", str(exc))
            return
        self._confirm_and_run(plan)

    def create_loopback(self) -> None:
        name = self.loopback_name_var.get().strip()
        if not name:
            messagebox.showinfo("Loopback Name Required", "Enter a loopback adapter name or alias.")
            return
        try:
            plan = self.backend.plan_loopback_create(name)
        except BackendError as exc:
            messagebox.showerror("Loopback Error", str(exc))
            return
        self._confirm_and_run(plan)

    def delete_selected_loopback(self) -> None:
        adapter = self._selected_adapter()
        if adapter is None:
            messagebox.showinfo("No Adapter Selected", "Select a loopback adapter first.")
            return
        if not adapter.is_loopback:
            messagebox.showinfo("Not a Loopback Adapter", "The selected adapter is not marked as loopback.")
            return
        try:
            plan = self.backend.plan_loopback_delete(adapter)
        except BackendError as exc:
            messagebox.showerror("Loopback Error", str(exc))
            return
        self._confirm_and_run(plan)

    def create_virtual_adapter(self) -> None:
        name = self.virtual_name_var.get().strip()
        if not name:
            messagebox.showinfo("Virtual NIC Name Required", "Enter a virtual NIC name.")
            return
        try:
            address = _address_from_text(self.virtual_address_var.get().strip() or "192.168.56.1/24")
            plan = self.backend.plan_virtual_adapter_create(name, address)
        except (ValueError, BackendError) as exc:
            messagebox.showerror("Virtual NIC Error", str(exc))
            return
        self._confirm_and_run(plan)

    def delete_selected_virtual_adapter(self) -> None:
        adapter = self._selected_virtual_adapter()
        if adapter is None:
            messagebox.showinfo("No Virtual NIC Selected", "Select a virtual NIC first.")
            return
        try:
            plan = self.backend.plan_virtual_adapter_delete(adapter)
        except BackendError as exc:
            messagebox.showerror("Virtual NIC Error", str(exc))
            return
        self._confirm_and_run(plan)

    def add_route(self) -> None:
        try:
            route = self._route_from_form()
            plan = self.backend.plan_route_add(route)
        except (ValueError, BackendError) as exc:
            messagebox.showerror("Invalid Route", str(exc))
            return
        self._confirm_and_run(plan)

    def update_selected_route(self) -> None:
        old_route = self._selected_route()
        if old_route is None:
            messagebox.showinfo("No Route Selected", "Select a route first.")
            return
        try:
            new_route = self._route_from_form()
            plan = self.backend.plan_route_update(old_route, new_route)
        except (ValueError, BackendError) as exc:
            messagebox.showerror("Invalid Route", str(exc))
            return
        self._confirm_and_run(plan)

    def delete_selected_route(self) -> None:
        route = self._selected_route()
        if route is None:
            messagebox.showinfo("No Route Selected", "Select a route first.")
            return
        try:
            plan = self.backend.plan_route_delete(route)
        except BackendError as exc:
            messagebox.showerror("Route Error", str(exc))
            return
        self._confirm_and_run(plan)

    def add_nat_rule(self) -> None:
        try:
            rule = self._nat_rule_from_form()
            plan = self.backend.plan_nat_create(rule)
        except (ValueError, BackendError) as exc:
            messagebox.showerror("Invalid NAT Rule", str(exc))
            return
        self._confirm_and_run(plan)

    def update_selected_nat_rule(self) -> None:
        old_rule = self._selected_nat_rule()
        if old_rule is None:
            messagebox.showinfo("No NAT Rule Selected", "Select a NAT rule first.")
            return
        try:
            new_rule = self._nat_rule_from_form()
            plan = self.backend.plan_nat_update(old_rule, new_rule)
        except (ValueError, BackendError) as exc:
            messagebox.showerror("Invalid NAT Rule", str(exc))
            return
        self._confirm_and_run(plan)

    def delete_selected_nat_rule(self) -> None:
        rule = self._selected_nat_rule()
        if rule is None:
            messagebox.showinfo("No NAT Rule Selected", "Select a NAT rule first.")
            return
        try:
            plan = self.backend.plan_nat_delete(rule)
        except BackendError as exc:
            messagebox.showerror("NAT Error", str(exc))
            return
        self._confirm_and_run(plan)

    def export_current_configuration(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Export Network Configuration",
            defaultextension=".json",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return
        self.status_var.set("Exporting configuration snapshot...")
        self._run_background(
            lambda: self._export_configuration_to_path(path),
            lambda exported_path: self._on_configuration_exported(str(exported_path)),
            busy_message="Exporting configuration snapshot...",
        )

    def import_configuration_file(self) -> None:
        path = filedialog.askopenfilename(
            title="Import Network Configuration",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return
        self.status_var.set("Importing configuration snapshot...")
        self._run_background(
            lambda: (path, import_snapshot(path)),
            self._on_configuration_imported,
            busy_message="Importing configuration snapshot...",
        )

    def _on_configuration_imported(self, payload: tuple[str, NetworkSnapshot]) -> None:
        path, snapshot = payload
        self.imported_snapshot = snapshot
        self._set_config_text(
            f"Imported: {path}\n"
            f"Captured at: {snapshot.captured_at}\n"
            f"Source platform: {snapshot.platform}\n"
            f"Global IPv4 forwarding: {_format_forwarding(snapshot.global_forwarding_enabled)}\n"
            f"Adapters: {len(snapshot.adapters)}\n"
            f"Routes: {len(snapshot.routes)}\n\n"
            f"NAT rules: {len(snapshot.nat_rules)}\n"
            "\n"
            "Use Apply Imported Configuration to preview and apply this snapshot."
        )
        self.status_var.set("Imported configuration snapshot.")

    def apply_imported_configuration(self) -> None:
        if self.imported_snapshot is None:
            messagebox.showinfo("No Snapshot Imported", "Import a configuration file first.")
            return
        if self.imported_snapshot.platform and self.imported_snapshot.platform != self.backend.name:
            proceed = messagebox.askyesno(
                "Platform Mismatch",
                "This snapshot was captured on "
                f"{self.imported_snapshot.platform}, but this system is using "
                f"the {self.backend.name} backend. Continue with best-effort apply?",
            )
            if not proceed:
                return
        self.status_var.set("Preparing imported configuration plan...")
        self._run_background(
            lambda: self.backend.plan_snapshot_apply(self.imported_snapshot),
            self._confirm_and_run,
            busy_message="Preparing imported configuration plan...",
        )

    def _export_configuration_to_path(self, path: str) -> str:
        snapshot = NetworkSnapshot(
            platform=self.backend.name,
            adapters=self.adapters or self.backend.list_adapters(),
            routes=self.routes or self.backend.list_routes(),
            nat_rules=self.nat_rules or self.backend.list_nat_rules(),
            virtual_adapters=self.virtual_adapters or self.backend.list_virtual_adapters(),
            global_forwarding_enabled=(
                self.global_forwarding_enabled
                if self.global_forwarding_enabled is not None
                else self.backend.get_global_forwarding_enabled()
            ),
        )
        export_snapshot(snapshot, path)
        return path

    def _on_configuration_exported(self, path: str) -> None:
        self.status_var.set(f"Exported configuration to {path}")
        self._log(f"Exported configuration to {path}")

    def _adapter_address_from_form(self) -> AddressInfo | None:
        ip_value = self.adapter_ip_var.get().strip()
        if not ip_value:
            return None
        return AddressInfo(
            address=validate_ip(ip_value),
            prefix_length=validate_prefix(self.adapter_prefix_var.get().strip() or "24"),
            family="ipv4",
        )

    def _route_from_form(self) -> RouteInfo:
        metric_text = self.route_metric_var.get().strip()
        return RouteInfo(
            destination=validate_network(self.route_destination_var.get()),
            gateway=validate_ip(self.route_gateway_var.get(), allow_empty=True),
            interface=self.route_interface_var.get().strip(),
            metric=int(metric_text) if metric_text else None,
            family="ipv4",
        )

    def _nat_rule_from_form(self) -> NatRule:
        name = self.nat_name_var.get().strip()
        if not name:
            raise ValueError("A NAT rule name is required.")
        return NatRule(
            name=name,
            source_cidr=validate_network(self.nat_source_var.get()),
            outbound_interface=self.nat_outbound_var.get().strip(),
            enabled=self.nat_enabled_var.get(),
            persistent=True,
            managed=True,
            family="ipv4",
        )

    def _confirm_and_run(self, plan: OperationPlan) -> None:
        if not self.is_admin:
            messagebox.showwarning(
                "Administrator Access Required",
                "This action changes system network settings. Restart Py NIC Manager "
                "as Administrator/root and try again.",
            )
            return
        if not plan.commands:
            notes = "\n".join(plan.notes) if plan.notes else "No system commands were generated."
            messagebox.showinfo("Nothing to Apply", notes)
            return
        dialog = PlanDialog(self, plan)
        self.wait_window(dialog)
        if not dialog.confirmed:
            return
        self.status_var.set("Running network command plan...")
        self._active_plan = plan
        self._run_background(
            lambda: self.backend.run_plan(plan),
            self._on_plan_finished,
            busy_message="Running network command plan...",
        )

    def _on_plan_finished(self, results: object) -> None:
        failures = []
        should_refresh = True
        for result in results:
            self._log(result.summary())
            if not result.ok:
                failures.append(result)
        if failures:
            messagebox.showerror(
                "Command Failed",
                "\n\n".join(result.error_message() for result in failures[:3]),
            )
            self.status_var.set(f"{len(failures)} command(s) failed.")
        else:
            self.status_var.set("Network command plan completed.")
            messagebox.showinfo("Done", "The network command plan completed.")
            if self._active_plan and self._active_plan.restart_required:
                should_refresh = not self._ask_restart_now()
        self._active_plan = None
        if should_refresh:
            self.refresh_all()

    def _ask_restart_now(self) -> bool:
        dialog = RestartPromptDialog(self)
        self.wait_window(dialog)
        if dialog.restart_now:
            self.status_var.set("Restarting system...")
            self._run_background(
                self.backend.restart_system,
                self._on_restart_command_finished,
                busy_message="Restarting system...",
            )
            return True
        return False

    def _on_restart_command_finished(self, result: object) -> None:
        if isinstance(result, CommandResult):
            self._log(result.summary())
            if not result.ok:
                messagebox.showerror("Restart Failed", result.summary())
                self.status_var.set("Restart command failed.")

    def _run_background(self, func, callback, *, busy_message: str = "Working...") -> None:
        self._begin_busy(busy_message)

        def worker() -> None:
            try:
                result = func()
                self._queue.put(("ok", (callback, result)))
            except Exception as exc:
                self._queue.put(("error", exc))

        threading.Thread(target=worker, daemon=True).start()

    def _poll_queue(self) -> None:
        try:
            while True:
                kind, payload = self._queue.get_nowait()
                self._end_busy()
                if kind == "ok":
                    callback, result = payload
                    callback(result)
                else:
                    self.status_var.set("Operation failed.")
                    self._log(str(payload))
                    messagebox.showerror("Operation Failed", str(payload))
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _begin_busy(self, message: str) -> None:
        if self._busy_depth == 0:
            self._busy_started_at = time.monotonic()
        self._busy_depth += 1
        self._busy_message_var.set(message)
        self._update_busy_elapsed()
        self.status_var.set(message)
        self.busy_overlay.grid()
        self.busy_overlay.lift()
        self.busy_overlay.focus_set()
        if self._busy_depth == 1:
            self.busy_progress.start(12)
        self.update_idletasks()

    def _end_busy(self) -> None:
        if self._busy_depth > 0:
            self._busy_depth -= 1
        if self._busy_depth == 0:
            self._cancel_busy_elapsed_timer()
            self._busy_started_at = None
            self._busy_elapsed_var.set("Elapsed: 0s")
            self.busy_progress.stop()
            self.busy_overlay.grid_remove()
            self.configure(cursor="")
        else:
            self.busy_overlay.lift()

    def _update_busy_elapsed(self) -> None:
        if self._busy_depth <= 0 or self._busy_started_at is None:
            return
        elapsed_seconds = int(time.monotonic() - self._busy_started_at)
        self._busy_elapsed_var.set(f"Elapsed: {format_elapsed_time(elapsed_seconds)}")
        self._cancel_busy_elapsed_timer()
        self._busy_elapsed_after_id = self.after(1000, self._update_busy_elapsed)

    def _cancel_busy_elapsed_timer(self) -> None:
        if self._busy_elapsed_after_id is None:
            return
        try:
            self.after_cancel(self._busy_elapsed_after_id)
        except tk.TclError:
            pass
        self._busy_elapsed_after_id = None

    def _set_config_text(self, text: str) -> None:
        self.config_text.configure(state="normal")
        self.config_text.delete("1.0", "end")
        self.config_text.insert("1.0", text)
        self.config_text.configure(state="disabled")

    def _refresh_global_forwarding_controls(self) -> None:
        self.global_forwarding_status_var.set(
            f"Global IPv4 Forwarding: {_format_forwarding(self.global_forwarding_enabled)}"
        )
        self.global_forwarding_var.set(bool(self.global_forwarding_enabled))

    def _log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message.rstrip() + "\n\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _selected_adapter_id(self) -> str | None:
        selection = self.adapter_tree.selection() if hasattr(self, "adapter_tree") else ()
        return selection[0] if selection else None

    def _selected_route_id(self) -> str | None:
        selection = self.route_tree.selection() if hasattr(self, "route_tree") else ()
        return selection[0] if selection else None

    def _selected_nat_id(self) -> str | None:
        selection = self.nat_tree.selection() if hasattr(self, "nat_tree") else ()
        return selection[0] if selection else None

    def _selected_adapter(self) -> AdapterInfo | None:
        selected = self._selected_adapter_id()
        if selected is None:
            return None
        try:
            return self.adapters[int(selected)]
        except (IndexError, ValueError):
            return None

    def _selected_route(self) -> RouteInfo | None:
        selected = self._selected_route_id()
        if selected is None:
            return None
        try:
            return self.routes[int(selected)]
        except (IndexError, ValueError):
            return None

    def _selected_nat_rule(self) -> NatRule | None:
        selected = self._selected_nat_id()
        if selected is None:
            return None
        try:
            return self.nat_rules[int(selected)]
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
            return VirtualAdapterInfo(
                name=selected_adapter.name,
                kind=selected_adapter.virtual_kind or "virtual",
                status=selected_adapter.status,
                address=_format_address(ipv4),
                source_cidr=_source_cidr_from_text(_format_address(ipv4)),
                backend_id=selected_adapter.id,
            )
        return None

    def _refresh_loopback_suggestion(self) -> None:
        current = self.loopback_name_var.get().strip()
        if current and current != self._last_suggested_loopback_value:
            return
        suggestion = _suggest_loopback_value(self.backend.name, self.adapters)
        self._last_suggested_loopback_value = suggestion
        self.loopback_name_var.set(suggestion)

    def _refresh_virtual_suggestion(self) -> None:
        current = self.virtual_name_var.get().strip()
        if current and current != self._last_suggested_virtual_value:
            return
        suggestion = _suggest_virtual_adapter_value(self.backend.name, self.adapters)
        self._last_suggested_virtual_value = suggestion
        self.virtual_name_var.set(suggestion)


class PlanDialog(tk.Toplevel):
    def __init__(self, parent: tk.Tk, plan: OperationPlan) -> None:
        super().__init__(parent)
        self.title("Confirm Network Changes")
        self.transient(parent)
        self.grab_set()
        self.confirmed = False
        self.geometry("760x460")
        self.minsize(640, 360)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        ttk.Label(self, text=plan.title, style="Header.TLabel", padding=(12, 10, 12, 4)).grid(row=0, column=0, sticky="ew")
        text_font = getattr(parent, "ui_text_font", None)
        text = scrolledtext.ScrolledText(self, font=text_font, wrap="word")
        text.grid(row=1, column=0, sticky="nsew", padx=12, pady=8)
        text.insert("1.0", plan.as_text())
        text.configure(state="disabled")

        buttons = ttk.Frame(self, padding=(12, 4, 12, 12))
        buttons.grid(row=2, column=0, sticky="ew")
        buttons.columnconfigure(0, weight=1)
        ttk.Button(buttons, text="Cancel", command=self.destroy).grid(row=0, column=1, padx=(0, 8))
        ttk.Button(buttons, text="Run Commands", command=self._confirm).grid(row=0, column=2)

        self.bind("<Escape>", lambda _event: self.destroy())
        self.wait_visibility()
        self.focus()

    def _confirm(self) -> None:
        self.confirmed = True
        self.destroy()


class RestartPromptDialog(tk.Toplevel):
    def __init__(self, parent: tk.Tk) -> None:
        super().__init__(parent)
        self.title("Restart Required")
        self.transient(parent)
        self.grab_set()
        self.restart_now = False
        self.resizable(False, False)

        frame = ttk.Frame(self, padding=16)
        frame.grid(row=0, column=0, sticky="nsew")
        ttk.Label(frame, text="Restart Required", style="Header.TLabel").grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(
            frame,
            text="This setting may require a restart to take effect. Restart now?",
            wraplength=420,
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(10, 16))
        ttk.Button(frame, text="Later", command=self.destroy).grid(row=2, column=0, sticky="e", padx=(0, 8))
        ttk.Button(frame, text="Restart Now", command=self._restart).grid(row=2, column=1, sticky="e")

        self.bind("<Escape>", lambda _event: self.destroy())
        self.wait_visibility()
        self.focus()

    def _restart(self) -> None:
        self.restart_now = True
        self.destroy()


def _first_ipv4(adapter: AdapterInfo) -> AddressInfo | None:
    return next((item for item in adapter.addresses if item.family.lower() == "ipv4"), None)


def _format_address(address: AddressInfo | None) -> str:
    if address is None:
        return ""
    if address.prefix_length is None:
        return address.address
    return f"{address.address}/{address.prefix_length}"


def _address_from_text(value: str) -> AddressInfo | None:
    text = value.strip()
    if not text:
        return None
    if "/" in text:
        interface = ipaddress.ip_interface(text)
        return AddressInfo(str(interface.ip), int(interface.network.prefixlen), f"ipv{interface.ip.version}")
    return AddressInfo(validate_ip(text), 24, "ipv4")


def _source_cidr_from_text(value: str) -> str:
    text = value.strip()
    if not text or "/" not in text:
        return ""
    try:
        return str(ipaddress.ip_interface(text).network)
    except ValueError:
        return ""


def _adapter_kind(adapter: AdapterInfo) -> str:
    if adapter.is_loopback:
        return "Loopback"
    if adapter.is_virtual:
        return f"Virtual ({adapter.virtual_kind})" if adapter.virtual_kind else "Virtual"
    return "Physical"


def _future_result_or(future, fallback, label: str = "Optional state", errors: list[str] | None = None):
    try:
        return future.result()
    except Exception as exc:
        if errors is not None:
            errors.append(f"{label} unavailable: {exc}")
        return fallback


def _format_forwarding(value: bool | None) -> str:
    if value is None:
        return "Unknown"
    return "Enabled" if value else "Disabled"


def format_elapsed_time(seconds: int | float) -> str:
    total_seconds = max(0, int(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {seconds:02d}s"
    if minutes:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"


def _default_loopback_value(backend_name: str) -> str:
    if backend_name in {"macOS", "POSIX"}:
        return "127.0.0.2/32"
    return "py-loopback0"


def _default_virtual_adapter_value(backend_name: str) -> str:
    if backend_name in {"macOS", "POSIX"}:
        return "bridge0"
    return "py-virtual0"


def _suggest_loopback_value(backend_name: str, adapters: list[AdapterInfo]) -> str:
    if backend_name in {"macOS", "POSIX"}:
        used_addresses = {
            address.address
            for adapter in adapters
            for address in adapter.addresses
            if address.family.lower() == "ipv4"
        }
        for host in range(2, 255):
            candidate = f"127.0.0.{host}"
            if candidate not in used_addresses:
                return f"{candidate}/32"
        return "127.0.1.1/32"

    used_names = {adapter.name.strip().lower() for adapter in adapters}
    index = 0
    while True:
        candidate = f"py-loopback{index}"
        if candidate.lower() not in used_names:
            return candidate
        index += 1


def _suggest_virtual_adapter_value(backend_name: str, adapters: list[AdapterInfo]) -> str:
    used_names = {adapter.name.strip().lower() for adapter in adapters}
    if backend_name in {"macOS", "POSIX"}:
        for index in range(0, 256):
            candidate = f"bridge{index}"
            if candidate.lower() not in used_names:
                return candidate
        return "bridge256"
    index = 0
    while True:
        candidate = f"py-virtual{index}"
        if candidate.lower() not in used_names:
            return candidate
        index += 1


def route_sort_key(route: RouteInfo, column: str) -> tuple:
    if column == "destination":
        return _network_sort_key(route.destination)
    if column == "gateway":
        return _ip_or_text_sort_key(route.gateway)
    if column == "route_metric":
        return _optional_int_sort_key(route.metric)
    if column == "interface_metric":
        return _optional_int_sort_key(route.interface_metric)
    if column == "effective_metric":
        return _optional_int_sort_key(route.effective_metric)
    values = {
        "interface": route.interface,
        "protocol": route.protocol,
        "table": route.table,
    }
    return _text_sort_key(values.get(column, ""))


def nat_sort_key(rule: NatRule, column: str) -> tuple:
    if column == "source_cidr":
        return _network_sort_key(rule.source_cidr)
    if column in {"enabled", "persistent", "managed"}:
        return (0, 0 if getattr(rule, column) else 1)
    values = {
        "name": rule.name,
        "outbound_interface": rule.outbound_interface,
    }
    return _text_sort_key(values.get(column, ""))


def _format_bool(value: bool) -> str:
    return "Yes" if value else "No"


def _network_sort_key(value: str) -> tuple:
    text = value.strip()
    if not text:
        return (1, 0, 0, "")
    if text.lower() == "default":
        return (0, 0, 0, "default")
    try:
        network = ipaddress.ip_network(text, strict=False)
    except ValueError:
        return _text_sort_key(text)
    if network.version == 4:
        return (0, int(network.network_address), int(network.prefixlen), "")
    return (0, int(network.network_address), int(network.prefixlen), f"ipv{network.version}")


def _ip_or_text_sort_key(value: str) -> tuple:
    text = value.strip()
    if not text:
        return (1, 0, "")
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return _text_sort_key(text)
    if ip.version == 4:
        return (0, int(ip), "")
    return (0, int(ip), str(ip.version))


def _optional_int_sort_key(value: int | None) -> tuple[int, int]:
    if value is None:
        return (1, 0)
    return (0, int(value))


def _text_sort_key(value: str) -> tuple[int, str]:
    text = value.strip().lower()
    return (0 if text else 1, text)


def main() -> None:
    app = NetworkManagerApp()
    app.mainloop()
