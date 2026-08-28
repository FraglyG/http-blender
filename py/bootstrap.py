"""
Everything that happens inside Blender, driven by a job file.

One process per request. The alternative — a long-lived Blender kept warm between calls — trades
a ~1.5s startup for a process whose global state is whatever the last three scripts did to it,
and bpy offers no way to truly reset it. Sessions are persisted as .blend files instead, so state
survives without the process having to.

The job file is read from $BLENDER_JOB and the result is written to job["resultPath"] as JSON.
Nothing useful is printed to stdout: Blender writes its own noise there, and parsing a result out
of it is how you end up with a service that fails whenever a plugin logs a deprecation warning.
"""

import bpy
import json
import os
import sys
import traceback
from contextlib import redirect_stdout, redirect_stderr
from io import StringIO

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import lib_io
import lib_render
import lib_scene

MAX_STDOUT_CHARS = 20_000


def load_session(job):
    """Open the session's .blend, or start from a genuinely empty file."""
    session_file = job.get("sessionFile")

    if session_file and os.path.exists(session_file):
        bpy.ops.wm.open_mainfile(filepath=session_file)
        return False

    # `use_empty` matters: the factory scene ships a cube, a camera and a light, and a script that
    # assumes an empty world silently inherits all three.
    bpy.ops.wm.read_factory_settings(use_empty=True)
    return True


def save_session(job):
    session_file = job.get("sessionFile")
    if not session_file:
        return

    os.makedirs(os.path.dirname(session_file), exist_ok=True)

    # Pack textures before writing. A .blend stores a generated image as the recipe that made it,
    # not the pixels a script painted into it, so anything not packed here is quietly blank by the
    # time the next call in this session opens the file.
    packed, missing = lib_io.materialize_images()

    # compress: these are shipped nowhere, but a rigged character with textures is tens of MB and
    # the session directory is the thing that fills the disk.
    bpy.ops.wm.save_as_mainfile(filepath=session_file, compress=True, copy=True)
    return {"packedImages": packed, "missingImages": missing}


def run_user_code(code, capture):
    """
    Execute the agent's script, keeping its output and its failure.

    Compiled under a stable filename so the traceback carries the line numbers of the script the
    agent wrote, not offsets into some wrapper it has never seen. Being handed `line 14` for a
    script you can count to 14 in is the difference between a fix and another blind guess.
    """
    scope = {"__name__": "__main__", "bpy": bpy}

    try:
        compiled = compile(code, "<script>", "exec")
    except SyntaxError as error:
        return {
            "type": "SyntaxError",
            "message": str(error.msg),
            "line": error.lineno,
            "text": (error.text or "").rstrip(),
            "traceback": "".join(traceback.format_exception_only(type(error), error)),
        }

    try:
        with redirect_stdout(capture), redirect_stderr(capture):
            exec(compiled, scope)
        return None
    except Exception as error:  # noqa: BLE001 - the whole point is to report anything it throws
        frames = traceback.extract_tb(sys.exc_info()[2])
        user_frames = [frame for frame in frames if frame.filename == "<script>"]
        last = user_frames[-1] if user_frames else None

        # The failing source line has to be recovered from the string we were given: the code
        # never touched disk, so the traceback machinery has nothing to read it back from and
        # leaves the line blank. Quoting the offending line is most of the value of the report.
        source = code.splitlines()
        text = source[last.lineno - 1].strip() if last and 0 < last.lineno <= len(source) else None

        return {
            "type": type(error).__name__,
            "message": str(error),
            "line": last.lineno if last else None,
            "text": text,
            "traceback": "".join(traceback.format_exception(type(error), error, error.__traceback__)),
        }


def main():
    job = json.load(open(os.environ["BLENDER_JOB"]))
    params = job.get("params") or {}
    result = {"ok": True, "error": None, "stdout": "", "warnings": []}
    capture = StringIO()
    dirty = False

    try:
        created = load_session(job)
        result["sessionCreated"] = created

        op = job["op"]

        if op == "run":
            before = lib_scene.object_names()
            error = run_user_code(job["code"], capture)
            dirty = True

            if error:
                result["ok"] = False
                result["error"] = error
            else:
                result["created"] = sorted(lib_scene.object_names() - before)

        elif op == "import":
            with redirect_stdout(capture), redirect_stderr(capture):
                result["imported"] = lib_io.import_asset(
                    path=params["path"],
                    fmt=params.get("format"),
                    options=params.get("options") or {},
                )
            dirty = True

        elif op == "export":
            with redirect_stdout(capture), redirect_stderr(capture):
                result["export"] = lib_io.export_asset(
                    out_dir=params["outDir"],
                    fmt=params["format"],
                    objects=params.get("objects"),
                    options=params.get("options") or {},
                )

        elif op == "inspect":
            pass  # the scene summary below is the whole job

        else:
            raise ValueError(f"Unknown op: {op}")

        # Rendering is deliberately last: a preview of a scene that failed halfway is still the
        # fastest way to see *where* it went wrong, so previews are attached even to failures.
        if params.get("preview") is not None:
            with redirect_stdout(capture), redirect_stderr(capture):
                result["preview"] = lib_render.render_views(params.get("preview") or {})

        result["scene"] = lib_scene.summarize()

        # A failed script still leaves whatever it managed to build. Saving that is what makes
        # "the second half of my script broke, keep the first half" a recoverable position rather
        # than a full restart.
        if dirty:
            # Preview scaffolding (camera, lights) is torn down by the renderer before this point,
            # so a session never accumulates furniture it did not ask for.
            saved = save_session(job)
            if saved and saved["missingImages"]:
                result["warnings"].append({
                    "type": "missing_textures",
                    "message": "Some images have no usable pixel data and will not survive export.",
                    "images": saved["missingImages"],
                })

    except Exception as error:  # noqa: BLE001
        result["ok"] = False
        result["error"] = {
            "type": type(error).__name__,
            "message": str(error),
            "traceback": traceback.format_exc(),
            "stage": "harness",
        }

    text = capture.getvalue()
    if len(text) > MAX_STDOUT_CHARS:
        head = text[: MAX_STDOUT_CHARS // 2]
        tail = text[-MAX_STDOUT_CHARS // 2:]
        text = f"{head}\n...[{len(capture.getvalue()) - MAX_STDOUT_CHARS} chars omitted]...\n{tail}"
    result["stdout"] = text

    with open(os.environ["BLENDER_RESULT"], "w") as handle:
        json.dump(result, handle)


main()
