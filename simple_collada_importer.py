bl_info = {
    "name": "Simple COLLADA (.dae) Importer (Positions + Normals + Colors + UVs)",
    "author": "ekztal",
    "version": (0, 2, 0),
    "blender": (5, 0, 0),
    "location": "File > Import > Simple COLLADA (.dae)",
    "description": "Imports COLLADA meshes with POSITION/NORMAL/COLOR/TEXCOORD inside <triangles> with multi-offset indexing.",
    "category": "Import-Export",
}

import os
import math
import bpy
from bpy_extras.io_utils import ImportHelper
from bpy.types import Operator
from bpy.props import StringProperty
from mathutils import Vector
import xml.etree.ElementTree as ET


# ---------------------- XML/NAMESPACE HELPERS ----------------------

def get_collada_ns(root):
    """Return COLLADA namespace prefix '{...}' or empty."""
    if root.tag.startswith("{"):
        return root.tag.split("}")[0] + "}"
    return ""


def q(ns, tag):
    """Qualify XML tag with namespace."""
    return f"{ns}{tag}"


def parse_source_float_array(source_elem, ns):
    """
    Parse <source><float_array>…</float_array></source>
    Handles stride from <accessor>.
    Returns list of tuples (length = stride).
    """
    float_array = source_elem.find(q(ns, "float_array"))
    if float_array is None or float_array.text is None:
        return []

    raw_vals = float_array.text.strip().split()
    try:
        floats = [float(v) for v in raw_vals]
    except ValueError:
        return []

    accessor = source_elem.find(f"{q(ns,'technique_common')}/{q(ns,'accessor')}")
    stride = int(accessor.attrib.get("stride", "3")) if accessor is not None else 3

    out = []
    for i in range(0, len(floats), stride):
        chunk = floats[i:i+stride]
        if len(chunk) < stride:
            break
        out.append(tuple(chunk))
    return out


def extract_material_texture_map(root, ns):
    """
    Creates map: material_id -> texture_name (from first <init_from>).

    Reads:
      - <library_effects>
      - <library_materials>
    and connects material id -> effect id -> first init_from text.
    """
    texture_for_effect = {}
    material_to_effect = {}

    # ---- Read <library_effects> ----
    effects = root.findall(f".//{q(ns,'effect')}")
    for eff in effects:
        eff_id = eff.attrib.get("id")
        if not eff_id:
            continue
        # First <init_from> in this effect
        init_from = eff.find(f".//{q(ns,'init_from')}")
        if init_from is not None and init_from.text:
            tex = init_from.text.strip()
            texture_for_effect[eff_id] = tex

    # ---- Read <library_materials> ----
    materials = root.findall(f".//{q(ns,'material')}")
    for mat in materials:
        mat_id = mat.attrib.get("id")
        if not mat_id:
            continue
        inst = mat.find(f"./{q(ns,'instance_effect')}")
        if inst is not None:
            eff_url = inst.attrib.get("url", "")
            if eff_url.startswith("#"):
                eff_url = eff_url[1:]
            material_to_effect[mat_id] = eff_url

    # ---- Build final map ----
    mat_to_texture = {}
    for mat_id, eff_id in material_to_effect.items():
        if eff_id in texture_for_effect:
            mat_to_texture[mat_id] = texture_for_effect[eff_id]

    return mat_to_texture


# ---------------------- GEOMETRY IMPORTER ----------------------

