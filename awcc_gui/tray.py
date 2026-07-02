"""System-tray integration via the StatusNotifierItem (SNI) DBus protocol.

KDE Plasma (and other SNI hosts) render tray icons through
org.kde.StatusNotifierWatcher rather than the old XEmbed tray. We implement the
StatusNotifierItem and its com.canonical.dbusmenu menu directly over GDBus
(Gio), which:

  * needs no extra dependencies (Gio ships with PyGObject),
  * stays in the same process as the GTK4 GUI (libappindicator would force a
    conflicting GTK3 load), and
  * is the native path on Wayland/KDE.

The item exposes: left-click to show the window, and a context menu with live
CPU/GPU temps, quick thermal-profile and fan-mode switching, and Quit.
"""

from __future__ import annotations

import os

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib  # noqa: E402

SNI_IFACE = "org.kde.StatusNotifierItem"
MENU_IFACE = "com.canonical.dbusmenu"
ITEM_PATH = "/StatusNotifierItem"
MENU_PATH = "/MenuBar"
WATCHER_NAME = "org.kde.StatusNotifierWatcher"
WATCHER_PATH = "/StatusNotifierWatcher"

ICON_NAME = "io.github.awcclinux.Awcc"

PROFILE_LABELS = {
    "quiet": "Quiet", "cool": "Cool", "balanced": "Balanced",
    "balanced-performance": "Balanced+", "performance": "Performance",
    "custom": "Custom",
}
MODE_ITEMS = [("profile", "Follow Profile"), ("custom", "Custom Curves"),
              ("manual", "Manual")]

# Introspection XML for the two interfaces we serve.
SNI_XML = f"""
<node>
  <interface name="{SNI_IFACE}">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="IconThemePath" type="s" access="read"/>
    <property name="OverlayIconName" type="s" access="read"/>
    <property name="AttentionIconName" type="s" access="read"/>
    <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <method name="Activate"><arg name="x" type="i"/><arg name="y" type="i"/></method>
    <method name="SecondaryActivate"><arg name="x" type="i"/><arg name="y" type="i"/></method>
    <method name="ContextMenu"><arg name="x" type="i"/><arg name="y" type="i"/></method>
    <method name="Scroll"><arg name="delta" type="i"/><arg name="orientation" type="s"/></method>
    <signal name="NewIcon"/>
    <signal name="NewToolTip"/>
    <signal name="NewStatus"><arg name="status" type="s"/></signal>
    <signal name="NewTitle"/>
  </interface>
</node>
"""

MENU_XML = f"""
<node>
  <interface name="{MENU_IFACE}">
    <property name="Version" type="u" access="read"/>
    <property name="TextDirection" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconThemePath" type="as" access="read"/>
    <method name="GetLayout">
      <arg type="i" name="parentId" direction="in"/>
      <arg type="i" name="recursionDepth" direction="in"/>
      <arg type="as" name="propertyNames" direction="in"/>
      <arg type="u" name="revision" direction="out"/>
      <arg type="(ia{{sv}}av)" name="layout" direction="out"/>
    </method>
    <method name="GetGroupProperties">
      <arg type="ai" name="ids" direction="in"/>
      <arg type="as" name="propertyNames" direction="in"/>
      <arg type="a(ia{{sv}})" name="properties" direction="out"/>
    </method>
    <method name="GetProperty">
      <arg type="i" name="id" direction="in"/>
      <arg type="s" name="name" direction="in"/>
      <arg type="v" name="value" direction="out"/>
    </method>
    <method name="Event">
      <arg type="i" name="id" direction="in"/>
      <arg type="s" name="eventId" direction="in"/>
      <arg type="v" name="data" direction="in"/>
      <arg type="u" name="timestamp" direction="in"/>
    </method>
    <method name="AboutToShow">
      <arg type="i" name="id" direction="in"/>
      <arg type="b" name="needUpdate" direction="out"/>
    </method>
    <signal name="ItemsPropertiesUpdated">
      <arg type="a(ia{{sv}})" name="updatedProps"/>
      <arg type="a(ias)" name="removedProps"/>
    </signal>
    <signal name="LayoutUpdated">
      <arg type="u" name="revision"/>
      <arg type="i" name="parent"/>
    </signal>
    <signal name="ItemActivationRequested">
      <arg type="i" name="id"/>
      <arg type="u" name="timestamp"/>
    </signal>
  </interface>
</node>
"""


