# Second Brain — Full On-Device Mobile Vault Client (iOS & Android)

Zero-knowledge, 100% offline-capable mobile vault replica and grounded chat client for **Second Brain**.

## Security & Epistemic Guarantees

1. **Byte-Exact Zero-Knowledge E2EE Sync (`www/lib/vault-crypto.js`)**:
   - Pure-JS RFC 7914 `scrypt(N=32768, r=8, p=1, dkLen=32)` + WebCrypto `HKDF-SHA256` + `AES-256-GCM` matching `scripts/vault_sync.py` and `scripts/vault_backup.py` bit-for-bit.
   - Verifies `Ed25519` device head signatures (WebCrypto with pure-JS `BigInt` RFC 8032 fallback), master-key `HMAC-SHA256` (`head_mac`), peer key pinning, blind blob IDs, and monotonic anti-rollback epochs.
   - Enforces the strict Three-Store path boundary (`validateSyncRelPath`) before touching device storage.
   - Supports direct HTTPS / AWS S3 / Cloudflare R2 `AWS4-HMAC-SHA256` SigV4 sync pulls **and** air-gapped `.sbvault` (`SBVBKP1`) backup imports.
   - Encrypts the synced vault at rest in IndexedDB via `EncryptedDeviceStore` (`PBKDF2-SHA256`, 210,000 iterations -> `AES-256-GCM`), so plaintext memories never sit unencrypted in IndexedDB or `localStorage`.

2. **100% Offline Epistemic Verification, Search & Grounded Chat (`www/lib/vault-epistemic-engine.js`)**:
   - **Owner Approval Gate**: Re-verifies `0-raw/archive/decision-record.jsonl` (`action === "approve"` and exact `SHA-256` parity) on device.
   - **Byte-Offset Evidence Gate**: Verifies raw `0-raw/` / `sources/` file `SHA-256` and exact UTF-8 byte-slice (`[evidence_start:evidence_end]`) whitespace-normalized quote parity. Unapproved, superseded, or tampered-source atoms have **0% leakage**.
   - **Bi-Temporal & Project Containment**: Enforces `valid_from` / `valid_until` against `as_of` and strict `project:<id>` scoping, separating `owner_subjective_preference` from factual claims.
   - **Offline Search & Merkle Audit**: Runs Porter stemmer + Okapi BM25 (`k1=1.5, b=0.75`), verifies `0-raw/archive/vault-ledger.jsonl` hash chains, and generates portable Merkle inclusion proofs offline.
   - **Outbound Quarantine**: Mobile quick-captures encrypt forward-secret `SBID1` (`ECDH P-256` + `AES-256-GCM`) envelopes exclusively into `inbox-drops/*.enc` -> `0-raw/inbox/` — mobile devices never hold host `Ed25519` signing keys and never auto-approve canonical atoms.

---

## Option A: Zero-Build Offline PWA Install (iOS Safari & Android Chrome)

Because `www/` has **zero external npm runtime dependencies**, you can host `www/` on any static HTTPS origin (Cloudflare Pages, GitHub Pages, private LAN TLS, Tailscale Serve):

1. Generate a pairing URI on your desktop:
   ```bash
   uv run python scripts/mobile_vault_client.py pair --label "My iPhone"
   ```
2. Open the `#vaultpair=...` link once on your phone:
   - **iOS (Safari)**: Tap **Share** -> **Add to Home Screen**.
   - **Android (Chrome)**: Tap **⋮** -> **Add to Home screen / Install app**.
3. Set a local **Device Lock PIN**, tap **Pull & Verify E2EE Vault** (or import a `.sbvault` file from the Files app), and switch to **Airplane Mode** — Search, Grounded Chat, Notes, and Merkle Audit run 100% offline!

---

## Option B: Native iOS App Build (Capacitor + Xcode)

1. Install Capacitor build tooling (macOS with Xcode 16+):
   ```bash
   cd mobile/vault-client
   npm install
   npx cap sync ios
   npx cap open ios
   ```
2. In Xcode:
   - Bundle Identifier: `com.secondbrain.vault`
   - `Info.plist` enforces `NSAppTransportSecurity` (`NSAllowsArbitraryLoads = false`), disables iTunes file sharing (`UIFileSharingEnabled = false`), excludes WebView storage from unencrypted iCloud backups, and registers `.sbvault` document import.
   - Select your signing team and run on your iPhone / iPad.

---

## Option C: Native Android APK / AAB Build (Capacitor + Gradle)

1. Sync web assets into the Android project (Android Studio Ladybug+ / JDK 17):
   ```bash
   cd mobile/vault-client
   npm install
   npx cap sync android
   cd android
   ./gradlew assembleRelease
   ```
2. Hardened Android defaults:
   - `android:usesCleartextTraffic="false"` (blocks non-TLS remote traffic)
   - `android:allowBackup="false"` and `android:fullBackupContent="false"` (prevents ADB / cloud plaintext extraction)
   - `WindowManager.LayoutParams.FLAG_SECURE` in `MainActivity.kt` (prevents task-switcher screenshot leakage)

---

## Running Automated Tests

```bash
# Pure Node 20+ test suite (zero npm install required):
node --test mobile/vault-client/test/mobile_vault.test.js

# Full cross-runtime Python <-> Node E2EE sync, .sbvault, Merkle & adversarial test suite:
uv run --frozen python -m unittest scripts.mobile_vault_client_tests -v
```
