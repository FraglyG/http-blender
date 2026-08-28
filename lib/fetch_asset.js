import { createWriteStream } from "fs";
import { unlink } from "fs/promises";
import path from "path";
import { pipeline } from "stream/promises";

import { HttpError } from "./sessions.js";

const ALLOWED_PROTOCOLS = new Set(["http:", "https:"]);

/**
 * Pull a remote asset onto disk so Blender can import it.
 *
 * Fetching by URL is what lets the agent work on things the user already has — a character from
 * the CDN, a reference model, a texture — instead of only what it can generate from an empty
 * scene. That is also why it needs bounding: this runs inside the project's private network, so
 * an unbounded fetcher is an SSRF pivot with a 3D modelling hobby.
 */
export async function fetchAsset(url, destDir, { maxBytes, timeoutMs = 60_000 }) {
    let parsed;
    try {
        parsed = new URL(url);
    } catch {
        throw new HttpError(400, `Not a URL: ${url}`);
    }

    if (!ALLOWED_PROTOCOLS.has(parsed.protocol)) {
        throw new HttpError(400, `Only http(s) URLs can be imported (got ${parsed.protocol})`);
    }

    if (isPrivateHost(parsed.hostname)) {
        throw new HttpError(400, `Refusing to fetch from a private address (${parsed.hostname})`);
    }

    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);

    let response;
    try {
        response = await fetch(parsed, { signal: controller.signal, redirect: "follow" });
    } catch (error) {
        clearTimeout(timer);
        throw new HttpError(502, `Could not fetch ${url}: ${error.message}`);
    }

    if (!response.ok) {
        clearTimeout(timer);
        throw new HttpError(502, `Could not fetch ${url}: upstream returned ${response.status}`);
    }

    const declared = Number(response.headers.get("content-length") || 0);
    if (declared && declared > maxBytes) {
        clearTimeout(timer);
        throw new HttpError(413, `Asset is ${(declared / 1e6).toFixed(1)}MB, over the ${(maxBytes / 1e6).toFixed(0)}MB limit`);
    }

    const filename = safeFilename(parsed);
    const dest = path.join(destDir, filename);

    let written = 0;
    const counter = new TransformStream({
        transform(chunk, controllerRef) {
            written += chunk.byteLength;
            // Content-Length is a claim, not a promise; enforce against what actually arrives.
            if (written > maxBytes) controllerRef.error(new HttpError(413, `Asset exceeded the ${(maxBytes / 1e6).toFixed(0)}MB limit while downloading`));
            else controllerRef.enqueue(chunk);
        },
    });

    try {
        await pipeline(response.body.pipeThrough(counter), createWriteStream(dest));
    } catch (error) {
        await unlink(dest).catch(() => { });
        throw error instanceof HttpError ? error : new HttpError(502, `Download failed: ${error.message}`);
    } finally {
        clearTimeout(timer);
    }

    return { path: dest, filename, bytes: written };
}

function safeFilename(parsed) {
    const raw = path.basename(decodeURIComponent(parsed.pathname)) || "asset";
    const cleaned = raw.replace(/[^a-zA-Z0-9._-]/g, "_").slice(-120);
    return cleaned.includes(".") ? cleaned : `${cleaned}.bin`;
}

function isPrivateHost(hostname) {
    const host = hostname.toLowerCase();

    if (host === "localhost" || host.endsWith(".localhost") || host.endsWith(".internal")) return true;
    if (host === "0.0.0.0" || host === "::1" || host === "[::1]") return true;

    const ipv4 = host.match(/^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/);
    if (!ipv4) return false;

    const [a, b] = ipv4.slice(1).map(Number);
    if (a === 10 || a === 127) return true;
    if (a === 192 && b === 168) return true;
    if (a === 172 && b >= 16 && b <= 31) return true;
    if (a === 169 && b === 254) return true;

    return false;
}
