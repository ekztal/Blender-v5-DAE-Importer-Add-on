bl_info = {
    "name": "Simple COLLADA (.dae) Importer (Positions + Normals + Colors + UVs)",
    "author": "ekztal / MilesExilium",
    "version": (3, 3, 0),
    "blender": (4, 0, 0),
    "location": "File > Import > Simple COLLADA (.dae)",
    "description": "Imports COLLADA meshes with positions, normals, UVs, vertex colors, textures, armature and skin weights.",
    "category": "Import-Export",
    "support": "COMMUNITY",
}

import os
import math
import bpy
from bpy_extras.io_utils import ImportHelper
from bpy.types import Operator
from bpy.props import StringProperty, BoolProperty, FloatProperty
from mathutils import Vector, Matrix
import xml.etree.ElementTree as ET


# ── XML / NAMESPACE HELPERS ─────────────────────────────────────────────────

def get_collada_ns(root):
    if root.tag.startswith("{"):
        return root.tag.split("}")[0] + "}"
    return ""

def q(ns, tag):
    return f"{ns}{tag}"

def parse_source_float_array(source_elem, ns):
    fa = source_elem.find(q(ns, "float_array"))
    if fa is None or fa.text is None:
        return []
    try:
        floats = [float(v) for v in fa.text.strip().split()]
    except ValueError:
        return []
    acc = source_elem.find(f"{q(ns,'technique_common')}/{q(ns,'accessor')}")
    stride = int(acc.attrib.get("stride", "3")) if acc is not None else 3
    out = []
    for i in range(0, len(floats), stride):
        chunk = floats[i:i+stride]
        if len(chunk) < stride:
            break
        out.append(tuple(chunk))
    return out

def parse_matrix(text):
    vals = [float(v) for v in text.strip().split()]
    if len(vals) != 16:
        return Matrix.Identity(4)
    return Matrix([vals[0:4], vals[4:8], vals[8:12], vals[12:16]])

def get_up_axis_matrix(root, ns):
    asset = root.find(q(ns, "asset"))
    up    = asset.find(q(ns, "up_axis")) if asset is not None else None
    axis  = up.text.strip().upper() if (up is not None and up.text) else "Z_UP"
    if axis == "Z_UP":
        return Matrix.Identity(4)
    elif axis == "X_UP":
        return Matrix.Rotation(-math.pi / 2.0, 4, 'Y')
    else:
        return Matrix.Rotation(math.pi / 2.0, 4, 'X')

def analyse_dae(root, ns):
    asset      = root.find(q(ns, "asset"))
    up_elem    = asset.find(q(ns, "up_axis")) if asset is not None else None
    up_axis    = up_elem.text.strip().upper() if (up_elem is not None and up_elem.text) else "Z_UP"
    unit_elem  = asset.find(q(ns, "unit")) if asset is not None else None
    unit_meter = float(unit_elem.attrib.get("meter", 1.0)) if unit_elem is not None else 1.0
    joint_nodes  = root.findall(f".//{q(ns,'node')}[@type='JOINT']")
    ctrl_lib     = root.find(q(ns, "library_controllers"))
    ctrl_list    = list(ctrl_lib) if ctrl_lib is not None else []
    skin_ctrls   = [c for c in ctrl_list if c.find(q(ns,"skin")) is not None]
    is_rigged    = len(joint_nodes) > 0 and len(skin_ctrls) > 0
    has_lib_nodes  = root.find(q(ns, "library_nodes")) is not None
    has_inst_nodes = bool(root.findall(f".//{q(ns,'instance_node')}"))
    is_assembly    = has_inst_nodes or (has_lib_nodes and not is_rigged)
    anim_lib  = root.find(q(ns, "library_animations"))
    has_anims = anim_lib is not None and len(list(anim_lib)) > 0
    profile = {
        "is_rigged": is_rigged, "is_assembly": is_assembly,
        "up_axis": up_axis, "unit_meter": unit_meter,
        "joint_count": len(joint_nodes), "controller_count": len(skin_ctrls),
        "has_lib_nodes": has_lib_nodes, "has_inst_nodes": has_inst_nodes,
        "has_anims": has_anims,
    }
    print(f"[DAE Profile] rigged={is_rigged} assembly={is_assembly} "
          f"up={up_axis} unit={unit_meter} "
          f"joints={len(joint_nodes)} ctrls={len(skin_ctrls)} anims={has_anims}")
    return profile


# ── MATERIAL / TEXTURE HELPERS ───────────────────────────────────────────────

