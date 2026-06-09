# -*- coding: utf-8 -*-
"""Shared utilities for Mass_To_Room pyRevit extension.

Provides geometry helpers, parameter validation, and common
element-collection routines used by all four pushbuttons.
"""

import clr
clr.AddReference("RevitAPI")

from Autodesk.Revit.DB import (
    FilteredElementCollector,
    BuiltInCategory,
    Options,
    GeometryInstance,
    Solid,
    XYZ,
    Line,
    CurveLoop,
)
import math


# ── Element Collection ───────────────────────────────────────────

def get_masses_in_view(doc, view):
    """Collect all Mass instances visible in the given view."""
    collector = (
        FilteredElementCollector(doc, view.Id)
        .OfCategory(BuiltInCategory.OST_Mass)
        .WhereElementIsNotElementType()
    )
    return list(collector)


def get_all_masses(doc):
    """Collect every Mass instance in the document."""
    collector = (
        FilteredElementCollector(doc)
        .OfCategory(BuiltInCategory.OST_Mass)
        .WhereElementIsNotElementType()
    )
    return list(collector)


def get_detail_curves_in_view(doc, view):
    """Collect all detail curves (OST_Lines) in the given view."""
    collector = (
        FilteredElementCollector(doc, view.Id)
        .OfCategory(BuiltInCategory.OST_Lines)
        .WhereElementIsNotElementType()
    )
    return list(collector)


# ── Geometry Helpers ─────────────────────────────────────────────

def get_solids(element, detail_level=None):
    """Extract all non-trivial Solid objects from a Revit element.

    Args:
        element: A Revit Element.
        detail_level: ViewDetailLevel enum value (default Fine).

    Returns:
        List of Solid objects with Volume > 0.
    """
    from Autodesk.Revit.DB import ViewDetailLevel

    opts = Options()
    opts.ComputeReferences = True
    opts.DetailLevel = detail_level or ViewDetailLevel.Fine

    solids = []
    geo = element.get_Geometry(opts)
    if geo is None:
        return solids

    for geo_obj in geo:
        if isinstance(geo_obj, Solid) and geo_obj.Volume > 0:
            solids.append(geo_obj)
        elif isinstance(geo_obj, GeometryInstance):
            for inst_obj in geo_obj.GetInstanceGeometry():
                if isinstance(inst_obj, Solid) and inst_obj.Volume > 0:
                    solids.append(inst_obj)
    return solids


def get_largest_solid(element):
    """Return the Solid with the greatest volume, or None."""
    solids = get_solids(element)
    if not solids:
        return None
    return max(solids, key=lambda s: s.Volume)


def get_bottom_face(solid):
    """Find the face whose outward normal points most downward (-Z).

    This replaces the fragile face-index-0 assumption by checking
    each face's average normal and selecting the one closest to
    (0, 0, -1). Works for masses with voids, chamfers, or
    non-standard topology.

    Returns:
        The bottom PlanarFace, or None.
    """
    best_face = None
    best_dot = 1.0  # start above any valid dot with -Z

    for face in solid.Faces:
        try:
            uv = face.Evaluate(
                face.GetBoundingBox().Min.Add(
                    face.GetBoundingBox().Max.Subtract(
                        face.GetBoundingBox().Min
                    ).Multiply(0.5)
                )
            )
            normal = face.ComputeNormal(uv)
        except Exception:
            # Fallback: evaluate at midpoint of UV domain
            try:
                bbox = face.GetBoundingBox()
                mid_u = (bbox.Min.U + bbox.Max.U) / 2.0
                mid_v = (bbox.Min.V + bbox.Max.V) / 2.0
                from Autodesk.Revit.DB import UV
                normal = face.ComputeNormal(UV(mid_u, mid_v))
            except Exception:
                continue

        # Dot product with -Z axis: the more negative Z the normal,
        # the larger the dot product with (0, 0, -1).
        dot = normal.Z  # dot with (0,0,-1) == -normal.Z
        if dot < best_dot:
            best_dot = dot
            best_face = face

    return best_face


def face_edges_to_curves(face):
    """Extract ordered curves from a face's edge loops.

    Returns the outer loop curves (the first CurveLoop).
    """
    loops = face.GetEdgesAsCurveLoops()
    if not loops:
        return []
    # The first CurveLoop is typically the outer boundary
    outer_loop = loops[0]
    return [c for c in outer_loop]


