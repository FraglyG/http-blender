import { execFile } from "child_process";
import { randomUUID } from "crypto";
import { mkdtemp, readFile, rm, writeFile } from "fs/promises";
import { tmpdir } from "os";
import path from "path";
import { fileURLToPath } from "url";

import { HttpError } from "./sessions.js";

const PY_DIR = path.join(path.dirname(fileURLToPath(import.meta.url)), "..", "py");

/**
 * Serialises Blender launches.
 *
 * Each launch is a whole Blender: hundreds of megabytes of RSS and a core's worth of CPU while it
 * runs. The old service spawned one per request with nothing in between, so three concurrent
 * requests on a small container meant three processes fighting, and the OOM killer picking a
 * winner. Queueing turns an overload into a wait instead of a crash.
 */
class Queue {
    constructor(limit) {
        this.limit = limit;
        this.active = 0;
        this.waiting = [];
    }

    get depth() {
        return this.waiting.length;
    }

    run(task) {
        return new Promise((resolve, reject) => {
            const attempt = () => {
                this.active += 1;
                task().then(resolve, reject).finally(() => {
                    this.active -= 1;
                    const next = this.waiting.shift();
                    if (next) next();
                });
            };

            if (this.active < this.limit) attempt();
            else this.waiting.push(attempt);
        });
    }
}

export class BlenderRunner {
    constructor({ binary, concurrency, defaultTimeoutMs, maxTimeoutMs }) {
        this.binary = binary;
        this.queue = new Queue(concurrency);
        this.defaultTimeoutMs = defaultTimeoutMs;
        this.maxTimeoutMs = maxTimeoutMs;
    }

    get stats() {
        return { active: this.queue.active, queued: this.queue.depth, limit: this.queue.limit };
    }

    /**
     * Run one job inside Blender and return the structured result it wrote.
     *
     * The result travels through a file rather than stdout. Blender prints its own banner,
     * warnings from every addon it loads, and whatever the user's script prints; scraping a
     * payload out of that stream works right up until something logs an unexpected line.
     */
    async run({ op, sessionFile, code, params, timeoutMs }) {
        const jobDir = await mkdtemp(path.join(tmpdir(), "blender-job-"));
        const jobPath = path.join(jobDir, "job.json");
        const resultPath = path.join(jobDir, "result.json");

        const limit = Math.min(timeoutMs || this.defaultTimeoutMs, this.maxTimeoutMs);

        await writeFile(jobPath, JSON.stringify({ op, sessionFile, code, params, resultPath }));

        const started = Date.now();

        try {
            return await this.queue.run(async () => {
                const outcome = await this.#spawn(jobPath, resultPath, limit);
                const durationMs = Date.now() - started;

                let result;
                try {
                    result = JSON.parse(await readFile(resultPath, "utf8"));
                } catch {
                    // No result file means Blender died before the harness could write one:
                    // a segfault, an OOM kill, or the timeout below. Report the real reason
                    // rather than the old "produced no output file", which described a symptom
                    // three layers away from the cause.
                    throw new HttpError(500, outcome.timedOut
                        ? `Blender exceeded its ${Math.round(limit / 1000)}s budget and was killed. Simplify the script, or raise timeoutMs.`
                        : `Blender exited (code ${outcome.code}, signal ${outcome.signal}) without producing a result.`,
                        { stderr: tail(outcome.stderr), stdout: tail(outcome.stdout) });
                }

                return { ...result, durationMs, queueDepth: this.queue.depth };
            });
        } finally {
            await rm(jobDir, { recursive: true, force: true });
        }
    }

    #spawn(jobPath, resultPath, timeoutMs) {
        return new Promise((resolve) => {
            const child = execFile(
                this.binary,
                ["--background", "--factory-startup", "--python", path.join(PY_DIR, "bootstrap.py")],
                {
                    timeout: timeoutMs,
                    killSignal: "SIGKILL",
                    maxBuffer: 32 * 1024 * 1024,
                    env: { ...process.env, BLENDER_JOB: jobPath, BLENDER_RESULT: resultPath },
                },
                (error, stdout, stderr) => {
                    resolve({
                        code: error?.code ?? 0,
                        signal: error?.signal ?? null,
                        timedOut: error?.killed === true && error?.signal === "SIGKILL",
                        stdout,
                        stderr,
                    });
                },
            );

            child.on("error", () => { /* surfaced through the callback above */ });
        });
    }
}

function tail(text, limit = 4000) {
    if (!text) return "";
    return text.length > limit ? `...${text.slice(-limit)}` : text;
}

export function newId() {
    return randomUUID();
}
