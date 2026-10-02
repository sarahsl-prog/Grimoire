"""Synchronous HTTP client for the Grimoire REST API.

Sync on purpose: Qt owns the event loop, and every call from the GUI runs on
a worker thread rather than in an asyncio task.  Responses are parsed with the
server's own Pydantic models, so the wire format has exactly one definition.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import quote

import httpx
from loguru import logger
from pydantic import BaseModel

from grimoire.api.schemas import (
    DocumentDetailResponse,
    DocumentListResponse,
    IngestResultResponse,
    QueryResponse,
    SearchResponse,
)
from grimoire.client.config import SUPPORTED_EXTENSIONS, GuiConfig
from grimoire.client.errors import (
    AuthFailed,
    ConnectionFailed,
    GuiError,
    MalformedResponse,
    RateLimited,
    RequestRejected,
    ServerError,
    TimedOut,
)

ModelT = TypeVar("ModelT", bound=BaseModel)

_API_PREFIX = "/api/v1"


class GrimoireClient:
    """Typed access to the endpoints the desktop client needs.

    Every public method raises only GuiError subclasses.  That is the
    contract the widgets depend on: they never handle httpx or pydantic
    exceptions.
    """

    def __init__(self, config: GuiConfig) -> None:
        self.config = config
        try:
            self._http = httpx.Client(
                base_url=config.base_url,
                headers=self._headers(config),
                timeout=httpx.Timeout(
                    connect=config.connect_timeout,
                    read=config.read_timeout,
                    write=config.read_timeout,
                    pool=config.connect_timeout,
                ),
            )
        except httpx.InvalidURL as exc:
            # httpx.InvalidURL does not subclass httpx.HTTPError, so _request's
            # translation never sees it - a malformed base_url must be caught
            # here instead, since Task 6 constructs a client at startup and
            # whenever a pasted API key changes.
            raise ConnectionFailed(
                f"Invalid Grimoire API URL: {config.base_url}"
            ) from exc

    @staticmethod
    def _headers(config: GuiConfig) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if config.api_key:
            headers["X-API-Key"] = config.api_key
        if config.session_id:
            # Set once on the client's default headers, so every request,
            # health checks included, carries it.
            headers["X-Session-Id"] = config.session_id
        return headers

    def close(self) -> None:
        """Release the underlying connection pool."""
        self._http.close()

    # -- endpoints ---------------------------------------------------------

    def health(self) -> bool:
        """Whether /health answers 200.  Never raises."""
        try:
            response = self._http.get("/health", timeout=self.config.connect_timeout)
        except httpx.HTTPError as exc:
            logger.debug(f"Health check failed: {exc}")
            return False
        return response.status_code == 200

    def ask(
        self,
        query: str,
        *,
        top_k: int = 5,
        use_cache: bool = True,
        filter_dict: dict[str, Any] | None = None,
    ) -> QueryResponse:
        """Run the full RAG pipeline: retrieval plus a generated answer.

        Args:
            filter_dict: Metadata filters (``tags``, ``severity``, ...). Sent
                only when non-empty, so callers that never filter produce
                exactly the request body they always did.
        """
        payload: dict[str, Any] = {
            "query": query,
            "top_k": top_k,
            "use_cache": use_cache,
        }
        if filter_dict:
            payload["filter_dict"] = filter_dict
        response = self._request(
            "POST",
            f"{_API_PREFIX}/query/ask",
            json=payload,
            timeout=self._long_timeout(),
        )
        return self._parse(response, QueryResponse)

    def search(
        self,
        query: str,
        *,
        top_k: int = 5,
        filter_dict: dict[str, Any] | None = None,
    ) -> SearchResponse:
        """Retrieval only — no LLM in the path, so this is the fast check."""
        payload: dict[str, Any] = {"query": query, "top_k": top_k}
        if filter_dict:
            payload["filter_dict"] = filter_dict
        response = self._request("POST", f"{_API_PREFIX}/query/search", json=payload)
        return self._parse(response, SearchResponse)

    def list_documents(
        self,
        *,
        offset: int = 0,
        limit: int = 50,
        status: str | None = None,
        file_type: str | None = None,
    ) -> DocumentListResponse:
        """One page of documents, newest first.

        The endpoint orders by ``created_at`` descending, so no client-side
        sorting is needed. Only filters that are actually set go on the query
        string; the server treats an absent filter as "match everything".
        """
        params: dict[str, str | int] = {"offset": offset, "limit": limit}
        if status:
            params["status"] = status
        if file_type:
            params["file_type"] = file_type
        response = self._request("GET", f"{_API_PREFIX}/documents", params=params)
        return self._parse(response, DocumentListResponse)

    def recent_documents(self, *, limit: int = 10) -> DocumentListResponse:
        """Most recently created documents."""
        return self.list_documents(offset=0, limit=limit)

    def get_document(self, document_id: str) -> DocumentDetailResponse:
        """Full detail for one document.

        Raises:
            RequestRejected: The id is blank, or the server has no such
                document (404).
        """
        cleaned = document_id.strip()
        if not cleaned:
            raise RequestRejected("A document id is required.")
        # safe="" also encodes "/" so an id can never add path segments or a
        # query string to the request.
        response = self._request(
            "GET", f"{_API_PREFIX}/documents/{quote(cleaned, safe='')}"
        )
        return self._parse(response, DocumentDetailResponse)

    def upload(self, path: Path, *, auto_tag: bool = True) -> IngestResultResponse:
        """Upload a local file and ingest it.

        Preflight checks run before any bytes leave the machine, so an
        obviously doomed upload costs nothing and reports immediately.
        """
        self._preflight_upload(path)
        try:
            handle = path.open("rb")
        except OSError as exc:
            # TOCTOU: _preflight_upload's is_file() check and this open() are
            # not atomic - the file can vanish or lose permissions in between.
            raise RequestRejected(
                f"Could not read {path.name}: {exc.strerror}"
            ) from exc
        with handle:
            response = self._request(
                "POST",
                f"{_API_PREFIX}/ingest/upload",
                files={"file": (path.name, handle, "application/octet-stream")},
                data={"auto_tag": "true" if auto_tag else "false"},
                timeout=self._long_timeout(),
            )
        return self._parse(response, IngestResultResponse)

    # -- internals ---------------------------------------------------------

    def _long_timeout(self) -> httpx.Timeout:
        """Timeout for slow endpoints (LLM generation, document parsing).

        A bare float widens every phase - including connect - to the long
        read timeout, which would make the client hang for minutes trying to
        reach a dead server instead of failing fast.  Only read/write/pool
        should be long; connect stays governed by connect_timeout.
        """
        return httpx.Timeout(
            self.config.long_read_timeout, connect=self.config.connect_timeout
        )

    def _preflight_upload(self, path: Path) -> None:
        """Reject a file the server is certain to refuse."""
        if not path.is_file():
            raise RequestRejected(f"Not a file: {path}")
        suffix = path.suffix.lower()
        if suffix not in SUPPORTED_EXTENSIONS:
            raise RequestRejected(
                f"Unsupported file type: {suffix or '(no extension)'}"
            )
        size = path.stat().st_size
        if size > self.config.max_upload_bytes:
            limit_mb = self.config.max_upload_bytes / (1024 * 1024)
            raise RequestRejected(
                f"{path.name} exceeds the {limit_mb:.0f} MB upload limit"
            )

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Perform a request, translating transport failures and bad statuses."""
        try:
            response = self._http.request(method, url, **kwargs)
        except httpx.TimeoutException as exc:
            logger.warning(f"{method} {url} timed out: {exc}")
            raise TimedOut(
                "The request timed out. The API may be busy generating an answer."
            ) from exc
        except httpx.HTTPError as exc:
            logger.warning(f"{method} {url} failed to connect: {exc}")
            raise ConnectionFailed(
                f"Cannot reach the Grimoire API at {self.config.base_url} "
                "- is the stack running?"
            ) from exc

        if response.status_code >= 400:
            raise self._error_for(response)
        return response

    def _error_for(self, response: httpx.Response) -> GuiError:
        """Map an error response to the matching GuiError."""
        status = response.status_code
        detail = self._detail(response)

        if status == 401:
            return AuthFailed("API key rejected. Check GRIMOIRE_API_KEY.")
        if status == 403:
            return RequestRejected(
                f"This API key is not permitted to do that: {detail}"
            )
        if status == 404:
            return RequestRejected(f"Not found: {detail}")
        if status == 429:
            retry_after = self._retry_after(response)
            suffix = f" Retry in {retry_after}s." if retry_after else ""
            return RateLimited(f"Rate limited.{suffix}", retry_after=retry_after)
        if status >= 500:
            logger.error(f"API returned {status}: {response.text[:500]}")
            return ServerError("Grimoire API error - check the API logs.")
        return RequestRejected(detail)

    @staticmethod
    def _retry_after(response: httpx.Response) -> int | None:
        raw = response.headers.get("Retry-After")
        if raw is None:
            return None
        try:
            return int(float(raw))
        except ValueError:
            return None

    @staticmethod
    def _detail(response: httpx.Response) -> str:
        """Flatten FastAPI's error body to one line.

        422 bodies are a list of per-field objects; anything multi-line would
        break the single-line error labels in the UI.
        """
        try:
            body = response.json()
        except ValueError:
            return response.text.strip()[:200] or f"HTTP {response.status_code}"

        detail = body.get("detail") if isinstance(body, dict) else None
        if isinstance(detail, str):
            return detail
        if isinstance(detail, list):
            parts = []
            for item in detail:
                if not isinstance(item, dict):
                    continue
                location = ".".join(str(part) for part in item.get("loc", [])[1:])
                parts.append(f"{location}: {item.get('msg', '')}".strip(": "))
            if parts:
                return "; ".join(parts)
        return f"HTTP {response.status_code}"

    @staticmethod
    def _parse(response: httpx.Response, model: type[ModelT]) -> ModelT:
        """Validate a successful response against the server's own schema.

        The explicit "shares no fields" check below is only load-bearing
        for DocumentListResponse: it is the one model among this client's
        four whose fields are all optional, so it is the one a body sharing
        none of its field names would otherwise validate silently into an
        all-defaults instance for, instead of failing loudly. (The other
        three - QueryResponse, SearchResponse, IngestResultResponse - each
        have at least one required field and pydantic would already reject
        such a body on its own.) The guard is cheap and harmless for all
        four, so it stays here rather than being special-cased to one.
        """
        try:
            body = response.json()
        except ValueError as exc:  # json.JSONDecodeError subclasses ValueError
            logger.error(f"Could not decode JSON from {response.url}: {exc}")
            raise MalformedResponse(
                "Unexpected response from the API. It may be a different version."
            ) from exc

        if (
            isinstance(body, dict)
            and body
            and not set(model.model_fields) & body.keys()
        ):
            logger.error(
                f"Response from {response.url} shares no fields with "
                f"{model.__name__}: {sorted(body)}"
            )
            raise MalformedResponse(
                "Unexpected response from the API. It may be a different version."
            )

        try:
            return model.model_validate(body)
        except ValueError as exc:  # ValidationError subclasses ValueError
            logger.error(f"Could not parse {model.__name__} from {response.url}: {exc}")
            raise MalformedResponse(
                "Unexpected response from the API. It may be a different version."
            ) from exc
