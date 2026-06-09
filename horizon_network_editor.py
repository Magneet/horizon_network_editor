"""
Horizon Network Editor
Edit portgroup/network-label assignments for instant-clone desktop pools and RDS farms.
Supports Horizon 2312 and later (uses v8 pool / v7 farm endpoints).
"""

import sys
import os
import json

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QTabWidget, QWidget,
    QPushButton, QLabel, QComboBox, QLineEdit, QCheckBox,
    QPlainTextEdit, QInputDialog, QTableWidget, QTableWidgetItem,
    QAbstractItemView, QHeaderView,
)
from PySide6.QtCore import QThread, Signal, Qt
from PySide6.QtGui import QIcon, QStandardItemModel, QStandardItem
import configparser
import keyring
import requests
import ast
from loguru import logger

import horizon_functions
import horizon_app

requests.packages.urllib3.disable_warnings()

# ── Constants ─────────────────────────────────────────────────────────────────
APPLICATION_NAME = "hneditor"
CONFIG_FILE = "hneditor_config.ini"
FROM_GOLDEN_IMAGE = "From Golden Image"
MAX_NICS = 4

# ── Logging ───────────────────────────────────────────────────────────────────
_log_handler_id = logger.add(
    'hneditor.log', retention="10 days", rotation="50 MB",
    format="{time:YYYY-MM-DD at HH:mm:ss} {level} {message}",
    level="INFO", enqueue=True, backtrace=True, diagnose=True, catch=True,
)

# ── Configuration loading ─────────────────────────────────────────────────────
config = configparser.ConfigParser()
config.read(CONFIG_FILE)

config_password = None

if 'UserInfo' in config:
    config_username = config.get('UserInfo', 'Username')
    config_domain = config.get('UserInfo', 'Domain')
    config_server_name = config.get('UserInfo', 'ServerName')
    config_save_password = config.getboolean('UserInfo', 'Save_Password')
    try:
        config_password = keyring.get_password(APPLICATION_NAME, config_username)
    except Exception:
        pass
else:
    config_username = None
    config_domain = None
    config_server_name = None
    config_save_password = False

if 'Pods' in config:
    config_pods = ast.literal_eval(config.get('Pods', 'Pods'))
else:
    config_pods = []

if 'Connection_Servers' in config:
    config_connection_servers = ast.literal_eval(
        config.get('Connection_Servers', 'Connection_Servers'))
else:
    config_connection_servers = []

# ── Global state ──────────────────────────────────────────────────────────────
global_desktop_pools = []
global_rds_farms = []
VDI_pool_values = {}
RDS_farm_values = {}

vdi_selected_pool = {}
rds_selected_farm = {}

# Current NIC info for selected pool/farm: list of dicts with 'id' and 'name'
vdi_nics = []
rds_nics = []

_connect_worker = None
_vdi_net_worker = None
_rds_net_worker = None
_vdi_apply_worker = None
_rds_apply_worker = None
_config_test_worker = None

# Holds strong references to running QThread workers so Python's GC cannot
# destroy the Python wrapper while the C++ thread is still active (→ SIGSEGV).
_worker_pool: list = []

# ── Helpers ───────────────────────────────────────────────────────────────────

def _connect_pod(pod):
    conn, _ = horizon_app.connect_to_pod(
        pod, config_connection_servers, config_username, config_domain, config_password)
    return conn if conn is not False else None


def _track_worker(worker):
    """Keep a strong reference to worker until its QThread finishes.

    Without this, reassigning the global _xxx_worker variable drops the Python
    reference count to zero and the GC destroys the wrapper while the C++ thread
    is still running, causing a SIGSEGV on the next signal emission.
    """
    _worker_pool.append(worker)

    def _remove():
        try:
            _worker_pool.remove(worker)
        except ValueError:
            pass

    worker.finished.connect(_remove)


