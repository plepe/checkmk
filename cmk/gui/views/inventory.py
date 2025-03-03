#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (C) 2019 tribe29 GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.

from __future__ import annotations

import abc
import time
from dataclasses import dataclass
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    NamedTuple,
    Optional,
    Sequence,
    Tuple,
    TYPE_CHECKING,
    Union,
)

from livestatus import LivestatusResponse, OnlySites, SiteId

import cmk.utils.defines as defines
import cmk.utils.render
from cmk.utils.regex import regex
from cmk.utils.structured_data import (
    Attributes,
    RetentionIntervals,
    SDKey,
    SDKeyColumns,
    SDPath,
    SDRawPath,
    SDRow,
    SDValue,
    StructuredDataNode,
    Table,
)
from cmk.utils.type_defs import HostName

import cmk.gui.inventory as inventory
import cmk.gui.pages
import cmk.gui.sites as sites
from cmk.gui.config import active_config
from cmk.gui.exceptions import MKUserError
from cmk.gui.hooks import request_memoize
from cmk.gui.htmllib.foldable_container import foldable_container
from cmk.gui.htmllib.generator import HTMLWriter
from cmk.gui.htmllib.html import html
from cmk.gui.http import request
from cmk.gui.i18n import _
from cmk.gui.plugins.views.utils import (
    ABCDataSource,
    Cell,
    cmp_simple_number,
    data_source_registry,
    declare_1to1_sorter,
    display_options,
    inventory_displayhints,
    InventoryHintSpec,
    multisite_builtin_views,
    paint_age,
    Painter,
    painter_option_registry,
    painter_registry,
    PainterOption,
    PainterOptions,
    register_painter,
    register_sorter,
    RowTable,
)
from cmk.gui.plugins.visuals.inventory import (
    FilterInvBool,
    FilterInvFloat,
    FilterInvtableIDRange,
    FilterInvtableText,
    FilterInvText,
)
from cmk.gui.plugins.visuals.utils import (
    filter_registry,
    get_livestatus_filter_headers,
    get_ranged_table,
    visual_info_registry,
    VisualInfo,
)
from cmk.gui.type_defs import ColumnName, FilterName, Icon, Row, Rows, SingleInfos
from cmk.gui.utils.escaping import escape_text
from cmk.gui.utils.html import HTML
from cmk.gui.utils.output_funnel import output_funnel
from cmk.gui.utils.urls import makeuri_contextless
from cmk.gui.utils.user_errors import user_errors
from cmk.gui.valuespec import Checkbox, Dictionary
from cmk.gui.view_utils import CellSpec, render_labels
from cmk.gui.views.builtin_views import host_view_filters

if TYPE_CHECKING:
    from cmk.gui.plugins.visuals.utils import Filter
    from cmk.gui.views import View


PaintResult = Tuple[str, Union[str, HTML]]
PaintFunction = Callable[[Any], PaintResult]


_PAINT_FUNCTION_NAME_PREFIX = "inv_paint_"
_PAINT_FUNCTIONS: dict[str, PaintFunction] = {}


def update_paint_functions(mapping: Mapping[str, Any]) -> None:
    # Update paint functions from
    # 1. views (local web plugins are loaded including display hints and paint functions)
    # 2. here
    _PAINT_FUNCTIONS.update(
        {k: v for k, v in mapping.items() if k.startswith(_PAINT_FUNCTION_NAME_PREFIX)}
    )


def _get_paint_function_from_globals(paint_name: str) -> PaintFunction:
    # Do not overwrite local paint functions
    update_paint_functions({k: v for k, v in globals().items() if k not in _PAINT_FUNCTIONS})
    return _PAINT_FUNCTIONS[_PAINT_FUNCTION_NAME_PREFIX + paint_name]


class MultipleInventoryTreesError(Exception):
    pass


def _validate_inventory_tree_uniqueness(row: Row) -> None:
    raw_hostname = row.get("host_name")
    assert isinstance(raw_hostname, str)

    if (
        len(
            sites_with_same_named_hosts := _get_sites_with_same_named_hosts_cache().get(
                HostName(raw_hostname), []
            )
        )
        > 1
    ):
        html.show_error(
            _("Cannot display inventory tree of host '%s': Found this host on multiple sites: %s")
            % (raw_hostname, ", ".join(sites_with_same_named_hosts))
        )
        raise MultipleInventoryTreesError()


@request_memoize()
def _get_sites_with_same_named_hosts_cache() -> Mapping[HostName, Sequence[SiteId]]:
    cache: Dict[HostName, List[SiteId]] = {}
    query_str = "GET hosts\nColumns: host_name\n"
    with sites.prepend_site():
        for row in sites.live().query(query_str):
            cache.setdefault(HostName(row[1]), []).append(SiteId(row[0]))
    return cache


def _cmp_inv_generic(val_a: object, val_b: object) -> int:
    if isinstance(val_a, (float, int)) and isinstance(val_b, (float, int)):
        return (val_a > val_b) - (val_a < val_b)

    if isinstance(val_a, str) and isinstance(val_b, str):
        return (val_a > val_b) - (val_a < val_b)

    raise TypeError(
        "Unsupported operand types for > and < (%s and %s)" % (type(val_a), type(val_b))
    )


@painter_option_registry.register
class PainterOptionShowInternalTreePaths(PainterOption):
    @property
    def ident(self):
        return "show_internal_tree_paths"

    @property
    def valuespec(self):
        return Checkbox(
            title=_("Show internal tree paths"),
            default_value=False,
        )


@painter_registry.register
class PainterInventoryTree(Painter):
    @property
    def ident(self):
        return "inventory_tree"

    def title(self, cell):
        return _("Hardware & Software Tree")

    @property
    def columns(self):
        return ["host_inventory", "host_structured_status"]

    @property
    def painter_options(self):
        return ["show_internal_tree_paths"]

    @property
    def load_inv(self):
        return True

    def render(self, row: Row, cell: Cell) -> CellSpec:
        try:
            _validate_inventory_tree_uniqueness(row)
        except MultipleInventoryTreesError:
            return "", ""

        struct_tree = row.get("host_inventory")
        if struct_tree is None:
            return "", ""

        assert isinstance(struct_tree, StructuredDataNode)

        painter_options = PainterOptions.get_instance()
        tree_renderer = NodeRenderer(
            row["site"],
            row["host_name"],
            show_internal_tree_paths=painter_options.get("show_internal_tree_paths"),
        )

        with output_funnel.plugged():
            struct_tree.show(tree_renderer)
            code = HTML(output_funnel.drain())

        return "invtree", code


class ABCRowTable(RowTable):
    def __init__(self, info_names, add_host_columns):
        super().__init__()
        self._info_names = info_names
        self._add_host_columns = add_host_columns

    def query(
        self,
        view: View,
        columns: List[ColumnName],
        headers: str,
        only_sites: OnlySites,
        limit,
        all_active_filters: List[Filter],
    ) -> Tuple[Rows, int]:
        self._add_declaration_errors()

        # Create livestatus filter for filtering out hosts
        host_columns = (
            ["host_name"]
            + list({c for c in columns if c.startswith("host_") and c != "host_name"})
            + self._add_host_columns
        )

        query = "GET hosts\n"
        query += "Columns: " + (" ".join(host_columns)) + "\n"

        query += "".join(get_livestatus_filter_headers(view.context, all_active_filters))

        if (
            active_config.debug_livestatus_queries
            and html.output_format == "html"
            and display_options.enabled(display_options.W)
        ):
            html.open_div(class_="livestatus message", onmouseover="this.style.display='none';")
            html.open_tt()
            html.write_text(query.replace("\n", "<br>\n"))
            html.close_tt()
            html.close_div()

        data = self._get_raw_data(only_sites, query)

        # Now create big table of all inventory entries of these hosts
        headers = ["site"] + host_columns
        rows = []
        for row in data:
            hostrow: Row = dict(zip(headers, row))
            for subrow in self._get_rows(hostrow):
                subrow.update(hostrow)
                rows.append(subrow)
        return rows, len(data)

    def _get_raw_data(self, only_sites: OnlySites, query: str) -> LivestatusResponse:
        sites.live().set_only_sites(only_sites)
        sites.live().set_prepend_site(True)
        data = sites.live().query(query)
        sites.live().set_prepend_site(False)
        sites.live().set_only_sites(None)
        return data

    def _get_rows(self, hostrow: Row) -> Iterable[Row]:
        inv_data = self._get_inv_data(hostrow)
        return self._prepare_rows(inv_data)

    @abc.abstractmethod
    def _get_inv_data(self, hostrow: Row) -> Any:
        raise NotImplementedError()

    @abc.abstractmethod
    def _prepare_rows(self, inv_data: Any) -> Iterable[Row]:
        raise NotImplementedError()

    def _add_declaration_errors(self) -> None:
        pass


# .
#   .--paint helper--------------------------------------------------------.
#   |                   _       _     _          _                         |
#   |       _ __   __ _(_)_ __ | |_  | |__   ___| |_ __   ___ _ __         |
#   |      | '_ \ / _` | | '_ \| __| | '_ \ / _ \ | '_ \ / _ \ '__|        |
#   |      | |_) | (_| | | | | | |_  | | | |  __/ | |_) |  __/ |           |
#   |      | .__/ \__,_|_|_| |_|\__| |_| |_|\___|_| .__/ \___|_|           |
#   |      |_|                                    |_|                      |
#   '----------------------------------------------------------------------'


