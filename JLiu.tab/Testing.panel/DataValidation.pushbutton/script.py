# -*- coding: utf-8 -*-
# Excel Comparison Tool - PyRevit Pushbutton
#
# Folder structure:
#   ExcelCompare.pushbutton/
#   ├── script.py
#   └── ui.xaml
#
# No third-party libraries required.

import os
import sys
import zipfile
import xml.etree.ElementTree as ET
from io import BytesIO

from pyrevit import forms
from pyrevit.forms import WPFWindow

import clr
clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("WindowsBase")

from System.Windows import Visibility, FontWeights
from System.Windows.Controls import (
    ComboBox, TextBox, StackPanel, GridViewColumn
)
from System.Windows.Data import Binding
from System.Windows.Media import SolidColorBrush
from System.Windows.Media import Color as WpfColor
from System.Windows import MessageBox, MessageBoxButton, MessageBoxImage
from System.Windows import Thickness


# ===========================================================================
# Colors
# ===========================================================================

def _brush(r, g, b):
    return SolidColorBrush(WpfColor.FromRgb(r, g, b))

XL_GREEN  = "C6EFCE"
XL_RED    = "FFC7CE"
XL_AMBER  = "FFEB9C"
XL_HDR_BG = "4472C4"
XL_HDR_FG = "FFFFFF"


# ===========================================================================
# Stdlib xlsx reader
# ===========================================================================

NS_SS  = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

def _tag(ns, local):
    return "{%s}%s" % (ns, local)

def _xl_col_index(col_str):
    result = 0
    for ch in col_str.upper():
        result = result * 26 + (ord(ch) - ord("A") + 1)
    return result - 1

def _parse_cell_ref(ref):
    col = ""; row = ""
    for ch in ref:
        if ch.isalpha(): col += ch
        else: row += ch
    return col, int(row)

def _parse_ref(ref):
    parts = ref.split(":")
    c0, r0 = _parse_cell_ref(parts[0])
    if len(parts) == 1:
        return _xl_col_index(c0), r0-1, _xl_col_index(c0), r0-1
    c1, r1 = _parse_cell_ref(parts[1])
    return _xl_col_index(c0), r0-1, _xl_col_index(c1), r1-1


class XlWorkbook(object):
    def __init__(self, filepath):
        self._zf = zipfile.ZipFile(filepath, "r")
        self._shared = self._load_shared()
        self._sheet_paths = self._load_sheet_paths()
        self._cache = {}
        self.sheet_names = list(self._sheet_paths.keys())

    def _load_shared(self):
        try:
            root = ET.fromstring(self._zf.read("xl/sharedStrings.xml"))
            result = []
            for si in root.findall(_tag(NS_SS, "si")):
                texts = si.findall(".//" + _tag(NS_SS, "t"))
                result.append("".join(t.text or "" for t in texts))
            return result
        except KeyError:
            return []

    def _load_sheet_paths(self):
        wb_xml = ET.fromstring(self._zf.read("xl/workbook.xml"))
        sheets_el = wb_xml.find(_tag(NS_SS, "sheets"))
        rid_to_name = {}
        for s in (sheets_el or []):
            rid = s.get(_tag(NS_REL, "id"))
            rid_to_name[rid] = s.get("name", "")
        try:
            rels = ET.fromstring(self._zf.read("xl/_rels/workbook.xml.rels"))
        except KeyError:
            return {}
        result = {}
        for rel in rels:
            rid = rel.get("Id")
            if rid in rid_to_name:
                target = rel.get("Target", "")
                if not target.startswith("xl/"):
                    target = "xl/" + target
                result[rid_to_name[rid]] = target
        return result

    def sheet(self, name):
        if name not in self._cache:
            path = self._sheet_paths[name]
            raw  = self._zf.read(path)
            fname = path.split("/")[-1]
            rels_path = path.replace(
                "worksheets/" + fname,
                "worksheets/_rels/" + fname + ".rels"
            )
            table_refs = []
            try:
                for rel in ET.fromstring(self._zf.read(rels_path)):
                    if rel.get("Type", "").endswith("/table"):
                        tgt = rel.get("Target", "")
                        if not tgt.startswith("xl/"):
                            tgt = "xl/" + tgt.lstrip("../")
                        try:
                            tbl = ET.fromstring(self._zf.read(tgt))
                            tname = tbl.get("displayName") or tbl.get("name", "")
                            tref  = tbl.get("ref", "")
                            if tname and tref:
                                table_refs.append((tname, tref))
                        except Exception:
                            pass
            except KeyError:
                pass
            self._cache[name] = XlSheet(raw, self._shared, table_refs)
        return self._cache[name]

    def close(self):
        self._zf.close()


