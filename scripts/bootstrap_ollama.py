from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


DEFAULT_BASE_URL = "http://127.0.0.1:11434/v1"
MODEL_ENV_KEYS = (
    "OLLAMA_MODEL",
    "OLLAMA_MODEL_INTENT_ROUTER",
    "OLLAMA_MODEL_TASK_PLANNER",
    "OLLAMA_MODEL_CODE_GENERATOR",
    "OLLAMA_MODEL_CODE_REFINER",
    "OLLAMA_MODEL_VALIDATION_FIXER",
    "OLLAMA_MODEL_VERIFICATION_FIXER",
    "OLLAMA_MODEL_REQUIREMENTS_VERIFIER",
    "OLLAMA_MODEL_SOLUTION_EXPLAINER",
    "OLLAMA_MODEL_QUESTION_ANSWERER",
)


def _normalize_root_url() -> str:
    base_url = str(os.getenv("OLLAMA_BASE_URL", DEFAULT_BASE_URL) or "").strip() or DEFAULT_BASE_URL
    parsed = urllib.parse.urlparse(base_url)
    path = parsed.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[:-3]
    normalized = parsed._replace(path=path, params="", query="", fragment="")
    return urllib.parse.urlunparse(normalized).rstrip("/")


def _read_json(url: str, method: str = "GET", payload: dict | None = None, timeout: float = 60.0) -> dict:
    body = None
    headers = {}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        content = response.read().decode("utf-8")
    return json.loads(content) if content.strip() else {}


def _wait_for_ollama(root_url: str, attempts: int = 90, delay_seconds: float = 2.0) -> None:
    tags_url = f"{root_url}/api/tags"
    for attempt in range(1, attempts + 1):
        try:
            _read_json(tags_url, timeout=5.0)
            print(f"[bootstrap] Ollama is ready after {attempt} attempt(s).", flush=True)
            return
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            if attempt == attempts:
                raise RuntimeError("Ollama did not become ready in time.")
            time.sleep(delay_seconds)


def _collect_required_models() -> list[str]:
    models: list[str] = []
    for key in MODEL_ENV_KEYS:
        value = str(os.getenv(key, "") or "").strip()
        if value and value not in models:
            models.append(value)

    rag_enabled = str(os.getenv("RAG_TEMPLATES_ENABLED", "false") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if rag_enabled:
        embed_model = str(os.getenv("RAG_TEMPLATES_EMBED_MODEL", "") or "").strip()
        if embed_model and embed_model not in models:
            models.append(embed_model)

    return models


def _list_installed_models(root_url: str) -> set[str]:
    payload = _read_json(f"{root_url}/api/tags")
    installed: set[str] = set()
    for item in payload.get("models", []) or []:
        name = str(item.get("name", "") or "").strip()
        if name:
            installed.add(name)
    return installed


def _pull_model(root_url: str, model_name: str) -> None:
    print(f"[bootstrap] Pulling missing Ollama model: {model_name}", flush=True)
    request = urllib.request.Request(
        f"{root_url}/api/pull",
        data=json.dumps({"name": model_name, "stream": True}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    last_percent = -1
    last_status = ""
    with urllib.request.urlopen(request, timeout=3600.0) as response:
        for raw_line in response:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue

            status = str(payload.get("status", "") or "").strip()
            completed = payload.get("completed")
            total = payload.get("total")

            if isinstance(completed, int) and isinstance(total, int) and total > 0:
                percent = int((completed / total) * 100)
                if percent != last_percent:
                    print(
                        f"[bootstrap] Pulling {model_name}: {percent}% ({completed}/{total})",
                        flush=True,
                    )
                    last_percent = percent
                last_status = status or last_status
                continue

            if status and status != last_status:
                print(f"[bootstrap] Pulling {model_name}: {status}", flush=True)
                last_status = status

            if payload.get("error"):
                raise RuntimeError(str(payload["error"]))

    print(f"[bootstrap] Model ready: {model_name}", flush=True)


def main() -> int:
    root_url = _normalize_root_url()
    _wait_for_ollama(root_url)

    required_models = _collect_required_models()
    if not required_models:
        print("[bootstrap] No Ollama models declared in env. Skipping pull.", flush=True)
        return 0

    installed_models = _list_installed_models(root_url)
    for model_name in required_models:
        if model_name in installed_models:
            print(f"[bootstrap] Ollama model already present: {model_name}", flush=True)
            continue
        _pull_model(root_url, model_name)
        installed_models.add(model_name)

    print("[bootstrap] Ollama bootstrap complete.", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # pragma: no cover - startup path
        print(f"[bootstrap] ERROR: {exc}", file=sys.stderr, flush=True)
        raise
