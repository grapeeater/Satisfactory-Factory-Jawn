"""Experiment configuration (TOML -> validated pydantic models)."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FrmConfig(_M):
    base_url: str = "http://127.0.0.1:8080"
    token_env: str = "FRM_TOKEN"
    allow_remote: bool = False  # refuse non-loopback FRM hosts unless explicitly allowed
    read_endpoints: list[str] = Field(
        default_factory=lambda: ["getFactory", "getStorageInv", "getPower", "getSwitches", "getSessionInfo", "getChatMessages"]
    )
    write_endpoints: list[str] = Field(default_factory=lambda: ["setEnabled", "setSwitches", "sendChatMessage"])
    timeout_s: float = 3.0
    get_retries: int = 1


class SafetyConfig(_M):
    max_decision_age_s: float = 30.0
    max_snapshot_age_s: float = 20.0
    min_dwell_s: float = 10.0
    human_lock_s: float = 120.0
    verify_timeout_s: float = 15.0
    max_writes_per_min: int = 12
    power_margin: float = 0.05
    count_pending_as_running: bool = True
    chat_commands: bool = True
    chat_prefix: str = "!jawn"
    announce_in_chat: bool = False
    stop_file: str = "STOP"
    on_stop: Literal["hold", "restore"] = "restore"
    stop_restore_timeout_s: float = 30.0
    game_paused_after_s: float = 12.0
    suspect_cooldown_s: float = 30.0
    shed_lock_s: float = 30.0  # lock after a priority switch went off on its own (game load shedding)
    advisory_min_interval_s: float = 120.0  # per agent; identical advisories are never repeated


class TargetBinding(_M):
    """A controllable object, referred to by alias everywhere in the harness."""

    id: str
    kind: Literal["machine", "switch"] = "machine"
    class_name: str | None = None
    recipe_class: str | None = None
    product: str | None = None  # item class this machine makes (for progress tracking)
    location: tuple[float, float, float] | None = None
    description: str = ""


class ConstraintConfig(_M):
    kind: Literal["max_running"] = "max_running"
    name: str
    targets: list[str]
    max: int = 1


class TriggerConfig(_M):
    min_interval_s: float = 15.0
    heartbeat_s: float = 180.0


class AgentConfig(_M):
    id: str
    role: str
    objective: str
    owns: list[str]
    allowed_actions: list[str] = Field(default_factory=lambda: ["machine.enable", "machine.disable"])
    targets: dict[str, int] = Field(default_factory=dict)  # item class -> count delivered to storage
    priority_weight: float = 1.0
    triggers: TriggerConfig = Field(default_factory=TriggerConfig)
    memory_chars: int = 600
    history_len: int = 6
    max_calls: int = 200
    on_budget_exhausted: Literal["hold", "baseline"] = "baseline"
    personality: str = "cooperative"  # mock provider only


class PricingConfig(_M):
    """USD per 1M tokens at PEAK; off-peak = peak * offpeak_factor (DeepSeek docs, 2026-11-26)."""

    input_hit: float = 0.006
    input_miss: float = 0.30
    output: float = 1.20
    offpeak_factor: float = 0.5
    peak_windows_utc: list[tuple[int, int]] = Field(default_factory=lambda: [(1, 4), (6, 10)])
    peak_weekdays_only: bool = True


class DeepSeekConfig(_M):
    model: str = "deepseek-flash"  # verified via GET /models and pricing docs on 2026-11-26
    base_url: str = "https://api.deepseek.com"
    api_key_env: str = "DEEPSEEK_API_KEY"
    allow_paid: bool = False
    mode: Literal["tools_strict", "json"] = "tools_strict"
    max_tokens: int = 200
    temperature: float = 0.0
    timeout_s: float = 30.0
    max_retries: int = 2
    pricing: PricingConfig = Field(default_factory=PricingConfig)


class MockConfig(_M):
    latency_s: float = 2.0
    seed: int = 0
    simulate_usage: bool = True


class LlmConfig(_M):
    provider: Literal["mock", "scripted", "replay", "deepseek"] = "mock"
    max_concurrent: int = 2
    deepseek: DeepSeekConfig = Field(default_factory=DeepSeekConfig)
    mock: MockConfig = Field(default_factory=MockConfig)
    replay_log: str | None = None


class BudgetConfig(_M):
    max_usd: float = 0.0  # real spend cap per run; 0 = no paid calls at all
    max_calls: int = 400
    max_calls_per_agent: int = 200
    simulated_max_usd: float = 1.0  # cap applied to simulated (mock) usage so budget logic is exercised offline


class ExperimentConfig(_M):
    name: str
    duration_s: float = 1200.0
    poll_interval_s: float = 5.0
    end_when_targets_met: bool = True
    settle_s: float = 10.0
    controller: Literal["agents", "baseline"] = "agents"
    # machines: enabled | disabled; switches: on | off
    initial_state: dict[str, Literal["enabled", "disabled", "on", "off"]] = Field(default_factory=dict)


class SimConfig(_M):
    scenario: str | None = None
    seed: int = 0
    token: str = "sim-token"


class DashboardConfig(_M):
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8787


class Config(_M):
    experiment: ExperimentConfig
    frm: FrmConfig = Field(default_factory=FrmConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    targets: dict[str, TargetBinding] = Field(default_factory=dict)
    constraints: list[ConstraintConfig] = Field(default_factory=list)
    agents: list[AgentConfig] = Field(default_factory=list)
    llm: LlmConfig = Field(default_factory=LlmConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    sim: SimConfig = Field(default_factory=SimConfig)
    dashboard: DashboardConfig = Field(default_factory=DashboardConfig)
    base_dir: str = "."

    @model_validator(mode="after")
    def _check(self) -> "Config":
        owners: dict[str, str] = {}
        for a in self.agents:
            for t in a.owns:
                if t not in self.targets:
                    raise ValueError(f"agent {a.id} owns unknown target alias {t!r}")
                if t in owners:
                    raise ValueError(f"target {t!r} owned by both {owners[t]} and {a.id}")
                owners[t] = a.id
        ids = [a.id for a in self.agents]
        if len(set(ids)) != len(ids):
            raise ValueError("agent ids must be unique")
        for c in self.constraints:
            for t in c.targets:
                if t not in self.targets:
                    raise ValueError(f"constraint {c.name} references unknown target {t!r}")
        for t, s in self.experiment.initial_state.items():
            if t not in self.targets:
                raise ValueError(f"initial_state references unknown target {t!r}")
            kind = self.targets[t].kind
            if (kind == "machine") != (s in ("enabled", "disabled")):
                raise ValueError(f"initial_state {t}={s!r}: use enabled/disabled for machines, on/off for switches")
        return self

    def desired_initial(self) -> dict[str, bool]:
        """alias -> observed value wanted after setup (machines: paused, switches: on)."""
        return {t: (s in ("disabled", "on")) for t, s in self.experiment.initial_state.items()}

    def owner_of(self, alias: str) -> str | None:
        for a in self.agents:
            if alias in a.owns:
                return a.id
        return None

    def resolve(self, rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else Path(self.base_dir) / p


def load_config(path: str | Path) -> Config:
    path = Path(path)
    with open(path, "rb") as f:
        data = tomllib.load(f)
    data.setdefault("base_dir", str(path.parent.resolve()))
    return Config.model_validate(data)
