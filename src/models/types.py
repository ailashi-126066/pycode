# Source: https://github.com/earendil-works/pi/blob/2c8d1fe/packages/ai/src/types.ts
# Source: https://github.com/earendil-works/pi/blob/2c8d1fe/packages/ai/src/utils/retry.ts
# Adaptation: provider configuration without secrets, per-attempt request records,
# at most three retries.
"""Provider and model identities, request records and the retry policy."""

import ipaddress
import math
from dataclasses import dataclass, field
from typing import Any, Literal

from ..kernel import (
    ModelCapabilities,
    ModelSelection,
    ProviderKind,
    Purpose,
    UsageRecord,
    clone_value,
)


@dataclass(frozen=True)
class CapabilityOverride:
    context_window: int | None = None
    max_output_tokens: int | None = None
    supports_tools: bool | None = None
    supports_thinking: bool | None = None
    supports_images: bool | None = None


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    implementation: ProviderKind
    model_id: str
    base_url: str
    api_key_env: str
    context_window: int
    max_output_tokens: int
    openai_api: Literal["chat_completions", "responses"] | None = None
    thinking: dict[str, Any] = field(default_factory=dict)
    supports_tools: bool = False
    supports_thinking: bool = False
    supports_images: bool = False
    timeout_seconds: float = 60
    model_overrides: dict[str, CapabilityOverride] = field(default_factory=dict)
    reasoning_effort: str | None = None


@dataclass(frozen=True)
class DiscoveredModel:
    model_id: str
    capabilities: ModelCapabilities = field(default_factory=ModelCapabilities)


@dataclass(frozen=True)
class ModelDescriptor:
    provider: str
    model_id: str
    implementation: ProviderKind
    capabilities: ModelCapabilities
    availability: Literal["available", "unknown", "unavailable"]
    source: Literal["configured", "server", "cache", "override"]


@dataclass(frozen=True)
class CatalogSnapshot:
    config_hash: str
    refreshed_at: float
    models: tuple[ModelDescriptor, ...] = ()
    errors: dict[str, str] = field(default_factory=dict)


def model_selection(config: ProviderConfig) -> ModelSelection:
    thinking = (
        config.reasoning_effort
        or config.thinking.get("type")
        or config.thinking.get("effort")
        or "off"
    )
    if thinking == "disabled":
        thinking = "off"
    capabilities = ModelCapabilities(
        config.context_window,
        config.max_output_tokens,
        config.supports_tools,
        config.supports_thinking,
        config.supports_images,
    )
    return ModelSelection(
        config.name,
        config.model_id,
        config.implementation,
        str(thinking),
        capabilities,
        clone_value(config.thinking),
        config.reasoning_effort,
        config.openai_api,
    )


def is_loopback_host(hostname):
    """Return whether a URL hostname names this machine (127.0.0.0/8, ::1, localhost)."""
    if not hostname:
        return False
    hostname = hostname.strip("[]").rstrip(".").casefold()
    if hostname == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class ModelFailure:
    code: str
    message: str
    retryable: bool
    retry_after: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RequestRecord:
    request_id: str
    session_id: str
    run_id: str
    provider: str
    model_id: str
    purpose: Purpose
    attempt: int
    status: Literal["started", "completed", "failed", "interrupted"]
    started_at: float
    agent_id: str = "lead"
    settled_at: float | None = None
    provider_request_id: str | None = None
    usage: UsageRecord | None = None
    error_code: str | None = None
    logical_id: str | None = None


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int = 3
    base_delay: float = 1
    max_delay: float = 60

    def __post_init__(self) -> None:
        if not 0 <= self.max_retries <= 3:
            raise ValueError("Model requests permit at most three retries")
        if not math.isfinite(self.base_delay) or not math.isfinite(self.max_delay):
            raise ValueError("Retry delays must be finite")
        if self.base_delay < 0 or self.max_delay < self.base_delay:
            raise ValueError("Invalid retry delay bounds")

    def delay(self, retry_number: int, retry_after: float | None = None) -> float:
        delay = min(self.base_delay * (2 ** (retry_number - 1)), self.max_delay)
        if retry_after is not None and math.isfinite(retry_after) and retry_after >= 0:
            delay = max(delay, retry_after)
        return delay