def extract_material_texture_map(root, ns):
    image_path_for_id = {}
    for img in root.findall(f".//{q(ns,'image')}"):
        img_id = img.attrib.get("id")
        if not img_id:
            continue
        init_from = img.find(q(ns, "init_from"))
        if init_from is not None and init_from.text:
            image_path_for_id[img_id] = init_from.text.strip()

    channels_for_effect = {}
    for eff in root.findall(f".//{q(ns,'effect')}"):
        eff_id = eff.attrib.get("id")
        if not eff_id:
            continue
        sid_to_image   = {}
        sid_to_surface = {}
        for newparam in eff.findall(f".//{q(ns,'newparam')}"):
            sid     = newparam.attrib.get("sid", "")
            surface = newparam.find(q(ns, "surface"))
            if surface is not None:
                inf = surface.find(q(ns, "init_from"))
                if inf is not None and inf.text:
                    sid_to_image[sid] = inf.text.strip()
            sampler = newparam.find(q(ns, "sampler2D"))
            if sampler is not None:
                src = sampler.find(q(ns, "source"))
                if src is not None and src.text:
                    sid_to_surface[sid] = src.text.strip()

        def resolve(tex_ref, s2surf=sid_to_surface, s2img=sid_to_image):
            if tex_ref in s2surf:
                image_id = s2img.get(s2surf[tex_ref], "")
            elif tex_ref in s2img:
                image_id = s2img[tex_ref]
            else:
                image_id = tex_ref
            return image_path_for_id.get(image_id)

        channels = {}
        prof = eff.find(q(ns, "profile_COMMON"))
        if prof is not None:
            technique = prof.find(q(ns, "technique"))
            if technique is not None:
                for shader in technique:
                    shader_tag = shader.tag.replace(ns, "")
                    if shader_tag not in ("phong","lambert","blinn","constant"):
                        continue
                    for chan in shader:
                        chan_name = chan.tag.replace(ns, "")
                        tex = chan.find(q(ns, "texture"))
                        if tex is not None:
                            path = resolve(tex.attrib.get("texture", ""))
                            if path:
                                if chan_name == "diffuse":
                                    channels["diffuse"] = path
                                elif chan_name in ("bump", "normal"):
                                    channels["normal"] = path
                                elif chan_name == "specular":
                                    channels["specular"] = path

        for tech in eff.findall(f".//{q(ns,'technique')}") + eff.findall(".//technique"):
            profile_name = tech.attrib.get("profile", "")
            if profile_name in ("FCOLLADA", "OpenCOLLADA3dsMax", "MAX3D"):
                bump = tech.find("bump")
                if bump is not None:
                    tex = bump.find("texture")
                    if tex is not None:
                        path = resolve(tex.attrib.get("texture", ""))
                        if path:
                            channels.setdefault("normal", path)

        all_tex_refs = [t.attrib.get("texture","") for t in eff.findall(f".//{q(ns,'texture')}")]
        all_paths    = [resolve(ref) for ref in all_tex_refs if resolve(ref)]
        if "diffuse" not in channels and all_paths:
            channels["diffuse"] = all_paths[0]

        if channels:
            channels_for_effect[eff_id] = channels

    material_to_effect = {}
    for mat in root.findall(f".//{q(ns,'material')}"):
        mat_id = mat.attrib.get("id")
        if not mat_id:
            continue
        inst = mat.find(f"./{q(ns,'instance_effect')}")
        if inst is not None:
            eff_url = inst.attrib.get("url", "")[1:]
            material_to_effect[mat_id] = eff_url

    mat_to_textures = {}
    for mat_id, eff_id in material_to_effect.items():
        if eff_id in channels_for_effect:
            mat_to_textures[mat_id] = channels_for_effect[eff_id]
    return mat_to_textures


# ── ARMATURE BUILDER ─────────────────────────────────────────────────────────

def build_armature(root, ns, collection, model_name="Armature", correction_mat=None):
    vs = root.find(f".//{q(ns,'visual_scene')}")
    if vs is None:
        return None

    joint_bind_world = {}
    joint_bsm        = {}

    ctrl_lib = root.find(f".//{q(ns,'library_controllers')}")
    if ctrl_lib is not None:
        for ctrl in ctrl_lib.findall(q(ns, "controller")):
            skin = ctrl.find(q(ns, "skin"))
            if skin is None:
                continue
            geom_id  = skin.attrib.get("source", "")[1:]
            bsm_elem = skin.find(q(ns, "bind_shape_matrix"))
            bsm = parse_matrix(bsm_elem.text) if (bsm_elem is not None and bsm_elem.text) else Matrix.Identity(4)
            joint_bsm[geom_id] = bsm

            joints_elem = skin.find(q(ns, "joints"))
            if joints_elem is None:
                continue
            jnames_src = ibm_src = None
            for inp in joints_elem.findall(q(ns, "input")):
                sem = inp.attrib.get("semantic", "")
                src = inp.attrib.get("source", "")[1:]
                if sem == "JOINT":             jnames_src = src
                elif sem == "INV_BIND_MATRIX": ibm_src    = src

            sources = {}
            for src in skin.findall(q(ns, "source")):
                sid = src.attrib.get("id", "")
                na  = src.find(q(ns, "Name_array"))
                fa  = src.find(q(ns, "float_array"))
                if na is not None and na.text:   sources[sid] = na.text.strip().split()
                elif fa is not None and fa.text: sources[sid] = [float(x) for x in fa.text.strip().split()]

            jnames     = sources.get(jnames_src, [])
            ibm_floats = sources.get(ibm_src, [])

            ibm_col_scale = 1.0
            if len(ibm_floats) >= 9:
                import math as _math
                ibm_col_scale = _math.sqrt(ibm_floats[0]**2 + ibm_floats[4]**2 + ibm_floats[8]**2)
                if abs(ibm_col_scale - 1.0) < 0.001:
                    ibm_col_scale = 1.0

            for i, jname in enumerate(jnames):
                if jname in joint_bind_world:
                    continue
                start = i * 16
                if start + 16 > len(ibm_floats):
                    continue
                m = ibm_floats[start:start+16]
                if abs(ibm_col_scale - 1.0) > 0.001:
                    s = ibm_col_scale
                    tx, ty, tz = m[3], m[7], m[11]
                    r00,r01,r02 = m[0]/s, m[4]/s, m[8]/s
                    r10,r11,r12 = m[1]/s, m[5]/s, m[9]/s
                    r20,r21,r22 = m[2]/s, m[6]/s, m[10]/s
                    bx = -(r00*tx + r01*ty + r02*tz) / s
                    by = -(r10*tx + r11*ty + r12*tz) / s
                    bz = -(r20*tx + r21*ty + r22*tz) / s
                    world = Matrix.Identity(4)
                    world.translation = Vector((bx, by, bz))
                    joint_bind_world[jname] = world
                else:
                    inv_bind = Matrix([m[0:4], m[4:8], m[8:12], m[12:16]])
                    try:
                        joint_bind_world[jname] = inv_bind.inverted()
                    except Exception:
                        joint_bind_world[jname] = Matrix.Identity(4)

    if not joint_bind_world:
        return None

    bone_info  = {}
    name_to_id = {}

    def walk_joints(node, parent_id):
        node_id   = node.attrib.get("id",   "")
        node_name = node.attrib.get("name", node_id)
        node_type = node.attrib.get("type", "")
        if node_type == "JOINT" and node_id:
            node_name_norm = node_name.replace(" ", "_")
            bone_info[node_id] = {"name": node_name_norm, "parent_id": parent_id}
            name_to_id[node_name] = node_id
            name_to_id[node_name_norm] = node_id
            for child in node.findall(q(ns, "node")):
                walk_joints(child, node_id)
        else:
            for child in node.findall(q(ns, "node")):
                walk_joints(child, parent_id)

    for node in vs.findall(q(ns, "node")):
        walk_joints(node, None)

    id_to_id     = {jid: jid for jid in bone_info}
    name_to_id   = {}
    norm_to_id   = {}
    suffix_to_id = {}

    for jid, info in bone_info.items():
        raw_name  = next((n.attrib.get("name","") for n in vs.findall(f".//{q(ns,'node')}[@id='{jid}']")), info["name"])
        norm_name = info["name"]
        name_to_id[raw_name]  = jid
        name_to_id[norm_name] = jid
        norm_to_id[norm_name] = jid
        parts = jid.replace("-","_").split("_")
        for i in range(len(parts)):
            suffix = "_".join(parts[i:])
            if suffix and suffix not in suffix_to_id:
                suffix_to_id[suffix] = jid

    def resolve_skin_ref(ref):
        ref_norm = ref.replace(" ", "_")
        return (id_to_id.get(ref) or name_to_id.get(ref) or
                norm_to_id.get(ref_norm) or suffix_to_id.get(ref) or
                suffix_to_id.get(ref_norm) or None)

    remapped = {}
    for skin_ref, world_mat in joint_bind_world.items():
        jid = resolve_skin_ref(skin_ref)
        remapped[jid if jid else skin_ref] = world_mat
    joint_bind_world = remapped

    arm_data = bpy.data.armatures.new(model_name)
    arm_data.display_type = 'OCTAHEDRAL'
    arm_obj  = bpy.data.objects.new(model_name, arm_data)
    collection.objects.link(arm_obj)

    bpy.context.view_layer.objects.active = arm_obj
    bpy.ops.object.mode_set(mode='EDIT')
    edit_bones = arm_data.edit_bones
    created    = {}

    for bid, info in bone_info.items():
        if bid not in joint_bind_world:
            continue
        world      = joint_bind_world[bid]
        head_world = world.to_translation()
        eb         = edit_bones.new(info["name"])
        eb.head    = head_world
        children_with_pos = [c for c, ci in bone_info.items()
                              if ci["parent_id"] == bid and c in joint_bind_world]
        if children_with_pos:
            child_heads = [joint_bind_world[c].to_translation() for c in children_with_pos]
            avg_child   = sum(child_heads, Vector()) / len(child_heads)
            tail_vec    = avg_child - head_world
            length      = tail_vec.length
            eb.tail     = (head_world + tail_vec.normalized() * max(length, 0.02)
                           if length > 1e-4 else head_world + Vector((0, 0, 0.05)))
        else:
            y_axis  = world.to_3x3() @ Vector((0, 1, 0))
            y_axis  = y_axis.normalized() if y_axis.length > 1e-6 else Vector((0, 0, 1))
            eb.tail = head_world + y_axis * 0.05
        if (eb.tail - eb.head).length < 1e-5:
            eb.tail = eb.head + Vector((0, 0, 0.05))
        created[bid] = eb

    for bid, info in bone_info.items():
        if bid not in created:
            continue
        pid = info["parent_id"]
        if pid and pid in created:
            created[bid].parent = created[pid]

    bpy.ops.object.mode_set(mode='OBJECT')
    print(f"Armature '{model_name}' created with {len(created)} bones.")
    return arm_obj