class XlSheet(object):
    def __init__(self, raw_xml, shared, table_refs):
        self._shared = shared
        self.tables = {n: r for n, r in table_refs}
        self._cells = {}
        root = ET.fromstring(raw_xml)
        sd = root.find(_tag(NS_SS, "sheetData"))
        if sd is None:
            return
        for row_el in sd.findall(_tag(NS_SS, "row")):
            for c_el in row_el.findall(_tag(NS_SS, "c")):
                ref = c_el.get("r", "")
                if not ref:
                    continue
                col_str, row_int = _parse_cell_ref(ref)
                ci = _xl_col_index(col_str)
                ri = row_int - 1
                t    = c_el.get("t", "")
                v_el = c_el.find(_tag(NS_SS, "v"))
                is_el = c_el.find(_tag(NS_SS, "is"))
                if is_el is not None:
                    texts = is_el.findall(".//" + _tag(NS_SS, "t"))
                    val = "".join(te.text or "" for te in texts)
                elif v_el is not None and v_el.text is not None:
                    if t == "s":
                        try:    val = self._shared[int(v_el.text)]
                        except: val = v_el.text
                    elif t == "b":
                        val = v_el.text == "1"
                    else:
                        try:    val = int(v_el.text)
                        except:
                            try: val = float(v_el.text)
                            except: val = v_el.text
                else:
                    val = None
                self._cells[(ri, ci)] = val

    def rows_in_range(self, ref):
        c0, r0, c1, r1 = _parse_ref(ref)
        return [[self._cells.get((ri, ci)) for ci in range(c0, c1+1)]
                for ri in range(r0, r1+1)]

    def all_rows(self):
        if not self._cells:
            return []
        max_r = max(r for r, c in self._cells) + 1
        max_c = max(c for r, c in self._cells) + 1
        return [[self._cells.get((ri, ci)) for ci in range(max_c)]
                for ri in range(max_r)]


def load_workbook_tables(filepath):
    wb = XlWorkbook(filepath)
    tables = {}
    for name in wb.sheet_names:
        ws = wb.sheet(name)
        for tbl_name, tbl_ref in ws.tables.items():
            tables[tbl_name] = (name, tbl_ref)
    return wb, tables


def table_to_rows(wb, sheet_name, ref):
    data = wb.sheet(sheet_name).rows_in_range(ref)
    if not data:
        return [], []
    headers = [str(h) if h is not None else "" for h in data[0]]
    rows = [{headers[i]: data[r][i] for i in range(len(headers))}
            for r in range(1, len(data))]
    return headers, rows


def sheet_to_rows(wb, sheet_name):
    data = wb.sheet(sheet_name).all_rows()
    if not data:
        return [], []
    headers = [str(h) if h is not None else "" for h in data[0]]
    rows = []
    for raw in data[1:]:
        rows.append({headers[i]: (raw[i] if i < len(raw) else None)
                     for i in range(len(headers))})
    return headers, rows


def check_key_uniqueness(rows, key_col):
    seen = {}
    for r in rows:
        v = str(r.get(key_col)) if r.get(key_col) is not None else "__NONE__"
        seen[v] = seen.get(v, 0) + 1
    return [k for k, cnt in seen.items() if cnt > 1]


