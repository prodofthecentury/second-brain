#!/usr/bin/env bash
# Verified one-step POSIX (Linux / macOS) setup script for Second Brain.
set -euo pipefail

NO_LAUNCH=0
PULL_MODELS=0
REBUILD_VECTORS=0

for arg in "$@"; do
    case "$arg" in
        --no-launch)
            NO_LAUNCH=1
            ;;
        --pull-models)
            PULL_MODELS=1
            ;;
        --rebuild-vectors)
            REBUILD_VECTORS=1
            ;;
        -h|--help)
            echo "Usage: ./setup.sh [--no-launch] [--pull-models] [--rebuild-vectors]"
            exit 0
            ;;
        *)
            echo "Unknown option: $arg" >&2
            exit 2
            ;;
    esac
done

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo "Setup will not initialize, stage, or commit files. Review the repository boundary separately."

echo "=== Host Capability Inspection ==="
OS_NAME="$(uname -s)"
OS_ARCH="$(uname -m)"
OS_REL="$(uname -r)"
echo "OS:        ${OS_NAME} ${OS_REL} (${OS_ARCH})"

if command -v nproc >/dev/null 2>&1; then
    CPU_COUNT="$(nproc)"
elif [ "${OS_NAME}" = "Darwin" ] && command -v sysctl >/dev/null 2>&1; then
    CPU_COUNT="$(sysctl -n hw.ncpu)"
else
    CPU_COUNT="unknown"
fi
echo "CPU:       ${CPU_COUNT} logical processors"

if [ -r /proc/meminfo ]; then
    MEM_TOTAL_KB="$(awk '/^MemTotal:/ {print $2; exit}' /proc/meminfo)"
    MEM_AVAIL_KB="$(awk '/^MemAvailable:/ {print $2; exit}' /proc/meminfo)"
    if [ -z "${MEM_AVAIL_KB:-}" ]; then
        MEM_AVAIL_KB="$(awk '/^MemFree:/ {print $2; exit}' /proc/meminfo)"
    fi
    echo "Memory:    $(( ${MEM_AVAIL_KB:-0} / 1048576 )) GiB available / $(( ${MEM_TOTAL_KB:-0} / 1048576 )) GiB total"
elif [ "${OS_NAME}" = "Darwin" ] && command -v sysctl >/dev/null 2>&1; then
    MEM_BYTES="$(sysctl -n hw.memsize)"
    echo "Memory:    $(( ${MEM_BYTES:-0} / 1073741824 )) GiB total"
fi

# Verify C++ runtime ABI required by native vector/ONNX wheels (model-free FTS5 works without it)
CPP_FOUND=0
if [ "${OS_NAME}" = "Darwin" ]; then
    if [ -e /usr/lib/libc++.1.dylib ] || [ -e /System/Library/Frameworks/Foundation.framework ] || xcode-select -p >/dev/null 2>&1; then
        echo "C++ ABI:   macOS libc++ runtime verified"
        CPP_FOUND=1
    else
        echo "[WARN] macOS C++ runtime / Command Line Tools not detected. Model-free SQLite FTS5 lexical recall works without it, while native Chroma dense vectors require libc++ (xcode-select --install)." >&2
    fi
else
    for candidate in /lib/x86_64-linux-gnu/libstdc++.so.6 /lib/aarch64-linux-gnu/libstdc++.so.6 /usr/lib/libstdc++.so.6 /usr/lib64/libstdc++.so.6; do
        if [ -e "${candidate}" ]; then
            echo "C++ ABI:   ${candidate} verified"
            CPP_FOUND=1
            break
        fi
    done
    if [ "${CPP_FOUND}" -eq 0 ] && command -v ldconfig >/dev/null 2>&1; then
        if ldconfig -p 2>/dev/null | grep -q "libstdc++.so.6"; then
            echo "C++ ABI:   libstdc++.so.6 verified via ldconfig"
            CPP_FOUND=1
        fi
    fi
    if [ "${CPP_FOUND}" -eq 0 ]; then
        echo "[WARN] libstdc++.so.6 not found. Model-free SQLite FTS5 lexical recall works without it, while native Chroma dense vectors require libstdc++6 (Debian/Ubuntu) or libstdc++ (Fedora/RHEL)." >&2
    fi
fi

# Inspect GPU drivers
if command -v nvidia-smi >/dev/null 2>&1; then
    GPU_SUMMARY="$(nvidia-smi --query-gpu=name,driver_version --format=csv,noheader 2>/dev/null | head -n 1 || true)"
    if [ -n "${GPU_SUMMARY}" ]; then
        echo "GPU:       NVIDIA (${GPU_SUMMARY})"
    else
        echo "GPU:       nvidia-smi present (no active CUDA device reported; CPU mode available)"
    fi
elif [ "${OS_NAME}" = "Darwin" ] && [ "${OS_ARCH}" = "arm64" ]; then
    echo "GPU:       Apple Silicon Unified GPU (Metal)"
