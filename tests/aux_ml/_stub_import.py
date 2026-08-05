"""Shared test helper for stubbing optional deps around an app import (issue #91).

The aux-ml test modules need to import the ``app`` package without the full
aux-ml runtime installed. They temporarily stub optional deps (``httpx``,
``pymupdf``) in ``sys.modules`` for the duration of the application import, then
must fully restore process-global state so the stubs cannot leak.

The subtle failure mode this helper guards against: if the ``app`` package
already existed in ``sys.modules`` before the stubbed import, fake-loaded
children set attributes on the ``app`` package object (e.g.
``app.llama_router``). Merely evicting the new ``sys.modules`` entries leaves
those package attributes bound to fake-loaded modules, so a later
``from app import llama_router`` reuses the fake-bound object without
re-importing. This helper snapshots and restores BOTH the ``sys.modules``
``app``/``app.*`` state AND the ``app`` package object's ``__dict__``
attributes, so neither ``import app.child`` nor ``from app import child`` can
reuse fake-bound objects.

This is test-only infrastructure; it must not be imported by production code.
"""

from __future__ import annotations

import contextlib
import sys
import types


_MISSING = object()


@contextlib.contextmanager
def stubbed_app_import(*dep_names: str):
    """Context manager that stubs ``dep_names`` in ``sys.modules``, runs the
    body (which should perform the ``app.*`` imports), then fully restores:

    - the stubbed deps in ``sys.modules`` (removed if absent before, restored
      otherwise);
    - the exact prior ``sys.modules`` state for every ``app``/``app.*`` key
      (new keys removed, evicted keys restored);
    - the ``__dict__`` of every ``app``/``app.*`` module that existed before
      (added attributes removed, changed attributes restored), so a pre-existing
      ``app`` package cannot retain fake-bound child attributes.

    Bound objects imported inside the body remain valid references
    independent of ``sys.modules``.
    """
    saved_deps = {name: sys.modules.get(name, _MISSING) for name in dep_names}
    app_keys = [k for k in sys.modules if k == "app" or k.startswith("app.")]
    saved_app_modules = {k: sys.modules.get(k, _MISSING) for k in app_keys}
    # Snapshot the __dict__ of every app/app.* module that exists before, so we
    # can remove attributes added by fake-loaded children and restore changed
    # ones. Copy values so restoration is exact.
    saved_app_attrs = {
        k: dict(sys.modules[k].__dict__) for k in app_keys if k in sys.modules
    }
    for name in dep_names:
        sys.modules[name] = types.ModuleType(name)
    try:
        yield
    finally:
        # 1. Restore the stubbed deps.
        for name, orig in saved_deps.items():
            if orig is _MISSING:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = orig
        # 2. Restore the exact prior sys.modules app/app.* state: remove every
        # app/app.* key that was not present before, and restore every key that
        # was present before to its original module object.
        for k in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
            if k not in saved_app_modules:
                sys.modules.pop(k, None)
        for k, orig in saved_app_modules.items():
            if orig is _MISSING:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = orig
        # 3. Restore the __dict__ of every app/app.* module that existed before,
        # removing attributes added by fake-loaded children and restoring changed
        # ones. This prevents `from app import child` from reusing fake-bound
        # objects left on a pre-existing app package.
        for k, saved_dict in saved_app_attrs.items():
            mod = sys.modules.get(k)
            if mod is None:
                continue
            d = mod.__dict__
            for attr in list(d.keys()):
                if attr not in saved_dict:
                    del d[attr]
            for attr, val in saved_dict.items():
                d[attr] = val