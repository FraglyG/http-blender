import express from "express";
import { execFile } from "child_process";
import { randomUUID } from "crypto";
import { writeFile, unlink, access } from "fs/promises";
import { createReadStream } from "fs";
import path from "path";
import os from "os";

const app = express();
app.use(express.json());

const WORK_DIR = path.join(os.tmpdir(), "blender_tasks");

const EXPORT_FOOTER = (outputPath, format) => {
    if (format === "glb") {
        return `
import bpy
bpy.ops.object.select_all(action='DESELECT')
for obj in bpy.data.objects:
    if obj.type == 'MESH':
        obj.select_set(True)
bpy.ops.export_scene.gltf(
    filepath=${JSON.stringify(outputPath)},
    export_format='GLB',
    use_selection=True
)
`;
    }
    return `
import bpy
bpy.ops.object.select_all(action='DESELECT')
for obj in bpy.data.objects:
    if obj.type == 'MESH':
        obj.select_set(True)
bpy.ops.wm.obj_export(filepath=${JSON.stringify(outputPath)}, export_selected_objects=True)
`;
};

function detectFormat(code) {
    const materialIndicators = [
        "bpy.data.materials",
        "bpy.ops.object.material_slot",
        "node_tree",
        "ShaderNodeBsdf",
        "ShaderNodeTex",
        "Image.load",
        "bpy.data.images",
    ];
    return materialIndicators.some(indicator => code.includes(indicator)) ? "glb" : "obj";
}

app.get("/health", (req, res) => {
    res.json({ status: "ok" });
});

app.post("/generate", async (req, res) => {
    const { code } = req.body ?? {};
    if (!code) return res.status(400).json({ error: "Missing 'code' field" });

    const id = randomUUID();
    const scriptPath = path.join(WORK_DIR, `${id}.py`);

    const format = detectFormat(code);
    const outputPath = path.join(WORK_DIR, `${id}.${format}`);

    await writeFile(scriptPath, SCRIPT_HEADER + "\n" + code + "\n" + EXPORT_FOOTER(outputPath, format));

    execFile(
        "/home/headless/blender/blender",
        ["--background", "--python", scriptPath],
        { timeout: 60_000 },
        async (err, _stdout, stderr) => {
            await unlink(scriptPath).catch(() => { });

            if (err) {
                return res.status(500).json({ error: "Blender crashed", details: stderr });
            }

            try {
                await access(outputPath);
            } catch {
                return res.status(500).json({ error: "Blender ran but produced no output file" });
            }

            res.setHeader("Content-Disposition", `attachment; filename="model.${format}"`);
            res.setHeader("Content-Type", "application/octet-stream");

            const stream = createReadStream(outputPath);
            stream.pipe(res);
            stream.on("close", () => unlink(outputPath).catch(() => { }));
        }
    );
});

app.listen(80, () => console.log("Listening on :80"));