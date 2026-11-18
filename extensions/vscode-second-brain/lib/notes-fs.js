"use strict";

function isSafeNoteFilename(name) {
  if (typeof name !== "string") return false;
  const trimmed = name.trim();
  if (!trimmed || trimmed.length > 200) return false;
  if (
    trimmed === "." ||
    trimmed === ".." ||
    trimmed.includes("/") ||
    trimmed.includes("\\") ||
    trimmed.includes("..") ||
    trimmed.includes("\0")
  ) {
    return false;
  }
  for (let i = 0; i < trimmed.length; i++) {
    if (trimmed.charCodeAt(i) < 32) return false;
  }
  return true;
}

class SecondBrainNoteFileSystemProvider {
  constructor({ vscode, apiRequest, onVaultMutated }) {
    this._vscode = vscode;
    this._apiRequest = apiRequest;
    this._onVaultMutated = onVaultMutated;
    this._onDidChangeFile = new vscode.EventEmitter();
    this.onDidChangeFile = this._onDidChangeFile.event;
    this._meta = new Map();
  }

  static toNoteUri(vscode, filename) {
    const clean = String(filename || "").trim();
    if (!isSafeNoteFilename(clean)) {
      throw new Error(`Invalid note filename: ${filename}`);
    }
    if (vscode.Uri && typeof vscode.Uri.from === "function") {
      return vscode.Uri.from({ scheme: "secondbrain-note", path: `/${clean}` });
    }
    return vscode.Uri.parse(`secondbrain-note:/${encodeURIComponent(clean)}`);
  }

  static fromNoteUri(uri) {
    const rawPath = String(uri?.path || "").replace(/^\/+/, "");
    if (!rawPath) return "";
    let decoded = rawPath;
    try {
      decoded = decodeURIComponent(rawPath);
    } catch {
      return "";
    }
    return isSafeNoteFilename(decoded) ? decoded.trim() : "";
  }

  getExpectedSha256(filename) {
    return this._meta.get(filename)?.sha256 || "";
  }

  getNoteMeta(filename) {
    return this._meta.get(filename) || null;
  }

  primeNoteMetadata(filename, { sha256 = "", title = "", tags = [], body = "" } = {}) {
    if (!isSafeNoteFilename(filename)) return;
    const now = Date.now();
    this._meta.set(filename, {
      sha256: String(sha256 || ""),
      title: String(title || ""),
      tags: Array.isArray(tags) ? [...tags] : [],
      ctime: now,
      mtime: now,
      size: Buffer.byteLength(String(body || ""), "utf8"),
    });
  }

  forgetNote(filename) {
    this._meta.delete(filename);
  }

  watch() {
    return { dispose() {} };
  }

  async stat(uri) {
    const vscode = this._vscode;
    const rawPath = String(uri?.path || "").replace(/^\/+/, "");
    if (!rawPath) {
      return {
        type: vscode.FileType.Directory,
        ctime: 0,
        mtime: 0,
        size: 0,
      };
    }
    const filename = SecondBrainNoteFileSystemProvider.fromNoteUri(uri);
    if (!filename) {
      throw vscode.FileSystemError.FileNotFound(uri);
    }

    const cached = this._meta.get(filename);
    if (cached) {
      return {
        type: vscode.FileType.File,
        ctime: cached.ctime,
        mtime: cached.mtime,
        size: cached.size,
      };
    }

    const res = await this._apiRequest(
      "GET",
      `/v1/notes/${encodeURIComponent(filename)}`,
      null,
      { useOwnerToken: true }
    );
    if (!res.ok) {
      throw vscode.FileSystemError.FileNotFound(uri);
    }
    const n = res.data?.note || res.data || {};
    const bodyText = String(n.body ?? "");
    const size = Buffer.byteLength(bodyText, "utf8");
    const now = Date.now();
    this._meta.set(filename, {
      sha256: String(n.sha256 || ""),
      title: String(n.title || ""),
      tags: Array.isArray(n.tags) ? [...n.tags] : [],
      ctime: now,
      mtime: now,
      size,
    });
    return {
      type: vscode.FileType.File,
      ctime: now,
      mtime: now,
      size,
    };
  }

  async readDirectory() {
    const vscode = this._vscode;
    const res = await this._apiRequest("GET", "/v1/notes?limit=200&offset=0", null, {
      useOwnerToken: true,
    });
    if (!res.ok) return [];
    const items = res.data?.items || res.data?.notes || [];
    return items
      .filter((n) => n && isSafeNoteFilename(n.filename))
      .map((n) => [n.filename, vscode.FileType.File]);
  }

