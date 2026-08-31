from __future__ import annotations

import base64
import os
import re
import time
from pathlib import Path
from typing import Any

from ..markdown import postprocess as sp


IMAGE_TOKEN_ESTIMATE = 4000
_SESSION = None

# Optional remote-deployment settings. The default base URL is the Docker host's
# loopback, where none of this applies; these exist for the case where a user
# points --ollama-base-url at a shared GPU box.
#
# The token is environment-only on purpose: a CLI flag would expose it in
# process listings, and a YAML field invites committing it. The CA bundle is
# just a path, so it also gets a config field.
AUTH_TOKEN_ENV = "DOCLING_RAG_OLLAMA_AUTH_TOKEN"
EXTRA_HEADERS_ENV = "DOCLING_RAG_OLLAMA_HEADERS"
CA_BUNDLE_ENV = "DOCLING_RAG_OLLAMA_CA_BUNDLE"

# Set from models.ca_bundle by apply_detection_config, the same way the
# detection thresholds reach their modules. Keeps every call site unchanged.
CA_BUNDLE = ""

_USERINFO_RE = re.compile(r"://[^/@]*@")


def redact_url(url: str) -> str:
    """Strip any ``user:pass@`` before a URL reaches a message or a log."""
    return _USERINFO_RE.sub("://", url)


