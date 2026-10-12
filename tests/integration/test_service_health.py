from __future__ import annotations

from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from src.app.api.api import app


def _requests_total(path: str, status: str, method: str = "GET") -> float:
    value = REGISTRY.get_sample_value("http_requests_total", {"method": method, "path": path, "status": status})
    return 0.0 if value is None else value


def test_health_endpoint_responds() -> None:
    """Проверка здоровья сервиса отвечает по /health."""
    response = TestClient(app).get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_metrics_endpoint_responds() -> None:
    """Метрики доступны по /metrics."""
    response = TestClient(app).get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")


def test_requests_are_counted_in_metrics() -> None:
    """Обработанные запросы попадают в метрики."""
    before = _requests_total("/health", "200")

    assert TestClient(app).get("/health").status_code == 200

    assert _requests_total("/health", "200") == before + 1
