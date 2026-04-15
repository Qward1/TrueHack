from __future__ import annotations

import json
import os
import re
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
    "OLLAMA_MODEL_TEMPLATE_SELECTOR",
    "OLLAMA_MODEL_CODE_REFINER",
    "OLLAMA_MODEL_CODE_VALIDATOR",
    "OLLAMA_MODEL_VALIDATION_FIXER",
    "OLLAMA_MODEL_VERIFICATION_FIXER",
    "OLLAMA_MODEL_REQUIREMENTS_VERIFIER",
    "OLLAMA_MODEL_SOLUTION_EXPLAINER",
    "OLLAMA_MODEL_QUESTION_ANSWERER",
    "OLLAMA_MODEL_RESPONSE_ASSEMBLER",
)
DEFAULT_CREATE_MODEL_NAME = ""
DEFAULT_CREATE_MODEL_FILE = "/app/Modelfile"


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
    create_model_name = str(os.getenv("OLLAMA_CREATE_MODEL_NAME", DEFAULT_CREATE_MODEL_NAME) or "").strip()
    create_model_file = str(os.getenv("OLLAMA_CREATE_MODEL_FILE", DEFAULT_CREATE_MODEL_FILE) or "").strip()
    for key in MODEL_ENV_KEYS:
        value = str(os.getenv(key, "") or "").strip()
        if create_model_name and value == create_model_name:
            continue
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

    if create_model_name and create_model_file and os.path.exists(create_model_file):
        try:
            with open(create_model_file, "r", encoding="utf-8") as handle:
                for raw_line in handle:
                    line = raw_line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if line.upper().startswith("FROM "):
                        base_model = line.split(None, 1)[1].strip()
                        if base_model and base_model not in models:
                            models.append(base_model)
        except OSError:
            pass

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


def _streaming_post(root_url: str, endpoint: str, payload: dict, success_message: str) -> None:
    request = urllib.request.Request(
        f"{root_url}{endpoint}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    last_status = ""
    with urllib.request.urlopen(request, timeout=3600.0) as response:
        for raw_line in response:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue

            status = str(item.get("status", "") or "").strip()
            if status and status != last_status:
                print(f"[bootstrap] {status}", flush=True)
                last_status = status
            if item.get("error"):
                raise RuntimeError(str(item["error"]))

    print(success_message, flush=True)


def _coerce_parameter_value(raw_value: str) -> object:
    value = raw_value.strip()
    lowered = value.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if re.fullmatch(r"-?\d+", value):
        try:
            return int(value)
        except ValueError:
            return value
    if re.fullmatch(r"-?\d+\.\d+", value):
        try:
            return float(value)
        except ValueError:
            return value
    return value


def _parse_modelfile(modelfile_path: str) -> dict:
    with open(modelfile_path, "r", encoding="utf-8") as handle:
        lines = handle.readlines()

    payload: dict[str, object] = {}
    parameters: dict[str, object] = {}
    licenses: list[str] = []
    messages: list[dict[str, str]] = []

    index = 0
    while index < len(lines):
        raw_line = lines[index].rstrip("\n")
        stripped = raw_line.strip()
        index += 1

        if not stripped or stripped.startswith("#"):
            continue

        parts = stripped.split(None, 1)
        command = parts[0].upper()
        remainder = parts[1] if len(parts) > 1 else ""

        if remainder.startswith('"""'):
            block_parts: list[str] = []
            trailing = remainder[3:]
            if trailing.endswith('"""') and trailing != '"""':
                remainder = trailing[:-3]
            else:
                if trailing:
                    block_parts.append(trailing)
                while index < len(lines):
                    block_line = lines[index].rstrip("\n")
                    index += 1
                    if block_line.endswith('"""'):
                        block_parts.append(block_line[:-3])
                        break
                    block_parts.append(block_line)
                remainder = "\n".join(block_parts)

        value = remainder.strip()

        if command == "FROM":
            payload["from"] = value
        elif command == "PARAMETER":
            param_parts = value.split(None, 1)
            if len(param_parts) != 2:
                continue
            parameters[param_parts[0]] = _coerce_parameter_value(param_parts[1])
        elif command == "SYSTEM":
            payload["system"] = value
        elif command == "TEMPLATE":
            payload["template"] = value
        elif command == "LICENSE":
            licenses.append(value)
        elif command == "MESSAGE":
            message_parts = value.split(None, 1)
            if len(message_parts) != 2:
                continue
            messages.append({"role": message_parts[0], "content": message_parts[1]})

    if parameters:
        payload["parameters"] = parameters
    if licenses:
        payload["license"] = licenses if len(licenses) > 1 else licenses[0]
    if messages:
        payload["messages"] = messages
    return payload


def _create_model(root_url: str, model_name: str, modelfile_path: str) -> None:
    payload = _parse_modelfile(modelfile_path)
    payload["model"] = model_name
    payload["stream"] = True
    if "from" not in payload:
        raise RuntimeError(f"Modelfile is missing FROM: {modelfile_path}")
    print(f"[bootstrap] Creating Ollama model from Modelfile: {model_name}", flush=True)
    _streaming_post(
        root_url,
        "/api/create",
        payload,
        f"[bootstrap] Custom Ollama model ready: {model_name}",
    )


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

    create_model_name = str(os.getenv("OLLAMA_CREATE_MODEL_NAME", DEFAULT_CREATE_MODEL_NAME) or "").strip()
    create_model_file = str(os.getenv("OLLAMA_CREATE_MODEL_FILE", DEFAULT_CREATE_MODEL_FILE) or "").strip()
    if create_model_name and create_model_file and os.path.exists(create_model_file):
        recreate = str(os.getenv("OLLAMA_CREATE_MODEL_RECREATE", "0") or "").strip().lower() in {"1", "true", "yes", "on"}
        if recreate or create_model_name not in installed_models:
            _create_model(root_url, create_model_name, create_model_file)
            installed_models.add(create_model_name)
        else:
            print(f"[bootstrap] Custom Ollama model already present: {create_model_name}", flush=True)

    print("[bootstrap] Ollama bootstrap complete.", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # pragma: no cover - startup path
        print(f"[bootstrap] ERROR: {exc}", file=sys.stderr, flush=True)
        raise
