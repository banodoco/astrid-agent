"""Pack data structures: :class:`PackDefinition` and :class:`PackPermission`."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from astrid.core.pack._common import _normalize_json_value


@dataclass(frozen=True)
class PackPermission:
    id: str
    reason: str
    access: str = ""
    services: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "reason": self.reason,
        }
        if self.access:
            payload["access"] = self.access
        if self.services:
            payload["services"] = list(self.services)
        return payload


@dataclass(frozen=True)
class PackDefinition:
    id: str
    name: str
    version: str
    root: Path
    manifest_path: Path
    metadata: dict[str, Any]
    description: str = ""
    content: dict[str, Any] = field(default_factory=dict)
    agent: dict[str, Any] = field(default_factory=dict)
    status: str = field(default="active")
    visibility: str = field(default="visible")
    schema_version: str = field(default="")
    aliases: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    permissions: tuple[PackPermission, ...] = field(default_factory=tuple)
    extensions: dict[str, Any] = field(default_factory=dict)
    origin: str = field(default="unknown")
    install_tier: str = field(default="default")
    pack_type: str = field(default="capability")
    domain: str = field(default="general")
    stability: str = field(default="stable")
    support: str = field(default="project")
    actions: dict[str, Any] = field(default_factory=dict)
    ui: dict[str, Any] = field(default_factory=dict)
    rendering: dict[str, Any] = field(default_factory=dict)
    documents: dict[str, Any] = field(default_factory=dict)
    documentation: dict[str, Any] = field(default_factory=dict)
    pack_api: str | None = None
    dependencies: dict[str, Any] = field(default_factory=dict)
    cli: dict[str, Any] = field(default_factory=dict)
    engines: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        taxonomy = {
            "origin": self.origin,
            "install_tier": self.install_tier,
            "pack_type": self.pack_type,
            "domain": self.domain,
            "stability": self.stability,
            "support": self.support,
        }
        payload = {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "root": str(self.root),
            "manifest_path": str(self.manifest_path),
            "metadata": dict(self.metadata),
            "content": dict(self.content),
            "agent": dict(self.agent),
            "status": self.status,
            "visibility": self.visibility,
            "schema_version": self.schema_version,
            **taxonomy,
            "taxonomy": taxonomy,
        }
        if self.aliases:
            payload["aliases"] = [dict(alias) for alias in self.aliases]
        if self.permissions:
            payload["permissions"] = [permission.to_dict() for permission in self.permissions]
        if self.extensions:
            payload["extensions"] = _normalize_json_value(
                self.extensions,
                path="pack.extensions",
            )
        if self.documentation:
            payload["documentation"] = dict(self.documentation)
        for section in ("actions", "ui", "rendering", "documents"):
            declarations = getattr(self, section)
            if declarations or str(self.schema_version) == "3":
                payload[section] = _normalize_json_value(declarations, path=f"pack.{section}")
        if self.pack_api is not None:
            payload["pack_api"] = self.pack_api
        if self.dependencies:
            payload["dependencies"] = _normalize_json_value(self.dependencies, path="pack.dependencies")
        if self.cli:
            payload["cli"] = _normalize_json_value(self.cli, path="pack.cli")
        if self.engines:
            payload["engines"] = _normalize_json_value(self.engines, path="pack.engines")
        return payload