  createDirectory(uri) {
    throw this._vscode.FileSystemError.NoPermissions(uri);
  }

  async readFile(uri) {
    const vscode = this._vscode;
    const filename = SecondBrainNoteFileSystemProvider.fromNoteUri(uri);
    if (!filename) {
      throw vscode.FileSystemError.FileNotFound(uri);
    }
    const res = await this._apiRequest(
      "GET",
      `/v1/notes/${encodeURIComponent(filename)}`,
      null,
      { useOwnerToken: true }
    );
    if (!res.ok) {
      throw vscode.FileSystemError.FileNotFound(uri);
    }
    const n = res.data?.note || res.data || {};
    const bodyText = String(n.body ?? "");
    const buf = Buffer.from(bodyText, "utf8");
    const now = Date.now();
    const prev = this._meta.get(filename);
    this._meta.set(filename, {
      sha256: String(n.sha256 || ""),
      title: String(n.title || ""),
      tags: Array.isArray(n.tags) ? [...n.tags] : [],
      ctime: prev?.ctime || now,
      mtime: now,
      size: buf.byteLength,
    });
    return new Uint8Array(buf);
  }

  async writeFile(uri, content, options = {}) {
    const vscode = this._vscode;
    const filename = SecondBrainNoteFileSystemProvider.fromNoteUri(uri);
    if (!filename) {
      throw vscode.FileSystemError.NoPermissions(uri);
    }
    const bodyText = Buffer.from(content).toString("utf8");
    let meta = this._meta.get(filename);

    if ((!meta || !meta.sha256) && options.create) {
      const checkRes = await this._apiRequest(
        "GET",
        `/v1/notes/${encodeURIComponent(filename)}`,
        null,
        { useOwnerToken: true }
      );
      if (!checkRes.ok && checkRes.status === 404) {
        const createRes = await this._apiRequest(
          "POST",
          "/v1/notes",
          {
            title: filename.replace(/\.md$/i, ""),
            body: bodyText,
            tags: [],
          },
          { useOwnerToken: true }
        );
        if (!createRes.ok) {
          throw vscode.FileSystemError.NoPermissions(
            createRes.data?.error || `Create note failed (${createRes.status})`
          );
        }
        const created = createRes.data?.note || createRes.data || {};
        this.primeNoteMetadata(created.filename || filename, {
          sha256: created.sha256 || "",
          title: created.title || filename.replace(/\.md$/i, ""),
          tags: created.tags || [],
          body: bodyText,
        });
        this._onDidChangeFile.fire([{ type: vscode.FileChangeType.Created, uri }]);
        if (typeof this._onVaultMutated === "function") this._onVaultMutated();
        return;
      }
      if (checkRes.ok) {
        const existing = checkRes.data?.note || checkRes.data || {};
        this.primeNoteMetadata(filename, {
          sha256: existing.sha256 || "",
          title: existing.title || "",
          tags: existing.tags || [],
          body: existing.body || "",
        });
        meta = this._meta.get(filename);
      }
    }

    if (!meta || !meta.sha256) {
      const fetchRes = await this._apiRequest(
        "GET",
        `/v1/notes/${encodeURIComponent(filename)}`,
        null,
        { useOwnerToken: true }
      );
      if (!fetchRes.ok) {
        throw vscode.FileSystemError.FileNotFound(uri);
      }
      const existing = fetchRes.data?.note || fetchRes.data || {};
      this.primeNoteMetadata(filename, {
        sha256: existing.sha256 || "",
        title: existing.title || "",
        tags: existing.tags || [],
        body: existing.body || "",
      });
      meta = this._meta.get(filename);
    }

    const expectedSha = meta.sha256;
    const putPayload = {
      body: bodyText,
      expected_sha256: expectedSha,
    };
    const putRes = await this._apiRequest(
      "PUT",
      `/v1/notes/${encodeURIComponent(filename)}`,
      putPayload,
      { useOwnerToken: true }
    );

    if (putRes.ok) {
      const updated = putRes.data?.note || putRes.data || {};
      const now = Date.now();
      meta.sha256 = String(updated.sha256 || expectedSha);
      meta.mtime = now;
      meta.size = Buffer.byteLength(bodyText, "utf8");
      this._onDidChangeFile.fire([{ type: vscode.FileChangeType.Changed, uri }]);
      if (typeof this._onVaultMutated === "function") this._onVaultMutated();
      return;
    }

    if (putRes.status === 409) {
      const action = await vscode.window.showWarningMessage(
        `CAS conflict saving '${filename}' (409): note was modified since opened.`,
        "Overwrite Remote",
        "Reload Remote",
        "Cancel"
      );

      if (action === "Overwrite Remote") {
        const latestRes = await this._apiRequest(
          "GET",
          `/v1/notes/${encodeURIComponent(filename)}`,
          null,
          { useOwnerToken: true }
        );
        if (!latestRes.ok) {
          throw vscode.FileSystemError.NoPermissions(
            `409 Conflict: could not fetch latest SHA-256 for ${filename}`
          );
        }
        const latest = latestRes.data?.note || latestRes.data || {};
        const retryRes = await this._apiRequest(
          "PUT",
          `/v1/notes/${encodeURIComponent(filename)}`,
          {
            body: bodyText,
            expected_sha256: String(latest.sha256 || ""),
          },
          { useOwnerToken: true }
        );
        if (!retryRes.ok) {
          throw vscode.FileSystemError.NoPermissions(
            retryRes.data?.error || `409 Conflict: overwrite failed (${retryRes.status})`
          );
        }
        const saved = retryRes.data?.note || retryRes.data || {};
        meta.sha256 = String(saved.sha256 || latest.sha256 || "");
        meta.mtime = Date.now();
        meta.size = Buffer.byteLength(bodyText, "utf8");
        this._onDidChangeFile.fire([{ type: vscode.FileChangeType.Changed, uri }]);
        if (typeof this._onVaultMutated === "function") this._onVaultMutated();
        return;
      }

      if (action === "Reload Remote") {
        const latestRes = await this._apiRequest(
          "GET",
          `/v1/notes/${encodeURIComponent(filename)}`,
          null,
          { useOwnerToken: true }
        );
        if (latestRes.ok) {
          const latest = latestRes.data?.note || latestRes.data || {};
          this.primeNoteMetadata(filename, {
            sha256: latest.sha256 || "",
            title: latest.title || meta.title,
            tags: latest.tags || meta.tags,
            body: latest.body || "",
          });
          this._onDidChangeFile.fire([{ type: vscode.FileChangeType.Changed, uri }]);
        }
        throw vscode.FileSystemError.NoPermissions(
          `409 Conflict: reloaded remote version of ${filename}`
        );
      }

      throw vscode.FileSystemError.NoPermissions(
        `409 Conflict: ${putRes.data?.error || `saving ${filename} failed (expected_sha256 mismatch)`}`
      );
    }

    vscode.window.showErrorMessage(
      `Save note failed (${putRes.status}): ${putRes.data?.error || "error"}`
    );
    throw vscode.FileSystemError.NoPermissions(
      putRes.data?.error || `Save note failed (${putRes.status})`
    );
  }

