# -*- coding: utf-8 -*-
"""Convert detail curves in the active view into Revit walls.

Collects all detail lines, creates walls from their geometry,
sets base/top level constraints and offsets, then optionally
deletes the source detail curves.
"""

__title__ = "Curve to\nWall"
__doc__ = (
    "Select wall type, levels, and offsets. Creates walls from "
    "detail curves in the active view."
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
    FilteredElementCollector,
    BuiltInCategory,
    BuiltInParameter,
    Wall,
    WallType,
    Level,
    Line,
    XYZ,
    Transaction,
    ElementId,
)
from pyrevit import revit, forms, script
from pyrevit.forms import WPFWindow

import mass_utils as mu

doc = revit.doc
uidoc = revit.uidoc
output = script.get_output()


# -- Helpers -----------------------------------------------------------

def _element_name(element):
    """Safely read an element's Name under IronPython.

    IronPython can throw MissingMemberException when accessing .Name
    directly on some .NET element types. Using the explicit .NET
    property getter via Element.Name.GetValue() is reliable across
    all Revit element types.
    """
    try:
        from Autodesk.Revit.DB import Element as DBElement
        return DBElement.Name.GetValue(element)
    except Exception:
        pass
    try:
        return element.Name
    except Exception:
        pass
    try:
        p = element.get_Parameter(
            BuiltInParameter.ALL_MODEL_TYPE_NAME
        )
        if p:
            return p.AsString()
    except Exception:
        pass
    return str(element.Id.IntegerValue)


def get_wall_types(doc):
    """Return dict of {name: WallType element}."""
    collector = FilteredElementCollector(doc).OfClass(WallType)
    result = {}
    for wt in collector:
        name = _element_name(wt)
        if name:
            result[name] = wt
    return result


def get_levels(doc):
    """Return dict of {name: Level element}, sorted by elevation."""
    collector = FilteredElementCollector(doc).OfClass(Level)
    levels = sorted(collector, key=lambda l: l.Elevation)
    result = {}
    for l in levels:
        name = _element_name(l)
        if name:
            result[name] = l
    return result


def extract_curve(detail_curve_element):
    """Get the geometric Curve from a detail curve element."""
    try:
        return detail_curve_element.GeometryCurve
    except Exception:
        pass
    try:
        loc = detail_curve_element.Location
        if loc:
            return loc.Curve
    except Exception:
        return None


# -- UI ----------------------------------------------------------------

class CurveToWallForm(WPFWindow):
    def __init__(self, wall_types, levels, curve_count):
        xaml_path = os.path.join(os.path.dirname(__file__), "ui.xaml")
        WPFWindow.__init__(self, xaml_path)

        self.wall_type_dict = wall_types
        self.level_dict = levels
        self.result = None

        for name in sorted(wall_types.keys()):
            self.cb_wall_type.Items.Add(name)
        if self.cb_wall_type.Items.Count > 0:
            self.cb_wall_type.SelectedIndex = 0

        level_names = list(levels.keys())
        for name in level_names:
            self.cb_base_level.Items.Add(name)
            self.cb_top_level.Items.Add(name)

        if level_names:
            self.cb_base_level.SelectedIndex = 0
            self.cb_top_level.SelectedIndex = (
                1 if len(level_names) > 1 else 0
            )

        self.tb_curve_count.Text = (
            "Detail curves found in active view: {}".format(curve_count)
        )

    def run_click(self, sender, args):
        if self.cb_wall_type.SelectedItem is None:
            forms.alert("Please select a wall type.")
            return
        if self.cb_base_level.SelectedItem is None:
            forms.alert("Please select a base level.")
            return
        if self.cb_top_level.SelectedItem is None:
            forms.alert("Please select a top level.")
            return

        try:
            base_offset = float(self.tb_base_offset.Text.strip())
        except ValueError:
            forms.alert("Base offset must be a number (in feet).")
            return
        try:
            top_offset = float(self.tb_top_offset.Text.strip())
        except ValueError:
            forms.alert("Top offset must be a number (in feet).")
            return

        self.result = {
            "wall_type_name": str(self.cb_wall_type.SelectedItem),
            "base_level_name": str(self.cb_base_level.SelectedItem),
            "top_level_name": str(self.cb_top_level.SelectedItem),
            "base_offset": base_offset,
            "top_offset": top_offset,
            "delete_curves": bool(self.chk_delete_curves.IsChecked),
        }
        self.Close()

    def cancel_click(self, sender, args):
        self.result = None
        self.Close()


