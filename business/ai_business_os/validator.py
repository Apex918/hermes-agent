"""Versioned JSON Schema validation for the Hermes AI Business OS.

The contract surface is intentionally small:
- EvidenceEnvelope, DecisionRecord, ActionRequest, and WorkItem are versioned
  JSON Schema contracts.
- Twelve enterprise role manifests are validated against a shared role
  manifest schema.
- Validation is fail-closed for missing rights / freshness / license data and
  parent artifacts are read locally without network access.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable

CONTRACT_DIR = Path(__file__).resolve().parent / 'contracts'
ROLE_MANIFEST_DIR = CONTRACT_DIR / 'role-manifests'
CONTRACT_FILES = {
    'evidence-envelope': CONTRACT_DIR / 'evidence-envelope.v1.schema.json',
    'decision-record': CONTRACT_DIR / 'decision-record.v1.schema.json',
    'action-request': CONTRACT_DIR / 'action-request.v1.schema.json',
    'work-item': CONTRACT_DIR / 'work-item.v1.schema.json',
    'role-manifest': CONTRACT_DIR / 'role-manifest.v1.schema.json',
}
CONTRACT_ALIASES = {
    'evidence_envelope': 'evidence-envelope',
    'EvidenceEnvelope': 'evidence-envelope',
    'decision_record': 'decision-record',
    'DecisionRecord': 'decision-record',
    'action_request': 'action-request',
    'ActionRequest': 'action-request',
    'work_item': 'work-item',
    'WorkItem': 'work-item',
}
EXPECTED_ROLE_MANIFEST_ROLES = ['ceo', 'cfo', 'coo', 'cmo', 'cro', 'cpo', 'cto', 'ciso', 'chro', 'gc', 'cco', 'cdo']

def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding='utf-8'))

def load_manifest(path: Path) -> dict[str, Any]:
    return load_json(path)

def load_named_schema(name: str) -> dict[str, Any]:
    name = CONTRACT_ALIASES.get(name, name)
    try:
        path = CONTRACT_FILES[name]
    except KeyError as exc:
        raise KeyError(f'unknown contract {name!r}') from exc
    return load_json(path)

def _type_ok(instance: Any, schema_type: str) -> bool:
    if schema_type == 'object':
        return isinstance(instance, dict)
    if schema_type == 'array':
        return isinstance(instance, list)
    if schema_type == 'string':
        return isinstance(instance, str)
    if schema_type == 'integer':
        return isinstance(instance, int) and not isinstance(instance, bool)
    if schema_type == 'number':
        return isinstance(instance, (int, float)) and not isinstance(instance, bool)
    if schema_type == 'boolean':
        return isinstance(instance, bool)
    if schema_type == 'null':
        return instance is None
    raise ValueError(f'unsupported schema type: {schema_type}')

def _resolve_ref(schema: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    ref = schema['$ref']
    if not ref.startswith('#/'):
        raise ValueError(f'only local refs are supported, got {ref!r}')
    target: Any = root
    for part in ref[2:].split('/'):
        target = target[part]
    return target

def validate_json_schema(instance: Any, schema: dict[str, Any], *, root: dict[str, Any] | None = None, path: str = '$') -> list[str]:
    if root is None:
        root = schema
    if '$ref' in schema:
        return validate_json_schema(instance, _resolve_ref(schema, root), root=root, path=path)

    errors: list[str] = []
    # Evaluate combinators before the object/array fast paths below.  The
    # contract schemas use oneOf for a deliberately supported legacy shape and
    # for scalar-or-list values such as allowed_use.
    if 'allOf' in schema:
        for option in schema['allOf']:
            errors.extend(validate_json_schema(instance, option, root=root, path=path))
    if 'anyOf' in schema:
        attempts = [validate_json_schema(instance, option, root=root, path=path) for option in schema['anyOf']]
        if not any(not attempt for attempt in attempts):
            errors.append(f'{path}: did not match any anyOf branch')
    if 'oneOf' in schema:
        attempts = [validate_json_schema(instance, option, root=root, path=path) for option in schema['oneOf']]
        matching = sum(not attempt for attempt in attempts)
        if matching != 1:
            errors.append(f'{path}: did not match exactly one oneOf branch')
            if matching == 0:
                # Preserve the useful missing/type diagnostics from each
                # branch; fail-closed callers must be able to identify which
                # required field was absent.
                for attempt in attempts:
                    errors.extend(attempt)
    if 'not' in schema and not validate_json_schema(instance, schema['not'], root=root, path=path):
        errors.append(f'{path}: matched prohibited schema')
    if errors:
        # Combinators are assertions in addition to any type/properties below;
        # retain their diagnostics while continuing to report strict fields.
        combinator_errors = list(errors)
    else:
        combinator_errors = []
    if 'const' in schema and instance != schema['const']:
        combinator_errors.append(f"{path}: expected const {schema['const']!r}, got {instance!r}")
        return combinator_errors
    if 'enum' in schema and instance not in schema['enum']:
        combinator_errors.append(f"{path}: expected one of {schema['enum']!r}, got {instance!r}")
        return combinator_errors
    schema_type = schema.get('type')
    if schema_type and not _type_ok(instance, schema_type):
        combinator_errors.append(f"{path}: expected {schema_type}, got {type(instance).__name__}")
        return combinator_errors
    if schema_type == 'string':
        min_length = schema.get('minLength')
        if min_length is not None and len(instance) < min_length:
            combinator_errors.append(f"{path}: expected minLength {min_length}, got {len(instance)}")
        pattern = schema.get('pattern')
        if pattern and not re.match(pattern, instance):
            combinator_errors.append(f"{path}: value {instance!r} does not match {pattern!r}")
        return combinator_errors
    if schema_type == 'integer':
        minimum = schema.get('minimum')
        if minimum is not None and instance < minimum:
            combinator_errors.append(f"{path}: expected minimum {minimum}, got {instance}")
        maximum = schema.get('maximum')
        if maximum is not None and instance > maximum:
            combinator_errors.append(f"{path}: expected maximum {maximum}, got {instance}")
        return combinator_errors
    if schema_type == 'array':
        min_items = schema.get('minItems')
        if min_items is not None and len(instance) < min_items:
            combinator_errors.append(f"{path}: expected at least {min_items} items, got {len(instance)}")
        item_schema = schema.get('items')
        if item_schema:
            for idx, item in enumerate(instance):
                combinator_errors.extend(validate_json_schema(item, item_schema, root=root, path=f'{path}[{idx}]'))
        return combinator_errors
    if schema_type == 'object':
        required = schema.get('required', [])
        for key in required:
            if key not in instance:
                combinator_errors.append(f"{path}: missing required key {key!r}")
        properties = schema.get('properties', {})
        for key, value in instance.items():
            if key in properties:
                combinator_errors.extend(validate_json_schema(value, properties[key], root=root, path=f'{path}.{key}'))
            elif schema.get('additionalProperties', True) is False:
                combinator_errors.append(f"{path}: unexpected key {key!r}")
        return combinator_errors
    return combinator_errors

def validate_named_contract(name: str, payload: Any) -> list[str]:
    name = CONTRACT_ALIASES.get(name, name)
    schema = load_named_schema(name)
    errors = validate_json_schema(payload, schema)
    # Shape/type validation is supplemented by fail-closed semantic checks.
    if name == 'evidence-envelope' and isinstance(payload, dict) and 'source_id' in payload:
        if payload.get('pii_class') == 'unknown':
            errors.append('$.pii_class: unknown classification fails closed')
        if str(payload.get('license_or_entitlement', '')).lower() in {'unknown', 'unverified'}:
            errors.append('$.license_or_entitlement: unknown entitlement fails closed')
        freshness = payload.get('freshness')
        if (isinstance(freshness, dict) and freshness.get('status') in {'unknown', 'expired', 'stale', 'missing'}) or (isinstance(freshness, str) and freshness in {'unknown', 'expired', 'stale', 'missing'}):
            errors.append('$.freshness: non-fresh evidence fails closed')
    return errors


def read_parent_artifact(artifact: Any, contract: str) -> dict[str, Any]:
    """Read one contract from a local parent artifact without network access."""
    from .contracts import _from_artifact

    canonical = CONTRACT_ALIASES.get(contract, contract)
    return _from_artifact(artifact, canonical.replace('-', '_'))


def validate_parent_artifact(artifact: Any, contract: str) -> list[str]:
    """Validate a contract nested in a local parent artifact."""
    canonical = CONTRACT_ALIASES.get(contract, contract)
    return validate_named_contract(canonical, read_parent_artifact(artifact, canonical))

def validate_role_manifest_catalog() -> list[str]:
    errors: list[str] = []
    if not ROLE_MANIFEST_DIR.is_dir():
        return [f'missing role manifest directory: {ROLE_MANIFEST_DIR}']
    paths = sorted(ROLE_MANIFEST_DIR.glob('*.json'))
    if len(paths) != len(EXPECTED_ROLE_MANIFEST_ROLES):
        errors.append(f'expected {len(EXPECTED_ROLE_MANIFEST_ROLES)} role manifests, found {len(paths)}')
    seen_roles: set[str] = set()
    for path in paths:
        payload = load_manifest(path)
        errors.extend(validate_named_contract('role-manifest', payload))
        role = payload.get('role')
        if role in seen_roles:
            errors.append(f'duplicate role manifest for {role!r}')
        seen_roles.add(role)
    missing = [role for role in EXPECTED_ROLE_MANIFEST_ROLES if role not in seen_roles]
    if missing:
        errors.append(f"missing role manifests: {', '.join(missing)}")
    return errors

def validate_catalog() -> list[str]:
    return validate_role_manifest_catalog()

def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('contract', choices=sorted(CONTRACT_FILES), nargs='?', default='role-manifest', help='Named contract to validate (default: role-manifest catalog).')
    parser.add_argument('--path', type=Path, help='Optional path to a single JSON payload to validate instead of the catalog.')
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.path is not None:
        payload = load_json(args.path)
        errors = validate_named_contract(args.contract, payload)
    elif args.contract == 'role-manifest':
        errors = validate_role_manifest_catalog()
    else:
        parser.error('--path is required for single-contract validation')
        return 2

    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    print(f'{args.contract} validation passed')
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
