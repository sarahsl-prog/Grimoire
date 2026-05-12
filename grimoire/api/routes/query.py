"""Query and search API routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from grimoire.api.auth import get_api_key
from grimoire.api.dependencies import get_db_session, get_query_agent
from grimoire.api.schemas import (
    QueryRequest,
    QueryResponse,
    SearchRequest,
    SearchResponse,
    SearchResultItem,
    warn_unknown_filter_keys,
)
from grimoire.db.models import ApiKey

router = APIRouter(prefix="/query", tags=["query"])


def _merge_security_filters(
    filter_dict: dict[str, Any] | None,
    *,
    severity: str | None,
    mitre_tactic: str | None,
    mitre_technique_id: str | None,
    source_type: str | None,
    cve_id: str | None,
    content_date_after: str | None,
    platforms: str | None,
) -> dict[str, Any] | None:
    """Fold the dedicated security query params into ``filter_dict``.

    Body-level ``filter_dict`` takes precedence over the query-string
    shortcuts so a client can still pass arbitrary filters in JSON while
    keeping the simple ``?severity=high`` ergonomics for common cases.
    """
    extras: dict[str, Any] = {}
    if severity is not None:
        extras["severity"] = severity
    if mitre_tactic is not None:
        extras["mitre_tactic"] = mitre_tactic
    if mitre_technique_id is not None:
        extras["mitre_technique_id"] = mitre_technique_id
    if source_type is not None:
        extras["source_type"] = source_type
    if cve_id is not None:
        extras["cve_id"] = cve_id
    if content_date_after is not None:
        extras["content_date_after"] = content_date_after
    if platforms is not None:
        # Comma-separated list → list[str] for downstream consumers.
        extras["platforms"] = [p.strip() for p in platforms.split(",") if p.strip()]

    if not extras and not filter_dict:
        return None
    merged: dict[str, Any] = dict(extras)
    if filter_dict:
        merged.update(filter_dict)
    warn_unknown_filter_keys(merged)
    return merged


@router.post("/ask", response_model=QueryResponse)
async def ask_question(
    request: Request,
    body: QueryRequest,
    severity: str | None = Query(
        default=None, description="Filter by severity (security domain)."
    ),
    mitre_tactic: str | None = Query(
        default=None, alias="tactic", description="Filter by MITRE tactic."
    ),
    mitre_technique_id: str | None = Query(
        default=None, alias="technique", description="Filter by MITRE technique ID."
    ),
    source_type: str | None = Query(
        default=None,
        description="Filter by source type (sigma_rule, nvd_cve, mitre_attack, prose).",
    ),
    cve_id: str | None = Query(default=None, description="Filter by CVE ID."),
    content_date_after: str | None = Query(
        default=None, description="ISO-8601 date — only content on or after this date."
    ),
    platforms: str | None = Query(
        default=None,
        description="Comma-separated list of platforms (windows, linux, …).",
    ),
    api_key: ApiKey = Depends(get_api_key),
    db: AsyncSession = Depends(get_db_session),
) -> QueryResponse:
    """Ask a question and get an AI-generated answer with citations.

    Security filters are accepted as query parameters and merged into the
    request's ``filter_dict``. The body's ``filter_dict`` takes precedence
    on key conflicts. ``tactic`` and ``technique`` are short aliases for
    the ``mitre_tactic`` / ``mitre_technique_id`` filter keys.
    """
    agent = get_query_agent()
    filter_dict = _merge_security_filters(
        body.filter_dict,
        severity=severity,
        mitre_tactic=mitre_tactic,
        mitre_technique_id=mitre_technique_id,
        source_type=source_type,
        cve_id=cve_id,
        content_date_after=content_date_after,
        platforms=platforms,
    )
    result = await agent.query(
        db,
        body.query,
        top_k=body.top_k,
        filter_dict=filter_dict,
        use_cache=body.use_cache,
    )
    return QueryResponse(**result.model_dump())


@router.post("/search", response_model=SearchResponse)
async def search_documents(
    request: Request,
    body: SearchRequest,
    severity: str | None = Query(
        default=None, description="Filter by severity (security domain)."
    ),
    mitre_tactic: str | None = Query(
        default=None, alias="tactic", description="Filter by MITRE tactic."
    ),
    mitre_technique_id: str | None = Query(
        default=None, alias="technique", description="Filter by MITRE technique ID."
    ),
    source_type: str | None = Query(default=None, description="Filter by source type."),
    cve_id: str | None = Query(default=None, description="Filter by CVE ID."),
    content_date_after: str | None = Query(
        default=None, description="ISO-8601 date — only content on or after this date."
    ),
    platforms: str | None = Query(
        default=None, description="Comma-separated list of platforms."
    ),
    api_key: ApiKey = Depends(get_api_key),
    db: AsyncSession = Depends(get_db_session),
) -> SearchResponse:
    """Search for documents without generating an answer.

    Accepts the same security-filter query params as ``/ask``.
    """
    agent = get_query_agent()
    filter_dict = _merge_security_filters(
        body.filter_dict,
        severity=severity,
        mitre_tactic=mitre_tactic,
        mitre_technique_id=mitre_technique_id,
        source_type=source_type,
        cve_id=cve_id,
        content_date_after=content_date_after,
        platforms=platforms,
    )
    result = await agent.search(
        db,
        body.query,
        top_k=body.top_k,
        filter_dict=filter_dict,
    )
    items = [
        SearchResultItem(**r) if isinstance(r, dict) else r for r in result.results
    ]
    return SearchResponse(
        query=result.query,
        results=items,
        total_results=result.total_results,
        duration_ms=result.duration_ms,
    )
