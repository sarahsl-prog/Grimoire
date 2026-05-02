"""Category management API routes."""

from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from grimoire.api.auth import get_api_key
from grimoire.api.dependencies import get_db_session
from grimoire.api.schemas import (
    CategoryCreateRequest,
    CategoryListResponse,
    CategoryResponse,
)
from grimoire.db.models import ApiKey, Category

router = APIRouter(prefix="/categories", tags=["categories"])


@router.get("", response_model=CategoryListResponse)
async def list_categories(
    request: Request,
    api_key: ApiKey = Depends(get_api_key),
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
    request: Request,
    body: CategoryCreateRequest,
    api_key: ApiKey = Depends(get_api_key),
    db: AsyncSession = Depends(get_db_session),
) -> CategoryResponse:
    """Create a new category."""
    from slugify import slugify
    
    slug = slugify(body.name)
    
    # Check for existing slug and handle collision
    existing = (
        await db.execute(select(Category).where(Category.slug == slug))
    ).scalars().first()
    if existing:
        counter = 1
        new_slug = f"{slug}-{counter}"
        while (
            await db.execute(select(Category).where(Category.slug == new_slug))
        ).scalars().first():
            counter += 1
            new_slug = f"{slug}-{counter}"
        slug = new_slug

    parent_id = None
    if body.parent_slug:
        parent = (
            await db.execute(select(Category).where(Category.slug == body.parent_slug))
        ).scalars().first()
        if not parent:
            raise HTTPException(status_code=404, detail=f"Parent category '{body.parent_slug}' not found")
        parent_id = parent.id

    cat = Category(
        id=str(uuid4()),
        name=body.name,
        slug=slug,
        description=body.description,
        parent_id=parent_id,
        color=body.color,
    )
    db.add(cat)
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise HTTPException(status_code=500, detail="Failed to create category")
    await db.refresh(cat)

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
    request: Request,
    api_key: ApiKey = Depends(get_api_key),
    db: AsyncSession = Depends(get_db_session),
) -> None:
    """Delete a category."""
    cat = await db.get(Category, category_id)
    if not cat:
        raise HTTPException(status_code=404, detail=f"Category {category_id} not found")

    await db.delete(cat)
    await db.commit()