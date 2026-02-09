import os
from typing import List, Optional

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field, HttpUrl

from src.api.db import (
    create_link,
    create_pool,
    delete_link,
    get_analytics,
    get_link_by_slug,
    init_schema,
    list_links,
    record_click_and_get_destination,
)

openapi_tags = [
    {"name": "Health", "description": "Service health and basic diagnostics."},
    {"name": "Links", "description": "Create and manage short links."},
    {"name": "Redirect", "description": "Redirect short links to their destination and record clicks."},
    {"name": "Analytics", "description": "Basic per-link analytics (click count, last clicked timestamp)."},
]

app = FastAPI(
    title="LinkShortener Pro API",
    description=(
        "URL shortener backend: create short links, redirect with click tracking, "
        "and manage links for a simple dashboard."
    ),
    version="1.0.0",
    openapi_tags=openapi_tags,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, restrict this to your frontend origin(s).
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_pool = create_pool()


class ErrorResponse(BaseModel):
    """Standard error response payload."""
    detail: str = Field(..., description="Human readable error message")


class LinkCreateRequest(BaseModel):
    """Payload for creating a short link."""
    long_url: HttpUrl = Field(..., description="Original (long) URL to shorten.")
    custom_slug: Optional[str] = Field(
        default=None,
        description="Optional custom slug (3-64 chars; letters, numbers, '_' or '-').",
        examples=["my-campaign", "Promo2026"],
    )


class LinkUpdateRequest(BaseModel):
    """Payload for updating a link destination."""
    long_url: HttpUrl = Field(..., description="New destination URL for the given short link.")


class LinkResponse(BaseModel):
    """Link record returned by the API."""
    id: int = Field(..., description="Database identifier.")
    slug: str = Field(..., description="Short slug used in the short link.")
    long_url: HttpUrl = Field(..., description="Destination URL.")
    short_url: str = Field(..., description="Computed short URL that can be shared.")
    created_at: str = Field(..., description="ISO timestamp when the link was created.")
    updated_at: str = Field(..., description="ISO timestamp when the link was last updated.")
    click_count: int = Field(..., description="Total recorded clicks.")


class LinkListResponse(BaseModel):
    """Paginated list of links."""
    items: List[LinkResponse] = Field(..., description="List of links.")
    total: int = Field(..., description="Total number of links in the system.")
    limit: int = Field(..., description="Page size.")
    offset: int = Field(..., description="Page offset.")


class AnalyticsResponse(BaseModel):
    """Basic analytics response."""
    slug: str = Field(..., description="Slug for the link.")
    click_count: int = Field(..., description="Total recorded clicks.")
    last_clicked_at: Optional[str] = Field(None, description="ISO timestamp of most recent click, if any.")


def _short_base_url_from_request(request: Request) -> str:
    """
    Derive base URL for building short links.

    Priority:
      1) SHORTENER_BASE_URL env var (recommended for correct external URL)
      2) request.base_url (backend origin)
    """
    configured = os.getenv("SHORTENER_BASE_URL")
    if configured:
        return configured.rstrip("/")
    return str(request.base_url).rstrip("/")


def _to_link_response(request: Request, row: dict) -> LinkResponse:
    base = _short_base_url_from_request(request)
    return LinkResponse(
        id=int(row["id"]),
        slug=row["slug"],
        long_url=row["long_url"],
        short_url=f"{base}/r/{row['slug']}",
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        click_count=int(row["click_count"]),
    )


@app.on_event("startup")
def _on_startup() -> None:
    """Initialize database schema and open the connection pool."""
    _pool.open()
    init_schema(_pool)


@app.on_event("shutdown")
def _on_shutdown() -> None:
    """Close the database connection pool."""
    _pool.close()


@app.get(
    "/health",
    tags=["Health"],
    summary="Health check",
    description="Returns a simple payload indicating the service is healthy.",
)
def health_check():
    """Health check endpoint.

    Returns:
        dict: { "message": "Healthy" }
    """
    return {"message": "Healthy"}


@app.post(
    "/api/links",
    tags=["Links"],
    summary="Create a short link",
    description="Create a new short link for a given long URL (optionally with a custom slug).",
    response_model=LinkResponse,
    responses={400: {"model": ErrorResponse}},
)
def api_create_link(payload: LinkCreateRequest, request: Request):
    """Create a short link."""
    try:
        row = create_link(_pool, str(payload.long_url), payload.custom_slug)
        return _to_link_response(request, row)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        # Typically unique constraint violations for custom_slug.
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.get(
    "/api/links",
    tags=["Links"],
    summary="List links",
    description="List existing short links (paginated).",
    response_model=LinkListResponse,
)
def api_list_links(
    request: Request,
    limit: int = Query(50, ge=1, le=200, description="Maximum number of links to return."),
    offset: int = Query(0, ge=0, description="Offset into the link list."),
):
    """List links with pagination."""
    rows, total = list_links(_pool, limit=limit, offset=offset)
    items = [_to_link_response(request, r) for r in rows]
    return LinkListResponse(items=items, total=total, limit=limit, offset=offset)


@app.get(
    "/api/links/{slug}",
    tags=["Links"],
    summary="Get link details",
    description="Fetch a single link by slug.",
    response_model=LinkResponse,
    responses={404: {"model": ErrorResponse}},
)
def api_get_link(slug: str, request: Request):
    """Get a link by slug."""
    row = get_link_by_slug(_pool, slug)
    if not row:
        raise HTTPException(status_code=404, detail="Link not found")
    return _to_link_response(request, row)


@app.put(
    "/api/links/{slug}",
    tags=["Links"],
    summary="Update link destination",
    description="Update the destination URL for a given slug (slug remains unchanged).",
    response_model=LinkResponse,
    responses={404: {"model": ErrorResponse}},
)
def api_update_link(slug: str, payload: LinkUpdateRequest, request: Request):
    """Update a link destination URL."""
    row = update_link(_pool, slug, str(payload.long_url))
    if not row:
        raise HTTPException(status_code=404, detail="Link not found")
    return _to_link_response(request, row)


@app.delete(
    "/api/links/{slug}",
    tags=["Links"],
    summary="Delete link",
    description="Delete a link by slug (also deletes its click event history).",
    responses={200: {"content": {"application/json": {"schema": {"type": "object"}}}}, 404: {"model": ErrorResponse}},
)
def api_delete_link(slug: str):
    """Delete a link by slug."""
    deleted = delete_link(_pool, slug)
    if not deleted:
        raise HTTPException(status_code=404, detail="Link not found")
    return {"deleted": True}


@app.get(
    "/api/links/{slug}/analytics",
    tags=["Analytics"],
    summary="Get link analytics",
    description="Return basic analytics for a link: click count and last clicked timestamp.",
    response_model=AnalyticsResponse,
    responses={404: {"model": ErrorResponse}},
)
def api_link_analytics(slug: str):
    """Get analytics for a link."""
    analytics = get_analytics(_pool, slug)
    if not analytics:
        raise HTTPException(status_code=404, detail="Link not found")
    return AnalyticsResponse(
        slug=slug,
        click_count=int(analytics["click_count"]),
        last_clicked_at=str(analytics["last_clicked_at"]) if analytics["last_clicked_at"] else None,
    )


@app.get(
    "/r/{slug}",
    tags=["Redirect"],
    summary="Redirect short link",
    description="Redirect to the destination URL and record a click event.",
    responses={307: {"description": "Redirect"}, 404: {"model": ErrorResponse}},
)
def redirect_slug(
    slug: str,
    request: Request,
    user_agent: Optional[str] = Header(default=None, alias="User-Agent"),
    referer: Optional[str] = Header(default=None, alias="Referer"),
):
    """Redirect endpoint used by shared short URLs."""
    ip = request.client.host if request.client else None
    destination = record_click_and_get_destination(_pool, slug, user_agent, referer, ip)
    if not destination:
        raise HTTPException(status_code=404, detail="Link not found")
    return RedirectResponse(url=destination, status_code=307)
