# -*- coding: utf-8 -*-
"""Generate Revit rooms from mass families with flexible parameter mapping.

Places rooms at mass centroids for each selected level, then transfers
user-defined parameters from mass families to the created rooms.
"""

__title__ = "Generate\nRooms"
__doc__ = (
    "Create rooms at mass centroids for selected levels. "
    "Map any mass parameter to any room parameter."
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
    Level,
    XYZ,
    UV,
    Transaction,
)
from pyrevit import revit, forms, script
from pyrevit.forms import WPFWindow

import mass_utils as mu

doc = revit.doc
uidoc = revit.uidoc
output = script.get_output()


# -- Data Model --------------------------------------------------------

class ParamMapping(object):
    """Row in the parameter mapping DataGrid."""
    def __init__(self, mass_param="", room_param=""):
        self._mass_param = mass_param
        self._room_param = room_param

    @property
    def MassParam(self):
        return self._mass_param

    @MassParam.setter
    def MassParam(self, value):
        self._mass_param = value

    @property
    def RoomParam(self):
        return self._room_param

    @RoomParam.setter
    def RoomParam(self, value):
        self._room_param = value


DEFAULT_MAPPINGS = [
    ParamMapping("ROOM NUMBER", "Number"),
    ParamMapping("ROOM NAME", "Name"),
    ParamMapping("DEPARTMENT", "Department"),
]


# -- Helpers -----------------------------------------------------------

def _element_name(element):
    """Safely read an element's Name under IronPython."""
    try:
        from Autodesk.Revit.DB import Element as DBElement
        return DBElement.Name.GetValue(element)
    except Exception:
        pass
    try:
        return element.Name
    except Exception:
        return str(element.Id.IntegerValue)


def get_levels(doc):
    collector = FilteredElementCollector(doc).OfClass(Level)
    return sorted(collector, key=lambda l: l.Elevation)


def get_room_param_names(doc):
    """Return writable parameter names from existing rooms.

    Collects from up to five room instances so the target column
    shows real project parameters. Falls back to common defaults
    if no rooms exist yet.
    """
    collector = (
        FilteredElementCollector(doc)
        .OfCategory(BuiltInCategory.OST_Rooms)
        .WhereElementIsNotElementType()
    )
    rooms = list(collector)
    if rooms:
        names = set()
        for r in rooms[:5]:
            for p in r.Parameters:
                if not p.IsReadOnly:
                    names.add(p.Definition.Name)
        return sorted(names)

    # No rooms yet - return common room parameter names
    return sorted([
        "Number", "Name", "Department", "Comments",
        "Occupancy", "Base Finish", "Ceiling Finish",
        "Wall Finish", "Floor Finish", "Phase",
    ])


def get_mass_centroid_xy(mass, z_override=None):
    solid = mu.get_largest_solid(mass)
    if solid is None:
        return None
    centroid = solid.ComputeCentroid()
    if centroid is None:
        return None
    z = z_override if z_override is not None else centroid.Z
    return XYZ(centroid.X, centroid.Y, z)


def masses_on_level(masses, level, doc):
    """Filter masses whose Level parameter matches, with bbox fallback."""
    result = []
    for m in masses:
        level_param = m.LookupParameter("Level")
        if level_param and level_param.HasValue:
            try:
                param_level_id = level_param.AsElementId()
                if param_level_id == level.Id:
                    result.append(m)
                    continue
            except Exception:
                pass

        bbox = m.get_BoundingBox(None)
        if bbox:
            mass_min_z = bbox.Min.Z
            mass_max_z = bbox.Max.Z
            level_z = level.Elevation
            if mass_min_z - 1.0 <= level_z <= mass_max_z + 1.0:
                result.append(m)

    return result


# -- UI ----------------------------------------------------------------

