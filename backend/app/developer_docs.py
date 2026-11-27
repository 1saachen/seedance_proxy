from functools import lru_cache
from importlib.resources import files

from fastapi import APIRouter, Request
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse
from fastapi.routing import APIRoute

from . import asset_liveness, asset_presets, assets, general_models, model_api, uploads, video_task_management
from .accounts import SeedanceCustomer


router = APIRouter(include_in_schema=False)
PUBLIC_MODEL_PATHS = {"/v1/models", "/v1/chat/completions", "/v1/images/generations"}
VIDEO_PATH_PREFIXES = ("/api/v3/contents/generations/tasks", "/api/asset/", "/api/asset-group/")


@lru_cache(maxsize=1)
def _video_manual() -> str:
    return files("app").joinpath("manual", "video.html").read_text(encoding="utf-8")


@router.get("/api/customer/docs/video", response_class=HTMLResponse)
def video_manual(_: SeedanceCustomer) -> HTMLResponse:
    return HTMLResponse(_video_manual(), headers={
        "Content-Security-Policy": "default-src 'none'; style-src 'self' 'unsafe-inline'; script-src 'self'; "
                                   "base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
        "Referrer-Policy": "no-referrer",
    })


def _schema(request: Request, *, video: bool) -> dict:
    routers = [model_api.router, general_models.router]
    if video:
        routers.extend((assets.router, asset_liveness.router, asset_presets.router, uploads.router, video_task_management.router))
    routes = [
        route for source in routers for route in source.routes
        if isinstance(route, APIRoute) and (
            route.path in PUBLIC_MODEL_PATHS or (video and route.path.startswith(VIDEO_PATH_PREFIXES))
        )
    ]
    return get_openapi(title=request.app.title, version=request.app.version, routes=routes)


@router.get("/openapi.json")
def public_openapi(request: Request) -> dict:
    return _schema(request, video=False)


@router.get("/api/customer/docs/video/openapi.json")
def video_openapi(request: Request, _: SeedanceCustomer) -> dict:
    return _schema(request, video=True)
