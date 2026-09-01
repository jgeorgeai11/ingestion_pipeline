"""The one correct download implementation for the whole workspace.

Both former instance downloaders (`download_cms_iom.download_file` and
`download_usc_titles.download_file`) were character-for-character identical
apart from a ``verify`` argument, and both carried the same four defects:

  1. **Non-atomic writes.** Bytes streamed straight onto the final path, so an
     interrupted run left a truncated file that the *next* run's
     ``if dest.exists()`` check happily skipped — a silently corrupt corpus.
     :func:`fetch` streams to ``<dest>.part`` and ``replace``s onto the final
     name only after the stream completes.
  2. **No retries.** A single transient 5xx or dropped connection failed a
     target permanently. :func:`build_session` supplies a ``urllib3.Retry``
     adapter.
  3. **An over-narrow exception class.** The USC loop caught ``HTTPError`` and
     ``ConnectionError`` only; a ``ReadTimeout`` inherits from ``Timeout``,
     not ``ConnectionError``, so it escaped the per-target handler and aborted
     the whole run. Everything here raises :class:`FetchError`, so the runner
     has exactly one class to catch.
  4. **Leaked connections on the error path.** Neither downloader closed its
     response when the stream raised mid-transfer. The request is issued as a
     context manager here.

Two things this module deliberately does NOT offer: a status flag (a failure
is always an exception, because the runner's accounting depends on it) and a
way to turn off SSL verification from config. A genuinely broken host is
handled by passing in a session configured for it — a code change, and
therefore a visible decision.
"""

from pathlib import Path

import requests
import urllib3
from ingpipe_lib.logconfig import get_logger
from requests.adapters import HTTPAdapter

__all__ = ["FetchError", "build_session", "fetch"]

logger = get_logger(__name__)

# Streamed in 64 KiB blocks: large enough that the per-chunk Python overhead is
# negligible against multi-megabyte PDFs, small enough to bound memory.
CHUNK_SIZE = 64 * 1024

# The status codes worth retrying: 429 (rate limited, which polite scraping can
# still hit) and the transient 5xx family. A 404 is NOT retried -- a missing
# document is an answer, not a hiccup.
RETRY_STATUS_CODES = (429, 500, 502, 503, 504)


class FetchError(Exception):
    """Raised for any failure to place the requested bytes at the destination.

    One class for every cause -- transport error, HTTP status, size mismatch,
    over-limit response, or local I/O failure -- because the runner's
    per-target handler must be able to fail one target without accidentally
    letting a differently-typed failure abort the run. The original exception
    is always chained.
    """


class _TimeoutHTTPAdapter(HTTPAdapter):
    """An adapter that applies a default timeout to every request.

    ``requests`` has no session-level timeout: a session with no per-call
    ``timeout`` will block forever on a server that accepts the connection and
    then goes silent. Injecting the default here means every call site inherits
    it, rather than each having to remember.
    """

    def __init__(self, *args, timeout: float, **kwargs) -> None:
        """Store the default timeout and initialize the underlying adapter.

        Args:
            *args: Positional arguments forwarded to ``HTTPAdapter``.
            timeout: Seconds to use when a request specifies no timeout.
            **kwargs: Keyword arguments forwarded to ``HTTPAdapter``.
        """
        self._timeout = timeout
        super().__init__(*args, **kwargs)

    def send(self, request, **kwargs):  # type: ignore[no-untyped-def]
        """Send a request, defaulting its timeout when the caller gave none.

        Args:
            request: The prepared request to send.
            **kwargs: Transport keyword arguments from ``requests``.

        Returns:
            The ``requests.Response`` produced by the underlying adapter.
        """
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = self._timeout
        return super().send(request, **kwargs)


def build_session(
    *, retries: int = 3, backoff_factor: float = 0.5, timeout: float = 60.0
) -> requests.Session:
    """Build the session every fetch in a run shares.

    One session per run means one connection pool (so a 40-chapter manual
    reuses one TCP/TLS handshake instead of doing forty) and one retry policy.
    Retries cover connection errors, read errors, and the transient status
    codes in :data:`RETRY_STATUS_CODES`; a 404 is not retried because a missing
    document is a real answer.

    SSL verification is left at the ``requests`` default of ON. There is no
    parameter to disable it, and no config key in this repo may set it: the
    only remaining way to talk to a genuinely broken host is for a caller to
    construct its own session, which is a code change and therefore reviewable.

    Args:
        retries: Total retry attempts per request after the first. Defaults
            to 3.
        backoff_factor: urllib3 exponential backoff factor in seconds.
            Defaults to 0.5.
        timeout: Default per-request timeout in seconds, applied to any request
            that does not pass its own. Defaults to 60.0.

    Returns:
        A configured ``requests.Session``. The caller is responsible for
        closing it (or using it as a context manager).
    """
    retry = urllib3.Retry(
        total=retries,
        connect=retries,
        read=retries,
        status=retries,
        status_forcelist=RETRY_STATUS_CODES,
        backoff_factor=backoff_factor,
        # Retry the idempotent methods only; acquisition never POSTs.
        allowed_methods=frozenset({"GET", "HEAD"}),
        # Surface the final failed response as a normal response so
        # raise_for_status() produces a message naming the status, rather than
        # urllib3's MaxRetryError surfacing as a bare ConnectionError.
        raise_on_status=False,
    )
    adapter = _TimeoutHTTPAdapter(max_retries=retry, timeout=timeout)

    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    logger.debug(
        f"Built session: retries={retries}, backoff_factor={backoff_factor}, timeout={timeout}"
    )
    return session