# ── SKIN WEIGHT PARSER ───────────────────────────────────────────────────────

def parse_controllers(root, ns):
    result   = {}
    ctrl_lib = root.find(f".//{q(ns,'library_controllers')}")
    if ctrl_lib is None:
        return result

    for ctrl in ctrl_lib.findall(q(ns, "controller")):
        ctrl_id = ctrl.attrib.get("id", "")
        skin    = ctrl.find(q(ns, "skin"))
        if skin is None:
            continue
        skin_source = skin.attrib.get("source", "")[1:]
        bsm_elem    = skin.find(q(ns, "bind_shape_matrix"))
        bind_shape_matrix = parse_matrix(bsm_elem.text) if (bsm_elem is not None and bsm_elem.text) else Matrix.Identity(4)

        sources = {}
        for src in skin.findall(q(ns, "source")):
            src_id   = src.attrib.get("id", "")
            name_arr = src.find(q(ns, "Name_array"))
            if name_arr is not None and name_arr.text:
                sources[src_id] = name_arr.text.strip().split()
                continue
            float_arr = src.find(q(ns, "float_array"))
            if float_arr is not None and float_arr.text:
                try:
                    sources[src_id] = [float(v) for v in float_arr.text.strip().split()]
                except ValueError:
                    sources[src_id] = []

        joints_elem     = skin.find(q(ns, "joints"))
        joint_names_src = None
        if joints_elem is not None:
            for inp in joints_elem.findall(q(ns, "input")):
                if inp.attrib.get("semantic") == "JOINT":
                    joint_names_src = inp.attrib.get("source", "")[1:]

        joint_names = sources.get(joint_names_src, []) if joint_names_src else []
        real_bones  = [n for n in joint_names if not n.lower().startswith("notabone")]
        if not real_bones:
            print(f"Skipping placeholder-only skin controller '{ctrl_id}'")
            continue

        vw             = skin.find(q(ns, "vertex_weights"))
        vertex_weights = {}
        if vw is not None:
            joint_offset  = 0
            weight_offset = 1
            weight_src_id = None
            for inp in vw.findall(q(ns, "input")):
                sem = inp.attrib.get("semantic", "")
                off = int(inp.attrib.get("offset", "0"))
                src = inp.attrib.get("source", "")[1:]
                if sem == "JOINT":
                    joint_offset  = off
                elif sem == "WEIGHT":
                    weight_offset = off
                    weight_src_id = src
            weight_values = sources.get(weight_src_id, []) if weight_src_id else []
            vcount_elem   = vw.find(q(ns, "vcount"))
            v_elem        = vw.find(q(ns, "v"))
            if vcount_elem is not None and v_elem is not None and vcount_elem.text and v_elem.text:
                vcounts    = [int(x) for x in vcount_elem.text.strip().split()]
                v_data     = [int(x) for x in v_elem.text.strip().split()]
                if any(c > 0 for c in vcounts):
                    num_inputs = max(joint_offset, weight_offset) + 1
                    cursor     = 0
                    for vert_idx, count in enumerate(vcounts):
                        pairs = []
                        for _ in range(count):
                            j_idx = v_data[cursor + joint_offset]
                            w_idx = v_data[cursor + weight_offset]
                            w_val = weight_values[w_idx] if 0 <= w_idx < len(weight_values) else 0.0
                            pairs.append((j_idx, w_val))
                            cursor += num_inputs
                        vertex_weights[vert_idx] = pairs

        ibm_col_scale = 1.0
        ibm_R         = None
        joints_elem2  = skin.find(q(ns, "joints"))
        if joints_elem2 is not None:
            ibm_src_id = None
            for inp in joints_elem2.findall(q(ns, "input")):
                if inp.attrib.get("semantic") == "INV_BIND_MATRIX":
                    ibm_src_id = inp.attrib.get("source","")[1:]
            if ibm_src_id and ibm_src_id in sources:
                ibm_f = sources[ibm_src_id]
                if len(ibm_f) >= 9:
                    import math as _math
                    ibm_col_scale = round(_math.sqrt(ibm_f[0]**2 + ibm_f[4]**2 + ibm_f[8]**2), 4)
                    if abs(ibm_col_scale - 1.0) < 0.001:
                        ibm_col_scale = 1.0
                    elif len(ibm_f) >= 16:
                        s     = ibm_col_scale
                        ibm_R = Matrix([
                            [ibm_f[0]/s, ibm_f[1]/s, ibm_f[2]/s],
                            [ibm_f[4]/s, ibm_f[5]/s, ibm_f[6]/s],
                            [ibm_f[8]/s, ibm_f[9]/s, ibm_f[10]/s],
                        ])

        result[ctrl_id] = {
            "skin_source":        skin_source,
            "joint_names":        [n.replace(" ", "_") for n in joint_names],
            "vertex_weights":     vertex_weights,
            "bind_shape_matrix":  bind_shape_matrix,
            "inv_bind_col_scale": ibm_col_scale,
            "inv_bind_R":         ibm_R,
        }
    return result


