"""
Preview renders: the feedback loop.

An agent that cannot see its output is not iterating, it is guessing with extra steps. These
renders exist purely so the model can look at what it built and notice that the roof is inside
the house.

Everything here is temporary. The camera, the lights and the render settings are added, used and
removed, so a preview never becomes part of the session the agent is editing.
"""

import base64
import math
import os
import tempfile

import bpy
from mathutils import Vector

# Azimuth (degrees, 0 = looking along +Y at the front) and elevation.
NAMED_VIEWS = {
    "front": (0.0, 0.0),
    "back": (180.0, 0.0),
    "right": (90.0, 0.0),
    "left": (-90.0, 0.0),
    "top": (0.0, 89.0),
    "bottom": (0.0, -89.0),
    "iso": (45.0, 30.0),
    "iso_back": (215.0, 30.0),
    "iso_low": (45.0, -20.0),
}

DEFAULT_VIEWS = ["iso", "front", "right"]
MAX_VIEWS = 6
MAX_RESOLUTION = 1024
RENDERABLE = {"MESH", "CURVE", "SURFACE", "FONT", "META", "VOLUME", "GPENCIL"}


def _renderable_objects(names=None):
    objects = [
        obj for obj in bpy.data.objects
        if obj.type in RENDERABLE and not obj.hide_render
    ]
    if names:
        wanted = set(names)
        objects = [obj for obj in objects if obj.name in wanted]
    return objects


def _bounding_sphere(objects):
    points = []
    for obj in objects:
        points.extend(obj.matrix_world @ Vector(corner) for corner in obj.bound_box)

    if not points:
        return Vector((0.0, 0.0, 0.0)), 1.0

    lo = Vector((min(p.x for p in points), min(p.y for p in points), min(p.z for p in points)))
    hi = Vector((max(p.x for p in points), max(p.y for p in points), max(p.z for p in points)))
    center = (lo + hi) / 2.0
    radius = max((hi - lo).length / 2.0, 1e-3)
    return center, radius


def _direction(azimuth, elevation):
    az = math.radians(azimuth)
    el = math.radians(elevation)
    return Vector((
        math.sin(az) * math.cos(el),
        -math.cos(az) * math.cos(el),
        math.sin(el),
    ))


def _resolve_view(view):
    """A view is either a name or an explicit [azimuth, elevation]."""
    if isinstance(view, (list, tuple)) and len(view) == 2:
        return f"az{int(view[0])}_el{int(view[1])}", (float(view[0]), float(view[1]))

    key = str(view).lower()
    if key not in NAMED_VIEWS:
        raise ValueError(f"Unknown view '{view}'. Known: {', '.join(sorted(NAMED_VIEWS))}, or [azimuth, elevation].")
    return key, NAMED_VIEWS[key]


def _pick_engine(requested):
    if requested and requested.upper() != "AUTO":
        engine = requested.upper()
        if engine not in {"BLENDER_EEVEE", "BLENDER_WORKBENCH", "CYCLES"}:
            raise ValueError(f"Unknown engine '{requested}' (BLENDER_EEVEE, BLENDER_WORKBENCH, CYCLES or AUTO)")
        return engine

    # Workbench is several times faster and renders untextured geometry more legibly than EEVEE's
    # default grey; EEVEE only earns its cost once there is material work worth looking at.
    has_materials = any(
        slot.material for obj in bpy.data.objects if obj.type == "MESH" for slot in obj.material_slots
    )
    return "BLENDER_EEVEE" if has_materials else "BLENDER_WORKBENCH"


def _setup_world(scene, engine, background):
    world = bpy.data.worlds.new("preview_world")
    world.use_nodes = True
    world.node_tree.nodes["Background"].inputs[0].default_value = (*background, 1.0)
    world.node_tree.nodes["Background"].inputs[1].default_value = 1.0 if engine == "BLENDER_WORKBENCH" else 1.6
    previous = scene.world
    scene.world = world
    return previous, world


def _setup_lights(center, radius):
    """A cheap three-point rig, so EEVEE renders read as shapes rather than silhouettes."""
    created = []
    spec = [
        ("key", Vector((1.0, -1.0, 1.4)), 4.0),
        ("fill", Vector((-1.2, -0.6, 0.4)), 1.5),
        ("rim", Vector((0.0, 1.4, 0.8)), 2.5),
    ]

    for name, offset, energy in spec:
        light_data = bpy.data.lights.new(f"preview_{name}", type="SUN")
        light_data.energy = energy
        light_object = bpy.data.objects.new(f"preview_{name}", light_data)
        bpy.context.scene.collection.objects.link(light_object)
        light_object.location = center + offset.normalized() * radius * 4
        light_object.rotation_euler = (-offset).to_track_quat("-Z", "Y").to_euler()
        created.append(light_object)

    return created


