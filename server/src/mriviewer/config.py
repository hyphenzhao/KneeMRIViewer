"""Configuration: a single TOML file, overridable by MRIVIEWER_CONFIG."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # Ubuntu 22.04 ships 3.10
    import tomli as tomllib

DEFAULT_CONFIG_PATHS = [
    Path("/etc/mriviewer/config.toml"),
    Path.home() / ".config" / "mriviewer" / "config.toml",
    Path(__file__).resolve().parents[3] / "config.toml",
]


@dataclass
class DatasetConfig:
    key: str
    adapter: str
    root: Path
    name: str = ""
    label_set: str | None = None
    viewer: str = "mpr"           # "mpr" | "frames_2d"
    delivery_profile: str = "native"
    reports_csv: str | None = None
    metadata_xlsx: str | None = None
    enabled: bool = True
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class AiConfig:
    """LLM report generation.

    Two switches on purpose. ``enabled`` turns the feature on; ``allow_egress``
    is what permits talking to anything that is not this machine, and it is
    enforced in the client rather than documented in a comment. An air-gapped
    deployment points ``base_url`` at a local model (Ollama, vLLM) and leaves
    ``allow_egress`` false, and the feature still works.

    The key is never in this file. It is read from a 0600 file at request time
    so it cannot end up in a config dump, a log line, or a backup of the repo.
    """
    enabled: bool = False
    allow_egress: bool = False
    base_url: str = "https://api.deepseek.com/v1"
    model: str = "deepseek-v4-pro"
    api_key_file: Path | None = None
    timeout_s: float = 60.0
    # Whitelist of patient attributes allowed into the prompt. Empty = none.
    # ageBand is a decade ("50-59"), never an exact age or date.
    patient_context: list[str] = field(default_factory=lambda: ["ageBand", "sex", "laterality"])
    prompts_dir: Path = Path(__file__).resolve().parents[2] / "prompts"

    def read_api_key(self) -> str | None:
        if not self.api_key_file:
            return None
        try:
            return Path(self.api_key_file).read_text(encoding="utf-8").strip() or None
        except OSError:
            return None


@dataclass
class Config:
    bind_host: str = "0.0.0.0"
    bind_port: int = 8080
    state_dir: Path = Path("/var/lib/mriviewer")
    labelsets_dir: Path = Path(__file__).resolve().parents[2] / "labelsets"
    # Clinical reference values and metric wording. A file, not a table: doctors
    # edit it directly and every revision is a reviewable diff.
    refs_dir: Path = Path(__file__).resolve().parents[2] / "refs"
    # Report chapter templates (YAML). Doctors edit these to change the
    # report's structure and wording.
    reports_dir: Path = Path(__file__).resolve().parents[2] / "reports"
    web_dir: Path | None = None
    predictions_roots: list[Path] = field(default_factory=list)
    scan_workers: int = 12
    # Volume larger than this (bytes, uncompressed) gets downsampled before delivery.
    browser_volume_budget: int = 150 * 1024 * 1024
    datasets: list[DatasetConfig] = field(default_factory=list)
    ai: AiConfig = field(default_factory=AiConfig)

    @property
    def db_path(self) -> Path:
        return self.state_dir / "index.sqlite"

    @property
    def cache_dir(self) -> Path:
        return self.state_dir / "cache"

    @property
    def derived_dir(self) -> Path:
        return self.state_dir / "derived"

    def dataset(self, key: str) -> DatasetConfig:
        for d in self.datasets:
            if d.key == key:
                return d
        raise KeyError(f"unknown dataset {key!r}")

    def ensure_dirs(self) -> None:
        for p in (self.state_dir, self.cache_dir, self.derived_dir,
                  self.cache_dir / "vol", self.cache_dir / "seg",
                  self.cache_dir / "mesh", self.cache_dir / "thumb"):
            p.mkdir(parents=True, exist_ok=True)


def _find_config() -> Path | None:
    env = os.environ.get("MRIVIEWER_CONFIG")
    if env:
        return Path(env)
    for p in DEFAULT_CONFIG_PATHS:
        if p.exists():
            return p
    return None


def load_config(path: str | os.PathLike | None = None) -> Config:
    p = Path(path) if path else _find_config()
    if p is None:
        raise FileNotFoundError(
            "no config.toml found; set MRIVIEWER_CONFIG or create /etc/mriviewer/config.toml"
        )
    raw = tomllib.loads(Path(p).read_text(encoding="utf-8"))

    cfg = Config()
    if "bind" in raw:
        host, _, port = str(raw["bind"]).rpartition(":")
        cfg.bind_host, cfg.bind_port = host or "0.0.0.0", int(port)
    if "state_dir" in raw:
        cfg.state_dir = Path(raw["state_dir"]).expanduser()
    if "labelsets_dir" in raw:
        cfg.labelsets_dir = Path(raw["labelsets_dir"]).expanduser()
    if "refs_dir" in raw:
        cfg.refs_dir = Path(raw["refs_dir"]).expanduser()
    if "reports_dir" in raw:
        cfg.reports_dir = Path(raw["reports_dir"]).expanduser()
    if "ai" in raw:
        a = raw["ai"]
        for name in ("enabled", "allow_egress", "base_url", "model", "timeout_s",
                     "patient_context"):
            if name in a:
                setattr(cfg.ai, name, a[name])
        for name in ("api_key_file", "prompts_dir"):
            if name in a:
                setattr(cfg.ai, name, Path(a[name]).expanduser())
    if "web_dir" in raw:
        cfg.web_dir = Path(raw["web_dir"]).expanduser()
    if "scan_workers" in raw:
        cfg.scan_workers = int(raw["scan_workers"])
    if "browser_volume_budget_mb" in raw:
        cfg.browser_volume_budget = int(raw["browser_volume_budget_mb"]) * 1024 * 1024
    cfg.predictions_roots = [Path(x).expanduser() for x in raw.get("predictions_roots", [])]

    for d in raw.get("datasets", []):
        cfg.datasets.append(DatasetConfig(
            key=d["key"],
            adapter=d["adapter"],
            root=Path(d["root"]).expanduser(),
            name=d.get("name", d["key"]),
            label_set=d.get("label_set"),
            viewer=d.get("viewer", "mpr"),
            delivery_profile=d.get("delivery_profile", "native"),
            # `reports_file` is the general name; `reports_csv` predates xlsx support.
            reports_csv=d.get("reports_file") or d.get("reports_csv"),
            metadata_xlsx=d.get("metadata_xlsx"),
            enabled=d.get("enabled", True),
            extra={k: v for k, v in d.items() if k not in {
                "key", "adapter", "root", "name", "label_set", "viewer",
                "delivery_profile", "reports_csv", "reports_file", "metadata_xlsx",
                "enabled"}},
        ))
    cfg.config_path = Path(p)  # type: ignore[attr-defined]
    return cfg
