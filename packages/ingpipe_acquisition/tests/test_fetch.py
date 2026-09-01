"""Tests for ingpipe_acquisition.fetch.

Four of these are regression guards for defects that were live in BOTH former
instance downloaders: the truncated-file-left-behind defect (which the next
run then skipped), the missing retry policy, the over-narrow exception class,
and the connection leaked on the error path. The retry tests run against a
throwaway ``localhost`` HTTP server rather than a mock, because the retry
happens inside urllib3 -- mocking at the adapter boundary would test the mock.
"""

import http.server
import threading
from pathlib import Path

import pytest
import requests
from ingpipe_acquisition.fetch import CHUNK_SIZE, FetchError, build_session, fetch


class _FakeResponse:
    """A minimal streaming response standing in for ``requests.Response``."""

    def __init__(
        self,
        chunks: list[bytes],
        *,
        headers: dict[str, str] | None = None,
        raise_for_status: Exception | None = None,
        raise_mid_stream: Exception | None = None,
    ) -> None:
        self.chunks = chunks
        self.headers = headers or {}
        self._raise_for_status = raise_for_status
        self._raise_mid_stream = raise_mid_stream
        self.closed = False

    def raise_for_status(self) -> None:
        if self._raise_for_status is not None:
            raise self._raise_for_status

    def iter_content(self, chunk_size: int = 1):
        for chunk in self.chunks:
            yield chunk
        if self._raise_mid_stream is not None:
            raise self._raise_mid_stream

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc_info) -> None:
        self.closed = True


class _FakeSession:
    """A session whose ``get`` returns a queued :class:`_FakeResponse`."""

    def __init__(self, response: _FakeResponse) -> None:
        self.response = response
        self.calls: list[tuple[str, dict]] = []

    def get(self, url: str, **kwargs) -> _FakeResponse:
        self.calls.append((url, kwargs))
        return self.response


def _partial(dest: Path) -> Path:
    """Return the ``.part`` sibling the download streams to."""
    return dest.with_suffix(dest.suffix + ".part")


# ---------------------------------------------------------------------------
# fetch: the happy path and the atomicity guarantee
# ---------------------------------------------------------------------------


def test_fetch_writes_the_file_and_returns_the_byte_count(tmp_path):
    """A successful fetch writes the bytes, returns the count, and cleans up."""
    dest = tmp_path / "nested" / "doc.pdf"
    response = _FakeResponse([b"abc", b"defg"], headers={"Content-Length": "7"})
    session = _FakeSession(response)

    written = fetch("https://example.test/doc.pdf", dest, session=session)  # type: ignore[arg-type]

    assert written == 7
    assert dest.read_bytes() == b"abcdefg"
    assert not _partial(dest).exists()


def test_fetch_requests_the_response_as_a_stream_context_manager(tmp_path):
    """The response is closed on the way out, even on the success path."""
    dest = tmp_path / "doc.pdf"
    response = _FakeResponse([b"abc"])
    session = _FakeSession(response)

    fetch("https://example.test/doc.pdf", dest, session=session)  # type: ignore[arg-type]

    assert session.calls[0][1]["stream"] is True
    assert response.closed is True


def test_fetch_skips_empty_keepalive_chunks(tmp_path):
    """Empty keep-alive chunks do not count toward the byte total."""
    dest = tmp_path / "doc.pdf"
    session = _FakeSession(_FakeResponse([b"ab", b"", b"cd"]))

    written = fetch("https://example.test/doc.pdf", dest, session=session)  # type: ignore[arg-type]

    assert written == 4


def test_fetch_stream_failing_mid_transfer_leaves_no_file_and_reraises(tmp_path):
    """Regression guard: an interrupted download leaves NOTHING behind.

    The former downloaders streamed onto the final path, so an interruption
    left a truncated file that the next run's existence check then skipped --
    silently corrupting the corpus. Neither the destination nor the partial
    file may survive.
    """
    dest = tmp_path / "doc.pdf"
    response = _FakeResponse(
        [b"partial"], raise_mid_stream=requests.ConnectionError("connection reset")
    )
    session = _FakeSession(response)

    with pytest.raises(FetchError, match="Failed to fetch"):
        fetch("https://example.test/doc.pdf", dest, session=session)  # type: ignore[arg-type]

    assert not dest.exists()
    assert not _partial(dest).exists()
    assert response.closed is True