def decorate_inv_paint(
    skip_painting_if_string: bool = False,
) -> Callable[[PaintFunction], PaintFunction]:
    def decorator(f: PaintFunction) -> PaintFunction:
        def wrapper(v: Any) -> PaintResult:
            if v in ["", None]:
                return "", ""
            if skip_painting_if_string and isinstance(v, str):
                return "number", v
            return f(v)

        return wrapper

    return decorator


@decorate_inv_paint()
def inv_paint_generic(v: Union[str, float, int]) -> PaintResult:
    if isinstance(v, float):
        return "number", "%.2f" % v
    if isinstance(v, int):
        return "number", "%d" % v
    return "", escape_text("%s" % v)


@decorate_inv_paint(skip_painting_if_string=True)
def inv_paint_hz(hz: float) -> PaintResult:
    return "number", cmk.utils.render.fmt_number_with_precision(hz, drop_zeroes=False, unit="Hz")


@decorate_inv_paint(skip_painting_if_string=True)
def inv_paint_bytes(b: int) -> PaintResult:
    if b == 0:
        return "number", "0"
    return "number", cmk.utils.render.fmt_bytes(b, precision=0)


@decorate_inv_paint(skip_painting_if_string=True)
def inv_paint_size(b: int) -> PaintResult:
    return "number", cmk.utils.render.fmt_bytes(b)


@decorate_inv_paint(skip_painting_if_string=True)
def inv_paint_bytes_rounded(b: int) -> PaintResult:
    if b == 0:
        return "number", "0"
    return "number", cmk.utils.render.fmt_bytes(b)


@decorate_inv_paint()
def inv_paint_number(b: Union[str, int, float]) -> PaintResult:
    return "number", str(b)


# Similar to paint_number, but is allowed to
# abbreviate things if numbers are very large
# (though it doesn't do so yet)
@decorate_inv_paint()
def inv_paint_count(b: Union[str, int, float]) -> PaintResult:
    return "number", str(b)


@decorate_inv_paint()
def inv_paint_nic_speed(bits_per_second: str) -> PaintResult:
    return "number", cmk.utils.render.fmt_nic_speed(bits_per_second)


@decorate_inv_paint()
def inv_paint_if_oper_status(oper_status: int) -> PaintResult:
    if oper_status == 1:
        css_class = "if_state_up"
    elif oper_status == 2:
        css_class = "if_state_down"
    else:
        css_class = "if_state_other"

    return "if_state " + css_class, defines.interface_oper_state_name(
        oper_status, "%s" % oper_status
    ).replace(" ", "&nbsp;")


# admin status can only be 1 or 2, matches oper status :-)
@decorate_inv_paint()
def inv_paint_if_admin_status(admin_status: int) -> PaintResult:
    return inv_paint_if_oper_status(admin_status)


@decorate_inv_paint()
def inv_paint_if_port_type(port_type: int) -> PaintResult:
    type_name = defines.interface_port_types().get(port_type, _("unknown"))
    return "", "%d - %s" % (port_type, type_name)


@decorate_inv_paint()
def inv_paint_if_available(available: bool) -> PaintResult:
    return "if_state " + (available and "if_available" or "if_not_available"), (
        available and _("free") or _("used")
    )


@decorate_inv_paint()
def inv_paint_mssql_is_clustered(clustered: bool) -> PaintResult:
    return "mssql_" + (clustered and "is_clustered" or "is_not_clustered"), (
        clustered and _("is clustered") or _("is not clustered")
    )


@decorate_inv_paint()
def inv_paint_mssql_node_names(node_names: str) -> PaintResult:
    return "", node_names


@decorate_inv_paint()
def inv_paint_ipv4_network(nw: str) -> PaintResult:
    if nw == "0.0.0.0/0":
        return "", _("Default")
    return "", nw


@decorate_inv_paint()
def inv_paint_ip_address_type(t: str) -> PaintResult:
    if t == "ipv4":
        return "", _("IPv4")
    if t == "ipv6":
        return "", _("IPv6")
    return "", t


@decorate_inv_paint()
def inv_paint_route_type(rt: str) -> PaintResult:
    if rt == "local":
        return "", _("Local route")
    return "", _("Gateway route")


@decorate_inv_paint(skip_painting_if_string=True)
def inv_paint_volt(volt: float) -> PaintResult:
    return "number", "%.1f V" % volt


@decorate_inv_paint(skip_painting_if_string=True)
def inv_paint_date(timestamp: int) -> PaintResult:
    date_painted = time.strftime("%Y-%m-%d", time.localtime(timestamp))
    return "number", "%s" % date_painted


@decorate_inv_paint(skip_painting_if_string=True)
def inv_paint_date_and_time(timestamp: int) -> PaintResult:
    date_painted = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))
    return "number", "%s" % date_painted


@decorate_inv_paint(skip_painting_if_string=True)
def inv_paint_age(age: float) -> PaintResult:
    return "number", cmk.utils.render.approx_age(age)


@decorate_inv_paint()
def inv_paint_bool(value: bool) -> PaintResult:
    return "", (_("Yes") if value else _("No"))


@decorate_inv_paint(skip_painting_if_string=True)
def inv_paint_timestamp_as_age(timestamp: int) -> PaintResult:
    age = time.time() - timestamp
    return inv_paint_age(age)


@decorate_inv_paint(skip_painting_if_string=True)
def inv_paint_timestamp_as_age_days(timestamp: int) -> PaintResult:
    def round_to_day(ts):
        broken = time.localtime(ts)
        return int(
            time.mktime(
                (
                    broken.tm_year,
                    broken.tm_mon,
                    broken.tm_mday,
                    0,
                    0,
                    0,
                    broken.tm_wday,
                    broken.tm_yday,
                    broken.tm_isdst,
                )
            )
        )

    now_day = round_to_day(time.time())
    change_day = round_to_day(timestamp)
    age_days = int((now_day - change_day) / 86400.0)

    css_class = "number"
    if age_days == 0:
        return css_class, _("today")
    if age_days == 1:
        return css_class, _("yesterday")
    return css_class, "%d %s ago" % (int(age_days), _("days"))


@decorate_inv_paint()
def inv_paint_csv_labels(csv_list: str) -> PaintResult:
    return "labels", HTMLWriter.render_br().join(csv_list.split(","))


@decorate_inv_paint()
def inv_paint_cmk_label(label: List[str]) -> PaintResult:
    return "labels", render_labels(
        {label[0]: label[1]},
        object_type="host",
        with_links=True,
        label_sources={label[0]: "discovered"},
    )


@decorate_inv_paint()
def inv_paint_container_ready(ready: str) -> PaintResult:
    if ready == "yes":
        css_class = "if_state_up"
    elif ready == "no":
        css_class = "if_state_down"
    else:
        css_class = "if_state_other"

    return "if_state " + css_class, ready


@decorate_inv_paint()
def inv_paint_service_status(status: str) -> PaintResult:
    if status == "running":
        css_class = "if_state_up"
    elif status == "stopped":
        css_class = "if_state_down"
    else:
        css_class = "if_not_available"

    return "if_state " + css_class, status


# .
#   .--display hints-------------------------------------------------------.
#   |           _ _           _               _     _       _              |
#   |        __| (_)___ _ __ | | __ _ _   _  | |__ (_)_ __ | |_ ___        |
#   |       / _` | / __| '_ \| |/ _` | | | | | '_ \| | '_ \| __/ __|       |
#   |      | (_| | \__ \ |_) | | (_| | |_| | | | | | | | | | |_\__ \       |
#   |       \__,_|_|___/ .__/|_|\__,_|\__, | |_| |_|_|_| |_|\__|___/       |
#   |                  |_|            |___/                                |
#   '----------------------------------------------------------------------'


def _get_raw_path(path: SDPath) -> str:
    return ".".join(map(str, path))


def _get_paint_function(raw_hint: InventoryHintSpec) -> tuple[str, PaintFunction]:
    # FIXME At the moment  we need it to get tdclass: Clean this up one day.
    if "paint" in raw_hint:
        data_type = raw_hint["paint"]
        return data_type, _get_paint_function_from_globals(data_type)

    return "str", inv_paint_generic


def _make_title_function(raw_hint: InventoryHintSpec) -> Callable[[str], str]:
    if "title" not in raw_hint:
        return lambda word: word.replace("_", " ").title()

    if callable(title := raw_hint["title"]):
        return title

    return lambda word: title


def _make_long_title_function(title: str, parent_path: SDPath) -> Callable[[], str]:
    return lambda: (
        NodeDisplayHint.make(parent_path).title + " ➤ " + title if parent_path else title
    )


