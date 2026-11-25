import asyncio
from contextlib import asynccontextmanager, suppress
import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
import httpx

from . import accounts, assets, model_api, upload_registration, uploads, usage
from .config import Settings
from .db import Database
from .upload_storage import LocalUploadStorage, UploadStorage


ARK_ORIGIN = "https://ark.cn-beijing.volces.com"
logger = logging.getLogger(__name__)


async def poll_videos(app: FastAPI, seconds: int) -> None:
    while True:
        await asyncio.sleep(seconds)
        try:
            await model_api.poll_unfinished(app)
        except Exception:
            logger.error("Video polling pass failed")


def create_app(
    settings: Settings | None = None,
    *,
    ark_transport: httpx.AsyncBaseTransport | None = None,
    ark_asset_transport: httpx.AsyncBaseTransport | None = None,
    upload_storage: UploadStorage | None = None,
) -> FastAPI:
    settings = settings or Settings()
    database = Database(settings.database_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings.database_path.parent.mkdir(parents=True, exist_ok=True)
        with database.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            accounts.initialize(connection)
            assets.initialize(connection)
            model_api.initialize(connection)
            uploads.initialize(connection)
            upload_registration.initialize(connection)
        async with httpx.AsyncClient(
            base_url=ARK_ORIGIN,
            transport=ark_transport,
            timeout=httpx.Timeout(20, connect=5),
            follow_redirects=False,
            trust_env=False,
        ) as ark, httpx.AsyncClient(
            base_url=assets.ARK_ASSET_ORIGIN,
            transport=ark_asset_transport,
            timeout=httpx.Timeout(20, connect=5),
            follow_redirects=False,
            trust_env=False,
        ) as ark_assets:
            app.state.ark = ark
            app.state.ark_assets = ark_assets
            upload_directory = settings.upload_directory or (settings.database_path.parent / "asset-uploads")
            app.state.upload_storage = upload_storage if upload_storage is not None else LocalUploadStorage(upload_directory)
            background_tasks = []
            try:
                if settings.video_poll_seconds and settings.ark_api_key and settings.ark_api_key.get_secret_value():
                    background_tasks.append(asyncio.create_task(poll_videos(app, settings.video_poll_seconds)))
                if settings.upload_cleanup_seconds:
                    background_tasks.append(asyncio.create_task(uploads.poll_cleanup(app, settings.upload_cleanup_seconds)))
                yield
            finally:
                for task in background_tasks:
                    task.cancel()
                for task in background_tasks:
                    with suppress(asyncio.CancelledError):
                        await task

    app = FastAPI(title="Richi Proxy", version="0.1.0", lifespan=lifespan)
    upload_registration.redact_source_access_log()
    app.state.settings = settings
    app.state.db = database

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_: Request, __: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content={"detail": {"code": "invalid_request", "message": "请求参数无效"}},
        )

    @app.middleware("http")
    async def protect_customer_response(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith(("/api/customer/", "/api/v3/", "/api/asset/", "/api/asset-group/")):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(accounts.router)
    app.include_router(assets.router)
    app.include_router(uploads.router)
    app.include_router(upload_registration.router)
    app.include_router(model_api.router)
    app.include_router(usage.router)
    return app


app = create_app()