  async delete(uri) {
    const vscode = this._vscode;
    const filename = SecondBrainNoteFileSystemProvider.fromNoteUri(uri);
    if (!filename) {
      throw vscode.FileSystemError.FileNotFound(uri);
    }
    let expectedSha = this.getExpectedSha256(filename);
    if (!expectedSha) {
      const fetchRes = await this._apiRequest(
        "GET",
        `/v1/notes/${encodeURIComponent(filename)}`,
        null,
        { useOwnerToken: true }
      );
      if (!fetchRes.ok) {
        throw vscode.FileSystemError.FileNotFound(uri);
      }
      const existing = fetchRes.data?.note || fetchRes.data || {};
      expectedSha = String(existing.sha256 || "");
    }
    const delRes = await this._apiRequest(
      "DELETE",
      `/v1/notes/${encodeURIComponent(filename)}`,
      { expected_sha256: expectedSha },
      { useOwnerToken: true }
    );
    if (!delRes.ok) {
      throw vscode.FileSystemError.NoPermissions(
        delRes.data?.error || `Delete note failed (${delRes.status})`
      );
    }
    this._meta.delete(filename);
    this._onDidChangeFile.fire([{ type: vscode.FileChangeType.Deleted, uri }]);
    if (typeof this._onVaultMutated === "function") this._onVaultMutated();
  }

  rename(oldUri) {
    throw this._vscode.FileSystemError.NoPermissions(oldUri);
  }
}

module.exports = {
  isSafeNoteFilename,
  SecondBrainNoteFileSystemProvider,
};
