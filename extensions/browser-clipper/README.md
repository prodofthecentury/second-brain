# Second Brain — Local-First Browser Web Clipper (Manifest V3)

Capture highlighted web passages and cleaned page text directly into your local Second Brain vault over `http://127.0.0.1:<port>` with **zero cloud relay**, **verifiable UTF-8 SHA-256 snapshots** (`0-raw/note-snapshots/`), and **automatic server-side DLP quarantine** for accidental secrets or prompt-injection payloads.

## Security Model
- **Loopback-only permissions:** `host_permissions` is strictly `["http://127.0.0.1/*"]`.
- **Write-only scoped token:** Pairing mints a revocable `clipper_*` token bound to your exact extension origin (`chrome-extension://<id>` or `moz-extension://<uuid>`). It can **only** call `/v1/clipper/{ping,status,clip}` and can never read your private atoms, notes, or governance routes.

## Quick Setup (Chrome / Edge / Firefox)
1. **Load the extension:**
   - **Chrome / Edge:** Open `chrome://extensions` (or `edge://extensions`), enable **Developer mode**, click **Load unpacked**, and select `extensions/browser-clipper/`.
   - **Firefox:** Open `about:debugging#/runtime/this-firefox`, click **Load Temporary Add-on**, and select `extensions/browser-clipper/manifest.json`.
2. **Copy your extension origin:** Click the Second Brain Clipper toolbar icon -> **Pair** -> **Copy**.
3. **Pair with your local vault:**
   - In **Second Brain Desktop** (`System` -> **Browser Web Clipper**) or **Streamlit Dashboard**, paste your extension origin and click **Pair Extension**, or run:
     ```bash
     second-brain clipper pair --origin chrome-extension://<your-32-char-id> --label "Chrome"
     ```
4. Paste the minted `clipper_...` token (and your API port, default `8765`) into the extension popup and click **Save & Test**.
