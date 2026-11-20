"""Standalone tabs_at_intersection addon for Rayforge.

Adds tabs where geometry in the Tabs layer intersects the Outside contour,
and optionally the Inside contours. This addon intentionally has its own
stock detection/creation helpers and does not import or depend on layer_rules.
"""

import math
import shutil
import traceback
from pathlib import Path

import pluggy
import yaml
from gi.repository import Gio, GLib, Gtk
from rayforge.context import get_context
from rayforge.ui_gtk.action_registry import MenuPlacement
from rayforge.core.tab import Tab

hookimpl = pluggy.HookimplMarker("rayforge")
_main_window = None

DEFAULT_TAB_WIDTH = 0.8
DEFAULT_TAB_POWER_PLY = 40  # percent
CURVE_STEPS = {"simplified": 1, "few": 8, "max": 32}


@hookimpl
def main_window_ready(main_window):
    global _main_window
    _main_window = main_window
    print(f"[tabs_at_intersection] Main window captured: {type(main_window).__name__}")


# ===========================================================================
# Stock material detection (returns structured info)
# ===========================================================================
def _detect_stock_info() -> dict:
    """Return a dict describing the document's stock.

    Keys:
      material  : "balsa" | "ply" | "other"  (normalised for dialogs)
      name      : the raw material name, if resolvable
      has_stock : True if any stock exists in the document
    """
    default = {
        "material": "balsa",
        "name": None,
        "has_stock": False,
    }

    if _main_window is None:
        return default

    try:
        de = _main_window.doc_editor
        doc = de.doc
    except Exception:
        return default

    material_mgr = None
    try:
        material_mgr = getattr(get_context(), "material_mgr", None)
    except Exception:
        pass

    # --- Collect stock candidates ---
    candidates = []
    try:
        stock_items = list(getattr(doc, "stock_items", []) or [])
        for item in stock_items:
            asset_uid = getattr(item, "stock_asset_uid", None)
            visible = getattr(item, "visible", True)
            asset = None
            try:
                asset = getattr(item, "stock_asset", None)
            except Exception:
                pass
            if asset is None and asset_uid:
                getter = getattr(doc, "get_asset_by_uid", None)
                if callable(getter):
                    try:
                        asset = getter(asset_uid)
                    except Exception:
                        pass
            material_uid = getattr(asset, "material_uid", None) if asset else None
            if material_uid:
                candidates.append((bool(visible), "stock item", item, asset, material_uid))
    except Exception:
        pass

    selected = next((c for c in candidates if c[0]), None)
    if selected is None and candidates:
        selected = candidates[0]

    if selected is None:
        # Rotary stock: layer.stock_material_uid
        try:
            for layer in doc.layers:
                uid = getattr(layer, "stock_material_uid", None)
                if uid:
                    selected = (True, "layer", layer, None, uid)
                    break
        except Exception:
            pass

    if selected is None:
        return default

    has_stock = True
    _visible, _source, owner, asset, material_uid = selected

    # --- Resolve the material object ---
    material = None
    if material_mgr is not None:
        lookup = getattr(material_mgr, "get_material_or_none", None)
        if callable(lookup):
            try:
                material = lookup(material_uid)
            except Exception:
                pass
    if material is None:
        for obj in (asset, owner):
            try:
                c = getattr(obj, "material", None) if obj is not None else None
                if c is not None:
                    material = c
                    break
            except Exception:
                pass

    name = getattr(material, "name", None) if material is not None else None
    normalized = str(name).casefold() if name else ""

    if "ply" in normalized or "sperrholz" in normalized:
        material_key = "ply"
    elif "balsa" in normalized:
        material_key = "balsa"
    else:
        material_key = "other"

    return {
        "material": material_key,
        "name": name,
        "has_stock": has_stock,
    }


# ===========================================================================
# Existing tabs: detection + overwrite confirmation
# ===========================================================================
def _count_existing_tabs(layer_names) -> dict:
    """Return {layer_name: tab_count} for the given layers (only > 0)."""
    counts = {}
    if _main_window is None:
        return counts
    try:
        doc = _main_window.doc_editor.doc
        for layer in doc.layers:
            if layer.name not in layer_names:
                continue
            n = 0
            for wp in layer.workpieces:
                tabs = getattr(wp, "tabs", None)
                if tabs:
                    n += len(tabs)
            if n > 0:
                counts[layer.name] = counts.get(layer.name, 0) + n
    except Exception as e:
        print(f"[tabs_at_intersection] Could not check for existing tabs: {e}")
    return counts


def _show_overwrite_tabs_dialog(parent, counts: dict, on_decision):
    """Warn that tabs already exist; ask whether to overwrite them.

    Calls on_decision(True) to continue (overwriting), on_decision(False)
    to abort.
    """
    dlg = Gtk.Dialog(
        transient_for=parent,
        modal=True,
        title="Existing tabs found",
    )
    dlg.set_default_size(460, 200)
    dlg.add_button("Cancel", Gtk.ResponseType.CANCEL)
    dlg.add_button("Overwrite and continue", Gtk.ResponseType.OK)
    ok_btn = dlg.get_widget_for_response(Gtk.ResponseType.OK)
    if ok_btn is not None:
        ok_btn.add_css_class("destructive-action")

    box = dlg.get_content_area()
    box.set_spacing(8)
    box.set_margin_top(12)
    box.set_margin_bottom(12)
    box.set_margin_start(12)
    box.set_margin_end(12)

    header = Gtk.Label()
    header.set_markup("<b>This document already contains tabs</b>")
    header.set_xalign(0)
    box.append(header)

    details = "\n".join(
        f"\u2022 {GLib.markup_escape_text(name)}: {n} tab(s)"
        for name, n in counts.items()
    )
    body = Gtk.Label()
    body.set_markup(
        f"Tabs were already set on:\n{details}\n\n"
        f"Continuing will <b>overwrite</b> these tabs (including tabs you "
        f"placed manually) with the newly calculated ones.\n\n"
        f"Do you want to continue?"
    )
    body.set_wrap(True)
    body.set_xalign(0)
    box.append(body)

    def _on_response(d, rid):
        d.destroy()
        on_decision(rid == Gtk.ResponseType.OK)

    dlg.connect("response", _on_response)
    dlg.present()