class GenerateRoomsForm(WPFWindow):
    def __init__(self, levels, mass_param_names, room_param_names, mass_count):
        xaml_path = os.path.join(os.path.dirname(__file__), "ui.xaml")
        WPFWindow.__init__(self, xaml_path)

        self.level_dict = {_element_name(l): l for l in levels}
        self.result = None

        for level in levels:
            self.lb_levels.Items.Add(_element_name(level))
        if self.lb_levels.Items.Count > 0:
            self.lb_levels.SelectedIndex = 0

        clr.AddReference("System")
        from System.Collections.ObjectModel import ObservableCollection

        # Row data
        self.mappings = ObservableCollection[object]()
        for m in DEFAULT_MAPPINGS:
            self.mappings.Add(m)
        self.dg_mapping.ItemsSource = self.mappings

        # Populate column dropdowns
        mass_opts = ObservableCollection[str]()
        for name in mass_param_names:
            mass_opts.Add(name)

        room_opts = ObservableCollection[str]()
        for name in room_param_names:
            room_opts.Add(name)

        # Columns[0] = mass source, Columns[1] = room target
        from System.Windows.Controls import DataGridComboBoxColumn
        self.dg_mapping.Columns[0].ItemsSource = mass_opts
        self.dg_mapping.Columns[1].ItemsSource = room_opts

        self.tb_mass_count.Text = (
            "Mass elements in project: {}".format(mass_count)
        )

    def add_row_click(self, sender, args):
        self.mappings.Add(ParamMapping("", ""))

    def remove_row_click(self, sender, args):
        selected = self.dg_mapping.SelectedItem
        if selected and self.mappings.Count > 0:
            self.mappings.Remove(selected)

    def run_click(self, sender, args):
        selected_levels = []
        for item in self.lb_levels.SelectedItems:
            selected_levels.append(str(item))

        if not selected_levels:
            forms.alert("Please select at least one level.")
            return

        param_map = []
        for mapping in self.mappings:
            mp = mapping.MassParam.strip() if mapping.MassParam else ""
            rp = mapping.RoomParam.strip() if mapping.RoomParam else ""
            if mp and rp:
                param_map.append((mp, rp))

        if not param_map:
            forms.alert("Please define at least one parameter mapping.")
            return

        self.result = {
            "level_names": selected_levels,
            "param_mappings": param_map,
        }
        self.Close()

    def cancel_click(self, sender, args):
        self.result = None
        self.Close()


# -- Main Logic --------------------------------------------------------

def main():
    all_masses = mu.get_all_masses(doc)
    if not all_masses:
        forms.alert(
            "No mass elements found in the project.", exitscript=True
        )

    levels = get_levels(doc)
    if not levels:
        forms.alert("No levels found in the project.", exitscript=True)

    # Collect parameter names for dropdown columns
    mass_param_names = set()
    for m in all_masses[:10]:
        mass_param_names.update(mu.get_element_parameter_names(m))
    mass_param_names = sorted(mass_param_names)

    room_param_names = get_room_param_names(doc)

    form = GenerateRoomsForm(levels, mass_param_names, room_param_names, len(all_masses))
    form.ShowDialog()

    if not form.result:
        return

    cfg = form.result
    level_map = {_element_name(l): l for l in levels}

    rooms_created = 0
    params_set = 0
    skipped = []

    with revit.Transaction("Generate_Rooms"):
        for level_name in cfg["level_names"]:
            level = level_map.get(level_name)
            if level is None:
                skipped.append("Level '{}' not found".format(level_name))
                continue

            level_masses = masses_on_level(all_masses, level, doc)
            if not level_masses:
                skipped.append(
                    "No masses found on level '{}'".format(level_name)
                )
                continue

            for mass in level_masses:
                mass_name = (
                    mass.Name
                    or "Element {}".format(mass.Id.IntegerValue)
                )

                point = get_mass_centroid_xy(mass, level.Elevation)
                if point is None:
                    skipped.append(
                        "{} on {}: no centroid".format(mass_name, level_name)
                    )
                    continue

                try:
                    room = doc.Create.NewRoom(
                        level, UV(point.X, point.Y)
                    )
                except Exception as e:
                    skipped.append(
                        "{} on {}: room creation failed ({})".format(
                            mass_name, level_name, str(e)
                        )
                    )
                    continue

                if room is None:
                    skipped.append(
                        "{} on {}: room is None (point outside walls?)".format(
                            mass_name, level_name
                        )
                    )
                    continue

                rooms_created += 1

                for mass_param, room_param in cfg["param_mappings"]:
                    value = mu.get_param_value(mass, mass_param)
                    if value is not None:
                        success = mu.set_param_value(
                            room, room_param, value
                        )
                        if success:
                            params_set += 1
                        else:
                            skipped.append(
                                "{}: could not set '{}' = '{}'".format(
                                    mass_name, room_param, value
                                )
                            )

    summary = (
        "Created {} room(s) across {} level(s).\n"
        "Set {} parameter value(s)."
    ).format(rooms_created, len(cfg["level_names"]), params_set)

    if skipped:
        summary += "\n\nNotes ({}):\n{}".format(
            len(skipped),
            "\n".join("  - " + s for s in skipped[:30]),
        )
    forms.alert(summary, title="Generate Rooms")


if __name__ == "__main__":
    main()
