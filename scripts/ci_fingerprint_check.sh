#!/bin/bash
# Fingerprint gate. Run the copy from the pinned harness, not the pull request.
# The pin is the Actions variable PINNED_HARNESS_SHA, or .github/harness-pin on
# the base ref. It is not a line inside ci.yml. A missing pin fails closed.
set -euo pipefail

base_ref="${1:?base ref, for example origin/cursor/reason-core-2e0f}"
workspace="$(pwd)"

pin="${PINNED_HARNESS_SHA:-}"
if [ -z "$pin" ]; then
  pin="$(git show "${base_ref}:.github/harness-pin" 2>/dev/null | tr -d '[:space:]' || true)"
fi
if ! printf '%s' "$pin" | grep -Eq '^[0-9a-f]{40}$'; then
  echo "owner has not set PINNED_HARNESS_SHA for ${base_ref}; fail closed" >&2
  exit 2
fi
if ! git cat-file -e "${pin}^{commit}" 2>/dev/null; then
  echo "pinned harness ${pin} is not in this repository; fail closed" >&2
  exit 2
fi

# ci.yml may contain a PINNED_HARNESS_SHA line. That one line is not a rewrite.
mask_workflow() {
  python3 -c '
import re, sys
text = sys.stdin.read()
text = re.sub(r"^[ \t]*PINNED_HARNESS_SHA:.*$", "PINNED_HARNESS_SHA: \"<masked>\"", text, flags=re.M)
sys.stdout.write(text)
'
}

protected=(
  scripts/ci_fingerprint_check.sh
  .github/workflows/ci.yml
)
while IFS= read -r path; do
  protected+=("$path")
done < <(git ls-tree -r --name-only "$pin" | grep -E '^scripts/reason_bench.*\.py$' || true)

for path in "${protected[@]}"; do
  if ! git cat-file -e "${pin}:${path}" 2>/dev/null; then
    echo "pinned harness ${pin} has no ${path}; fail closed" >&2
    exit 2
  fi
  if [ ! -f "$workspace/$path" ]; then
    echo "PR removed ${path} relative to pinned harness ${pin}" >&2
    exit 1
  fi
  if [ "$path" = ".github/workflows/ci.yml" ]; then
    recorded="$(git show "${pin}:${path}" | mask_workflow | git hash-object --stdin)"
    current="$(mask_workflow < "$workspace/$path" | git hash-object --stdin)"
  else
    recorded="$(git rev-parse "${pin}:${path}")"
    current="$(git hash-object "$workspace/$path")"
  fi
  if [ "$current" != "$recorded" ]; then
    echo "PR rewrote ${path} relative to pinned harness ${pin}" >&2
    exit 1
  fi
done

dest="${RUNNER_TEMP:-/tmp}/pinned-harness-$$"
rm -rf "$dest"
mkdir -p "$dest"
git archive "$pin" | tar -x -C "$dest"
# The checker environment is the pinned lockfile, not the pull request's.
lock_id="$(git rev-parse "${pin}:uv.lock")"
export UV_PROJECT_ENVIRONMENT="${RUNNER_TEMP:-/tmp}/pinned-venv-${lock_id}"
uv sync --frozen --extra math --project "$dest"
cd "$dest"
uv run --frozen --project "$dest" python -c '
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd()))
from scripts.reason_bench import gate_main
raise SystemExit(gate_main(Path(sys.argv[1]), sys.argv[2], sys.argv[3]))
' "$workspace" "$base_ref" "$pin"
