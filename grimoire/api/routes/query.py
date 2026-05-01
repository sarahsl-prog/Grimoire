"""Query and search API routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from grimoire.api.auth import get_api_key
from grimoire.api.dependencies import get_db_session, get_query_agent
from grimoire.api.schemas import (
    QueryRequest,
    QueryResponse,
    SearchRequest,
    SearchResponse,
    SearchResultItem,
)
from grimoire.db.models import ApiKey

router = APIRouter(prefix="/query", tags=["query"])


@router.post("/ask", response_model=QueryResponse)
async def ask_question(
    request: Request,
    body: QueryRequest,
    api_key: ApiKey = Depends(get_api_key),
    db: AsyncSession = Depends(get_db_session),
) -> QueryResponse:
    """Ask a question and get an AI-generated answer with citations."""
    agent = get_query_agent()
    result = await agent.query(
        db,
        body.query,
        top_k=body.top_k,
        filter_dict=body.filter_dict,
        use_cache=body.use_cache,
    )
    return QueryResponse(**result.model_dump())


@router.post("/search", response_model=SearchResponse)
async def search_documents(
    request: Request,
    body: SearchRequest,
    api_key: ApiKey = Depends(get_api_key),
    db: AsyncSession = Depends(get_db_session),
) -> SearchResponse:
    """Search for documents without generating an answer."""
    agent = get_query_agent()
    result = await agent.search(
        db, body.query, top_k=body.top_k, filter_dict=body.filter_dict,
    )
    items = [SearchResultItem(**r) if isinstance(r, dict) else r for r in result.results]
    return SearchResponse(
        query=result.query,
        results=items,
        total_results=result.total_results,
        duration_ms=result.duration_ms,
    )