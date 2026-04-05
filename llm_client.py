import http.client
import json
from pathlib import Path
import posixpath
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit, urlunsplit

from config import ModelConfig


class LocalModelError(RuntimeError):
    pass


@dataclass
class LocalChatModel:
    config: ModelConfig

    def chat(
        self,
        messages: Iterable[Mapping[str, Any]],
        *,
        temperature: float | None = None,
    ) -> str:
        payload = {
            "model": self.config.model,
            "messages": list(messages),
            "temperature": self.config.temperature if temperature is None else temperature,
        }
        errors: list[str] = []

        for base_url in self._candidate_base_urls():
            try:
                return self._chat_once(base_url=base_url, payload=payload)
            except LocalModelError as exc:
                errors.append(f"{base_url}: {exc}")

        raise LocalModelError(
            "Could not connect to the local model server. "
            f"Tried: {'; '.join(errors)}"
        )

    def _chat_once(self, *, base_url: str, payload: dict[str, Any]) -> str:
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url=f"{base_url.rstrip('/')}/chat/completions",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.config.api_key}",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(
                request,
                timeout=self.config.timeout_seconds,
            ) as response:
                raw_response = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")
            raise LocalModelError(
                f"request failed with HTTP {exc.code}: {details}"
            ) from exc
        except urllib.error.URLError as exc:
            raise LocalModelError(f"connection error: {exc.reason}") from exc
        except http.client.RemoteDisconnected as exc:
            raise LocalModelError(
                "remote server closed the connection without a response. "
                "Check that LM Studio is running, the model is loaded, and the local API server is enabled."
            ) from exc
        except http.client.IncompleteRead as exc:
            raise LocalModelError(
                f"incomplete response from local model server: {exc}"
            ) from exc
        except (ConnectionResetError, TimeoutError, OSError) as exc:
            raise LocalModelError(
                f"transport error while talking to local model server: {exc}"
            ) from exc

        try:
            data = json.loads(raw_response)
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, json.JSONDecodeError) as exc:
            raise LocalModelError("unexpected response payload") from exc

        if isinstance(content, str):
            return content

        if isinstance(content, list):
            text_parts: list[str] = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text_parts.append(str(block.get("text", "")))
            return "\n".join(text_parts).strip()

        raise LocalModelError("unsupported response content format")

    def _candidate_base_urls(self) -> list[str]:
        base_url = self._normalize_base_url(self.config.base_url)
        candidates = [base_url]
        parsed = urlsplit(base_url)

        if parsed.hostname not in {"127.0.0.1", "localhost"}:
            return candidates

        candidate_hosts = ["127.0.0.1", "localhost"]
        wsl_host_ip = self._read_wsl_host_ip()
        if wsl_host_ip:
            candidate_hosts.append(wsl_host_ip)

        for host in candidate_hosts:
            netloc = host
            if parsed.port:
                netloc = f"{host}:{parsed.port}"
            candidate_url = urlunsplit(
                (parsed.scheme, netloc, self._normalize_path(parsed.path), "", "")
            )
            if candidate_url not in candidates:
                candidates.append(candidate_url)

        return candidates

    @staticmethod
    def _read_wsl_host_ip() -> str | None:
        resolv_conf = Path("/etc/resolv.conf")
        if not resolv_conf.exists():
            return None

        for line in resolv_conf.read_text(encoding="utf-8").splitlines():
            parts = line.strip().split()
            if len(parts) == 2 and parts[0] == "nameserver":
                return parts[1]

        return None

    @classmethod
    def _normalize_base_url(cls, raw_url: str) -> str:
        parsed = urlsplit(raw_url.strip())
        normalized_path = cls._normalize_path(parsed.path)
        return urlunsplit((parsed.scheme, parsed.netloc, normalized_path, "", ""))

    @staticmethod
    def _normalize_path(path: str) -> str:
        raw_segments = [segment for segment in (path or "/").split("/") if segment]
        normalized = "/" + "/".join(raw_segments)
        collapsed = posixpath.normpath(normalized)
        if collapsed == ".":
            collapsed = "/"
        if not collapsed.startswith("/"):
            collapsed = f"/{collapsed}"
        return collapsed.rstrip("/")
