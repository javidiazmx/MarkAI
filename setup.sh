#!/usr/bin/env bash
# One-time setup: creates a virtual environment, installs Mark, and runs `mark init`.
# Usage:  bash setup.sh
set -euo pipefail

cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is not installed. Install Python 3.11 or newer from https://www.python.org/downloads/"
  exit 1
fi

python3 - <<'PY'
import sys
if sys.version_info < (3, 11):
    sys.exit(
        f"Python 3.11 or newer is required (found {sys.version.split()[0]}). "
        "Install a newer Python and run this again."
    )
PY

if [ ! -d .venv ]; then
  echo "Creating the virtual environment in .venv ..."
  python3 -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

echo "Installing Mark and its dependencies (this takes a minute) ..."
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -e ".[dev]"

echo
mark init

cat <<'EOF'

Setup is done. Every time you open a new terminal, run this first:

    source .venv/bin/activate

Then:

    mark doctor        check everything is in place
    mark ingest        build the knowledge base from sources/sources.yaml
    mark chat          talk to Mark in the terminal
    mark serve         open the browser page

EOF
