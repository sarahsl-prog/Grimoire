"""Category management API routes."""

from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from grimoire.api.dependencies import get_db_session
from grimoire.api.schemas import (
    CategoryCreateRequest,
    CategoryListResponse,
    CategoryResponse,
)
from grimoire.db.models import Category

router = APIRouter(prefix="/categories", tags=["categories"])


@router.get("", response_model=CategoryListResponse)
async def list_categories(
    db: AsyncSession = Depends(get_db_session),
) -> CategoryListResponse:
    """List all categories."""
    result = await db.execute(select(Category).order_by(Category.name))
    cats = result.scalars().all()

    total = (await db.execute(select(func.count(Category.id)))).scalar() or 0

    return CategoryListResponse(
        categories=[
            CategoryResponse(
                id=cat.id,
                name=cat.name,
                slug=cat.slug,
                description=cat.description or "",
                parent_id=cat.parent_id,
                color=cat.color or "#3498db",
            )
            for cat in cats
        ],
        total=total,
    )


@router.post("", response_model=CategoryResponse, status_code=201)
async def create_category(
    request: CategoryCreateRequest,
    db: AsyncSession = Depends(get_db_session),
) -> CategoryResponse:
    """Create a new category."""
    slug = request.name.lower().replace(" ", "-")

    parent_id = None
    if request.parent_slug:
        parent = (
            await db.execute(select(Category).where(Category.slug == request.parent_slug))
        ).scalars().first()
        if not parent:
            raise HTTPException(status_code=404, detail=f"Parent category '{request.parent_slug}' not found")
        parent_id = parent.id

    cat = Category(
        id=str(uuid4()),
        name=request.name,
        slug=slug,
        description=request.description,
        parent_id=parent_id,
        color=request.color,
    )
    db.add(cat)
    await db.commit()

    return CategoryResponse(
        id=cat.id,
        name=cat.name,
        slug=cat.slug,
        description=cat.description or "",
        parent_id=cat.parent_id,
        color=cat.color or "#3498db",
    )


@router.delete("/{category_id}", status_code=204)
async def delete_category(
    category_id: str,
    db: AsyncSession = Depends(get_db_session),
) -> None:
    """Delete a category."""
    cat = await db.get(Category, category_id)
    if not cat:
        raise HTTPException(status_code=404, detail=f"Category {category_id} not found")

    await db.delete(cat)
    await db.commit()
