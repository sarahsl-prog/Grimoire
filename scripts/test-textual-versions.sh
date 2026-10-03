#!/usr/bin/env bash
# Run the TUI test suite against several Textual releases.
#
# pyproject.toml allows `textual>=8.2,<9`, but a range is only a claim until it
# has been run.  This installs each version into a throwaway directory that is
# put ahead of the active environment on PYTHONPATH, so nothing in the
# environment itself is changed, and runs `pytest tests/tui` against it.
#
# Usage (from the repository root, in the dev environment):
#   scripts/test-textual-versions.sh                  # every tested release
#   scripts/test-textual-versions.sh 8.2.0 8.2.8      # just these
#
# Environment:
#   PYTHON       interpreter to use (default: python)
#   PYTEST_ARGS  extra arguments for pytest (default: -n auto -q)
#
# Needs `uv` and the dev extra (for pytest-xdist).  Exits non-zero if any
# version fails, after trying all of them.
set -uo pipefail

# Every 8.2.x release the suite has been run against.  Add a version here, and to
# the README, only after it has passed.
DEFAULT_VERSIONS=(8.2.0 8.2.1 8.2.2 8.2.3 8.2.4 8.2.5 8.2.6 8.2.7 8.2.8)

PYTHON="${PYTHON:-python}"
PYTEST_ARGS="${PYTEST_ARGS:--n auto -q}"
versions=("$@")
if [ "${#versions[@]}" -eq 0 ]; then
  versions=("${DEFAULT_VERSIONS[@]}")
fi

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

failed=()
for version in "${versions[@]}"; do
  echo "=== textual ${version} ==="
  target="${work}/textual-${version}"
  if ! uv pip install --quiet --python "$PYTHON" --target "$target" "textual==${version}"; then
    echo "could not install textual ${version}" >&2
    failed+=("${version} (install)")
    continue
  fi
  # Confirm the override took effect before trusting the result.
  got="$(PYTHONPATH="$target" "$PYTHON" -c 'import textual; print(textual.__version__)')"
  if [ "$got" != "$version" ]; then
    echo "expected textual ${version} but imported ${got}" >&2
    failed+=("${version} (wrong version imported: ${got})")
    continue
  fi
  # shellcheck disable=SC2086  # PYTEST_ARGS is meant to word-split
  if ! PYTHONPATH="$target" "$PYTHON" -m pytest tests/tui -p no:cacheprovider $PYTEST_ARGS; then
    failed+=("${version}")
  fi
done

echo
if [ "${#failed[@]}" -eq 0 ]; then
  echo "All ${#versions[@]} Textual version(s) passed."
else
  echo "FAILED: ${failed[*]}" >&2
  exit 1
fi