# ===========================================================================
# Material existence and creation
# ===========================================================================
def _material_exists(name_substring: str) -> bool:
    """True if a loaded material's name contains the substring (casefolded)."""
    try:
        mgr = get_context().material_mgr
        needle = name_substring.casefold()
        for m in mgr.get_all_materials():
            name = str(getattr(m, "name", "") or "").casefold()
            if needle in name:
                return True
    except Exception as e:
        print(f"[tabs_at_intersection] Could not check for {name_substring!r}: {e}")
    return False


_BIRCH_HINTS = ("birch", "birke", "bouleau", "abedul", "betula")
_WOOD_HINTS = ("wood", "holz", "bois", "madera")

BALSA_UID = "balsa"
BALSA_NAME = "Balsa"
BALSA_DESCRIPTION = "Lightweight balsa wood."
USER_LIBRARY_NAME = "Tabs at Intersection"
USER_LIBRARY_DIRNAME = "tabs_at_intersection"  # only used by the manual fallback


def _text_of(value) -> str:
    """Casefolded text of a str / LocalizedField (substring checks only)."""
    return str(value or "").casefold()


def _is_wood_material(material) -> bool:
    category = _text_of(getattr(material, "category", ""))
    return any(hint in category for hint in _WOOD_HINTS)


def _pick_template_material():
    """Return an existing material whose YAML will be cloned for Balsa.

    Only materials with an existing YAML file on disk qualify. Preference:
      1. wood + birch-ish name + texture file
      2. wood + texture file
      3. any wood material
      4. any material
    """
    try:
        materials = list(get_context().material_mgr.get_all_materials())
    except Exception as e:
        print(f"[tabs_at_intersection] Could not enumerate materials: {e}")
        return None

    def _has_file(m):
        fp = getattr(m, "file_path", None)
        return fp is not None and Path(fp).is_file()

    def _has_texture(m):
        try:
            tp = m.get_texture_path()
        except Exception:
            return False
        return tp is not None and Path(tp).is_file()

    def _is_birch(m):
        name = _text_of(getattr(m, "name", ""))
        return any(hint in name for hint in _BIRCH_HINTS)

    usable = [m for m in materials if _has_file(m)]
    tiers = (
        lambda m: _is_wood_material(m) and _has_texture(m) and _is_birch(m),
        lambda m: _is_wood_material(m) and _has_texture(m),
        _is_wood_material,
        lambda m: True,
    )
    for predicate in tiers:
        for m in usable:
            if predicate(m):
                return m
    return None


def _set_text_fields(node, values: dict):
    """Replace 'name' / 'description' values anywhere in a YAML tree.

    Handles plain strings as well as per-language dicts (every language
    receives the same text). The 'appearance' subtree is left alone.
    """
    if isinstance(node, dict):
        for key in list(node.keys()):
            if key == "appearance":
                continue
            value = node[key]
            if key in values:
                if isinstance(value, dict):
                    node[key] = {lang: values[key] for lang in value}
                else:
                    node[key] = values[key]
            else:
                _set_text_fields(value, values)
    elif isinstance(node, list):
        for item in node:
            _set_text_fields(item, values)


def _library_dir(lib):
    directory = getattr(lib, "_directory", None)
    return Path(directory) if directory is not None else None


def _find_target_library(mgr):
    """Return the library that should hold the Balsa material, or None.

    Preference: any writable library; otherwise the library this addon
    registered manually (see _ensure_user_library, fallback 2).
    """
    libs = list(mgr._libraries.values())
    for lib in libs:
        if not lib.read_only and _library_dir(lib) is not None:
            return lib
    try:
        manual_dir = (Path(mgr.user_dir) / USER_LIBRARY_DIRNAME).resolve()
    except Exception:
        return None
    for lib in libs:
        d = _library_dir(lib)
        if d is not None and d.resolve() == manual_dir:
            return lib
    return None


def _ensure_user_library(mgr):
    """Return a library that can hold the Balsa material (creating one)."""
    lib = _find_target_library(mgr)
    if lib is not None:
        return lib

    # 1) The manager's own factory.
    try:
        mgr.create_user_library(USER_LIBRARY_NAME)
    except Exception:
        print("[tabs_at_intersection] create_user_library() failed:\n"
              + traceback.format_exc())
    lib = _find_target_library(mgr)
    if lib is not None:
        return lib

    # 2) Manual: create the library folder and register it.
    try:
        from rayforge.core.material_library import MaterialLibrary
        target = Path(mgr.user_dir) / USER_LIBRARY_DIRNAME
        if not target.exists():
            if MaterialLibrary.create(target, USER_LIBRARY_NAME) is None:
                raise RuntimeError("MaterialLibrary.create() returned None")
        mgr.add_library_from_path(target)
    except Exception:
        print("[tabs_at_intersection] Manual library creation failed:\n"
              + traceback.format_exc())
        return None

    lib = _find_target_library(mgr)
    if lib is None:
        print("[tabs_at_intersection] Library was created but is not registered.")
    return lib