def build_ctrl_mat_map(root, ns, controllers):
    geom_to_mat_override = {}
    for ic in root.findall(f".//{q(ns,'instance_controller')}"):
        ctrl_url = ic.attrib.get("url", "")[1:]
        if ctrl_url not in controllers:
            continue
        geom_id = controllers[ctrl_url]["skin_source"]
        mat_map = {}
        for im in ic.findall(f".//{q(ns,'instance_material')}"):
            mat_map[im.attrib.get("symbol","")] = im.attrib.get("target","")[1:]
        geom_to_mat_override[geom_id] = mat_map
    for ig in root.findall(f".//{q(ns,'instance_geometry')}"):
        geom_id = ig.attrib.get("url", "")[1:]
        mat_map = {}
        for im in ig.findall(f".//{q(ns,'instance_material')}"):
            mat_map[im.attrib.get("symbol","")] = im.attrib.get("target","")[1:]
        if mat_map:
            geom_to_mat_override[geom_id] = mat_map
    return geom_to_mat_override


# ── GEOMETRY IMPORTER ────────────────────────────────────────────────────────

def build_mesh_from_geometry(geom_elem, ns, collection, material_texture_map,
                              arm_obj, controllers, ctrl_mat_override, dae_filepath,
                              import_uvs=True, import_normals=True,
                              import_vertex_colors=True, merge_vertices=False,
                              merge_threshold=0.0001, correction_mat=None):
    mesh_elem = geom_elem.find(q(ns, "mesh"))
    if mesh_elem is None:
        return None

    geom_id   = geom_elem.attrib.get("id", "")
    geom_name = geom_elem.attrib.get("name") or geom_id or "DAE_Mesh"

    sources = {}
    for src in mesh_elem.findall(q(ns, "source")):
        src_id = src.attrib.get("id")
        if src_id:
            sources[src_id] = parse_source_float_array(src, ns)

    vertices_map       = {}
    vertices_normals   = {}
    vertices_texcoords = {}
    vertices_colors    = {}
    for verts in mesh_elem.findall(q(ns, "vertices")):
        v_id = verts.attrib.get("id")
        if not v_id:
            continue
        for inp in verts.findall(q(ns, "input")):
            sem = inp.attrib.get("semantic","")
            src = inp.attrib.get("source", "")[1:]
            if sem == "POSITION":  vertices_map[v_id]       = src
            elif sem == "NORMAL":  vertices_normals[v_id]   = src
            elif sem == "TEXCOORD": vertices_texcoords[v_id] = src
            elif sem == "COLOR":   vertices_colors[v_id]    = src

    positions    = None
    faces        = []
    face_mat_ids = []
    corner_uvs   = []
    corner_cols  = []
    corner_norms = []

    prim_blocks = (
        [(tri, None) for tri in mesh_elem.findall(q(ns, "triangles"))] +
        [(pl,  pl.find(q(ns, "vcount"))) for pl in mesh_elem.findall(q(ns, "polylist"))]
    )

    for prim, vcount_elem in prim_blocks:
        count  = int(prim.attrib.get("count", "0"))
        p_elem = prim.find(q(ns, "p"))
        if p_elem is None or not p_elem.text:
            continue

        tri_mat_symbol = prim.attrib.get("material")
        tri_mat_id     = ctrl_mat_override.get(tri_mat_symbol, tri_mat_symbol)

        all_inputs = []
        max_offset = 0
        for inp in prim.findall(q(ns, "input")):
            sem   = inp.attrib.get("semantic")
            src   = inp.attrib.get("source", "")[1:]
            off   = int(inp.attrib.get("offset", "0"))
            set_i = inp.attrib.get("set")
            all_inputs.append((sem, src, off, set_i))
            max_offset = max(max_offset, off)

        num_inputs = max_offset + 1

        input_by_offset = {}
        for sem, src, off, set_i in all_inputs:
            if off not in input_by_offset or sem == "VERTEX":
                input_by_offset[off] = (sem, src, set_i)

        vertex_offset = pos_source_id = None
        vertex_src_id = None
        for off, (sem, src, _) in input_by_offset.items():
            if sem == "VERTEX":
                vertex_offset = off
                vertex_src_id = src
                pos_source_id = vertices_map.get(src)
                break

        if vertex_offset is None or pos_source_id is None:
            print("Missing POSITION source in:", geom_name)
            return None

        positions = sources.get(pos_source_id)
        if not positions:
            print("Position source missing:", pos_source_id)
            return None

        normal_offset = uv_offset = color_offset = None
        normal_source = uv_source = color_source = None

        # Scan ALL inputs (not just input_by_offset) so we catch TEXCOORD and COLOR
        # even when they share offset=0 with VERTEX (common in OoT/MM 3DS exports).
        for sem, src, off, set_idx in all_inputs:
            if sem == "NORMAL":
                if normal_source is None:
                    normal_offset = off;  normal_source = sources.get(src)
            elif sem == "COLOR":
                if color_source is None:
                    color_offset  = off;  color_source  = sources.get(src)
            elif sem == "TEXCOORD":
                if uv_source is None or set_idx == "0":
                    uv_offset = off;  uv_source = sources.get(src)

        # Fallback: check if NORMAL/TEXCOORD/COLOR were declared inside <vertices>
        if normal_source is None and vertex_src_id in vertices_normals:
            normal_offset = vertex_offset
            normal_source = sources.get(vertices_normals[vertex_src_id])
        if uv_source is None and vertex_src_id in vertices_texcoords:
            uv_offset = vertex_offset
            uv_source = sources.get(vertices_texcoords[vertex_src_id])
        if color_source is None and vertex_src_id in vertices_colors:
            color_offset = vertex_offset
            color_source = sources.get(vertices_colors[vertex_src_id])

        raw_idx = [int(x) for x in p_elem.text.strip().split()]

        if vcount_elem is not None and vcount_elem.text:
            vcounts = [int(x) for x in vcount_elem.text.strip().split()]
        else:
            real_count = len(raw_idx) // (3 * num_inputs) if num_inputs > 0 else count
            if real_count != count and real_count > 0:
                print(f"  [{geom_name}] count={count} but p has {len(raw_idx)} values "
                      f"-> correcting to {real_count} triangles")
                count = real_count
            vcounts = [3] * count

        cursor = 0
        for poly_vcount in vcounts:
            poly_vi   = []
            poly_uv   = []
            poly_col  = []
            poly_norm = []

            for v in range(poly_vcount):
                b  = cursor + v * num_inputs
                vi = raw_idx[b + vertex_offset]
                poly_vi.append(vi)

                if normal_offset is not None and normal_source:
                    ni = raw_idx[b + normal_offset]
                    poly_norm.append(Vector(normal_source[ni]) if 0 <= ni < len(normal_source) else Vector((0,0,1)))

                if color_offset is not None and color_source:
                    ci = raw_idx[b + color_offset]
                    if 0 <= ci < len(color_source):
                        c = color_source[ci]
                        poly_col.append((c[0], c[1], c[2], c[3] if len(c) == 4 else 1.0))
                    else:
                        poly_col.append((1,1,1,1))

                if uv_offset is not None and uv_source:
                    ti = raw_idx[b + uv_offset]
                    uv = uv_source[ti] if 0 <= ti < len(uv_source) else (0,0)
                    poly_uv.append((uv[0], uv[1]))  # raw V unchanged: COLLADA and Blender both use V=0 at bottom

            cursor += poly_vcount * num_inputs

            for i in range(1, poly_vcount - 1):
                tri_vi = [poly_vi[0], poly_vi[i], poly_vi[i+1]]
                if len(set(tri_vi)) < 3:
                    continue
                faces.append(tuple(tri_vi))
                face_mat_ids.append(tri_mat_id)
                if poly_norm: corner_norms.extend([poly_norm[0], poly_norm[i], poly_norm[i+1]])
                if poly_col:  corner_cols.extend( [poly_col[0],  poly_col[i],  poly_col[i+1]])
                if poly_uv:   corner_uvs.extend(  [poly_uv[0],   poly_uv[i],   poly_uv[i+1]])

    if not positions or not faces:
        print("No valid geometry in:", geom_name)
        return None

    # ── CREATE MESH ──────────────────────────────────────────────────────────
    skin_ctrl = next((c for c in controllers.values() if c["skin_source"] == geom_id), None)

    if skin_ctrl is not None:
        bsm = skin_ctrl.get("bind_shape_matrix", Matrix.Identity(4))
        if bsm != Matrix.Identity(4):
            bsm3 = bsm.to_3x3()
            bsm_t = bsm.to_translation()
            positions = [tuple(bsm3 @ Vector(p) + bsm_t) for p in positions]

        ibm_col_scale = skin_ctrl.get("inv_bind_col_scale", 1.0)
        if abs(ibm_col_scale - 1.0) > 0.001:
            s     = ibm_col_scale
            ibm_R = skin_ctrl.get("inv_bind_R", None)
            if ibm_R is not None:
                positions = [tuple(ibm_R @ (Vector(p) * (1.0/s))) for p in positions]
            else:
                positions = [tuple(x / s for x in p) for p in positions]

    # Apply up-axis correction to non-rigged meshes (e.g. Y_UP map geometry).
    # Rigged meshes are already handled via bind_shape_matrix above.
    if skin_ctrl is None and correction_mat is not None:
        corr3 = correction_mat.to_3x3()
        positions    = [tuple(corr3 @ Vector(p)) for p in positions]
        corner_norms = [tuple(corr3 @ Vector(n)) for n in corner_norms]

    mesh = bpy.data.meshes.new(geom_name)
    mesh.from_pydata([Vector(p) for p in positions], [], faces)
    mesh.update(calc_edges=True)

    obj = bpy.data.objects.new(geom_name, mesh)
    collection.objects.link(obj)

    # ── MATERIALS ────────────────────────────────────────────────────────────
    dae_dir = os.path.dirname(bpy.path.abspath(dae_filepath))

    def _resolve_tex(raw_path):
        if not raw_path:
            return None
        fname = os.path.basename(raw_path)
        candidates = [raw_path, os.path.join(dae_dir, raw_path), os.path.join(dae_dir, fname)]
        parent = os.path.dirname(dae_dir)
        for _ in range(2):
            candidates.append(os.path.join(parent, fname))
            try:
                for sub in os.listdir(parent):
                    candidates.append(os.path.join(parent, sub, fname))
            except Exception:
                pass
            parent = os.path.dirname(parent)
        for c in candidates:
            c = os.path.normpath(c)
            if os.path.isfile(c):
                return c
        return None

    def _load_img(raw_path):
        resolved = _resolve_tex(raw_path)
        if not resolved:
            return None
        try:
            img = bpy.data.images.load(resolved, check_existing=True)
            img.colorspace_settings.name = "sRGB"
            return img
        except Exception as e:
            print(f"Failed to load texture '{resolved}': {e}")
            return None

    def _mat_diffuse_path(m):
        if not m.use_nodes:
            return None
        for n in m.node_tree.nodes:
            if n.type == 'TEX_IMAGE' and n.image and n.label == "diffuse":
                return os.path.normpath(bpy.path.abspath(n.image.filepath))
        return None

    def _build_mat_nodes(m, channels):
        """
        Exactly matches what the Blender 3.5 screenshot shows:
          TexImage (sRGB) → Principled BSDF Base Color → Material Output
        Alpha is NOT connected. No other nodes. Works in Blender 3.x, 4.x and 5.x.
        """
        m.use_nodes = True
        nodes = m.node_tree.nodes
        links = m.node_tree.links
        nodes.clear()

        out_n  = nodes.new("ShaderNodeOutputMaterial")
        out_n.location = (600, 0)

        bsdf_n = nodes.new("ShaderNodeBsdfPrincipled")
        bsdf_n.location = (200, 0)
        links.new(bsdf_n.outputs["BSDF"], out_n.inputs["Surface"])

        diff_path = channels.get("diffuse")
        if not diff_path:
            return

        img = _load_img(diff_path)
        if not img:
            return

        tex_n          = nodes.new("ShaderNodeTexImage")
        tex_n.image    = img
        tex_n.label    = "diffuse"
        tex_n.location = (-300, 0)

        # Color → Base Color only. Alpha is intentionally NOT connected.
        links.new(tex_n.outputs["Color"], bsdf_n.inputs["Base Color"])

        # Specular = 0.0 so Blender doesn't add extra specularity on top of
        # the baked lighting that is already embedded in the texture.
        # Every other value stays at Blender's default (matching the screenshot).
        for spec_input in ("Specular", "Specular IOR Level"):
            if spec_input in bsdf_n.inputs:
                bsdf_n.inputs[spec_input].default_value = 0.0
                break

    unique_mat_ids = sorted({m for m in face_mat_ids if m is not None})
    mat_index_map  = {}
    obj.data.materials.clear()

    for idx, mat_id in enumerate(unique_mat_ids):
        channels  = material_texture_map.get(mat_id, {})
        diff_path = _resolve_tex(channels.get("diffuse"))
        tex_base  = os.path.splitext(os.path.basename(diff_path))[0] if diff_path else mat_id

        existing   = bpy.data.materials.get(tex_base)
        want_path  = os.path.normpath(diff_path) if diff_path else None
        exist_path = _mat_diffuse_path(existing) if existing is not None else None

        if existing is not None and exist_path is not None and exist_path == want_path:
            mat = existing
        else:
            mat = bpy.data.materials.new(tex_base) if existing is None else existing
            _build_mat_nodes(mat, channels)
            if diff_path:
                print(f"Material: '{mat.name}' <- '{os.path.basename(diff_path)}'")
            else:
                print(f"Material: '{mat.name}' (no diffuse)")

        obj.data.materials.append(mat)
        mat_index_map[mat_id] = idx

    for poly, mat_id in zip(mesh.polygons, face_mat_ids):
        if mat_id and mat_id in mat_index_map:
            poly.material_index = mat_index_map[mat_id]

    # ── UVs ──────────────────────────────────────────────────────────────────
    if import_uvs and corner_uvs and len(corner_uvs) == len(mesh.loops):
        uv_layer = mesh.uv_layers.new(name="UVMap")
        for li, uv in enumerate(corner_uvs):
            uv_layer.data[li].uv = uv

    # ── VERTEX COLORS ────────────────────────────────────────────────────────
    if import_vertex_colors and corner_cols and len(corner_cols) == len(mesh.loops):
        col_attr = mesh.color_attributes.new(name="Col", type="FLOAT_COLOR", domain="CORNER")
        for li, col in enumerate(corner_cols):
            col_attr.data[li].color = tuple(max(0.0, min(1.0, c)) for c in col)

    # ── NORMALS ──────────────────────────────────────────────────────────────
    if import_normals and corner_norms and len(corner_norms) == len(mesh.loops):
        try:
            mesh.normals_split_custom_set(corner_norms)
        except Exception:
            pass

    # ── REMOVE STRAY VERTICES ────────────────────────────────────────────────
    referenced = set(v for f in faces for v in f)
    if len(referenced) < len(positions):
        import bmesh as _bm
        bm2 = _bm.new()
        bm2.from_mesh(mesh)
        stray = [v for v in bm2.verts if not v.link_edges]
        if stray:
            _bm.ops.delete(bm2, geom=stray, context='VERTS')
            bm2.to_mesh(mesh)
            mesh.update()
        bm2.free()

    # ── MERGE VERTICES ───────────────────────────────────────────────────────
    if merge_vertices:
        import bmesh
        bm = bmesh.new()
        bm.from_mesh(mesh)
        bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=merge_threshold)
        bm.to_mesh(mesh)
        bm.free()
        mesh.update()

    # ── SKIN WEIGHTS ─────────────────────────────────────────────────────────
    if arm_obj is not None and skin_ctrl is not None and skin_ctrl["vertex_weights"]:
        joint_names    = skin_ctrl["joint_names"]
        vertex_weights = skin_ctrl["vertex_weights"]
        vgroups = {}
        for jname in joint_names:
            if jname.lower().startswith("notabone"):
                vgroups[jname] = None
            else:
                vgroups[jname] = obj.vertex_groups.new(name=jname)
        for vert_idx, pairs in vertex_weights.items():
            for j_idx, weight in pairs:
                if j_idx < 0 or j_idx >= len(joint_names) or weight <= 0.0:
                    continue
                vg = vgroups.get(joint_names[j_idx])
                if vg is not None:
                    vg.add([vert_idx], weight, 'ADD')
        obj.parent = arm_obj
        mod = obj.modifiers.new(name="Armature", type='ARMATURE')
        mod.object            = arm_obj
        mod.use_vertex_groups = True
        print(f"Skin weights applied to '{geom_name}' ({len(vgroups)} groups).")
    elif arm_obj is not None:
        obj.parent      = arm_obj
        obj.parent_type = 'OBJECT'

    return obj