@dataclass(frozen=True)
class NodeDisplayHint:
    raw_path: str
    icon: Optional[str]
    title: str
    short_title: str
    _long_title_function: Callable[[], str]
    attributes_hint: AttributesDisplayHint
    table_hint: TableDisplayHint

    @property
    def long_title(self) -> str:
        return self._long_title_function()

    @classmethod
    def make(cls, path: SDPath) -> NodeDisplayHint:
        raw_path, raw_hint, attributes_hint, table_hint = cls._get_raw_path_or_hint(path)
        title = _make_title_function(raw_hint)(path[-1] if path else "")
        return cls(
            raw_path=raw_path,
            icon=raw_hint.get("icon"),
            title=title,
            short_title=raw_hint.get("short", title),
            _long_title_function=_make_long_title_function(title, path[:-1]),
            attributes_hint=attributes_hint,
            table_hint=table_hint,
        )

    @staticmethod
    def _get_raw_path_or_hint(
        path: SDPath,
    ) -> Tuple[str, InventoryHintSpec, AttributesDisplayHint, TableDisplayHint]:
        if not path:
            raw_path = "."
            return (
                raw_path,
                inventory_displayhints.get(raw_path, {}),
                AttributesDisplayHint.make_from_hint({}),
                TableDisplayHint.make_from_hint({}),
            )

        # We either have attributes or a table.
        if (raw_path := f".{_get_raw_path(path)}.") in inventory_displayhints:
            raw_hint = inventory_displayhints[raw_path]
            return (
                raw_path,
                raw_hint,
                AttributesDisplayHint.make_from_hint(raw_hint),
                TableDisplayHint.make_from_hint({}),
            )

        if (raw_path := f".{_get_raw_path(path)}:") in inventory_displayhints:
            raw_hint = inventory_displayhints[raw_path]
            return (
                raw_path,
                raw_hint,
                AttributesDisplayHint.make_from_hint({}),
                TableDisplayHint.make_from_hint(raw_hint),
            )

        return (
            f".{_get_raw_path(path)}.",
            {},
            AttributesDisplayHint.make_from_hint({}),
            TableDisplayHint.make_from_hint({}),
        )

    @classmethod
    def make_from_hint(cls, raw_path: SDRawPath, raw_hint: InventoryHintSpec) -> NodeDisplayHint:
        inventory_path = inventory.InventoryPath.parse(raw_path)
        attributes_hint, table_hint = cls._get_attributes_or_table_hint(inventory_path, raw_hint)
        title = _make_title_function(raw_hint)(inventory_path.node_name)
        return cls(
            raw_path=raw_path,
            icon=raw_hint.get("icon"),
            title=title,
            short_title=raw_hint.get("short", title),
            _long_title_function=_make_long_title_function(title, inventory_path.path[:-1]),
            attributes_hint=attributes_hint,
            table_hint=table_hint,
        )

    @staticmethod
    def _get_attributes_or_table_hint(
        inventory_path: inventory.InventoryPath, raw_hint: InventoryHintSpec
    ) -> Tuple[AttributesDisplayHint, TableDisplayHint]:
        if inventory_path.source == inventory.TreeSource.table:
            return (
                AttributesDisplayHint.make_from_hint({}),
                TableDisplayHint.make_from_hint(raw_hint),
            )
        return (
            AttributesDisplayHint.make_from_hint(raw_hint),
            TableDisplayHint.make_from_hint({}),
        )


class TableTitle(NamedTuple):
    title: str
    key: str
    is_key_column: bool


@dataclass(frozen=True)
class TableDisplayHint:
    key_order: Sequence[str]
    is_show_more: bool
    view_name: Optional[str]

    @classmethod
    def make_from_hint(cls, raw_hint: InventoryHintSpec) -> TableDisplayHint:
        return cls(
            key_order=raw_hint.get("keyorder", []),
            is_show_more=raw_hint.get("is_show_more", True),
            view_name=raw_hint.get("view"),
        )

    def make_titles(
        self, rows: Sequence[SDRow], key_columns: SDKeyColumns, path: SDPath
    ) -> Sequence[TableTitle]:
        sorting_keys = list(self.key_order) + sorted(
            set(k for r in rows for k in r) - set(self.key_order)
        )
        return [
            TableTitle(col_hint.short_title, k, k in key_columns)
            for k in sorting_keys
            for col_hint in (ColumnDisplayHint.make(path, k),)
        ]

    def sort_rows(self, rows: Sequence[SDRow], titles: Sequence[TableTitle]) -> Sequence[SDRow]:
        return sorted(rows, key=lambda r: tuple(r.get(t.key) or "" for t in titles))


@dataclass(frozen=True)
class ColumnDisplayHint:
    title: str
    short_title: str
    data_type: str
    paint_function: PaintFunction
    sort_function: Callable[[Any, Any], int]  # TODO improve type hints for args

    @classmethod
    def make(cls, path: SDPath, key: str) -> ColumnDisplayHint:
        raw_hint = inventory_displayhints.get(f".{_get_raw_path(path)}:*.{key}", {}) if path else {}
        data_type, paint_function = _get_paint_function(raw_hint)
        title = _make_title_function(raw_hint)(key)
        return cls(
            title=title,
            short_title=raw_hint.get("short", title),
            data_type=data_type,
            paint_function=paint_function,
            sort_function=raw_hint.get("sort", _cmp_inv_generic),
        )

    @classmethod
    def make_from_hint(cls, raw_path: SDRawPath, raw_hint: InventoryHintSpec) -> ColumnDisplayHint:
        inventory_path = inventory.InventoryPath.parse(raw_path)
        data_type, paint_function = _get_paint_function(raw_hint)
        title = _make_title_function(raw_hint)(
            inventory_path.key if inventory_path.key else inventory_path.node_name
        )
        return cls(
            title=title,
            short_title=raw_hint.get("short", title),
            data_type=data_type,
            paint_function=paint_function,
            sort_function=raw_hint.get("sort", _cmp_inv_generic),
        )


@dataclass(frozen=True)
class AttributesDisplayHint:
    key_order: Sequence[str]

    @classmethod
    def make_from_hint(cls, raw_hint: InventoryHintSpec) -> AttributesDisplayHint:
        return cls(
            key_order=raw_hint.get("keyorder", []),
        )

    def sort_pairs(self, pairs: Mapping[SDKey, SDValue]) -> Sequence[Tuple[SDKey, SDValue]]:
        sorting_keys = list(self.key_order) + sorted(set(pairs) - set(self.key_order))
        return [(k, pairs[k]) for k in sorting_keys if k in pairs]


@dataclass(frozen=True)
class AttributeDisplayHint:
    title: str
    short_title: str
    _long_title_function: Callable[[], str]
    data_type: str
    paint_function: PaintFunction
    is_show_more: bool

    @property
    def long_title(self) -> str:
        return self._long_title_function()

    @classmethod
    def make(cls, path: SDPath, key: str) -> AttributeDisplayHint:
        raw_hint = inventory_displayhints.get(f".{_get_raw_path(path)}.{key}", {}) if path else {}
        data_type, paint_function = _get_paint_function(raw_hint)
        title = _make_title_function(raw_hint)(key)
        return cls(
            title=title,
            short_title=raw_hint.get("short", title),
            _long_title_function=_make_long_title_function(title, path),
            data_type=data_type,
            paint_function=paint_function,
            is_show_more=raw_hint.get("is_show_more", True),
        )

    @classmethod
    def make_from_hint(
        cls, raw_path: SDRawPath, raw_hint: InventoryHintSpec
    ) -> AttributeDisplayHint:
        inventory_path = inventory.InventoryPath.parse(raw_path)
        data_type, paint_function = _get_paint_function(raw_hint)
        title = _make_title_function(raw_hint)(
            inventory_path.key if inventory_path.key else inventory_path.node_name
        )
        return cls(
            title=title,
            short_title=raw_hint.get("short", title),
            _long_title_function=_make_long_title_function(title, inventory_path.path),
            data_type=data_type,
            paint_function=paint_function,
            is_show_more=raw_hint.get("is_show_more", True),
        )


def _find_display_hint_id(raw_path: SDRawPath) -> Optional[str]:
    """Looks up the display hint for the given inventory path.

    It returns either the ID of the display hint matching the given raw_path
    or None in case no entry was found.

    In case no exact match is possible try to match display hints that use
    some kind of *-syntax. There are two types of cases here:

      :* -> Entries in lists (* resolves to list index numbers)
      .* -> Path entries (* resolves to a path element)

    The current logic has some limitations related to the ways stars can
    be used.
    """
    raw_path = raw_path.rstrip(".:")

    # Convert index of lists to *-syntax
    # e.g. ".foo.bar:18.test" to ".foo.bar:*.test"
    r = regex(r"([^\.]):[0-9]+")
    raw_path = r.sub("\\1:*", raw_path)

    candidates = [
        raw_path,
    ]

    # Produce a list of raw_path candidates with a "*" going from back to front.
    #
    # This algorithm only allows one ".*" in a raw_path. It finds the match with
    # the longest path prefix before the ".*".
    #
    # TODO: Implement a generic mechanism that allows as many stars as possible
    raw_path_parts = raw_path.split(".")
    star_index = len(raw_path_parts) - 1
    while star_index >= 0:
        parts = raw_path_parts[:star_index] + ["*"] + raw_path_parts[star_index + 1 :]
        raw_path_with_star = "%s" % ".".join(parts)
        candidates.append(raw_path_with_star)
        star_index -= 1

    for candidate in candidates:
        # TODO: Better cleanup trailing ":" and "." from display hints at all. They are useless
        # for finding the right entry.
        if candidate in inventory_displayhints:
            return candidate

        if candidate + "." in inventory_displayhints:
            return candidate + "."

        if candidate + ":" in inventory_displayhints:
            return candidate + ":"

    return None


def inv_titleinfo_long(raw_path: SDRawPath) -> str:
    """Return the titles of the last two path components of the node, e.g. "BIOS / Vendor"."""
    inventory_path = inventory.InventoryPath.parse(raw_path)

    if inventory_path.key:
        return AttributeDisplayHint.make(inventory_path.path, inventory_path.key).long_title

    return NodeDisplayHint.make(inventory_path.path).long_title


