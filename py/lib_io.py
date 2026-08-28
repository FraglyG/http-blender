"""
Getting assets in and out.

The old service could only build from nothing and only emit meshes, which ruled out every task
shaped like "take this model and change it" and quietly destroyed every rig on the way out. Both
directions matter, and so does what comes with them: an armature without its skin weights, or an
export whose textures live at a path the recipient cannot see, is a file that technically exists
and practically does not.
"""

import os
import tempfile

import bpy

IMPORT_FORMATS = {
    ".glb": "gltf", ".gltf": "gltf",
    ".fbx": "fbx",
    ".obj": "obj",
    ".stl": "stl",
    ".ply": "ply",
    ".usd": "usd", ".usda": "usd", ".usdc": "usd", ".usdz": "usd",
    ".abc": "abc",
    ".dae": "dae",
    ".blend": "blend",
    ".x3d": "x3d",
}

EXPORT_FORMATS = {"glb", "gltf", "fbx", "obj", "stl", "ply", "usd", "usdz", "abc", "blend"}

# Textures are exported as separate files for these, so the caller has to collect a directory
# rather than a single file.
MULTI_FILE = {"obj", "gltf", "usd"}


def _select(objects):
    bpy.ops.object.select_all(action="DESELECT")
    for obj in objects:
        obj.select_set(True)
    if objects:
        bpy.context.view_layer.objects.active = objects[0]


def import_asset(path, fmt=None, options=None):
    """
    Load a file into the current scene and report what arrived.

    The returned object list is not a nicety: an agent that has just imported a character needs to
    know the names it can address, and guessing them from the filename is how scripts end up
    silently operating on nothing.
    """
    options = options or {}
    extension = os.path.splitext(path)[1].lower()
    kind = (fmt or IMPORT_FORMATS.get(extension) or "").lower()

    if not kind:
        raise ValueError(f"Unsupported import extension '{extension}'. Supported: {', '.join(sorted(set(IMPORT_FORMATS)))}")

    before = {obj.name for obj in bpy.data.objects}

    if kind == "gltf":
        bpy.ops.import_scene.gltf(filepath=path)
    elif kind == "fbx":
        bpy.ops.import_scene.fbx(
            filepath=path,
            # Off by default, despite the name being tempting. "Leaf bone" here means any bone
            # with no children, not just the synthetic `_end` bones some exporters append — so
            # enabling it deletes real terminal bones. On a character rig that is the head, the
            # hands and the feet: precisely the bones anything gets attached to.
            ignore_leaf_bones=options.get("ignoreLeafBones", False),
            automatic_bone_orientation=options.get("automaticBoneOrientation", False),
        )
    elif kind == "obj":
        bpy.ops.wm.obj_import(filepath=path)
    elif kind == "stl":
        bpy.ops.wm.stl_import(filepath=path)
    elif kind == "ply":
        bpy.ops.wm.ply_import(filepath=path)
    elif kind == "usd":
        bpy.ops.wm.usd_import(filepath=path)
    elif kind == "abc":
        bpy.ops.wm.alembic_import(filepath=path)
    elif kind == "dae":
        bpy.ops.wm.collada_import(filepath=path)
    elif kind == "x3d":
        bpy.ops.import_scene.x3d(filepath=path)
    elif kind == "blend":
        with bpy.data.libraries.load(path, link=False) as (source, target):
            target.objects = list(source.objects)
        for obj in target.objects:
            if obj is not None:
                bpy.context.scene.collection.objects.link(obj)
    else:
        raise ValueError(f"Unsupported import format '{kind}'")

    added = sorted({obj.name for obj in bpy.data.objects} - before)

    if options.get("scale"):
        factor = float(options["scale"])
        for name in added:
            obj = bpy.data.objects[name]
            if not obj.parent:
                obj.scale = [value * factor for value in obj.scale]

    return {
        "format": kind,
        "objects": added,
        "armatures": [name for name in added if bpy.data.objects[name].type == "ARMATURE"],
        "meshes": [name for name in added if bpy.data.objects[name].type == "MESH"],
    }


def materialize_images(work_dir=None):
    """
    Give every texture a real file, then pack it into the .blend.

    Two failures live here, and both are silent.

    Exporters copy textures from disk, so an image a script generated with `bpy.data.images.new`
    has pixels but no file, and `path_mode='COPY'` finds nothing to copy: the export succeeds with
    no textures on it. Worse, a generated image saved into a .blend keeps only the recipe that
    made it (size and fill colour), not the pixels a script painted into it — so the texture
    disappears at the *session* boundary too, and the agent's next call sees a material wired to
    a blank image it has no way to know was ever populated.

    Writing the pixels out and packing them fixes both. Returns what was salvaged and what was
    not, so a missing texture gets reported rather than discovered by whoever opens the file.
    """
    work_dir = work_dir or tempfile.mkdtemp(prefix="textures_")
    os.makedirs(work_dir, exist_ok=True)

    packed = []
    missing = []

    for image in bpy.data.images:
        if image.name == "Render Result" or image.packed_file:
            continue

        try:
            if not image.filepath or image.source == "GENERATED":
                safe = "".join(char if char.isalnum() or char in "-_" else "_" for char in image.name)
                target = os.path.join(work_dir, f"{safe}.png")
                image.file_format = "PNG"
                image.filepath_raw = target
                image.save()
            elif not image.has_data:
                # A path pointing at a file that is no longer there, usually because whatever the
                # asset shipped with has since been cleaned up.
                missing.append({"name": image.name, "filepath": image.filepath})
                continue

            image.pack()
            packed.append(image.name)
        except RuntimeError as error:
            missing.append({"name": image.name, "error": str(error)})

    return packed, missing