# ── IMPORT OPERATOR ──────────────────────────────────────────────────────────

class IMPORT_OT_simple_collada_full(Operator, ImportHelper):
    """Import a COLLADA (.dae) file"""
    bl_idname    = "import_scene.simple_collada_full"
    bl_label     = "Import Simple COLLADA (.dae)"
    filename_ext = ".dae"
    filter_glob: StringProperty(default="*.dae", options={'HIDDEN'})

    import_rig: BoolProperty(
        name="Import Rig",
        description="Import armature and skin weights if present",
        default=True,
    )
    import_materials: BoolProperty(
        name="Import Materials",
        description="Load textures and build material node graphs",
        default=True,
    )
    import_normals: BoolProperty(
        name="Import Normals",
        description="Use custom split normals from the DAE file",
        default=True,
    )
    import_uvs: BoolProperty(
        name="Import UVs",
        description="Import texture coordinate data",
        default=True,
    )
    import_vertex_colors: BoolProperty(
        name="Import Vertex Colors",
        description="Import vertex color data if present",
        default=True,
    )
    merge_vertices: BoolProperty(
        name="Merge Vertices",
        description="Remove duplicate vertices by distance after import",
        default=False,
    )
    merge_threshold: FloatProperty(
        name="Merge Distance",
        default=0.0001, min=0.0, max=0.1, precision=5,
    )

    def execute(self, context):
        self._prescan()

        if not os.path.isfile(self.filepath):
            self.report({'ERROR'}, f"File not found: {self.filepath}")
            return {'CANCELLED'}

        try:
            tree = ET.parse(self.filepath)
            root = tree.getroot()
        except ET.ParseError as e:
            try:
                import re
                with open(self.filepath, 'r', encoding='utf-8', errors='replace') as f:
                    raw = f.read()
                raw = re.sub(r'<\w+:\w+[^>]*/>', '', raw)
                raw = re.sub(r'<(\w+:\w+)[^>]*>.*?</\1>', '', raw, flags=re.DOTALL)
                raw = re.sub(r'<(\w+):(\w+)', r'<\2', raw)
                raw = re.sub(r'</(\w+):(\w+)', r'</\2', raw)
                raw = re.sub(r'\s+\w+:\w+\s*=\s*"[^"]*"', '', raw)
                raw = re.sub(r"\s+\w+:\w+\s*=\s*'[^']*'", '', raw)
                root = ET.fromstring(raw)
            except Exception as e2:
                self.report({'ERROR'}, f"Failed to parse DAE: {e} / {e2}")
                return {'CANCELLED'}
        except Exception as e:
            self.report({'ERROR'}, f"Failed to parse DAE: {e}")
            return {'CANCELLED'}

        ns  = get_collada_ns(root)
        dae = self.filepath

        if context.view_layer.active_layer_collection:
            collection = context.view_layer.active_layer_collection.collection
        else:
            collection = context.scene.collection

        profile        = analyse_dae(root, ns)
        is_rigged      = profile["is_rigged"]
        is_assembly    = profile["is_assembly"]
        correction_mat = get_up_axis_matrix(root, ns)

        material_texture_map = extract_material_texture_map(root, ns) if self.import_materials else {}
        model_name           = os.path.splitext(os.path.basename(dae))[0]

        arm_obj     = None
        controllers = {}
        if self.import_rig and is_rigged:
            arm_obj     = build_armature(root, ns, collection, model_name, correction_mat)
            controllers = parse_controllers(root, ns)
        elif self.import_rig and not is_rigged:
            print("[DAE] No rig found — skipping armature import.")

        geom_mat_override = build_ctrl_mat_map(root, ns, controllers)

        geom_world_mat = {}
        if is_assembly:
            def _node_mat(node):
                m = node.find(q(ns, "matrix"))
                return parse_matrix(m.text) if (m is not None and m.text) else Matrix.Identity(4)

            def _walk(node, parent_mat):
                world = parent_mat @ _node_mat(node)
                ig = node.find(q(ns, "instance_geometry"))
                if ig is not None:
                    gid = ig.attrib.get("url","")[1:]
                    geom_world_mat.setdefault(gid, world)
                for inn in node.findall(q(ns, "instance_node")):
                    nid = inn.attrib.get("url","").lstrip("#")
                    lib = root.find(q(ns, "library_nodes"))
                    if lib is not None:
                        tgt = lib.find(f".//{q(ns,'node')}[@id='{nid}']")
                        if tgt is not None:
                            _walk(tgt, world)
                for child in node.findall(q(ns, "node")):
                    _walk(child, world)

            vs = root.find(f".//{q(ns,'visual_scene')}")
            if vs is not None:
                for node in vs.findall(q(ns, "node")):
                    _walk(node, Matrix.Identity(4))

        geometries = root.findall(f".//{q(ns,'geometry')}")
        if not geometries:
            self.report({'ERROR'}, "No <geometry> found in DAE")
            return {'CANCELLED'}

        imported = 0
        for geom in geometries:
            geom_id      = geom.attrib.get("id", "")
            mat_override = geom_mat_override.get(geom_id, {})
            obj = build_mesh_from_geometry(
                geom, ns, collection, material_texture_map,
                arm_obj, controllers, mat_override, dae,
                import_uvs=self.import_uvs,
                import_normals=self.import_normals,
                import_vertex_colors=self.import_vertex_colors,
                merge_vertices=self.merge_vertices,
                merge_threshold=self.merge_threshold,
                correction_mat=correction_mat,
            )
            if obj:
                if geom_id in geom_world_mat:
                    obj.matrix_world = geom_world_mat[geom_id]
                imported += 1

        if imported == 0:
            self.report({'ERROR'}, "No objects created. Check console.")
            return {'CANCELLED'}

        rig_msg = f" + armature ({arm_obj.name})" if arm_obj else ""
        self.report({'INFO'}, f"Imported {imported} object(s){rig_msg}.")
        return {'FINISHED'}

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def _prescan(self):
        if not self.filepath or not os.path.isfile(self.filepath):
            return
        try:
            import re as _re
            try:
                _tree = ET.parse(self.filepath)
                _root = _tree.getroot()
            except ET.ParseError:
                with open(self.filepath, 'r', encoding='utf-8', errors='replace') as f:
                    _raw = f.read()
                _raw = _re.sub(r'<(\w+:\w+)[^>]*>.*?</\1>', '', _raw, flags=_re.DOTALL)
                _raw = _re.sub(r'<(\w+):(\w+)', r'<\2', _raw)
                _raw = _re.sub(r'</(\w+):(\w+)', r'</\2', _raw)
                _root = ET.fromstring(_raw)
            _ns = get_collada_ns(_root)
            _profile = analyse_dae(_root, _ns)
            self.import_rig = _profile["is_rigged"]
            _img_lib = _root.find(f"{_ns}library_images" if _ns else "library_images")
            self.import_materials = _img_lib is not None and len(list(_img_lib)) > 0
            self._profile_summary = (
                f"Joints: {_profile['joint_count']}  "
                f"Controllers: {_profile['controller_count']}  "
                f"Up: {_profile['up_axis']}  "
                f"Unit: {_profile['unit_meter']}m  "
                f"Assembly: {_profile['is_assembly']}  "
                f"Anims: {_profile['has_anims']}"
            )
        except Exception as e:
            print(f"[DAE pre-scan failed: {e}]")

    def draw(self, context):
        layout = self.layout
        if hasattr(self, '_profile_summary'):
            box = layout.box()
            box.label(text="Detected:", icon='INFO')
            parts = self._profile_summary.split("  ")
            box.label(text="  ".join(parts[:3]))
            box.label(text="  ".join(parts[3:]))
            layout.separator()
        layout.label(text="Mesh")
        layout.prop(self, "import_uvs")
        layout.prop(self, "import_normals")
        layout.prop(self, "import_vertex_colors")
        layout.prop(self, "merge_vertices")
        if self.merge_vertices:
            layout.prop(self, "merge_threshold")
        layout.separator()
        layout.label(text="Materials")
        layout.prop(self, "import_materials")
        layout.separator()
        layout.label(text="Rig")
        layout.prop(self, "import_rig")


