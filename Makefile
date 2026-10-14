PYTHON ?= python3
VERSION := $(shell sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml)
PKG_VERSION := $(shell sed -n 's/^__version__ = "\(.*\)"/\1/p' src/pgddl/__init__.py)
TAG := v$(VERSION)

.PHONY: help test build check-release release

help:
	@echo "make test     run the test suite (set PGDDL_TEST_DSN for integration tests)"
	@echo "make build    build sdist and wheel into dist/"
	@echo "make release  tag v<version> from pyproject.toml, push it and create a GitHub release"

test:
	$(PYTHON) -m unittest discover -s tests -v

build:
	rm -rf dist build
	$(PYTHON) -m build
	$(PYTHON) -m twine check dist/*

check-release:
	@test -n "$(VERSION)" || { echo "cannot read version from pyproject.toml"; exit 1; }
	@test "$(VERSION)" = "$(PKG_VERSION)" || { echo "version mismatch: pyproject=$(VERSION) __init__=$(PKG_VERSION)"; exit 1; }
	@test "$$(git branch --show-current)" = "main" || { echo "release must be run from main"; exit 1; }
	@test -z "$$(git status --porcelain)" || { echo "working tree is not clean; commit or stash first"; exit 1; }
	@git fetch origin main --tags --quiet
	@test "$$(git rev-parse HEAD)" = "$$(git rev-parse origin/main)" || { echo "local main differs from origin/main; push or pull first"; exit 1; }
	@! git rev-parse -q --verify "refs/tags/$(TAG)" >/dev/null || { echo "tag $(TAG) already exists; bump the version"; exit 1; }
	@command -v gh >/dev/null || { echo "gh CLI is required"; exit 1; }

# Publishing the release triggers .github/workflows/publish.yml, which uploads to PyPI.
release: check-release
	git tag -a "$(TAG)" -m "Release $(TAG)"
	git push origin "$(TAG)"
	gh release create "$(TAG)" --verify-tag --title "$(TAG)" --generate-notes
