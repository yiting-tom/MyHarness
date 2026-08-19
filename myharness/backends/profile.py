"""Backend profiles.

A lane says which backend it runs on and which capability tier of model it
wants; the profile turns that into the environment overrides the SDK needs.
``ClaudeAgentOptions.env`` is merged into the CLI subprocess environment, so two
lanes in one job can target different endpoints without interfering.

Capabilities are *declared*, not probed (design.md D7): probing costs money and
a single failure does not prove absence. A wrong declaration surfaces in the
live tests, and the event log records which path each run actually took.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final


class BackendCapability(StrEnum):
    """What a backend can enforce for us, as opposed to merely ask for."""

    STRUCTURED_OUTPUT = "structured_output"
    PROMPT_CACHING = "prompt_caching"
    TASK_BUDGET = "task_budget"


class ModelTier(StrEnum):
    """Capability tier a lane asks for, resolved per backend to a real model."""

    STRONG = "strong"
    MID = "mid"
    CHEAP = "cheap"


class BackendError(Exception):
    """Base for configuration problems, all raised before any request is sent."""


class MissingCredential(BackendError):
    def __init__(self, profile: str, env_var: str) -> None:
        super().__init__(
            f"backend {profile!r} needs environment variable {env_var!r}, which is not set"
        )
        self.env_var = env_var


class UnknownModelAlias(BackendError):
    def __init__(self, profile: str, alias: str, available: list[str]) -> None:
        super().__init__(
            f"backend {profile!r} has no model for tier {alias!r}; "
            f"available: {', '.join(sorted(available))}"
        )
        self.alias = alias


#: Built-in tools the CLI ships. Their definitions cost ~18.9k tokens per
#: request and ``allowed_tools`` does not remove them -- only ``disallowed_tools``
#: does (spikes/RESULTS.md §Spike #2b). Every ephemeral worker pays this unless
#: the lane declares what it actually needs.
BUILTIN_TOOLS: Final = (
    "Agent", "Bash", "CronCreate", "CronDelete", "CronList", "DesignSync", "Edit",
    "EnterWorktree", "ExitWorktree", "Glob", "Grep", "ListAgents", "Monitor",
    "NotebookEdit", "PushNotification", "Read", "RemoteTrigger", "ReportFindings",
    "ScheduleWakeup", "SendMessage", "Skill", "TaskCreate", "TaskGet", "TaskList",
    "TaskOutput", "TaskStop", "TaskUpdate", "WebFetch", "WebSearch", "Workflow",
    "Write",
)


@dataclass(frozen=True, slots=True)
class BackendProfile:
    """Where a lane's requests go, and what that endpoint can enforce."""

    name: str
    models: dict[str, str]
    capabilities: frozenset[BackendCapability]
    base_url: str | None = None
    auth_token_env: str | None = None
    extra_env: dict[str, str] = field(default_factory=dict)

    def supports(self, capability: BackendCapability) -> bool:
        return capability in self.capabilities

    def resolve_model(self, tier: str) -> str:
        try:
            return self.models[tier]
        except KeyError:
            raise UnknownModelAlias(self.name, tier, list(self.models)) from None

    def credential(self) -> str | None:
        """Read the key from the environment. Never stored in the profile."""
        if self.auth_token_env is None:
            return None
        value = os.environ.get(self.auth_token_env)
        if not value:
            raise MissingCredential(self.name, self.auth_token_env)
        return value

    def to_sdk_env(self) -> dict[str, str]:
        """Environment overrides for ``ClaudeAgentOptions.env``."""
        env: dict[str, str] = {"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
        if self.base_url:
            env["ANTHROPIC_BASE_URL"] = self.base_url
        token = self.credential()
        if token:
            env["ANTHROPIC_AUTH_TOKEN"] = token
        env.update(self.extra_env)
        return env

    @staticmethod
    def disallowed_for(declared_tools: object) -> list[str]:
        """Built-ins to strip so a worker pays only for what it declared."""
        keep = {str(t) for t in declared_tools}
        return [t for t in BUILTIN_TOOLS if t not in keep]


# --- shipped profiles ----------------------------------------------------

ANTHROPIC_DIRECT: Final = BackendProfile(
    name="anthropic",
    models={ModelTier.STRONG: "opus", ModelTier.MID: "sonnet", ModelTier.CHEAP: "haiku"},
    capabilities=frozenset(BackendCapability),
)

#: The paid super-120b variant, not ``:free``. The free tiers are unusable here:
#: ``ultra:free`` does not declare structured outputs and could not finish three
#: short requests in twelve minutes, and ``super-120b:free`` hits OpenRouter's
#: per-day free-model quota with a 429 (spikes/RESULTS.md §Spike #5). At
#: $0.08/M in and $0.40/M out the paid variant costs cents.
OPENROUTER: Final = BackendProfile(
    name="openrouter",
    models={
        ModelTier.STRONG: "nvidia/nemotron-3-super-120b-a12b",
        ModelTier.MID: "nvidia/nemotron-3-super-120b-a12b",
        ModelTier.CHEAP: "nvidia/nemotron-3-nano-30b-a3b",
    },
    capabilities=frozenset(BackendCapability),
    base_url="https://openrouter.ai/api",
    auth_token_env="OPENROUTER_KEY",
)

#: A self-hosted Anthropic-compatible proxy (LiteLLM and similar). Capabilities
#: are deliberately empty: an unknown proxy must prove what it can enforce.
SELF_HOSTED: Final = BackendProfile(
    name="self-hosted",
    models={},
    capabilities=frozenset(),
    base_url=None,
    auth_token_env="HARNESS_PROXY_KEY",
)


class BackendRegistry:
    def __init__(self, *profiles: BackendProfile) -> None:
        self._by_name = {p.name: p for p in profiles}

    def get(self, name: str) -> BackendProfile:
        try:
            return self._by_name[name]
        except KeyError:
            raise BackendError(
                f"unknown backend {name!r}; registered: {', '.join(sorted(self._by_name))}"
            ) from None

    def register(self, profile: BackendProfile) -> None:
        self._by_name[profile.name] = profile

    def names(self) -> list[str]:
        return sorted(self._by_name)


registry: Final = BackendRegistry(ANTHROPIC_DIRECT, OPENROUTER, SELF_HOSTED)
