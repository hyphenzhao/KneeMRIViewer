"""Adapter registry."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import Adapter, BaseAdapter, SegmentationCandidate, SeriesCandidate
from .copd import CopdAdapter
from .ds0826 import Ds0826Adapter
from .generic import ChangzhengAdapter, FsPdwAdapter, SniffAdapter

REGISTRY: dict[str, type[BaseAdapter]] = {
    "ds0826": Ds0826Adapter,
    "changzheng": ChangzhengAdapter,
    "fspdw": FsPdwAdapter,
    "sniff": SniffAdapter,
    "bonescan": SniffAdapter,
    "copd_nifti": CopdAdapter,
}


def make_adapter(cfg: Any) -> BaseAdapter:
    try:
        cls = REGISTRY[cfg.adapter]
    except KeyError:
        raise KeyError(
            "unknown adapter %r (known: %s)" % (cfg.adapter, ", ".join(sorted(REGISTRY)))
        ) from None
    return cls(Path(cfg.root), cfg)


__all__ = [
    "Adapter", "BaseAdapter", "SegmentationCandidate", "SeriesCandidate",
    "REGISTRY", "make_adapter",
]
