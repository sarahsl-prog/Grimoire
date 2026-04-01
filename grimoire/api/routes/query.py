"""Query and search API routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from grimoire.api.dependencies import get_db_session, get_query_agent
from grimoire.api.schemas import (
    QueryRequest,
    QueryResponse,
    SearchRequest,
    SearchResponse,
    SearchResultItem,
)

router = APIRouter(prefix="/query", tags=["query"])


@router.post("/ask", response_model=QueryResponse)
async def ask_question(
    request: QueryRequest,
    db: AsyncSession = Depends(get_db_session),
) -> QueryResponse:
    """Ask a question and get an AI-generated answer with citations."""
    agent = get_query_agent()
    result = await agent.query(
        db,
        request.query,
        top_k=request.top_k,
        filter_dict=request.filter_dict,
        use_cache=request.use_cache,
    )
    return QueryResponse(**result.model_dump())


@router.post("/search", response_model=SearchResponse)
async def search_documents(
    request: SearchRequest,
    db: AsyncSession = Depends(get_db_session),
) -> SearchResponse:
    """Search for documents without generating an answer."""
    agent = get_query_agent()
    result = await agent.search(
        db, request.query, top_k=request.top_k, filter_dict=request.filter_dict,
    )
    items = [SearchResultItem(**r) if isinstance(r, dict) else r for r in result.results]
    return SearchResponse(
        query=result.query,
        results=items,
        total_results=result.total_results,
        duration_ms=result.duration_ms,
    )
