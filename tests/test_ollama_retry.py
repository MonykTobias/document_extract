"""Synthetic transport-retry checks for the Ollama client."""

from __future__ import annotations

import json
import os
from pathlib import Path

import requests


import document_extract.llm.ollama as ollama


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"[ok] {message}")


class Response:
    def __init__(self, status_code: int, payload: dict[str, object]) -> None:
        self.status_code = status_code
        self.payload = payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self) -> dict[str, object]:
        return self.payload


class Session:
    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.calls = 0

    def post(self, *_args: object, **kwargs: object) -> Response:
        self.calls += 1
        self.last_kwargs = kwargs
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def post() -> dict[str, object]:
    return ollama._ollama_post(
        base_url="http://stub",
        model="stub-model",
        prompt="prompt",
        image_b64="image",
        options={},
    )


def with_clean_transport_env(action) -> None:
    """Run with no transport settings inherited from the developer's shell."""
    names = (ollama.AUTH_TOKEN_ENV, ollama.EXTRA_HEADERS_ENV, ollama.CA_BUNDLE_ENV)
    saved = {name: os.environ.pop(name, None) for name in names}
    saved_bundle = ollama.CA_BUNDLE
    ollama.CA_BUNDLE = ""
    try:
        action()
    finally:
        ollama.CA_BUNDLE = saved_bundle
        for name in names:
            os.environ.pop(name, None)
            if saved[name] is not None:
                os.environ[name] = saved[name]


def with_session(session: Session, action) -> None:
    saved_session, saved_sleep = ollama._SESSION, ollama.time.sleep
    ollama._SESSION = session
    ollama.time.sleep = lambda _seconds: None
    try:
        action()
    finally:
        ollama._SESSION, ollama.time.sleep = saved_session, saved_sleep


def test_connection_error_retries() -> None:
    session = Session([requests.ConnectionError("temporary"), Response(200, {"ok": True})])

    def action() -> None:
        check(post() == {"ok": True}, "connection error retry returns the payload")

    with_session(session, action)
    check(session.calls == 2, "connection error is retried once")


def test_server_error_retries_three_times() -> None:
    session = Session([Response(500, {}), Response(500, {}), Response(500, {})])

    def action() -> None:
        try:
            post()
        except requests.HTTPError:
            check(True, "final server error raises")
        else:
            raise AssertionError("final server error did not raise")

    with_session(session, action)
    check(session.calls == 3, "server error receives three attempts")


def test_client_error_is_not_retried() -> None:
    session = Session([Response(400, {})])

    def action() -> None:
        try:
            post()
        except requests.HTTPError:
            check(True, "client error raises immediately")
        else:
            raise AssertionError("client error did not raise")

    with_session(session, action)
    check(session.calls == 1, "client error is not retried")


def test_effective_num_ctx_is_quantized() -> None:
    common = {"num_ctx": 16384, "num_predict": 4000, "auto": True}
    check(
        ollama.effective_num_ctx(prompt="", **common) == 16384,
        "auto context below the configured size is unchanged",
    )
    check(
        ollama.effective_num_ctx(prompt="x" * 25348, **common) == 20480,
        "auto context just above 16384 rounds up to 20480",
    )
    check(
        ollama.effective_num_ctx(prompt="x" * 41728, **common) == 20480,
        "auto context at 20480 stays on the same context step",
    )


def test_transport_defaults_are_unchanged() -> None:
    """With nothing configured, the request must match the historical one."""
    session = Session([Response(200, {"ok": True})])
    with_clean_transport_env(lambda: with_session(session, post))
    check(
        session.last_kwargs["headers"] == {"Content-Type": "application/json"},
        "no auth configured sends only the Content-Type header",
    )
    check(
        "verify" not in session.last_kwargs,
        "no CA bundle configured leaves requests' own TLS default in place",
    )


def test_auth_token_and_headers_are_applied() -> None:
    """A remote deployment can authenticate without a CLI flag or a YAML field."""
    session = Session([Response(200, {"ok": True})])

    def action() -> None:
        os.environ[ollama.AUTH_TOKEN_ENV] = "s3cret"
        os.environ[ollama.EXTRA_HEADERS_ENV] = "X-Tenant=acme, X-Trace=42"
        with_session(session, post)

    with_clean_transport_env(action)
    headers = session.last_kwargs["headers"]
    check(
        headers["Authorization"] == "Bearer s3cret",
        "the environment token becomes a bearer Authorization header",
    )
    check(
        headers["X-Tenant"] == "acme" and headers["X-Trace"] == "42",
        "extra headers are parsed from the environment",
    )


def test_ca_bundle_sources_and_precedence() -> None:
    """Explicit argument beats configuration, configuration beats environment."""
    for label, setup, expected in (
        ("environment", lambda: os.environ.__setitem__(ollama.CA_BUNDLE_ENV, "/env.pem"), "/env.pem"),
        ("configuration", lambda: setattr(ollama, "CA_BUNDLE", "/cfg.pem"), "/cfg.pem"),
    ):
        session = Session([Response(200, {"ok": True})])

        def action() -> None:
            setup()
            with_session(session, post)

        with_clean_transport_env(action)
        check(
            session.last_kwargs.get("verify") == expected,
            f"a CA bundle from {label} reaches the request",
        )

    session = Session([Response(200, {"ok": True})])

    def explicit() -> None:
        os.environ[ollama.CA_BUNDLE_ENV] = "/env.pem"
        ollama.CA_BUNDLE = "/cfg.pem"
        with_session(
            session,
            lambda: ollama._ollama_post(
                base_url="http://stub",
                model="stub-model",
                prompt="prompt",
                image_b64="image",
                options={},
                ca_bundle="/explicit.pem",
            ),
        )

    with_clean_transport_env(explicit)
    check(
        session.last_kwargs.get("verify") == "/explicit.pem",
        "an explicit CA bundle argument wins over configuration and environment",
    )


def test_credentials_never_reach_usage_or_messages() -> None:
    """A token must not leak into checkpoints, manifests, or error text."""
    def action() -> None:
        os.environ[ollama.AUTH_TOKEN_ENV] = "s3cret"
        usage = ollama.ollama_usage_from_payload(
            payload={"prompt_eval_count": 1, "eval_count": 2},
            prompt="prompt",
            output="output",
            image_path=Path(__file__),
        )
        check(
            "s3cret" not in json.dumps(usage),
            "the auth token never reaches the usage record",
        )

        session = Session([requests.ConnectionError("x")] * 3)

        def failing() -> None:
            try:
                ollama._ollama_post(
                    base_url="http://user:s3cret@stub",
                    model="m",
                    prompt="p",
                    image_b64="i",
                    options={},
                )
            except RuntimeError as error:
                check(
                    "s3cret" not in str(error) and "http://stub" in str(error),
                    "credentials embedded in the base URL are redacted in errors",
                )
            else:
                raise AssertionError("unreachable Ollama did not raise RuntimeError")

        with_session(session, failing)

    with_clean_transport_env(action)


def main() -> int:
    test_connection_error_retries()
    test_server_error_retries_three_times()
    test_client_error_is_not_retried()
    test_effective_num_ctx_is_quantized()
    test_transport_defaults_are_unchanged()
    test_auth_token_and_headers_are_applied()
    test_ca_bundle_sources_and_precedence()
    test_credentials_never_reach_usage_or_messages()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
