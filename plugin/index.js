import { execFile } from "node:child_process";
import { dirname, isAbsolute, join } from "node:path";
import { homedir } from "node:os";
import { fileURLToPath } from "node:url";

export const inject = ["connection"];
export const fields = {
  list: ["query", "source_id", "category", "limit", "offset"], events: ["query", "source_id", "category", "campus", "date_from", "date_to", "status", "favorites", "limit", "offset"],
  detail: ["event_id"], favorite: ["event_id", "enabled"], feedback: ["event_id", "value"],
  preferences: [], "preferences-set": ["themes", "source_ids"], sources: [], "source-toggle": ["source_id", "enabled"],
  digest: ["day", "refresh", "limit"], digests: [], update: ["source_ids", "limit", "max_pages"], "update-status": ["job_id"],
  schedule: [], "schedule-set": ["enabled", "hour", "minute", "timezone"],
  candidates: [], "candidate-add": ["url"], "candidate-fetch": ["url"], "discover-search": ["query", "limit"],
};

export function runtimeConfig(config = {}) {
  for (const key of ["python", "repoRoot", "dbPath", "dataDir"]) {
    if (config[key] !== undefined && (typeof config[key] !== "string" || !isAbsolute(config[key]))) {
      throw new Error(`campus-radar: ${key} must be an absolute path`);
    }
  }
  const dataDir = config.dataDir ?? (config.dbPath ? dirname(config.dbPath) : process.env.CAMPUS_RADAR_HOME ?? join(homedir(), "Library/Application Support/CampusRadar"));
  return {
    python: config.python ?? join(dataDir, "venv/bin/python3"),
    repoRoot: config.repoRoot ?? fileURLToPath(new URL("../", import.meta.url)),
    dbPath: config.dbPath ?? join(dataDir, "public.sqlite3"),
  };
}

export function createHandler(options = {}, action = "list") {
  const config = runtimeConfig(options);
  for (const key of ["python", "repoRoot", "dbPath"]) {
    if (typeof config[key] !== "string" || !isAbsolute(config[key])) {
      throw new Error(`campus-radar: ${key} must be an absolute path`);
    }
  }
  if (!Object.hasOwn(fields, action)) throw new Error("Unknown operation");
  return async (request) => {
    let message;
    try {
      if (request.headers.get("content-type")?.split(";", 1)[0] !== "application/json") {
        return new Response("JSON required", { status: 415 });
      }
      const raw = await request.text();
      if (raw.length > 8192) return new Response("request too large", { status: 413 });
      message = JSON.parse(raw);
      if (message.type !== "client-request" || typeof message.rpcId !== "string" ||
          message.rpcId.length > 100 || message.method !== `campus-radar/${action}` ||
          !message.payload || typeof message.payload !== "object" || Array.isArray(message.payload) ||
          Object.keys(message.payload).some((key) => !fields[action].includes(key))) {
        return new Response("invalid request", { status: 400 });
      }
    } catch {
      return new Response("invalid JSON", { status: 400 });
    }
    let result;
    try {
      const stdout = await new Promise((resolve, reject) => {
        const child = execFile(config.python, ["-m", "campus_radar", "--db", config.dbPath, "api", action], {
          cwd: config.repoRoot, timeout: 30000, maxBuffer: 8 * 1024 * 1024, signal: request.signal,
          env: { ...process.env, CAMPUS_RADAR_HOME: dirname(config.dbPath) },
        }, (error, stdout, stderr) => {
          if (error) { error.stderr = stderr; reject(error); } else resolve(stdout);
        });
        child.stdin.on("error", () => {}); // A child that exits early is reported by execFile.
        child.stdin.end(JSON.stringify(message.payload));
      });
      const data = JSON.parse(stdout);
      if (!data || typeof data !== "object" ||
          (data.documents && data.documents.some((row) => row.visibility !== "public"))) {
        throw new Error("invalid public database result");
      }
      result = { ok: true, value: data };
    } catch (error) {
      const setupRequired = error.code === "ENOENT" || /No module named ['"]lxml['"]/.test(error.stderr ?? "");
      result = { ok: false, error: { code: setupRequired ? "campus-radar/setup-required" : "campus-radar/read-failed",
        message: setupRequired ? "Python 环境尚未就绪。请在此 DSH profile 中运行 campus-radar setup，再重新打开校园雷达。" : "操作未完成。请检查输入、来源状态或本地运行记录；已有数据保留。", details: {} } };
    }
    return Response.json({ type: "server-response", rpcId: message.rpcId, result }, {
      headers: { "cache-control": "no-store" },
    });
  };
}

export function apply(ctx, config) {
  // Exact routes inherit DSH's authenticated owner-only connection policy.
  for (const action of Object.keys(fields)) {
    ctx.connection.fetch.register({
      path: `/api/campus-radar/${action}`, methods: ["POST"], requestBody: "buffered",
      fetch: createHandler(config, action),
    });
  }
}
