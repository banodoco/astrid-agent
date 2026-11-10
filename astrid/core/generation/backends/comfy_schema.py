"""Adapt native Comfy wire inputs for the pinned VibeComfy schema parser."""
from __future__ import annotations

import copy
import re

from vibecomfy.schema.provider import TargetSchemaProvider as _TargetSchemaProvider


class TargetSchemaProvider(_TargetSchemaProvider):
    def trust_model_assets(self, rows):
        """Mark declaration-supplied diagnostics honestly before native persistence."""
        self._trusted_model_keys = {(row["name"], row.get("subdir", ""), row["sha256"], row["size"]) for row in rows}
        self._approval_diagnostics = getattr(self, "_approval_diagnostics", [])
        return self

    @property
    def _approval_diagnostics(self):
        return self.__dict__.get("_astrid_approval_diagnostics", [])

    @_approval_diagnostics.setter
    def _approval_diagnostics(self, diagnostics):
        trusted = self.__dict__.get("_trusted_model_keys", set())
        self.__dict__["_astrid_approval_diagnostics"] = [
            {**row, "status": "declared_unverified", "model_bytes_status": "declared_unverified"}
            if (isinstance(row, dict) and row.get("code") == "target_model_asset"
                and row.get("model_bytes_status") == "verified_target_provisioning"
                and (row.get("name"), row.get("subdir", ""), row.get("sha256"), row.get("size")) in trusted)
            else row for row in diagnostics]

    def _schema_from_loaded_object_info(self, class_type, info):
        # An Autogrow TemplateNames group is not itself a wire input. Its
        # populated sockets are dotted names; min=0 permits no sockets at all.
        # Keep the original capture/digest intact and adapt only its parsed view.
        adapted = None
        groups = info.get('input', {})
        if not isinstance(groups, dict):
            return super()._schema_from_loaded_object_info(class_type, info)
        for section in ('required', 'optional'):
            rows = groups.get(section, {})
            if not isinstance(rows, dict):
                continue
            for controller, raw in rows.items():
                if (not isinstance(raw, (list, tuple)) or len(raw) != 2
                        or raw[0] != 'COMFY_AUTOGROW_V3' or not isinstance(raw[1], dict)):
                    continue
                template = raw[1].get('template', {})
                if (not isinstance(template, dict) or type(template.get('min')) is not int
                        or template['min'] != 0):
                    continue
                names = template.get('names')
                inner = template.get('input')
                if (not isinstance(names, list) or not 1 <= len(names) <= 4096
                        or any(not isinstance(name, str)
                               or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name) for name in names)
                        or len(set(names)) != len(names) or not isinstance(inner, dict)
                        or set(inner) != {'required'} or not isinstance(inner['required'], dict)
                        or len(inner['required']) != 1):
                    continue
                leaf = next(iter(inner['required'].values()))
                if not isinstance(leaf, (list, tuple)) or not leaf:
                    continue
                if adapted is None:
                    adapted = copy.deepcopy(info)
                inputs = adapted['input']
                optional = inputs.setdefault('optional', {})
                fields = {f'{controller}.{name}': copy.deepcopy(leaf) for name in names}
                if any(name in group for name in fields for group in inputs.values()
                       if isinstance(group, dict)):
                    raise ValueError('Comfy autogrow input names collide with declared inputs')
                inputs[section].pop(controller)
                optional.update(fields)
                order = adapted.get('input_order')
                if isinstance(order, dict):
                    order[section] = [name for name in order.get(section, []) if name != controller]
                    order.setdefault('optional', []).extend(fields)
        return super()._schema_from_loaded_object_info(
            class_type, adapted if adapted is not None else info)
