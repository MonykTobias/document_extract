"""Synthetic transport-retry checks for the Ollama client."""

from __future__ import annotations

import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

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

    def post(self, *_args: object, **_kwargs: object) -> Response:
        self.calls += 1
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


def main() -> int:
    test_connection_error_retries()
    test_server_error_retries_three_times()
    test_client_error_is_not_retried()
    test_effective_num_ctx_is_quantized()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
