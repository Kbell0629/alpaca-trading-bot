#!/usr/bin/env bash
# compute_sri.sh — fetch each CDN-hosted JS dependency, compute its
# SHA-384 hash, and print the `integrity` attribute strings ready to
# paste into the template <script> tags.
#
# Run locally (not in the Claude Code sandbox — that blocks external
# hosts). Re-run whenever you bump a dependency version in the templates.
#
# Usage:
#   bash scripts/compute_sri.sh
#
# Output looks like:
#   templates/dashboard.html:29  chart.js@4.4.0
#     integrity="sha384-XXXXXXXXXXXXXXX"
#
# Copy the integrity="..." line and add it next to the matching <script>
# tag. The template already has `crossorigin="anonymous"` set; SRI
# requires that for cross-origin assets.

set -euo pipefail

if ! command -v openssl >/dev/null 2>&1; then
    echo "ERROR: openssl not found; install it or use another SHA-384 tool." >&2
    exit 1
fi

declare -a URLS=(
    "https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"
    "https://cdn.jsdelivr.net/npm/marked@11.1.1/marked.min.js"
    "https://cdn.jsdelivr.net/npm/zxcvbn@4.4.2/dist/zxcvbn.js"
)

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

for url in "${URLS[@]}"; do
    fname=$(basename "$url")
    dest="$TMP/$fname"
    printf '\n# %s\n' "$url"
    if ! curl -fsSL --max-time 15 -o "$dest" "$url"; then
        echo "  FETCH FAILED"
        continue
    fi
    size=$(wc -c < "$dest" | tr -d '[:space:]')
    if [ "$size" -lt 100 ]; then
        echo "  SUSPICIOUSLY SMALL FILE (${size} bytes) — check the URL"
        continue
    fi
    hash=$(openssl dgst -sha384 -binary "$dest" | openssl base64 -A)
    printf '  size: %s bytes\n' "$size"
    printf '  integrity="sha384-%s"\n' "$hash"
done
