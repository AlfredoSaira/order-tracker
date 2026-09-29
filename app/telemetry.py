"""OpenTelemetry setup: traces, metrics, and structured logs for order lookups.

Exporter choice is driven by OTEL_EXPORTER_OTLP_ENDPOINT: unset means
console (Question 2), set means ship through the Collector (Question 3+).
"""

import logging
import os

from opentelemetry import metrics, trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor, ConsoleLogExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import ConsoleMetricExporter, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

RESOURCE = Resource.create(
    {
        "service.name": os.getenv("OTEL_SERVICE_NAME", "order-tracker"),
        "deployment.environment": os.getenv("APP_ENV", "dev"),
        "service.version": os.getenv("APP_VERSION", "local"),
    }
)

OTLP_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")


def _span_exporter():
    if OTLP_ENDPOINT:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

        return OTLPSpanExporter()
    return ConsoleSpanExporter()


def _metric_reader():
    if OTLP_ENDPOINT:
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter

        return PeriodicExportingMetricReader(OTLPMetricExporter(), export_interval_millis=5000)
    return PeriodicExportingMetricReader(ConsoleMetricExporter(), export_interval_millis=5000)


def _log_exporter():
    if OTLP_ENDPOINT:
        from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter

        return OTLPLogExporter()
    return ConsoleLogExporter()


def setup_telemetry(app):
    """Wire up tracing, metrics, and logging; instrument the FastAPI app for traces."""
    tracer_provider = TracerProvider(resource=RESOURCE)
    tracer_provider.add_span_processor(BatchSpanProcessor(_span_exporter(), schedule_delay_millis=1000))
    trace.set_tracer_provider(tracer_provider)

    meter_provider = MeterProvider(resource=RESOURCE, metric_readers=[_metric_reader()])
    metrics.set_meter_provider(meter_provider)

    logger_provider = LoggerProvider(resource=RESOURCE)
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(_log_exporter()))
    handler = LoggingHandler(level=logging.INFO, logger_provider=logger_provider)

    app_logger = logging.getLogger("order_tracker")
    app_logger.setLevel(logging.INFO)
    app_logger.addHandler(handler)

    FastAPIInstrumentor.instrument_app(app, tracer_provider=tracer_provider)

    meter = metrics.get_meter("order_tracker", meter_provider=meter_provider)
    order_lookup_requests = meter.create_counter(
        "order_lookup_requests",
        description="Order lookup requests by route and HTTP status code",
    )

    return order_lookup_requests, app_logger
