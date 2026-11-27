#!/bin/bash
# Exact fingerprint gate. The baseline is read from the base ref only.
# A missing file or a fingerprint without item_hashes fails closed.
set -euo pipefail
base_ref="${1:?base ref, for example origin/cursor/reason-core-2e0f}"
root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"
path="bench/reasoning/baseline/fingerprint.json"
if ! git cat-file -e "${base_ref}:${path}" 2>/dev/null; then
  echo "baseline fingerprint missing at ${base_ref}:${path}; fail closed" >&2
  exit 2
fi
base_fp="${RUNNER_TEMP:-/tmp}/base-hard-failures.json"
git show "${base_ref}:${path}" > "${base_fp}"
python3 - "$base_fp" << 'PY'
import json, sys
payload = json.load(open(sys.argv[1], encoding="utf-8"))
hashes = payload.get("item_hashes")
if not isinstance(hashes, dict) or not hashes:
    sys.exit("baseline fingerprint has no item_hashes; fail closed")
if "should_fire_answered" not in payload:
    sys.exit("baseline fingerprint has no should_fire_answered; fail closed")
PY
uv run --frozen python -m scripts.reason_bench fingerprint \
  --check bench/reasoning/expected-hard-failures.current.json \
  --base-ref "$base_ref" \
  --base-file "$base_fp"
