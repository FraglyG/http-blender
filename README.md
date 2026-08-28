# http-blender

An HTTP interface to a headless Blender, built for an AI agent to work in rather than to fire
one-shot scripts at.

The service holds **sessions**: a session is a `.blend` on disk that survives between calls, so an
agent can import a model, change it, look at it, change it again, and export when it is happy —
instead of regenerating everything from a single script every time and never seeing the result.

## Why it looks like this

The previous version accepted a script, wiped the scene, ran it, and streamed back one file. That
shape ruled out most real work:

- **it could not see** — no renders, so an agent had no way to tell whether it built the right thing;
- **it could not explain** — a Python exception came back as `Blender ran but produced no output file`, with no traceback, so nothing could be debugged or self-corrected;
- **it could not remember** — the scene was wiped per request, so every refinement was a from-scratch rebuild;
- **it could not load anything** — no import, so "edit this model" was impossible;
- **it could not keep a rig** — export selected `MESH` objects only, so armatures, cameras and lights were dropped on the way out;
- **it lost materials** — the OBJ exporter writes a companion `.mtl` that was never sent, so every OBJ referenced a file the caller did not have.

Each endpoint below exists to close one of those.

## Endpoints

All requests and responses are JSON unless noted. Set `BLENDER_API_TOKEN` to require
`Authorization: Bearer <token>`.

### `GET /health`
Liveness plus queue depth.

### `GET /capabilities`
Supported formats, engines, view names and limits. Reported by the server so a caller's prompt
cannot drift from what is actually installed.

### `POST /session` → `{ sessionId }`
Start an empty session. Most endpoints will create one implicitly if you omit `sessionId`.

### `GET /session/:id`
Scene summary without running anything.

### `DELETE /session/:id`

### `POST /run`
```jsonc
{
  "sessionId": "…",          // optional; omit to start fresh
  "code": "import bpy\n…",   // bpy script, run against the session's current scene
  "preview": { "views": ["iso", "front"], "resolution": 512 },  // optional
  "timeoutMs": 120000
}
```
Returns:
```jsonc
{
  "ok": false,
  "sessionId": "…",
  "stdout": "…",             // everything the script printed
  "error": {                 // null when ok
    "type": "KeyError",
    "message": "bpy_prop_collection[key]: key \"Nope\" not found",
    "line": 3,               // line in YOUR script
    "text": "bpy.data.objects[\"Nope\"].location.z = 1",
    "traceback": "…"
  },
  "created": ["Cube"],       // objects the script added
  "scene": { … },            // full summary: counts, tris, bounds, per-object detail
  "preview": { "images": [{ "view": "iso", "base64": "…", "mediaType": "image/png" }] }
}
```
A script that fails partway still has its work saved and its scene reported — the half that
succeeded is usually worth keeping.

### `POST /import`
```jsonc
{ "sessionId": "…", "url": "https://…/character.fbx", "options": { "scale": 1.0 } }
```
Fetches over http(s) (private addresses refused, size-capped) and imports it. Returns the names of
the objects that arrived, split into `meshes` and `armatures`, because an agent cannot address what
it cannot name.

Formats: `glb gltf fbx obj stl ply usd usdz abc dae x3d blend`.

### `POST /preview`
Render the session from named angles without changing it. Views: `front back left right top bottom
iso iso_back iso_low`, or an explicit `[azimuth, elevation]`. Engine defaults to Workbench for
untextured scenes and EEVEE once materials exist; `CYCLES` is available when it is worth the wait.
Camera, lights and render settings are added and torn down per call, so previews never become part
of the session.

### `POST /export`
```jsonc
{ "sessionId": "…", "format": "fbx", "objects": ["Body", "Rig"], "options": { "animations": true } }
```
Streams the file back, with an `X-Blender-Export` header describing what was written. Exports
**everything** by default, not just meshes, so rigs survive. Multi-file formats (OBJ + MTL +
textures) come back as a zip rather than losing their companions.

Formats: `glb gltf fbx obj stl ply usd usdz abc blend`. FBX embeds its textures; OBJ copies them
next to the `.mtl`.

### `POST /generate` *(legacy)*
The original one-shot endpoint, kept working for existing callers. Builds in a throwaway session
and returns a single file.

## Notes for operators

- **Sessions live in `DATA_DIR` (`/data`)**, which is container-local unless a volume is mounted.
  Sessions are working state rather than deliverables, so losing them on redeploy is survivable —
  it drops in-flight work, not results. Mount a volume if that matters.
- Idle sessions are reaped after `SESSION_TTL_MS` (24h default).
- `MAX_CONCURRENCY` (default 2) caps simultaneous Blender processes. Each is a few hundred MB;
  unbounded spawning is how the container gets OOM-killed under load.

| Variable | Default | |
|---|---|---|
| `PORT` | `80` | |
| `BLENDER_BIN` | `/home/headless/blender/blender` | |
| `DATA_DIR` | `/data` | session storage |
| `MAX_CONCURRENCY` | `2` | concurrent Blender processes |
| `DEFAULT_TIMEOUT_MS` / `MAX_TIMEOUT_MS` | `120000` / `600000` | per job |
| `MAX_ASSET_BYTES` | `200MB` | import size cap |
| `SESSION_TTL_MS` | `86400000` | idle session lifetime |
| `BLENDER_API_TOKEN` | *(unset)* | enables bearer auth |
