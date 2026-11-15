# Dashboard security boundary

**Supported deployment:** one owner using the dashboard on the same OS account
and machine. The server binds to `127.0.0.1:8501`. Reverse proxies, tunnels,
LAN exposure, and cloud deployment are unsupported.

## Assets and threats

- **Assets:** raw captures, source evidence, approved memory, chat content, and
  owner review actions.
- **Unauthenticated browser session:** could otherwise read vault-derived UI or
  submit review actions. The app now requires a password before it computes
  vault counts or renders any page content; the gate runs before page logic.
- **Cross-origin website:** could try to read the loopback app, open a forged
  Streamlit WebSocket, or trigger owner actions. CORS and XSRF are explicitly
  enabled, XSRF cookies use `SameSite=Strict`, the CORS origin is fixed to the
  loopback UI URL, and WebSocket host validation allows only `127.0.0.1`.
- **Guessing the owner password:** the verifier uses a random 128-bit salt,
  scrypt (`N=32768`, `r=8`, `p=1`), constant-time digest comparison, and a
  cross-session 60-second lockout after five failed attempts. Authenticated
  session state lives strictly server-side inside Tornado's origin-gated
  WebSocket `st.session_state` (never serialized into a custom client cookie or
  exposed to browser JavaScript), expires after 30 minutes without app activity,
  and can be locked manually.
- **Same-account malware or local process:** out of scope. A process running as
  the owner can read the vault, inspect/replace the app, or access the local
  browser session. The password gate is not a sandbox or OS account boundary.

## Credential handling

The owner password is entered through a masked local form. Only the salted
scrypt verifier is persisted, under the current OS user's application state
directory (`%LOCALAPPDATA%/SecondBrain/security` on Windows, or the XDG state
directory on Unix-like systems). On POSIX the directory/file are restricted to
the current user. Use `uv run --locked python scripts/dashboard_auth.py set-password` to set or
reset it. `run_dashboard.ps1` prompts for a password on first launch and refuses
to start if setup fails. The verifier is not stored in the vault or Git.

## Verification and remaining limits

Unit and live boundary tests cover missing/malformed verifier behavior, salt
uniqueness, correct and incorrect passwords, thread-safe cross-session
rate-limiting and automated 60-second lockout, server-side `st.session_state`
authentication inside Tornado's origin-gated WebSocket (never exposed to browser
JavaScript) paired with Streamlit's `SameSite=Strict` XSRF cookie
(`--server.xsrfCookieSameSite=strict`), strict `127.0.0.1` loopback binding
(rejecting `0.0.0.0` or external interfaces), cross-origin HTTP/WebSocket
rejection (`403 Forbidden`), and the session gate before vault reads. MCP
remains disabled by default (`BRAIN_ENABLE_LOCAL_MCP=1`), fails closed without a
configured local bearer token (`--generate-token`, `>= 16` chars, `0600` /
owner-only DACL) authenticated either per-request via MCP `_meta`
(`authorization: Bearer <token>`, never via tool `arguments`) or via Phase 4's
local stdio handshake (`BRAIN_MCP_TRUSTED_STDIO=1` paired with a constant-time
`BRAIN_MCP_CLIENT_TOKEN` match managed by `second-brain connect`), enforces
granular tool scoping (`BRAIN_MCP_ALLOWED_TOOLS`), restricts canonical atom
reads (`search_memory`, `get_atom`) to `review_status == accepted`
non-superseded atoms while tagging working-note reads (`search_notes`,
`get_note`) as `<owner_notes untrusted="true">`, and routes untrusted or risky
`add_to_inbox` / `remember` captures into `brain/0-raw/inbox/` requiring
explicit owner promotion into `brain/0-raw/` before extraction; do not connect
it to a cloud client.
