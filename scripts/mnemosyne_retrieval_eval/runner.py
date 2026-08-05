"""Runner for the Mnemosyne retrieval quality harness.

Host-side orchestration (stdlib only). The actual Mnemosyne/Beam ingest and
recall runs inside the built Hermes container via a generated Python script,
so the host needs neither the mnemosyne package nor a model download.

The runner:
  - validates the dataset (public or activation) via the schema module,
  - copies the dataset into a DISPOSABLE temporary input directory created by
    the host test (never mounting production state),
  - generates an in-container Python script that:
      * creates a fresh disposable BeamMemory store at a temp data dir,
      * ingests each corpus row via beam.remember(content_with_marker, ...),
        retaining the returned memory_id,
      * queries each labeled query via beam.recall(query, top_k=...),
      * emits a JSON results blob with ranked_ids, scores, latencies,
  - parses the JSON blob on the host and computes metrics + gate.

The in-container script uses the EXACT pinned API:
  - BeamMemory(session_id=..., db_path=Path(...)) for a disposable store,
  - beam.remember(content, source=..., scope="global") -> memory_id,
  - beam.recall(query, top_k=N) -> List[Dict] with id/score/keyword_score/
    dense_score/tier.

The harness does NOT add query/passage prefixes: the Mnemosyne embeddings
module applies them itself from the env vars, so double-prefixing is avoided.

Keyword and TEI modes run in SEPARATE one-off containers with SEPARATE
disposable BeamMemory data dirs, so MNEMOSYNE_NO_EMBEDDINGS (set only in the
keyword script) cannot leak into the TEI process.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Tuple

from .schema import (
    validate_dataset_dir,
    is_activation_dataset,
    REVIEW_READY,
    REVIEW_NOT_READY,
)
from .metrics import evaluate_query, evaluate_run
from .gate import (
    evaluate_gate,
    merge_thresholds,
    PUBLIC_SMOKE_THRESHOLDS,
    STANDARD_ACTIVATION_THRESHOLDS,
    ACTIVATION_THRESHOLDS,
    PUBLIC_SMOKE_THRESHOLD_KEYS,
    ACTIVATION_THRESHOLD_KEYS,
)
from .report import build_report, write_report, REPORT_DIR_NAME, CONTENT_MARKER_PREFIX

REPO_ROOT = Path(__file__).resolve().parents[2]

# Dedicated test-only Compose overlay that replaces the repository's real
# agent-state/credentials bind mounts with disposable empty dirs. Applied by
# EvalRuntime so the eval's one-off Hermes containers can never mount
# production state.
ISOLATION_OVERLAY = (
    REPO_ROOT / "tests" / "runtime" / "docker-compose.test-isolation.yml"
)

# Env vars that must NEVER inherit into the eval Docker runtimes. Any
# production-like value present in the caller environment (or the repo `.env`
# file, which compose reads when the key is absent from the shell env) is
# forcibly blanked so the disposable runtime stays fail-closed. Mirrors
# tests/runtime/helpers.py FORCED_EMPTY_ENV_KEYS without importing the test
# helper module.
FORCED_EMPTY_ENV_KEYS = (
    # Telegram / gateway identity.
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_ENABLED",
    "PRIMARY_TELEGRAM_ID",
    "TELEGRAM_ALLOWED_USERS",
    "TELEGRAM_HOME_CHANNEL",
    "GATEWAY_ALLOWED_USERS",
    "GATEWAY_AUTH_PASSWORD",
    "GATEWAY_AUTH_TOKEN",
    "HERMES_TELEGRAM_BOT_TOKEN",
    "HERMES_TELEGRAM_ALLOWED_USERS",
    "HERMES_TELEGRAM_HOME_CHANNEL",
    "HERMES_GATEWAY_ALLOWED_USERS",
    # Workspace state sync.
    "WORKSPACE_STATE_REPO",
    "WORKSPACE_REPO_TOKEN",
    "WORKSPACE_GIT_BRANCH",
    "WORKSPACE_GIT_USER_EMAIL",
    "WORKSPACE_GIT_USER_NAME",
    "WORKSPACE_MEMORY_DAYS",
    # Hosted provider credentials.
    "ZAI_API_KEY",
    "GLM_API_KEY",
    "DEEPSEEK_API_KEY",
    "OLLAMA_API_KEY",
    "TAVILY_API_KEY",
    "APOLLO_IO_API_KEY",
    "HERMES_MODEL",
    # Tailscale / keyring secrets and control-plane credentials.
    "TS_AUTHKEY",
    "GOG_KEYRING_PASSWORD",
    "HERMES_API_SERVER_KEY",
    "CONTROL_UI_ALLOWED_ORIGIN_1",
    "CONTROL_UI_ALLOWED_ORIGIN_2",
    "FORCE_OVERWRITE_SKILLS",
    # Hermes dashboard credentials (session token + basic auth). Never
    # inherited from host/.env. EvalRuntime replaces them with deterministic
    # test-only values (the base compose declares them with `:?` interpolation,
    # so they must be non-empty for compose to render).
    "HERMES_DASHBOARD_SESSION_TOKEN",
    "HERMES_DASHBOARD_BASIC_AUTH_USERNAME",
    "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD",
    "HERMES_DASHBOARD_BASIC_AUTH_SECRET",
    # Remote backup targets.
    "OBSIDIAN_GDRIVE_REMOTE",
    "OBSIDIAN_GDRIVE_PATH",
    "MNEMOSYNE_BACKUP_RCLONE_REMOTE",
    "MNEMOSYNE_BACKUP_RCLONE_PATH",
    # Mnemosyne activation/runtime switches.
    "MNEMOSYNE_PROVIDER",
    "MNEMOSYNE_DATA_DIR",
    "MNEMOSYNE_HOME",
    "MNEMOSYNE_NO_EMBEDDINGS",
    "MNEMOSYNE_EMBEDDINGS_VIA_API",
    "MNEMOSYNE_EMBEDDING_MODEL",
    "MNEMOSYNE_EMBEDDING_DIM",
    "MNEMOSYNE_EMBEDDING_API_URL",
)

# Pinned E5-small tuple (matches docker-compose.embeddings.yml defaults and
# .env.example). The TEI mode uses these via the embeddings overlay env vars.
E5_MODEL_ID = "intfloat/multilingual-e5-small"
E5_MODEL_REVISION = "614241f622f53c4eeff9890bdc4f31cfecc418b3"
E5_MODEL_DIMENSIONS = "384"
E5_QUERY_PREFIX = "query: "
E5_PASSAGE_PREFIX = "passage: "
E5_API_URL = "http://embeddings:80/v1"


def make_disposable_input(dataset_dir: Path) -> Tuple[Path, Path]:
    """Copy a dataset into a fresh disposable temp input directory.

    Returns (temp_input_dir, manifest_path). The caller MUST remove
    temp_input_dir when done. This never mounts production state; it is a
    plain host copy into a tempdir that the container reads via a bind mount
    created by the test's ComposeRuntime.
    """
    dataset_dir = Path(dataset_dir).resolve()
    tmp = Path(tempfile.mkdtemp(prefix="mnemosyne-eval-input-"))
    for name in ("manifest.json", "corpus.jsonl", "queries.jsonl", "review.json"):
        src = dataset_dir / name
        if src.is_file():
            shutil.copy2(src, tmp / name)
    return tmp, tmp / "manifest.json"


def generate_incontainer_script(
    *,
    input_dir_in_container: str,
    output_json_path_in_container: str,
    mode: str,
    top_k: int = 10,
) -> str:
    """Generate the Python script that runs inside the Hermes container.

    ``mode`` is "keyword" or "tei". For "keyword" the script sets
    MNEMOSYNE_NO_EMBEDDINGS=true at the top of THIS process only. For "tei"
    it relies on the env vars already wired by docker-compose.embeddings.yml.
    Each mode runs in its own one-off container, so the keyword env var
    cannot leak into the TEI process.
    """
    template = '''\
import json, os, sys, time, tempfile, shutil
from pathlib import Path

INPUT_DIR = Path(__INPUT_DIR__)
OUTPUT = Path(__OUTPUT__)
MODE = __MODE__
TOP_K = __TOP_K__
MARKER = __MARKER__

if MODE == "keyword":
    os.environ["MNEMOSYNE_NO_EMBEDDINGS"] = "true"

# Load dataset from the disposable input dir.
corpus = []
with (INPUT_DIR / "corpus.jsonl").open(encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if line:
            corpus.append(json.loads(line))
queries = []
with (INPUT_DIR / "queries.jsonl").open(encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if line:
            queries.append(json.loads(line))

# Fresh disposable data dir inside the container temp.
data_dir = Path(tempfile.mkdtemp(prefix="mnemosyne-eval-data-"))
db_path = data_dir / "mnemosyne.db"

def _embeddings_available():
    try:
        from mnemosyne.core import embeddings as _e
        return bool(_e.available())
    except Exception:
        return False

try:
    from mnemosyne.core.beam import BeamMemory
    beam = BeamMemory(session_id="eval-session", db_path=db_path)

    # Ingest corpus. Embed a stable marker in content and retain returned IDs.
    id_map = {}  # corpus_id -> memory_id
    for row in corpus:
        content = row["content"] + " " + MARKER + " " + row["id"]
        mid = beam.remember(content, source=row.get("source", "eval"),
                            scope=row.get("scope", "global"))
        id_map[row["id"]] = mid

    # Query each labeled query via the exact pinned API.
    results = []
    for q in queries:
        t0 = time.perf_counter()
        ranked = beam.recall(q["query"], top_k=TOP_K)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        ranked_ids = []
        signal_scores = []
        for r in ranked:
            # Map memory_id back to corpus_id via the marker suffix.
            mid = r.get("id")
            content = r.get("content", "") or ""
            cid = None
            # The marker + corpus id is at the end of the stored content.
            if MARKER in content:
                tail = content.rsplit(MARKER, 1)[-1].strip()
                # tail is "<corpus_id>..." possibly truncated; match by prefix.
                for known_cid in id_map:
                    if tail.startswith(known_cid) or known_cid in tail:
                        cid = known_cid
                        break
            # Fallback: direct memory_id match.
            if cid is None:
                for known_cid, known_mid in id_map.items():
                    if known_mid == mid:
                        cid = known_cid
                        break
            ranked_ids.append(cid if cid is not None else mid)
            signal_scores.append({
                "score": r.get("score"),
                "keyword_score": r.get("keyword_score"),
                "dense_score": r.get("dense_score"),
                "tier": r.get("tier"),
            })
        results.append({
            "query_id": q["id"],
            "difficulty": q["difficulty"],
            "expected_ids": q["expected_ids"],
            "ranked_ids": ranked_ids,
            "signal_scores": signal_scores,
            "latency_ms": elapsed_ms,
        })

    out = {
        "mode": MODE,
        "marker": MARKER,
        "top_k": TOP_K,
        "results": results,
        "embeddings_available": _embeddings_available(),
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    print("EVAL_SCRIPT_OK")
finally:
    shutil.rmtree(data_dir, ignore_errors=True)
'''
    return (
        template
        .replace("__INPUT_DIR__", repr(input_dir_in_container))
        .replace("__OUTPUT__", repr(output_json_path_in_container))
        .replace("__MODE__", repr(mode))
        .replace("__TOP_K__", repr(top_k))
        .replace("__MARKER__", repr(CONTENT_MARKER_PREFIX))
    )


# ---------------------------------------------------------------------------
# Reusable host-side runtime helper.
# ---------------------------------------------------------------------------


class EvalRuntime:
    """Reusable Docker Compose runtime for one eval mode run.

    Wraps ComposeRuntime-style orchestration without depending on the test
    helper module, so the runner can be used outside the test suite. Each
    EvalRuntime owns a unique Compose project and cleans up with
    ``down -v --remove-orphans``.

    Fail-closed isolation: every inherited production-influencing env var
    (Telegram, workspace state-sync, hosted provider keys, tailscale, backup
    remotes, Mnemosyne activation switches) is forcibly blanked, the container
    prefix is always the unique test project, and the dedicated test-isolation
    overlay replaces the repository's real agent-state/credentials bind mounts
    with disposable empty dirs.

    Keyword mode uses ``docker-compose.yml`` + the isolation overlay. TEI mode
    also uses ``docker-compose.embeddings.yml`` and starts the embeddings
    service, waiting for it to become healthy before running the eval script.
    The two modes use separate one-off containers and separate disposable
    BeamMemory data dirs, so MNEMOSYNE_NO_EMBEDDINGS (set only in the keyword
    in-container script) cannot leak into the TEI process.
    """

    def __init__(self, *, mode: str, project: str, env: Dict[str, str] | None = None) -> None:
        if mode not in ("keyword", "tei"):
            raise ValueError(f"mode must be 'keyword' or 'tei', got {mode!r}")
        self.mode = mode
        self.project = project
        if not str(project).strip() or str(project).strip() == "josemar":
            raise AssertionError("Eval runtime must use a unique test container prefix")
        self.env = dict(env or os.environ)
        # Fail-closed: blank every inherited production-influencing value BEFORE
        # the eval values below are applied. Compose gives the shell env
        # precedence over the repo `.env` file, so empty values here defeat a
        # production `.env` too.
        for key in FORCED_EMPTY_ENV_KEYS:
            self.env[key] = ""
        # Compose selection itself must never be inherited either.
        self.env.pop("COMPOSE_FILE", None)
        self.env.pop("COMPOSE_PATH_SEPARATOR", None)
        self.env.pop("COMPOSE_PROJECT_NAME", None)
        self.env.pop("COMPOSE_PROFILES", None)
        self.env["COMPOSE_PROJECT_NAME"] = project
        # Unique container prefix to avoid colliding with production. Always
        # forced to this project, never inherited.
        self.env["JOSEMAR_CONTAINER_PREFIX"] = project
        # Disable Telegram/state-sync so no production side effects occur.
        self.env["WORKSPACE_SYNC_ON_START"] = "false"
        self.env["WORKSPACE_SYNC_INTERVAL"] = "0"
        # Hermes dashboard credentials: forced to deterministic test-only values
        # (never inherited from host/.env). The base compose declares these with
        # `:?` interpolation, so they must be non-empty; test values keep
        # `docker compose` rendering while guaranteeing production dashboard
        # credentials can never reach the disposable containers.
        _dash_token = str(project).rsplit("-", 1)[-1]
        self.env["HERMES_DASHBOARD_SESSION_TOKEN"] = f"test-session-{_dash_token}"
        self.env["HERMES_DASHBOARD_BASIC_AUTH_USERNAME"] = "test-admin"
        self.env["HERMES_DASHBOARD_BASIC_AUTH_PASSWORD"] = f"test-password-{_dash_token}"
        self.env["HERMES_DASHBOARD_BASIC_AUTH_SECRET"] = f"test-secret-{_dash_token}"
        self._state_dir: Path | None = None
        self._credentials_dir: Path | None = None

        if mode == "tei":
            self.compose_files = ["docker-compose.yml", str(ISOLATION_OVERLAY), "docker-compose.embeddings.yml"]
            # Force the pinned E5-small tuple (matches .env.example defaults).
            # Never inherited: a production `.env`/shell EMBEDDING_* value must
            # not silently change the eval's model tuple.
            self.env["EMBEDDING_MODEL_ID"] = E5_MODEL_ID
            self.env["EMBEDDING_MODEL_REVISION"] = E5_MODEL_REVISION
            self.env["EMBEDDING_MODEL_DIMENSIONS"] = E5_MODEL_DIMENSIONS
            self.env["EMBEDDING_QUERY_PREFIX"] = E5_QUERY_PREFIX
            self.env["EMBEDDING_PASSAGE_PREFIX"] = E5_PASSAGE_PREFIX
            self.env["EMBEDDING_API_URL"] = E5_API_URL
        else:
            self.compose_files = ["docker-compose.yml", str(ISOLATION_OVERLAY)]

        base = ["docker", "compose"]
        for f in self.compose_files:
            base += ["-f", f]
        base += ["-p", self.project]
        self._base_cmd = base

    def _ensure_disposable_mounts(self) -> None:
        """Create (once) disposable EMPTY dirs that replace the repository's
        real agent-state/credentials bind mounts, and expose them to compose."""
        if self._state_dir is None:
            self._state_dir = Path(tempfile.mkdtemp(prefix=f"{self.project}-state-"))
            self._credentials_dir = Path(tempfile.mkdtemp(prefix=f"{self.project}-creds-"))
        self.env["JOSEMAR_TEST_STATE_DIR"] = str(self._state_dir)
        self.env["JOSEMAR_TEST_CREDENTIALS_DIR"] = str(self._credentials_dir)

    def disposable_mounts(self) -> Tuple[Path, Path]:
        """Return the (state, credentials) disposable mount dirs, creating them
        on first use. Both are empty and never point at repository state."""
        self._ensure_disposable_mounts()
        assert self._state_dir is not None and self._credentials_dir is not None
        return self._state_dir, self._credentials_dir

    def _run(self, *args: str, timeout: int = 600, check: bool = True) -> subprocess.CompletedProcess[str]:
        self._ensure_disposable_mounts()
        return subprocess.run(
            self._base_cmd + list(args),
            cwd=REPO_ROOT, env=self.env,
            capture_output=True, text=True, check=check, timeout=timeout,
        )

    def build(self, *services: str, timeout: int = 1800) -> subprocess.CompletedProcess[str]:
        return self._run("build", *services, timeout=timeout)

    def up(self, *services: str, timeout: int = 600) -> subprocess.CompletedProcess[str]:
        return self._run("up", "-d", *services, timeout=timeout)

    def wait_healthy(self, service: str, timeout_s: int = 360) -> bool:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            ps = self._run("ps", service, check=False, timeout=60)
            if "(healthy)" in ps.stdout:
                return True
            time.sleep(10)
        return False

    def run_script(self, dataset_tmp_dir: Path, input_in_container: str, script: str, timeout: int = 900) -> subprocess.CompletedProcess[str]:
        """Write the script into the bind-mounted temp dir and run it in a one-off container."""
        self._ensure_disposable_mounts()
        (dataset_tmp_dir / "run_eval.py").write_text(script, encoding="utf-8")
        cmd = self._base_cmd + [
            "run", "--rm", "--no-deps", "--entrypoint", "sh",
            "-v", f"{dataset_tmp_dir}:{input_in_container}",
            "hermes", "-lc",
            f"/opt/hermes/.venv/bin/python3 {input_in_container}/run_eval.py",
        ]
        return subprocess.run(
            cmd, cwd=REPO_ROOT, env=self.env,
            capture_output=True, text=True, check=False, timeout=timeout,
        )

    def down(self) -> subprocess.CompletedProcess[str]:
        result = self._run("down", "-v", "--remove-orphans", check=False, timeout=180)
        self._cleanup_disposable_mounts()
        return result

    def _cleanup_disposable_mounts(self) -> None:
        for path in (self._state_dir, self._credentials_dir):
            if path is not None:
                shutil.rmtree(path, ignore_errors=True)
        self._state_dir = None
        self._credentials_dir = None


def run_eval_mode(
    *,
    mode: str,
    dataset_dir: Path,
    project: str,
    top_k: int = 10,
    env: Dict[str, str] | None = None,
    health_timeout_s: int = 360,
    run_timeout_s: int = 900,
) -> Dict:
    """Run one eval mode (keyword or TEI) end-to-end and return the results JSON.

    This is the reusable host-side helper used by both the public smoke and
    the activation eval. It:
      - creates a disposable input copy of the dataset,
      - builds the needed images,
      - (TEI only) starts the embeddings service and waits for health,
      - runs the in-container eval script in a one-off container,
      - reads the results JSON back from the bind-mounted temp dir,
      - cleans up with down -v --remove-orphans.

    Raises AssertionError on build/run failure or missing results. Returns
    the parsed results JSON dict (mode, marker, top_k, results,
    embeddings_available).
    """
    runtime = EvalRuntime(mode=mode, project=project, env=env)
    tmp_input = None
    try:
        # Build the needed images.
        if mode == "tei":
            build = runtime.build("hermes", "embeddings", timeout=1800)
        else:
            build = runtime.build("hermes", timeout=1200)
        assert build.returncode == 0, f"build failed:\n{build.stderr}"

        if mode == "tei":
            up = runtime.up("embeddings", timeout=600)
            assert up.returncode == 0, f"embeddings up failed:\n{up.stderr}"
            healthy = runtime.wait_healthy("embeddings", timeout_s=health_timeout_s)
            if not healthy:
                logs = runtime._run("logs", "embeddings", check=False, timeout=60)
                raise RuntimeError(
                    "TEI embeddings service did not become healthy within "
                    f"{health_timeout_s}s. Environmental blocker: ensure the "
                    "embeddings overlay can download the E5-small model "
                    "(network egress to huggingface.co) and that the service "
                    "starts. Logs:\n" + logs.stdout + logs.stderr
                )

        # Disposable input copy.
        tmp_input, _ = make_disposable_input(dataset_dir)
        input_in_container = "/tmp/eval-input"
        output_host = tmp_input / "results.json"
        output_in_container = f"{input_in_container}/results.json"

        script = generate_incontainer_script(
            input_dir_in_container=input_in_container,
            output_json_path_in_container=output_in_container,
            mode=mode,
            top_k=top_k,
        )
        proc = runtime.run_script(tmp_input, input_in_container, script, timeout=run_timeout_s)
        assert proc.returncode == 0, (
            f"{mode} eval script failed:\n{proc.stdout}\n{proc.stderr}"
        )
        assert "EVAL_SCRIPT_OK" in proc.stdout, (
            f"{mode} eval script did not print EVAL_SCRIPT_OK:\n{proc.stdout}\n{proc.stderr}"
        )
        assert output_host.is_file(), f"results.json not written to {output_host}"
        results = json.loads(output_host.read_text(encoding="utf-8"))
        assert results["mode"] == mode, f"results mode mismatch: {results['mode']!r} != {mode!r}"
        return results
    finally:
        if tmp_input is not None:
            shutil.rmtree(tmp_input, ignore_errors=True)
        runtime.down()


# ---------------------------------------------------------------------------
# Results parsing, metrics, gate, report.
# ---------------------------------------------------------------------------


def _per_query_from_results(results_json: Dict) -> Tuple[List[Dict], List[float]]:
    per_query: List[Dict] = []
    latencies: List[float] = []
    for row in results_json["results"]:
        metrics = evaluate_query(row["expected_ids"], row["ranked_ids"])
        sig = row.get("signal_scores", [{}])[0] if row.get("signal_scores") else {}
        per_query.append({
            "query_id": row["query_id"],
            "difficulty": row["difficulty"],
            "expected_ids": row["expected_ids"],
            "ranked_ids": row["ranked_ids"],
            "latency_ms": row["latency_ms"],
            "score": sig.get("score"),
            "keyword_score": sig.get("keyword_score"),
            "dense_score": sig.get("dense_score"),
            "tier": sig.get("tier"),
            **metrics,
        })
        latencies.append(row["latency_ms"])
    return per_query, latencies


def _aggregate_from_results(results_json: Dict) -> Dict:
    per_query, latencies = _per_query_from_results(results_json)
    return evaluate_run(per_query, latencies)


def _dense_signal_evidence(results_json: Dict) -> float:
    """Return the strongest finite dense score observed in TEI results."""
    scores = []
    for row in results_json.get("results", []):
        for signal in row.get("signal_scores", []):
            value = signal.get("dense_score")
            if isinstance(value, (int, float)) and value == value:
                scores.append(float(value))
    return max(scores, default=0.0)


def parse_results_and_evaluate(
    *,
    results_json: Dict,
    dataset_manifest: Dict,
    dataset_kind: str,
    dataset_count: int,
    review_ready: bool,
    is_activation: bool,
    is_smoke: bool,
    keyword_results_json: Dict | None = None,
    thresholds: Dict | None = None,
) -> Tuple[Dict, Dict, Dict]:
    """Parse the in-container results JSON and compute metrics + gate.

    Returns (report, aggregate, gate_result). If ``thresholds`` is None, the
    appropriate default thresholds are used (public smoke defaults for smoke,
    activation defaults for activation). When the manifest carries a
    ``thresholds`` object, the caller should merge it over the defaults and
    pass the merged dict here so the report records the exact thresholds used.
    """
    per_query, latencies = _per_query_from_results(results_json)
    aggregate = evaluate_run(per_query, latencies)

    if thresholds is None:
        thresholds = PUBLIC_SMOKE_THRESHOLDS if is_smoke else ACTIVATION_THRESHOLDS

    keyword_aggregate = None
    if keyword_results_json is not None:
        keyword_aggregate = _aggregate_from_results(keyword_results_json)

    gate = evaluate_gate(
        aggregate=aggregate,
        dataset_count=dataset_count,
        review_ready=review_ready,
        thresholds=thresholds,
        is_activation=is_activation,
        is_smoke=is_smoke,
        keyword_aggregate=keyword_aggregate,
        embeddings_available=bool(results_json.get("embeddings_available", False)),
        dense_signal=_dense_signal_evidence(results_json),
        review_status=dataset_manifest.get("review_status"),
    )

    report = build_report(
        mode=results_json.get("mode", "unknown"),
        dataset_manifest=dataset_manifest,
        dataset_kind=dataset_kind,
        dataset_count=dataset_count,
        review_ready=review_ready,
        per_query=per_query,
        aggregate=aggregate,
        thresholds=thresholds,
        gate_result=gate,
        is_activation=is_activation,
    )
    return report, aggregate, gate


def build_comparison_report(
    *,
    keyword_results: Dict,
    tei_results: Dict,
    dataset_manifest: Dict,
    dataset_kind: str,
    dataset_count: int,
    review_ready: bool,
    is_activation: bool,
    thresholds: Dict,
    review_status: str | None = None,
    precondition_failures: List[str] | None = None,
    dataset_dir: Path | None = None,
) -> Dict:
    """Build a combined keyword-vs-TEI comparison report for a activation eval.

    The report carries both modes' aggregates, per-query details, the
    TEI-vs-keyword regression evidence, and the activation gate evaluated
    against the TEI aggregate with the keyword aggregate as the regression
    baseline. No raw activation query/corpus text is included.

    Standard activation is fail-closed: identity is derived here by a full
    independent validation of ``dataset_dir``. There is no caller-supplied
    identity dict; a forged or tampered standard manifest is rejected.
    """
    standard_identity = None
    if dataset_kind == "public-standard-activation":
        from .gate import STANDARD_ACTIVATION_THRESHOLDS, STANDARD_POLICY_DIGEST
        from .schema import validated_standard_dataset_identity
        if dataset_dir is None:
            raise ValueError("standard activation requires dataset_dir for authoritative validation")
        standard_identity = validated_standard_dataset_identity(Path(dataset_dir))
        if thresholds != STANDARD_ACTIVATION_THRESHOLDS or standard_identity["threshold_policy_digest"] != STANDARD_POLICY_DIGEST:
            raise ValueError("standard activation requires the code-pinned threshold policy")
        thresholds = STANDARD_ACTIVATION_THRESHOLDS
    kw_pq, kw_lat = _per_query_from_results(keyword_results)
    tei_pq, tei_lat = _per_query_from_results(tei_results)
    kw_agg = evaluate_run(kw_pq, kw_lat)
    tei_agg = evaluate_run(tei_pq, tei_lat)

    gate = evaluate_gate(
        aggregate=tei_agg,
        dataset_count=dataset_count,
        review_ready=review_ready,
        thresholds=thresholds,
        is_activation=is_activation,
        is_smoke=False,
        keyword_aggregate=kw_agg,
        embeddings_available=bool(tei_results.get("embeddings_available", False)),
        dense_signal=_dense_signal_evidence(tei_results),
        review_status=review_status,
        precondition_failures=precondition_failures,
        standard_dataset_dir=dataset_dir if dataset_kind == "public-standard-activation" else None,
    )

    # TEI-vs-keyword regression evidence (overall + per-difficulty).
    regression = _regression_evidence(kw_agg, tei_agg, thresholds)

    report = {
        "schema_version": 1,
        "kind": "activation-comparison",
        "dataset_kind": dataset_kind,
        "dataset_count": dataset_count,
        "review_ready": review_ready,
        "review_status": review_status,
        "dataset_identity": standard_identity,
        "is_activation": is_activation,
        "thresholds": thresholds,
        "gate": gate,
        "keyword": {
            "mode": "keyword",
            "embeddings_available": keyword_results.get("embeddings_available", False),
            "aggregate": kw_agg,
            "per_query": _redacted_per_query(kw_pq),
        },
        "tei": {
            "mode": "tei",
            "embeddings_available": tei_results.get("embeddings_available", False),
            "aggregate": tei_agg,
            "per_query": _redacted_per_query(tei_pq),
            "dense_signal_max": _dense_signal_evidence(tei_results),
        },
        "regression": regression,
    }
    return report


def _redacted_per_query(per_query: List[Dict]) -> List[Dict]:
    """Strip raw text fields from per_query rows for report emission."""
    out = []
    for row in per_query:
        entry = {
            "query_id": row["query_id"],
            "difficulty": row["difficulty"],
            "expected_ids": list(row.get("expected_ids", [])),
            "ranked_ids": list(row.get("ranked_ids", [])),
            "metrics": {
                "recall@1": row["recall@1"],
                "recall@3": row["recall@3"],
                "recall@5": row["recall@5"],
                "mrr": row["mrr"],
                "ndcg@5": row["ndcg@5"],
                "ndcg@10": row["ndcg@10"],
            },
            "latency_ms": row.get("latency_ms", 0.0),
        }
        for sig in ("score", "keyword_score", "dense_score", "tier"):
            if sig in row:
                entry[sig] = row[sig]
        out.append(entry)
    return out


def _regression_evidence(kw_agg: Dict, tei_agg: Dict, thresholds: Dict) -> Dict:
    """Compute TEI-vs-keyword Recall@3 regression evidence (overall + slices)."""
    max_reg = thresholds.get("max_regression_vs_keyword_recall_at_3", 0.0)
    kw_r3 = kw_agg.get("overall", {}).get("recall@3", 0.0)
    tei_r3 = tei_agg.get("overall", {}).get("recall@3", 0.0)
    delta = tei_r3 - kw_r3
    overall = {
        "keyword_recall_at_3": kw_r3,
        "tei_recall_at_3": tei_r3,
        "delta_tei_minus_keyword": delta,
        "allowed_regression_below_keyword": max_reg,
        "regresses": delta < -max_reg,
    }
    slices = {}
    kw_slices = kw_agg.get("difficulty_slices", {})
    tei_slices = tei_agg.get("difficulty_slices", {})
    for diff in ("easy", "medium", "hard"):
        k = kw_slices.get(diff, {}).get("recall@3", 0.0)
        t = tei_slices.get(diff, {}).get("recall@3", 0.0)
        slices[diff] = {
            "keyword_recall_at_3": k,
            "tei_recall_at_3": t,
            "delta_tei_minus_keyword": t - k,
        }
    return {"overall": overall, "slices": slices}


def render_comparison_markdown(report: Dict) -> str:
    """Render a concise Markdown summary of the activation comparison report."""
    lines: List[str] = []
    lines.append("# Mnemosyne Retrieval Quality — Activation Comparison Report")
    lines.append("")
    lines.append(f"- Dataset kind: `{report['dataset_kind']}`")
    lines.append(f"- Dataset count: {report['dataset_count']}")
    lines.append(f"- Activation dataset: `{report['is_activation']}`")
    lines.append(f"- Review ready: `{report['review_ready']}`")
    lines.append(f"- Gate: **{report['gate'].get('status', 'UNKNOWN')}**")
    lines.append("")
    lines.append("## Overall Metrics (keyword vs TEI)")
    lines.append("")
    lines.append("| Mode | Recall@1 | Recall@3 | Recall@5 | MRR | nDCG@5 |")
    lines.append("|---|---|---|---|---|---|")
    for mode_key in ("keyword", "tei"):
        agg = report[mode_key]["aggregate"]["overall"]
        lines.append(
            f"| {mode_key} | {agg['recall@1']:.4f} | {agg['recall@3']:.4f} | "
            f"{agg['recall@5']:.4f} | {agg['mrr']:.4f} | {agg['ndcg@5']:.4f} |"
        )
    lines.append("")
    lines.append("## Embeddings Availability")
    lines.append("")
    lines.append(f"- keyword `embeddings_available`: `{report['keyword']['embeddings_available']}`")
    lines.append(f"- TEI `embeddings_available`: `{report['tei']['embeddings_available']}`")
    lines.append("")
    reg = report.get("regression", {})
    if reg:
        ro = reg.get("overall", {})
        lines.append("## TEI vs Keyword Regression (Recall@3)")
        lines.append("")
        lines.append("| Metric | Value |")
        lines.append("|---|---|")
        lines.append(f"| keyword recall@3 | {ro.get('keyword_recall_at_3', 0):.4f} |")
        lines.append(f"| TEI recall@3 | {ro.get('tei_recall_at_3', 0):.4f} |")
        lines.append(f"| delta (TEI - keyword) | {ro.get('delta_tei_minus_keyword', 0):.4f} |")
        lines.append(f"| allowed regression below keyword | {ro.get('allowed_regression_below_keyword', 0):.4f} |")
        lines.append(f"| regresses | `{ro.get('regresses', False)}` |")
        lines.append("")
    lines.append("## Difficulty Slices (keyword vs TEI)")
    lines.append("")
    lines.append("| Slice | Mode | Count | Recall@1 | Recall@3 | Recall@5 | MRR | nDCG@5 |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for mode_key in ("keyword", "tei"):
        for diff, m in sorted(report[mode_key]["aggregate"].get("difficulty_slices", {}).items()):
            lines.append(
                f"| {diff} | {mode_key} | {m['count']} | {m['recall@1']:.4f} | {m['recall@3']:.4f} | "
                f"{m['recall@5']:.4f} | {m['mrr']:.4f} | {m['ndcg@5']:.4f} |"
            )
    lines.append("")
    lines.append("## Latency (ms)")
    lines.append("")
    lines.append("| Mode | p50 | p90 | p95 | p99 | max | mean |")
    lines.append("|---|---|---|---|---|---|---|")
    for mode_key in ("keyword", "tei"):
        lat = report[mode_key]["aggregate"].get("latency_ms", {})
        lines.append(
            f"| {mode_key} | {lat.get('p50',0):.1f} | {lat.get('p90',0):.1f} | "
            f"{lat.get('p95',0):.1f} | {lat.get('p99',0):.1f} | "
            f"{lat.get('max',0):.1f} | {lat.get('mean',0):.1f} |"
        )
    lines.append("")
    lines.append("## Thresholds")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(report["thresholds"], indent=2, ensure_ascii=False))
    lines.append("```")
    lines.append("")
    lines.append("## Gate")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(report["gate"], indent=2, ensure_ascii=False))
    lines.append("```")
    lines.append("")
    lines.append(
        "_Raw activation text is redacted from this report. Per-query rows carry "
        "IDs, ranks, scores, and metrics only._"
    )
    return "\n".join(lines) + "\n"


def write_comparison_report(report: Dict, out_dir: Path) -> Dict[str, Path]:
    """Write the comparison report.json and report.md under out_dir."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "report.json"
    md_path = out_dir / "report.md"
    json_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    md_path.write_text(render_comparison_markdown(report), encoding="utf-8")
    return {"json": json_path, "markdown": md_path}