# .
#   .--inventory columns---------------------------------------------------.
#   |             _                      _                                 |
#   |            (_)_ ____   _____ _ __ | |_ ___  _ __ _   _               |
#   |            | | '_ \ \ / / _ \ '_ \| __/ _ \| '__| | | |              |
#   |            | | | | \ V /  __/ | | | || (_) | |  | |_| |              |
#   |            |_|_| |_|\_/ \___|_| |_|\__\___/|_|   \__, |              |
#   |                                                  |___/               |
#   |                          _                                           |
#   |                 ___ ___ | |_   _ _ __ ___  _ __  ___                 |
#   |                / __/ _ \| | | | | '_ ` _ \| '_ \/ __|                |
#   |               | (_| (_) | | |_| | | | | | | | | \__ \                |
#   |                \___\___/|_|\__,_|_| |_| |_|_| |_|___/                |
#   |                                                                      |
#   '----------------------------------------------------------------------'


def declare_inventory_columns() -> None:
    # create painters for node with a display hint
    for raw_path, raw_hint in inventory_displayhints.items():
        if "*" in raw_path:
            continue

        _declare_inv_column(raw_path, raw_hint)


def _declare_inv_column(raw_path: SDRawPath, raw_hint: InventoryHintSpec) -> None:
    """Declares painters, sorters and filters to be used in views based on all host related
    datasources."""
    if raw_path == ".":
        name = "inv"
    else:
        name = "inv_" + raw_path.replace(":", "_").replace(".", "_").strip("_")

    hint = _make_hint_from_raw(raw_path, raw_hint)

    # Declare column painter
    painter_spec = {
        "title": (
            raw_path == "." and _("Inventory Tree") or (_("Inventory") + ": " + hint.long_title)
        ),
        "short": hint.short_title,
        "columns": ["host_inventory", "host_structured_status"],
        "options": ["show_internal_tree_paths"],
        "params": Dictionary(
            title=_("Report options"),
            elements=[
                (
                    "use_short",
                    Checkbox(
                        title=_("Use short title in reports header"),
                        default_value=False,
                    ),
                ),
            ],
            required_keys=["use_short"],
        ),
        # Only attributes can be shown in reports. There is currently no way to render trees.
        # The HTML code would simply be stripped by the default rendering mechanism which does
        # not look good for the HW/SW inventory tree
        "printable": isinstance(hint, AttributeDisplayHint),
        "load_inv": True,
        "paint": lambda row: _paint_host_inventory_tree(row, raw_path),
        "sorter": name,
    }

    register_painter(name, painter_spec)

    # Sorters and Filters only for attributes
    if isinstance(hint, AttributeDisplayHint):
        # Declare sorter. It will detect numbers automatically
        register_sorter(
            name,
            {
                "_inv_path": raw_path,
                "title": _("Inventory") + ": " + hint.long_title,
                "columns": ["host_inventory", "host_structured_status"],
                "load_inv": True,
                "cmp": lambda self, a, b: _cmp_inventory_node(a, b, self._spec["_inv_path"]),
            },
        )

        # Declare filter. Sync this with _declare_invtable_column()
        if hint.data_type == "str":
            filter_registry.register(
                FilterInvText(
                    ident=name,
                    title=hint.long_title,
                    inv_path=raw_path,
                    is_show_more=hint.is_show_more,
                )
            )
        elif hint.data_type == "bool":
            filter_registry.register(
                FilterInvBool(
                    ident=name,
                    title=hint.long_title,
                    inv_path=raw_path,
                    is_show_more=hint.is_show_more,
                )
            )
        else:
            filter_info = _inv_filter_info().get(hint.data_type, {})
            filter_registry.register(
                FilterInvFloat(
                    ident=name,
                    title=hint.long_title,
                    inv_path=raw_path,
                    unit=filter_info.get("unit"),
                    scale=filter_info.get("scale", 1.0),
                    is_show_more=hint.is_show_more,
                )
            )


def _make_hint_from_raw(
    raw_path: SDRawPath, raw_hint: InventoryHintSpec
) -> Union[NodeDisplayHint, AttributeDisplayHint]:
    if raw_path.endswith(".") or raw_path.endswith(":"):
        return NodeDisplayHint.make_from_hint(raw_path, raw_hint)
    return AttributeDisplayHint.make_from_hint(raw_path, raw_hint)


def _cmp_inventory_node(
    a: Dict[str, StructuredDataNode], b: Dict[str, StructuredDataNode], raw_path: SDRawPath
) -> int:
    val_a = inventory.get_inventory_attribute(a["host_inventory"], raw_path)
    val_b = inventory.get_inventory_attribute(b["host_inventory"], raw_path)
    return _decorate_sort_func(_cmp_inv_generic)(val_a, val_b)


def _paint_host_inventory_tree(row: Row, raw_path: SDRawPath) -> CellSpec:
    try:
        _validate_inventory_tree_uniqueness(row)
    except MultipleInventoryTreesError:
        return "", ""

    struct_tree = row.get("host_inventory")
    if struct_tree is None:
        return "", ""

    assert isinstance(struct_tree, StructuredDataNode)

    inventory_path = inventory.InventoryPath.parse(raw_path)

    painter_options = PainterOptions.get_instance()
    tree_renderer = NodeRenderer(
        row["site"],
        row["host_name"],
        show_internal_tree_paths=painter_options.get("show_internal_tree_paths"),
    )

    td_class = ""
    with output_funnel.plugged():
        if inventory_path.source == inventory.TreeSource.node:
            if node := struct_tree.get_node(inventory_path.path):
                node.show(tree_renderer)
                td_class = "invtree"

        elif inventory_path.source == inventory.TreeSource.table:
            if table := struct_tree.get_table(inventory_path.path):
                table.show(tree_renderer)

        elif inventory_path.source == inventory.TreeSource.attributes:
            if (
                attributes := struct_tree.get_attributes(inventory_path.path)
            ) and inventory_path.key in attributes.pairs:
                tree_renderer.show_attribute(
                    attributes.pairs[inventory_path.key],
                    AttributeDisplayHint.make(inventory_path.path, inventory_path.key),
                )

        code = HTML(output_funnel.drain())

    return td_class, code


def _inv_filter_info():
    return {
        "bytes": {"unit": _("MB"), "scale": 1024 * 1024},
        "bytes_rounded": {"unit": _("MB"), "scale": 1024 * 1024},
        "hz": {"unit": _("MHz"), "scale": 1000000},
        "volt": {"unit": _("Volt")},
        "timestamp": {"unit": _("secs")},
    }


# .
#   .--Datasources---------------------------------------------------------.
#   |       ____        _                                                  |
#   |      |  _ \  __ _| |_ __ _ ___  ___  _   _ _ __ ___ ___  ___         |
#   |      | | | |/ _` | __/ _` / __|/ _ \| | | | '__/ __/ _ \/ __|        |
#   |      | |_| | (_| | || (_| \__ \ (_) | |_| | | | (_|  __/\__ \        |
#   |      |____/ \__,_|\__\__,_|___/\___/ \__,_|_|  \___\___||___/        |
#   |                                                                      |
#   +----------------------------------------------------------------------+
#   |  Basic functions for creating datasources for for table-like infor-  |
#   |  mation like software packages or network interfaces. That way the   |
#   |  user can access inventory data just like normal Livestatus tables.  |
#   |  This is needed for inventory data that is organized in tables.      |
#   |  Data where there is one fixed path per host for an item (like the   |
#   |  number of CPU cores) no datasource is being needed. These are just  |
#   |  painters that are available in the hosts info.                      |
#   '----------------------------------------------------------------------'


def _inv_find_subtable_columns(raw_path: SDRawPath) -> List[str]:
    """Find the name of all columns of an embedded table that have a display
    hint. Respects the order of the columns if one is specified in the
    display hint.

    Also use the names found in keyorder to get even more of the available columns."""
    table_hint = NodeDisplayHint.make_from_hint(
        raw_path, inventory_displayhints[raw_path]
    ).table_hint

    # Create dict from column name to its order number in the list
    with_numbers = enumerate(table_hint.key_order)
    swapped = [(t[1], t[0]) for t in with_numbers]
    order = dict(swapped)

    columns = []
    for path in inventory_displayhints:
        if path.startswith(raw_path + "*."):
            # ".networking.interfaces:*.port_type" -> "port_type"
            columns.append(path.split(".")[-1])

    for key in table_hint.key_order:
        if key not in columns:
            columns.append(key)

    return sorted(columns, key=lambda x: (order.get(x, 999), x))


def _decorate_sort_func(f):
    def wrapper(val_a, val_b):
        if val_a is None:
            return 0 if val_b is None else -1

        if val_b is None:
            return 0 if val_a is None else 1

        return f(val_a, val_b)

    return wrapper


