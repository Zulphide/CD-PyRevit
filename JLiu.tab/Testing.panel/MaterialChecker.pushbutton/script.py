
# -*- coding: utf-8 -*-
"""Collects all Appearance Assets in the current Revit model
and displays their associated file paths in a review window."""

__title__ = "List\nAppearance Assets"
__author__ = "James Liu"

from pyrevit import revit, DB, forms, script

output = script.get_output()

doc = revit.doc

# ── Collect all AppearanceAssetElement instances ──────────────────────────────
collector = DB.FilteredElementCollector(doc)\
               .OfClass(DB.AppearanceAssetElement)\
               .ToElements()

if not collector:
    forms.alert("No appearance assets found in the current model.", exitscript=True)

# ── Build results ─────────────────────────────────────────────────────────────
results = []  # list of dicts: {name, schema, param_name, path}

TEXTURE_PARAMS = [
    # Common texture/bitmap parameter names used across Revit asset schemas
    "unifiedbitmap_Bitmap",
    "bumpmap_Bitmap",
    "generic_diffuse",
    "generic_bump_map",
    "generic_cutout_opacity",
    "generic_reflectivity_map",
    "generic_transparency_map",
    "generic_self_illum_map",
    "hardwood_color",
    "hardwood_application_type",
    "stone_color",
    "ceramic_color",
    "concrete_color",
    "masonry_color",
    "metallic_paint_base_color",
    "wallpaint_color",
    "glazing_transmittance_map",
    "prism_layered_bottom_color",
    "prism_layered_top_color",
]

def get_asset_paths(asset):
    """Recursively walk an Asset's properties and collect file path strings."""
    paths = []
    try:
        for i in range(asset.Size):
            prop = asset[i]
            # Connected sub-assets (e.g. texture nodes)
            if prop.IsConnected:
                connected = prop.GetConnectedProperty(0)
                paths.extend(get_asset_paths(connected))
            else:
                val = prop.Value
                if isinstance(val, str) and val.strip():
                    # Heuristic: looks like a file path
                    if any(val.lower().endswith(ext) for ext in
                           [".png", ".jpg", ".jpeg", ".bmp", ".tif",
                            ".tiff", ".exr", ".hdr", ".dds", ".gif"]):
                        paths.append((prop.Name, val.strip()))
    except Exception:
        pass
    return paths


for asset_elem in collector:
    name = asset_elem.Name
    try:
        render_asset = asset_elem.GetRenderingAsset()
        schema = render_asset.LibraryName if render_asset.LibraryName else "Unknown Schema"
        paths = get_asset_paths(render_asset)

        if paths:
            for param_name, path in paths:
                results.append({
                    "Asset Name": name,
                    "Schema": schema,
                    "Parameter": param_name,
                    "File Path": path,
                })
        else:
            results.append({
                "Asset Name": name,
                "Schema": schema,
                "Parameter": "—",
                "File Path": "(no external file references found)",
            })
    except Exception as e:
        results.append({
            "Asset Name": name,
            "Schema": "ERROR",
            "Parameter": "—",
            "File Path": str(e),
        })

# ── Output to PyRevit output window ──────────────────────────────────────────
output.print_md("# 🎨 Appearance Asset File Paths")
output.print_md("**Model:** `{}`  \n **Total Assets Found:** `{}`".format(
    doc.Title, len(collector)
))
output.print_md("---")

# Group by asset name for readability
current_asset = None
for row in results:
    if row["Asset Name"] != current_asset:
        current_asset = row["Asset Name"]
        output.print_md("### 📦 {}".format(current_asset))
        output.print_md("*Schema: {}*".format(row["Schema"]))

    if row["Parameter"] == "—":
        output.print_md("- *(no external textures)*")
    else:
        output.print_md("- **{}** → `{}`".format(row["Parameter"], row["File Path"]))

output.print_md("---")
output.print_md("*{} asset(s) scanned.*".format(len(collector)))