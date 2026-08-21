"""FashionJobs operational configuration."""

from dataclasses import dataclass

from monitor.config import Env, RunnerConfig, load_runner_config
from monitor.types import OpsLabels

LOG_PREFIX = "fashionjobs-monitor"

LABELS = OpsLabels(
    name="FashionJobs internship monitor",
    tracked_noun="listing identity(ies)",
    death_footer="Liveness alert — check FashionJobs Stage listings manually.",
)


@dataclass(frozen=True)
class FashionJobsConfig:
    runner: RunnerConfig


def load_config(env: Env) -> FashionJobsConfig:
    return FashionJobsConfig(
        runner=load_runner_config(
            env,
            labels=LABELS,
            log_prefix=LOG_PREFIX,
            default_poll_interval_sec=600,
        )
    )
