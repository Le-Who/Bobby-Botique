# Required for consistent pytest collection when rootdir uses src-layout style imports.
# Without this, pytest may fail to resolve relative imports in some configurations
# when running tests/e2e/ in isolation (e.g. pytest tests/e2e/).