def _write_balsa_files(template, target_dir: Path) -> Path:
    """Write balsa.yaml (cloned from template) + texture into target_dir.

    Returns the path of the written YAML file.
    """
    src_yaml = Path(template.file_path)
    with open(src_yaml, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"Template YAML {src_yaml} is not a mapping")

    for key in ("uid", "id"):
        if key in data:
            data[key] = BALSA_UID
    _set_text_fields(data, {"name": BALSA_NAME,
                            "description": BALSA_DESCRIPTION})
    if not _is_wood_material(template):
        data["category"] = "Wood"

    target_dir.mkdir(parents=True, exist_ok=True)

    # Copy the texture file, keeping its position relative to the YAML.
    try:
        tex = template.get_texture_path()
    except Exception:
        tex = None
    if tex is not None and Path(tex).is_file():
        tex = Path(tex)
        try:
            rel = tex.relative_to(src_yaml.parent)
        except ValueError:
            rel = Path(tex.name)
        dest = target_dir / rel
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(tex, dest)

    out_path = target_dir / f"{BALSA_UID}.yaml"
    text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return out_path


def _create_balsa_material() -> bool:
    """Create a 'Balsa' material in a writable library.

    Returns True on success. Safe to call repeatedly: returns True
    immediately if a material containing 'balsa' already exists.
    """
    if _material_exists("balsa"):
        return True

    try:
        mgr = get_context().material_mgr
    except Exception as e:
        print(f"[tabs_at_intersection] material_mgr unavailable: {e}")
        return False

    template = _pick_template_material()
    if template is None:
        print("[tabs_at_intersection] No template material available for Balsa.")
        return False

    lib = _ensure_user_library(mgr)
    if lib is None:
        return False
    print(f"[tabs_at_intersection] Creating 'Balsa' (template: "
          f"{str(getattr(template, 'name', ''))!r}, "
          f"library: {lib.display_name!r}).")

    yaml_path = None
    try:
        yaml_path = _write_balsa_files(template, _library_dir(lib))
        lib.reload()
        if not _material_exists("balsa"):
            # The manager may cache; ask it to re-read once.
            mgr.reload_libraries()
    except Exception:
        print("[tabs_at_intersection] Creating Balsa failed:\n"
              + traceback.format_exc())

    if _material_exists("balsa"):
        try:
            mgr.libraries_changed.send(mgr)  # refresh the Materials UI
        except Exception as e:
            print(f"[tabs_at_intersection] libraries_changed signal failed: {e}")
        print("[tabs_at_intersection] 'Balsa' material created.")
        return True

    # Roll back so a file Rayforge cannot load never stays in the library.
    if yaml_path is not None:
        try:
            yaml_path.unlink()
            lib.reload()
            print(f"[tabs_at_intersection] Removed {yaml_path} again.")
        except Exception as e:
            print(f"[tabs_at_intersection] Could not remove {yaml_path}: {e}")
    print("[tabs_at_intersection] Balsa material could not be created.")
    return False