def compare_rows(rows_a, rows_b, key_a, key_b, col_pairs):
    index_a = {str(r.get(key_a, "")): r for r in rows_a}
    index_b = {str(r.get(key_b, "")): r for r in rows_b}
    all_keys = list(dict.fromkeys(list(index_a.keys()) + list(index_b.keys())))
    results = []
    for key in all_keys:
        in_a = key in index_a
        in_b = key in index_b
        row_a = index_a.get(key, {})
        row_b = index_b.get(key, {})
        source = "Both" if (in_a and in_b) else ("File_A Only" if in_a else "File_B Only")
        rec = {"_key": key, "_source": source, "_cells": {}}
        for col_a, col_b, label in col_pairs:
            val_a = row_a.get(col_a) if in_a else None
            val_b = row_b.get(col_b) if in_b else None
            str_a = str(val_a) if val_a is not None else ""
            str_b = str(val_b) if val_b is not None else ""
            if source != "Both":    status = "missing"
            elif str_a == str_b:    status = "match"
            else:                   status = "differ"
            rec["_cells"][label] = (str_a, str_b, status)
        results.append(rec)
    return results


# ===========================================================================
# xlsx export - stdlib only
# ===========================================================================

def _col_letter(n):
    result = ""
    while n:
        n, r = divmod(n - 1, 26)
        result = chr(65 + r) + result
    return result


