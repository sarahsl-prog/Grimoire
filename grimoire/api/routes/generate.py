"""Content generation API routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from grimoire.api.dependencies import get_content_gen_agent, get_db_session
from grimoire.api.schemas import GenerateRequest, GenerateResponse
from grimoire.db.models import ContentType

router = APIRouter(prefix="/generate", tags=["generate"])


@router.post("", response_model=GenerateResponse)
async def generate_content(
    request: GenerateRequest,
    db: AsyncSession = Depends(get_db_session),
) -> GenerateResponse:
    """Generate content from documents.

    Supported content_type values: summary, flash_card, cliff_notes, outline, extract.
    """
    agent = get_content_gen_agent()

    try:
        ct = ContentType(request.content_type)
    except ValueError:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=f"Invalid content_type: {request.content_type}")

    if ct == ContentType.SUMMARY:
        result = await agent.generate_summary(db, request.document_ids, style=request.style or "concise")
    elif ct == ContentType.FLASH_CARD:
        result = await agent.generate_flash_cards(db, request.document_ids, count=request.count)
    elif ct == ContentType.CLIFF_NOTES:
        result = await agent.generate_cliff_notes(db, request.document_ids)
    elif ct == ContentType.OUTLINE:
        result = await agent.generate_outline(db, request.document_ids)
    elif ct == ContentType.EXTRACT:
        if not request.query:
            from fastapi import HTTPException
            raise HTTPException(status_code=400, detail="'query' is required for extract generation.")
        result = await agent.generate_extract(db, request.document_ids, query=request.query)
    else:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=f"Unsupported content type: {request.content_type}")

    return GenerateResponse(**result.model_dump())