# ===========================================================================
# Ensure default stock
# ===========================================================================
def _ensure_default_stock(material_type: str,
                          thickness_mm: float = None) -> bool:
    """Create the add-on's default sheet stock if appropriate.

    Rules:
      - If the document already has Balsa or Ply stock, leave it alone.
      - If the document has any other stock, a new stock is created
        alongside it.
      - If the document has no stock, a new stock is created.

    Material lookup prefers an exact 'Balsa' (creating it if needed) or
    'Sperrholz'; existing stock is never modified.

    thickness_mm: thickness of the new stock. If None (or <= 0), Rayforge's
    default_stock_thickness_mm from the config is used.
    """
    if _main_window is None:
        return False

    try:
        de = _main_window.doc_editor
        doc = de.doc
    except Exception as exc:
        print(f"[tabs_at_intersection] Could not access document for stock creation: "
              f"{exc}")
        return False

    # Inspect existing stock and decide whether to skip creation.
    try:
        existing_stock_items = list(getattr(doc, "stock_items", []) or [])
    except Exception as exc:
        print(f"[tabs_at_intersection] Could not inspect document stock items: {exc}")
        return False

    # If a stock is already Balsa or Ply, don't duplicate.
    if existing_stock_items:
        info = _detect_stock_info()
        if info["material"] in ("balsa", "ply"):
            print(f"[tabs_at_intersection] Document already has {info['material']} "
                  "stock; leaving it unchanged.")
            return False
        print("[tabs_at_intersection] Existing stock is neither Balsa nor Ply; "
              "creating a new stock alongside it.")

    # ----- Material resolution -----
    if material_type == "ply":
        material_names = ["Sperrholz", "Ply"]
        width_mm, height_mm = 400.0, 600.0
        # Balsa material creation is not needed for Ply.
    else:
        # Ensure a Balsa material exists before trying to resolve it.
        _create_balsa_material()
        material_names = ["Balsa"]
        width_mm, height_mm = 100.0, 1000.0

    material = None
    try:
        ctx = get_context()
        material_mgr = ctx.material_mgr
        materials = material_mgr.get_all_materials()
    except Exception as exc:
        print(f"[tabs_at_intersection] Could not look up materials: {exc}")
        return False

    for needle in material_names:
        # Exact match first, then case-insensitive substring.
        material = next(
            (m for m in materials
             if str(getattr(m, "name", "") or "") == needle),
            None,
        )
        if material is None:
            folded = needle.casefold()
            material = next(
                (m for m in materials
                 if folded in str(getattr(m, "name", "") or "").casefold()),
                None,
            )
        if material is not None:
            break

    if material is None:
        if material_type == "balsa":
            print("[tabs_at_intersection] Stock not created: no Balsa material could "
                  "be found or created.")
        else:
            print("[tabs_at_intersection] Stock not created: no Ply material "
                  f"(tried {material_names}) was found in the libraries.")
        return False

    # ----- Stock creation -----
    try:
        from raygeo.geo import Geometry, Matrix
        from rayforge.core.stock import StockItem
        from rayforge.core.stock_asset import StockAsset
        from rayforge.core.undo import Command

        geometry = Geometry()
        geometry.move_to(0, 0)
        geometry.line_to(width_mm, 0)
        geometry.line_to(width_mm, height_mm)
        geometry.line_to(0, height_mm)
        geometry.close_path()

        stock_name = "Stock 1"
        stock_asset = StockAsset(name=stock_name, geometry=geometry)
        stock_asset.material_uid = material.uid
        if thickness_mm is not None and thickness_mm > 0:
            stock_asset.thickness = float(thickness_mm)
        else:
            stock_asset.thickness = float(
                getattr(ctx.config, "default_stock_thickness_mm", 2.0) or 2.0
            )

        stock_item = StockItem(
            stock_asset_uid=stock_asset.uid,
            name=stock_name,
        )
        natural_width, natural_height = stock_asset.get_natural_size()
        stock_item.matrix = Matrix.scale(natural_width, natural_height)

        position = (0.0, 0.0)
        machine = getattr(ctx.config, "machine", None)
        panel = getattr(machine, "panel", None) if machine is not None else None
        reference = getattr(panel, "reference_position_world", None)
        position_from_origin = getattr(panel, "world_position_from_origin", None)
        if reference is not None and callable(position_from_origin):
            position = position_from_origin(
                reference[0], reference[1], (width_mm, height_mm)
            )
        stock_item.pos = position

        class _CreateDefaultStockCommand(Command):
            def __init__(self):
                super().__init__(name="Create default stock")

            def execute(self):
                self.do()

            def do(self):
                doc.add_asset(stock_asset, silent=True)
                doc.add_child(stock_item)

            def undo(self):
                doc.remove_child(stock_item)
                doc.remove_asset_by_uid(stock_asset.uid)

        doc.history_manager.execute(_CreateDefaultStockCommand())

        if not any(getattr(item, "uid", None) == stock_item.uid
                   for item in doc.stock_items):
            raise RuntimeError("Rayforge did not register the new StockItem")

        print(
            f"[tabs_at_intersection] Created {material.name} stock "
            f"({width_mm:g} x {height_mm:g} mm)."
        )
        return True

    except Exception as exc:
        print(
            f"[tabs_at_intersection] Failed to create {material.name} stock: "
            f"{type(exc).__name__}: {exc}"
        )
        return False



# ===========================================================================
# Tabs geometry helpers
# ===========================================================================
def _point_from_item(item):
    if item is None:
        return None
    if hasattr(item, "x") and hasattr(item, "y"):
        return (float(item.x), float(item.y))
    if isinstance(item, (tuple, list)):
        if len(item) >= 2 and isinstance(item[0], (int, float)):
            return (float(item[0]), float(item[1]))
        if len(item) >= 1:
            return _point_from_item(item[0])
    return None


def _extract_tab_segments(tabs_geom):
    segments = []
    last = None
    for cmd in tabs_geom.data:
        ctype = type(cmd).__name__
        end = _point_from_item(getattr(cmd, "end", None))
        if ctype == "Move":
            last = end
        elif ctype == "Line" and last is not None and end is not None:
            segments.append((last, end))
            last = end
        else:
            last = end
    return segments


def _segment_intersect(p1, p2, p3, p4):
    x1, y1 = p1; x2, y2 = p2
    x3, y3 = p3; x4, y4 = p4
    ax, ay = x2 - x1, y2 - y1
    bx, by = x4 - x3, y4 - y3
    cx, cy = x3 - x1, y3 - y1
    det = bx * ay - ax * by
    if abs(det) < 1e-9:
        return None
    t = (bx * cy - cx * by) / det
    u = (ax * cy - cx * ay) / det
    if 0.0 <= t <= 1.0 and 0.0 <= u <= 1.0:
        return (x1 + t * ax, y1 + t * ay)
    return None


def _sample_curve(cmd, steps):
    if steps <= 1:
        return []
    pts = []
    for i in range(steps + 1):
        t = i / steps
        p = None
        for method in ("get_point", "point_at", "evaluate", "position_at", "at"):
            if hasattr(cmd, method):
                try:
                    p = getattr(cmd, method)(t)
                    if p is not None:
                        break
                except Exception:
                    continue
        pt = _point_from_item(p)
        if pt is not None:
            pts.append(pt)
    return pts