def _declare_invtable_column(
    infoname: str, raw_table_path: SDRawPath, topic: str, name: str, column: str
) -> None:
    raw_col_path = raw_table_path + "*." + name
    raw_col_hint = inventory_displayhints.get(raw_col_path, {})
    col_hint = ColumnDisplayHint.make_from_hint(raw_col_path, raw_col_hint)

    # TODO
    # - Sync this with _declare_inv_column()
    # - filter -> ColumnDisplayHint
    filter_class = raw_col_hint.get("filter")
    if filter_class:
        filter_registry.register(
            filter_class(
                inv_info=infoname,
                ident=infoname + "_" + name,
                title=topic + ": " + col_hint.title,
            )
        )
    elif (ranged_table := get_ranged_table(infoname)) is not None:
        filter_registry.register(
            FilterInvtableIDRange(
                inv_info=ranged_table,
                ident=infoname + "_" + name,
                title=topic + ": " + col_hint.title,
            )
        )
    else:
        filter_registry.register(
            FilterInvtableText(
                inv_info=infoname,
                ident=infoname + "_" + name,
                title=topic + ": " + col_hint.title,
            )
        )

    register_painter(
        column,
        {
            "title": topic + ": " + col_hint.title,
            "short": col_hint.short_title,
            "columns": [column],
            "paint": lambda row: col_hint.paint_function(row.get(column)),
            "sorter": column,
        },
    )

    register_sorter(
        column,
        {
            "title": _("Inventory") + ": " + col_hint.title,
            "columns": [column],
            "cmp": lambda self, a, b: _decorate_sort_func(col_hint.sort_function)(
                a.get(column), b.get(column)
            ),
        },
    )


class RowTableInventory(ABCRowTable):
    def __init__(self, info_name: str, inventory_path: SDRawPath) -> None:
        super().__init__([info_name], ["host_structured_status"])
        self._inventory_path = inventory_path

    def _get_inv_data(self, hostrow: Row) -> inventory.InventoryRows:
        try:
            merged_tree = inventory.load_filtered_and_merged_tree(hostrow)
        except inventory.LoadStructuredDataError:
            user_errors.add(
                MKUserError(
                    "load_inventory_tree",
                    _("Cannot load HW/SW inventory tree %s. Please remove the corrupted file.")
                    % inventory.get_short_inventory_filepath(hostrow.get("host_name", "")),
                )
            )
            return []

        if merged_tree is None:
            return []

        table = inventory.get_inventory_table(merged_tree, self._inventory_path)
        if table is None:
            return []
        return table

    def _prepare_rows(self, inv_data: inventory.InventoryRows) -> Iterable[Row]:
        # TODO check: hopefully there's only a table as input arg
        info_name = self._info_names[0]
        entries = []
        for entry in inv_data:
            newrow: Row = {}
            for key, value in entry.items():
                newrow[info_name + "_" + key] = value
            entries.append(newrow)
        return entries


class ABCDataSourceInventory(ABCDataSource):
    @property
    def ignore_limit(self):
        return True

    @property
    @abc.abstractmethod
    def inventory_path(self) -> str:
        raise NotImplementedError()


# One master function that does all
def declare_invtable_view(
    infoname: str,
    raw_path: SDRawPath,
    title_singular: str,
    title_plural: str,
    icon: Optional[Icon] = None,
) -> None:
    _register_info_class(infoname, title_singular, title_plural)

    # Create the datasource (like a database view)
    ds_class = type(
        "DataSourceInventory%s" % infoname.title(),
        (ABCDataSourceInventory,),
        {
            "_ident": infoname,
            "_inventory_path": raw_path,
            "_title": "%s: %s" % (_("Inventory"), title_plural),
            "_infos": ["host", infoname],
            "ident": property(lambda s: s._ident),
            "title": property(lambda s: s._title),
            "table": property(lambda s: RowTableInventory(s._ident, s._inventory_path)),
            "infos": property(lambda s: s._infos),
            "keys": property(lambda s: []),
            "id_keys": property(lambda s: []),
            "inventory_path": property(lambda s: s._inventory_path),
        },
    )
    data_source_registry.register(ds_class)

    painters: List[Tuple[str, str, str]] = []
    filters = []
    for name in _inv_find_subtable_columns(raw_path):
        column = infoname + "_" + name

        # Declare a painter, sorter and filters for each path with display hint
        _declare_invtable_column(infoname, raw_path, title_singular, name, column)

        painters.append((column, "", ""))
        filters.append(column)

    _declare_views(infoname, title_plural, painters, filters, [raw_path], icon)


InventorySourceRows = Tuple[str, inventory.InventoryRows]


class RowMultiTableInventory(ABCRowTable):
    def __init__(
        self,
        sources: List[Tuple[str, SDRawPath]],
        match_by: List[str],
        errors: List[str],
    ) -> None:
        super().__init__([infoname for infoname, _path in sources], ["host_structured_status"])
        self._sources = sources
        self._match_by = match_by
        self._errors = errors

    def _get_inv_data(self, hostrow: Row) -> List[InventorySourceRows]:
        try:
            merged_tree = inventory.load_filtered_and_merged_tree(hostrow)
        except inventory.LoadStructuredDataError:
            user_errors.add(
                MKUserError(
                    "load_inventory_tree",
                    _("Cannot load HW/SW inventory tree %s. Please remove the corrupted file.")
                    % inventory.get_short_inventory_filepath(hostrow.get("host_name", "")),
                )
            )
            return []

        if merged_tree is None:
            return []

        multi_table: List[InventorySourceRows] = []
        for info_name, inventory_path in self._sources:
            table = inventory.get_inventory_table(merged_tree, inventory_path)
            if table is not None:
                multi_table.append((info_name, table))
        return multi_table

    def _prepare_rows(self, inv_data: List[InventorySourceRows]) -> Iterable[Row]:
        joined_rows: Dict[Tuple[str, ...], Dict] = {}
        for this_info_name, this_inv_data in inv_data:
            for entry in this_inv_data:
                inst = joined_rows.setdefault(tuple(entry[key] for key in self._match_by), {})
                inst.update({this_info_name + "_" + k: v for k, v in entry.items()})
        return [joined_rows[match_by_key] for match_by_key in sorted(joined_rows)]

    def _add_declaration_errors(self) -> None:
        if self._errors:
            user_errors.add(MKUserError("declare_invtable_view", ", ".join(self._errors)))


def declare_joined_inventory_table_view(
    tablename: str,
    title_singular: str,
    title_plural: str,
    tables: List[str],
    match_by: List[str],
) -> None:

    _register_info_class(tablename, title_singular, title_plural)

    info_names: List[str] = []
    raw_paths: List[str] = []
    titles: List[str] = []
    errors = []
    for this_tablename in tables:
        visual_info_class = visual_info_registry.get(this_tablename)
        data_source_class = data_source_registry.get(this_tablename)
        if data_source_class is None or visual_info_class is None:
            errors.append(
                "Missing declare_invtable_view for inventory table view '%s'" % this_tablename
            )
            continue

        assert issubclass(data_source_class, ABCDataSourceInventory)
        ds = data_source_class()
        info_names.append(ds.ident)
        raw_paths.append(ds.inventory_path)
        titles.append(visual_info_class().title)

    # Create the datasource (like a database view)
    ds_class = type(
        "DataSourceInventory%s" % tablename.title(),
        (ABCDataSource,),
        {
            "_ident": tablename,
            "_sources": list(zip(info_names, raw_paths)),
            "_match_by": match_by,
            "_errors": errors,
            "_title": "%s: %s" % (_("Inventory"), title_plural),
            "_infos": ["host"] + info_names,
            "ident": property(lambda s: s._ident),
            "title": property(lambda s: s._title),
            "table": property(lambda s: RowMultiTableInventory(s._sources, s._match_by, s._errors)),
            "infos": property(lambda s: s._infos),
            "keys": property(lambda s: []),
            "id_keys": property(lambda s: []),
        },
    )
    data_source_registry.register(ds_class)

    known_common_columns = set()
    painters: List[Tuple[str, str, str]] = []
    filters = []
    for this_raw_path, this_infoname, this_title in zip(raw_paths, info_names, titles):
        for name in _inv_find_subtable_columns(this_raw_path):
            if name in match_by:
                # Filter out duplicate common columns which are used to join tables
                if name in known_common_columns:
                    continue
                known_common_columns.add(name)

            column = this_infoname + "_" + name

            # Declare a painter, sorter and filters for each path with display hint
            _declare_invtable_column(this_infoname, this_raw_path, this_title, name, column)

            painters.append((column, "", ""))
            filters.append(column)

    _declare_views(tablename, title_plural, painters, filters, raw_paths)


def _register_info_class(infoname: str, title_singular: str, title_plural: str) -> None:
    # Declare the "info" (like a database table)
    info_class = type(
        "VisualInfo%s" % infoname.title(),
        (VisualInfo,),
        {
            "_ident": infoname,
            "ident": property(lambda self: self._ident),
            "_title": title_singular,
            "title": property(lambda self: self._title),
            "_title_plural": title_plural,
            "title_plural": property(lambda self: self._title_plural),
            "single_spec": property(lambda self: []),
        },
    )
    visual_info_registry.register(info_class)


