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

const EXPORT_FOOTER = (outputPath) => `
import bpy, os
bpy.ops.object.select_all(action='DESELECT')
for obj in bpy.data.objects:
    if obj.type == 'MESH':
        obj.select_set(True)
bpy.ops.export_scene.obj(filepath=${JSON.stringify(outputPath)}, use_selection=True)
`;

app.post("/generate", async (req, res) => {
    const { code } = req.body ?? {};
    if (!code) return res.status(400).json({ error: "Missing 'code' field" });

    const id = randomUUID();
    const scriptPath = path.join(WORK_DIR, `${id}.py`);
    const outputPath = path.join(WORK_DIR, `${id}.obj`);

    await writeFile(scriptPath, code + "\n" + EXPORT_FOOTER(outputPath));

    execFile(
        "blender",
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

            res.setHeader("Content-Disposition", 'attachment; filename="model.obj"');
            res.setHeader("Content-Type", "application/octet-stream");

            const stream = createReadStream(outputPath);
            stream.pipe(res);
            stream.on("close", () => unlink(outputPath).catch(() => { }));
        }
    );
});

app.listen(5000, "0.0.0.0", () => console.log("Listening on :5000"));