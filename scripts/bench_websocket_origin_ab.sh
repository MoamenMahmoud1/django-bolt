#!/usr/bin/env bash
# Repeatable A/B of the SAME real-Rust Origin routing benchmark.
# Usage: scripts/bench_websocket_origin_ab.sh [results_dir]
# Override BEFORE_REF / AFTER_REF when comparing other commits.
set -euo pipefail

REPO="$(git rev-parse --show-toplevel)"
BEFORE_REF="${BEFORE_REF:-89badcb4dc9e2d35c215077789cdabf8268b7aac}"
AFTER_REF="${AFTER_REF:-fix/asgi-websocket-origin-boundary}"
RESULTS_DIR="${1:-$REPO/websocket-origin-ab-results}"
mkdir -p "$RESULTS_DIR"
RESULTS_DIR="$(cd "$RESULTS_DIR" && pwd)"
TMP="$(mktemp -d)"
BEFORE="$TMP/before"
AFTER="$TMP/after"

cleanup() {
    git -C "$REPO" worktree remove --force "$BEFORE" >/dev/null 2>&1 || true
    git -C "$REPO" worktree remove --force "$AFTER" >/dev/null 2>&1 || true
    rm -rf "$TMP"
}
trap cleanup EXIT

git -C "$REPO" rev-parse --verify "$BEFORE_REF^{commit}" >/dev/null
git -C "$REPO" rev-parse --verify "$AFTER_REF^{commit}" >/dev/null
git -C "$REPO" worktree add --detach "$BEFORE" "$BEFORE_REF"
git -C "$REPO" worktree add --detach "$AFTER" "$AFTER_REF"

# The benchmark is new: copy the EXACT same script into the old tree.
cp "$AFTER/python/benchmark/websocket_origin_benchmark.py" \
   "$BEFORE/python/benchmark/websocket_origin_benchmark.py"

run_case() {
    local path="$1"
    local name="$2"
    (
        cd "$path"
        uv sync
        uv run maturin develop --release
        uv run python python/benchmark/websocket_origin_benchmark.py \
            --count 3000 --repeats 9 --output "$RESULTS_DIR/$name.json" \
            "${@:3}"
    )
}

echo "BEFORE: $(git -C "$BEFORE" rev-parse HEAD)"
run_case "$BEFORE" before
echo "AFTER:  $(git -C "$AFTER" rev-parse HEAD)"
run_case "$AFTER" after --compare "$RESULTS_DIR/before.json"
echo "Baseline and changed results: $RESULTS_DIR/{before,after}.json"
echo "Interpret with care: repeat on an idle machine using the same Rust/Python toolchain."