def build_mesh_from_geometry(geom_elem, ns, collection, material_texture_map):
    """Convert <geometry> → Blender mesh with positions, normals, colors, UVs and materials."""
    mesh_elem = geom_elem.find(q(ns, "mesh"))
    if mesh_elem is None:
        print("Skipping geometry (no <mesh>):", geom_elem.attrib.get("id"))
        return None

    geom_name = geom_elem.attrib.get("name") or geom_elem.attrib.get("id") or "DAE_Mesh"

    # --- Parse <source> blocks ---
    sources = {}
    for src in mesh_elem.findall(q(ns, "source")):
        src_id = src.attrib.get("id")
        if not src_id:
            continue
        sources[src_id] = parse_source_float_array(src, ns)

    # --- Parse <vertices> mapping ---
    vertices_map = {}
    for verts in mesh_elem.findall(q(ns, "vertices")):
        v_id = verts.attrib.get("id")
        if not v_id:
            continue
        for inp in verts.findall(q(ns, "input")):
            if inp.attrib.get("semantic") == "POSITION":
                pos_id = inp.attrib.get("source", "")[1:]
                vertices_map[v_id] = pos_id

    # --- Prepare accumulators ---
    positions = None
    faces = []              # list[(v0, v1, v2)]
    face_mat_ids = []       # list[str or None], same length as faces
    corner_uvs = []         # list[(u, v)] per loop
    corner_cols = []        # list[(r,g,b,a)] per loop
    corner_norms = []       # list[Vector] per loop

    # --- Process <triangles> blocks ---
    for tri in mesh_elem.findall(q(ns, "triangles")):
        count = int(tri.attrib.get("count", "0"))
        inputs = tri.findall(q(ns, "input"))
        p_elem = tri.find(q(ns, "p"))
        if p_elem is None or not p_elem.text:
            continue

        tri_mat_id = tri.attrib.get("material")  # e.g. "Material 2"

        # offset → (semantic, srcID, set)
        input_by_offset = {}
        max_offset = 0
        for inp in inputs:
            sem = inp.attrib.get("semantic")
            src = inp.attrib.get("source", "")[1:]
            off = int(inp.attrib.get("offset", "0"))
            set_idx = inp.attrib.get("set")
            input_by_offset[off] = (sem, src, set_idx)
            max_offset = max(max_offset, off)

        num_inputs = max_offset + 1

        # --- Resolve VERTEX / POSITION source ---
        vertex_offset = None
        pos_source_id = None
        for off, (sem, src, _set) in input_by_offset.items():
            if sem == "VERTEX":
                vertex_offset = off
                pos_source_id = vertices_map.get(src)
                break

        if vertex_offset is None or pos_source_id is None:
            print("Missing POSITION source in:", geom_name)
            return None

        positions = sources.get(pos_source_id)
        if not positions:
            print("Position source missing:", pos_source_id)
            return None

        # --- Optional semantic sources ---
        normal_offset = None
        uv_offset = None
        color_offset = None

        normal_source = None
        uv_source = None
        color_source = None

        for off, (sem, src, set_idx) in input_by_offset.items():
            if sem == "NORMAL":
                normal_offset = off
                normal_source = sources.get(src)
            elif sem == "COLOR":
                color_offset = off
                color_source = sources.get(src)
            elif sem == "TEXCOORD":
                # prefer TEXCOORD set="0" if multiple
                if uv_source is None or set_idx == "0":
                    uv_offset = off
                    uv_source = sources.get(src)

        raw_idx = [int(x) for x in p_elem.text.strip().split()]
        expected = count * 3 * num_inputs
        if len(raw_idx) < expected:
            print(f"Warning: Index count short in {geom_name}")

        # --- Build triangles ---
        for i_tri in range(count):
            base = i_tri * 3 * num_inputs
            tri_vertices = []
            tri_uv = []
            tri_col = []
            tri_norm = []

            for v in range(3):
                b = base + v * num_inputs

                # Position index
                vi = raw_idx[b + vertex_offset]
                tri_vertices.append(vi)

                # Normal
                if normal_offset is not None and normal_source:
                    ni = raw_idx[b + normal_offset]
                    if 0 <= ni < len(normal_source):
                        tri_norm.append(Vector(normal_source[ni]))
                    else:
                        tri_norm.append(Vector((0, 0, 1)))

                # Color
                if color_offset is not None and color_source:
                    ci = raw_idx[b + color_offset]
                    if 0 <= ci < len(color_source):
                        c = color_source[ci]
                        if len(c) == 4:
                            tri_col.append((c[0], c[1], c[2], c[3]))
                        elif len(c) == 3:
                            tri_col.append((c[0], c[1], c[2], 1))
                        else:
                            tri_col.append((1, 1, 1, 1))
                    else:
                        tri_col.append((1, 1, 1, 1))

                # UV
                if uv_offset is not None and uv_source:
                    ti = raw_idx[b + uv_offset]
                    if 0 <= ti < len(uv_source):
                        uv = uv_source[ti]
                        tri_uv.append((uv[0], uv[1]))
                    else:
                        tri_uv.append((0, 0))

            # Skip degenerate
            if len(set(tri_vertices)) < 3:
                continue

            faces.append(tuple(tri_vertices))
            face_mat_ids.append(tri_mat_id)
            corner_norms.extend(tri_norm)
            corner_cols.extend(tri_col)
            corner_uvs.extend(tri_uv)

    if not positions or not faces:
        print("No valid geometry in:", geom_name)
        return None

    # ---------------------- CREATE MESH ----------------------

    mesh = bpy.data.meshes.new(geom_name)
    verts = [Vector(p) for p in positions]

    mesh.from_pydata(verts, [], faces)
    mesh.update(calc_edges=True)

    # Create object
    obj = bpy.data.objects.new(geom_name, mesh)
    collection.objects.link(obj)

    # Rotate Y-up → Z-up
    obj.rotation_euler[0] = math.pi / 2.0

