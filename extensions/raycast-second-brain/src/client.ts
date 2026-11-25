import fs from "fs";
import os from "os";
import path from "path";

export function securityDir(): string {
  if (process.env.BRAIN_SECURITY_STATE_DIR) return path.resolve(process.env.BRAIN_SECURITY_STATE_DIR);
  if (process.platform === "darwin") {
    return path.join(os.homedir(), ".local", "state", "SecondBrain", "security");
  }
  return path.join(process.env.XDG_STATE_HOME || path.join(os.homedir(), ".local", "state"), "SecondBrain", "security");
}

function readToken(name: string): string {
  try {
    const p = path.join(securityDir(), name);
    const st = fs.lstatSync(p);
    if (!st.isFile() || st.isSymbolicLink() || st.size > 4096) return "";
    return fs.readFileSync(p, "utf8").trim();
  } catch {
    return "";
  }
}

export function apiBaseUrl(): string {
  try {
    const raw = readToken("api-port");
    if (raw) {
      const parsed = JSON.parse(raw);
      const port = Number(parsed.port);
      if (port >= 1 && port <= 65535) return `http://127.0.0.1:${port}`;
    }
  } catch {}
  return "http://127.0.0.1:8765";
}

export async function callBrainApi<T = any>(apiPath: string, method: "GET" | "POST" = "GET", body?: unknown): Promise<T> {
  const token = readToken("memory-api-token") || readToken("api-token");
  const ownerToken = readToken("owner-token");
  const headers: Record<string, string> = { Accept: "application/json" };
  if (token) headers.Authorization = `Bearer ${token}`;
  if (ownerToken) headers["X-Owner-Token"] = ownerToken;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const res = await fetch(`${apiBaseUrl()}${apiPath}`, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  const data = await res.json();
  if (!res.ok) throw new Error((data as any)?.error || `HTTP ${res.status}`);
  return data as T;
}