def export_asset(out_dir, fmt, objects=None, options=None):
    """
    Write the scene (or part of it) to disk.

    Exports everything by default. The previous behaviour — meshes only — is why a rigged
    character came back as an unposeable statue: the armature was simply not in the file.
    """
    options = options or {}
    kind = fmt.lower()

    if kind not in EXPORT_FORMATS:
        raise ValueError(f"Unsupported export format '{fmt}'. Supported: {', '.join(sorted(EXPORT_FORMATS))}")

    os.makedirs(out_dir, exist_ok=True)
    name = options.get("name") or "model"
    use_selection = bool(objects)

    if use_selection:
        missing = [item for item in objects if item not in bpy.data.objects]
        if missing:
            raise ValueError(f"Cannot export unknown objects: {', '.join(missing)}")
        _select([bpy.data.objects[item] for item in objects])

    # Materialise into scratch space, not the export directory: anything sitting in out_dir when
    # the export finishes is treated as part of the deliverable and zipped up with it.
    packed, missing_images = materialize_images()

    if kind in {"glb", "gltf"}:
        path = os.path.join(out_dir, f"{name}.{'glb' if kind == 'glb' else 'gltf'}")
        bpy.ops.export_scene.gltf(
            filepath=path,
            export_format="GLB" if kind == "glb" else "GLTF_SEPARATE",
            use_selection=use_selection,
            export_apply=options.get("applyModifiers", False),
            # Skinning and animation are the reason a rigged export is worth anything.
            export_skins=options.get("skins", True),
            export_animations=options.get("animations", True),
            export_yup=options.get("yUp", True),
        )

    elif kind == "fbx":
        path = os.path.join(out_dir, f"{name}.fbx")
        bpy.ops.export_scene.fbx(
            filepath=path,
            use_selection=use_selection,
            # COPY + embed is what makes a single .fbx openable somewhere else. The default
            # ('AUTO') writes paths pointing at this container's filesystem.
            path_mode="COPY",
            embed_textures=options.get("embedTextures", True),
            add_leaf_bones=False,
            bake_anim=options.get("animations", True),
            mesh_smooth_type=options.get("smoothType", "FACE"),
            apply_scale_options=options.get("applyScale", "FBX_SCALE_NONE"),
        )

    elif kind == "obj":
        path = os.path.join(out_dir, f"{name}.obj")
        bpy.ops.wm.obj_export(
            filepath=path,
            export_selected_objects=use_selection,
            export_materials=True,
            # Without COPY the .mtl points at absolute paths and the textures never travel.
            path_mode="COPY",
            apply_modifiers=options.get("applyModifiers", True),
        )

    elif kind == "stl":
        path = os.path.join(out_dir, f"{name}.stl")
        bpy.ops.wm.stl_export(filepath=path, export_selected_objects=use_selection)

    elif kind == "ply":
        path = os.path.join(out_dir, f"{name}.ply")
        bpy.ops.wm.ply_export(filepath=path, export_selected_objects=use_selection)

    elif kind in {"usd", "usdz"}:
        path = os.path.join(out_dir, f"{name}.{'usdz' if kind == 'usdz' else 'usdc'}")
        bpy.ops.wm.usd_export(filepath=path, selected_objects_only=use_selection)

    elif kind == "abc":
        path = os.path.join(out_dir, f"{name}.abc")
        bpy.ops.wm.alembic_export(filepath=path, selected=use_selection)

    elif kind == "blend":
        path = os.path.join(out_dir, f"{name}.blend")
        bpy.ops.wm.save_as_mainfile(filepath=path, compress=True, copy=True)

    if not os.path.exists(path):
        raise RuntimeError(f"{kind} export produced no file — check the scene actually contains exportable objects")

    files = sorted(os.listdir(out_dir))
    return {
        "format": kind,
        "primary": os.path.basename(path),
        "files": files,
        "multiFile": len(files) > 1,
        "bytes": os.path.getsize(path),
        "packedImages": packed,
        "missingImages": missing_images,
        "exportedObjects": objects if use_selection else [obj.name for obj in bpy.data.objects],
    }