# ---------------------- MATERIALS ----------------------
    unique_mat_ids = sorted({m for m in face_mat_ids if m is not None})
    mat_objects = {}
    mat_index_map = {}

    obj.data.materials.clear()

    for idx, mat_id in enumerate(unique_mat_ids):

        # Find texture name for this DAE material
        texname = material_texture_map.get(mat_id)
        if texname:
            tex_base = texname.split('.')[0]
        else:
            tex_base = mat_id  # fallback

        # Reuse existing material if it already exists
        mat = bpy.data.materials.get(tex_base)
        if mat is None:
            mat = bpy.data.materials.new(tex_base)

        obj.data.materials.append(mat)
        mat_objects[mat_id] = mat
        mat_index_map[mat_id] = idx


    # Assign material index per polygon
    for poly, mat_id in zip(mesh.polygons, face_mat_ids):
        if mat_id is None:
            continue
        if mat_id in mat_index_map:
            poly.material_index = mat_index_map[mat_id]

# ---------------------- UVs ----------------------
    if corner_uvs and len(corner_uvs) == len(mesh.loops):
        uv_layer = mesh.uv_layers.new(name="UVMap")
        for li, uv in enumerate(corner_uvs):
            uv_layer.data[li].uv = uv

# ---------------------- COLORS ----------------------
    if corner_cols and len(corner_cols) == len(mesh.loops):
        col_attr = mesh.color_attributes.new(
            name="Col",
            type="FLOAT_COLOR",
            domain="CORNER"
        )
        for li, col in enumerate(corner_cols):
            col_attr.data[li].color = col

# ---------------------- NORMALS ----------------------
    if corner_norms and len(corner_norms) == len(mesh.loops):
        mesh.normals_split_custom_set(corner_norms)
        # Blender 5: no need for use_auto_smooth, split normals are used automatically

    return obj


# ---------------------- IMPORT OPERATOR ----------------------

