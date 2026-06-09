# -*- coding: utf-8 -*-
"""Resolve overlapping walls detected by Revit warnings.

For collinear pairs: deletes both walls and creates a single merged
wall spanning the full combined extent, copying constraints and wall
type from the longer original.

For non-collinear pairs (walls sharing area but not on the same line):
falls back to deleting the shorter wall and reports the case.
"""

__title__ = "Delete\nOverlapping Walls"
__doc__ = (
    "Resolve overlapping wall pairs from Revit warnings. "
    "Collinear pairs are merged into one wall; non-collinear "
    "pairs fall back to deleting the shorter wall."
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
    BuiltInParameter,
    Wall,
    Line,
    XYZ,
    Transaction,
)
from pyrevit import revit, forms, script

doc = revit.doc
output = script.get_output()

# Collinearity tolerances
_ANGLE_TOL = 1e-4   # max deviation from |dot product| == 1
_DIST_TOL  = 0.05   # max perpendicular distance between lines (feet, ~15mm)


# -- Geometry Helpers --------------------------------------------------

def get_wall_curve(wall):
    """Return the LocationCurve's Line for a wall, or None."""
    try:
        loc = wall.Location
        if loc and hasattr(loc, "Curve"):
            return loc.Curve
    except Exception:
        pass
    return None


def get_wall_length(wall):
    """Return wall length in feet."""
    try:
        param = wall.get_Parameter(BuiltInParameter.CURVE_ELEM_LENGTH)
        if param and param.HasValue:
            return param.AsDouble()
    except Exception:
        pass
    try:
        c = get_wall_curve(wall)
        if c:
            return c.Length
    except Exception:
        pass
    return 0.0


def get_collinear_axis(curve0, curve1):
    """Check whether two lines are collinear.

    Returns (ref_point, direction_XYZ) if collinear, else None.

    Two lines are collinear when:
      1. Their direction vectors are parallel (|dot| ~ 1).
      2. Any point on one line is within _DIST_TOL of the other line.
    """
    p0 = curve0.GetEndPoint(0)
    p1 = curve0.GetEndPoint(1)
    q0 = curve1.GetEndPoint(0)

    # Flatten to XY - walls are vertical so Z should match, but small
    # floating-point differences from Revit's geometry engine are common.
    # We compare direction and co-linearity in XY only.
    d0_raw = XYZ(p1.X - p0.X, p1.Y - p0.Y, 0.0)
    d1_raw = XYZ(
        curve1.GetEndPoint(1).X - q0.X,
        curve1.GetEndPoint(1).Y - q0.Y,
        0.0,
    )

    len0 = d0_raw.GetLength()
    len1 = d1_raw.GetLength()
    if len0 < 1e-9 or len1 < 1e-9:
        return None

    d0 = d0_raw.Multiply(1.0 / len0)  # normalize
    d1 = d1_raw.Multiply(1.0 / len1)

    # 1. Parallel check
    if abs(abs(d0.DotProduct(d1)) - 1.0) > _ANGLE_TOL:
        return None

    # 2. Co-linear check: perpendicular distance from q0 to line(p0, d0)
    pq = XYZ(q0.X - p0.X, q0.Y - p0.Y, 0.0)
    t = pq.DotProduct(d0)
    closest = XYZ(p0.X + d0.X * t, p0.Y + d0.Y * t, 0.0)
    perp_dist = XYZ(q0.X - closest.X, q0.Y - closest.Y, 0.0).GetLength()

    if perp_dist > _DIST_TOL:
        return None

    return (p0, d0)


def merge_wall_curves(curve0, curve1):
    """Return a Line spanning the full combined extent of two collinear lines.

    Projects all four endpoints onto the shared axis and returns a new
    Line from the minimum to the maximum projected parameter.
    Returns None if the lines are not collinear.
    """
    axis = get_collinear_axis(curve0, curve1)
    if axis is None:
        return None

    ref_pt, direction = axis

    # Use the Z of the first curve's start point (both walls on same level)
    z = curve0.GetEndPoint(0).Z

    endpoints = [
        curve0.GetEndPoint(0),
        curve0.GetEndPoint(1),
        curve1.GetEndPoint(0),
        curve1.GetEndPoint(1),
    ]

    t_values = [
        XYZ(pt.X - ref_pt.X, pt.Y - ref_pt.Y, 0.0).DotProduct(direction)
        for pt in endpoints
    ]

    t_min = min(t_values)
    t_max = max(t_values)

    if abs(t_max - t_min) < 1e-6:
        return None  # degenerate

    new_start = XYZ(ref_pt.X + direction.X * t_min,
                    ref_pt.Y + direction.Y * t_min, z)
    new_end   = XYZ(ref_pt.X + direction.X * t_max,
                    ref_pt.Y + direction.Y * t_max, z)

    try:
        return Line.CreateBound(new_start, new_end)
    except Exception:
        return None


# -- Wall Helpers ------------------------------------------------------

def copy_wall_constraints(source_wall, new_wall):
    """Copy level constraints and offsets from source_wall to new_wall."""
    for bip in (
        BuiltInParameter.WALL_BASE_CONSTRAINT,
        BuiltInParameter.WALL_HEIGHT_TYPE,
        BuiltInParameter.WALL_BASE_OFFSET,
        BuiltInParameter.WALL_TOP_OFFSET,
    ):
        try:
            src_param = source_wall.get_Parameter(bip)
            dst_param = new_wall.get_Parameter(bip)
            if src_param and dst_param and not dst_param.IsReadOnly:
                dst_param.Set(src_param.AsDouble()
                              if src_param.StorageType.ToString() == "Double"
                              else src_param.AsElementId())
        except Exception:
            pass


