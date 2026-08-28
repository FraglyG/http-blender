import archiver from "archiver";
import express from "express";
import { createReadStream } from "fs";
import { mkdtemp, rm } from "fs/promises";
import { tmpdir } from "os";
import path from "path";

import { BlenderRunner } from "./lib/blender.js";
import { fetchAsset } from "./lib/fetch_asset.js";
import { HttpError, SessionStore } from "./lib/sessions.js";

const CONFIG = {
    port: Number(process.env.PORT || 80),
    binary: process.env.BLENDER_BIN || "/home/headless/blender/blender",
    dataDir: process.env.DATA_DIR || "/data",
    concurrency: Number(process.env.MAX_CONCURRENCY || 2),
    defaultTimeoutMs: Number(process.env.DEFAULT_TIMEOUT_MS || 120_000),
    maxTimeoutMs: Number(process.env.MAX_TIMEOUT_MS || 600_000),
    maxAssetBytes: Number(process.env.MAX_ASSET_BYTES || 200 * 1024 * 1024),
    sessionTtlMs: Number(process.env.SESSION_TTL_MS || 24 * 60 * 60 * 1000),
    token: process.env.BLENDER_API_TOKEN || "",
};

const app = express();
// Renders come back as base64 and scripts can be long; the default 100kb limit rejects both.
app.use(express.json({ limit: "64mb" }));

const sessions = new SessionStore({ root: path.join(CONFIG.dataDir, "sessions"), ttlMs: CONFIG.sessionTtlMs });
const blender = new BlenderRunner({
    binary: CONFIG.binary,
    concurrency: CONFIG.concurrency,
    defaultTimeoutMs: CONFIG.defaultTimeoutMs,
    maxTimeoutMs: CONFIG.maxTimeoutMs,
});

/**
 * Optional shared-secret auth.
 *
 * The service is only published on the private network, but "only reachable from inside" is a
 * property of today's networking rather than a property of the service. Off by default so nothing
 * breaks on deploy; set BLENDER_API_TOKEN and it is enforced.
 */
app.use((req, res, next) => {
    if (!CONFIG.token || req.path === "/health") return next();
    const provided = (req.get("authorization") || "").replace(/^Bearer /i, "");
    if (provided !== CONFIG.token) return res.status(401).json({ error: "Unauthorized" });
    next();
});

const wrap = (handler) => (req, res, next) => Promise.resolve(handler(req, res)).catch(next);

app.get("/health", (_req, res) => {
    res.json({ status: "ok", blender: blender.stats });
});

/**
 * Everything an agent needs to know before its first call.
 *
 * Capabilities belong next to the thing that has them. A prompt listing supported formats drifts
 * from the server the moment either changes, and the agent is the last to find out.
 */
app.get("/capabilities", (_req, res) => {
    res.json({
        blender: "5.2",
        importFormats: ["glb", "gltf", "fbx", "obj", "stl", "ply", "usd", "usdz", "abc", "dae", "x3d", "blend"],
        exportFormats: ["glb", "gltf", "fbx", "obj", "stl", "ply", "usd", "usdz", "abc", "blend"],
        engines: ["BLENDER_EEVEE", "BLENDER_WORKBENCH", "CYCLES"],
        views: ["front", "back", "left", "right", "top", "bottom", "iso", "iso_back", "iso_low"],
        preservesRigs: true,
        maxAssetBytes: CONFIG.maxAssetBytes,
        maxTimeoutMs: CONFIG.maxTimeoutMs,
        sessionTtlMs: CONFIG.sessionTtlMs,
    });
});

app.post("/session", wrap(async (req, res) => {
    const id = await sessions.create();
    res.json({ sessionId: id, ttlMs: CONFIG.sessionTtlMs });
}));

app.get("/session/:id", wrap(async (req, res) => {
    const { id } = await sessions.resolve(req.params.id);
    const result = await blender.run({ op: "inspect", sessionFile: sessions.statePath(id), params: {} });
    res.json({ sessionId: id, ...result });
}));

app.delete("/session/:id", wrap(async (req, res) => {
    await sessions.destroy(req.params.id);
    res.json({ deleted: true });
}));

/**
 * Run a script against a session and say what happened.
 *
 * The response is JSON, not a file. The old endpoint answered every question — syntax error,
 * empty scene, wrong export path — with the same 500 and the same twelve words, which is why the
 * agent could never fix anything: it was never told what broke.
 */
app.post("/run", wrap(async (req, res) => {
    const { sessionId, code, timeoutMs, preview } = req.body ?? {};
    if (typeof code !== "string" || !code.trim()) throw new HttpError(400, "Missing 'code'");

    const session = await sessions.resolve(sessionId);
    const result = await blender.run({
        op: "run",
        sessionFile: sessions.statePath(session.id),
        code,
        params: { preview: normalisePreview(preview) },
        timeoutMs,
    });

    res.json({ sessionId: session.id, sessionCreated: session.created, ...result });
}));

app.post("/import", wrap(async (req, res) => {
    const { sessionId, url, format, options, preview, timeoutMs } = req.body ?? {};
    if (!url) throw new HttpError(400, "Missing 'url'");

    const session = await sessions.resolve(sessionId);
    const asset = await fetchAsset(url, sessions.assetsDir(session.id), { maxBytes: CONFIG.maxAssetBytes });

    const result = await blender.run({
        op: "import",
        sessionFile: sessions.statePath(session.id),
        params: { path: asset.path, format, options, preview: normalisePreview(preview) },
        timeoutMs,
    });

    res.json({ sessionId: session.id, sessionCreated: session.created, asset: { filename: asset.filename, bytes: asset.bytes }, ...result });
}));