class Tray:
    def __init__(self, window):
        self.window = window          # MainWindow (has .backend, .state, helpers)
        self.conn: Gio.DBusConnection | None = None
        self.revision = 1
        self._item_reg = 0
        self._menu_reg = 0
        self._actions: dict[int, tuple] = {}
        self.available = False

        self.bus_name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        self._sni_node = Gio.DBusNodeInfo.new_for_xml(SNI_XML)
        self._menu_node = Gio.DBusNodeInfo.new_for_xml(MENU_XML)
        self._owner_id = Gio.bus_own_name(
            Gio.BusType.SESSION, self.bus_name, Gio.BusNameOwnerFlags.NONE,
            self._on_bus_acquired, None, self._on_name_lost)

    # -- lifecycle ---------------------------------------------------------

    def _on_bus_acquired(self, conn, name):
        self.conn = conn
        try:
            self._item_reg = conn.register_object(
                ITEM_PATH, self._sni_node.interfaces[0],
                self._sni_method, self._sni_get_prop, None)
            self._menu_reg = conn.register_object(
                MENU_PATH, self._menu_node.interfaces[0],
                self._menu_method, self._menu_get_prop, None)
        except GLib.Error as exc:
            print(f"[tray] register_object failed: {exc}", flush=True)
            return
        # Register with the watcher so a host actually shows us.
        conn.call(WATCHER_NAME, WATCHER_PATH, WATCHER_NAME,
                  "RegisterStatusNotifierItem",
                  GLib.Variant("(s)", (self.bus_name,)), None,
                  Gio.DBusCallFlags.NONE, -1, None, self._on_registered)

    def _on_registered(self, conn, res):
        try:
            conn.call_finish(res)
            self.available = True
        except GLib.Error as exc:
            print(f"[tray] no StatusNotifierWatcher ({exc}); tray unavailable",
                  flush=True)

    def _on_name_lost(self, conn, name):
        self.available = False

    def shutdown(self):
        if self.conn:
            if self._item_reg:
                self.conn.unregister_object(self._item_reg)
            if self._menu_reg:
                self.conn.unregister_object(self._menu_reg)
        if self._owner_id:
            Gio.bus_unown_name(self._owner_id)

    # -- state / refresh ---------------------------------------------------

    def update(self):
        """Called when new daemon state arrives; refresh tooltip."""
        if self.conn and self.available:
            self._emit(ITEM_PATH, SNI_IFACE, "NewToolTip", None)

    def notify_layout_changed(self):
        self.revision += 1
        if self.conn and self.available:
            self._emit(MENU_PATH, MENU_IFACE, "LayoutUpdated",
                       GLib.Variant("(ui)", (self.revision, 0)))

    def _emit(self, path, iface, signal, params):
        try:
            self.conn.emit_signal(None, path, iface, signal, params)
        except GLib.Error:
            pass

    # -- SNI interface -----------------------------------------------------

    def _tooltip_text(self):
        st = self.window.state or {}
        cpu = st.get("cpu_temp")
        gpu = st.get("gpu_temp")
        prof = st.get("profile") or "—"
        parts = []
        if cpu is not None:
            parts.append(f"CPU {cpu:.0f}°C")
        if gpu is not None:
            parts.append(f"GPU {gpu:.0f}°C")
        parts.append(prof)
        return "  ·  ".join(parts)

    def _sni_get_prop(self, conn, sender, path, iface, name):
        if name == "Category":
            return GLib.Variant("s", "Hardware")
        if name == "Id":
            return GLib.Variant("s", "awcc-linux")
        if name == "Title":
            return GLib.Variant("s", "AWCC-Linux")
        if name == "Status":
            return GLib.Variant("s", "Active")
        if name == "IconName":
            return GLib.Variant("s", ICON_NAME)
        if name in ("IconThemePath", "OverlayIconName", "AttentionIconName"):
            return GLib.Variant("s", "")
        if name == "ItemIsMenu":
            return GLib.Variant("b", False)   # left-click -> Activate (show window)
        if name == "Menu":
            return GLib.Variant("o", MENU_PATH)
        if name == "ToolTip":
            return GLib.Variant("(sa(iiay)ss)",
                                (ICON_NAME, [], "AWCC-Linux", self._tooltip_text()))
        return None

    def _sni_method(self, conn, sender, path, iface, method, params, invocation):
        if method in ("Activate", "SecondaryActivate"):
            GLib.idle_add(self.window.show_from_tray)
        elif method == "Scroll":
            pass
        elif method == "ContextMenu":
            pass
        invocation.return_value(None)

    # -- DBus menu ---------------------------------------------------------

    def _build_menu(self):
        """(Re)build the menu model from current state. Returns list of nodes.

        Node = dict(id, props{}, children[]). Populates self._actions.
        """
        st = self.window.state or {}
        cur_profile = st.get("profile")
        cur_mode = (st.get("config") or {}).get("mode", "profile")
        choices = st.get("profile_choices", [])
        self._actions = {}

        def std(nid, label, **props):
            p = {"label": label, "enabled": True, "visible": True}
            p.update(props)
            return {"id": nid, "props": p, "children": []}

        cpu = st.get("cpu_temp")
        gpu = st.get("gpu_temp")
        header = "  ".join(
            ([f"CPU {cpu:.0f}°C"] if cpu is not None else [])
            + ([f"GPU {gpu:.0f}°C"] if gpu is not None else [])) or "AWCC-Linux"

        items = []
        items.append(std(1, header, enabled=False))
        items.append({"id": 2, "props": {"type": "separator"}, "children": []})
        items.append(std(3, "Show AWCC-Linux"))
        self._actions[3] = ("show",)

        # Thermal profile submenu.
        prof_children = []
        for i, prof in enumerate(choices):
            nid = 100 + i
            prof_children.append(std(
                nid, PROFILE_LABELS.get(prof, prof.title()),
                **{"toggle-type": "radio",
                   "toggle-state": 1 if prof == cur_profile else 0}))
            self._actions[nid] = ("profile", prof)
        prof_menu = std(4, "Thermal Profile")
        prof_menu["props"]["children-display"] = "submenu"
        prof_menu["children"] = prof_children
        items.append(prof_menu)

        # Fan-mode submenu.
        mode_children = []
        for i, (mkey, mlabel) in enumerate(MODE_ITEMS):
            nid = 200 + i
            mode_children.append(std(
                nid, mlabel,
                **{"toggle-type": "radio",
                   "toggle-state": 1 if mkey == cur_mode else 0}))
            self._actions[nid] = ("mode", mkey)
        mode_menu = std(5, "Fan Mode")
        mode_menu["props"]["children-display"] = "submenu"
        mode_menu["children"] = mode_children
        items.append(mode_menu)

        items.append({"id": 6, "props": {"type": "separator"}, "children": []})
        items.append(std(7, "Quit AWCC-Linux"))
        self._actions[7] = ("quit",)
        return items

    def _props_variant(self, props):
        out = {}
        for k, v in props.items():
            if isinstance(v, bool):
                out[k] = GLib.Variant("b", v)
            elif isinstance(v, int):
                out[k] = GLib.Variant("i", v)
            else:
                out[k] = GLib.Variant("s", str(v))
        return out

    def _node_pytuple(self, node, depth):
        """Python tuple (id, {str: Variant}, [Variant, ...]) for a menu node.

        The dbusmenu layout type is (ia{sv}av). A pre-built GLib.Variant may only
        occupy a `v` slot, so the node itself stays a python tuple (built by the
        outer constructor) while each `av` child is a boxed GLib.Variant.
        """
        props = self._props_variant(node["props"])
        children = []
        if depth != 0 and node["children"]:
            for c in node["children"]:
                children.append(self._node_variant(c, depth - 1))
        return (node["id"], props, children)

    def _node_variant(self, node, depth):
        return GLib.Variant("(ia{sv}av)", self._node_pytuple(node, depth))

    def _layout_pytuple(self, parent_id, depth):
        """Build the layout subtree rooted at `parent_id` (0 = whole menu).

        Honouring parent_id is essential: hosts fetch each submenu with a
        separate GetLayout(parentId=<submenu id>) call. Returning the root for
        every request makes every submenu render as a copy of the root menu.
        """
        items = self._build_menu()
        root = {"id": 0, "props": {}, "children": items}
        node = self._find_node([root], parent_id) or root
        return self._node_pytuple(node, depth if depth != 0 else -1)

    def _find_node(self, items, nid):
        for n in items:
            if n["id"] == nid:
                return n
            found = self._find_node(n["children"], nid)
            if found:
                return found
        return None

    def _menu_get_prop(self, conn, sender, path, iface, name):
        if name == "Version":
            return GLib.Variant("u", 3)
        if name == "TextDirection":
            return GLib.Variant("s", "ltr")
        if name == "Status":
            return GLib.Variant("s", "normal")
        if name == "IconThemePath":
            return GLib.Variant("as", [])
        return None

    def _menu_method(self, conn, sender, path, iface, method, params, invocation):
        if method == "GetLayout":
            parent_id, depth, _props = params.unpack()
            layout = self._layout_pytuple(parent_id, depth)
            invocation.return_value(GLib.Variant("(u(ia{sv}av))",
                                                 (self.revision, layout)))
        elif method == "GetGroupProperties":
            ids, _names = params.unpack()
            items = self._build_menu()
            result = []
            for nid in ids:
                node = self._find_node(items, nid)
                if node:
                    result.append((nid, self._props_variant(node["props"])))
            invocation.return_value(GLib.Variant("(a(ia{sv}))", (result,)))
        elif method == "GetProperty":
            nid, pname = params.unpack()
            items = self._build_menu()
            node = self._find_node(items, nid)
            val = (node or {}).get("props", {}).get(pname, "")
            invocation.return_value(GLib.Variant("(v)",
                                    (self._props_variant({pname: val})[pname],)))
        elif method == "Event":
            nid, event_id, _data, _ts = params.unpack()
            if event_id == "clicked":
                self._build_menu()  # ensure _actions populated for this id set
                GLib.idle_add(self._dispatch, nid)
            invocation.return_value(None)
        elif method == "AboutToShow":
            # Rebuild so temps/checks are fresh when the menu opens.
            self.revision += 1
            invocation.return_value(GLib.Variant("(b)", (True,)))
        else:
            invocation.return_value(None)

    def _dispatch(self, nid):
        action = self._actions.get(nid)
        if not action:
            return False
        kind = action[0]
        if kind == "show":
            self.window.show_from_tray()
        elif kind == "quit":
            self.window.quit_app()
        elif kind == "profile":
            self.window.backend.send({"cmd": "set_profile", "profile": action[1]})
        elif kind == "mode":
            self.window.backend.send({"cmd": "set_mode", "mode": action[1]})
        return False
