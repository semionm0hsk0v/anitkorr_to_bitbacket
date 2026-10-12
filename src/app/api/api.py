from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware

import src.app.warnings_config  # noqa: F401
from src.agents.llm.client import client_lifespan
from src.agents.llm.config import validate_llm_config
from src.app.api.exception_handlers import exception_response
from src.app.api.routers.health import router as health_router
from src.app.api.routers.metrics import router as metrics_router
from src.app.configs import info
from src.app.mcp import mcp_app
from src.app.version import __version__
from src.common.config.config import settings
from src.common.config.required import check_required_settings
from src.common.observability import METRICS_PATH, PrometheusMiddleware
from src.common.observability.metrics import BUILD_INFO
from src.common.utils.logger import configure_logging
from src.infrastructure.llm.context import RequestIdentityMiddleware
from src.infrastructure.mcp_servers.lifecycle import init_mcp_if_enabled
from src.infrastructure.prompts.prompt_kit import prompt_lifespan

configure_logging()


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(api_app: FastAPI):

        check_required_settings()
        validate_llm_config()
        await init_mcp_if_enabled()

        async with AsyncExitStack() as stack:
            await stack.enter_async_context(client_lifespan())
            await stack.enter_async_context(prompt_lifespan())
            await stack.enter_async_context(mcp_app.lifespan(api_app))
            yield

    app = FastAPI(
        title="Antikorr Expertise API",
        description=info.description,
        version=info.api_version,
        openapi_tags=info.tags_metadata,
        root_path=settings["API_ROOT"],
        debug=False,
        lifespan=lifespan,
    )

    app.include_router(health_router, tags=["Health"])
    # Скрейп ServiceMonitor из ci_cd/values*.yaml; в OpenAPI не показывается.
    app.include_router(metrics_router, tags=["Metrics"])

    app.mount("/mcp", mcp_app)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.add_middleware(PrometheusMiddleware, routes=app.routes, skip_paths=(METRICS_PATH,))
    app.add_middleware(RequestIdentityMiddleware)
    BUILD_INFO.info({"version": __version__})

    app.add_exception_handler(Exception, exception_response)

    return app


app = create_app()
