#!/usr/bin/env python3
import json
import socket
import urllib.error
import urllib.request


DEFAULT_URL = "http://127.0.0.1:1234/v1/chat/completions"
DEFAULT_MODEL = "local-model"
DEFAULT_REQUEST_TIMEOUT = 600.0


def request_chat_completion(
    url: str,
    payload: dict,
    timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT,
) -> str:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"LM Studio HTTP {exc.code}: {error_body}") from exc
    except (TimeoutError, socket.timeout) as exc:
        raise RuntimeError(
            f"LM Studio request timed out after {timeout_seconds} seconds."
        ) from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (TimeoutError, socket.timeout)):
            raise RuntimeError(
                f"LM Studio request timed out after {timeout_seconds} seconds."
            ) from exc
        raise RuntimeError(
            "Could not connect to LM Studio. "
            "Make sure the local server is running and API access is enabled."
        ) from exc
    except OSError as exc:
        if "timed out" in str(exc).lower():
            raise RuntimeError(
                f"LM Studio request timed out after {timeout_seconds} seconds."
            ) from exc
        raise

    try:
        result = json.loads(raw)
        return result["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Unexpected LM Studio response: {raw}") from exc