# ── TEXTURE ASSIGN OPERATOR ──────────────────────────────────────────────────

class OBJECT_OT_assign_textures_by_name(Operator):
    """Assign textures by matching material names to image filenames"""
    bl_idname  = "object.assign_textures_by_name"
    bl_label   = "Assign Textures by Name"
    bl_options = {'REGISTER', 'UNDO'}

    directory: StringProperty(
        name="Texture Folder", description="Folder containing texture images", subtype='DIR_PATH'
    )

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        folder = bpy.path.abspath(self.directory)
        if not os.path.isdir(folder):
            self.report({'ERROR'}, f"Not a directory: {folder}")
            return {'CANCELLED'}

        exts   = {".png", ".jpg", ".jpeg", ".tga", ".bmp", ".tif", ".tiff", ".dds"}
        images = {}
        for f in os.listdir(folder):
            name, ext = os.path.splitext(f)
            if ext.lower() in exts:
                try:
                    img = bpy.data.images.load(os.path.join(folder, f), check_existing=True)
                    images[name] = img
                except Exception:
                    pass

        assigned = 0
        for obj in context.selected_objects:
            if not hasattr(obj.data, "materials"):
                continue
            for mat in obj.data.materials:
                if not mat or str(mat.name).strip() not in images:
                    continue
                img = images[str(mat.name).strip()]
                mat.use_nodes = True
                nodes = mat.node_tree.nodes
                links = mat.node_tree.links
                while nodes:
                    nodes.remove(nodes[0])
                out_n  = nodes.new("ShaderNodeOutputMaterial"); out_n.location  = (600, 0)
                bsdf_n = nodes.new("ShaderNodeBsdfPrincipled"); bsdf_n.location = (200, 0)
                tex_n  = nodes.new("ShaderNodeTexImage");       tex_n.location  = (-300, 0)
                tex_n.image = img
                links.new(tex_n.outputs["Color"], bsdf_n.inputs["Base Color"])
                links.new(bsdf_n.outputs["BSDF"],  out_n.inputs["Surface"])
                assigned += 1

        self.report({'INFO'}, f"Assigned textures to {assigned} materials.")
        return {'FINISHED'}


# ── MENUS & REGISTER ─────────────────────────────────────────────────────────

def menu_func_import(self, context):
    self.layout.operator(IMPORT_OT_simple_collada_full.bl_idname, text="Simple COLLADA (.dae)")

def menu_func_assign_textures(self, context):
    self.layout.operator(OBJECT_OT_assign_textures_by_name.bl_idname, text="Assign Textures by Name")

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
