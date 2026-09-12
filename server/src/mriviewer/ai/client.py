"""OpenAI-compatible chat client on stdlib urllib.

No SDK, no httpx: this repository ships to machines with no package index, and
one more dependency is one more thing to vendor. The API is a POST with a JSON
body; that does not need a library.

``allow_egress`` is enforced here, by resolving the host and refusing anything
outside loopback/RFC1918 unless it is explicitly permitted. A comment in a
config file is not a control.
"""
from __future__ import annotations

import ipaddress
import json
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse


class EgressBlocked(Exception):
    """The configured endpoint is off-machine and egress is not permitted."""


class LlmError(Exception):
    """The model could not be reached or returned something unusable."""


@dataclass
class LlmResponse:
    content: str
    model: str
    latency_ms: int
    prompt_tokens: int | None
    completion_tokens: int | None


def _is_local(host: str) -> bool:
    """True when every address the host resolves to is loopback or private."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    addrs = {i[4][0] for i in infos}
    if not addrs:
        return False
    for a in addrs:
        try:
            ip = ipaddress.ip_address(a)
        except ValueError:
            return False
        if not (ip.is_loopback or ip.is_private or ip.is_link_local):
            return False
    return True


def check_egress(base_url: str, allow_egress: bool) -> None:
    """Raise unless we are allowed to talk to this endpoint."""
    host = urlparse(base_url).hostname or ""
    if not host:
        raise LlmError("ai.base_url has no host")
    if allow_egress or _is_local(host):
        return
    raise EgressBlocked(
        "%s is not on this machine or the local network and ai.allow_egress "
        "is false" % host)


def chat(base_url: str, model: str, api_key: str | None,
         messages: list[dict[str, str]], *, timeout_s: float = 60.0,
         allow_egress: bool = False, json_mode: bool = True) -> LlmResponse:
    """One chat completion. Raises LlmError on anything unusable."""
    check_egress(base_url, allow_egress)

    body: dict[str, Any] = {
        "model": model, "messages": messages, "temperature": 0.2, "stream": False,
    }
    if json_mode:
        # DeepSeek rejects this unless the word "json" appears in the prompt;
        # the prompt template says so, and the test asserts it.
        body["response_format"] = {"type": "json_object"}

    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            **({"Authorization": "Bearer " + api_key} if api_key else {}),
        },
        method="POST",
    )

    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        # Never echo the Authorization header or key material into the message.
        raise LlmError("HTTP %d: %s" % (exc.code, detail)) from None
    except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
        raise LlmError("unreachable: %s" % exc) from None
    except json.JSONDecodeError as exc:
        raise LlmError("response was not JSON: %s" % exc) from None

    latency_ms = int((time.perf_counter() - started) * 1000)
    try:
        content = raw["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise LlmError("no message content in response") from None

    usage = raw.get("usage") or {}
    return LlmResponse(
        content=content, model=raw.get("model", model), latency_ms=latency_ms,
        prompt_tokens=usage.get("prompt_tokens"),
        completion_tokens=usage.get("completion_tokens"),
    )


def list_models(base_url: str, api_key: str | None, *, timeout_s: float = 10.0,
                allow_egress: bool = False) -> list[str]:
    """Model ids the endpoint advertises.

    Used by /ai/status to warn when the configured model no longer exists -
    DeepSeek retired `deepseek-chat` and `deepseek-reasoner`, so a hard-coded
    name is a future outage.
    """
    check_egress(base_url, allow_egress)
    req = urllib.request.Request(
        base_url.rstrip("/") + "/models",
        headers={"Authorization": "Bearer " + api_key} if api_key else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:                      # noqa: BLE001
        raise LlmError(str(exc)[:200]) from None
    return [m.get("id") for m in raw.get("data", []) if m.get("id")]
