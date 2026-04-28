"""HTTP surface for project scoping.

  GET    /projects                 list every project + which one is active
  POST   /projects                 create a new project (slug + name + description)
  PUT    /projects/active          switch the active project

The active-project state survives daemon restarts via a flat state
file written by :class:`core.projects.ProjectsManager`. Switching is
cheap — it just rewrites the state file and the next master run picks
it up.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from core.logging import get_logger
from core.projects import ProjectsManager

log = get_logger("pilkd.projects")

router = APIRouter(prefix="/projects")


class CreateBody(BaseModel):
    slug: str = Field(
        min_length=1,
        max_length=64,
        description=(
            "URL-safe identifier — lowercase letters, digits, hyphens. "
            "Used in folder paths so keep it short and stable."
        ),
    )
    name: str = Field(
        min_length=1,
        max_length=120,
        description="Human-readable project name shown in the UI.",
    )
    description: str = Field(
        default="",
        max_length=8000,
        description=(
            "Free-form prompt describing the project — voice, audience, "
            "goals, anything PILK should know before working in it. "
            "All masters read this before each task in this project."
        ),
    )


class SetActiveBody(BaseModel):
    slug: str = Field(min_length=1, max_length=64)


def _manager(request: Request) -> ProjectsManager:
    mgr = getattr(request.app.state, "projects", None)
    if mgr is None:
        raise HTTPException(status_code=503, detail="projects manager offline")
    return mgr


@router.get("")
async def list_projects(request: Request) -> dict:
    mgr = _manager(request)
    items = mgr.list()
    return {
        "active": mgr.active_slug,
        "projects": [
            {
                "slug": p.slug,
                "name": p.name,
                "description": p.description,
                "is_active": p.is_active,
            }
            for p in items
        ],
    }


@router.post("")
async def create_project(body: CreateBody, request: Request) -> dict:
    mgr = _manager(request)
    try:
        info = mgr.create(
            slug=body.slug, name=body.name, description=body.description,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    log.info("project_created", slug=info.slug, name=info.name)
    return {
        "slug": info.slug,
        "name": info.name,
        "description": info.description,
        "is_active": info.is_active,
    }


@router.put("/active")
async def set_active_project(body: SetActiveBody, request: Request) -> dict:
    mgr = _manager(request)
    try:
        slug = mgr.set_active(body.slug)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    log.info("project_active_changed", slug=slug)
    return {"active": slug}
