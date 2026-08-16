"""Docker-gated physical-copy portability proof for the vault-recovery
exporter (Phase 1).

The whole phase-1 feature is release-blocked on this proof (see
.slim/deepwork/vault-recovery-dr.md): a real pinned gbrain image must be able
to copy a FULL `.gbrain` tree (physical copy, not a logical dump) and then
open the restored tree with the doctor, with REAL vector-bearing DB state
(actual pgvector rows created through the pinned gbrain embedding workflow),
DB-only records, config, schema-pack files/markers surviving — without any
reindex/rebuild/sync.

Flow (all inside a disposable, isolated Hermes container; project volumes
only; never production state):

  1. `gbrain-native init --pglite --no-embedding` into a fresh /opt/data.
  2. Pages + a DB-only manual link; schema-pack files + `active-schema-pack`
     marker.
  3. REAL vector state through the pinned gbrain embedding workflow
     (issue #65, the exact enable-embeddings + embed-backfill sequence at
     the native level): a stub OpenAI-compatible embeddings endpoint serves
     deterministic 384-dim vectors, then `migrate embeddings --no-embed`
     persists the tuple and clears the `embedding_disabled` sentinel, and
     `embed --stale --include-null-signature` stamps REAL vector rows into
     the PGLite DB. The `embedding-backfill-complete.json` marker is written
     with the real model tuple.
  4. Semantic search on the LIVE tree returns the expected page (proves the
     vectors are real and usable, not config sentinels).
  5. Live doctor preflight validated with the PRODUCTION validator
     (vault_recovery_core.validate_doctor_report imported in-container).
  6. Run the PRODUCTION export wrapper (lock runner + core) as the hermes
     user; assert the staged generation layout and manifest.
  7. Physically copy the staged generation into a fresh restore root
     (/tmp/vr-restore) — the same operation a future restore would perform.
  8. Open the restored tree with the real pinned doctor and re-validate;
     read back the DB-only link, page content, config keys, and markers.
  9. REAL restored vector proof: semantic search on the RESTORED tree still
     returns the page, and `embed --stale --dry-run` finds zero stale rows —
     the vectors + signatures survived the physical copy with no
     reindex/rebuild/sync.
 10. Asserts the staged trees are byte-identical to the live sources AS OF
     the converged scan via the manifest convergence record: the manifest's
     scan_digest equals its staged_digest (the exporter enforced staged ==
     live before publishing) and a re-scan of the immutable staged trees
     reproduces the manifest's staged_digest. A direct end-of-test live
     re-scan is not compared because PGLite background checkpoints may
     rewrite live files at any moment; the exporter's own pre-publication
     convergence is the deterministic, race-free proof.

Local runs skip unless RUN_DOCKER_TESTS=1 and the docker CLI is available.
Release/deploy runs (CI) set VAULT_RECOVERY_PORTABILITY_REQUIRED=1: the
opt-in env var is then IGNORED and a missing docker CLI FAILS the test — the
proof is mandatory and cannot be bypassed.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import time
import unittest
from pathlib import Path
from unittest import mock

from .helpers import ComposeRuntime, docker_available

REPO_ROOT = Path(__file__).resolve().parents[2]

GBRAIN_ENV = (
    "GBRAIN_HOME=/opt/data GBRAIN_BRAIN_REPO=/opt/data/obsidian "
    "GBRAIN_SCHEMA_PACK=josemar GBRAIN_SKIP_STARTUP_HOOKS=1 "
    "HOME=/opt/data XDG_CONFIG_HOME=/opt/data/.config"
)
NATIVE = "/opt/josemar/libexec/gbrain-native"
STAGING = "/opt/data/vault-recovery/staging"

# Pinned E5 model tuple (issue #65 / .env.example defaults). The gbrain
# provider id carries the `llama-server:` prefix exactly like the production
# compose overlay wires it; the migration signature includes the revision, so
# these must match what the stub serves.
EMBEDDING_MODEL = "llama-server:intfloat/multilingual-e5-small"
EMBEDDING_MODEL_BARE = "intfloat/multilingual-e5-small"
EMBEDDING_DIMENSIONS = "384"
EMBEDDING_REVISION = "614241f622f53c4eeff9890bdc4f31cfecc418b3"
EMBED_STUB_PORT = 8799
EMBED_STUB_URL = f"http://127.0.0.1:{EMBED_STUB_PORT}/v1"

EMBED_ENV = (
    f"GBRAIN_EMBEDDING_MODEL={EMBEDDING_MODEL} "
    f"GBRAIN_EMBEDDING_DIMENSIONS={EMBEDDING_DIMENSIONS} "
    f"GBRAIN_EMBEDDING_MODEL_REVISION={EMBEDDING_REVISION} "
    f"LLAMA_SERVER_BASE_URL={EMBED_STUB_URL} "
    "LLAMA_SERVER_API_KEY=test-key "
    "GBRAIN_EMBED_CONCURRENCY=1"
)

# A deterministic, OpenAI-shaped embeddings stub (TEI-compatible subset):
#   GET  /health, /info
#   POST /v1/embeddings  {"model", "input": str | [str]} ->
#       {"data": [{"embedding": [384 floats], "index": i}],
#        "usage": {"prompt_tokens", "total_tokens"}}
# The pinned client's migration preflight rejects a response without the
# usage token counts (observed: "Invalid JSON response" with usage: {}).
# Vectors are derived from the input's word set (hashed bag of words,
# normalized), so texts sharing content words get similar vectors and the
# semantic search meaningfully ranks the expected page.
EMBED_STUB_SOURCE = r'''
import hashlib, json, math
from http.server import BaseHTTPRequestHandler, HTTPServer

DIM = 384
MODEL = %(model)r
REVISION = %(revision)r

def vec_for(text):
    v = [0.0] * DIM
    for tok in text.lower().split():
        idx = int.from_bytes(hashlib.sha256(tok.encode()).digest()[:4], 'big') %% DIM
        v[idx] += 1.0
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [round(x / norm, 8) for x in v]

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/health", "/healthz"):
            self._send(200, {"status": "healthy"})
        elif self.path == "/info":
            self._send(200, {
                "model_id": MODEL,
                "model_revision": REVISION,
                "sha": REVISION,
                "max_batch_tokens": 16384,
                "max_batch_requests": 64,
                "max_client_batch_size": 64,
            })
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self.path.endswith("/embeddings"):
            self._send(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        req = json.loads(self.rfile.read(length))
        inputs = req.get("input")
        if isinstance(inputs, str):
            inputs = [inputs]
        data = [
            {"embedding": vec_for(text), "index": i, "object": "embedding"}
            for i, text in enumerate(inputs)
        ]
        tokens = sum(len(text.split()) for text in inputs)
        self._send(200, {
            "data": data,
            "model": req.get("model", MODEL),
            "object": "list",
            "usage": {"prompt_tokens": tokens, "total_tokens": tokens},
        })

HTTPServer(("127.0.0.1", %(port)d), Handler).serve_forever()
''' % {"model": EMBEDDING_MODEL, "revision": EMBEDDING_REVISION,
       "port": EMBED_STUB_PORT}


def _hermes_script(script: str) -> list[str]:
    """Wrap a shell fragment as the hermes runtime user (non-root, issue #110)."""
    return ["su", "-s", "/bin/sh", "hermes", "-c", script]


class VaultRecoveryPortabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.required = os.getenv("VAULT_RECOVERY_PORTABILITY_REQUIRED") == "1"
        self.runtime = ComposeRuntime()
        # A developer .env may carry a production LAN bind IP
        # (HERMES_API_SERVER_BIND_IP) that this host cannot bind, and local
        # dev processes may already own the default published ports. Pin both
        # published ports to localhost high ports so the disposable runtime
        # always starts.
        self.runtime.env["HERMES_API_SERVER_BIND_IP"] = "127.0.0.1"
        self.runtime.env["HERMES_DASHBOARD_BIND_IP"] = "127.0.0.1"
        self.runtime.env["HERMES_API_SERVER_PORT"] = "18642"
        self.runtime.env["HERMES_DASHBOARD_PORT"] = "19119"
        self.addCleanup(self.runtime.down)

    def _run(self, script: str, *, timeout: int = 300, check: bool = True) -> subprocess.CompletedProcess[str]:
        proc = self.runtime.exec(
            "hermes", *_hermes_script(script), check=False, timeout=timeout
        )
        if check and proc.returncode != 0:
            self.fail(
                f"hermes command failed ({proc.returncode}): {script}\n"
                f"stdout: {proc.stdout[-3000:]}\nstderr: {proc.stderr[-3000:]}"
            )
        return proc

    def _validate_doctor(self, report: dict) -> dict:
        """Validate a doctor report with the PRODUCTION validator
        (vault_recovery_core imported in-container). The report travels as
        base64 to avoid shell/JSON quoting issues."""
        b64 = base64.b64encode(json.dumps(report).encode()).decode()
        proc = self._run(
            f"echo {b64} | base64 -d > /tmp/doctor-report.json && "
            "python3 - <<'PY'\n"
            "import importlib.util, json\n"
            "spec = importlib.util.spec_from_file_location("
            "'vault_recovery_core', '/opt/josemar/scripts/vault_recovery_core.py')\n"
            "mod = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(mod)\n"
            "with open('/tmp/doctor-report.json') as f: report = json.load(f)\n"
            "print(json.dumps(mod.validate_doctor_report(report)))\n"
            "PY"
        )
        return json.loads(proc.stdout)

    def _doctor_json(self, env: str) -> dict:
        proc = self._run(f"{env} {NATIVE} doctor --json")
        return json.loads(proc.stdout)

    def _start_embed_stub(self) -> None:
        """Write the stub embeddings server into the container and start it
        in the background; wait until /health answers."""
        b64 = base64.b64encode(EMBED_STUB_SOURCE.encode()).decode()
        self._run(f"echo {b64} | base64 -d > /tmp/vr-embed-stub.py")
        self._run(
            f"nohup python3 /tmp/vr-embed-stub.py "
            f"> /tmp/vr-embed-stub.log 2>&1 & echo $! > /tmp/vr-embed-stub.pid"
        )
        for _ in range(30):
            proc = self._run(
                "python3 - <<'PY'\n"
                "import json, urllib.request\n"
                "try:\n"
                "    with urllib.request.urlopen("
                f"'http://127.0.0.1:{EMBED_STUB_PORT}/health', timeout=2) as r:\n"
                "        print(r.status)\n"
                "except Exception as exc:\n"
                "    print('down', exc)\n"
                "PY",
                check=False,
            )
            if proc.stdout.strip().startswith("200"):
                return
            time.sleep(2)
        self.fail(
            "embedding stub did not become healthy:\n"
            f"{self.runtime.exec('hermes', *_hermes_script('cat /tmp/vr-embed-stub.log'), check=False).stdout[-2000:]}"
        )

    def _semantic_query(self, env: str, query: str) -> str:
        """Semantic search through the REAL vector path (no LLM expansion)."""
        return self._run(f"{env} {EMBED_ENV} {NATIVE} query {query!r} --no-expand").stdout

    def _assert_no_stale(self, proc: subprocess.CompletedProcess[str]) -> None:
        """Assert the embed --dry-run verification found ZERO stale rows.

        The output phrasing is "<...> 0 stale found" (the bare substring
        "stale" always appears in the sentence), so the zero-count is parsed
        instead of a substring check.
        """
        match = re.search(r"(\d+)\s+stale\s+found", proc.stdout.lower())
        self.assertIsNotNone(
            match,
            f"embed dry-run output lacks a stale count:\n{proc.stdout[-3000:]}",
        )
        self.assertEqual(
            match.group(1), "0",
            f"stale embeddings remain after backfill:\n{proc.stdout[-3000:]}",
        )

    def test_pinned_image_physical_copy_portability(self) -> None:
        # Local opt-in; the release/deploy workflow forces the proof with
        # VAULT_RECOVERY_PORTABILITY_REQUIRED=1, which makes a missing docker
        # CLI a FAILURE (no opt-in bypass).
        if not self.required:
            if os.getenv("RUN_DOCKER_TESTS") != "1":
                self.skipTest("set RUN_DOCKER_TESTS=1 to run Docker runtime tests")
        if not docker_available():
            if self.required:
                self.fail(
                    "VAULT_RECOVERY_PORTABILITY_REQUIRED=1 but the docker CLI is "
                    "unavailable: the vault-recovery portability proof cannot "
                    "run and the release is blocked."
                )
            self.skipTest("docker CLI is not available")

        # Build once, then start hermes (disposable project; no Telegram, no
        # state repo — ComposeRuntime blanks all production env).
        self.runtime.run("build", "hermes", timeout=1800)
        self.runtime.up("hermes", timeout=300)

        # 1. Fresh PGLite activation in the disposable volume.
        self._run(f"mkdir -p /opt/data/obsidian && {GBRAIN_ENV} {NATIVE} init --pglite --no-embedding")
        self._run(f"{GBRAIN_ENV} {NATIVE} config set sync.repo_path /opt/data/obsidian")
        self._run(f"{GBRAIN_ENV} {NATIVE} config set search.mcp_keyword_only true")

        # 2. Pages + a DB-only manual link; plant schema-pack files + the
        # active-schema-pack marker.
        self._run(f"{GBRAIN_ENV} {NATIVE} put pa --content '# Page A\n\nportability marker A'")
        self._run(f"{GBRAIN_ENV} {NATIVE} put pb --content '# Page B\n\nportability marker B'")
        self._run(
            f"{GBRAIN_ENV} {NATIVE} link pa pb --link-type mentions "
            '--context "portability proof" --link-source manual'
        )
        self._run(
            "mkdir -p /opt/data/.gbrain/schema-packs/josemar && "
            "printf 'schema: josemar-test\\n' > /opt/data/.gbrain/schema-packs/josemar/pack.yaml && "
            "printf 'josemar\\n' > /opt/data/.gbrain/active-schema-pack"
        )

        # 3. REAL vector state through the pinned gbrain embedding workflow
        # (issue #65): stub embeddings endpoint + the exact native sequence
        # that josemar-gbrain's enable-embeddings + embed-backfill run.
        self._start_embed_stub()
        self._run(f"{GBRAIN_ENV} {EMBED_ENV} {NATIVE} config set search.mcp_keyword_only true")
        migrate = self._run(
            f"{GBRAIN_ENV} {EMBED_ENV} {NATIVE} migrate embeddings "
            f"--to {EMBEDDING_MODEL} --dim {EMBEDDING_DIMENSIONS} --yes "
            "--no-embed --ignore-env-override",
            check=False,
        )
        self.assertEqual(
            migrate.returncode, 0,
            f"migrate embeddings failed\nstdout: {migrate.stdout[-3000:]}\n"
            f"stderr: {migrate.stderr[-3000:]}",
        )
        self._run(f"{GBRAIN_ENV} {EMBED_ENV} {NATIVE} config set search.mcp_keyword_only false")
        backfill = self._run(
            f"{GBRAIN_ENV} {EMBED_ENV} {NATIVE} embed --stale --include-null-signature",
            check=False,
        )
        self.assertEqual(
            backfill.returncode, 0,
            f"embed backfill failed\nstdout: {backfill.stdout[-3000:]}\n"
            f"stderr: {backfill.stderr[-3000:]}",
        )
        # Zero stale rows must remain (the backfill's own verification).
        verify = self._run(
            f"{GBRAIN_ENV} {EMBED_ENV} {NATIVE} embed --stale --include-null-signature --dry-run",
            check=False,
        )
        self.assertEqual(
            verify.returncode, 0,
            f"embed verify failed\nstdout: {verify.stdout[-3000:]}\n"
            f"stderr: {verify.stderr[-3000:]}",
        )
        self._assert_no_stale(verify)
        # The completion marker carries the real model tuple (same payload
        # shape josemar-gbrain writes).
        self._run(
            "python3 - <<'PY'\n"
            "import json\n"
            "payload = {'model': %r, 'dimensions': int(%r), 'revision': %r}\n"
            "with open('/opt/data/.gbrain/embedding-backfill-complete.json', 'w') as f:\n"
            "    json.dump(payload, f, sort_keys=True); f.write('\\n')\n"
            "PY" % (EMBEDDING_MODEL, EMBEDDING_DIMENSIONS, EMBEDDING_REVISION)
        )

        # 4. The live tree must answer a semantic query through its REAL
        # vectors: the page whose content shares words with the query wins.
        live_query = self._semantic_query(GBRAIN_ENV, "portability marker A")
        self.assertIn(
            "pa", live_query,
            f"live semantic query did not return pa (vectors unusable?):\n{live_query[-3000:]}",
        )

        # 5. Live doctor preflight validated by the production contract.
        live_report = self._doctor_json(GBRAIN_ENV)
        live_summary = self._validate_doctor(live_report)
        for check in ("connection", "jsonb_integrity", "schema_version", "pgvector"):
            self.assertEqual(live_summary["required_checks"][check], "ok")

        # 6. Production export through the wrapper (lock runner + core) as
        # the hermes user. A busy lock would exit 75; the export must run.
        # A slightly higher convergence bound absorbs the rare async PGLite
        # shutdown-checkpoint that can land mid-export in this tiny state.
        export = self._run(
            f"VAULT_RECOVERY_STAGING_DIR={STAGING} "
            "VAULT_RECOVERY_CONVERGENCE_ATTEMPTS=6 "
            "/opt/josemar/scripts/vault-recovery-export.sh",
            check=False,
        )
        self.assertNotEqual(
            export.returncode, 75,
            f"export skipped: lock busy\nstdout: {export.stdout[-2000:]}\nstderr: {export.stderr[-2000:]}",
        )
        self.assertEqual(
            export.returncode, 0,
            f"export failed ({export.returncode})\nstdout: {export.stdout[-2000:]}\nstderr: {export.stderr[-2000:]}",
        )

        # Staging layout: latest pointer, generation dir, READY, manifest.
        latest = self._run(f"cat {STAGING}/latest").stdout.strip()
        self.assertRegex(latest, r"^\d{8}T\d{12}Z-[0-9a-f]{8}$")
        manifest_text = self._run(f"cat {STAGING}/{latest}/manifest.json").stdout
        manifest = json.loads(manifest_text)
        self.assertEqual(manifest["generation_id"], latest)
        self.assertEqual(manifest["phase"], 1)
        self.assertFalse(manifest["remote"]["uploaded"])
        self._run(f"test -f {STAGING}/{latest}/READY")
        # Layout-agnostic tree assertions: .gbrain and vault must be present
        # and non-empty; byte-identity to the live sources is proven by the
        # production scan-digest comparison at the end.
        self._run(f'test -n "$(ls -A {STAGING}/{latest}/.gbrain)"')
        self._run(f'test -n "$(ls -A {STAGING}/{latest}/vault)"')

        # 7. Physical-copy restore simulation into a fresh root.
        self._run(
            f"rm -rf /tmp/vr-restore && mkdir -p /tmp/vr-restore && "
            f"cp -a {STAGING}/{latest}/.gbrain /tmp/vr-restore/.gbrain && "
            f"cp -a {STAGING}/{latest}/vault /tmp/vr-restore/obsidian"
        )
        restored_env = (
            "GBRAIN_HOME=/tmp/vr-restore GBRAIN_BRAIN_REPO=/tmp/vr-restore/obsidian "
            "GBRAIN_SCHEMA_PACK=josemar GBRAIN_SKIP_STARTUP_HOOKS=1 "
            "HOME=/tmp/vr-restore XDG_CONFIG_HOME=/tmp/vr-restore/.config"
        )

        # 8. The restored full .gbrain must open with the real doctor and
        # pass the SAME pinned contract.
        restored_report = self._doctor_json(restored_env)
        restored_summary = self._validate_doctor(restored_report)
        for check in ("connection", "jsonb_integrity", "schema_version", "pgvector"):
            self.assertEqual(restored_summary["required_checks"][check], "ok")

        # 8a. DB-only manual link survived: backlinks on pb must list pa.
        backlinks = self._run(f"{restored_env} {NATIVE} backlinks pb").stdout
        self.assertIn("pa", backlinks)

        # 8b. Page content survived.
        page = self._run(f"{restored_env} {NATIVE} get pa").stdout
        self.assertIn("portability marker A", page)

        # 8c. Config survived: embeddings are ACTIVE (mcp_keyword_only false),
        # not a no-embedding sentinel.
        keyword_only = self._run(
            f"{restored_env} {NATIVE} config get search.mcp_keyword_only"
        ).stdout.strip()
        self.assertEqual(keyword_only, "false")

        # 8d. Schema-pack files + the real embedding completion marker
        # survived byte-identical.
        for rel in (
            "active-schema-pack",
            "embedding-backfill-complete.json",
            "schema-packs/josemar/pack.yaml",
        ):
            self._run(f"cmp /opt/data/.gbrain/{rel} /tmp/vr-restore/.gbrain/{rel}")

        # 9. REAL restored vector proof: the semantic query still returns the
        # page through the copied vectors, and the backfill verification finds
        # ZERO stale rows on the restored tree — the vector rows + signatures
        # survived the physical copy with no reindex/rebuild/sync.
        restored_query = self._semantic_query(restored_env, "portability marker A")
        self.assertIn(
            "pa", restored_query,
            f"restored semantic query did not return pa (vectors lost?):\n{restored_query[-3000:]}",
        )
        restored_verify = self._run(
            f"{restored_env} {EMBED_ENV} {NATIVE} embed --stale --include-null-signature --dry-run",
            check=False,
        )
        self.assertEqual(
            restored_verify.returncode, 0,
            f"restored embed verify failed\nstdout: {restored_verify.stdout[-3000:]}\n"
            f"stderr: {restored_verify.stderr[-3000:]}",
        )
        self._assert_no_stale(restored_verify)

        # 10. The staged generation trees are byte-identical to the live
        # sources AS OF the converged scan. A post-hoc live re-scan cannot be
        # compared directly: PGLite background activity (checkpoints) may
        # rewrite live relation files at any moment, so the live tree is not
        # guaranteed to equal the staged tree at the END of the test even
        # when the export was byte-perfect (verified: staged == live at
        # publication, modes included). The deterministic proof uses the
        # exporter's own convergence record, which it enforced before
        # publishing:
        #   (a) manifest trees.<name>.scan_digest == staged_digest  — the
        #       exporter verified staged == live at publication time, and
        #   (b) re-scanning the IMMUTABLE staged trees now must reproduce
        #       the manifest's staged_digest exactly.
        digests = self._run(
            f"VR_LATEST={latest} VR_STAGING={STAGING} python3 - <<'PY'\n"
            "import importlib.util, json, os\n"
            "spec = importlib.util.spec_from_file_location("
            "'vault_recovery_core', '/opt/josemar/scripts/vault_recovery_core.py')\n"
            "mod = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(mod)\n"
            "latest = os.environ['VR_LATEST']\n"
            "staging = os.environ['VR_STAGING']\n"
            "with open(staging + '/' + latest + '/manifest.json') as f: m = json.load(f)\n"
            "out = {}\n"
            "for name in ('.gbrain', 'vault'):\n"
            "    t = m['trees'][name]\n"
            "    out[name] = {'scan_digest': t['scan_digest'],\n"
            "                 'staged_digest': t['staged_digest'],\n"
            "                 'rescan_staged': mod.scan_digest(\n"
            "                     mod.scan_tree(staging + '/' + latest + '/' + name))}\n"
            "print(json.dumps(out))\n"
            "PY"
        )
        tree_digests = json.loads(digests.stdout)
        for name in ("gbrain", "vault"):
            with self.subTest(tree=name):
                manifest_name = ".gbrain" if name == "gbrain" else name
                entry = tree_digests[manifest_name]
                # (a) the exporter converged: staged == live at publication.
                self.assertEqual(
                    entry["scan_digest"], entry["staged_digest"],
                    f"{name}: manifest scan_digest != staged_digest "
                    f"(the exporter must not publish a divergent tree)",
                )
                # (b) the immutable staged tree still matches the manifest.
                self.assertEqual(
                    entry["rescan_staged"], entry["staged_digest"],
                    f"{name}: staged tree no longer matches the manifest "
                    f"staged_digest (staged generations must be immutable)",
                )


class VaultRecoveryPortabilityGateTests(unittest.TestCase):
    """The REQUIRED docker gate is fail-closed (council fix: the mandatory
    portability test must not skip before its REQUIRED check). With
    VAULT_RECOVERY_PORTABILITY_REQUIRED=1 a missing docker CLI FAILS the
    test; the skip applies only when the proof is NOT required. These are
    pure-logic tests — no docker, no containers: the gated test method is
    invoked directly with a patched `docker_available`, so the gate
    ordering itself is what runs."""

    def _make_case(self, required: bool) -> "VaultRecoveryPortabilityTests":
        case = VaultRecoveryPortabilityTests(
            methodName="test_pinned_image_physical_copy_portability"
        )
        case.required = required
        return case

    def test_required_without_docker_fails_closed(self) -> None:
        """The release/deploy environment (REQUIRED=1, RUN_DOCKER_TESTS=1)
        must FAIL — not skip — when the docker CLI is unavailable."""
        case = self._make_case(required=True)
        with mock.patch.dict(
            os.environ, {"VAULT_RECOVERY_PORTABILITY_REQUIRED": "1", "RUN_DOCKER_TESTS": "1"}
        ):
            with mock.patch(
                f"{__name__}.docker_available",
                return_value=False,
            ):
                with self.assertRaises(AssertionError):
                    case.test_pinned_image_physical_copy_portability()

    def test_optional_without_docker_skips(self) -> None:
        """Without REQUIRED, a missing docker CLI is a skip (local
        opt-in), never a failure."""
        case = self._make_case(required=False)
        with mock.patch.dict(os.environ, {"RUN_DOCKER_TESTS": "1"}):
            with mock.patch(
                f"{__name__}.docker_available",
                return_value=False,
            ):
                with self.assertRaises(unittest.SkipTest):
                    case.test_pinned_image_physical_copy_portability()

    def test_optional_without_run_docker_env_skips_before_docker_check(self) -> None:
        """The local default (no RUN_DOCKER_TESTS, not required) skips
        BEFORE the docker availability check — even with docker present,
        the proof only runs on explicit opt-in."""
        case = self._make_case(required=False)
        with mock.patch.dict(
            os.environ,
            {},
            clear=True,
        ):
            with mock.patch(
                f"{__name__}.docker_available",
                return_value=True,
            ):
                with self.assertRaises(unittest.SkipTest):
                    case.test_pinned_image_physical_copy_portability()


if __name__ == "__main__":
    unittest.main()
