"""MLflow-based LLM tracing and cost tracking.

Usage:
    # Initialize at app startup
    from app.observability import init_mlflow
    init_mlflow()

    # Option 1: Use traced LLM wrapper
    from app.observability import get_llm_with_tracing
    llm = get_llm_with_tracing(
        model="gpt-4o-mini",
        agent="smartqna",
        user_id="user-123",
    )
    response = await llm.ainvoke(messages)

    # Option 2: Manual tracing context
    from app.observability import traced_llm_call
    async with traced_llm_call(agent="smartqna", user_id="user-123") as trace:
        response = await llm.ainvoke(messages)
        trace.log_response(response)
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import wraps
from typing import Any, Callable
from uuid import uuid4

import mlflow
from langchain_openai import ChatOpenAI
from langchain_anthropic import ChatAnthropic
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult

from app.config.settings import settings

log = logging.getLogger(__name__)

# Cost per 1M tokens (as of 2024) - update as pricing changes
TOKEN_COSTS = {
    # OpenAI
    "gpt-4o": {"input": 2.50, "output": 10.00},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-4-turbo": {"input": 10.00, "output": 30.00},
    "gpt-4": {"input": 30.00, "output": 60.00},
    "gpt-3.5-turbo": {"input": 0.50, "output": 1.50},
    # Anthropic
    "claude-3-5-sonnet-20241022": {"input": 3.00, "output": 15.00},
    "claude-3-opus-20240229": {"input": 15.00, "output": 75.00},
    "claude-3-sonnet-20240229": {"input": 3.00, "output": 15.00},
    "claude-3-haiku-20240307": {"input": 0.25, "output": 1.25},
}

_mlflow_initialized = False


def init_mlflow() -> None:
    """Initialize MLflow tracking. Call once at app startup."""
    global _mlflow_initialized
    if _mlflow_initialized:
        return

    if not settings.llm_tracing_enabled:
        log.info("LLM tracing disabled via settings")
        return

    try:
        mlflow.set_tracking_uri(settings.mlflow_tracking_uri)
        # Disable autologging - we manage runs explicitly to avoid "unfinished" runs
        # mlflow.langchain.autolog() creates runs that don't close properly with async
        _mlflow_initialized = True
        log.info("MLflow initialized: %s", settings.mlflow_tracking_uri)
    except Exception as e:
        log.warning("MLflow init failed (tracing disabled): %s", e)


@dataclass
class LLMCallMetrics:
    """Metrics for a single LLM call."""
    call_id: str = field(default_factory=lambda: str(uuid4()))
    agent: str = ""
    user_id: str | None = None
    conversation_id: str | None = None
    model: str = ""
    provider: str = ""  # "openai" or "anthropic"

    # Timing
    start_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    end_time: datetime | None = None
    latency_ms: float = 0.0

    # Tokens
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0

    # Cost (USD)
    input_cost: float = 0.0
    output_cost: float = 0.0
    total_cost: float = 0.0

    # Status
    success: bool = True
    error: str | None = None

    # Optional context
    metadata: dict[str, Any] = field(default_factory=dict)

    def calculate_cost(self) -> None:
        """Calculate cost based on token usage and model."""
        costs = TOKEN_COSTS.get(self.model, {"input": 0, "output": 0})
        self.input_cost = (self.input_tokens / 1_000_000) * costs["input"]
        self.output_cost = (self.output_tokens / 1_000_000) * costs["output"]
        self.total_cost = self.input_cost + self.output_cost

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for logging."""
        return {
            "call_id": self.call_id,
            "agent": self.agent,
            "user_id": self.user_id,
            "conversation_id": self.conversation_id,
            "model": self.model,
            "provider": self.provider,
            "start_time": self.start_time.isoformat(),
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "latency_ms": self.latency_ms,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "input_cost_usd": round(self.input_cost, 6),
            "output_cost_usd": round(self.output_cost, 6),
            "total_cost_usd": round(self.total_cost, 6),
            "success": self.success,
            "error": self.error,
            "metadata": self.metadata,
        }


