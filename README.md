# Blender-v5-DAE-Importer-Add-on
A lightweight Blender 5 add-on that restores support for importing .dae (COLLADA) files after the official importer was removed in version 5.
Originally created by /u/varyingopinions on Reddit, ekztal on GitHub. Extended and reworked by MilesExilium and RebeccaNod1.

Installation

Download simple_collada_importer.py
Blender → Edit → Preferences → Add-ons
Click Install… → select the .py file
Enable Simple COLLADA (.dae)
Import via File → Import → Simple COLLADA (.dae)


Features & Improvements

"Visual Scene Support" (v1.0.0) — Full assembly of multi-part linksets. Preserves relative positions, rotations, and scales of all prims (Translation, Rotation, Scale tags supported).

"Second Life / Firestorm Hardening" (v1.0.0) — Advanced ID scavenging to fix "Missing POSITION source" errors common in SL/Firestorm exports. Automatically handles non-standard ID prefixes for robust geometry recovery.

"Blender 5.1.0 Ready" — Updated metadata and syntax for the modern V5 extension system.

"Texture loading" — Automatically loads image files from the same folder as the .dae and builds a Principled BSDF node graph with albedo, AO, and normal map channels wired correctly.

"Polylist support" — Handles both <triangles> and <polylist> with automatic fan-triangulation.

"Armature import" — Optionally imports the full bone hierarchy as a Blender armature, with correct world-space positioning. Toggle via the Import Rig checkbox in the file browser.

"Skin weights" — Vertex groups and an Armature modifier are created automatically, with the bind shape matrix baked in so the mesh aligns with the rig on import.

"Coordinate space fix" — Proper per-file handling so models land right-side up regardless of the source exporter (Z-UP vs Y-UP).

"Normal map compatibility" — Supports both FCOLLADA (Blender) and OpenCOLLADA3dsMax (3ds Max) exporter conventions for normal map channel detection.


Notes

Tested on Blender 5.1.0 (Flatpak & Local)
Skin weights require the mesh and armature to be exported together in the same .dae file
Normal maps on models with multiple UV channels may need to be connected manually in the shader editor