def _collect_outside_segments(geom, mode):
    segments = []

    if mode == "contours":
        try:
            contours = geom.get_valid_contours_data()
        except Exception as e:
            print(f"[tabs_at_intersection] get_valid_contours_data failed: {e}")
            return segments
        print(f"[tabs_at_intersection] Using valid contours: {len(contours)} polygon(s)")
        for contour in contours:
            verts = contour.get("vertices", [])
            if not verts or len(verts) < 2:
                continue
            is_closed = contour.get("is_closed", False)
            n_edges = len(verts) - 1 + (1 if is_closed else 0)
            for i in range(n_edges):
                p1 = verts[i]
                p2 = verts[(i + 1) % len(verts)]
                segments.append((p1[:2], p2[:2]))
        print(f"[tabs_at_intersection] Segments from contours: {len(segments)}")
        return segments

    curve_steps = CURVE_STEPS.get(mode, 1)
    print(f"[tabs_at_intersection] Using raw geometry, curve subdivision = {curve_steps}")

    sampled_curves = 0
    chord_curves = 0
    last = None
    for cmd in geom.data:
        ctype = type(cmd).__name__
        end = _point_from_item(getattr(cmd, "end", None))

        if ctype == "Move":
            last = end
            continue
        if last is None or end is None:
            last = end
            continue

        if ctype == "Line":
            segments.append((last, end))
        elif ctype in ("Bezier", "Arc"):
            pts = _sample_curve(cmd, curve_steps) if curve_steps > 1 else []
            if len(pts) >= 2:
                for i in range(len(pts) - 1):
                    segments.append((pts[i], pts[i + 1]))
                sampled_curves += 1
            else:
                segments.append((last, end))
                if curve_steps > 1:
                    chord_curves += 1
        else:
            last = end
            continue

        last = end

    print(f"[tabs_at_intersection] Segments from raw geometry: {len(segments)}")
    if sampled_curves or chord_curves:
        print(f"[tabs_at_intersection] Curves: {sampled_curves} sampled, "
              f"{chord_curves} fell back to chord")
    return segments


# ===========================================================================
# Reusable tab application for one layer
# ===========================================================================
def _apply_tabs_to_layer(doc, de, layer_name, tab_segments,
                         geometry_mode, tab_power, tab_width):
    target_layer = None
    for layer in doc.layers:
        if layer.name == layer_name:
            target_layer = layer
            break

    if target_layer is None:
        print(f"[tabs_at_intersection] {layer_name} layer not found.")
        return 0

    target_step = None
    for step in target_layer.workflow:
        target_step = step
        break

    if target_step is None:
        print(f"[tabs_at_intersection] {layer_name} layer has no workflow steps.")
        return 0

    current_tab_power = getattr(target_step, "tab_power", None)
    if (isinstance(current_tab_power, (int, float))
            and abs(float(current_tab_power) - tab_power) < 1e-9):
        print(f"[tabs_at_intersection] {layer_name} tab_power already = {tab_power}, no change.")
    else:
        try:
            target_dict = getattr(target_step, "settings", None) or target_step.__dict__
            de.step.set_step_param(
                target_dict, "tab_power", tab_power,
                f"Set tab power on {target_step.name}",
            )
            print(f"[tabs_at_intersection] {layer_name} tab_power = {tab_power} "
                  f"({'gap mode' if tab_power == 0.0 else 'power mode'})")
        except Exception as e:
            target_step.tab_power = tab_power
            print(f"[tabs_at_intersection] {layer_name} tab_power direct ({e})")

    if not target_step.workpiece_uids:
        target_step.workpiece_uids = [wp.uid for wp in target_layer.workpieces]

    total_tabs = 0
    for wp in target_layer.workpieces:
        target_geom = wp.get_world_geometry()
        target_segments = _collect_outside_segments(target_geom, geometry_mode)

        raw = []
        for (p1, p2) in tab_segments:
            for (p3, p4) in target_segments:
                pt = _segment_intersect(p1, p2, p3, p4)
                if pt is not None:
                    raw.append(pt)

        deduped = []
        for (x, y) in raw:
            dup = False
            for (dx, dy) in deduped:
                if abs(dx - x) < 1.0 and abs(dy - y) < 1.0:
                    dup = True
                    break
            if not dup:
                deduped.append((x, y))

        print(f"[tabs_at_intersection] {layer_name} wp {wp.uid}: "
              f"raw={len(raw)}, unique={len(deduped)}")

        new_tabs = []
        skipped_far = 0
        skipped_move = 0
        skipped_error = 0
        for (x, y) in deduped:
            try:
                seg_idx, pos, nearest = target_geom.find_closest_point(x, y)
            except Exception:
                skipped_error += 1
                continue

            nx, ny = nearest[0], nearest[1]
            snap_dist = math.hypot(nx - x, ny - y)
            if snap_dist > 5.0:
                skipped_far += 1
                continue

            try:
                cmd = target_geom.get_command_at(seg_idx)
            except Exception:
                skipped_error += 1
                continue
            if type(cmd).__name__ == "Move":
                skipped_move += 1
                continue

            try:
                new_tabs.append(
                    Tab(width=tab_width, segment_index=seg_idx, pos=pos)
                )
            except Exception:
                skipped_error += 1

        print(f"[tabs_at_intersection] {layer_name} wp {wp.uid}: created {len(new_tabs)} tabs "
              f"(far={skipped_far}, move={skipped_move}, error={skipped_error})")

        wp.tabs = new_tabs
        wp.tabs_enabled = True
        total_tabs += len(new_tabs)

        try:
            wp.updated.send(wp)
        except Exception:
            pass

    try:
        target_step.updated.send(target_step)
    except Exception:
        pass

    return total_tabs


def _get_geometry_source_layers(doc):
    """Return every document layer for use in any of the three selectors."""
    return list(doc.layers)


