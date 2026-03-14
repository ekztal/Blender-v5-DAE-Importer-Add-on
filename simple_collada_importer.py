bl_info = {
    "name": "Simple COLLADA (.dae) Importer (Positions + Normals + Colors + UVs + Textures + Rig)",
    "author": "ekztal",
    "additional help": "MilesExilium",
    "version": (0, 9, 6),
    "blender": (4, 0, 0),
    "location": "File > Import > Simple COLLADA (.dae)",
    "description": "Imports COLLADA meshes with textures, armature, and skin weights.",
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
    Parse <source><float_array>...</float_array></source>
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


def parse_matrix(text):
    """Parse a 16-float COLLADA row-major matrix string into a Blender Matrix."""
    vals = [float(v) for v in text.strip().split()]
    if len(vals) != 16:
        return Matrix.Identity(4)
    return Matrix([vals[0:4], vals[4:8], vals[8:12], vals[12:16]])


def get_up_axis_matrix(root, ns):
    """
    Return a 4x4 correction Matrix to bring the DAE coordinate system into
    Blender's Z-up right-handed space.
      Z_UP: identity  (already correct)
      Y_UP: rotate +90° around X  (most exporters)
      X_UP: rotate -90° around Y
    If no <up_axis> tag is present, default to Z_UP (no correction) since
    most game-rip exporters that omit the tag are already in Z-up space.
    """
    asset = root.find(q(ns, "asset"))
    up    = asset.find(q(ns, "up_axis")) if asset is not None else None
    axis  = up.text.strip().upper() if (up is not None and up.text) else "Z_UP"
    if axis == "Z_UP":
        return Matrix.Identity(4)
    elif axis == "X_UP":
        return Matrix.Rotation(-math.pi / 2.0, 4, 'Y')
    else:   # Y_UP
        return Matrix.Rotation(math.pi / 2.0, 4, 'X')


# ---------------------- MATERIAL / TEXTURE HELPERS ----------------------

def extract_material_texture_map(root, ns):
    """
    Returns dict: material_id -> {"diffuse": path, "normal": path, "ao": path, "specular": path}
    Reads library_images -> library_effects (sampler/surface chain) -> library_materials.
    Handles both standard <diffuse> and FCOLLADA <extra><bump> for normal maps.
    """

    # 1. image_id -> file path
    image_path_for_id = {}
    for img in root.findall(f".//{q(ns,'image')}"):
        img_id = img.attrib.get("id")
        if not img_id:
            continue
        init_from = img.find(q(ns, "init_from"))
        if init_from is not None and init_from.text:
            image_path_for_id[img_id] = init_from.text.strip()

    # 2. effect_id -> {channel: file_path}
    channels_for_effect = {}
    for eff in root.findall(f".//{q(ns,'effect')}"):
        eff_id = eff.attrib.get("id")
        if not eff_id:
            continue

        # Build sampler sid -> image path lookup for THIS effect
        # (must be built as a local dict, not a closure over a loop variable)
        sid_to_image   = {}   # surface sid  -> image_id
        sid_to_surface = {}   # sampler sid  -> surface sid
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
            """Resolve texture/@texture ref -> file path, using captured dicts."""
            if tex_ref in s2surf:
                image_id = s2img.get(s2surf[tex_ref], "")
            elif tex_ref in s2img:
                image_id = s2img[tex_ref]
            else:
                image_id = tex_ref
            return image_path_for_id.get(image_id)

        channels = {}
        shininess   = 10.0   # default
        spec_color  = None

        # --- Standard phong/lambert profile_COMMON technique ---
        profile = eff.find(q(ns, "profile_COMMON"))
        if profile is not None:
            technique = profile.find(q(ns, "technique"))
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
                                elif chan_name == "transparent":
                                    channels["alpha"] = path
                                elif chan_name == "specular":
                                    channels["specular"] = path
                        # Read shininess float
                        if chan_name == "shininess":
                            fval = chan.find(q(ns, "float"))
                            if fval is not None and fval.text:
                                try: shininess = float(fval.text.strip())
                                except: pass
                        # Read specular color if no specular texture
                        if chan_name == "specular" and tex is None:
                            cval = chan.find(q(ns, "color"))
                            if cval is not None and cval.text:
                                try:
                                    rgba = [float(x) for x in cval.text.strip().split()]
                                    spec_color = rgba[:3]
                                except: pass

        # Convert phong shininess to PBR roughness.
        # shininess=1 (matte) -> roughness=0.9, shininess=100 (shiny) -> roughness=0.3
        roughness = max(0.2, min(0.95, 1.0 - (shininess / 128.0) ** 0.5))
        channels["_roughness"]  = roughness
        channels["_spec_color"] = spec_color

        # --- Extra technique blocks: FCOLLADA and OpenCOLLADA3dsMax ---
        # Both store bump/normal maps here with no namespace prefix on tags.
        # We search the whole effect tree for any <technique> with known profiles.
        for tech in eff.findall(f".//{q(ns,'technique')}") + eff.findall(".//technique"):
            profile_name = tech.attrib.get("profile", "")
            if profile_name in ("FCOLLADA", "OpenCOLLADA3dsMax", "MAX3D"):
                # <bump> -> normal map
                bump = tech.find("bump")
                if bump is not None:
                    tex = bump.find("texture")
                    if tex is not None:
                        path = resolve(tex.attrib.get("texture", ""))
                        if path:
                            channels.setdefault("normal", path)
                # <specularLevel> -> specular texture
                spec_lvl = tech.find("specularLevel")
                if spec_lvl is not None:
                    tex = spec_lvl.find("texture")
                    if tex is not None:
                        path = resolve(tex.attrib.get("texture", ""))
                        if path:
                            channels.setdefault("specular", path)

        # --- Filename-hint fallback for any textures not yet categorised ---
        all_tex_refs = [t.attrib.get("texture","") for t in eff.findall(f".//{q(ns,'texture')}")]
        all_paths    = [resolve(ref) for ref in all_tex_refs]
        all_paths    = [p for p in all_paths if p]

        for path in all_paths:
            base = os.path.basename(path).lower()
            if any(h in base for h in ("_nrm","_normal","_norm","normal_map","_nor")):
                channels.setdefault("normal", path)
            elif any(h in base for h in ("_ao","_ambient_occlusion","_occlusion")):
                channels.setdefault("ao", path)
            elif any(h in base for h in ("_alb","_albedo","_diffuse","_color","_col","_base")):
                channels.setdefault("diffuse", path)
            elif any(h in base for h in ("_spm","_spec","_specular","_roughness","_rgh")):
                channels.setdefault("specular", path)

        # Absolute last resort: first resolved texture = diffuse
        if "diffuse" not in channels and all_paths:
            channels["diffuse"] = all_paths[0]

        # Sanity-check: if diffuse is an AO/normal/specular map (bad DAE export),
        # try to substitute the _alb variant from the same directory.
        diff = channels.get("diffuse", "")
        diff_base = os.path.basename(diff).lower()
        non_albedo_hints = ("_ao", "_nrm", "_normal", "_spm", "_spec", "_bump")
        if any(h in diff_base for h in non_albedo_hints):
            for suffix in non_albedo_hints:
                if suffix in diff_base:
                    alb_name = diff_base.replace(suffix, "_alb")
                    alb_path = os.path.join(os.path.dirname(diff), alb_name)
                    if os.path.isfile(alb_path):
                        channels["diffuse"] = alb_path
                    break

        if channels:
            channels_for_effect[eff_id] = channels

    # 3. material_id -> effect_id
    material_to_effect = {}
    for mat in root.findall(f".//{q(ns,'material')}"):
        mat_id = mat.attrib.get("id")
        if not mat_id:
            continue
        inst = mat.find(f"./{q(ns,'instance_effect')}")
        if inst is not None:
            eff_url = inst.attrib.get("url", "")[1:]
            material_to_effect[mat_id] = eff_url

    # 4. final map: mat_id -> channel dict
    mat_to_textures = {}
    for mat_id, eff_id in material_to_effect.items():
        if eff_id in channels_for_effect:
            mat_to_textures[mat_id] = channels_for_effect[eff_id]

    return mat_to_textures


# ---------------------- ARMATURE BUILDER ----------------------

def build_armature(root, ns, collection, model_name="Armature", correction_mat=None):
    """
    Parse joint hierarchy from <library_visual_scenes> and create a Blender Armature.

    Bone world positions are derived from the INV_BIND matrices in library_controllers.
    This is the authoritative approach: inv_bind[i] = inverse of the bone's world
    transform in the skeleton's bind pose. Inverting it gives exact bone positions,
    completely immune to armature node matrix confusion or exporter quirks.

    The armature object stays at identity. Mesh vertices are transformed by BSM only.
    Returns (armature_object, bsm_per_geom_dict) or (None, {}).
    """
    vs = root.find(f".//{q(ns,'visual_scene')}")
    if vs is None:
        return None, {}

    # --- Collect inv_bind matrices from all skin controllers ---
    # joint_id -> 4x4 Matrix (bind-pose world transform = inv of inv_bind)
    joint_bind_world = {}   # joint_id -> world Matrix in bind pose
    joint_bsm        = {}   # geom_id  -> bind_shape_matrix

    ctrl_lib = root.find(f".//{q(ns,'library_controllers')}")
    if ctrl_lib is not None:
        for ctrl in ctrl_lib.findall(q(ns, "controller")):
            skin = ctrl.find(q(ns, "skin"))
            if skin is None:
                continue
            geom_id = skin.attrib.get("source", "")[1:]

            # bind_shape_matrix for this skin
            bsm_elem = skin.find(q(ns, "bind_shape_matrix"))
            bsm = parse_matrix(bsm_elem.text) if (bsm_elem is not None and bsm_elem.text) else Matrix.Identity(4)
            joint_bsm[geom_id] = bsm

            # Find joint names and inv_bind sources
            joints_elem = skin.find(q(ns, "joints"))
            if joints_elem is None:
                continue
            jnames_src = ibm_src = None
            for inp in joints_elem.findall(q(ns, "input")):
                sem = inp.attrib.get("semantic", "")
                src = inp.attrib.get("source", "")[1:]
                if sem == "JOINT":           jnames_src = src
                elif sem == "INV_BIND_MATRIX": ibm_src  = src

            sources = {}
            for src in skin.findall(q(ns, "source")):
                sid = src.attrib.get("id", "")
                na  = src.find(q(ns, "Name_array"))
                fa  = src.find(q(ns, "float_array"))
                if na is not None and na.text:   sources[sid] = na.text.strip().split()
                elif fa is not None and fa.text: sources[sid] = [float(x) for x in fa.text.strip().split()]

            jnames     = sources.get(jnames_src, [])
            ibm_floats = sources.get(ibm_src, [])
            for i, jname in enumerate(jnames):
                if jname in joint_bind_world:
                    continue  # already have it from another controller
                start = i * 16
                if start + 16 > len(ibm_floats):
                    continue
                inv_bind = Matrix([ ibm_floats[start:start+4],
                                    ibm_floats[start+4:start+8],
                                    ibm_floats[start+8:start+12],
                                    ibm_floats[start+12:start+16] ])
                # bind_world = inverse of inv_bind = bone's world transform at bind pose
                try:
                    joint_bind_world[jname] = inv_bind.inverted()
                except Exception:
                    joint_bind_world[jname] = Matrix.Identity(4)

    if not joint_bind_world:
        return None, {}

    # --- Walk visual scene to get bone hierarchy and names ---
    # bone_info keyed by node_id; we also build a name->id reverse map
    bone_info    = {}   # joint_id -> {name, parent_id}
    name_to_id   = {}   # joint name attribute -> joint_id

    def walk_joints(node, parent_id):
        node_id   = node.attrib.get("id",   "")
        node_name = node.attrib.get("name", node_id)
        node_type = node.attrib.get("type", "")
        if node_type == "JOINT" and node_id:
            # Normalise name: replace spaces with underscores so it matches skin references
            node_name_normalised = node_name.replace(" ", "_")
            bone_info[node_id] = {"name": node_name_normalised, "parent_id": parent_id}
            name_to_id[node_name] = node_id
            name_to_id[node_name_normalised] = node_id
            for child in node.findall(q(ns, "node")):
                walk_joints(child, node_id)
        else:
            for child in node.findall(q(ns, "node")):
                walk_joints(child, parent_id)

    for node in vs.findall(q(ns, "node")):
        walk_joints(node, None)

    # --- Resolve joint_bind_world keys to node IDs ---
    # Different exporters use different conventions for joint references in the skin:
    #   Strategy 1: skin ref == node id exactly              (some exporters)
    #   Strategy 2: skin ref == node name exactly            (Dio-style)
    #   Strategy 3: skin ref == node name with spaces→_      (Jagi-style)
    #   Strategy 4: skin ref == node id suffix after prefix  (Link/Armor-style, e.g.
    #               node id "_0010201_model_JNT_Hips", skin ref "JNT_Hips")
    # We build lookup tables for all strategies and remap everything to node_id keys.

    # Lookup tables
    id_to_id     = {jid: jid for jid in bone_info}
    name_to_id   = {}
    norm_to_id   = {}   # name with spaces→underscores
    suffix_to_id = {}   # last component after splitting on _ or space

    for jid, info in bone_info.items():
        raw_name  = next((n.attrib.get("name","") for n in
                          vs.findall(f".//{q(ns,'node')}[@id='{jid}']")), info["name"])
        norm_name = info["name"]   # already normalised with underscores
        name_to_id[raw_name]   = jid
        name_to_id[norm_name]  = jid
        norm_to_id[norm_name]  = jid
        # Suffix: try progressively shorter suffixes of the node id
        parts = jid.replace("-","_").split("_")
        for i in range(len(parts)):
            suffix = "_".join(parts[i:])
            if suffix and suffix not in suffix_to_id:
                suffix_to_id[suffix] = jid

    def resolve_skin_ref(ref):
        """Map a skin Name_array entry to a node_id using all strategies."""
        ref_norm = ref.replace(" ", "_")
        return (id_to_id.get(ref)
             or name_to_id.get(ref)
             or norm_to_id.get(ref_norm)
             or suffix_to_id.get(ref)
             or suffix_to_id.get(ref_norm)
             or None)

    # Remap joint_bind_world so all keys are node_ids
    remapped = {}
    for skin_ref, world_mat in joint_bind_world.items():
        jid = resolve_skin_ref(skin_ref)
        if jid:
            remapped[jid] = world_mat
        else:
            remapped[skin_ref] = world_mat   # keep as-is, may already be a node_id
    joint_bind_world = remapped

    # Also remap parse_controllers joint_names to bone display names for vertex groups
    # Store the resolve function result in a module-level accessible way via closure
    _resolve_skin_ref = resolve_skin_ref

    # Create Armature object at identity
    arm_data = bpy.data.armatures.new(model_name)
    arm_data.display_type = 'OCTAHEDRAL'
    arm_obj  = bpy.data.objects.new(model_name, arm_data)
    collection.objects.link(arm_obj)

    bpy.context.view_layer.objects.active = arm_obj
    bpy.ops.object.mode_set(mode='EDIT')

    edit_bones = arm_data.edit_bones
    created    = {}   # joint_id -> EditBone

    for bid, info in bone_info.items():
        # Use inv_bind world if available, otherwise skip (no position data)
        if bid not in joint_bind_world:
            continue
        world      = joint_bind_world[bid]
        head_world = world.to_translation()

        eb       = edit_bones.new(info["name"])
        eb.head  = head_world

        # Tail: average of child heads, or fallback to world Y axis of this bone
        children_with_pos = [c for c, ci in bone_info.items()
                             if ci["parent_id"] == bid and c in joint_bind_world]
        if children_with_pos:
            child_heads = [joint_bind_world[c].to_translation() for c in children_with_pos]
            avg_child   = sum(child_heads, Vector()) / len(child_heads)
            tail_vec    = avg_child - head_world
            length      = tail_vec.length
            eb.tail     = (head_world + tail_vec.normalized() * max(lengt