# -- Warning Detection -------------------------------------------------

def find_overlapping_wall_pairs(doc):
    """Return list of (wall0, wall1) tuples from overlap warnings."""
    warnings = doc.GetWarnings()
    pairs = []
    seen = set()

    for warning in warnings:
        desc = warning.GetDescriptionText().lower()
        if "overlap" not in desc or "wall" not in desc:
            continue

        ids = list(warning.GetFailingElements())
        if len(ids) < 2:
            continue

        id0, id1 = ids[0], ids[1]
        key = (min(id0.IntegerValue, id1.IntegerValue),
               max(id0.IntegerValue, id1.IntegerValue))
        if key in seen:
            continue
        seen.add(key)

        e0 = doc.GetElement(id0)
        e1 = doc.GetElement(id1)
        if e0 is not None and e1 is not None:
            pairs.append((e0, e1))

    return pairs


# -- Main --------------------------------------------------------------

def main():
    pairs = find_overlapping_wall_pairs(doc)

    if not pairs:
        forms.alert(
            "No overlapping wall warnings found.\n\n"
            "The model is clean, or overlapping walls may have "
            "been suppressed in the Warnings dialog.",
            title="Delete Overlapping Walls",
        )
        return

    # Classify pairs for preview
    preview_lines = []
    for i, (w0, w1) in enumerate(pairs):
        c0 = get_wall_curve(w0)
        c1 = get_wall_curve(w1)
        len0 = get_wall_length(w0)
        len1 = get_wall_length(w1)

        if c0 and c1 and get_collinear_axis(c0, c1) is not None:
            action = "merge into one wall"
        else:
            shorter_id = w0.Id.IntegerValue if len0 <= len1 else w1.Id.IntegerValue
            action = "delete shorter (Wall {}, non-collinear fallback)".format(shorter_id)

        preview_lines.append(
            "Pair {}: Wall {} ({:.2f} ft) + Wall {} ({:.2f} ft) -> {}".format(
                i + 1,
                w0.Id.IntegerValue, len0,
                w1.Id.IntegerValue, len1,
                action,
            )
        )

    preview_text = "Found {} overlapping wall pair(s):\n\n{}".format(
        len(pairs), "\n".join(preview_lines[:20])
    )
    if len(preview_lines) > 20:
        preview_text += "\n... and {} more.".format(len(preview_lines) - 20)
    preview_text += "\n\nProceed?"

    if not forms.alert(preview_text, yes=True, no=True,
                       title="Delete Overlapping Walls"):
        return

    # Process
    merged_count   = 0
    deleted_count  = 0
    fallback_count = 0
    errors         = []

    with revit.Transaction("Resolve_Overlapping_Walls"):
        for w0, w1 in pairs:
            c0 = get_wall_curve(w0)
            c1 = get_wall_curve(w1)

            # Determine longer wall for type/constraint reference
            longer = w0 if get_wall_length(w0) >= get_wall_length(w1) else w1

            merged_curve = merge_wall_curves(c0, c1) if (c0 and c1) else None

            if merged_curve is not None:
                # --- Merge path ---
                try:
                    new_wall = Wall.Create(
                        doc,
                        merged_curve,
                        longer.WallType.Id,
                        longer.get_Parameter(
                            BuiltInParameter.WALL_BASE_CONSTRAINT
                        ).AsElementId(),
                        10.0,   # temporary height, overridden below
                        0.0,
                        False,
                        False,
                    )
                    copy_wall_constraints(longer, new_wall)
                    doc.Delete(w0.Id)
                    doc.Delete(w1.Id)
                    merged_count += 1
                except Exception as e:
                    errors.append(
                        "Merge Wall {} + {}: {}".format(
                            w0.Id.IntegerValue, w1.Id.IntegerValue, str(e)
                        )
                    )
            else:
                # --- Fallback: delete shorter ---
                try:
                    to_delete = (
                        w0 if get_wall_length(w0) <= get_wall_length(w1) else w1
                    )
                    doc.Delete(to_delete.Id)
                    deleted_count += 1
                    fallback_count += 1
                except Exception as e:
                    errors.append(
                        "Delete Wall {}: {}".format(
                            w0.Id.IntegerValue, str(e)
                        )
                    )

    # Report
    parts = []
    if merged_count:
        parts.append("Merged {} pair(s) into single walls.".format(merged_count))
    if deleted_count:
        parts.append(
            "Deleted shorter wall in {} non-collinear pair(s).".format(deleted_count)
        )
    summary = "\n".join(parts) if parts else "No walls were modified."

    if errors:
        summary += "\n\nErrors ({}):\n{}".format(
            len(errors), "\n".join("  - " + e for e in errors[:20])
        )

    remaining = find_overlapping_wall_pairs(doc)
    if remaining:
        summary += (
            "\n\n{} pair(s) still overlap. Run again to resolve them."
        ).format(len(remaining))
    else:
        summary += "\n\nAll overlapping walls have been resolved."

    forms.alert(summary, title="Delete Overlapping Walls")


if __name__ == "__main__":
    main()
