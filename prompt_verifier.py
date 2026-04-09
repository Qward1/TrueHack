#!/usr/bin/env python3
import json
import re

from lmstudio_client import (
    DEFAULT_MODEL,
    DEFAULT_REQUEST_TIMEOUT,
    DEFAULT_URL,
    request_chat_completion,
)


DEFAULT_VERIFICATION_TEMPERATURE = 0.0
DEFAULT_VERIFICATION_SYSTEM_PROMPT = (
    "You review whether a solution fully satisfies the user's request. "
    "Return strict JSON only with the keys: passed, score, summary, missing_requirements, warnings. "
    "Use passed=true only if all important requirements are satisfied."
)


def _extract_json_block(text: str) -> dict:
    cleaned = text.strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if not match:
        raise RuntimeError(f"Unexpected verification response: {text}")

    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Unexpected verification response: {text}") from exc


def _ensure_string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def normalize_verification_result(data: dict) -> dict:
    score = data.get("score", 0)
    try:
        score = int(score)
    except (TypeError, ValueError):
        score = 0
    score = max(0, min(100, score))

    return {
        "passed": bool(data.get("passed", False)),
        "score": score,
        "summary": str(data.get("summary", "")).strip(),
        "missing_requirements": _ensure_string_list(data.get("missing_requirements")),
        "warnings": _ensure_string_list(data.get("warnings")),
        "raw": data,
    }


def verify_prompt_requirements(
    prompt: str,
    solution_content: str,
    model: str = DEFAULT_MODEL,
    url: str = DEFAULT_URL,
    temperature: float = DEFAULT_VERIFICATION_TEMPERATURE,
    timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT,
    extra_context: str = "",
) -> dict:
    messages = [
        {"role": "system", "content": DEFAULT_VERIFICATION_SYSTEM_PROMPT},
        {"role": "user", "content": f"User request:\n{prompt}"},
    ]

    if extra_context.strip():
        messages.append({"role": "user", "content": f"Extra context:\n{extra_context}"})

    messages.extend(
        [
            {"role": "assistant", "content": solution_content},
            {
                "role": "user",
                "content": (
                    "Check whether the solution above fully satisfies the user request. "
                    "Return strict JSON only in this shape:\n"
                    '{'
                    '"passed": true, '
                    '"score": 100, '
                    '"summary": "short summary", '
                    '"missing_requirements": [], '
                    '"warnings": []'
                    '}'
                ),
            },
        ]
    )

    payload = {
        "model": model,
        "temperature": temperature,
        "messages": messages,
    }
    raw_response = request_chat_completion(url, payload, timeout_seconds)
    normalized = normalize_verification_result(_extract_json_block(raw_response))
    if not normalized["summary"]:
        normalized["summary"] = "Verification completed."
    return normalized