def _partial_path(dest: Path) -> Path:
    """Return the in-progress path a download streams to.

    The suffix is appended rather than replaced (``a.tar.gz`` ->
    ``a.tar.gz.part``) so the real extension survives and two destinations
    differing only in extension cannot collide on one partial file.

    Args:
        dest: The final destination path.

    Returns:
        The ``.part`` sibling path.
    """
    return dest.with_suffix(dest.suffix + ".part")


def fetch(
    url: str,
    dest: Path,
    *,
    session: requests.Session,
    max_bytes: int | None = None,
) -> int:
    """Stream `url` to `dest`, atomically, returning the bytes written.

    The download lands on ``<dest>.part`` and is moved onto `dest` only after
    the stream completes and every size check passes, so an interrupted run
    leaves NO file at `dest` -- the next run therefore re-fetches instead of
    skipping a truncated one. On any failure the partial file is removed, so a
    failed target leaves no residue either.

    Args:
        url: The absolute URL to fetch.
        dest: The final destination path. Parent directories are created.
        session: The shared session from :func:`build_session`, carrying the
            retry policy, the default timeout, and the SSL setting.
        max_bytes: An optional ceiling. Streaming aborts as soon as it is
            exceeded, so a mis-resolved URL pointing at something enormous
            cannot fill the disk before anyone notices. None means no limit.

    Returns:
        The number of bytes written to `dest`.

    Raises:
        FetchError: On any transport failure, non-2xx status, ``Content-Length``
            mismatch, ``max_bytes`` overrun, or local I/O error. Never returns
            a status flag: the runner's failure accounting is driven entirely
            by this exception.
    """
    logger.info(f"Fetching: {url} -> {dest}")
    partial = _partial_path(dest)

    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        # The context manager is the fix for the leaked connection: if the
        # stream raises mid-transfer, the response is still released.
        with session.get(url, stream=True) as response:
            response.raise_for_status()
            declared = _declared_length(response, url)

            written = 0
            with open(partial, "wb") as handle:
                for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                    if not chunk:
                        continue
                    written += len(chunk)
                    if max_bytes is not None and written > max_bytes:
                        raise FetchError(
                            f"Response from {url} exceeded max_bytes={max_bytes:,} "
                            f"(at least {written:,} bytes)"
                        )
                    handle.write(chunk)

        if declared is not None and written != declared:
            raise FetchError(
                f"Truncated download from {url}: Content-Length declared "
                f"{declared:,} bytes but {written:,} were received"
            )

        partial.replace(dest)
    except requests.RequestException as e:
        partial.unlink(missing_ok=True)
        logger.error(f"Transport failure fetching {url}: {e}")
        raise FetchError(f"Failed to fetch {url}: {e}") from e
    except OSError as e:
        partial.unlink(missing_ok=True)
        logger.error(f"Local I/O failure writing {dest}: {e}")
        raise FetchError(f"Failed to write {dest} while fetching {url}: {e}") from e
    except FetchError:
        # Already a typed, logged-at-the-raise-site failure; just clean up.
        partial.unlink(missing_ok=True)
        raise

    logger.info(f"Fetched {dest.name} ({written:,} bytes)")
    return written


def _declared_length(response: requests.Response, url: str) -> int | None:
    """Return the response's ``Content-Length``, or None when unusable.

    A server may omit the header entirely (chunked transfer encoding) or send
    something non-numeric. Neither is worth failing the download over -- the
    header is a cross-check, not the source of truth -- but a *usable* value is
    then compared against the bytes received, which is what catches a
    connection cut mid-transfer that ``iter_content`` reports as a clean end.

    Args:
        response: The streaming response.
        url: The URL, for the warning message.

    Returns:
        The declared byte count, or None when the header is absent or
        malformed. A response with ``Content-Encoding`` set returns None too:
        the header then describes the compressed size while ``iter_content``
        yields decoded bytes, so comparing them would fail every time.
    """
    if response.headers.get("Content-Encoding"):
        return None

    raw = response.headers.get("Content-Length")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        logger.warning(f"Ignoring non-numeric Content-Length {raw!r} from {url}")
        return None
