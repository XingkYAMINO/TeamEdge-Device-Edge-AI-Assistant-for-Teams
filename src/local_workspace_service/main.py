from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .agent_models import (
    AgentTaskCreate,
    AgentTaskView,
    CloudEnableRequest,
    CloudEnableResponse,
    ConnectionTestResult,
    SourceRecord,
)
from .agent_service import AgentService
from .credential_store import CredentialStore, create_system_credential_store
from .document_service import DocumentError, DocumentService
from .errors import (
    CredentialStoreError,
    InvalidRootError,
    ModelConnectionConflictError,
    ModelConnectionNotFoundError,
    RootChangedError,
    ScanInProgressError,
    WorkspaceConflictError,
    WorkspaceNotFoundError,
)
from .model_connection_models import (
    ModelConnection,
    ModelConnectionCreate,
    ModelConnectionList,
    ModelConnectionPatch,
    _is_loopback,
)
from .model_connection_repository import ModelConnectionRepository
from .model_connection_service import ModelConnectionService
from .model_gateway import JsonTransport, ModelGateway, ModelGatewayError
from .models import (
    LatestScanResult,
    ScanResult,
    Workspace,
    WorkspaceCreate,
    WorkspacePolicy,
)
from .repository import WorkspaceRepository
from .service import WorkspaceService


def _default_database_path() -> Path:
    configured = os.environ.get("LOCAL_WORKSPACE_DB")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path.cwd() / "data" / "workspaces.db"