def _get_default_layer_by_name(layers, exact_name):
    """Find a default layer by exact, case-insensitive, then partial name."""
    exact = next(
        (layer for layer in layers if str(layer.name) == exact_name),
        None,
    )
    if exact is not None:
        return exact

    folded_name = exact_name.casefold()
    case_insensitive = next(
        (layer for layer in layers
         if str(layer.name).casefold() == folded_name),
        None,
    )
    if case_insensitive is not None:
        return case_insensitive

    return next(
        (layer for layer in layers
         if folded_name in str(layer.name).casefold()),
        None,
    )


def _get_default_geometry_source_layer(layers):
    """Choose a tab-position layer, preferring Tabs/tab-like names."""
    if not layers:
        return None

    exact_tabs = next(
        (layer for layer in layers if str(layer.name) == "Tabs"),
        None,
    )
    if exact_tabs is not None:
        return exact_tabs

    exact_tab_name = next(
        (layer for layer in layers
         if str(layer.name).casefold() in ("tab", "tabs")),
        None,
    )
    if exact_tab_name is not None:
        return exact_tab_name

    similar = next(
        (layer for layer in layers
         if "tab" in str(layer.name).casefold()),
        None,
    )
    return similar if similar is not None else layers[0]


def _get_default_outside_layer(layers):
    return _get_default_layer_by_name(layers, "Outside")


def _get_default_inside_layer(layers):
    return _get_default_layer_by_name(layers, "Inside")


def _get_tab_source_segments(doc, layer_name):
    """Get tab-position segments from the first workpiece in a source layer.

    Returns (segments, error_message). If successful, error_message is None.
    """
    source_layer = next(
        (layer for layer in doc.layers if layer.name == layer_name),
        None,
    )

    if source_layer is None:
        return [], f"The layer '{layer_name}' no longer exists."

    source_wp = next(iter(source_layer.workpieces), None)
    if source_wp is None:
        return [], f"The layer '{layer_name}' contains no workpiece."

    try:
        source_geom = source_wp.get_world_geometry()
        segments = _extract_tab_segments(source_geom)
    except Exception as exc:
        return [], f"Could not read geometry from '{layer_name}': {exc}"

    if not segments:
        return [], (
            f"No usable line geometry was found in layer '{layer_name}'. "
            "Please choose another layer containing tab-position lines."
        )

    return segments, None


# ===========================================================================
# Real tabs (top-level)
# ===========================================================================
def _add_real_tabs(material_type: str, geometry_mode: str,
                   outside_layer_name: str, inside_layer_name: str,
                   tab_width: float, tab_power_ply: float,
                   source_layer_name: str):
    if _main_window is None:
        return

    de = _main_window.doc_editor
    doc = de.doc

    tab_segments, source_error = _get_tab_source_segments(
        doc, source_layer_name
    )
    if source_error:
        print(f"[tabs_at_intersection] {source_error}")
        return

    tab_power = 0.0 if material_type == "balsa" else tab_power_ply

    print(f"\n[tabs_at_intersection] Geometry source layer: {source_layer_name}")
    print(f"[tabs_at_intersection] Geometry mode: {geometry_mode}")
    print(f"[tabs_at_intersection] Tab line segments: {len(tab_segments)}")
    print(f"[tabs_at_intersection] Tab width: {tab_width} mm")
    print(f"[tabs_at_intersection] Outside target layer: {outside_layer_name or '(none)'}")
    print(f"[tabs_at_intersection] Inside target layer: {inside_layer_name or '(none)'}")
    print(f"[tabs_at_intersection] Material: {material_type}")
    print(f"[tabs_at_intersection] Tab power: {tab_power} "
          f"({'gap mode' if tab_power == 0.0 else 'power mode'})")

    outside_count = 0
    if outside_layer_name:
        outside_count = _apply_tabs_to_layer(
            doc, de, outside_layer_name, tab_segments, geometry_mode,
            tab_power, tab_width,
        )

    inside_count = 0
    if inside_layer_name:
        inside_count = _apply_tabs_to_layer(
            doc, de, inside_layer_name, tab_segments, geometry_mode,
            tab_power, tab_width,
        )

    print(f"\n[tabs_at_intersection] Done. {outside_layer_name or 'No Outside'} tabs: "
          f"{outside_count}, {inside_layer_name or 'No Inside'} tabs: {inside_count}")