app.post("/preview", wrap(async (req, res) => {
    const { sessionId, timeoutMs, ...preview } = req.body ?? {};
    if (!sessionId) throw new HttpError(400, "Missing 'sessionId'");

    const session = await sessions.resolve(sessionId);
    const result = await blender.run({
        op: "inspect",
        sessionFile: sessions.statePath(session.id),
        params: { preview: normalisePreview(preview) || {} },
        timeoutMs,
    });

    res.json({ sessionId: session.id, ...result });
}));

/**
 * Export a session to a real file.
 *
 * Multi-file formats (OBJ + MTL + textures) are zipped rather than having their companions
 * silently dropped — the previous service streamed the .obj alone, so every material it claimed
 * to support arrived as a reference to a .mtl that was never sent.
 */
app.post("/export", wrap(async (req, res) => {
    const { sessionId, format = "glb", objects, options, timeoutMs } = req.body ?? {};
    if (!sessionId) throw new HttpError(400, "Missing 'sessionId'");

    const session = await sessions.resolve(sessionId);
    const outDir = await mkdtemp(path.join(tmpdir(), "blender-export-"));

    try {
        const result = await blender.run({
            op: "export",
            sessionFile: sessions.statePath(session.id),
            params: { outDir, format, objects, options },
            timeoutMs,
        });

        if (!result.ok) return res.status(422).json({ sessionId: session.id, ...result });

        await sendExport(res, outDir, result.export);
    } finally {
        await rm(outDir, { recursive: true, force: true });
    }
}));

/**
 * The original one-shot endpoint: build from an empty scene, get a file back.
 *
 * Kept working so the deploy is not a flag day for anything still calling it, but it now runs on
 * the session machinery underneath and exports every object type rather than meshes alone.
 */
app.post("/generate", wrap(async (req, res) => {
    const { code, format } = req.body ?? {};
    if (!code) return res.status(400).json({ error: "Missing 'code' field" });

    const id = await sessions.create();
    const outDir = await mkdtemp(path.join(tmpdir(), "blender-export-"));

    try {
        const run = await blender.run({ op: "run", sessionFile: sessions.statePath(id), code, params: {} });
        if (!run.ok) return res.status(500).json({ error: "Script failed", details: run.error, stdout: run.stdout });

        // glb rather than the old material-sniffing heuristic: it is one file, and it carries
        // materials, rigs and animation instead of quietly dropping them.
        const exported = await blender.run({
            op: "export",
            sessionFile: sessions.statePath(id),
            params: { outDir, format: format || "glb", options: {} },
        });

        if (!exported.ok) return res.status(500).json({ error: "Export failed", details: exported.error });

        await sendExport(res, outDir, exported.export);
    } finally {
        await rm(outDir, { recursive: true, force: true });
        await sessions.destroy(id).catch(() => { });
    }
}));

app.use((error, _req, res, _next) => {
    const status = error instanceof HttpError ? error.status : 500;
    if (status >= 500) console.error(error);
    res.status(status).json({ error: error.message, ...(error.details ? { details: error.details } : {}) });
});

function normalisePreview(preview) {
    if (!preview) return undefined;
    return preview === true ? {} : preview;
}

/**
 * Send an export, zipping the formats that come as more than one file.
 *
 * OBJ writes a companion .mtl and copies textures next to it. The previous service streamed the
 * .obj alone, so every OBJ it produced referenced a material library the caller never received —
 * which is how a format that does support materials appeared not to.
 */
async function sendExport(res, outDir, info) {
    const { primary, files, multiFile } = info;

    if (!multiFile) {
        res.setHeader("Content-Type", "application/octet-stream");
        res.setHeader("Content-Disposition", `attachment; filename="${primary}"`);
        res.setHeader("X-Blender-Export", JSON.stringify(info));
        await streamAndCleanup(createReadStream(path.join(outDir, primary)), res);
        return;
    }

    const zipName = `${path.parse(primary).name}.zip`;
    res.setHeader("Content-Type", "application/zip");
    res.setHeader("Content-Disposition", `attachment; filename="${zipName}"`);
    res.setHeader("X-Blender-Export", JSON.stringify({ ...info, zipped: files }));

    const archive = archiver("zip", { zlib: { level: 6 } });
    archive.on("error", () => res.destroy());
    archive.pipe(res);
    for (const file of files) archive.file(path.join(outDir, file), { name: file });
    await archive.finalize();
}

async function streamAndCleanup(stream, res) {
    stream.pipe(res);
    await new Promise((resolve) => {
        stream.on("close", resolve);
        stream.on("error", resolve);
    });
}

await sessions.init();
setInterval(() => {
    sessions.collectGarbage()
        .then(removed => removed.length && console.log(`Reaped ${removed.length} idle session(s)`))
        .catch(error => console.error(`Session GC failed: ${error.message}`));
}, 60 * 60 * 1000).unref();

app.listen(CONFIG.port, () => {
    console.log(`Blender service on :${CONFIG.port} (concurrency ${CONFIG.concurrency}, data ${CONFIG.dataDir})`);
});
