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
  per-session lock after five failed attempts. Sessions expire after 30 minutes
  without app activity and can be locked manually.
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

Unit tests cover missing/malformed verifier behavior, salt uniqueness, correct
and incorrect passwords, fail-closed rendering, and the session gate before
vault reads. A live local browser check covered locked, unlocked, and manually
locked states. The effective Streamlit configuration was inspected. Automated
cross-origin browser/WebSocket and mutation probes are still required before a
release claim. MCP remains a separate unauthenticated stdio interface, disabled
by default; do not connect it to a cloud client. This dashboard control does
not secure MCP or turn the app into a remotely safe service.
