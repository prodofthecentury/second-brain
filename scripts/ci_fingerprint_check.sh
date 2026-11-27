#!/bin/bash
# Fingerprint gate. This file is executed from the owner-pinned harness, never
# from the pull request's copy. The pin is the PINNED_HARNESS_SHA line on the
# base branch workflow. A missing pin fails closed.
set -euo pipefail

base_ref="${1:?base ref, for example origin/cursor/reason-core-2e0f}"
workspace="$(pwd)"

pin="$(git show "${base_ref}:.github/workflows/ci.yml" | python3 -c '
import re, sys
match = re.search(r"^[ \t]*PINNED_HARNESS_SHA:\s*\"([0-9a-f]{40})\"", sys.stdin.read(), re.M)
print(match.group(1) if match else "")
')"
if [ -z "$pin" ]; then
  echo "owner has not set PINNED_HARNESS_SHA on ${base_ref}; fail closed" >&2
  exit 2
fi
if ! git cat-file -e "${pin}^{commit}" 2>/dev/null; then
  echo "pinned harness ${pin} is not in this repository; fail closed" >&2
  exit 2
fi

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
  recorded="$(git rev-parse "${pin}:${path}")"
  if [ ! -f "$workspace/$path" ]; then
    echo "PR removed ${path} relative to pinned harness ${pin}" >&2
    exit 1
  fi
  current="$(git hash-object "$workspace/$path")"
  if [ "$current" != "$recorded" ]; then
    echo "PR rewrote ${path} relative to pinned harness ${pin}" >&2
    exit 1
  fi
done

dest="${RUNNER_TEMP:-/tmp}/pinned-harness-$$"
rm -rf "$dest"
mkdir -p "$dest"
git archive "$pin" | tar -x -C "$dest"
cd "$dest"
uv run --frozen --project "$workspace" python -c '
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd()))
from scripts.reason_bench import gate_main
raise SystemExit(gate_main(Path(sys.argv[1]), sys.argv[2], sys.argv[3]))
' "$workspace" "$base_ref" "$pin"