else
    echo "GPU:       No discrete CUDA GPU detected (CPU inference mode)"
fi

# Locate or bootstrap uv
if command -v uv >/dev/null 2>&1; then
    UV_EXE="$(command -v uv)"
elif command -v python3.11 >/dev/null 2>&1; then
    echo "Installing user-scoped uv bootstrap via python3.11 pip..."
    python3.11 -m pip install --user uv
    USER_BASE="$(python3.11 -m site --user-base)"
    UV_EXE="${USER_BASE}/bin/uv"
    if [ ! -x "${UV_EXE}" ]; then
        echo "ERROR: uv installed, but executable not found at ${UV_EXE}. Add ~/.local/bin to PATH and rerun." >&2
        exit 1
    fi
else
    echo "ERROR: Install uv (https://docs.astral.sh/uv/) or python3.11, then rerun ./setup.sh." >&2
    exit 1
fi

"${UV_EXE}" python install 3.11

cd "${PROJECT_ROOT}"

echo "=== Synchronizing Locked Python Environment ==="
"${UV_EXE}" sync --frozen

echo "=== Checking Local Ollama Daemon & Models ==="
OLLAMA_RAW_HOST="${OLLAMA_HOST:-http://127.0.0.1:11434}"
case "${OLLAMA_RAW_HOST}" in
    http://*|https://*)
        OLLAMA_BASE_URL="${OLLAMA_RAW_HOST%/}"
        ;;
    *:*)
        OLLAMA_BASE_URL="http://${OLLAMA_RAW_HOST%/}"
        ;;
    *)
        OLLAMA_BASE_URL="http://${OLLAMA_RAW_HOST}:11434"
        ;;
esac
OLLAMA_TAGS_URL="${OLLAMA_BASE_URL}/api/tags"

fetch_ollama_tags() {
    if command -v curl >/dev/null 2>&1; then
        curl -fsS --max-time 3 "${OLLAMA_TAGS_URL}" 2>/dev/null
    else
        OLLAMA_TAGS_URL="${OLLAMA_TAGS_URL}" "${UV_EXE}" run --frozen python -c \
            "import os, sys, urllib.request; sys.stdout.write(urllib.request.urlopen(os.environ['OLLAMA_TAGS_URL'], timeout=2).read().decode('utf-8'))" 2>/dev/null
    fi
}

OLLAMA_REACHABLE=0
TAGS_JSON=""
if TAGS_JSON="$(fetch_ollama_tags)"; then
    OLLAMA_REACHABLE=1
    echo "Ollama reachable at ${OLLAMA_BASE_URL}"
else
    echo "Ollama daemon not reachable at ${OLLAMA_BASE_URL}; continuing in model-free FTS5 lexical mode."
fi

if [ "${PULL_MODELS}" -eq 1 ]; then
    if ! command -v ollama >/dev/null 2>&1 || [ "${OLLAMA_REACHABLE}" -eq 0 ]; then
        echo "ERROR: --pull-models requested, but ollama CLI or local daemon (${OLLAMA_BASE_URL}) is unavailable." >&2
        exit 1
    fi
    for model in nomic-embed-text llama3.2:3b; do
        echo "Pulling required local model: ${model} ..."
        ollama pull "${model}"
    done
    TAGS_JSON="$(fetch_ollama_tags || true)"
fi

echo "=== Initializing Vault & Disposable Indexes ==="
"${UV_EXE}" run --frozen second-brain install

if [ -n "${BRAIN_VAULT_DIR:-}" ]; then
    VAULT_DIR="${BRAIN_VAULT_DIR}"
else
    VAULT_DIR="${PROJECT_ROOT}/brain"
fi
MANIFEST_PATH="${VAULT_DIR}/.chroma/index-manifest.json"

if [ "${REBUILD_VECTORS}" -eq 1 ]; then
    echo "Building verified Chroma dense vector generation..."
    "${UV_EXE}" run --frozen python scripts/rebuild_vectors.py
elif [ "${CPP_FOUND}" -eq 1 ] && [ "${OLLAMA_REACHABLE}" -eq 1 ] && printf '%s' "${TAGS_JSON}" | grep -q "nomic-embed-text" && [ ! -f "${MANIFEST_PATH}" ]; then
    echo "Building verified Chroma dense vector generation..."
    if ! "${UV_EXE}" run --frozen python scripts/rebuild_vectors.py; then
        echo "[WARN] Dense vector index build failed; continuing with model-free SQLite FTS5 lexical recall." >&2
    fi
fi

echo "=== Running Environment & Health Verification ==="
"${UV_EXE}" run --frozen second-brain doctor
"${UV_EXE}" run --frozen python scripts/doctor.py

if [ "${NO_LAUNCH}" -eq 0 ]; then
    echo "Starting the local loopback dashboard (127.0.0.1:8501)."
    "${UV_EXE}" run --frozen second-brain dashboard
else
    echo "Setup is ready. Start the dashboard later with: uv run --frozen second-brain dashboard"
fi