def test_fetch_does_not_disturb_an_existing_destination_on_failure(tmp_path):
    """A failed re-fetch leaves the previously-good file untouched."""
    dest = tmp_path / "doc.pdf"
    dest.write_bytes(b"the good copy")
    session = _FakeSession(
        _FakeResponse([b"bad"], raise_mid_stream=requests.ReadTimeout("too slow"))
    )

    with pytest.raises(FetchError):
        fetch("https://example.test/doc.pdf", dest, session=session)  # type: ignore[arg-type]

    assert dest.read_bytes() == b"the good copy"


def test_fetch_wraps_read_timeout_in_fetch_error(tmp_path):
    """Regression guard for the over-narrow exception class.

    ``ReadTimeout`` inherits from ``Timeout``, not ``ConnectionError``, so the
    old USC loop's two-clause handler let it escape and abort the whole run.
    Everything here arrives as one catchable type.
    """
    dest = tmp_path / "doc.pdf"
    session = _FakeSession(_FakeResponse([], raise_mid_stream=requests.ReadTimeout("too slow")))

    with pytest.raises(FetchError):
        fetch("https://example.test/doc.pdf", dest, session=session)  # type: ignore[arg-type]


def test_fetch_non_2xx_status_raises_fetch_error(tmp_path):
    """An HTTP error status fails the target with the typed error."""
    dest = tmp_path / "doc.pdf"
    session = _FakeSession(
        _FakeResponse([], raise_for_status=requests.HTTPError("404 Client Error"))
    )

    with pytest.raises(FetchError, match="404"):
        fetch("https://example.test/doc.pdf", dest, session=session)  # type: ignore[arg-type]

    assert not dest.exists()


def test_fetch_local_io_failure_raises_fetch_error(tmp_path, mocker):
    """A local write failure is a FetchError, not a bare OSError."""
    dest = tmp_path / "doc.pdf"
    session = _FakeSession(_FakeResponse([b"abc"]))
    mocker.patch("ingpipe_acquisition.fetch.open", side_effect=OSError("read-only file system"))

    with pytest.raises(FetchError, match="Failed to write"):
        fetch("https://example.test/doc.pdf", dest, session=session)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# fetch: the size checks
# ---------------------------------------------------------------------------


def test_fetch_content_length_mismatch_raises(tmp_path):
    """A short read against a declared length is a truncation, not a success."""
    dest = tmp_path / "doc.pdf"
    session = _FakeSession(_FakeResponse([b"abc"], headers={"Content-Length": "99"}))

    with pytest.raises(FetchError, match="Truncated download"):
        fetch("https://example.test/doc.pdf", dest, session=session)  # type: ignore[arg-type]

    assert not dest.exists()
    assert not _partial(dest).exists()


def test_fetch_ignores_content_length_when_the_body_is_encoded(tmp_path):
    """A compressed body's declared length describes the compressed bytes."""
    dest = tmp_path / "doc.pdf"
    session = _FakeSession(
        _FakeResponse(
            [b"decoded bytes"],
            headers={"Content-Length": "4", "Content-Encoding": "gzip"},
        )
    )

    written = fetch("https://example.test/doc.pdf", dest, session=session)  # type: ignore[arg-type]

    assert written == len(b"decoded bytes")


def test_fetch_ignores_a_non_numeric_content_length(tmp_path):
    """A malformed header is a cross-check that cannot be made, not a failure."""
    dest = tmp_path / "doc.pdf"
    session = _FakeSession(_FakeResponse([b"abc"], headers={"Content-Length": "many"}))

    assert fetch("https://example.test/doc.pdf", dest, session=session) == 3  # type: ignore[arg-type]


def test_fetch_missing_content_length_is_accepted(tmp_path):
    """A chunked response with no declared length still downloads."""
    dest = tmp_path / "doc.pdf"
    session = _FakeSession(_FakeResponse([b"abc"]))

    assert fetch("https://example.test/doc.pdf", dest, session=session) == 3  # type: ignore[arg-type]


def test_fetch_response_exceeding_max_bytes_raises(tmp_path):
    """The ceiling aborts the stream rather than filling the disk first."""
    dest = tmp_path / "doc.pdf"
    session = _FakeSession(_FakeResponse([b"a" * 10, b"b" * 10]))

    with pytest.raises(FetchError, match="exceeded max_bytes"):
        fetch("https://example.test/doc.pdf", dest, session=session, max_bytes=15)  # type: ignore[arg-type]

    assert not dest.exists()
    assert not _partial(dest).exists()