def export_to_excel(results, col_pairs, key_label, filepath):
    headers = [key_label, "Source"]
    for _, _, label in col_pairs:
        headers.extend([label + " (A)", label + " (B)"])
    num_cols = len(headers)

    all_rows = []
    for rec in results:
        cells = rec["_cells"]
        vals = [rec["_key"], rec["_source"]]
        statuses = []
        for _, _, label in col_pairs:
            val_a, val_b, status = cells.get(label, ("", "", "missing"))
            vals.extend([val_a, val_b])
            statuses.append(status)
        is_missing = rec["_source"] != "Both"
        all_match  = (not is_missing) and all(s == "match" for s in statuses)
        fills = []
        for _ in range(2):
            fills.append(XL_AMBER if is_missing else (XL_GREEN if all_match else None))
        for status in statuses:
            f = (XL_AMBER if is_missing else
                 XL_GREEN if status == "match" else
                 XL_RED   if status == "differ" else None)
            fills.extend([f, f])
        all_rows.append((vals, fills))

    STYLE_IDX = {XL_HDR_BG: 1, XL_GREEN: 2, XL_RED: 3, XL_AMBER: 4, None: 5}

    def styles_xml():
        ns = NS_SS
        def tag(n): return "{%s}%s" % (ns, n)
        root = ET.Element(tag("styleSheet"))
        fonts = ET.SubElement(root, tag("fonts"), count="2")
        f0 = ET.SubElement(fonts, tag("font"))
        ET.SubElement(f0, tag("sz"), val="10")
        ET.SubElement(f0, tag("name"), val="Calibri")
        f1 = ET.SubElement(fonts, tag("font"))
        ET.SubElement(f1, tag("sz"), val="10")
        ET.SubElement(f1, tag("name"), val="Calibri")
        ET.SubElement(f1, tag("b"))
        ET.SubElement(f1, tag("color"), rgb="FF" + XL_HDR_FG)
        fills = ET.SubElement(root, tag("fills"), count="7")
        for pt in ("none", "gray125"):
            fi = ET.SubElement(fills, tag("fill"))
            ET.SubElement(fi, tag("patternFill"), patternType=pt)
        for hx in (XL_HDR_BG, XL_GREEN, XL_RED, XL_AMBER, "FFFFFF"):
            fi = ET.SubElement(fills, tag("fill"))
            pf = ET.SubElement(fi, tag("patternFill"), patternType="solid")
            ET.SubElement(pf, tag("fgColor"), rgb="FF" + hx)
        borders = ET.SubElement(root, tag("borders"), count="1")
        ET.SubElement(borders, tag("border"))
        csx = ET.SubElement(root, tag("cellStyleXfs"), count="1")
        ET.SubElement(csx, tag("xf"), numFmtId="0", fontId="0", fillId="0", borderId="0")
        cxfs = ET.SubElement(root, tag("cellXfs"), count="6")
        ET.SubElement(cxfs, tag("xf"), numFmtId="0", fontId="0", fillId="0", borderId="0", xfId="0")
        ET.SubElement(cxfs, tag("xf"), numFmtId="0", fontId="1", fillId="2", borderId="0", xfId="0", applyFont="1", applyFill="1")
        ET.SubElement(cxfs, tag("xf"), numFmtId="0", fontId="0", fillId="3", borderId="0", xfId="0", applyFill="1")
        ET.SubElement(cxfs, tag("xf"), numFmtId="0", fontId="0", fillId="4", borderId="0", xfId="0", applyFill="1")
        ET.SubElement(cxfs, tag("xf"), numFmtId="0", fontId="0", fillId="5", borderId="0", xfId="0", applyFill="1")
        ET.SubElement(cxfs, tag("xf"), numFmtId="0", fontId="0", fillId="6", borderId="0", xfId="0", applyFill="1")
        css = ET.SubElement(root, tag("cellStyles"), count="1")
        ET.SubElement(css, tag("cellStyle"), name="Normal", xfId="0", builtinId="0")
        body = ET.tostring(root)
        if isinstance(body, bytes):
            body = body.decode("utf-8")
        return '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' + body

    def sheet_xml():
        ns = NS_SS
        def tag(n): return "{%s}%s" % (ns, n)
        root = ET.Element(tag("worksheet"))
        cols_el = ET.SubElement(root, tag("cols"))
        for ci in range(1, num_cols + 1):
            ET.SubElement(cols_el, tag("col"),
                          min=str(ci), max=str(ci), width="18", customWidth="1")
        sd = ET.SubElement(root, tag("sheetData"))
        def add_row(ri, values, style_indices):
            row_el = ET.SubElement(sd, tag("row"), r=str(ri))
            for ci, (val, si) in enumerate(zip(values, style_indices), 1):
                ref = _col_letter(ci) + str(ri)
                c_el = ET.SubElement(row_el, tag("c"), r=ref, s=str(si), t="inlineStr")
                is_el = ET.SubElement(c_el, tag("is"))
                t_el  = ET.SubElement(is_el, tag("t"))
                t_el.text = str(val) if val is not None else ""
        add_row(1, headers, [1] * num_cols)
        for ri, (vals, fills) in enumerate(all_rows, 2):
            add_row(ri, vals, [STYLE_IDX.get(f, 5) for f in fills])
        body = ET.tostring(root)
        if isinstance(body, bytes):
            body = body.decode("utf-8")
        return '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' + body

    rels_root = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="xl/workbook.xml"/></Relationships>'
    )
    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="Comparison" sheetId="1" r:id="rId1"/></sheets>'
        '</workbook>'
    )
    wb_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        'Target="worksheets/sheet1.xml"/>'
        '<Relationship Id="rId2" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
        'Target="styles.xml"/></Relationships>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '<Override PartName="/xl/styles.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        '</Types>'
    )
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml",        content_types)
        zf.writestr("_rels/.rels",                rels_root)
        zf.writestr("xl/workbook.xml",            workbook_xml)
        zf.writestr("xl/_rels/workbook.xml.rels", wb_rels)
        zf.writestr("xl/styles.xml",              styles_xml())
        zf.writestr("xl/worksheets/sheet1.xml",   sheet_xml())
    with open(filepath, "wb") as f:
        f.write(buf.getvalue())


# ===========================================================================
# Result row - data object for DataGrid binding
# ===========================================================================

ROW_COLORS = {
    "match":   "#C6EFCE",
    "differ":  "#FFC7CE",
    "missing": "#FFEB9C",
}

class ResultRow(object):
    def __init__(self, row_status, cell_vals):
        self.RowStatus = row_status
        self.RowColor  = ROW_COLORS.get(row_status, "#FFFFFF")
        for k, v in cell_vals.items():
            setattr(self, k, str(v) if v is not None else "")