def request_headers() -> dict[str, str]:
    """Headers for one Ollama call, identical to the historical set by default."""
    headers = {"Content-Type": "application/json"}
    token = (os.getenv(AUTH_TOKEN_ENV) or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    for pair in (os.getenv(EXTRA_HEADERS_ENV) or "").split(","):
        name, separator, value = pair.partition("=")
        if separator and name.strip():
            headers[name.strip()] = value.strip()
    return headers


def request_verify(ca_bundle: str = "") -> str | None:
    """CA bundle for one Ollama call; None keeps requests' own default.

    Explicit argument first, then the configured value (which already reflects
    any environment override), then the environment directly for callers that
    never loaded a configuration.
    """
    return (ca_bundle or CA_BUNDLE or os.getenv(CA_BUNDLE_ENV) or "").strip() or None


def _session():
    global _SESSION
    if _SESSION is None:
        import requests  # noqa: PLC0415

        _SESSION = requests.Session()
    return _SESSION


def map_vlm_tasks(worker: Any, items: Any, max_workers: int) -> list[Any]:
    """Run one VLM task per item, preserving input order.

    max_workers <= 1 reproduces the serial loop exactly. Workers must touch
    only their own item; shared state (stats dicts, reporter prints, files not
    namespaced per item) stays in the caller. The first worker exception
    propagates and fails the stage, matching the serial loop's semantics.
    """
    items = list(items)
    if max_workers <= 1 or len(items) <= 1:
        return [worker(item) for item in items]
    from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415

    with ThreadPoolExecutor(max_workers=min(max_workers, len(items))) as pool:
        return list(pool.map(worker, items))


def image_to_base64(image_path: Path) -> str:
    return base64.b64encode(image_path.read_bytes()).decode("ascii")


def rough_text_token_estimate(text: str) -> int:
    return max(1, round(len(text) / 4)) if text else 0


def effective_num_ctx(
    *, prompt: str, num_ctx: int, num_predict: int, auto: bool
) -> int:
    if not auto:
        return num_ctx
    estimate = (
        rough_text_token_estimate(prompt)
        + IMAGE_TOKEN_ESTIMATE
        + max(num_predict, 1024)
        + 2048
    )
    if estimate <= num_ctx:
        return num_ctx
    return ((estimate + 4095) // 4096) * 4096


def _ollama_post(
    *,
    base_url: str,
    model: str,
    prompt: str,
    image_b64: str,
    options: dict[str, Any],
    ca_bundle: str = "",
) -> dict[str, Any]:
    import requests  # noqa: PLC0415

    url = f"{base_url.rstrip('/')}/api/chat"
    # Applied per request rather than on the shared session, so the session's
    # lifetime and sharing behaviour are untouched.
    headers = request_headers()
    verify = request_verify(ca_bundle)
    extra: dict[str, Any] = {} if verify is None else {"verify": verify}
    for attempt in range(3):
        try:
            response = _session().post(
                url,
                headers=headers,
                json={
                    "model": model,
                    "stream": False,
                    "options": options,
                    "messages": [
                        {"role": "user", "content": prompt, "images": [image_b64]}
                    ],
                },
                timeout=(10, 600),
                **extra,
            )
        except (requests.ConnectionError, requests.Timeout) as error:
            if attempt == 2:
                if isinstance(error, requests.ConnectionError):
                    raise RuntimeError(
                        f"Could not reach Ollama at {redact_url(url)}. If this is running "
                        "in Docker on Windows, pass --ollama-base-url "
                        "http://host.docker.internal:11434 "
                        "and make sure Ollama is bound to 0.0.0.0:11434."
                    ) from error
                raise
        else:
            if response.status_code < 500:
                response.raise_for_status()
                return response.json()
            if attempt == 2:
                response.raise_for_status()
        time.sleep((1, 4)[attempt])


def ollama_usage_from_payload(
    *,
    payload: dict[str, Any],
    prompt: str,
    output: str,
    image_path: Path,
) -> dict[str, Any]:
    prompt_tokens = payload.get("prompt_eval_count")
    output_tokens = payload.get("eval_count")
    estimated = prompt_tokens is None or output_tokens is None

    if prompt_tokens is None:
        prompt_tokens = rough_text_token_estimate(prompt)
    if output_tokens is None:
        output_tokens = rough_text_token_estimate(output)

    usage = {
        "provider": "ollama",
        "prompt_tokens": int(prompt_tokens),
        "output_tokens": int(output_tokens),
        "total_tokens": int(prompt_tokens) + int(output_tokens),
        "estimated": estimated,
        "prompt_characters": len(prompt),
        "output_characters": len(output),
        "image_bytes": image_path.stat().st_size,
    }
    if "done_reason" in payload:
        usage["done_reason"] = payload["done_reason"]
    for key in ("total_duration", "load_duration", "prompt_eval_duration", "eval_duration"):
        if key in payload:
            usage[key] = payload[key]
    return usage


def strip_markdown_fences(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if len(lines) < 2 or not lines[-1].strip().startswith("```"):
        return stripped
    return "\n".join(lines[1:-1]).strip()


def call_ollama_vlm(
    *,
    base_url: str,
    model: str,
    prompt: str,
    image_path: Path,
    temperature: float,
    num_ctx: int,
    num_predict: int = 0,
    auto_num_ctx: bool = False,
    ca_bundle: str = "",
) -> tuple[str, dict[str, Any]]:
    image_b64 = image_to_base64(image_path)
    ctx = effective_num_ctx(
        prompt=prompt, num_ctx=num_ctx, num_predict=num_predict, auto=auto_num_ctx
    )

    def make_options(temp: float, repeat_penalty: float | None) -> dict[str, Any]:
        options: dict[str, Any] = {"temperature": temp}
        if ctx > 0:
            options["num_ctx"] = ctx
        if num_predict > 0:
            options["num_predict"] = num_predict
        if repeat_penalty is not None:
            options["repeat_penalty"] = repeat_penalty
        return options

    payload = _ollama_post(
        base_url=base_url,
        model=model,
        prompt=prompt,
        image_b64=image_b64,
        options=make_options(temperature, None),
        ca_bundle=ca_bundle,
    )
    content = strip_markdown_fences(payload["message"]["content"])
    ratio, anomalous = sp.detect_repeated_lines(content)
    length_capped = payload.get("done_reason") == "length"
    retried = False

    if anomalous or length_capped:
        retried = True
        retry_payload = _ollama_post(
            base_url=base_url,
            model=model,
            prompt=prompt,
            image_b64=image_b64,
            options=make_options(max(temperature, 0.2), 1.1),
            ca_bundle=ca_bundle,
        )
        retry_content = strip_markdown_fences(retry_payload["message"]["content"])
        retry_ratio, retry_anom = sp.detect_repeated_lines(retry_content)
        if retry_ratio <= ratio:
            payload = retry_payload
            content = retry_content
            ratio = retry_ratio
            anomalous = retry_anom
            length_capped = payload.get("done_reason") == "length"

    usage = ollama_usage_from_payload(
        payload=payload, prompt=prompt, output=content, image_path=image_path
    )
    usage["num_ctx"] = ctx or None
    usage["repeated_line_ratio"] = ratio
    usage["decoding_anomaly"] = bool(anomalous)
    usage["length_capped"] = bool(length_capped)
    usage["retried"] = retried
    usage["context_overflow"] = bool(ctx and usage["prompt_tokens"] > 0.9 * ctx)
    return content, usage