def _declare_views(
    infoname: str,
    title_plural: str,
    painters: List[Tuple[str, str, str]],
    filters: List[FilterName],
    raw_paths: List[SDRawPath],
    icon: Optional[Icon] = None,
) -> None:
    is_show_more = True
    if len(raw_paths) == 1:
        is_show_more = NodeDisplayHint.make(
            inventory.InventoryPath.parse(raw_paths[0] or "").path
        ).table_hint.is_show_more

    # Declare two views: one for searching globally. And one
    # for the items of one host.
    view_spec = {
        "datasource": infoname,
        "topic": "inventory",
        "sort_index": 30,
        "public": True,
        "layout": "table",
        "num_columns": 1,
        "browser_reload": 0,
        "column_headers": "pergroup",
        "user_sortable": True,
        "play_sounds": False,
        "force_checkboxes": False,
        "mobile": False,
        "group_painters": [],
        "sorters": [],
        "is_show_more": is_show_more,
    }

    # View for searching for items
    multisite_builtin_views[infoname + "_search"] = {
        # General options
        "title": _("Search %s") % title_plural,
        "description": _("A view for searching in the inventory data for %s") % title_plural,
        "hidden": False,
        "mustsearch": True,
        # Columns
        "painters": [("host", "inv_host", "")] + painters,
        # Filters
        "show_filters": [
            "siteopt",
            "hostregex",
            "hostgroups",
            "opthostgroup",
            "opthost_contactgroup",
            "host_address",
            "host_tags",
            "hostalias",
            "host_favorites",
        ]
        + filters,
        "hide_filters": [],
        "hard_filters": [],
        "hard_filtervars": [],
    }
    multisite_builtin_views[infoname + "_search"].update(view_spec)

    # View for the items of one host
    multisite_builtin_views[infoname + "_of_host"] = {
        # General options
        "title": title_plural,
        "description": _("A view for the %s of one host") % title_plural,
        "hidden": True,
        "mustsearch": False,
        "link_from": {
            "single_infos": ["host"],
            "has_inventory_tree": raw_paths,
        },
        # Columns
        "painters": painters,
        # Filters
        "show_filters": filters,
        "hard_filters": [],
        "hard_filtervars": [],
        "hide_filters": ["host"],
        "icon": icon,
    }
    multisite_builtin_views[infoname + "_of_host"].update(view_spec)


def declare_invtable_views() -> None:
    """Declare views for a couple of embedded tables"""
    declare_invtable_view(
        "invswpac",
        ".software.packages:",
        _("Software package"),
        _("Software packages"),
    )
    declare_invtable_view(
        "invinterface",
        ".networking.interfaces:",
        _("Network interface"),
        _("Network interfaces"),
        "networking",
    )

    declare_invtable_view(
        "invdockerimages",
        ".software.applications.docker.images:",
        _("Docker images"),
        _("Docker images"),
    )
    declare_invtable_view(
        "invdockercontainers",
        ".software.applications.docker.containers:",
        _("Docker containers"),
        _("Docker containers"),
    )

    declare_invtable_view(
        "invother",
        ".hardware.components.others:",
        _("Other entity"),
        _("Other entities"),
    )
    declare_invtable_view(
        "invunknown",
        ".hardware.components.unknowns:",
        _("Unknown entity"),
        _("Unknown entities"),
    )
    declare_invtable_view(
        "invchassis",
        ".hardware.components.chassis:",
        _("Chassis"),
        _("Chassis"),
    )
    declare_invtable_view(
        "invbackplane",
        ".hardware.components.backplanes:",
        _("Backplane"),
        _("Backplanes"),
    )
    declare_invtable_view(
        "invcmksites",
        ".software.applications.check_mk.sites:",
        _("Checkmk site"),
        _("Checkmk sites"),
        "checkmk",
    )
    declare_invtable_view(
        "invcmkversions",
        ".software.applications.check_mk.versions:",
        _("Checkmk version"),
        _("Checkmk versions"),
        "checkmk",
    )
    declare_invtable_view(
        "invcontainer",
        ".hardware.components.containers:",
        _("HW container"),
        _("HW containers"),
    )
    declare_invtable_view(
        "invpsu",
        ".hardware.components.psus:",
        _("Power supply"),
        _("Power supplies"),
    )
    declare_invtable_view(
        "invfan",
        ".hardware.components.fans:",
        _("Fan"),
        _("Fans"),
    )
    declare_invtable_view(
        "invsensor",
        ".hardware.components.sensors:",
        _("Sensor"),
        _("Sensors"),
    )
    declare_invtable_view(
        "invmodule",
        ".hardware.components.modules:",
        _("Module"),
        _("Modules"),
    )
    declare_invtable_view(
        "invstack",
        ".hardware.components.stacks:",
        _("Stack"),
        _("Stacks"),
    )

    declare_invtable_view(
        "invorainstance",
        ".software.applications.oracle.instance:",
        _("Oracle instance"),
        _("Oracle instances"),
    )
    declare_invtable_view(
        "invorarecoveryarea",
        ".software.applications.oracle.recovery_area:",
        _("Oracle recovery area"),
        _("Oracle recovery areas"),
    )
    declare_invtable_view(
        "invoradataguardstats",
        ".software.applications.oracle.dataguard_stats:",
        _("Oracle dataguard statistic"),
        _("Oracle dataguard statistics"),
    )
    declare_invtable_view(
        "invoratablespace",
        ".software.applications.oracle.tablespaces:",
        _("Oracle tablespace"),
        _("Oracle tablespaces"),
    )
    declare_invtable_view(
        "invorasga",
        ".software.applications.oracle.sga:",
        _("Oracle SGA performance"),
        _("Oracle SGA performance"),
    )
    declare_invtable_view(
        "invorapga",
        ".software.applications.oracle.pga:",
        _("Oracle PGA performance"),
        _("Oracle PGA performance"),
    )
    declare_invtable_view(
        "invorasystemparameter",
        ".software.applications.oracle.systemparameter:",
        _("Oracle system parameter"),
        _("Oracle system parameters"),
    )
    declare_invtable_view(
        "invibmmqmanagers",
        ".software.applications.ibm_mq.managers:",
        _("Manager"),
        _("IBM MQ Managers"),
    )
    declare_invtable_view(
        "invibmmqchannels",
        ".software.applications.ibm_mq.channels:",
        _("Channel"),
        _("IBM MQ Channels"),
    )
    declare_invtable_view(
        "invibmmqqueues", ".software.applications.ibm_mq.queues:", _("Queue"), _("IBM MQ Queues")
    )
    declare_invtable_view(
        "invtunnels", ".networking.tunnels:", _("Networking Tunnels"), _("Networking Tunnels")
    )
    declare_invtable_view(
        "invkernelconfig",
        ".software.kernel_config:",
        _("Kernel configuration (sysctl)"),
        _("Kernel configurations (sysctl)"),
    )


# This would also be possible. But we muss a couple of display and filter hints.
# declare_invtable_view("invdisks",       ".hardware.storage.disks:",  _("Hard Disk"),          _("Hard Disks"))

# .
#   .--Views---------------------------------------------------------------.
#   |                    __     ___                                        |
#   |                    \ \   / (_) _____      _____                      |
#   |                     \ \ / /| |/ _ \ \ /\ / / __|                     |
#   |                      \ V / | |  __/\ V  V /\__ \                     |
#   |                       \_/  |_|\___| \_/\_/ |___/                     |
#   |                                                                      |
#   +----------------------------------------------------------------------+
#   |  Special Multisite table views for software, ports, etc.             |
#   '----------------------------------------------------------------------'

# View for Inventory tree of one host
multisite_builtin_views["inv_host"] = {
    # General options
    "datasource": "hosts",
    "topic": "inventory",
    "title": _("Inventory of host"),
    "description": _("The complete hardware- and software inventory of a host"),
    "icon": "inventory",
    "hidebutton": False,
    "public": True,
    "hidden": True,
    "link_from": {
        "single_infos": ["host"],
        "has_inventory_tree": ".",
    },
    # Layout options
    "layout": "dataset",
    "num_columns": 1,
    "browser_reload": 0,
    "column_headers": "pergroup",
    "user_sortable": False,
    "play_sounds": False,
    "force_checkboxes": False,
    "mustsearch": False,
    "mobile": False,
    # Columns
    "group_painters": [],
    "painters": [
        ("host", "host", ""),
        ("inv", None, ""),
    ],
    # Filters
    "hard_filters": [],
    "hard_filtervars": [],
    # Previously (<2.0/1.6??) the hide_filters: ['host, 'site'] were needed to build the URL.
    # Now for creating the URL these filters are obsolete;
    # Side effect: with 'site' in hide_filters the only_sites filter for livestatus is NOT set
    # properly. Thus we removed 'site'.
    "hide_filters": ["host"],
    "show_filters": [],
    "sorters": [],
}

# View with table of all hosts, with some basic information
multisite_builtin_views["inv_hosts_cpu"] = {
    # General options
    "datasource": "hosts",
    "topic": "inventory",
    "sort_index": 10,
    "title": _("CPU inventory of all hosts"),
    "description": _("A list of all hosts with some CPU related inventory data"),
    "public": True,
    "hidden": False,
    "is_show_more": True,
    # Layout options
    "layout": "table",
    "num_columns": 1,
    "browser_reload": 0,
    "column_headers": "pergroup",
    "user_sortable": True,
    "play_sounds": False,
    "force_checkboxes": False,
    "mustsearch": False,
    "mobile": False,
    # Columns
    "group_painters": [],
    "painters": [
        ("host", "inv_host", ""),
        ("inv_software_os_name", None, ""),
        ("inv_hardware_cpu_cpus", None, ""),
        ("inv_hardware_cpu_cores", None, ""),
        ("inv_hardware_cpu_max_speed", None, ""),
        ("perfometer", None, "", "CPU load"),
        ("perfometer", None, "", "CPU utilization"),
    ],
    # Filters
    "hard_filters": ["has_inv"],
    "hard_filtervars": [("is_has_inv", "1")],
    "hide_filters": [],
    "show_filters": [
        "inv_hardware_cpu_cpus",
        "inv_hardware_cpu_cores",
        "inv_hardware_cpu_max_speed",
    ],
    "sorters": [],
}