# ===========================================================================
# Mapping row - one row in the column mapping panel
# ===========================================================================

class MappingRowUI(object):
    def __init__(self, panel, index, headers_a, headers_b):
        self._panel = panel

        container = StackPanel()
        container.Margin = Thickness(0, 2, 0, 2)
        from System.Windows.Controls import Grid as WGrid, ColumnDefinition as CD
        from System.Windows import GridLength as GL, GridUnitType

        g = WGrid()
        for w in (28, 220, 220, 0):
            cd = CD()
            cd.Width = GL(w) if w else GL(1, GridUnitType.Star)
            g.ColumnDefinitions.Add(cd)

        from System.Windows.Controls import TextBlock as TB
        num = TB()
        num.Text = str(index + 1)
        num.Foreground = _brush(160, 160, 160)
        num.FontSize = 11
        from System.Windows import VerticalAlignment
        num.VerticalAlignment = VerticalAlignment.Center
        WGrid.SetColumn(num, 0)
        g.Children.Add(num)

        self.combo_a = ComboBox()
        self.combo_a.Margin = Thickness(0, 0, 8, 0)
        for h in ["(skip)"] + list(headers_a):
            self.combo_a.Items.Add(h)
        self.combo_a.SelectedIndex = 0
        WGrid.SetColumn(self.combo_a, 1)
        g.Children.Add(self.combo_a)

        self.combo_b = ComboBox()
        self.combo_b.Margin = Thickness(0, 0, 8, 0)
        for h in ["(skip)"] + list(headers_b):
            self.combo_b.Items.Add(h)
        self.combo_b.SelectedIndex = 0
        WGrid.SetColumn(self.combo_b, 2)
        g.Children.Add(self.combo_b)

        self.txt_label = TextBox()
        self.txt_label.Margin = Thickness(0, 0, 0, 0)
        WGrid.SetColumn(self.txt_label, 3)
        g.Children.Add(self.txt_label)

        container.Children.Add(g)
        self._container = container
        panel.Children.Add(container)

    def set_auto(self, auto_a, headers_a, auto_b, headers_b):
        if auto_a and auto_a in headers_a:
            self.combo_a.SelectedIndex = list(headers_a).index(auto_a) + 1
        if auto_b and auto_b in headers_b:
            self.combo_b.SelectedIndex = list(headers_b).index(auto_b) + 1
        self.txt_label.Text = auto_a or ""

    def get_pair(self):
        a = str(self.combo_a.SelectedItem) if self.combo_a.SelectedItem else "(skip)"
        b = str(self.combo_b.SelectedItem) if self.combo_b.SelectedItem else "(skip)"
        if a == "(skip)" or b == "(skip)":
            return None
        label = self.txt_label.Text.strip() or "{} / {}".format(a, b)
        return (a, b, label)

    def remove(self):
        self._panel.Children.Remove(self._container)


# ===========================================================================
# Main window
# ===========================================================================