def render_views(params):
    """
    Render the scene from several angles and return base64 PNGs.

    Returns a list rather than one image because a single view hides exactly the mistakes worth
    catching — an object floating behind another, a hat sitting inside a head.
    """
    views = params.get("views") or DEFAULT_VIEWS
    if len(views) > MAX_VIEWS:
        raise ValueError(f"At most {MAX_VIEWS} views per render (asked for {len(views)})")

    resolution = min(int(params.get("resolution") or 512), MAX_RESOLUTION)
    focus = params.get("focus")
    background = params.get("background") or (0.05, 0.05, 0.06)
    scene = bpy.context.scene

    targets = _renderable_objects(focus)
    if not targets:
        return {"images": [], "note": "Nothing renderable in the scene — no meshes, curves or volumes."}

    center, radius = _bounding_sphere(targets)
    engine = _pick_engine(params.get("engine"))

    # Everything from here is restored in the finally block: the session must come back out of a
    # preview exactly as it went in.
    previous = {
        "engine": scene.render.engine,
        "resolution_x": scene.render.resolution_x,
        "resolution_y": scene.render.resolution_y,
        "percentage": scene.render.resolution_percentage,
        "file_format": scene.render.image_settings.file_format,
        "filepath": scene.render.filepath,
        "camera": scene.camera,
        "film_transparent": scene.render.film_transparent,
    }

    camera_data = bpy.data.cameras.new("preview_camera")
    camera_data.lens_unit = "FOV"
    camera_data.angle = math.radians(40.0)
    camera = bpy.data.objects.new("preview_camera", camera_data)
    scene.collection.objects.link(camera)

    lights = _setup_lights(center, radius) if engine != "BLENDER_WORKBENCH" else []
    previous_world, preview_world = _setup_world(scene, engine, background)

    scene.render.engine = engine
    scene.render.resolution_x = resolution
    scene.render.resolution_y = resolution
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.film_transparent = False
    scene.camera = camera

    if engine == "CYCLES":
        scene.cycles.samples = int(params.get("samples") or 32)
        scene.cycles.use_denoising = True
    elif engine == "BLENDER_WORKBENCH":
        shading = scene.display.shading
        shading.light = "STUDIO"
        shading.color_type = "MATERIAL" if params.get("colored", True) else "OBJECT"
        shading.show_cavity = True

    # 40° FOV: distance that fits the bounding sphere, plus margin so nothing kisses the frame.
    distance = radius / math.sin(camera_data.angle / 2.0) * 1.15
    images = []
    temp_dir = tempfile.mkdtemp(prefix="preview_")

    try:
        for view in views:
            name, (azimuth, elevation) = _resolve_view(view)
            direction = _direction(azimuth, elevation)

            camera.location = center + direction * distance
            camera.rotation_euler = (-direction).to_track_quat("-Z", "Y").to_euler()

            path = os.path.join(temp_dir, f"{name}.png")
            scene.render.filepath = path
            bpy.ops.render.render(write_still=True)

            with open(path, "rb") as handle:
                images.append({
                    "view": name,
                    "azimuth": azimuth,
                    "elevation": elevation,
                    "mediaType": "image/png",
                    "base64": base64.b64encode(handle.read()).decode("ascii"),
                })
            os.unlink(path)
    finally:
        for light in lights:
            bpy.data.objects.remove(light, do_unlink=True)
        bpy.data.objects.remove(camera, do_unlink=True)
        bpy.data.cameras.remove(camera_data, do_unlink=True)
        scene.world = previous_world
        bpy.data.worlds.remove(preview_world, do_unlink=True)

        scene.render.engine = previous["engine"]
        scene.render.resolution_x = previous["resolution_x"]
        scene.render.resolution_y = previous["resolution_y"]
        scene.render.resolution_percentage = previous["percentage"]
        scene.render.image_settings.file_format = previous["file_format"]
        scene.render.filepath = previous["filepath"]
        scene.render.film_transparent = previous["film_transparent"]
        scene.camera = previous["camera"]
        os.rmdir(temp_dir)

    return {
        "engine": engine,
        "resolution": resolution,
        "images": images,
    }
