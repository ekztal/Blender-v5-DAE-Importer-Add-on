# Blender-v5-DAE-Importer-Add-on
A lightweight Blender 5 add-on that restores support for importing .dae (COLLADA) files after the official importer was removed in version 5.

(Originally created by /u/varyingopinions on Reddit, ekztal on GitHub. Extended and reworked by MilesExilium.)

## Installation:

Download simple_collada_importer.py

Blender → Edit → Preferences → Add-ons

Click Install… → select the .py file

Enable Simple COLLADA (.dae)

Import via File → Import → Simple COLLADA (.dae)


## What's New (v0.9.5):

The original add-on imported geometry with basic material name assignment but no textures, no rig, and no skin weights. The following has been added:

+ **Texture loading**  — Automatically loads image files from the same folder as the .dae and builds a Principled BSDF node graph with albedo, AO, and normal map channels wired correctly.

+ **Polylist support** — The original only handled <triangles>. Many game model exporters use <polylist> instead; both are now supported with automatic fan-triangulation.

+ **Armature import** — Optionally imports the full bone hierarchy as a Blender armature, with correct world-space positioning. Toggle via the Import Rig checkbox in the file browser.

+ **Skin weights** — Vertex groups and an Armature modifier are created automatically, with the bind shape matrix baked in so the mesh aligns with the rig on import.

+ **Coordinate space fix** — Replaced the hardcoded 90° rotation with proper per-file handling so models land right-side up regardless of the source exporter.

+ **Normal map compatibility** — Supports both FCOLLADA (Blender) and OpenCOLLADA3dsMax (3ds Max) exporter conventions for normal map channel detection.

+ **Bad diffuse correction** — If an exporter accidentally assigned an AO or specular map to the diffuse channel, the importer automatically substitutes the correct albedo file if one is found nearby.

+ **Bone name matching** — Some exporters prefix joint node IDs with a model name while the skin controller references bones by their short name only. The importer now resolves this mismatch automatically across multiple naming conventions.
  
+ **Import options panel** — The file browser now includes a proper options panel: toggle UVs, normals, vertex colors, and materials independently.
  
+ **Merge Vertices** — Optional post-import pass to remove duplicate vertices by distance, useful for models with split seams along UV borders.

+ **Universal joint name resolution** — Added a four-strategy lookup (exact ID, exact name, space-to-underscore normalisation, and suffix matching) so rigs from any exporter bind correctly without manual edits to the DAE file.
  
+ **Unskinned mesh handling** — Meshes with no skin weights (rigid accessories, hair stubs with empty controllers) are now automatically parented to the armature object so they move with the rig.
  
+ **Placeholder bone filtering**— Exporters that write NotABone placeholder entries in skin controllers no longer corrupt vertex group assignments. Placeholders are silently skipped while real bone weights are applied correctly.
  
+ **Vertices-block normal/UV support** — Some exporters (notably Wii-era models) declare normals and UVs inside the <vertices> block rather than as separate primitive inputs. These are now read correctly, fixing flat shading on affected models.
  
+ **Missing up-axis handling** — DAE files with no <up_axis> tag now default to Z-up instead of incorrectly applying a 90° rotation.

+ **Texture search improvement** — The importer now searches parent directories and subdirectories when textures aren't found directly next to the DAE, handling nested folder structures automatically.
  
+ **Material rebuild fix** — Materials with no texture nodes are now always rebuilt rather than reused as grey placeholders.
  
+ **Bad diffuse correction expanded** — Extended to catch more exporter-specific misassignments including specular and bump maps placed in the diffuse slot.


### Notes

+ Tested on Blender 5.0
+ Skin weights require the mesh and armature to be exported together in the same .dae file
+ Normal maps on models with multiple UV channels may need to be connected manually in the shader editor