class TracingCallbackHandler(BaseCallbackHandler):
    """LangChain callback handler that captures token usage and logs to MLflow."""

    def __init__(self, metrics: LLMCallMetrics):
        self.metrics = metrics
        self._start_time: float = 0
        self._run_id: str | None = None

    def on_llm_start(self, serialized: dict, prompts: list[str], **kwargs) -> None:
        self._start_time = time.perf_counter()
        self.metrics.start_time = datetime.now(timezone.utc)

        # Start MLflow run
        try:
            run = mlflow.start_run(run_name=f"{self.metrics.agent}-llm-call")
            self._run_id = run.info.run_id
            mlflow.set_tags({
                "agent": self.metrics.agent,
                "user_id": self.metrics.user_id or "anonymous",
                "model": self.metrics.model,
                "provider": self.metrics.provider,
            })
        except Exception as e:
            log.warning("Failed to start MLflow run: %s", e)

    def on_llm_end(self, response: LLMResult, **kwargs) -> None:
        self.metrics.end_time = datetime.now(timezone.utc)
        self.metrics.latency_ms = (time.perf_counter() - self._start_time) * 1000

        # Extract token usage from response
        if response.llm_output:
            usage = response.llm_output.get("token_usage", {})
            self.metrics.input_tokens = usage.get("prompt_tokens", 0)
            self.metrics.output_tokens = usage.get("completion_tokens", 0)
            self.metrics.total_tokens = usage.get("total_tokens", 0)

            # Model info
            if "model_name" in response.llm_output:
                self.metrics.model = response.llm_output["model_name"]

        self.metrics.calculate_cost()
        self.metrics.success = True

        # Log to MLflow and end run
        self._log_to_mlflow()
        self._end_run()

    def on_llm_error(self, error: Exception, **kwargs) -> None:
        self.metrics.end_time = datetime.now(timezone.utc)
        self.metrics.latency_ms = (time.perf_counter() - self._start_time) * 1000
        self.metrics.success = False
        self.metrics.error = str(error)

        self._log_to_mlflow()
        self._end_run(status="FAILED")

    def _log_to_mlflow(self) -> None:
        """Log metrics to MLflow."""
        if not self._run_id:
            return

        try:
            # Log as metrics
            mlflow.log_metrics({
                "latency_ms": self.metrics.latency_ms,
                "input_tokens": self.metrics.input_tokens,
                "output_tokens": self.metrics.output_tokens,
                "total_tokens": self.metrics.total_tokens,
                "cost_usd": self.metrics.total_cost,
            })

            # Log call details as params
            mlflow.log_params({
                "call_id": self.metrics.call_id,
                "agent": self.metrics.agent,
                "model": self.metrics.model,
                "provider": self.metrics.provider,
                "success": str(self.metrics.success),
            })

            log.debug(
                "LLM call logged: agent=%s model=%s tokens=%d cost=$%.4f latency=%dms",
                self.metrics.agent,
                self.metrics.model,
                self.metrics.total_tokens,
                self.metrics.total_cost,
                int(self.metrics.latency_ms),
            )
        except Exception as e:
            log.warning("Failed to log LLM metrics to MLflow: %s", e)

    def _end_run(self, status: str = "FINISHED") -> None:
        """End the MLflow run."""
        if not self._run_id:
            return

        try:
            mlflow.end_run(status=status)
            self._run_id = None
        except Exception as e:
            log.warning("Failed to end MLflow run: %s", e)


