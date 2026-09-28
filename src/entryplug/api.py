"""Small supported interface for task providers and client harnesses."""

from entryplug.core.evidence import EvidenceRecord
from entryplug.core.operation import (
    CapabilitySpec,
    EffectState,
    Lifecycle,
    OperationHost,
    OperationSnapshot,
)
from entryplug.harness.session import Session

__all__ = (
    "CapabilitySpec",
    "EvidenceRecord",
    "EffectState",
    "Lifecycle",
    "OperationHost",
    "OperationSnapshot",
    "Session",
)
