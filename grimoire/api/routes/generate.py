"""Content generation API routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from grimoire.api.auth import get_api_key
from grimoire.api.dependencies import get_content_gen_agent, get_db_session
from grimoire.api.schemas import GenerateRequest, GenerateResponse
from grimoire.db.models import ApiKey, ContentType

router = APIRouter(prefix="/generate", tags=["generate"])


@router.post("", response_model=GenerateResponse)
async def generate_content(
    request: Request,
    body: GenerateRequest,
    api_key: ApiKey = Depends(get_api_key),
    db: AsyncSession = Depends(get_db_session),
) -> GenerateResponse:
    """Generate content from documents.

    Supported content_type values: summary, flash_card, cliff_notes, outline, extract.
    """
    agent = get_content_gen_agent()

    try:
        ct = ContentType(body.content_type)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid content_type: {body.content_type}")

    if ct == ContentType.SUMMARY:
        result = await agent.generate_summary(db, body.document_ids, style=body.style or "concise")
    elif ct == ContentType.FLASH_CARD:
        result = await agent.generate_flash_cards(db, body.document_ids, count=body.count)
    elif ct == ContentType.CLIFF_NOTES:
        result = await agent.generate_cliff_notes(db, body.document_ids)
    elif ct == ContentType.OUTLINE:
        result = await agent.generate_outline(db, body.document_ids)
    elif ct == ContentType.EXTRACT:
        if not body.query:
            raise HTTPException(status_code=400, detail="'query' is required for extract generation.")
        result = await agent.generate_extract(db, body.document_ids, query=body.query)
    else:
        raise HTTPException(status_code=400, detail=f"Unsupported content type: {body.content_type}")

    return GenerateResponse(**result.model_dump())