# ===========================================================================
# Action callbacks — Add Tabs
# ===========================================================================
def _real_tabs_dialog(default_material: str = "balsa"):
    from gi.repository import Gtk as _Gtk, GLib

    dialog = _Gtk.Dialog(
        transient_for=_main_window, modal=True,
        title="Add Tabs at intersectionpoints",
    )
    dialog.set_default_size(660, 800)
    dialog.add_button("Cancel", _Gtk.ResponseType.CANCEL)
    dialog.add_button("OK", _Gtk.ResponseType.OK)
    ok_button = dialog.get_widget_for_response(_Gtk.ResponseType.OK)

    content_area = dialog.get_content_area()
    scrolled = _Gtk.ScrolledWindow()
    scrolled.set_policy(_Gtk.PolicyType.NEVER, _Gtk.PolicyType.AUTOMATIC)
    scrolled.set_vexpand(True)
    scrolled.set_hexpand(True)
    content_area.append(scrolled)

    box = _Gtk.Box(orientation=_Gtk.Orientation.VERTICAL, spacing=4)
    box.set_margin_top(12); box.set_margin_bottom(12)
    box.set_margin_start(12); box.set_margin_end(12)
    scrolled.set_child(box)

    def _row(label_text, widget, expand_widget=False):
        row = _Gtk.Box(orientation=_Gtk.Orientation.HORIZONTAL, spacing=8)
        label = _Gtk.Label(label=label_text, xalign=0)
        label.set_wrap(True)
        if expand_widget:
            label.set_size_request(160, -1)
            widget.set_hexpand(True)
            widget.set_halign(_Gtk.Align.FILL)
        else:
            label.set_hexpand(True)
            widget.set_halign(_Gtk.Align.END)
        row.append(label)
        row.append(widget)
        box.append(row)
        return widget

    def _caption(text, margin_bottom=8):
        caption = _Gtk.Label()
        caption.set_markup(f"<small>{GLib.markup_escape_text(text)}</small>")
        caption.add_css_class("dim-label")
        caption.set_wrap(True)
        caption.set_xalign(0)
        caption.set_margin_start(12)
        caption.set_margin_top(2)
        caption.set_margin_bottom(margin_bottom)
        box.append(caption)
        return caption

    intro = _Gtk.Label()
    intro.set_markup(
        "<b>Place holding tabs at layer intersections</b>\n\n"
        "Choose the layer containing tab-position lines and the layer(s) "
        "whose contours should receive tabs. Each layer can be assigned "
        "to only one of the three roles."
    )
    intro.set_wrap(True)
    intro.set_xalign(0)
    intro.set_margin_bottom(16)
    box.append(intro)

    doc = _main_window.doc_editor.doc
    layers = _get_geometry_source_layers(doc)
    empty_id = "__empty_layer_selection__"

    def make_layer_combo():
        combo = _Gtk.ComboBoxText()
        combo.append(empty_id, "<empty>")
        for layer in layers:
            combo.append(layer.name, layer.name)
        combo.set_active_id(empty_id)
        return combo

    source_layer_combo = make_layer_combo()
    outside_layer_combo = make_layer_combo()
    inside_layer_combo = make_layer_combo()

    # Establish defaults in priority order: Tabs first, then Outside, Inside.
    # A default already claimed by an earlier selector remains empty here.
    defaults = (
        (source_layer_combo, _get_default_geometry_source_layer(layers)),
        (outside_layer_combo, _get_default_outside_layer(layers)),
        (inside_layer_combo, _get_default_inside_layer(layers)),
    )
    claimed_names = set()
    for combo, layer in defaults:
        if layer is not None and layer.name not in claimed_names:
            combo.set_active_id(layer.name)
            claimed_names.add(layer.name)

    _row("Layer with lines defining tab positions", source_layer_combo)
    _caption(
        "The geometry in this layer defines where holding tabs should be placed."
    )
    _row("Outside target layer", outside_layer_combo)
    _caption("Layer whose outside contours should receive tabs.")
    _row("Inside target layer", inside_layer_combo)
    _caption("Optional layer whose inside contours (holes) should also receive tabs.")

    # Keep the three roles mutually exclusive. Selecting a layer in one
    # selector clears it from either of the other selectors automatically.
    changing_selection = {"active": False}
    selectors = (
        source_layer_combo,
        outside_layer_combo,
        inside_layer_combo,
    )

    validation_label = _Gtk.Label()
    validation_label.set_xalign(0)
    validation_label.set_wrap(True)
    validation_label.set_markup(
        '<small><span foreground="red">Select a tab-position layer and at least one target layer (Outside or Inside).</span></small>'
    )
    validation_label.set_margin_top(4)
    validation_label.set_margin_bottom(8)

    def update_assignment_state(*_args):
        if changing_selection["active"]:
            return
        changing_selection["active"] = True
        try:
            for changed_combo in selectors:
                selected_id = changed_combo.get_active_id()
                if not selected_id or selected_id == empty_id:
                    continue
                for other_combo in selectors:
                    if other_combo is not changed_combo and other_combo.get_active_id() == selected_id:
                        other_combo.set_active_id(empty_id)
        finally:
            changing_selection["active"] = False

        has_tabs_layer = source_layer_combo.get_active_id() not in (None, empty_id)
        has_target_layer = (
            outside_layer_combo.get_active_id() not in (None, empty_id)
            or inside_layer_combo.get_active_id() not in (None, empty_id)
        )
        valid = has_tabs_layer and has_target_layer
        ok_button.set_sensitive(valid)
        validation_label.set_visible(not valid)

    for combo in selectors:
        combo.connect("changed", update_assignment_state)
    update_assignment_state()

    material_combo = _Gtk.ComboBoxText()
    material_combo.append("balsa", "Balsa")
    material_combo.append("ply", "Ply")
    material_combo.set_active_id(default_material)
    _row("Material", material_combo)
    _caption(
        "Balsa leaves a physical gap at each tab. "
        "Ply lowers laser power at each tab instead."
    )

    tab_width_scale = _Gtk.Scale.new_with_range(
        _Gtk.Orientation.HORIZONTAL, 0.5, 1.5, 0.1
    )
    tab_width_scale.set_value(DEFAULT_TAB_WIDTH)
    tab_width_scale.set_digits(1)
    tab_width_scale.set_draw_value(True)
    tab_width_scale.set_size_request(280, -1)
    _row("Tab width (mm)", tab_width_scale, expand_widget=True)
    _caption("Width of each tab in millimetres (0.5 – 1.5 mm).")

    power_scale = _Gtk.Scale.new_with_range(
        _Gtk.Orientation.HORIZONTAL, 0, 90, 1
    )
    power_scale.set_value(DEFAULT_TAB_POWER_PLY)
    power_scale.set_digits(0)
    power_scale.set_draw_value(True)
    power_scale.set_size_request(280, -1)
    _row("Tab power (%)", power_scale, expand_widget=True)
    power_caption = _caption("")

    mode_combo = _Gtk.ComboBoxText()
    mode_combo.append("contours", "a) Valid Contours")
    mode_combo.append("simplified", "b) Raw Geometry - Simplified (curves as chords)")
    mode_combo.append("few", "c) Raw Geometry - Few Subdivisions (8/curve)")
    mode_combo.append("max", "d) Raw Geometry - Max Subdivisions (32/curve)")
    mode_combo.set_active(0)
    _row("Geometry source", mode_combo)
    _caption("How the contour geometry is read when finding intersections.")

    # Keep the validation hint visible immediately above the dialog buttons.
    content_area.append(validation_label)

    def update_power_state():
        material = material_combo.get_active_id()
        if material == "ply":
            power_scale.set_sensitive(True)
            power_caption.set_markup(
                f"<small>{GLib.markup_escape_text('Laser power used while crossing each tab. Lower values make tabs easier to snap off; higher values keep parts secure.')}</small>"
            )
        else:
            power_scale.set_sensitive(False)
            power_caption.set_markup(
                f"<small>{GLib.markup_escape_text('Not adjustable — Balsa uses gap mode: the laser is switched off during each tab.')}</small>"
            )

    material_combo.connect("changed", lambda _c: update_power_state())
    update_power_state()

    def show_source_warning(message):
        warning = _Gtk.Dialog(
            transient_for=dialog,
            modal=True,
            title="No usable tab geometry",
        )
        warning.add_button("Cancel", _Gtk.ResponseType.CANCEL)
        warning.add_button("Choose another layer", _Gtk.ResponseType.OK)

        warning_box = warning.get_content_area()
        warning_box.set_spacing(8)
        warning_box.set_margin_top(12)
        warning_box.set_margin_bottom(12)
        warning_box.set_margin_start(12)
        warning_box.set_margin_end(12)

        label = _Gtk.Label(label=message)
        label.set_wrap(True)
        label.set_xalign(0)
        warning_box.append(label)

        def on_warning_response(warning_dialog, response_id):
            warning_dialog.destroy()
            if response_id == _Gtk.ResponseType.OK:
                dialog.present()
            else:
                dialog.destroy()

        warning.connect("response", on_warning_response)
        warning.present()

    def on_response(d, rid):
        if rid != _Gtk.ResponseType.OK:
            d.destroy()
            return

        source_layer_name = source_layer_combo.get_active_id()
        if source_layer_name == empty_id:
            source_layer_name = None
        outside_layer_name = outside_layer_combo.get_active_id()
        if outside_layer_name == empty_id:
            outside_layer_name = None
        inside_layer_name = inside_layer_combo.get_active_id()
        if inside_layer_name == empty_id:
            inside_layer_name = None

        if not source_layer_name or not (outside_layer_name or inside_layer_name):
            # Normally prevented by the insensitive OK button; retain a guard.
            return

        _segments, source_error = _get_tab_source_segments(
            _main_window.doc_editor.doc,
            source_layer_name,
        )
        if source_error:
            show_source_warning(source_error)
            return

        material = material_combo.get_active_id()
        tab_width = tab_width_scale.get_value()
        tab_power_ply = power_scale.get_value() / 100.0
        geometry_mode = mode_combo.get_active_id()

        def _run():
            _ensure_default_stock(material)
            _add_real_tabs(
                material,
                geometry_mode,
                outside_layer_name,
                inside_layer_name,
                tab_width,
                tab_power_ply,
                source_layer_name,
            )

        target_layers = [
            name for name in (outside_layer_name, inside_layer_name) if name
        ]
        existing = _count_existing_tabs(target_layers)
        if existing:
            print(f"[tabs_at_intersection] Existing tabs found: {existing}")

            def _on_decision(overwrite: bool):
                if overwrite:
                    _run()
                else:
                    print(
                        "[tabs_at_intersection] "
                        "Tab placement cancelled by user."
                    )

            _show_overwrite_tabs_dialog(
                _main_window,
                existing,
                _on_decision,
            )
        else:
            _run()

        d.destroy()

    dialog.connect("response", on_response)
    dialog.present()


def on_add_real_tabs_activated(action, parameter):
    info = _detect_stock_info()
    print(f"[tabs_at_intersection] Stock info: {info}")

    default_material = info["material"] if info["material"] in ("balsa", "ply") else "balsa"
    print(f"[tabs_at_intersection] Default material for tabs dialog: {default_material}")

    _real_tabs_dialog(default_material=default_material)



# ---------------------------------------------------------------------------
# Register this addon's uniquely named action. No shortcut is assigned so it
# cannot conflict with the legacy layer_rules action during the transition.
# ---------------------------------------------------------------------------
@hookimpl
def register_actions(action_registry):
    action = Gio.SimpleAction.new("tabs-at-intersectionpoints", None)
    action.connect("activate", on_add_real_tabs_activated)
    action_registry.register(
        action_name="tabs-at-intersectionpoints",
        action=action,
        addon_name="tabs_at_intersection",
        label="Add Tabs at intersectionpoints",
        icon_name="document-send-symbolic",
        menu=MenuPlacement(menu_id="tools", priority=52),
    )