# View with available and used ethernet ports
multisite_builtin_views["inv_hosts_ports"] = {
    # General options
    "datasource": "hosts",
    "topic": "inventory",
    "sort_index": 20,
    "title": _("Switch port statistics"),
    "description": _(
        "A list of all hosts with statistics about total, used and free networking interfaces"
    ),
    "public": True,
    "hidden": False,
    "is_show_more": False,
    # Layout options
    "layout": "table",
    "num_columns": 1,
    "browser_reload": 0,
    "column_headers": "pergroup",
    "user_sortable": True,
    "play_sounds": False,
    "force_checkboxes": False,
    "mustsearch": False,
    "mobile": False,
    # Columns
    "group_painters": [],
    "painters": [
        ("host", "invinterface_of_host", ""),
        ("inv_hardware_system_product", None, ""),
        ("inv_networking_total_interfaces", None, ""),
        ("inv_networking_total_ethernet_ports", None, ""),
        ("inv_networking_available_ethernet_ports", None, ""),
    ],
    # Filters
    "hard_filters": ["has_inv"],
    "hard_filtervars": [("is_has_inv", "1")],
    "hide_filters": [],
    "show_filters": host_view_filters + [],
    "sorters": [("inv_networking_available_ethernet_ports", True)],
}

# .
#   .--History-------------------------------------------------------------.
#   |                   _   _ _     _                                      |
#   |                  | | | (_)___| |_ ___  _ __ _   _                    |
#   |                  | |_| | / __| __/ _ \| '__| | | |                   |
#   |                  |  _  | \__ \ || (_) | |  | |_| |                   |
#   |                  |_| |_|_|___/\__\___/|_|   \__, |                   |
#   |                                             |___/                    |
#   +----------------------------------------------------------------------+
#   |  Code for history view of inventory                                  |
#   '----------------------------------------------------------------------'


class RowTableInventoryHistory(ABCRowTable):
    def __init__(self) -> None:
        super().__init__(["invhist"], [])
        self._inventory_path = None

    def _get_inv_data(self, hostrow: Row) -> Sequence[inventory.HistoryEntry]:
        hostname: HostName = hostrow["host_name"]
        history, corrupted_history_files = inventory.get_history(hostname)
        if corrupted_history_files:
            user_errors.add(
                MKUserError(
                    "load_inventory_delta_tree",
                    _(
                        "Cannot load HW/SW inventory history entries %s. Please remove the corrupted files."
                    )
                    % ", ".join(sorted(corrupted_history_files)),
                )
            )

        return history

    def _prepare_rows(self, inv_data: Sequence[inventory.HistoryEntry]) -> Iterable[Row]:
        for history_entry in inv_data:
            yield {
                "invhist_time": history_entry.timestamp,
                "invhist_delta": history_entry.delta_tree,
                "invhist_removed": history_entry.removed,
                "invhist_new": history_entry.new,
                "invhist_changed": history_entry.changed,
            }


@data_source_registry.register
class DataSourceInventoryHistory(ABCDataSource):
    @property
    def ident(self) -> str:
        return "invhist"

    @property
    def title(self) -> str:
        return _("Inventory: History")

    @property
    def table(self) -> RowTable:
        return RowTableInventoryHistory()

    @property
    def infos(self) -> SingleInfos:
        return ["host", "invhist"]

    @property
    def keys(self) -> List[ColumnName]:
        return []

    @property
    def id_keys(self) -> List[ColumnName]:
        return ["host_name", "invhist_time"]


@painter_registry.register
class PainterInvhistTime(Painter):
    @property
    def ident(self) -> str:
        return "invhist_time"

    def title(self, cell: Cell) -> str:
        return _("Inventory Date/Time")

    def short_title(self, cell: Cell) -> str:
        return _("Date/Time")

    @property
    def columns(self) -> List[ColumnName]:
        return ["invhist_time"]

    @property
    def painter_options(self) -> List[str]:
        return ["ts_format", "ts_date"]

    def render(self, row: Row, cell: Cell) -> CellSpec:
        return paint_age(row["invhist_time"], True, 60 * 10)


@painter_registry.register
class PainterInvhistDelta(Painter):
    @property
    def ident(self) -> str:
        return "invhist_delta"

    def title(self, cell: Cell) -> str:
        return _("Inventory changes")

    @property
    def columns(self) -> List[ColumnName]:
        return ["invhist_delta", "invhist_time"]

    def render(self, row: Row, cell: Cell) -> CellSpec:
        try:
            _validate_inventory_tree_uniqueness(row)
        except MultipleInventoryTreesError:
            return "", ""

        struct_tree = row.get("invhist_delta")
        if struct_tree is None:
            return "", ""

        assert isinstance(struct_tree, StructuredDataNode)

        tree_renderer = DeltaNodeRenderer(
            row["site"],
            row["host_name"],
            tree_id="/" + str(row["invhist_time"]),
        )

        with output_funnel.plugged():
            struct_tree.show(tree_renderer)
            code = HTML(output_funnel.drain())

        return "invtree", code


def _paint_invhist_count(row: Row, what: str) -> CellSpec:
    number = row["invhist_" + what]
    if number:
        return "narrow number", str(number)
    return "narrow number unused", "0"


@painter_registry.register
class PainterInvhistRemoved(Painter):
    @property
    def ident(self) -> str:
        return "invhist_removed"

    def title(self, cell: Cell) -> str:
        return _("Removed entries")

    def short_title(self, cell: Cell) -> str:
        return _("Removed")

    @property
    def columns(self) -> List[ColumnName]:
        return ["invhist_removed"]

    def render(self, row: Row, cell: Cell) -> CellSpec:
        return _paint_invhist_count(row, "removed")


@painter_registry.register
class PainterInvhistNew(Painter):
    @property
    def ident(self) -> str:
        return "invhist_new"

    def title(self, cell: Cell) -> str:
        return _("New entries")

    def short_title(self, cell: Cell) -> str:
        return _("New")

    @property
    def columns(self) -> List[ColumnName]:
        return ["invhist_new"]

    def render(self, row: Row, cell: Cell) -> CellSpec:
        return _paint_invhist_count(row, "new")


@painter_registry.register
class PainterInvhistChanged(Painter):
    @property
    def ident(self) -> str:
        return "invhist_changed"

    def title(self, cell: Cell):
        return _("Changed entries")

    def short_title(self, cell: Cell) -> str:
        return _("Changed")

    @property
    def columns(self) -> List[ColumnName]:
        return ["invhist_changed"]

    def render(self, row: Row, cell: Cell) -> CellSpec:
        return _paint_invhist_count(row, "changed")


# sorters
declare_1to1_sorter("invhist_time", cmp_simple_number, reverse=True)
declare_1to1_sorter("invhist_removed", cmp_simple_number)
declare_1to1_sorter("invhist_new", cmp_simple_number)
declare_1to1_sorter("invhist_changed", cmp_simple_number)

# View for inventory history of one host

multisite_builtin_views["inv_host_history"] = {
    # General options
    "datasource": "invhist",
    "topic": "inventory",
    "title": _("Inventory history of host"),
    "description": _("The history for changes in hardware- and software inventory of a host"),
    "icon": {
        "icon": "inventory",
        "emblem": "time",
    },
    "hidebutton": False,
    "public": True,
    "hidden": True,
    "is_show_more": True,
    "link_from": {
        "single_infos": ["host"],
        "has_inventory_tree_history": ".",
    },
    # Layout options
    "layout": "table",
    "num_columns": 1,
    "browser_reload": 0,
    "column_headers": "pergroup",
    "user_sortable": True,
    "play_sounds": False,
    "force_checkboxes": False,
    "mustsearch": False,
    "mobile": False,
    # Columns
    "group_painters": [],
    "painters": [
        ("invhist_time", None, ""),
        ("invhist_removed", None, ""),
        ("invhist_new", None, ""),
        ("invhist_changed", None, ""),
        ("invhist_delta", None, ""),
    ],
    # Filters
    "hard_filters": [],
    "hard_filtervars": [],
    "hide_filters": ["host"],
    "show_filters": [],
    "sorters": [("invhist_time", False)],
}

# .
#   .--Node Renderer-------------------------------------------------------.
#   |  _   _           _        ____                _                      |
#   | | \ | | ___   __| | ___  |  _ \ ___ _ __   __| | ___ _ __ ___ _ __   |
#   | |  \| |/ _ \ / _` |/ _ \ | |_) / _ \ '_ \ / _` |/ _ \ '__/ _ \ '__|  |
#   | | |\  | (_) | (_| |  __/ |  _ <  __/ | | | (_| |  __/ | |  __/ |     |
#   | |_| \_|\___/ \__,_|\___| |_| \_\___|_| |_|\__,_|\___|_|  \___|_|     |
#   |                                                                      |
#   '----------------------------------------------------------------------'


# Just for compatibility
def render_inv_dicttable(*args):
    pass