# -- Main Logic --------------------------------------------------------

def main():
    active_view = doc.ActiveView

    detail_curves = mu.get_detail_curves_in_view(doc, active_view)
    if not detail_curves:
        forms.alert(
            "No detail curves (OST_Lines) found in the active view.",
            exitscript=True,
        )

    wall_types = get_wall_types(doc)
    levels = get_levels(doc)

    if not wall_types:
        forms.alert("No wall types found in the project.", exitscript=True)
    if not levels:
        forms.alert("No levels found in the project.", exitscript=True)

    form = CurveToWallForm(wall_types, levels, len(detail_curves))
    form.ShowDialog()

    if not form.result:
        return

    cfg = form.result
    wall_type = wall_types[cfg["wall_type_name"]]
    base_level = levels[cfg["base_level_name"]]
    top_level = levels[cfg["top_level_name"]]

    # Phase 1: Extract geometry curves
    geometry_curves = []
    source_elements = []
    for dc in detail_curves:
        curve = extract_curve(dc)
        if curve is not None:
            geometry_curves.append(curve)
            source_elements.append(dc)

    if not geometry_curves:
        forms.alert("Could not extract geometry from detail curves.")
        return

    # Phase 2: Create walls
    created_walls = []
    errors = []

    with revit.Transaction("Curve_To_Wall_Create"):
        for i, curve in enumerate(geometry_curves):
            try:
                wall = Wall.Create(
                    doc,
                    curve,
                    wall_type.Id,
                    base_level.Id,
                    10.0,  # temporary height, overridden below
                    0.0,
                    False,
                    False,
                )
                created_walls.append(wall)
            except Exception as e:
                errors.append("Curve {}: {}".format(i + 1, str(e)))

    # Phase 3: Set constraints and offsets
    with revit.Transaction("Curve_To_Wall_Constraints"):
        for wall in created_walls:
            try:
                wall.get_Parameter(
                    BuiltInParameter.WALL_BASE_CONSTRAINT
                ).Set(base_level.Id)
            except Exception:
                pass
            try:
                wall.get_Parameter(
                    BuiltInParameter.WALL_HEIGHT_TYPE
                ).Set(top_level.Id)
            except Exception:
                pass
            try:
                wall.get_Parameter(
                    BuiltInParameter.WALL_BASE_OFFSET
                ).Set(cfg["base_offset"])
            except Exception:
                pass
            try:
                wall.get_Parameter(
                    BuiltInParameter.WALL_TOP_OFFSET
                ).Set(cfg["top_offset"])
            except Exception:
                pass

    # Phase 4: Delete source detail curves if requested
    deleted_count = 0
    if cfg["delete_curves"]:
        with revit.Transaction("Curve_To_Wall_Cleanup"):
            for dc in source_elements:
                try:
                    doc.Delete(dc.Id)
                    deleted_count += 1
                except Exception:
                    pass

    # Report
    summary = "Created {} wall(s) from {} curve(s).".format(
        len(created_walls), len(geometry_curves)
    )
    if cfg["delete_curves"]:
        summary += "\nDeleted {} source detail curve(s).".format(deleted_count)
    if errors:
        summary += "\n\nErrors ({}):\n{}".format(
            len(errors), "\n".join("  - " + e for e in errors[:20])
        )
    forms.alert(summary, title="Curve to Wall")


if __name__ == "__main__":
    main()
