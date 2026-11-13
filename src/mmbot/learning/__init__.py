"""Rolling online learning for mmbot.

The package is deliberately dependency-light (numpy + stdlib) and opt-in:
nothing here runs unless ``MM_LEARNING=1`` (trading process) or the
``rolling_learning_daemon`` is started.  The trading loop only *records*;
training runs out-of-band so a checkpoint write can never block quoting.
"""
from __future__ import annotations

from .buffer import DecisionStep, RollingExperienceBuffer
from .features import FEATURE_NAMES, FeatureExtractor
from .keys import InMemoryKeyVault, parse_key_command
from .policy import ACTIONS, RISK_HOLD_ACTION, SoftmaxLinearPolicy
from .rewards import RewardTracker
from .trainer import RollingTrainer, TrainerConfig, TrainResult

__all__ = [
    "ACTIONS",
    "RISK_HOLD_ACTION",
    "DecisionStep",
    "FEATURE_NAMES",
    "FeatureExtractor",
    "InMemoryKeyVault",
    "RewardTracker",
    "RollingExperienceBuffer",
    "RollingTrainer",
    "SoftmaxLinearPolicy",
    "TrainResult",
    "TrainerConfig",
    "parse_key_command",
]