def create_app(
    database_path: str | Path | None = None,
    credential_store: CredentialStore | None = None,
    model_transport: JsonTransport | None = None,
) -> FastAPI:
    resolved_database = (
        Path(database_path).resolve() if database_path else _default_database_path()
    )
    repository = WorkspaceRepository(
        resolved_database
    )
    service = WorkspaceService(repository)
    model_repository = ModelConnectionRepository(resolved_database)
    model_service = ModelConnectionService(
        model_repository, credential_store or create_system_credential_store()
    )
    document_service = DocumentService(repository)
    agent_service = AgentService(
        model_service, document_service, ModelGateway(model_transport)
    )
    static_directory = Path(__file__).with_name("static")

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        repository.initialize()
        model_repository.initialize()
        yield

    app = FastAPI(
        title="Local Workspace Service",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.workspace_service = service
    app.state.model_connection_service = model_service
    app.state.document_service = document_service
    app.state.agent_service = agent_service
    app.mount("/static", StaticFiles(directory=static_directory), name="static")

    @app.middleware("http")
    async def protect_local_browser(request: Request, call_next):
        origin = request.headers.get("origin")
        fetch_site = request.headers.get("sec-fetch-site")
        if request.url.path.startswith("/api/") and (origin is not None or fetch_site is not None):
            local_origin = f"{request.url.scheme}://{request.url.netloc}"
            if (
                not _is_loopback(request.url.hostname or "")
                or (origin is not None and origin != local_origin)
                or fetch_site in {"cross-site", "same-site"}
            ):
                return JSONResponse(status_code=403, content={"detail": "请从本机服务页面访问此接口"})
        response = await call_next(request)
        response.headers["X-Frame-Options"] = "DENY"
        if request.url.path.startswith("/api/v1/agent/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(WorkspaceNotFoundError)
    async def not_found(_: Request, exc: WorkspaceNotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(ModelConnectionNotFoundError)
    async def model_not_found(
        _: Request, exc: ModelConnectionNotFoundError
    ) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(WorkspaceConflictError)
    async def conflict(_: Request, exc: WorkspaceConflictError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(ModelConnectionConflictError)
    async def model_conflict(
        _: Request, exc: ModelConnectionConflictError
    ) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(CredentialStoreError)
    async def credential_failure(
        _: Request, exc: CredentialStoreError
    ) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    @app.exception_handler(ModelGatewayError)
    async def model_gateway_failure(
        _: Request, exc: ModelGatewayError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=502,
            content={"detail": str(exc), "code": exc.code},
        )

    @app.exception_handler(DocumentError)
    async def document_failure(_: Request, exc: DocumentError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"detail": str(exc), "code": exc.code},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_failure(
        _: Request, exc: RequestValidationError
    ) -> JSONResponse:
        sanitized_errors: list[dict[str, Any]] = []
        for original in exc.errors():
            error = dict(original)
            # Pydantic may place a ValueError instance in ctx. It is not JSON
            # serializable and is unnecessary for a public validation response.
            error.pop("ctx", None)
            location = tuple(str(item) for item in error.get("loc", ()))
            if "api_key" in location:
                error.pop("input", None)
            elif "input" in error:
                error["input"] = _without_api_keys(error["input"])
            sanitized_errors.append(error)
        return JSONResponse(status_code=422, content={"detail": sanitized_errors})

    @app.exception_handler(InvalidRootError)
    async def invalid_root(_: Request, exc: InvalidRootError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(RootChangedError)
    async def changed_root(_: Request, exc: RootChangedError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(ScanInProgressError)
    async def scan_in_progress(_: Request, exc: ScanInProgressError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(static_directory / "index.html")

    @app.get("/model-connections", include_in_schema=False)
    async def model_connections_page() -> FileResponse:
        return FileResponse(static_directory / "model-connections.html")

    @app.get("/agent", include_in_schema=False)
    async def agent_page() -> FileResponse:
        return FileResponse(static_directory / "agent.html")

    @app.get("/health", tags=["system"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post(
        "/api/v1/workspaces",
        response_model=Workspace,
        status_code=status.HTTP_201_CREATED,
        tags=["workspaces"],
    )
    def register_workspace(payload: WorkspaceCreate) -> Workspace:
        return service.register(payload)

    @app.get(
        "/api/v1/workspaces",
        response_model=list[Workspace],
        tags=["workspaces"],
    )
    def list_workspaces() -> list[Workspace]:
        return service.list()

    @app.get(
        "/api/v1/workspaces/{workspace_id}",
        response_model=Workspace,
        tags=["workspaces"],
    )
    def get_workspace(workspace_id: str) -> Workspace:
        return service.get(workspace_id)

    @app.put(
        "/api/v1/workspaces/{workspace_id}/policy",
        response_model=Workspace,
        tags=["workspaces"],
    )
    def update_workspace_policy(
        workspace_id: str, payload: WorkspacePolicy
    ) -> Workspace:
        return service.update_policy(workspace_id, payload)

    @app.post(
        "/api/v1/workspaces/{workspace_id}/scan",
        response_model=ScanResult,
        tags=["scans"],
    )
    def scan(workspace_id: str) -> ScanResult:
        return service.scan(workspace_id)

    @app.get(
        "/api/v1/workspaces/{workspace_id}/scan-result",
        response_model=LatestScanResult,
        tags=["scans"],
    )
    def get_scan_result(workspace_id: str) -> LatestScanResult:
        return service.get_scan_result(workspace_id)

    @app.get(
        "/api/v1/model-connections",
        response_model=ModelConnectionList,
        tags=["model-connections"],
    )
    def list_model_connections() -> ModelConnectionList:
        return model_service.list()

    @app.post(
        "/api/v1/model-connections",
        response_model=ModelConnection,
        status_code=status.HTTP_201_CREATED,
        tags=["model-connections"],
    )
    def create_model_connection(payload: ModelConnectionCreate) -> ModelConnection:
        return model_service.create(payload)

    @app.get(
        "/api/v1/model-connections/{connection_id}",
        response_model=ModelConnection,
        tags=["model-connections"],
    )
    def get_model_connection(connection_id: str) -> ModelConnection:
        return model_service.get(connection_id)

    @app.patch(
        "/api/v1/model-connections/{connection_id}",
        response_model=ModelConnection,
        tags=["model-connections"],
    )
    def update_model_connection(
        connection_id: str, payload: ModelConnectionPatch
    ) -> ModelConnection:
        return model_service.update(connection_id, payload)

    @app.put(
        "/api/v1/model-connections/{connection_id}/activate",
        response_model=ModelConnection,
        tags=["model-connections"],
    )
    def activate_model_connection(connection_id: str) -> ModelConnection:
        return model_service.activate(connection_id)

    @app.delete(
        "/api/v1/model-connections/{connection_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        tags=["model-connections"],
    )
    def delete_model_connection(connection_id: str) -> Response:
        model_service.delete(connection_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post(
        "/api/v1/model-connections/{connection_id}/test-chat",
        response_model=ConnectionTestResult,
        tags=["model-connections"],
    )
    def test_model_chat(connection_id: str) -> ConnectionTestResult:
        return agent_service.test_chat(connection_id)

    @app.post(
        "/api/v1/model-connections/{connection_id}/test-tools",
        response_model=ConnectionTestResult,
        tags=["model-connections"],
    )
    def test_model_tools(connection_id: str) -> ConnectionTestResult:
        return agent_service.test_tool(connection_id)

    @app.post(
        "/api/v1/agent/cloud-enable",
        response_model=CloudEnableResponse,
        tags=["agent"],
    )
    def enable_cloud_qa(payload: CloudEnableRequest) -> CloudEnableResponse:
        return agent_service.enable(payload.workspace_id, payload.connection_id, payload.connection_version)

    @app.post(
        "/api/v1/agent/tasks",
        response_model=AgentTaskView,
        status_code=status.HTTP_202_ACCEPTED,
        tags=["agent"],
    )
    def create_agent_task(payload: AgentTaskCreate) -> AgentTaskView:
        try:
            return agent_service.create_task(
                payload.workspace_id, payload.question, payload.request_key, payload.connection_id, payload.connection_version
            )
        except PermissionError as exc:
            return JSONResponse(
                status_code=403,
                content={"detail": str(exc), "code": "cloud_qa_not_enabled"},
            )

    @app.get(
        "/api/v1/agent/tasks/{task_id}",
        response_model=AgentTaskView,
        tags=["agent"],
    )
    def get_agent_task(task_id: str) -> AgentTaskView:
        try:
            return agent_service.get_task(task_id)
        except KeyError as exc:
            return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.get(
        "/api/v1/agent/tasks/{task_id}/sources/{source_id}",
        response_model=SourceRecord,
        tags=["agent"],
    )
    def get_agent_source(task_id: str, source_id: str) -> SourceRecord:
        try:
            return agent_service.get_source(task_id, source_id)
        except KeyError as exc:
            return JSONResponse(status_code=404, content={"detail": str(exc)})

    return app


app = create_app()


def _without_api_keys(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _without_api_keys(item)
            for key, item in value.items()
            if str(key).casefold() != "api_key"
        }
    if isinstance(value, list):
        return [_without_api_keys(item) for item in value]
    return value
