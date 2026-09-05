# list all targets
default:
    @just --list

# list all variables
var:
    @just --evaluate

# run formatters
fmt:
    uv run ruff format src tests
    uv run ruff check --fix --unsafe-fixes src tests

# lint the code
lint:
    uv run ruff format --check --diff src tests
    uv run ruff check src tests

# lint using pyright
lint-pyright:
    PYRIGHT_PYTHON_FORCE_VERSION=latest uv run pyright src tests

# run all linters
lint-all:
    just lint
    just lint-pyright

# find dead code with vulture
# Textual dispatches by name: an action_* method is reached through a Binding
# string, an on_* handler through an event class, and BINDINGS/CSS/TITLE/
# AUTO_FOCUS are read by the framework. Vulture sees none of that.
vulture:
    uv run vulture src tests vulture_whitelist.py \
      --ignore-names 'action_*,on_*,watch_*,validate_*,compose,BINDINGS,CSS,TITLE,AUTO_FOCUS,cursor_type,sub_title'

# run tests
test:
    uv run pytest --timeout 30 -n 8 tests

# run only tests affected by code changes since last run
test-changed:
    uv run pytest --testmon --timeout 60 -n 8 tests

# run all tests with coverage
test-all:
    COVERAGE_CORE=sysmon uv run pytest --timeout 60 -n 8 tests --cov-report=html --cov=src/cboard2

# show next possible versions (patch or minor bump)
next:
    #!/usr/bin/env bash
    set -euo pipefail
    LATEST=$(git tag -l 'v[0-9]*.[0-9]*.[0-9]*' | sed 's/^v//; s/-rc\..*//' | sort -t. -k1,1n -k2,2n -k3,3n -u | tail -1)
    LATEST=${LATEST:-0.0.0}
    IFS='.' read -r MAJOR MINOR PATCH <<< "$LATEST"
    RC=$(git tag -l "v${LATEST}-rc.*" | sort -V | tail -1 | sed -n 's/.*-rc\.//p')
    RELEASED=$(git tag -l "v${LATEST}" | head -1)
    if [ -n "$RC" ] && [ -z "$RELEASED" ]; then
        echo "Current: ${LATEST} (rc.${RC}, unreleased)"
    else
        echo "Current: ${LATEST}"
    fi
    echo "  patch: ${MAJOR}.${MINOR}.$((PATCH + 1))"
    echo "  minor: ${MAJOR}.$((MINOR + 1)).0"

# prepare a release: create RC tag, push branch, open PR
release-prep *args:
    ./scripts/release-prep {{args}}
    git pull origin main