def offset_curve_loop(curves, offset_distance, z_elevation=None):
    """Offset a closed curve loop by the given signed distance.

    Args:
        curves: List of Revit Curve objects forming a closed loop.
        offset_distance: Positive = inward, negative = outward.
            CurveLoop.CreateViaOffset uses the loop's winding direction
            and the supplied normal to determine side. For a CCW loop
            with Z-up normal, a positive value offsets inward.
        z_elevation: If set, flatten all curves to this Z.

    Returns:
        List of offset Curve objects, or original if offset fails.
    """
    try:
        loop = CurveLoop.Create(list(curves))
        offset_loop = CurveLoop.CreateViaOffset(
            loop, offset_distance, XYZ(0, 0, 1)
        )
        result = [c for c in offset_loop]
        if not result:
            return list(curves)
        return result
    except Exception:
        return list(curves)


def flatten_curves_to_z(curves, z_value):
    """Project all curve endpoints onto the given Z elevation.

    Returns new Line objects at the target Z for straight segments.
    Arcs are approximated as chords.
    """
    flat = []
    for c in curves:
        p0 = c.GetEndPoint(0)
        p1 = c.GetEndPoint(1)
        new_p0 = XYZ(p0.X, p0.Y, z_value)
        new_p1 = XYZ(p1.X, p1.Y, z_value)
        if new_p0.DistanceTo(new_p1) > 1e-6:
            flat.append(Line.CreateBound(new_p0, new_p1))
    return flat


# ── Parameter Helpers ────────────────────────────────────────────

def get_param_value(element, param_name):
    """Read a parameter value by name, checking instance then type.

    Returns the value as a Python object, or None if not found.
    """
    # Try instance parameter first
    param = element.LookupParameter(param_name)
    if param and param.HasValue:
        return _extract_param_value(param)

    # Fall back to type parameter
    elem_type = element.Document.GetElement(element.GetTypeId())
    if elem_type:
        param = elem_type.LookupParameter(param_name)
        if param and param.HasValue:
            return _extract_param_value(param)

    return None


def _extract_param_value(param):
    """Convert a Revit Parameter to a Python value.

    ElementId parameters (e.g. Level, Phase) are resolved to the
    referenced element's Name so filter dropdowns show readable
    text and comparisons stay consistent.
    """
    from Autodesk.Revit.DB import StorageType, ElementId

    st = param.StorageType
    if st == StorageType.String:
        return param.AsString()
    elif st == StorageType.Double:
        return param.AsDouble()
    elif st == StorageType.Integer:
        return param.AsInteger()
    elif st == StorageType.ElementId:
        eid = param.AsElementId()
        if eid == ElementId.InvalidElementId:
            return None
        try:
            ref_elem = param.Element.Document.GetElement(eid)
            if ref_elem is not None:
                return ref_elem.Name
        except Exception:
            pass
        # Fallback: raw integer so it is at least string-able
        return eid.IntegerValue
    return None


def set_param_value(element, param_name, value):
    """Set a parameter value by name. Returns True on success."""
    param = element.LookupParameter(param_name)
    if param is None or param.IsReadOnly:
        return False
    try:
        param.Set(value)
        return True
    except Exception:
        return False


def validate_params(element, param_names):
    """Check that an element has all listed parameters with values.

    Returns:
        (valid: bool, missing: list of str)
    """
    missing = []
    for name in param_names:
        val = get_param_value(element, name)
        if val is None:
            missing.append(name)
    return (len(missing) == 0, missing)


def get_unique_param_values(elements, param_name):
    """Collect the set of unique non-None values for a parameter."""
    values = set()
    for el in elements:
        v = get_param_value(el, param_name)
        if v is not None:
            values.add(str(v))
    return sorted(values)


def get_element_parameter_names(element):
    """List all parameter names on an element (instance + type)."""
    names = set()
    for p in element.Parameters:
        names.add(p.Definition.Name)
    elem_type = element.Document.GetElement(element.GetTypeId())
    if elem_type:
        for p in elem_type.Parameters:
            names.add(p.Definition.Name)
    return sorted(names)
