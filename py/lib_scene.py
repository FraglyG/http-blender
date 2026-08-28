"""
Telling the agent what is actually in the scene.

Without this the only feedback from a script was the bytes of an export, so an agent could not
tell an empty scene from a successful one, could not count what it had made, and could not notice
that its rig had arrived with zero bones. Introspection is cheaper than a render and answers most
questions a render would be used for.
"""

import bpy
from mathutils import Vector

MAX_OBJECTS = 120
MAX_BONE_NAMES = 80
MAX_MATERIALS = 60


def r3(vector):
    return [round(float(value), 4) for value in vector]


def object_names():
    return {obj.name for obj in bpy.data.objects}


def _triangle_count(mesh):
    # loop_triangles are only populated on demand, and calculating them on a heavy scene purely to
    # report a number is not worth it; the polygon fan estimate is exact for tris and quads.
    return sum(max(len(polygon.vertices) - 2, 1) for polygon in mesh.polygons)


def _mesh_detail(obj):
    mesh = obj.data
    detail = {
        "verts": len(mesh.vertices),
        "faces": len(mesh.polygons),
        "tris": _triangle_count(mesh),
        "materials": [slot.material.name for slot in obj.material_slots if slot.material],
        "uvMaps": [layer.name for layer in mesh.uv_layers],
        "modifiers": [{"name": mod.name, "type": mod.type} for mod in obj.modifiers],
        "vertexGroups": len(obj.vertex_groups),
    }

    if mesh.shape_keys:
        detail["shapeKeys"] = [key.name for key in mesh.shape_keys.key_blocks]

    # The single most useful fact about a mesh that is supposed to be rigged: whether it is bound
    # to anything. A character that exports as a statue almost always failed right here.
    armature = next((mod for mod in obj.modifiers if mod.type == "ARMATURE"), None)
    if armature:
        detail["boundTo"] = armature.object.name if armature.object else None

    return detail


def _armature_detail(obj):
    bones = [bone.name for bone in obj.data.bones]
    detail = {"boneCount": len(bones), "bones": bones[:MAX_BONE_NAMES]}
    if len(bones) > MAX_BONE_NAMES:
        detail["bonesTruncated"] = len(bones) - MAX_BONE_NAMES
    return detail


def _world_bounds(objects):
    points = []
    for obj in objects:
        if obj.type not in {"MESH", "CURVE", "SURFACE", "FONT", "META"}:
            continue
        points.extend(obj.matrix_world @ Vector(corner) for corner in obj.bound_box)

    if not points:
        return None

    lo = [min(point[axis] for point in points) for axis in range(3)]
    hi = [max(point[axis] for point in points) for axis in range(3)]
    return {
        "min": [round(value, 4) for value in lo],
        "max": [round(value, 4) for value in hi],
        "size": [round(hi[axis] - lo[axis], 4) for axis in range(3)],
    }


def summarize():
    # `dimensions`, and anything else derived from a transform, is only correct after the
    # dependency graph catches up. Reporting pre-update values is worse than reporting none: a
    # script that scales an object would be told, convincingly, that nothing happened.
    bpy.context.view_layer.update()

    objects = list(bpy.data.objects)
    counts = {}
    for obj in objects:
        counts[obj.type] = counts.get(obj.type, 0) + 1

    listed = []
    for obj in objects[:MAX_OBJECTS]:
        entry = {
            "name": obj.name,
            "type": obj.type,
            "location": r3(obj.location),
            "dimensions": r3(obj.dimensions),
            "visible": not obj.hide_render,
        }
        if obj.parent:
            entry["parent"] = obj.parent.name
            entry["parentType"] = obj.parent_type

        if obj.type == "MESH":
            entry.update(_mesh_detail(obj))
        elif obj.type == "ARMATURE":
            entry.update(_armature_detail(obj))

        listed.append(entry)

    total_tris = sum(_triangle_count(obj.data) for obj in objects if obj.type == "MESH")

    images = [
        {
            "name": image.name,
            "size": list(image.size),
            "packed": bool(image.packed_file),
            # An unpacked image with a path that no longer exists is the classic reason a model
            # exports untextured, and it is invisible unless something says so out loud.
            "missing": bool(image.filepath and not image.has_data),
        }
        for image in bpy.data.images
        if image.name != "Render Result"
    ]

    summary = {
        "objectCount": len(objects),
        "counts": counts,
        "totalTris": total_tris,
        "objects": listed,
        "materials": [material.name for material in bpy.data.materials][:MAX_MATERIALS],
        "images": images,
        "bounds": _world_bounds(objects),
        "frameRange": [bpy.context.scene.frame_start, bpy.context.scene.frame_end],
        "actions": [action.name for action in bpy.data.actions],
    }

    if len(objects) > MAX_OBJECTS:
        summary["objectsTruncated"] = len(objects) - MAX_OBJECTS

    return summary
