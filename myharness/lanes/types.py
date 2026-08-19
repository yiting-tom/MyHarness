"""Lane types and instances.

A lane's identity is its tool set and its charter -- and tools are code, so the
*type* has to be declared statically. What varies at runtime is which datasets
each instance owns, so instances are created per job (DESIGN.md decision #8).

``tabular-analyst`` is a type; ``txn-2024`` and ``txn-2023`` are two instances of
it, working in parallel with completely separate state.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from myharness.artifacts.ids import lane_namespace
from myharness.backends.profile import BackendProfile, ModelTier, registry

#: Defaults; design.md leaves these open pending real calibration data.
DEFAULT_STATE_MAX_TOKENS: Final = 8_000
DEFAULT_TOKEN_BUDGET: Final = 80_000
DEFAULT_MAX_TURNS: Final = 25
DEFAULT_INPUT_TOKEN_BUDGET: Final = 12_000


class LaneConfigError(Exception):
    """A lane type or instance was declared in a way that cannot run."""


class UnknownLaneType(LaneConfigError):
    def __init__(self, name: str, available: list[str]) -> None:
        super().__init__(
            f"unknown lane type {name!r}; registered: {', '.join(sorted(available)) or '(none)'}"
        )
        self.name = name
        self.available = available


@dataclass(frozen=True, slots=True)
class LaneType:
    """Static declaration: charter, tools, model tier, budgets."""

    name: str
    charter_path: Path
    tools: tuple[str, ...] = ()
    model_tier: str = ModelTier.MID
    backend: str = "anthropic"
    token_budget: int = DEFAULT_TOKEN_BUDGET
    max_turns: int = DEFAULT_MAX_TURNS
    state_max_tokens: int = DEFAULT_STATE_MAX_TOKENS
    input_token_budget: int = DEFAULT_INPUT_TOKEN_BUDGET
    description: str = ""

    def charter(self) -> str:
        """Read the charter. Kept in a file so it can be diffed and reviewed."""
        try:
            return Path(self.charter_path).read_text(encoding="utf-8")
        except OSError as exc:
            raise LaneConfigError(
                f"lane type {self.name!r}: cannot read charter {self.charter_path}: {exc}"
            ) from exc

    def charter_hash(self) -> str:
        """Recorded in the event log so a run can be tied to a charter version."""
        return hashlib.sha256(self.charter().encode("utf-8")).hexdigest()[:12]

    def backend_profile(self) -> BackendProfile:
        return registry.get(self.backend)

    def model(self) -> str:
        return self.backend_profile().resolve_model(self.model_tier)


@dataclass(frozen=True, slots=True)
class LaneInstance:
    """Runtime instance of a lane type, owning its own state and namespace."""

    id: str
    type: LaneType
    scope: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def namespace(self) -> str:
        return lane_namespace(self.id)

    @property
    def state_name(self) -> str:
        return f"{self.namespace}/state"

    def finding_name(self, seq: int | str) -> str:
        return f"{self.namespace}/findings/{seq}"

    def describe(self) -> str:
        base = f"{self.id} ({self.type.name})"
        return f"{base}: {self.scope}" if self.scope else base


class LaneRegistry:
    """Types are registered up front; instances are created per job."""

    def __init__(self, *types: LaneType) -> None:
        self._types: dict[str, LaneType] = {t.name: t for t in types}
        self._instances: dict[str, LaneInstance] = {}

    def register(self, lane_type: LaneType) -> LaneType:
        self._types[lane_type.name] = lane_type
        return lane_type

    def type_names(self) -> list[str]:
        return sorted(self._types)

    def get_type(self, name: str) -> LaneType:
        try:
            return self._types[name]
        except KeyError:
            raise UnknownLaneType(name, list(self._types)) from None

    def create(self, instance_id: str, type_name: str, *, scope: str = "", **metadata: Any) -> LaneInstance:
        if instance_id in self._instances:
            raise LaneConfigError(f"lane instance {instance_id!r} already exists")
        instance = LaneInstance(
            id=instance_id, type=self.get_type(type_name), scope=scope, metadata=metadata
        )
        self._instances[instance_id] = instance
        return instance

    def get(self, instance_id: str) -> LaneInstance:
        try:
            return self._instances[instance_id]
        except KeyError:
            raise LaneConfigError(
                f"unknown lane instance {instance_id!r}; "
                f"created: {', '.join(sorted(self._instances)) or '(none)'}"
            ) from None

    def instances(self) -> list[LaneInstance]:
        return [self._instances[k] for k in sorted(self._instances)]

    def describe_types(self) -> str:
        """The catalogue the orchestrator sees when planning."""
        return "\n".join(
            f"- {t.name}: {t.description or '(no description)'}"
            for t in (self._types[k] for k in sorted(self._types))
        )
