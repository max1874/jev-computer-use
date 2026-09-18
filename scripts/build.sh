#!/bin/bash
# Build the accessibility bridge. Prints the binary path; skips the compile when
# the source has not changed. No dependencies beyond the Swift toolchain.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
src="$root/jev_computer_use/axbridge.swift"
out="$root/jev_computer_use/bin/axbridge"
mkdir -p "$(dirname "$out")"
if [[ ! -x "$out" || "$src" -nt "$out" ]]; then
  swiftc -O -o "$out" "$src" >&2
fi
echo "$out"