@asynccontextmanager
async def traced_llm_call(
    agent: str,
    user_id: str | None = None,
    conversation_id: str | None = None,
    model: str = "",
    provider: str = "openai",
    metadata: dict[str, Any] | None = None,
):
    """
    Async context manager for tracing an LLM call.

    Usage:
        async with traced_llm_call(agent="smartqna", user_id="123") as trace:
            response = await llm.ainvoke(messages)
            trace.metrics.input_tokens = response.usage.input_tokens
            trace.metrics.output_tokens = response.usage.output_tokens
    """
    metrics = LLMCallMetrics(
        agent=agent,
        user_id=user_id,
        conversation_id=conversation_id,
        model=model,
        provider=provider,
        metadata=metadata or {},
    )

    start_time = time.perf_counter()

    try:
        # Start MLflow run for this call
        with mlflow.start_run(run_name=f"{agent}-llm-call", nested=True):
            mlflow.set_tags({
                "agent": agent,
                "user_id": user_id or "anonymous",
                "model": model,
                "provider": provider,
            })

            yield metrics

            # Finalize metrics
            metrics.end_time = datetime.now(timezone.utc)
            metrics.latency_ms = (time.perf_counter() - start_time) * 1000
            metrics.calculate_cost()
            metrics.success = True

            # Log final metrics
            mlflow.log_metrics({
                "latency_ms": metrics.latency_ms,
                "input_tokens": metrics.input_tokens,
                "output_tokens": metrics.output_tokens,
                "total_tokens": metrics.total_tokens,
                "cost_usd": metrics.total_cost,
            })

    except Exception as e:
        metrics.end_time = datetime.now(timezone.utc)
        metrics.latency_ms = (time.perf_counter() - start_time) * 1000
        metrics.success = False
        metrics.error = str(e)

        log.error("LLM call failed: agent=%s error=%s", agent, e)
        raise


def get_llm_with_tracing(
    model: str | None = None,
    provider: str = "openai",
    agent: str = "unknown",
    user_id: str | None = None,
    conversation_id: str | None = None,
    temperature: float = 0.1,
    **kwargs,
) -> ChatOpenAI | ChatAnthropic:
    """
    Get an LLM instance with tracing callback attached.

    Args:
        model: Model name (uses settings default if None)
        provider: "openai" or "anthropic"
        agent: Agent name for tracking
        user_id: User ID for tracking
        conversation_id: Conversation ID for tracking
        temperature: LLM temperature
        **kwargs: Additional LLM parameters

    Returns:
        LLM instance with tracing enabled
    """
    metrics = LLMCallMetrics(
        agent=agent,
        user_id=user_id,
        conversation_id=conversation_id,
        provider=provider,
    )

    callback = TracingCallbackHandler(metrics)

    if provider == "anthropic":
        model = model or settings.anthropic_chat_model
        metrics.model = model
        return ChatAnthropic(
            model=model,
            temperature=temperature,
            api_key=settings.anthropic_api_key,
            callbacks=[callback],
            **kwargs,
        )
    else:
        model = model or settings.openai_chat_model
        metrics.model = model
        return ChatOpenAI(
            model=model,
            temperature=temperature,
            api_key=settings.openai_api_key,
            callbacks=[callback],
            **kwargs,
        )


# Experiment tracking for evaluations
def create_experiment(name: str, tags: dict[str, str] | None = None) -> str:
    """Create or get an MLflow experiment for tracking evaluations."""
    try:
        experiment = mlflow.get_experiment_by_name(name)
        if experiment:
            return experiment.experiment_id

        experiment_id = mlflow.create_experiment(name, tags=tags)
        log.info("Created MLflow experiment: %s (id=%s)", name, experiment_id)
        return experiment_id
    except Exception as e:
        log.warning("Failed to create MLflow experiment: %s", e)
        return "0"  # Default experiment


def log_evaluation(
    experiment_name: str,
    metrics: dict[str, float],
    params: dict[str, Any] | None = None,
    tags: dict[str, str] | None = None,
) -> None:
    """Log an evaluation run to MLflow."""
    try:
        experiment_id = create_experiment(experiment_name)
        with mlflow.start_run(experiment_id=experiment_id):
            if tags:
                mlflow.set_tags(tags)
            if params:
                mlflow.log_params(params)
            mlflow.log_metrics(metrics)
    except Exception as e:
        log.warning("Failed to log evaluation to MLflow: %s", e)