def test_fetch_within_max_bytes_succeeds(tmp_path):
    """A response at exactly the ceiling is allowed."""
    dest = tmp_path / "doc.pdf"
    session = _FakeSession(_FakeResponse([b"abcde"]))

    assert fetch("https://example.test/doc.pdf", dest, session=session, max_bytes=5) == 5  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# build_session
# ---------------------------------------------------------------------------


def test_build_session_verifies_ssl_by_default():
    """Verification is on and there is no parameter that can turn it off."""
    with build_session() as session:
        assert session.verify is True


def test_build_session_configures_retries_for_connect_read_and_status():
    """The retry policy covers connection errors as well as transient 5xx."""
    with build_session(retries=5, backoff_factor=0.25, timeout=7.0) as session:
        retry = session.get_adapter("https://example.test").max_retries

        assert (retry.total, retry.connect, retry.read, retry.status) == (5, 5, 5, 5)
        assert retry.backoff_factor == 0.25
        assert 503 in retry.status_forcelist
        assert 404 not in retry.status_forcelist


def test_build_session_applies_a_default_timeout_to_every_request(mocker):
    """A caller that passes no timeout still gets one, so no call hangs."""
    with build_session(timeout=3.5) as session:
        adapter = session.get_adapter("https://example.test")
        send = mocker.patch("requests.adapters.HTTPAdapter.send")

        adapter.send(mocker.Mock())

        assert send.call_args.kwargs["timeout"] == 3.5


def test_build_session_respects_an_explicit_timeout(mocker):
    """An explicit per-request timeout is not overridden by the default."""
    with build_session(timeout=3.5) as session:
        adapter = session.get_adapter("https://example.test")
        send = mocker.patch("requests.adapters.HTTPAdapter.send")

        adapter.send(mocker.Mock(), timeout=1.0)

        assert send.call_args.kwargs["timeout"] == 1.0


# ---------------------------------------------------------------------------
# Retry behavior against a real (localhost) server
# ---------------------------------------------------------------------------


class _FlakyHandler(http.server.BaseHTTPRequestHandler):
    """Serves 503 for the first ``fail_times`` requests, then 200."""

    fail_times = 0
    seen = 0
    body = b"payload"

    def do_GET(self) -> None:
        type(self).seen += 1
        if type(self).seen <= type(self).fail_times:
            self.send_response(503)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(type(self).body)))
        self.end_headers()
        self.wfile.write(type(self).body)

    def log_message(self, *args) -> None:
        """Silence the handler's stderr access log during tests."""


@pytest.fixture
def flaky_server():
    """Run a localhost server that fails a configurable number of times."""
    _FlakyHandler.seen = 0
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FlakyHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/doc.bin"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_session_retries_a_transient_5xx_and_then_succeeds(tmp_path, flaky_server):
    """Regression guard: a transient 5xx no longer fails its target."""
    _FlakyHandler.fail_times = 2
    dest = tmp_path / "doc.bin"

    with build_session(retries=3, backoff_factor=0.0, timeout=5.0) as session:
        written = fetch(flaky_server, dest, session=session)

    assert written == len(_FlakyHandler.body)
    assert _FlakyHandler.seen == 3


def test_session_gives_up_after_the_configured_retry_count(tmp_path, flaky_server):
    """Retries are bounded: a permanently failing host fails its target."""
    _FlakyHandler.fail_times = 99
    dest = tmp_path / "doc.bin"

    with build_session(retries=2, backoff_factor=0.0, timeout=5.0) as session:
        with pytest.raises(FetchError):
            fetch(flaky_server, dest, session=session)

    # The first attempt plus two retries.
    assert _FlakyHandler.seen == 3
    assert not dest.exists()


def test_session_connection_error_fails_as_fetch_error(tmp_path):
    """A refused connection surfaces as FetchError after exhausting retries."""
    dest = tmp_path / "doc.bin"

    with build_session(retries=1, backoff_factor=0.0, timeout=2.0) as session:
        # Port 1 on the loopback interface refuses connections immediately.
        with pytest.raises(FetchError, match="Failed to fetch"):
            fetch("http://127.0.0.1:1/doc.bin", dest, session=session)

    assert not dest.exists()


def test_chunk_size_is_a_sane_streaming_block():
    """The streaming block size is bounded memory, not a whole-file read."""
    assert 0 < CHUNK_SIZE <= 1024 * 1024