class ABCNodeRenderer(abc.ABC):
    def __init__(
        self,
        site_id: SiteId,
        hostname: HostName,
        tree_id: str = "",
        show_internal_tree_paths: bool = False,
    ) -> None:
        self._site_id = site_id
        self._hostname = hostname
        self._tree_id = tree_id
        self._show_internal_tree_paths = "on" if show_internal_tree_paths else ""

    #   ---node-----------------------------------------------------------------

    def show_node(self, node: StructuredDataNode) -> None:
        node_hint = NodeDisplayHint.make(node.path)

        title = node_hint.title

        # Replace placeholders in title with the real values for this path
        if "%d" in title or "%s" in title:
            title = self._replace_placeholders(title, node_hint.raw_path)

        with foldable_container(
            treename="inv_%s%s" % (self._hostname, self._tree_id),
            id_=node_hint.raw_path,
            isopen=False,
            title=self._get_header(
                title,
                ".".join(map(str, node.path)),
                "#666",
            ),
            icon=node_hint.icon,
            fetch_url=makeuri_contextless(
                request,
                [
                    ("site", self._site_id),
                    ("host", self._hostname),
                    ("path", node_hint.raw_path),
                    ("show_internal_tree_paths", self._show_internal_tree_paths),
                    ("treeid", self._tree_id),
                ],
                "ajax_inv_render_tree.py",
            ),
        ) as is_open:
            if is_open:
                node.show(self)

    def _replace_placeholders(self, raw_title: str, raw_path: SDRawPath) -> str:
        hint_id = _find_display_hint_id(raw_path)
        if hint_id is None:
            return raw_title

        raw_path_parts = raw_path.strip(".").split(".")

        # Use the position of the stars in the path to build a list of texts
        # that should be used for replacing the tile placeholders
        replace_vars = []
        hint_parts = hint_id.strip(".").split(".")
        for index, hint_part in enumerate(hint_parts):
            if hint_part == "*":
                replace_vars.append(raw_path_parts[index])

        # Now replace the variables in the title. Handle the case where we have
        # more stars than macros in the title.
        num_macros = raw_title.count("%d") + raw_title.count("%s")
        return raw_title % tuple(replace_vars[:num_macros])

    #   ---table----------------------------------------------------------------

    def show_table(self, table: Table) -> None:
        table_hint = NodeDisplayHint.make(table.path).table_hint

        # Link to Multisite view with exactly this table
        if table_hint.view_name:
            url = makeuri_contextless(
                request,
                [
                    ("view_name", table_hint.view_name),
                    ("host", self._hostname),
                ],
                filename="view.py",
            )
            html.div(
                HTMLWriter.render_a(_("Open this table for filtering / sorting"), href=url),
                class_="invtablelink",
            )

        titles = table_hint.make_titles(table.rows, table.key_columns, table.path)

        # TODO: Use table.open_table() below.
        html.open_table(class_="data")
        html.open_tr()
        for table_title in titles:
            html.th(
                self._get_header(
                    table_title.title,
                    table_title.key,
                    "#DDD",
                    is_key_column=table_title.is_key_column,
                )
            )
        html.close_tr()

        for entry in table_hint.sort_rows(table.rows, titles):
            html.open_tr(class_="even0")
            for table_title in titles:
                value = entry.get(table_title.key)
                col_hint = ColumnDisplayHint.make(table.path, table_title.key)
                tdclass, _rendered_value = col_hint.paint_function(
                    value[1] if isinstance(value, tuple) else value
                )

                html.open_td(class_=tdclass)
                self._show_row_value(
                    value,
                    col_hint,
                    retention_intervals=table.get_retention_intervals(table_title.key, entry),
                )
                html.close_td()
            html.close_tr()
        html.close_table()

    @abc.abstractmethod
    def _show_row_value(
        self,
        value: Any,
        col_hint: ColumnDisplayHint,
        retention_intervals: Optional[RetentionIntervals] = None,
    ) -> None:
        raise NotImplementedError()

    #   ---attributes-----------------------------------------------------------

    def show_attributes(self, attributes: Attributes) -> None:
        attrs_hint = NodeDisplayHint.make(attributes.path).attributes_hint

        html.open_table()
        for key, value in attrs_hint.sort_pairs(attributes.pairs):
            attr_hint = AttributeDisplayHint.make(attributes.path, key)

            html.open_tr()
            html.th(
                self._get_header(
                    attr_hint.title,
                    key,
                    "#DDD",
                )
            )
            html.open_td()
            self.show_attribute(
                value,
                attr_hint,
                retention_intervals=attributes.get_retention_intervals(key),
            )
            html.close_td()
            html.close_tr()
        html.close_table()

    @abc.abstractmethod
    def show_attribute(
        self,
        value: Any,
        attr_hint: AttributeDisplayHint,
        retention_intervals: Optional[RetentionIntervals] = None,
    ) -> None:
        raise NotImplementedError()

    #   ---helper---------------------------------------------------------------

    def _get_header(
        self,
        title: str,
        key: str,
        hex_color: str,
        *,
        is_key_column: bool = False,
    ) -> HTML:
        header = HTML(title)
        if self._show_internal_tree_paths:
            key_info = "%s*" % key if is_key_column else key
            header += " " + HTMLWriter.render_span("(%s)" % key_info, style="color: %s" % hex_color)
        return header

    def _show_child_value(
        self,
        value: Any,
        hint: Union[ColumnDisplayHint, AttributeDisplayHint],
        retention_intervals: Optional[RetentionIntervals] = None,
    ) -> None:
        if isinstance(value, HTML):
            html.write_html(value)
        else:
            _tdclass, code = hint.paint_function(value)
            html.write_text(code)

        if (ret_value_to_write := self._get_retention_value(retention_intervals)) is not None:
            html.write_html(ret_value_to_write)

    def _get_retention_value(
        self,
        retention_intervals: Optional[RetentionIntervals],
    ) -> Optional[HTML]:
        if retention_intervals is None:
            return None

        now = time.time()

        if now <= retention_intervals.valid_until:
            return None

        if now <= retention_intervals.keep_until:
            _tdclass, value = inv_paint_age(retention_intervals.keep_until - now)
            return HTMLWriter.render_span(_(" (%s left)") % value, style="color: #DDD")

        return HTMLWriter.render_span(_(" (outdated)"), style="color: darkred")


class NodeRenderer(ABCNodeRenderer):
    def _show_row_value(
        self,
        value: Any,
        col_hint: ColumnDisplayHint,
        retention_intervals: Optional[RetentionIntervals] = None,
    ) -> None:
        self._show_child_value(value, col_hint, retention_intervals)

    def show_attribute(
        self,
        value: Any,
        attr_hint: AttributeDisplayHint,
        retention_intervals: Optional[RetentionIntervals] = None,
    ) -> None:
        self._show_child_value(value, attr_hint, retention_intervals)


class DeltaNodeRenderer(ABCNodeRenderer):
    def _show_row_value(
        self,
        value: Any,
        col_hint: ColumnDisplayHint,
        retention_intervals: Optional[RetentionIntervals] = None,
    ) -> None:
        self._show_delta_child_value(value, col_hint)

    def show_attribute(
        self,
        value: Any,
        attr_hint: AttributeDisplayHint,
        retention_intervals: Optional[RetentionIntervals] = None,
    ) -> None:
        self._show_delta_child_value(value, attr_hint)

    def _show_delta_child_value(
        self,
        value: Any,
        hint: Union[ColumnDisplayHint, AttributeDisplayHint],
    ) -> None:
        if value is None:
            value = (None, None)

        old, new = value
        if old is None and new is not None:
            html.open_span(class_="invnew")
            self._show_child_value(new, hint)
            html.close_span()
        elif old is not None and new is None:
            html.open_span(class_="invold")
            self._show_child_value(old, hint)
            html.close_span()
        elif old == new:
            self._show_child_value(old, hint)
        elif old is not None and new is not None:
            html.open_span(class_="invold")
            self._show_child_value(old, hint)
            html.close_span()
            html.write_text(" → ")
            html.open_span(class_="invnew")
            self._show_child_value(new, hint)
            html.close_span()


# Ajax call for fetching parts of the tree
@cmk.gui.pages.register("ajax_inv_render_tree")
def ajax_inv_render_tree() -> None:
    site_id = SiteId(request.get_ascii_input_mandatory("site"))
    hostname = HostName(request.get_ascii_input_mandatory("host"))
    inventory.verify_permission(hostname, site_id)

    raw_path = request.get_ascii_input_mandatory("path")
    tree_id = request.get_ascii_input("treeid", "")
    show_internal_tree_paths = bool(request.var("show_internal_tree_paths"))

    if tree_id:
        struct_tree, corrupted_history_files = inventory.load_delta_tree(hostname, int(tree_id[1:]))
        if corrupted_history_files:
            user_errors.add(
                MKUserError(
                    "load_inventory_delta_tree",
                    _(
                        "Cannot load HW/SW inventory history entries %s. Please remove the corrupted files."
                    )
                    % ", ".join(corrupted_history_files),
                )
            )
            return
        tree_renderer: ABCNodeRenderer = DeltaNodeRenderer(
            site_id,
            hostname,
            tree_id=tree_id,
        )

    else:
        row = inventory.get_status_data_via_livestatus(site_id, hostname)
        try:
            struct_tree = inventory.load_filtered_and_merged_tree(row)
        except inventory.LoadStructuredDataError:
            user_errors.add(
                MKUserError(
                    "load_inventory_tree",
                    _("Cannot load HW/SW inventory tree %s. Please remove the corrupted file.")
                    % inventory.get_short_inventory_filepath(hostname),
                )
            )
            return
        tree_renderer = NodeRenderer(
            site_id,
            hostname,
            show_internal_tree_paths=show_internal_tree_paths,
        )

    if struct_tree is None:
        html.show_error(_("No such inventory tree."))
        return

    inventory_path = inventory.InventoryPath.parse(raw_path or "")
    if (node := struct_tree.get_node(inventory_path.path)) is None:
        html.show_error(
            _("Invalid path in inventory tree: '%s' >> %s") % (raw_path, repr(inventory_path.path))
        )
        return

    node.show(tree_renderer)
