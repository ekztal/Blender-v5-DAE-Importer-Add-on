# Blender-v5-DAE-Importer-Add-on
A lightweight Blender 5 add-on that restores support for importing .dae (COLLADA) files after the official importer was removed in version 5.

(Originally created by /u/varyingopinions on Reddit, ekztal on GitHub. Extended and reworked by MilesExilium.)

## Installation:

Download simple_collada_importer.py

Blender → Edit → Preferences → Add-ons

Click Install… → select the .py file

Enable Simple COLLADA (.dae)

Import via File → Import → Simple COLLADA (.dae)


## What's New (v0.7.2):

The original add-on imported geometry with basic material name assignment but no textures, no rig, and no skin weights. The following has been added:

+ **Texture loading**  — Automatically loads image files from the same folder as the .dae and builds a Principled BSDF node graph with albedo, AO, and normal map channels wired correctly.

+ **Polylist support** — The original only handled <triangles>. Many game model exporters use <polylist> instead; both are now supported with automatic fan-triangulation.

+ **Armature import** — Optionally imports the full bone hierarchy as a Blender armature, with correct world-space positioning. Toggle via the Import Rig checkbox in the file browser.

+ **Skin weights** — Vertex groups and an Armature modifier are created automatically, with the bind shape matrix baked in so the mesh aligns with the rig on import.

+ **Coordinate space fix** — Replaced the hardcoded 90° rotation with proper per-file handling so models land right-side up regardless of the source exporter.

+ **Normal map compatibility** — Supports both FCOLLADA (Blender) and OpenCOLLADA3dsMax (3ds Max) exporter conventions for normal map channel detection.

+ **Bad diffuse correction** — If an exporter accidentally assigned an AO or specular map to the diffuse channel, the importer automatically substitutes the correct albedo file if one is found nearby.


### Notes

+ Tested on Blender 5.0
+ Skin weights require the mesh and armature to be exported together in the same .dae file
+ Normal maps on models with multiple UV channels may need to be connected manually in the shader editor

