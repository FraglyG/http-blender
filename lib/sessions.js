import { randomUUID } from "crypto";
import { mkdir, readdir, rm, stat, writeFile } from "fs/promises";
import path from "path";

/**
 * A session is a directory holding one `state.blend` plus whatever came with it.
 *
 * The previous service wiped the scene at the start of every request, which made every call a
 * cold start: "make the roof taller" meant regenerating the entire building from a single script,
 * and importing a model to edit was impossible because there was nothing for the next call to
 * edit. Persisting the .blend is what turns this from a renderer into something an agent can work
 * in.
 */
export class SessionStore {
    constructor({ root, ttlMs }) {
        this.root = root;
        this.ttlMs = ttlMs;
    }

    async init() {
        await mkdir(this.root, { recursive: true });
    }

    dir(id) {
        // Session ids come off the wire; a traversal here would hand out arbitrary filesystem
        // reads via the export endpoint.
        if (!/^[a-z0-9-]{8,64}$/i.test(id)) throw new HttpError(400, `Malformed session id`);
        return path.join(this.root, id);
    }

    statePath(id) {
        return path.join(this.dir(id), "state.blend");
    }

    assetsDir(id) {
        return path.join(this.dir(id), "assets");
    }

    async create() {
        const id = randomUUID();
        await mkdir(this.assetsDir(id), { recursive: true });
        await writeFile(path.join(this.dir(id), "created"), new Date().toISOString());
        return id;
    }

    async exists(id) {
        try {
            await stat(this.dir(id));
            return true;
        } catch {
            return false;
        }
    }

    /** Resolve the session for a request, creating one when the caller did not name it. */
    async resolve(id) {
        if (!id) return { id: await this.create(), created: true };
        if (!(await this.exists(id))) throw new HttpError(404, `No such session: ${id}. Create one, or omit sessionId to start fresh.`);
        return { id, created: false };
    }

    async destroy(id) {
        await rm(this.dir(id), { recursive: true, force: true });
    }

    async list() {
        const entries = await readdir(this.root, { withFileTypes: true });
        return entries.filter(entry => entry.isDirectory()).map(entry => entry.name);
    }

    /**
     * Drop sessions nobody has touched in a while.
     *
     * A rigged character with packed textures is tens of megabytes; without this the container
     * fills up and every subsequent request fails for reasons that look nothing like the cause.
     */
    async collectGarbage() {
        const cutoff = Date.now() - this.ttlMs;
        const removed = [];

        for (const id of await this.list()) {
            try {
                const info = await stat(this.dir(id));
                if (info.mtimeMs < cutoff) {
                    await this.destroy(id);
                    removed.push(id);
                }
            } catch {
                // Racing with another cleanup is fine.
            }
        }

        return removed;
    }
}

export class HttpError extends Error {
    constructor(status, message, details) {
        super(message);
        this.status = status;
        this.details = details;
    }
}
