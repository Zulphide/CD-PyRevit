# -*- coding: utf-8 -*-
"""Extract mass family boundaries and create offset detail curves.

Collects mass elements visible in the active view, extracts the bottom
face using a robust normal-vector check, offsets by half the wall
thickness parameter, and draws detail curves in the current view.
"""

__title__ = "Mass to\nDetail Curves"
__doc__ = (
    "Extract mass boundaries from the active view, offset by half "
    "wall thickness, and create detail curves."
)

import os
import sys
import clr

# Add panel-level lib to sys.path
_panel_dir = os.path.dirname(os.path.dirname(__file__))
_lib_dir = os.path.join(_panel_dir, "lib")
if _lib_dir not in sys.path:
    sys.path.insert(0, _lib_dir)

clr.AddReference("RevitAPI")
clr.AddReference("RevitAPIUI")

from Autodesk.Revit.DB import (
    XYZ,
    ViewDetailLevel,
    Transaction,
)
from pyrevit import revit, forms, script
from pyrevit.forms import WPFWindow

import mass_utils as mu

doc = revit.doc
uidoc = revit.uidoc
output = script.get_output()


# -- UI ----------------------------------------------------------------

class MassToDetailCurvesForm(WPFWindow):
    def __init__(self, masses, param_names):
        xaml_path = os.path.join(os.path.dirname(__file__), "ui.xaml")
        WPFWindow.__init__(self, xaml_path)

        self.masses = masses
        self.result = None

        for name in param_names:
            self.cb_thickness_param.Items.Add(name)
            self.cb_filter_param.Items.Add(name)

        if "ROOM WALL THICKNESS" in param_names:
            self.cb_thickness_param.Text = "ROOM WALL THICKNESS"

        if "DEPARTMENT" in param_names:
            self.cb_filter_param.SelectedItem = "DEPARTMENT"
        elif self.cb_filter_param.Items.Count > 0:
            self.cb_filter_param.SelectedIndex = 0

    def filter_toggled(self, sender, args):
        self.filter_panel.IsEnabled = self.chk_use_filter.IsChecked
        if self.chk_use_filter.IsChecked:
            self._refresh_filter_values()

    def filter_param_changed(self, sender, args):
        if self.chk_use_filter.IsChecked:
            self._refresh_filter_values()

    def refresh_filter_click(self, sender, args):
        self._refresh_filter_values()

    def _refresh_filter_values(self):
        param_name = self.cb_filter_param.SelectedItem
        if not param_name:
            return
        param_name = str(param_name).strip()
        self.cb_filter_value.Items.Clear()
        values = mu.get_unique_param_values(self.masses, param_name)
        for v in values:
            self.cb_filter_value.Items.Add(v)
        if values:
            self.cb_filter_value.SelectedIndex = 0

    def run_click(self, sender, args):
        thickness_param = self.cb_thickness_param.Text.strip()
        if not thickness_param:
            forms.alert("Please specify the wall thickness parameter name.")
            return

        try:
            tolerance = float(self.tb_tolerance.Text.strip())
        except ValueError:
            forms.alert("Join tolerance must be a number.")
            return

        self.result = {
            "thickness_param": thickness_param,
            "tolerance": tolerance,
            "offset_inward": bool(self.chk_offset_inward.IsChecked),
            "use_filter": bool(self.chk_use_filter.IsChecked),
            "filter_param": str(self.cb_filter_param.SelectedItem or "").strip(),
            "filter_value": self.cb_filter_value.Text.strip(),
        }
        self.Close()

    def cancel_click(self, sender, args):
        self.result = None
        self.Close()


# -- Main Logic --------------------------------------------------------

def main():
    active_view = doc.ActiveView

    masses = mu.get_masses_in_view(doc, active_view)
    if not masses:
        forms.alert("No mass elements found in the active view.",
                    exitscript=True)

    param_names = set()
    for m in masses[:10]:
        param_names.update(mu.get_element_parameter_names(m))
    param_names = sorted(param_names)

    form = MassToDetailCurvesForm(masses, param_names)
    form.ShowDialog()

    if not form.result:
        return

    cfg = form.result

    # Apply parameter filter if enabled
    if cfg["use_filter"] and cfg["filter_param"] and cfg["filter_value"]:
        filtered = [
            m for m in masses
            if str(mu.get_param_value(m, cfg["filter_param"])) == cfg["filter_value"]
        ]
        if not filtered:
            forms.alert(
                "No masses match the filter '{} = {}'.".format(
                    cfg["filter_param"], cfg["filter_value"]
                )
            )
            return
        masses = filtered

    created_count = 0
    skipped = []

    with revit.Transaction("Mass_To_Detail_Curves"):
        for mass in masses:
            mass_name = mass.Name or "Element {}".format(mass.Id.IntegerValue)

            thickness = mu.get_param_value(mass, cfg["thickness_param"])
            if thickness is None or not isinstance(thickness, (int, float)):
                skipped.append("{}: missing or non-numeric '{}'".format(
                    mass_name, cfg["thickness_param"]))
                continue

            if thickness <= 0:
                skipped.append("{}: '{}' is zero or negative".format(
                    mass_name, cfg["thickness_param"]))
                continue

            offset_dist = thickness / 2.0

            solid = mu.get_largest_solid(mass)
            if solid is None:
                skipped.append("{}: no solid geometry found".format(mass_name))
                continue

            bottom_face = mu.get_bottom_face(solid)
            if bottom_face is None:
                skipped.append("{}: no bottom face found".format(mass_name))
                continue

            curves = mu.face_edges_to_curves(bottom_face)
            if not curves:
                skipped.append("{}: no edge curves on bottom face".format(mass_name))
                continue

            if cfg["offset_inward"]:
                curves = mu.offset_curve_loop(curves, offset_dist)
            else:
                curves = mu.offset_curve_loop(curves, -offset_dist)

            for curve in curves:
                try:
                    doc.Create.NewDetailCurve(active_view, curve)
                    created_count += 1
                except Exception as e:
                    skipped.append("{}: failed to create detail curve ({})".format(
                        mass_name, str(e)))

    summary = "Created {} detail curve(s) from {} mass(es).".format(
        created_count, len(masses)
    )
    if skipped:
        summary += "\n\nSkipped ({}):\n{}".format(
            len(skipped), "\n".join("  - " + s for s in skipped)
        )
    forms.alert(summary, title="Mass to Detail Curves")


if __name__ == "__main__":
    main()
