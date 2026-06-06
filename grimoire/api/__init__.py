"""FastAPI REST API for Grimoire."""

__all__ = ["app", "create_app"]

# Lazy imports to avoid triggering app construction at package import time.
_app = None
_create_app = None


def __getattr__(name: str):
    global _app, _create_app
    if name == "app" or name == "create_app":
        from grimoire.api.main import app as _app, create_app as _create_app
        if name == "app":
            return _app
        return _create_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")