def get_report_dir(base: Path | None = None) -> Path:
    """Return the gitignored report output directory under dump_folder/."""
    base = Path(base) if base else REPO_ROOT / "dump_folder"
    return base / REPORT_DIR_NAME


def get_thresholds_for_dataset(manifest: Dict, *, is_smoke: bool) -> Dict:
    """Merge manifest thresholds over the appropriate defaults.

    For public smoke, defaults are PUBLIC_SMOKE_THRESHOLDS and the manifest
    may override the smoke keys. For activation eval, defaults are
    ACTIVATION_THRESHOLDS and the manifest ``thresholds`` object is
    the authoritative source (merged over defaults). The returned dict is
    what the gate and report use, so the activation report records the exact
    thresholds used.
    """
    if is_smoke:
        defaults = PUBLIC_SMOKE_THRESHOLDS
    elif manifest.get("dataset_kind") == "public-standard-activation":
        from .gate import STANDARD_ACTIVATION_THRESHOLDS, STANDARD_POLICY_DIGEST, STANDARD_POLICY_VERSION
        if manifest.get("threshold_policy_version") != STANDARD_POLICY_VERSION or manifest.get("threshold_policy_digest") != STANDARD_POLICY_DIGEST:
            raise ValueError("standard activation manifest threshold policy does not match the code-pinned policy")
        if manifest.get("thresholds") != STANDARD_ACTIVATION_THRESHOLDS:
            raise ValueError("standard activation manifest cannot override code-pinned thresholds")
        return dict(STANDARD_ACTIVATION_THRESHOLDS)
    else:
        defaults = ACTIVATION_THRESHOLDS
    overrides = manifest.get("thresholds") if isinstance(manifest, dict) else None
    return merge_thresholds(defaults, overrides)