class IMPORT_OT_simple_collada_full(Operator, ImportHelper):
    """Import a COLLADA (.dae) mesh with full features"""
    bl_idname = "import_scene.simple_collada_full"
    bl_label = "Import Simple COLLADA (.dae)"

    filename_ext = ".dae"
    filter_glob: StringProperty(default="*.dae", options={'HIDDEN'})

    def execute(self, context):

        if not os.path.isfile(self.filepath):
            self.report({'ERROR'}, f"File not found: {self.filepath}")
            return {'CANCELLED'}

        try:
            tree = ET.parse(self.filepath)
            root = tree.getroot()
        except Exception as e:
            self.report({'ERROR'}, f"Failed to parse DAE: {e}")
            return {'CANCELLED'}

        ns = get_collada_ns(root)

        # Build material -> texture mapping from DAE
        material_texture_map = extract_material_texture_map(root, ns)

        # Use active collection
        if context.view_layer.active_layer_collection:
            collection = context.view_layer.active_layer_collection.collection
        else:
            collection = context.scene.collection

        geometries = root.findall(f".//{q(ns,'geometry')}")
        if not geometries:
            self.report({'ERROR'}, "No <geometry> found in DAE")
            return {'CANCELLED'}

        imported = 0
        for geom in geometries:
            obj = build_mesh_from_geometry(geom, ns, collection, material_texture_map)
            if obj:
                imported += 1

        if imported == 0:
            self.report({'ERROR'}, "No objects created. Check console.")
            return {'CANCELLED'}

        self.report({'INFO'}, f"Imported {imported} object(s).")
        return {'FINISHED'}


# ---------------------- TEXTURE ASSIGN OPERATOR ----------------------

class OBJECT_OT_assign_textures_by_name(Operator):
    """Assign textures based on material names matching image file names"""
    bl_idname = "object.assign_textures_by_name"
    bl_label = "Assign Textures by Name"
    bl_options = {'REGISTER', 'UNDO'}

    directory: StringProperty(
        name="Texture Folder",
        description="Folder containing texture images",
        subtype='DIR_PATH'
    )

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        folder = bpy.path.abspath(self.directory)

        if not os.path.isdir(folder):
            self.report({'ERROR'}, f"Not a directory: {folder}")
            return {'CANCELLED'}

        exts = {".png", ".jpg", ".jpeg", ".tga", ".bmp", ".tif", ".tiff", ".dds"}
        images = {}

        # Load images from folder
        for f in os.listdir(folder):
            name, ext = os.path.splitext(f)
            if ext.lower() in exts:
                full = os.path.join(folder, f)
                try:
                    img = bpy.data.images.load(full, check_existing=True)
                    images[name] = img
                except:
                    pass

        assigned = 0

        for obj in context.selected_objects:
            if not hasattr(obj.data, "materials"):
                continue

            for mat in obj.data.materials:
                if not mat:
                    continue

                key = str(mat.name).strip()  # <- CRUCIAL FIX

                if key not in images:
                    # print(f"No match for: {repr(key)}")
                    continue

                img = images[key]

                mat.use_nodes = True
                nodes = mat.node_tree.nodes
                links = mat.node_tree.links

                while nodes:
                    nodes.remove(nodes[0])

                out = nodes.new("ShaderNodeOutputMaterial")
                out.location = (300, 0)
                bsdf = nodes.new("ShaderNodeBsdfPrincipled")
                bsdf.location = (0, 0)
                img_node = nodes.new("ShaderNodeTexImage")
                img_node.image = img
                img_node.location = (-300, 0)

                links.new(img_node.outputs["Color"], bsdf.inputs["Base Color"])
                links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])

                assigned += 1

        self.report({'INFO'}, f"Assigned textures to {assigned} materials.")
        return {'FINISHED'}


# ---------------------- MENUS & REGISTER ----------------------

def menu_func_import(self, context):
    self.layout.operator(
        IMPORT_OT_simple_collada_full.bl_idname,
        text="Simple COLLADA (.dae)"
    )


def menu_func_assign_textures(self, context):
    self.layout.operator(
        OBJECT_OT_assign_textures_by_name.bl_idname,
        text="Assign Textures by Name"
    )


def register():
    bpy.utils.register_class(IMPORT_OT_simple_collada_full)
    bpy.utils.register_class(OBJECT_OT_assign_textures_by_name)
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)
    bpy.types.VIEW3D_MT_object.append(menu_func_assign_textures)


def unregister():
    bpy.types.VIEW3D_MT_object.remove(menu_func_assign_textures)
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)
    bpy.utils.unregister_class(OBJECT_OT_assign_textures_by_name)
    bpy.utils.unregister_class(IMPORT_OT_simple_collada_full)


if __name__ == "__main__":
    register()