class CompareWindow(WPFWindow):

    STEP_FILES   = 0
    STEP_KEYS    = 1
    STEP_MAPPING = 2
    STEP_RESULTS = 3

    def __init__(self):
        xaml_path = os.path.join(os.path.dirname(__file__), "ui.xaml")
        WPFWindow.__init__(self, xaml_path)

        # State
        self.path_a    = None
        self.path_b    = None
        self.wb_a      = None
        self.wb_b      = None
        self.tables_a  = {}
        self.tables_b  = {}
        self.headers_a = []
        self.headers_b = []
        self.rows_a    = []
        self.rows_b    = []
        self.key_a     = None
        self.key_b     = None
        self.col_pairs = []
        self.results   = []
        self._mapping_rows = []

        self._panels = [
            self.PanelFiles,
            self.PanelKeys,
            self.PanelMapping,
            self.PanelResults,
        ]
        self._step_labels = [
            self.StepLabel0,
            self.StepLabel1,
            self.StepLabel2,
            self.StepLabel3,
        ]

        self._show_step(self.STEP_FILES)

    # ------------------------------------------------------------------
    # Step navigation
    # ------------------------------------------------------------------

    def _show_step(self, step):
        self._current_step = step
        for i, p in enumerate(self._panels):
            p.Visibility = Visibility.Visible if i == step else Visibility.Collapsed
        for i, lbl in enumerate(self._step_labels):
            lbl.Foreground = (_brush(255, 255, 255) if i == step
                              else _brush(130, 130, 130))
        self.BtnBack.Visibility    = Visibility.Visible if step > 0 else Visibility.Collapsed
        self.BtnNext.Visibility    = Visibility.Visible if step < 2 else Visibility.Collapsed
        self.BtnCompare.Visibility = Visibility.Visible if step == 2 else Visibility.Collapsed
        self.BtnExport.Visibility  = Visibility.Visible if step == 3 else Visibility.Collapsed
        self.BtnClose.Visibility   = Visibility.Visible if step == 3 else Visibility.Collapsed
        self.LblStatus.Text = ""

    def BtnBack_Click(self, s, e):
        if self._current_step > 0:
            self._show_step(self._current_step - 1)

    def BtnClose_Click(self, s, e):
        self.Close()

    # ------------------------------------------------------------------
    # Step 1 - Load Files
    # ------------------------------------------------------------------

    def BtnBrowseA_Click(self, s, e):
        p = self._browse_xlsx()
        if p:
            self.path_a = p
            self.LblPathA.Text = os.path.basename(p)
            self.LblPathA.Foreground = _brush(30, 30, 30)

    def BtnBrowseB_Click(self, s, e):
        p = self._browse_xlsx()
        if p:
            self.path_b = p
            self.LblPathB.Text = os.path.basename(p)
            self.LblPathB.Foreground = _brush(30, 30, 30)

    def _browse_xlsx(self):
        clr.AddReference("System.Windows.Forms")
        from System.Windows.Forms import OpenFileDialog, DialogResult
        dlg = OpenFileDialog()
        dlg.Filter = "Excel Files (*.xlsx)|*.xlsx"
        dlg.Title = "Select Excel File"
        return dlg.FileName if dlg.ShowDialog() == DialogResult.OK else None

    # ------------------------------------------------------------------
    # Step 2 - Tables + Keys
    # ------------------------------------------------------------------

    def _populate_tbl_combo(self, combo, tables, wb):
        combo.Items.Clear()
        for name in tables.keys():
            combo.Items.Add(name)
        for name in wb.sheet_names:
            combo.Items.Add("[Sheet] " + name)
        if combo.Items.Count > 0:
            combo.SelectedIndex = 0

    def _load_selection(self, combo, tables, wb):
        sel = str(combo.SelectedItem) if combo.SelectedItem else ""
        if sel.startswith("[Sheet] "):
            return sheet_to_rows(wb, sel[len("[Sheet] "):])
        elif sel in tables:
            sheet_name, ref = tables[sel]
            return table_to_rows(wb, sheet_name, ref)
        return [], []

    def _fill_key_combo(self, combo, headers):
        combo.Items.Clear()
        for h in headers:
            combo.Items.Add(h)
        if combo.Items.Count > 0:
            combo.SelectedIndex = 0

    def ComboTblA_Changed(self, s, e):
        if self.wb_a:
            h, r = self._load_selection(self.ComboTblA, self.tables_a, self.wb_a)
            self.headers_a = h; self.rows_a = r
            self._fill_key_combo(self.ComboKeyA, h)

    def ComboTblB_Changed(self, s, e):
        if self.wb_b:
            h, r = self._load_selection(self.ComboTblB, self.tables_b, self.wb_b)
            self.headers_b = h; self.rows_b = r
            self._fill_key_combo(self.ComboKeyB, h)

    # ------------------------------------------------------------------
    # Step 3 - Column Mapping
    # ------------------------------------------------------------------

    def _populate_mapping(self):
        self.MappingPanel.Children.Clear()
        self._mapping_rows = []
        set_b = set(self.headers_b)
        matched = [(h, h) for h in self.headers_a if h in set_b]
        if matched:
            for a, b in matched:
                mr = MappingRowUI(self.MappingPanel, len(self._mapping_rows),
                                  self.headers_a, self.headers_b)
                mr.set_auto(a, self.headers_a, b, self.headers_b)
                self._mapping_rows.append(mr)
        else:
            self._mapping_rows.append(
                MappingRowUI(self.MappingPanel, 0, self.headers_a, self.headers_b))

    def BtnAddRow_Click(self, s, e):
        mr = MappingRowUI(self.MappingPanel, len(self._mapping_rows),
                          self.headers_a, self.headers_b)
        self._mapping_rows.append(mr)

    def BtnRemoveRow_Click(self, s, e):
        if self._mapping_rows:
            self._mapping_rows.pop().remove()

    def _collect_pairs(self):
        pairs = []; seen = set()
        for mr in self._mapping_rows:
            pair = mr.get_pair()
            if pair is None:
                continue
            _, _, label = pair
            if label in seen:
                self._warn("Duplicate output label '{}'. Each label must be unique.".format(label))
                return None
            seen.add(label)
            pairs.append(pair)
        return pairs

    # ------------------------------------------------------------------
    # Step 4 - Results
    # ------------------------------------------------------------------

    def _populate_results(self):
        self.GridViewA.Columns.Clear()
        self.GridA.Items.Clear()
        self.GridViewB.Columns.Clear()
        self.GridB.Items.Clear()
        self._syncing = False

        key_label = "{} / {}".format(self.key_a, self.key_b)
        self.LblGridA.Text = os.path.basename(self.path_a)
        self.LblGridB.Text = os.path.basename(self.path_b)

        def safe(name):
            out = ""
            for ch in name:
                out += ch if ch.isalnum() or ch == "_" else "_"
            return out if out and out[0].isalpha() else "C_" + out

        def add_col(grid_view, header, attr):
            col = GridViewColumn()
            col.Header = header
            col.DisplayMemberBinding = Binding(attr)
            col.Width = 120
            grid_view.Columns.Add(col)

        key_attr = safe(key_label)
        self._key_attr = key_attr

        # GridView A: key + A-side values
        add_col(self.GridViewA, key_label, key_attr)
        for _, _, label in self.col_pairs:
            add_col(self.GridViewA, label, safe(label + "_A"))

        # GridView B: key + B-side values
        add_col(self.GridViewB, key_label, key_attr)
        for _, _, label in self.col_pairs:
            add_col(self.GridViewB, label, safe(label + "_B"))

        total = matches = differs = one_only = 0

        for rec in self.results:
            total += 1
            cells = rec["_cells"]
            statuses = []
            vals_a = {key_attr: str(rec["_key"])}
            vals_b = {key_attr: str(rec["_key"])}

            for _, _, label in self.col_pairs:
                val_a, val_b, status = cells.get(label, ("", "", "missing"))
                statuses.append(status)
                vals_a[safe(label + "_A")] = val_a
                vals_b[safe(label + "_B")] = val_b

            if rec["_source"] != "Both":
                row_status = "missing"; one_only += 1
            elif all(s == "match" for s in statuses):
                row_status = "match"; matches += 1
            else:
                row_status = "differ"; differs += 1

            self.GridA.Items.Add(ResultRow(row_status, vals_a))
            self.GridB.Items.Add(ResultRow(row_status, vals_b))

        self.LblSummary.Text = (
            "{} rows -- {} match, {} differ, {} orphaned".format(
                total, matches, differs, one_only))

    def GridA_SelectionChanged(self, s, e):
        self._sync_grids(self.GridA, self.GridB)

    def GridB_SelectionChanged(self, s, e):
        self._sync_grids(self.GridB, self.GridA)

    def _sync_grids(self, source, target):
        if getattr(self, "_syncing", False):
            return
        item = source.SelectedItem
        if item is None:
            return
        key = getattr(item, self._key_attr, None)
        if key is None:
            return
        # Search target by key value so sync works after sorting
        target_item = None
        for i in range(target.Items.Count):
            candidate = target.Items[i]
            if getattr(candidate, self._key_attr, None) == key:
                target_item = candidate
                break
        if target_item is None:
            return
        self._syncing = True
        try:
            target.SelectedItem = target_item
            target.ScrollIntoView(target_item)
        finally:
            self._syncing = False

    # ------------------------------------------------------------------
    # Next / Compare / Export
    # ------------------------------------------------------------------

    def BtnNext_Click(self, s, e):
        step = self._current_step

        if step == self.STEP_FILES:
            if not self.path_a or not self.path_b:
                self._warn("Please select both files."); return
            try:
                self.wb_a, self.tables_a = load_workbook_tables(self.path_a)
                self.wb_b, self.tables_b = load_workbook_tables(self.path_b)
            except Exception as ex:
                self._err("Failed to open workbook:\n" + str(ex)); return
            self.LblFileA.Text = os.path.basename(self.path_a)
            self.LblFileB.Text = os.path.basename(self.path_b)
            self._populate_tbl_combo(self.ComboTblA, self.tables_a, self.wb_a)
            self._populate_tbl_combo(self.ComboTblB, self.tables_b, self.wb_b)
            self.ComboTblA_Changed(None, None)
            self.ComboTblB_Changed(None, None)
            self._show_step(self.STEP_KEYS)

        elif step == self.STEP_KEYS:
            self.key_a = str(self.ComboKeyA.SelectedItem) if self.ComboKeyA.SelectedItem else None
            self.key_b = str(self.ComboKeyB.SelectedItem) if self.ComboKeyB.SelectedItem else None
            if not self.key_a or not self.key_b:
                self._warn("Please select key columns for both files."); return
            for key_col, rows, label in [(self.key_a, self.rows_a, "File A"),
                                         (self.key_b, self.rows_b, "File B")]:
                dupes = check_key_uniqueness(rows, key_col)
                if dupes:
                    self._err("{} key '{}' has duplicate values:\n{}".format(
                        label, key_col, ", ".join(dupes[:10]))); return
            self._populate_mapping()
            self._show_step(self.STEP_MAPPING)

    def BtnCompare_Click(self, s, e):
        pairs = self._collect_pairs()
        if pairs is None: return
        if not pairs:
            self._warn("Add at least one column pair to compare."); return
        self.col_pairs = pairs
        self.results = compare_rows(
            self.rows_a, self.rows_b,
            self.key_a, self.key_b,
            self.col_pairs
        )
        self._populate_results()
        self._show_step(self.STEP_RESULTS)

    def BtnExport_Click(self, s, e):
        clr.AddReference("System.Windows.Forms")
        from System.Windows.Forms import SaveFileDialog, DialogResult
        dlg = SaveFileDialog()
        dlg.Filter = "Excel Files (*.xlsx)|*.xlsx"
        dlg.Title = "Export Comparison Results"
        dlg.FileName = "Comparison_Results.xlsx"
        if dlg.ShowDialog() != DialogResult.OK:
            return
        try:
            export_to_excel(self.results, self.col_pairs,
                            "{} / {}".format(self.key_a, self.key_b), dlg.FileName)
            MessageBox.Show("Export complete:\n" + dlg.FileName,
                            "Export", MessageBoxButton.OK, MessageBoxImage.Information)
        except Exception as ex:
            self._err("Export failed:\n" + str(ex))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _warn(self, msg):
        MessageBox.Show(msg, "Warning", MessageBoxButton.OK, MessageBoxImage.Warning)

    def _err(self, msg):
        MessageBox.Show(msg, "Error", MessageBoxButton.OK, MessageBoxImage.Error)


# ===========================================================================
# Entry point
# ===========================================================================

win = CompareWindow()
win.ShowDialog()