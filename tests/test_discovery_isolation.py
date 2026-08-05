"""Order-safety contract tests for full unittest discovery (issue #91).

These tests prove that the test modules which previously mutated process-global
state at import time no longer contaminate the rest of discovery:

1. ``tests/runtime/test_mnemosyne_retrieval_quality.py`` and
   ``tests/runtime/test_mnemosyne_pilot.py`` must NOT leave ``scripts/`` on
   ``sys.path`` after their module import, otherwise ``import tasknotes_mcp``
   during discovery resolves to ``scripts/tasknotes_mcp.py`` (which needs the
   ``mcp`` package) instead of the ``tests/tasknotes_mcp`` package.

2. The aux-ml test modules that stub ``httpx``/``pymupdf`` must NOT leave a fake
   ``httpx`` in ``sys.modules`` for the whole process, otherwise the Mnemosyne
   DR seam imports (which import ``mcp`` -> ``httpx_sse`` -> real
   ``httpx.TransportError``) break.

These run as part of the normal fast suite (no Docker, no RUN_DOCKER_TESTS).
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]


def _is_real_httpx(module: object) -> bool:
    """True if ``module`` is the real httpx package (has TransportError)."""
    return hasattr(module, "TransportError")


class DiscoverySysPathIsolationTests(unittest.TestCase):
    """The Mnemosyne runtime test modules must not leave scripts/ on sys.path."""

    def _import_module(self, dotted: str):
        # Force a fresh import so the module-level sys.path mutation runs.
        sys.modules.pop(dotted, None)
        return importlib.import_module(dotted)

    def test_mnemosyne_retrieval_quality_does_not_leave_scripts_on_sys_path(self) -> None:
        scripts = str(REPO_ROOT / "scripts")
        before = scripts in sys.path
        try:
            self._import_module("tests.runtime.test_mnemosyne_retrieval_quality")
        finally:
            if before and scripts not in sys.path:
                # Restore the caller's pre-existing entry if we removed it.
                sys.path.insert(0, scripts)
        self.assertNotIn(scripts, sys.path)

    def test_mnemosyne_pilot_does_not_leave_scripts_on_sys_path(self) -> None:
        scripts = str(REPO_ROOT / "scripts")
        before = scripts in sys.path
        try:
            self._import_module("tests.runtime.test_mnemosyne_pilot")
        finally:
            if before and scripts not in sys.path:
                sys.path.insert(0, scripts)
        self.assertNotIn(scripts, sys.path)

    def test_tasknotes_mcp_package_resolves_to_tests_not_scripts(self) -> None:
        # After importing the Mnemosyne modules, ``import tasknotes_mcp`` must
        # resolve to the tests/tasknotes_mcp package (which has __init__.py),
        # not scripts/tasknotes_mcp.py. The package path proves the resolution.
        self._import_module("tests.runtime.test_mnemosyne_retrieval_quality")
        self._import_module("tests.runtime.test_mnemosyne_pilot")
        # Mimic `unittest discover -s tests` which puts the start dir on sys.path.
        tests_dir = str(REPO_ROOT / "tests")
        sys.path.insert(0, tests_dir)
        try:
            pkg = importlib.import_module("tasknotes_mcp")
        finally:
            while tests_dir in sys.path:
                sys.path.remove(tests_dir)
        self.assertTrue(
            str(Path(pkg.__file__).resolve()).startswith(
                str((REPO_ROOT / "tests" / "tasknotes_mcp").resolve())
            ),
            f"tasknotes_mcp resolved to {pkg.__file__}, expected the tests package",
        )


class DiscoveryHttpxIsolationTests(unittest.TestCase):
    """The aux-ml stub modules must not poison real httpx for the whole process."""

    AUX_STUB_MODULES = (
        "tests.aux_ml.test_service_cancel",
        "tests.aux_ml.test_cancel_endpoint",
        "tests.aux_ml.test_llama_router",
        "tests.aux_ml.test_transcribe_granite",
        "tests.aux_ml.test_transcribe_normalization",
    )

    def _import_fresh(self, dotted: str):
        sys.modules.pop(dotted, None)
        # Clear any app.* modules cached by other tests so the aux module's
        # _app_before snapshot is clean and its eviction logic is meaningful.
        for _k in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
            sys.modules.pop(_k, None)
        return importlib.import_module(dotted)

    def test_aux_modules_do_not_leave_fake_httpx_in_sys_modules(self) -> None:
        for dotted in self.AUX_STUB_MODULES:
            self._import_fresh(dotted)
        httpx_mod = sys.modules.get("httpx")
        # After importing the aux modules, httpx must be either the real package
        # (with TransportError) or absent — never a bare ModuleType stub.
        if httpx_mod is not None:
            self.assertTrue(
                _is_real_httpx(httpx_mod),
                f"sys.modules['httpx'] is a fake stub after aux imports: {httpx_mod!r}",
            )

    def test_real_httpx_transport_error_available_after_aux_imports(self) -> None:
        # Importing the aux modules must not prevent the real httpx (needed by
        # the Mnemosyne DR seam via mcp/httpx_sse) from importing cleanly.
        for dotted in self.AUX_STUB_MODULES:
            self._import_fresh(dotted)
        # Drop any cached httpx so the next import re-runs against the real pkg.
        sys.modules.pop("httpx", None)
        sys.modules.pop("httpx_sse", None)
        import httpx  # noqa: E402
        self.assertTrue(
            hasattr(httpx, "TransportError"),
            "real httpx.TransportError missing after aux imports (httpx poisoned)",
        )

    def test_mnemosyne_dr_seam_imports_after_aux_modules(self) -> None:
        # Order-safety: the Mnemosyne DR seam (which imports mcp -> httpx_sse ->
        # real httpx) must import cleanly after the aux stub modules have run.
        for dotted in self.AUX_STUB_MODULES:
            self._import_fresh(dotted)
        # The backup test's DR seam helper path is importable via the extracted
        # package; here we just prove the httpx/mcp import chain works.
        sys.modules.pop("httpx", None)
        sys.modules.pop("httpx_sse", None)
        sys.modules.pop("mcp", None)
        import httpx  # noqa: E402
        import httpx_sse  # noqa: E402
        from mcp.server.fastmcp import FastMCP  # noqa: E402
        self.assertTrue(hasattr(httpx, "TransportError"))
        self.assertTrue(hasattr(httpx_sse, "EventSource"))
        self.assertTrue(callable(FastMCP))

    def test_each_aux_module_leaves_app_llama_router_using_real_httpx(self) -> None:
        # After importing each aux test module, a subsequent fresh import of
        # app.llama_router must bind the REAL httpx (with TransportError), not a
        # fake stub cached during the aux module's stubbed application import
        # (issue #91). app.llama_router is the relevant contamination path for
        # MCP/Mnemosyne (mcp -> httpx_sse -> real httpx.TransportError).
        #
        # We deliberately do NOT import app.service here: it transitively pulls
        # the optional `pymupdf` production dependency, which is excluded from
        # requirements-test.txt. The required invariant — no fake-loaded
        # app/app.* modules remain cached after each aux import — is verified
        # independently by test_aux_modules_do_not_cache_app_modules_with_fake_deps.
        # aux-ml/ must be on sys.path for the app.* import.
        aux_ml = str(REPO_ROOT / "aux-ml")
        for dotted in self.AUX_STUB_MODULES:
            self._import_fresh(dotted)
            # Drop any cached app.* so the next import re-executes against
            # the real dependencies.
            for _k in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
                sys.modules.pop(_k, None)
            sys.path.insert(0, aux_ml)
            try:
                import app.llama_router as lr  # noqa: E402
            finally:
                while aux_ml in sys.path:
                    sys.path.remove(aux_ml)
            self.assertTrue(
                hasattr(getattr(lr, "httpx", None), "TransportError"),
                f"{dotted}: app.llama_router.httpx is not real httpx after aux import: "
                f"{getattr(lr, 'httpx', None)!r}",
            )
            # Clean up for the next iteration.
            for _k in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
                sys.modules.pop(_k, None)

    def test_aux_modules_do_not_cache_app_modules_with_fake_deps(self) -> None:
        # After importing each aux module, NO app/app.* module cached during
        # the stubbed import may remain in sys.modules (the cancel tests now
        # evict all app.* — including app.service — and patch via bound module
        # objects, so nothing stub-bound can leak). A later fresh import of any
        # app.* module must therefore bind the real dependencies.
        for dotted in self.AUX_STUB_MODULES:
            self._import_fresh(dotted)
            cached_app = sorted(k for k in sys.modules if k == "app" or k.startswith("app."))
            self.assertEqual(
                cached_app,
                [],
                f"{dotted}: stubbed import left cached app.* modules: {cached_app}",
            )

    def test_pre_existing_app_package_attrs_restored_after_aux_import(self) -> None:
        # The subtle case (issue #91): if the `app` package already existed
        # before the stubbed import, fake-loaded children set attributes on the
        # `app` package object (e.g. `app.llama_router`). The shared helper must
        # restore the `app` package's __dict__ so neither `import app.child` nor
        # `from app import child` can reuse a fake-bound object. This is
        # exercised per aux module with a pre-existing real `app` package.
        aux_ml = str(REPO_ROOT / "aux-ml")
        for dotted in self.AUX_STUB_MODULES:
            # Start clean: no app.* cached, and drop the test module so its
            # module-level stubbed import re-runs.
            for _k in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
                sys.modules.pop(_k, None)
            sys.modules.pop(dotted, None)
            # Pre-import the real app package + a real child so the package
            # exists and has real attributes before the stubbed import.
            sys.path.insert(0, aux_ml)
            try:
                import app as pre_app  # noqa: F401
                import app.llama_router as pre_lr  # noqa: F401
            finally:
                while aux_ml in sys.path:
                    sys.path.remove(aux_ml)
            pre_app_id = id(pre_app)
            pre_attrs = set(pre_app.__dict__.keys())
            # Import the aux test module (runs the stubbed app import). Do NOT
            # clear app.* first — the pre-existing app package must remain so
            # the helper's restoration of its __dict__ is exercised.
            importlib.import_module(dotted)
            # The pre-existing app package object must still be the same object
            # and present in sys.modules.
            self.assertIn("app", sys.modules)
            self.assertEqual(id(sys.modules["app"]), pre_app_id)
            # No attributes added by fake-loaded children may remain; changed
            # attributes must be restored to the real values.
            post_app = sys.modules["app"]
            post_attrs = set(post_app.__dict__.keys())
            added = post_attrs - pre_attrs
            self.assertEqual(
                added,
                set(),
                f"{dotted}: app package gained stale attributes from fake-loaded "
                f"children: {sorted(added)}",
            )
            # Both import forms must yield the REAL httpx, not a fake stub.
            for _k in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
                sys.modules.pop(_k, None)
            sys.path.insert(0, aux_ml)
            try:
                import app.llama_router as lr_import  # noqa: E402
                from app import llama_router as lr_from  # noqa: E402
            finally:
                while aux_ml in sys.path:
                    sys.path.remove(aux_ml)
            for label, lr in (("import app.child", lr_import), ("from app import child", lr_from)):
                self.assertTrue(
                    hasattr(getattr(lr, "httpx", None), "TransportError"),
                    f"{dotted}: {label} -> app.llama_router.httpx is not real: "
                    f"{getattr(lr, 'httpx', None)!r}",
                )
            # Clean up for the next iteration.
            for _k in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
                sys.modules.pop(_k, None)


if __name__ == "__main__":
    unittest.main()
