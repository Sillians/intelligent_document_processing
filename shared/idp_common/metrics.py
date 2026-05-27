import time

from fastapi import FastAPI, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

HTTP_REQUESTS = Counter(
    "http_requests_total",
    "Total number of HTTP requests",
    ["service", "method", "path", "status"],
)
HTTP_LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency in seconds",
    ["service", "method", "path"],
)


def instrument_app(app: FastAPI, service_name: str) -> None:
    @app.middleware("http")
    async def metrics_middleware(request: Request, call_next):
        start = time.perf_counter()
        response = await call_next(request)
        duration = time.perf_counter() - start

        path = request.url.path
        method = request.method
        status = str(response.status_code)

        HTTP_REQUESTS.labels(service_name, method, path, status).inc()
        HTTP_LATENCY.labels(service_name, method, path).observe(duration)
        return response

    @app.get("/metrics")
    async def metrics() -> Response:
        return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
