#!/usr/bin/env bash
set -e

# Local CI runner — mirrors .github/workflows/ci.yml
# Usage: ./tools/ci-local.sh

cd "$(dirname "$0")/.."
REPO_ROOT="$(pwd)"

# Use .venv if it exists, otherwise use system Python
if [ -d ".venv" ]; then
    PYTHON=".venv/bin/python"
    PYTEST=".venv/bin/pytest"
    RUFF=".venv/bin/ruff"
else
    PYTHON="python3"
    PYTEST="pytest"
    RUFF="ruff"
fi

echo "=== CI Local Runner ==="
echo "Python: $PYTHON"
echo "Repo: $REPO_ROOT"
echo ""

# 1. Ruff lint
echo "▶ Running ruff check on app, tests, migrations, tools..."
$RUFF check app tests migrations tools
echo "✓ Ruff passed"
echo ""

# 2. Pytest
echo "▶ Running pytest on tests..."
$PYTEST tests -q
echo "✓ Tests passed"
echo ""

# 3. Guard against legacy imports
echo "▶ Checking for legacy imports in app/tests/tools..."
if grep -rn "from legacy\|import legacy" app tests tools --include="*.py" 2>/dev/null; then
    echo "✗ ERROR: legacy imports detected"
    exit 1
else
    echo "✓ No legacy imports found"
fi
echo ""

# 4. Guard against tracked .env file
echo "▶ Checking for tracked .env file..."
if git ls-files | grep -x '.env'; then
    echo "✗ ERROR: .env file is tracked in git"
    exit 1
else
    echo "✓ .env file is properly gitignored"
fi
echo ""

echo "=== All checks passed ==="