class CheckableComboBox(QComboBox):
    """A combobox where each item carries a checkbox.

    "From Golden Image" (row 0) is exclusive with portgroup rows:
    checking a portgroup unchecks it; if all portgroups are unchecked it
    is re-checked automatically.  The popup stays open after each click so
    the user can select multiple items in one interaction.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setEditable(True)
        self.lineEdit().setReadOnly(True)
        self._mdl = QStandardItemModel(self)
        self.setModel(self._mdl)
        self.view().pressed.connect(self._on_item_pressed)
        self._suppress_hide = False

    # ── public API ────────────────────────────────────────────────────────

    def addItem(self, text, data=None):
        item = QStandardItem(text)
        item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
        item.setCheckState(Qt.Unchecked)
        self._mdl.appendRow(item)

    def addItems(self, texts):
        for text in texts:
            self.addItem(text)

    def clear(self):
        self._mdl.clear()
        self.lineEdit().clear()

    def checked_items(self) -> list:
        """Return text of all checked items."""
        return [
            self._mdl.item(i).text()
            for i in range(self._mdl.rowCount())
            if self._mdl.item(i) and self._mdl.item(i).checkState() == Qt.Checked
        ]

    def set_checked_items(self, texts: list):
        """Check exactly the items whose text appears in texts.

        If no portgroup in texts matches an available item, From Golden Image
        is checked.
        """
        portgroups = [t for t in texts if t != FROM_GOLDEN_IMAGE]
        for i in range(self._mdl.rowCount()):
            item = self._mdl.item(i)
            if not item:
                continue
            if i == 0:
                item.setCheckState(Qt.Unchecked if portgroups else Qt.Checked)
            else:
                item.setCheckState(
                    Qt.Checked if item.text() in portgroups else Qt.Unchecked)
        # If none of the requested portgroups existed in the list, fall back
        if portgroups and not any(
            self._mdl.item(i).checkState() == Qt.Checked
            for i in range(1, self._mdl.rowCount())
        ):
            if self._mdl.rowCount() > 0:
                self._mdl.item(0).setCheckState(Qt.Checked)
        self._update_display()

    # ── internals ─────────────────────────────────────────────────────────

    def hidePopup(self):
        if self._suppress_hide:
            self._suppress_hide = False
            return
        super().hidePopup()

    def _on_item_pressed(self, index):
        self._suppress_hide = True
        item = self._mdl.itemFromIndex(index)
        if index.row() == 0:
            # Clicking "From Golden Image" always resets to golden-image-only
            item.setCheckState(Qt.Checked)
            for i in range(1, self._mdl.rowCount()):
                self._mdl.item(i).setCheckState(Qt.Unchecked)
        else:
            # Toggle this portgroup
            new = Qt.Unchecked if item.checkState() == Qt.Checked else Qt.Checked
            item.setCheckState(new)
            any_pg = any(
                self._mdl.item(i).checkState() == Qt.Checked
                for i in range(1, self._mdl.rowCount())
            )
            # Keep From Golden Image exclusive
            self._mdl.item(0).setCheckState(Qt.Unchecked if any_pg else Qt.Checked)
        self._update_display()

    def _update_display(self):
        checked = self.checked_items()
        if not checked or FROM_GOLDEN_IMAGE in checked:
            self.lineEdit().setText(FROM_GOLDEN_IMAGE)
        else:
            self.lineEdit().setText(', '.join(checked))


def _current_portgroups(nics_list, nic_id) -> list:
    """Return currently assigned portgroup names for nic_id, or [FROM_GOLDEN_IMAGE]."""
    for nic in nics_list:
        if nic.get('network_interface_card_id') == nic_id:
            specs = nic.get('network_label_assignment_specs') or []
            names = [s.get('network_label_name') for s in specs if s.get('network_label_name')]
            return names if names else [FROM_GOLDEN_IMAGE]
    return [FROM_GOLDEN_IMAGE]


def _build_new_nics(nic_ids, nic_combos):
    """Build the nics update list from per-NIC CheckableComboBox selections.

    NICs left at FROM_GOLDEN_IMAGE are omitted so Horizon inherits the VM portgroup.
    NICs with multiple portgroups get one spec entry per portgroup.
    """
    new_nics = []
    for nic_id, combo in zip(nic_ids, nic_combos):
        portgroups = [s for s in combo.checked_items() if s and s != FROM_GOLDEN_IMAGE]
        if portgroups:
            new_nics.append({
                "network_interface_card_id": nic_id,
                "network_label_assignment_specs": [
                    {"enabled": True, "max_label_type": "UNLIMITED",
                     "network_label_name": pg}
                    for pg in portgroups
                ],
            })
    return new_nics


def _make_pool_update_body(pool, new_nics):
    """Build DesktopPoolUpdateSpecV3 body from a GET response, replacing nics."""
    _STRIP = {
        'id', 'created_at', 'updated_at', 'delete_in_progress',
        'provisioning_status_data', 'user_group_count', 'image_source',
        'naming_method', 'source', 'type', 'user_assignment',
        'vcenter_id', 'farm_id', 'global_desktop_entitlement_id',
    }
    body = {k: v for k, v in pool.items() if k not in _STRIP}
    # provisioning_settings update spec only takes host_or_cluster_id and resource_pool_id
    if 'provisioning_settings' in body:
        ps = body['provisioning_settings']
        body['provisioning_settings'] = {
            k: ps[k] for k in ('host_or_cluster_id', 'resource_pool_id') if k in ps
        }
    body['nics'] = new_nics
    return body


def _make_farm_update_body(farm, new_nics):
    """Build FarmUpdateSpecV5 body from a GET response, replacing nics."""
    _STRIP_FARM = {'id', 'created_at', 'updated_at', 'delete_in_progress', 'type',
                   'desktop_pool_id', 'app_volumes_manager_guid'}
    _STRIP_AFS = {'image_source', 'operating_system', 'operating_system_architecture',
                  'vcenter_id', 'provisioning_status_data'}
    body = {k: v for k, v in farm.items() if k not in _STRIP_FARM}
    if 'automated_farm_settings' in body:
        afs = {k: v for k, v in body['automated_farm_settings'].items()
               if k not in _STRIP_AFS}
        if 'provisioning_settings' in afs:
            ps = afs['provisioning_settings']
            afs['provisioning_settings'] = {
                k: ps[k] for k in ('host_or_cluster_id', 'resource_pool_id') if k in ps
            }
        afs['nics'] = new_nics
        body['automated_farm_settings'] = afs
    return body


def _networks_summary(nics):
    if not nics:
        return FROM_GOLDEN_IMAGE
    parts = []
    for nic in nics:
        specs = nic.get('network_label_assignment_specs') or []
        name = nic.get('network_interface_card_name') or nic.get('network_interface_card_id', '?')
        pgs = ', '.join(s.get('network_label_name', '?') for s in specs) if specs else FROM_GOLDEN_IMAGE
        parts.append(f"{name}: {pgs}")
    return '  |  '.join(parts)


def _pool_table_values(pool):
    return (
        pool.get('name', 'N/A'),
        pool.get('display_name', 'N/A'),
        'Yes' if pool.get('enabled') else 'No',
        _networks_summary(pool.get('nics') or []),
    )


def _farm_table_values(farm):
    afs = farm.get('automated_farm_settings', {})
    return (
        farm.get('name', 'N/A'),
        farm.get('display_name', 'N/A'),
        'Yes' if farm.get('enabled') else 'No',
        _networks_summary(afs.get('nics') or []),
    )


def _fill_table(table, rows):
    table.blockSignals(True)
    table.setRowCount(len(rows))
    for r, row_vals in enumerate(rows):
        for c, text in enumerate(row_vals):
            item = QTableWidgetItem(str(text))
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            table.setItem(r, c, item)
    table.resizeRowsToContents()
    table.blockSignals(False)


def _update_table_row(table, name, row_vals):
    for r in range(table.rowCount()):
        cell = table.item(r, 0)
        if cell and cell.text() == name:
            for c, text in enumerate(row_vals):
                item = table.item(r, c)
                if item:
                    item.setText(str(text))
            break


def bind_combobox_search(cb):
    cb._all_values = []
    cb.setEditable(True)
    cb.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)

    def on_text_edited(text):
        typed = text.lower()
        filtered = (
            [v for v in cb._all_values if typed in v.lower()]
            if typed else list(cb._all_values)
        )
        cb.blockSignals(True)
        current = cb.lineEdit().text()
        cb.clear()
        cb.addItems(filtered)
        cb.blockSignals(False)
        cb.lineEdit().setText(current)

    cb.lineEdit().textEdited.connect(on_text_edited)


def resource_path(rel):
    try:
        base = sys._MEIPASS
    except Exception:
        base = os.path.abspath('.')
    return os.path.join(base, rel)


# ── Worker threads ────────────────────────────────────────────────────────────

class ConnectWorker(QThread):
    status_updated = Signal(str)
    data_loaded = Signal(dict)

    def run(self):
        try:
            data = horizon_app.load_environment_data(
                config_pods, config_connection_servers,
                config_username, config_domain, config_password,
                on_status=lambda msg: self.status_updated.emit(msg),
                include_vms_snapshots=False,
            )
            self.data_loaded.emit(data)
        except horizon_functions.RateLimitError as e:
            self.status_updated.emit(f"Rate limited: {e}")
            self.data_loaded.emit({})
        except Exception as e:
            self.status_updated.emit(f"Error loading pools/farms: {e}")
            self.data_loaded.emit({})


class NetworkLoadWorker(QThread):
    """Fetch network labels and NIC IDs for a pool or farm."""
    status_updated = Signal(str)
    data_loaded = Signal(dict)

    def __init__(self, item, is_farm=False):
        super().__init__()
        self._item = item
        self._is_farm = is_farm

    def run(self):
        self.status_updated.emit("Loading network configuration...")
        pod = self._item.get('pod')
        item_id = self._item.get('id')
        try:
            conn = _connect_pod(pod)
            if conn is None:
                self.status_updated.emit("Failed to connect to pod")
                self.data_loaded.emit({'labels': [], 'nics': [], 'current_nics': [], 'nic_warning': ''})
                return

            # Always do a fresh individual GET so we have the complete object.
            # The list endpoint can return abbreviated data (missing parent_vm_id etc.).
            inventory = horizon_functions.Inventory(url=conn.url, access_token=conn.access_token)
            external = horizon_functions.External(url=conn.url, access_token=conn.access_token)

            self.status_updated.emit("Fetching pool/farm details...")
            if self._is_farm:
                full = inventory.get_farm(item_id)
                afs = full.get('automated_farm_settings', {})
                vcenter_id = afs.get('vcenter_id')
                ps = afs.get('provisioning_settings', {})
                current_nics = afs.get('nics') or []
            else:
                full = inventory.get_desktop_pool(item_id)
                vcenter_id = full.get('vcenter_id')
                ps = full.get('provisioning_settings', {})
                current_nics = full.get('nics') or []

            host_or_cluster_id = ps.get('host_or_cluster_id')
            base_vm_id = ps.get('parent_vm_id')
            base_snapshot_id = ps.get('base_snapshot_id')

            logger.debug(
                f"Pool/farm details — vcenter_id={vcenter_id} "
                f"host_or_cluster_id={host_or_cluster_id} "
                f"parent_vm_id={base_vm_id} base_snapshot_id={base_snapshot_id}"
            )

            labels = []
            if host_or_cluster_id and vcenter_id:
                self.status_updated.emit("Loading network labels...")
                try:
                    all_labels = external.get_network_labels(
                        vcenter_id=vcenter_id,
                        host_or_cluster_id=host_or_cluster_id,
                    )
                    labels = [l for l in all_labels if not l.get('incompatible_reasons')]
                    logger.info(f"Loaded {len(labels)} network labels")
                except Exception as e:
                    logger.error(f"Failed to load network labels: {e}")
                    self.status_updated.emit(f"Warning: could not load network labels — {e}")
            else:
                logger.warning(
                    f"Cannot load network labels: host_or_cluster_id={host_or_cluster_id} "
                    f"vcenter_id={vcenter_id}"
                )

            nics = []
            nic_warning = ""
            if base_vm_id and base_snapshot_id and vcenter_id:
                self.status_updated.emit("Loading network interface cards...")
                try:
                    nics = external.get_network_interface_cards(
                        vcenter_id=vcenter_id,
                        base_vm_id=base_vm_id,
                        base_snapshot_id=base_snapshot_id,
                    )
                    logger.info(f"Loaded {len(nics)} NICs")
                except Exception as e:
                    logger.error(f"Failed to load NICs with configured snapshot {base_snapshot_id}: {e}")
                    self.status_updated.emit("Configured snapshot unavailable — trying other snapshots...")
                    # Fallback 1: try any other available snapshot for this VM.
                    if base_vm_id and vcenter_id:
                        try:
                            snapshots = external.get_base_snapshots(
                                vcenter_id=vcenter_id, base_vm_id=base_vm_id)
                            logger.info(
                                f"Fallback: found {len(snapshots)} snapshot(s) for vm {base_vm_id}")
                            for snap in snapshots:
                                snap_id = snap.get('id')
                                if not snap_id or snap_id == base_snapshot_id:
                                    continue
                                try:
                                    nics = external.get_network_interface_cards(
                                        vcenter_id=vcenter_id,
                                        base_vm_id=base_vm_id,
                                        base_snapshot_id=snap_id,
                                    )
                                    if nics:
                                        logger.info(
                                            f"Loaded {len(nics)} NICs via fallback snapshot {snap_id}")
                                        break
                                except Exception:
                                    continue
                        except Exception as fe:
                            logger.warning(f"No snapshots available via fallback ({fe}); trying VM-only v2 lookup")
                    # Fallback 2: v2 endpoint can return NICs from just the base VM — no snapshot needed.
                    if not nics and base_vm_id and vcenter_id:
                        try:
                            self.status_updated.emit("Trying VM-level NIC lookup (no snapshot required)...")
                            nics = external.get_network_interface_cards_v2(
                                vcenter_id=vcenter_id, base_vm_id=base_vm_id)
                            if nics:
                                logger.info(f"Loaded {len(nics)} NICs via v2 VM-only lookup")
                        except Exception as ve:
                            logger.error(f"v2 VM-only NIC lookup failed: {ve}")
                    if not nics:
                        nic_warning = (
                            f"WARNING: Could not retrieve NIC information for base VM "
                            f"{base_vm_id} (configured snapshot: {base_snapshot_id}).\n"
                            f"All lookup methods returned 404 — the base VM or all its snapshots "
                            f"may have been deleted from vCenter.\n"
                            f"Please check the golden image configuration for this pool/farm "
                            f"and push a new image before editing network settings."
                        )
                        logger.warning(nic_warning)
            elif not base_vm_id:
                nic_warning = (
                    "WARNING: No golden image is configured for this pool/farm "
                    "(parent_vm_id is missing).\n"
                    "Please assign a golden image before editing network settings."
                )
                logger.warning(nic_warning)
            else:
                logger.warning(
                    f"Cannot load NICs: parent_vm_id={base_vm_id} "
                    f"base_snapshot_id={base_snapshot_id} vcenter_id={vcenter_id}"
                )

            conn.hv_disconnect()
            self.data_loaded.emit({
                'labels': labels,
                'nics': nics,
                'current_nics': current_nics,
                'nic_warning': nic_warning,
            })
        except horizon_functions.RateLimitError as e:
            logger.warning(f"NetworkLoadWorker rate limited: {e}")
            self.status_updated.emit(f"Rate limited — {e}")
            self.data_loaded.emit({'labels': [], 'nics': [], 'current_nics': [], 'nic_warning': str(e)})
        except Exception as e:
            logger.error(f"NetworkLoadWorker error: {e}")
            self.status_updated.emit(f"Error loading network config: {e}")
            self.data_loaded.emit({'labels': [], 'nics': [], 'current_nics': [], 'nic_warning': ''})


class ApplyWorker(QThread):
    """Apply updated NIC/portgroup settings to a pool or farm."""
    status_updated = Signal(str)
    finished = Signal(bool, str)

    def __init__(self, item, new_nics, is_farm=False,
                 enable_provisioning=False, delete_machines=False):
        super().__init__()
        self._item = item
        self._new_nics = new_nics
        self._is_farm = is_farm
        self._enable_provisioning = enable_provisioning
        self._delete_machines = delete_machines

    def run(self):
        self.status_updated.emit("Applying network changes...")
        try:
            pod = self._item.get('pod')
            item_id = self._item.get('id')
            item_name = self._item.get('name', item_id)

            conn = _connect_pod(pod)
            if conn is None:
                self.finished.emit(False, "Failed to connect to pod")
                return

            inventory = horizon_functions.Inventory(url=conn.url, access_token=conn.access_token)

            if self._is_farm:
                fresh = inventory.get_farm(item_id)
                body = _make_farm_update_body(fresh, self._new_nics)
                if self._enable_provisioning:
                    body.setdefault('automated_farm_settings', {})['enable_provisioning'] = True
                inventory.update_farm(body, item_id)
                msg = f"Farm '{item_name}' network configuration updated successfully"
                if self._delete_machines:
                    self.status_updated.emit("Deleting RDS servers for redeploy...")
                    try:
                        filter_spec = {"type": "Equals", "name": "farm_id", "value": item_id}
                        servers = inventory.get_rds_servers(filter=filter_spec)
                        deleted = 0
                        for srv in servers:
                            try:
                                inventory.delete_rds_server(srv['id'])
                                deleted += 1
                            except Exception as de:
                                logger.warning(f"Could not delete RDS server {srv['id']}: {de}")
                        logger.info(f"Deleted {deleted}/{len(servers)} RDS servers from farm '{item_name}'")
                        msg += f" — {deleted} server(s) queued for redeploy"
                    except Exception as me:
                        logger.error(f"RDS server deletion failed: {me}")
                        msg += f" (server deletion failed: {me})"
            else:
                fresh = inventory.get_desktop_pool(item_id)
                body = _make_pool_update_body(fresh, self._new_nics)
                if self._enable_provisioning:
                    body['enable_provisioning'] = True
                inventory.update_desktop_pool(body, item_id)
                msg = f"Pool '{item_name}' network configuration updated successfully"
                if self._delete_machines:
                    self.status_updated.emit("Deleting pool machines for redeploy...")
                    try:
                        filter_spec = {"type": "Equals", "name": "desktop_pool_id", "value": item_id}
                        machines = inventory.get_machines(filter=filter_spec)
                        machine_ids = [m['id'] for m in machines]
                        if machine_ids:
                            inventory.delete_machines(machine_ids, force_logoff=True)
                            logger.info(f"Deleted {len(machine_ids)} machines from pool '{item_name}'")
                            msg += f" — {len(machine_ids)} machine(s) queued for redeploy"
                        else:
                            msg += " — no machines found to delete"
                    except Exception as me:
                        logger.error(f"Machine deletion failed: {me}")
                        msg += f" (machine deletion failed: {me})"

            conn.hv_disconnect()
            logger.info(msg)
            self.finished.emit(True, msg)
        except horizon_functions.RateLimitError as e:
            logger.warning(f"ApplyWorker rate limited: {e}")
            self.finished.emit(False, f"Rate limited — {e}")
        except Exception as e:
            logger.error(f"ApplyWorker error: {e}")
            self.finished.emit(False, f"Error: {e}")


class ConfigTestWorker(QThread):
    status_updated = Signal(str)
    config_ready = Signal(list, list)  # (pods, connection_servers)

    def __init__(self, username, domain, server_name, password):
        super().__init__()
        self._username = username
        self._domain = domain
        self._server_name = server_name
        self._password = password

    def run(self):
        self.status_updated.emit("Testing connection...")
        url = "https://" + self._server_name
        conn = horizon_functions.Connection(
            username=self._username, domain=self._domain,
            password=self._password, url=url)
        try:
            conn.hv_connect()
            pods, servers = horizon_app.build_pod_info(conn, self._server_name)
            conn.hv_disconnect()
            self.config_ready.emit(pods, servers)
            self.status_updated.emit("Connected successfully — configuration saved")
        except Exception as e:
            logger.error(f"Config test error: {e}")
            self.status_updated.emit(f"Connection failed: {e}")


# ── UI callbacks ──────────────────────────────────────────────────────────────

def _show_password_dialog():
    global config_password
    pw, ok = QInputDialog.getText(
        window, "Password", "Enter your password:", QLineEdit.Password)
    if ok and pw:
        config_password = pw
        config_status_label.setText("Password set")


def _generic_connect():
    global _connect_worker
    if not config_server_name or config_password is None:
        VDI_status_label.setText("Configure credentials on the Config tab first")
        RDS_status_label.setText("Configure credentials on the Config tab first")
        return
    for btn in (VDI_connect_btn, RDS_connect_btn):
        btn.setEnabled(False)
    for lbl in (VDI_status_label, RDS_status_label):
        lbl.setText("Connecting...")
    _connect_worker = ConnectWorker()
    _connect_worker.status_updated.connect(lambda msg: (
        VDI_status_label.setText(msg), RDS_status_label.setText(msg)))
    _connect_worker.data_loaded.connect(_on_connect_finished)
    _connect_worker.start()


def _on_connect_finished(data):
    global global_desktop_pools, global_rds_farms, VDI_pool_values, RDS_farm_values

    global_desktop_pools = data['desktop_pools']
    global_rds_farms = data['rds_farms']

    seen = []
    for pool in global_desktop_pools:
        n = pool['name']
        if n in seen:
            pool['name'] = f"{n} ({pool['pod']})"
        else:
            seen.append(n)

    seen = []
    for farm in global_rds_farms:
        n = farm['name']
        if n in seen:
            farm['name'] = f"{n} ({farm['pod']})"
        else:
            seen.append(n)

    VDI_pool_values = {p['name']: p for p in global_desktop_pools}
    RDS_farm_values = {f['name']: f for f in global_rds_farms}

    if global_desktop_pools:
        names = list(VDI_pool_values.keys())
        VDI_pool_combo._all_values = names
        VDI_pool_combo.blockSignals(True)
        VDI_pool_combo.clear()
        VDI_pool_combo.addItems(names)
        VDI_pool_combo.blockSignals(False)
        VDI_pool_combo.setEnabled(True)
        _fill_table(VDI_pool_table, [_pool_table_values(p) for p in global_desktop_pools])
        VDI_pool_combo.setCurrentIndex(0)
        VDI_status_label.setText("Connected")
        _vdi_pool_selected(None)
    else:
        VDI_status_label.setText("Connected — no instant-clone VDI pools found")

    if global_rds_farms:
        names = list(RDS_farm_values.keys())
        RDS_farm_combo._all_values = names
        RDS_farm_combo.blockSignals(True)
        RDS_farm_combo.clear()
        RDS_farm_combo.addItems(names)
        RDS_farm_combo.blockSignals(False)
        RDS_farm_combo.setEnabled(True)
        _fill_table(RDS_farm_table, [_farm_table_values(f) for f in global_rds_farms])
        RDS_farm_combo.setCurrentIndex(0)
        RDS_status_label.setText("Connected")
        _rds_farm_selected(None)
    else:
        RDS_status_label.setText("Connected — no instant-clone RDS farms found")

    VDI_connect_btn.setText("Refresh")
    RDS_connect_btn.setText("Refresh")
    VDI_connect_btn.setEnabled(True)
    RDS_connect_btn.setEnabled(True)


def _on_vdi_table_selection_changed():
    items = VDI_pool_table.selectedItems()
    if not items:
        return
    name = VDI_pool_table.item(items[0].row(), 0).text()
    if name in VDI_pool_values and name != VDI_pool_combo.currentText():
        VDI_pool_combo.blockSignals(True)
        VDI_pool_combo.setCurrentText(name)
        VDI_pool_combo.blockSignals(False)
        _vdi_pool_selected(None)


def _on_rds_table_selection_changed():
    items = RDS_farm_table.selectedItems()
    if not items:
        return
    name = RDS_farm_table.item(items[0].row(), 0).text()
    if name in RDS_farm_values and name != RDS_farm_combo.currentText():
        RDS_farm_combo.blockSignals(True)
        RDS_farm_combo.setCurrentText(name)
        RDS_farm_combo.blockSignals(False)
        _rds_farm_selected(None)


# ── VDI tab ───────────────────────────────────────────────────────────────────

def _vdi_disable_controls():
    for lbl, cb in zip(VDI_nic_labels, VDI_nic_combos):
        lbl.setVisible(False)
        cb.setVisible(False)
        cb.setEnabled(False)
    VDI_apply_btn.setEnabled(False)


def _vdi_pool_selected(_):
    global vdi_selected_pool, _vdi_net_worker
    name = VDI_pool_combo.currentText()
    if name not in VDI_pool_values:
        return
    vdi_selected_pool = VDI_pool_values[name]
    logger.info(
        f"VDI pool selected — name={name!r} id={vdi_selected_pool.get('id')!r} "
        f"pod={vdi_selected_pool.get('pod')!r}"
    )
    # Disconnect the old worker's UI callback so a stale result from a previous
    # selection doesn't overwrite the UI after we've moved on.
    if _vdi_net_worker is not None:
        try:
            _vdi_net_worker.data_loaded.disconnect(_on_vdi_network_loaded)
        except RuntimeError:
            pass
    _vdi_disable_controls()
    for r in range(VDI_pool_table.rowCount()):
        item = VDI_pool_table.item(r, 0)
        if item and item.text() == name:
            VDI_pool_table.blockSignals(True)
            VDI_pool_table.selectRow(r)
            VDI_pool_table.blockSignals(False)
            break

    _vdi_net_worker = NetworkLoadWorker(vdi_selected_pool, is_farm=False)
    _track_worker(_vdi_net_worker)
    _vdi_net_worker.status_updated.connect(VDI_status_label.setText)
    _vdi_net_worker.data_loaded.connect(_on_vdi_network_loaded)
    _vdi_net_worker.start()


def _on_vdi_network_loaded(data):
    global vdi_nics
    vdi_nics = data.get('nics', [])
    labels = data.get('labels', [])
    current_nics = data.get('current_nics', [])
    nic_warning = data.get('nic_warning', '')

    label_names = [FROM_GOLDEN_IMAGE] + sorted(l['name'] for l in labels)
    visible = min(len(vdi_nics), MAX_NICS)

    for i in range(MAX_NICS):
        lbl = VDI_nic_labels[i]
        cb = VDI_nic_combos[i]
        if i < visible:
            nic = vdi_nics[i]
            lbl.setText(f"NIC {i + 1}: {nic.get('name', nic['id'])}")
            lbl.setVisible(True)
            cb.clear()
            cb.addItems(label_names)
            pgs = _current_portgroups(current_nics, nic['id'])
            cb.set_checked_items(pgs)
            cb.setEnabled(True)
            cb.setVisible(True)
        else:
            lbl.setVisible(False)
            cb.setVisible(False)
            cb.setEnabled(False)

    VDI_apply_btn.setEnabled(visible > 0)
    if nic_warning:
        VDI_status_label.setText(f"Cannot load NIC configuration: {nic_warning}")
    else:
        VDI_status_label.setText(
            f"Ready — {len(label_names) - 1} portgroup(s) available" if labels
            else "Ready — no network labels found for this host/cluster"
        )


def _vdi_apply():
    global _vdi_apply_worker
    if not vdi_nics:
        return
    ids = [nic['id'] for nic in vdi_nics[:MAX_NICS]]
    combos = VDI_nic_combos[:len(ids)]
    new_nics = _build_new_nics(ids, combos)
    VDI_apply_btn.setEnabled(False)
    _vdi_apply_worker = ApplyWorker(
        vdi_selected_pool, new_nics, is_farm=False,
        enable_provisioning=VDI_enable_prov_cb.isChecked(),
        delete_machines=VDI_delete_machines_cb.isChecked(),
    )
    _track_worker(_vdi_apply_worker)
    _vdi_apply_worker.status_updated.connect(VDI_status_label.setText)
    _vdi_apply_worker.finished.connect(_on_vdi_apply_finished)
    _vdi_apply_worker.start()


def _on_vdi_apply_finished(success, msg):
    VDI_status_label.setText(msg)
    VDI_apply_btn.setEnabled(bool(vdi_nics))
    if success:
        for i, nic in enumerate(vdi_nics[:MAX_NICS]):
            portgroups = [s for s in VDI_nic_combos[i].checked_items()
                          if s != FROM_GOLDEN_IMAGE]
            new_specs = [{'network_label_name': pg} for pg in portgroups]
            matched = False
            for existing in (vdi_selected_pool.get('nics') or []):
                if existing.get('network_interface_card_id') == nic['id']:
                    existing['network_label_assignment_specs'] = new_specs
                    matched = True
            if not matched and portgroups:
                vdi_selected_pool.setdefault('nics', []).append({
                    'network_interface_card_id': nic['id'],
                    'network_interface_card_name': nic.get('name', ''),
                    'network_label_assignment_specs': new_specs,
                })
        _update_table_row(VDI_pool_table, vdi_selected_pool.get('name', ''),
                          _pool_table_values(vdi_selected_pool))


# ── RDS tab ───────────────────────────────────────────────────────────────────

def _rds_disable_controls():
    for lbl, cb in zip(RDS_nic_labels, RDS_nic_combos):
        lbl.setVisible(False)
        cb.setVisible(False)
        cb.setEnabled(False)
    RDS_apply_btn.setEnabled(False)


def _rds_farm_selected(_):
    global rds_selected_farm, _rds_net_worker
    name = RDS_farm_combo.currentText()
    if name not in RDS_farm_values:
        return
    rds_selected_farm = RDS_farm_values[name]
    logger.info(
        f"RDS farm selected — name={name!r} id={rds_selected_farm.get('id')!r} "
        f"pod={rds_selected_farm.get('pod')!r}"
    )
    if _rds_net_worker is not None:
        try:
            _rds_net_worker.data_loaded.disconnect(_on_rds_network_loaded)
        except RuntimeError:
            pass
    _rds_disable_controls()
    for r in range(RDS_farm_table.rowCount()):
        item = RDS_farm_table.item(r, 0)
        if item and item.text() == name:
            RDS_farm_table.blockSignals(True)
            RDS_farm_table.selectRow(r)
            RDS_farm_table.blockSignals(False)
            break

    _rds_net_worker = NetworkLoadWorker(rds_selected_farm, is_farm=True)
    _track_worker(_rds_net_worker)
    _rds_net_worker.status_updated.connect(RDS_status_label.setText)
    _rds_net_worker.data_loaded.connect(_on_rds_network_loaded)
    _rds_net_worker.start()


def _on_rds_network_loaded(data):
    global rds_nics
    rds_nics = data.get('nics', [])
    labels = data.get('labels', [])
    current_nics = data.get('current_nics', [])
    nic_warning = data.get('nic_warning', '')

    label_names = [FROM_GOLDEN_IMAGE] + sorted(l['name'] for l in labels)
    visible = min(len(rds_nics), MAX_NICS)

    for i in range(MAX_NICS):
        lbl = RDS_nic_labels[i]
        cb = RDS_nic_combos[i]
        if i < visible:
            nic = rds_nics[i]
            lbl.setText(f"NIC {i + 1}: {nic.get('name', nic['id'])}")
            lbl.setVisible(True)
            cb.clear()
            cb.addItems(label_names)
            pgs = _current_portgroups(current_nics, nic['id'])
            cb.set_checked_items(pgs)
            cb.setEnabled(True)
            cb.setVisible(True)
        else:
            lbl.setVisible(False)
            cb.setVisible(False)
            cb.setEnabled(False)

    RDS_apply_btn.setEnabled(visible > 0)
    if nic_warning:
        RDS_status_label.setText(f"Cannot load NIC configuration: {nic_warning}")
    else:
        RDS_status_label.setText(
            f"Ready — {len(label_names) - 1} portgroup(s) available" if labels
            else "Ready — no network labels found for this host/cluster"
        )


def _rds_apply():
    global _rds_apply_worker
    if not rds_nics:
        return
    ids = [nic['id'] for nic in rds_nics[:MAX_NICS]]
    combos = RDS_nic_combos[:len(ids)]
    new_nics = _build_new_nics(ids, combos)
    RDS_apply_btn.setEnabled(False)
    _rds_apply_worker = ApplyWorker(
        rds_selected_farm, new_nics, is_farm=True,
        enable_provisioning=RDS_enable_prov_cb.isChecked(),
        delete_machines=RDS_delete_machines_cb.isChecked(),
    )
    _track_worker(_rds_apply_worker)
    _rds_apply_worker.status_updated.connect(RDS_status_label.setText)
    _rds_apply_worker.finished.connect(_on_rds_apply_finished)
    _rds_apply_worker.start()


def _on_rds_apply_finished(success, msg):
    RDS_status_label.setText(msg)
    RDS_apply_btn.setEnabled(bool(rds_nics))
    if success:
        afs = rds_selected_farm.setdefault('automated_farm_settings', {})
        existing_nics = afs.setdefault('nics', [])
        for i, nic in enumerate(rds_nics[:MAX_NICS]):
            portgroups = [s for s in RDS_nic_combos[i].checked_items()
                          if s != FROM_GOLDEN_IMAGE]
            new_specs = [{'network_label_name': pg} for pg in portgroups]
            matched = False
            for n in existing_nics:
                if n.get('network_interface_card_id') == nic['id']:
                    n['network_label_assignment_specs'] = new_specs
                    matched = True
            if not matched and portgroups:
                existing_nics.append({
                    'network_interface_card_id': nic['id'],
                    'network_interface_card_name': nic.get('name', ''),
                    'network_label_assignment_specs': new_specs,
                })
        _update_table_row(RDS_farm_table, rds_selected_farm.get('name', ''),
                          _farm_table_values(rds_selected_farm))


# ── Config tab ────────────────────────────────────────────────────────────────

def _clear_connection_state():
    """Reset all cached pool/farm data and UI controls.

    Called whenever credentials change so stale data from the previous server
    is never shown alongside a new connection.
    """
    global global_desktop_pools, global_rds_farms, VDI_pool_values, RDS_farm_values
    global vdi_selected_pool, rds_selected_farm, vdi_nics, rds_nics

    global_desktop_pools.clear()
    global_rds_farms.clear()
    VDI_pool_values.clear()
    RDS_farm_values.clear()
    vdi_selected_pool = {}
    rds_selected_farm = {}
    vdi_nics = []
    rds_nics = []

    for combo, status_lbl, table, apply_btn, nic_labels, nic_combos, connect_btn in (
        (VDI_pool_combo, VDI_status_label, VDI_pool_table,
         VDI_apply_btn, VDI_nic_labels, VDI_nic_combos, VDI_connect_btn),
        (RDS_farm_combo, RDS_status_label, RDS_farm_table,
         RDS_apply_btn, RDS_nic_labels, RDS_nic_combos, RDS_connect_btn),
    ):
        combo.blockSignals(True)
        combo.clear()
        combo.blockSignals(False)
        combo.setEnabled(False)
        status_lbl.setText("")
        table.clearContents()
        table.setRowCount(0)
        apply_btn.setEnabled(False)
        connect_btn.setText("Connect")
        for lbl, cb in zip(nic_labels, nic_combos):
            lbl.setVisible(False)
            cb.setVisible(False)
            cb.setEnabled(False)

    logger.info("Connection state cleared due to configuration change")


def _config_save():
    global config_username, config_domain, config_server_name, config_save_password

    new_username = config_username_tb.text().strip()
    new_domain = config_domain_tb.text().strip()
    new_server = config_server_tb.currentText().strip()

    if not all([new_username, new_domain, new_server, config_password]):
        config_status_label.setText("Please fill in all fields and set a password first")
        return

    credentials_changed = (
        new_username != config_username
        or new_domain != config_domain
        or new_server != config_server_name
    )

    config_username = new_username
    config_domain = new_domain
    config_server_name = new_server

    cfg = configparser.ConfigParser()
    cfg['UserInfo'] = {
        'Username': config_username,
        'Domain': config_domain,
        'ServerName': config_server_name,
        'Save_Password': str(config_save_pw_cb.isChecked()),
    }
    cfg['Pods'] = {'Pods': repr(config_pods)}
    cfg['Connection_Servers'] = {'Connection_Servers': repr(config_connection_servers)}
    with open(CONFIG_FILE, 'w') as f:
        cfg.write(f)
    config_save_password = config_save_pw_cb.isChecked()
    if config_save_password:
        try:
            keyring.set_password(APPLICATION_NAME, config_username, config_password)
        except Exception as e:
            logger.error(f"Failed to save password to keyring: {e}")

    if credentials_changed:
        _clear_connection_state()
        config_status_label.setText("Configuration saved — please reconnect")
        logger.info("Configuration saved with changed credentials")
    else:
        config_status_label.setText("Configuration saved")
        logger.info("Configuration saved")


def _config_test():
    global _config_test_worker
    username = config_username_tb.text().strip()
    domain = config_domain_tb.text().strip()
    server = config_server_tb.currentText().strip()
    if not all([username, domain, server]):
        config_status_label.setText("Please fill in Username, Domain and Server")
        return
    if config_password is None:
        config_status_label.setText("Please set a password first")
        return
    config_test_btn.setEnabled(False)
    _config_test_worker = ConfigTestWorker(username, domain, server, config_password)
    _config_test_worker.status_updated.connect(config_status_label.setText)
    _config_test_worker.config_ready.connect(_on_config_ready)
    _config_test_worker.finished.connect(lambda: config_test_btn.setEnabled(True))
    _config_test_worker.start()


def _on_config_ready(pods, servers):
    global config_pods, config_connection_servers
    config_pods.clear()
    config_pods.extend(pods)
    config_connection_servers.clear()
    config_connection_servers.extend(servers)
    _config_save()
    # Populate server combobox with all known connection servers
    server_dns_list = [s.get('ServerDNS', '') for s in servers if s.get('ServerDNS')]
    config_server_tb.blockSignals(True)
    current = config_server_tb.currentText()
    config_server_tb.clear()
    config_server_tb.addItems(server_dns_list)
    config_server_tb.blockSignals(False)
    if current in server_dns_list:
        config_server_tb.setCurrentText(current)
    elif server_dns_list:
        config_server_tb.setCurrentIndex(0)


# ── UI construction ───────────────────────────────────────────────────────────

app = QApplication(sys.argv)
app.setStyle("Fusion")

window = QMainWindow()
window.setWindowTitle("Horizon Network Editor")
window.setFixedSize(780, 530)

logo_path = resource_path("logo.ico")
if os.path.isfile(logo_path):
    window.setWindowIcon(QIcon(logo_path))

tabs = QTabWidget()
window.setCentralWidget(tabs)

# ─ VDI tab ────────────────────────────────────────────────────────────────────
tab_vdi = QWidget()
tabs.addTab(tab_vdi, "VDI Pools")

VDI_connect_btn = QPushButton("Connect", tab_vdi)
VDI_connect_btn.setGeometry(10, 10, 120, 25)
VDI_connect_btn.clicked.connect(_generic_connect)

VDI_status_label = QLabel("", tab_vdi)
VDI_status_label.setGeometry(140, 13, 620, 20)

VDI_pool_table = QTableWidget(tab_vdi)
VDI_pool_table.setGeometry(10, 40, 757, 160)
VDI_pool_table.setColumnCount(4)
VDI_pool_table.setHorizontalHeaderLabels(["Name", "Display Name", "Enabled", "Networks"])
VDI_pool_table.horizontalHeader().setStretchLastSection(True)
VDI_pool_table.setColumnWidth(0, 150)
VDI_pool_table.setColumnWidth(1, 150)
VDI_pool_table.setColumnWidth(2, 55)
VDI_pool_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
VDI_pool_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
VDI_pool_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
VDI_pool_table.verticalHeader().setVisible(False)
VDI_pool_table.itemSelectionChanged.connect(_on_vdi_table_selection_changed)

QLabel("Desktop Pool:", tab_vdi).setGeometry(10, 210, 100, 20)
VDI_pool_combo = QComboBox(tab_vdi)
VDI_pool_combo.setGeometry(10, 230, 560, 25)
VDI_pool_combo.setEnabled(False)
bind_combobox_search(VDI_pool_combo)
VDI_pool_combo.currentIndexChanged.connect(lambda _: _vdi_pool_selected(None))

QLabel("Network Configuration:", tab_vdi).setGeometry(10, 265, 200, 20)

VDI_nic_labels = []
VDI_nic_combos = []
for _i in range(MAX_NICS):
    _y = 288 + _i * 33
    _lbl = QLabel(f"NIC {_i + 1}:", tab_vdi)
    _lbl.setGeometry(10, _y, 160, 25)
    _lbl.setVisible(False)
    _cb = CheckableComboBox(tab_vdi)
    _cb.setGeometry(175, _y, 582, 25)
    _cb.setEnabled(False)
    _cb.setVisible(False)
    VDI_nic_labels.append(_lbl)
    VDI_nic_combos.append(_cb)

VDI_enable_prov_cb = QCheckBox("Enable provisioning after saving", tab_vdi)
VDI_enable_prov_cb.setGeometry(10, 406, 280, 22)

VDI_delete_machines_cb = QCheckBox("Delete all machines after saving (forces redeploy with new network)", tab_vdi)
VDI_delete_machines_cb.setGeometry(10, 430, 500, 22)

VDI_apply_btn = QPushButton("Apply Network Changes", tab_vdi)
VDI_apply_btn.setGeometry(10, 456, 200, 30)
VDI_apply_btn.setEnabled(False)
VDI_apply_btn.clicked.connect(_vdi_apply)

# ─ RDS tab ────────────────────────────────────────────────────────────────────
tab_rds = QWidget()
tabs.addTab(tab_rds, "RDS Farms")

RDS_connect_btn = QPushButton("Connect", tab_rds)
RDS_connect_btn.setGeometry(10, 10, 120, 25)
RDS_connect_btn.clicked.connect(_generic_connect)

RDS_status_label = QLabel("", tab_rds)
RDS_status_label.setGeometry(140, 13, 620, 20)

RDS_farm_table = QTableWidget(tab_rds)
RDS_farm_table.setGeometry(10, 40, 757, 160)
RDS_farm_table.setColumnCount(4)
RDS_farm_table.setHorizontalHeaderLabels(["Name", "Display Name", "Enabled", "Networks"])
RDS_farm_table.horizontalHeader().setStretchLastSection(True)
RDS_farm_table.setColumnWidth(0, 150)
RDS_farm_table.setColumnWidth(1, 150)
RDS_farm_table.setColumnWidth(2, 55)
RDS_farm_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
RDS_farm_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
RDS_farm_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
RDS_farm_table.verticalHeader().setVisible(False)
RDS_farm_table.itemSelectionChanged.connect(_on_rds_table_selection_changed)

QLabel("RDS Farm:", tab_rds).setGeometry(10, 210, 100, 20)
RDS_farm_combo = QComboBox(tab_rds)
RDS_farm_combo.setGeometry(10, 230, 560, 25)
RDS_farm_combo.setEnabled(False)
bind_combobox_search(RDS_farm_combo)
RDS_farm_combo.currentIndexChanged.connect(lambda _: _rds_farm_selected(None))

QLabel("Network Configuration:", tab_rds).setGeometry(10, 265, 200, 20)

RDS_nic_labels = []
RDS_nic_combos = []
for _i in range(MAX_NICS):
    _y = 288 + _i * 33
    _lbl = QLabel(f"NIC {_i + 1}:", tab_rds)
    _lbl.setGeometry(10, _y, 160, 25)
    _lbl.setVisible(False)
    _cb = CheckableComboBox(tab_rds)
    _cb.setGeometry(175, _y, 582, 25)
    _cb.setEnabled(False)
    _cb.setVisible(False)
    RDS_nic_labels.append(_lbl)
    RDS_nic_combos.append(_cb)

RDS_enable_prov_cb = QCheckBox("Enable provisioning after saving", tab_rds)
RDS_enable_prov_cb.setGeometry(10, 406, 280, 22)

RDS_delete_machines_cb = QCheckBox("Delete all servers after saving (forces redeploy with new network)", tab_rds)
RDS_delete_machines_cb.setGeometry(10, 430, 500, 22)

RDS_apply_btn = QPushButton("Apply Network Changes", tab_rds)
RDS_apply_btn.setGeometry(10, 456, 200, 30)
RDS_apply_btn.setEnabled(False)
RDS_apply_btn.clicked.connect(_rds_apply)

# ─ Config tab ─────────────────────────────────────────────────────────────────
tab_config = QWidget()
tabs.addTab(tab_config, "Configuration")

QLabel("Username:", tab_config).setGeometry(10, 15, 80, 20)
config_username_tb = QLineEdit(tab_config)
config_username_tb.setGeometry(100, 12, 250, 25)
if config_username:
    config_username_tb.setText(config_username)

QLabel("Domain:", tab_config).setGeometry(10, 50, 80, 20)
config_domain_tb = QLineEdit(tab_config)
config_domain_tb.setGeometry(100, 47, 250, 25)
if config_domain:
    config_domain_tb.setText(config_domain)

QLabel("Server:", tab_config).setGeometry(10, 85, 80, 20)
config_server_tb = QComboBox(tab_config)
config_server_tb.setGeometry(100, 82, 350, 25)
config_server_tb.setEditable(True)
if config_server_name:
    config_server_tb.addItem(config_server_name)
    for _cs in config_connection_servers:
        _dns = _cs.get('ServerDNS', '')
        if _dns and _dns != config_server_name:
            config_server_tb.addItem(_dns)

QLabel("Password:", tab_config).setGeometry(10, 120, 80, 20)
config_pw_btn = QPushButton("Set Password", tab_config)
config_pw_btn.setGeometry(100, 117, 130, 25)
config_pw_btn.clicked.connect(_show_password_dialog)

config_save_pw_cb = QCheckBox("Save password in keyring", tab_config)
config_save_pw_cb.setGeometry(10, 158, 220, 20)
config_save_pw_cb.setChecked(config_save_password)

config_test_btn = QPushButton("Test && Save", tab_config)
config_test_btn.setGeometry(10, 188, 130, 28)
config_test_btn.clicked.connect(_config_test)

config_status_label = QLabel("", tab_config)
config_status_label.setGeometry(10, 228, 720, 20)
if config_server_name:
    config_status_label.setText(f"Loaded saved configuration for {config_server_name}")

# ─ Launch ──────────────────────────────────────────────────────────────────────
window.show()
sys.exit(app.exec())
