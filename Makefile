PYTHON ?= python3

.PHONY: test test-openclaw lint

# tests/test_mcp.py runs when the mcp package is installed (pip install '.[mcp]'), and skips otherwise.
test:
	$(PYTHON) -W error::ResourceWarning -m unittest discover -s tests -v

# The OpenClaw plugin's tests need Node 24 or newer (it strips the TypeScript types).
NODE ?= node
test-openclaw:
	$(NODE) --test integrations/openclaw/test/*.test.ts

lint:
	ruff check --no-cache --select E,F,W,B --ignore E501 --target-version py314 .
