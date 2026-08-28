"""
Motet - Keycloak Organization Bootstrap

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Utility script that shells into the Keycloak container via docker-compose
    and uses `kcadm.sh` to ensure canonical organizations exist inside the
    imported `motet` realm. Intended to seed demo tenants such as `demo-org`
    (used by the demo chat UI) and `motet-global` (the cross-tenant operator
    scope) without manually clicking through the admin console.

Dependencies:
    - Python 3.11+
    - docker-compose CLI available on PATH
    - Running `keycloak` service defined in docker-compose.distributed.yml

Usage:
    python docker/keycloak/bootstrap_orgs.py \
        --compose-file docker-compose.distributed.yml \
        --realm motet

    # Add additional organizations
    python docker/keycloak/bootstrap_orgs.py \
        --org demo-org:"Demo Org":"Sample tenant" \
        --org motet-global:"Motet Global":"Cross-tenant operators"

Notes:
    - The script is idempotent: existing organizations are detected via the
      Keycloak Admin REST API before any create calls.
    - Credentials default to KEYCLOAK_ADMIN / KEYCLOAK_ADMIN_PASSWORD env vars.
    - All commands are executed inside the running Keycloak container using
      `docker-compose exec -T keycloak /opt/keycloak/bin/kcadm.sh`.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

COMPOSE_BINARY_DEFAULT = os.getenv("DOCKER_COMPOSE_BIN", "docker compose")
DIRECT_KCADM_PATH_DEFAULT = os.getenv(
    "KC_BOOTSTRAP_KCADM", "/opt/keycloak/bin/kcadm.sh"
)

USE_DIRECT_KCADM = False
DIRECT_KCADM_PATH = DIRECT_KCADM_PATH_DEFAULT
COMPOSE_COMMAND: List[str] = shlex.split(COMPOSE_BINARY_DEFAULT)


@dataclass(frozen=True)
class OrganizationPlan:
    name: str
    display_name: str
    description: Optional[str] = None
    member_emails: List[str] = field(default_factory=list)


def run_command(cmd: List[str], *, capture_output: bool = False) -> str:
    """Run a shell command and return stdout when requested."""
    max_attempts = 8
    retry_delay_seconds = 2.0

    for attempt in range(1, max_attempts + 1):
        try:
            result = subprocess.run(
                cmd,
                check=True,
                capture_output=True,
                text=True,
            )
            stdout = result.stdout.strip() if result.stdout else ""
            stderr = result.stderr.strip() if result.stderr else ""

            # Preserve some visibility when callers don't request captured output.
            if not capture_output:
                if stdout:
                    print(stdout)
                if stderr:
                    print(stderr)
                return ""

            return stdout
        except subprocess.CalledProcessError as exc:
            stdout = exc.stdout.strip() if exc.stdout else ""
            stderr = exc.stderr.strip() if exc.stderr else ""
            combined_output = "\n".join([part for part in (stdout, stderr) if part])

            # kcadm sometimes fails with "Cannot parse the JSON [unknown_error]" when Keycloak returns
            # a non-JSON error payload (typically during brief internal transitions). This is usually
            # transient; retrying makes the bootstrap job robust across cold starts.
            retryable_parse_error = "Cannot parse the JSON" in combined_output
            is_last_attempt = attempt >= max_attempts
            if retryable_parse_error and not is_last_attempt:
                print(
                    f"⚠️  kcadm returned non-JSON error payload; retrying ({attempt}/{max_attempts})..."
                )
                time.sleep(retry_delay_seconds)
                continue

            raise RuntimeError(
                f"Command failed: {' '.join(cmd)}\n{combined_output}"
            ) from exc


def _is_transient_kcadm_error(error: RuntimeError) -> bool:
    """Return True when a kcadm error looks like a transient Keycloak startup issue."""
    message = str(error)
    retry_markers = (
        "Cannot parse the JSON",
        "unknown_error",
        "Connection refused",
        "Connection reset",
        "Connection aborted",
        "EOF",
        "502 Bad Gateway",
        "503 Service Unavailable",
        "504 Gateway Timeout",
    )
    return any(marker in message for marker in retry_markers)


def wait_for_kcadm_json(
    compose_file: str,
    args: List[str],
    *,
    description: str,
    max_attempts: int = 15,
    retry_delay_seconds: float = 2.0,
) -> Any:
    """Poll a kcadm JSON command until it returns a parseable response."""
    for attempt in range(1, max_attempts + 1):
        try:
            payload = run_kcadm_json(compose_file, args)
        except RuntimeError as exc:
            if _is_transient_kcadm_error(exc) and attempt < max_attempts:
                print(
                    f"⏳ {description} not ready; retrying ({attempt}/{max_attempts})..."
                )
                time.sleep(retry_delay_seconds)
                continue
            raise

        if payload is not None:
            return payload

        if attempt < max_attempts:
            print(
                f"⏳ {description} not ready; retrying ({attempt}/{max_attempts})..."
            )
            time.sleep(retry_delay_seconds)

    raise RuntimeError(f"{description} did not become ready after {max_attempts} attempts")


def wait_for_realm_ready(compose_file: str, realm: str) -> Dict[str, Any]:
    """Wait until the target realm is visible via the admin API."""
    realm_cfg = wait_for_kcadm_json(
        compose_file,
        ["get", f"realms/{realm}"],
        description=f"Realm '{realm}'",
    )
    if not isinstance(realm_cfg, dict) or realm_cfg.get("realm") != realm:
        raise RuntimeError(f"Realm '{realm}' returned unexpected payload: {realm_cfg}")
    return realm_cfg


def build_kcadm_command(compose_file: str, kcadm_args: List[str]) -> List[str]:
    """Return the command used to invoke kcadm either directly or via compose."""
    if USE_DIRECT_KCADM:
        return [DIRECT_KCADM_PATH, *kcadm_args]

    return [
        *COMPOSE_COMMAND,
        "-f",
        compose_file,
        "exec",
        "-T",
        "keycloak",
        "/opt/keycloak/bin/kcadm.sh",
        *kcadm_args,
    ]


def login(
    compose_file: str,
    server: str,
    admin_realm: str,
    username: str,
    password: str,
) -> None:
    """Authenticate kcadm inside the container."""
    cmd = build_kcadm_command(
        compose_file,
        [
            "config",
            "credentials",
            "--server",
            server,
            "--realm",
            admin_realm,
            "--user",
            username,
            "--password",
            password,
        ],
    )
    run_command(cmd)


def ensure_organizations_enabled(compose_file: str, realm: str) -> None:
    """Enable Organizations for a realm when the realm-level toggle is off.

    Even if the Keycloak server is started with `--features=organization`, the realm may still have
    `organizationsEnabled=false`, which makes the Organizations Admin API unavailable.
    """
    realm_cfg = run_kcadm_json(compose_file, ["get", f"realms/{realm}"]) or {}
    if realm_cfg.get("organizationsEnabled") is True:
        return

    print(f"🔧 Enabling Organizations feature for realm '{realm}'")
    cmd = build_kcadm_command(
        compose_file,
        [
            "update",
            f"realms/{realm}",
            "-s",
            "organizationsEnabled=true",
        ],
    )
    run_command(cmd)


def run_kcadm_json(compose_file: str, args: List[str]) -> Any:
    """Execute kcadm with JSON output."""
    cmd = build_kcadm_command(compose_file, args)
    output = run_command(cmd, capture_output=True)
    if not output:
        return None
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        return None


def fetch_existing_orgs(compose_file: str, realm: str, search: str) -> List[dict]:
    """Return Keycloak organizations matching the search string."""
    cmd = build_kcadm_command(
        compose_file,
        [
            "get",
            "organizations",
            "-r",
            realm,
            "-q",
            f"search={search}",
        ],
    )
    output = run_command(cmd, capture_output=True)
    try:
        data = json.loads(output or "[]")
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


def ensure_organization(compose_file: str, realm: str, plan: OrganizationPlan) -> None:
    """Create the organization when it does not exist yet."""
    try:
        existing = fetch_existing_orgs(compose_file, realm, plan.name)
        org_api_available = True
    except RuntimeError as exc:
        if _organizations_api_missing(exc):
            _ensure_group_fallback(compose_file, realm, plan)
            return
        raise

    if not org_api_available:
        return

    org = _select_org(existing, plan.name)
    org_id: Optional[str] = None
    if org:
        org_id = org.get("id")
        print(f"✔ Organization '{plan.name}' already present (id={org_id})")
    else:
        org_id = _create_organization_via_api(compose_file, realm, plan)

    if org_id:
        _ensure_org_attributes(compose_file, realm, org_id, plan)

    ensure_group_path(compose_file, realm, "/orgs")
    tenant_group_id = ensure_group_path(compose_file, realm, f"/orgs/{plan.name}")
    print(f"✔ Ensured group-backed tenant '/orgs/{plan.name}'")

    if plan.member_emails:
        _ensure_plan_members(
            compose_file=compose_file,
            realm=realm,
            org_id=org_id,
            tenant_group_id=tenant_group_id,
            tenant_group_path=f"/orgs/{plan.name}",
            member_emails=plan.member_emails,
        )


def ensure_organization_scope(compose_file: str, realm: str, client_id: str) -> None:
    """
    Ensure the `organization` client scope exists, has the membership mapper,
    and is attached to the Motet demo client + realm defaults so OAuth tokens
    emit the canonical organization claim.
    """
    scope = _find_client_scope(compose_file, realm, "organization")
    scope_id = scope.get("id") if scope else None

    if not scope_id:
        print("➕ Creating 'organization' client scope")
        create_cmd = build_kcadm_command(
            compose_file,
            [
                "create",
                "client-scopes",
                "-r",
                realm,
                "-s",
                "name=organization",
                "-s",
                "description=Organization metadata claim",
                "-s",
                "protocol=openid-connect",
                "-s",
                "attributes.\"include.in.token.scope\"=true",
                "-s",
                "attributes.\"display.on.consent.screen\"=false",
            ],
        )
        run_command(create_cmd)
        scope = _find_client_scope(compose_file, realm, "organization")
        scope_id = scope.get("id") if scope else None

    if not scope_id:
        raise RuntimeError("Unable to create or locate 'organization' client scope")

    _ensure_org_scope_mapper(compose_file, realm, scope_id)
    _ensure_scope_in_client(compose_file, realm, client_id, "organization")
    _ensure_scope_in_realm_defaults(compose_file, realm, "organization")
    print("✔ 'organization' client scope ensured")


def ensure_audience_scope(
    compose_file: str,
    realm: str,
    client_id: str,
    audience: str,
) -> None:
    """
    Ensure the `audience` client scope exists and includes a mapper so access tokens
    carry the configured audience (aud) claim for the Motet client.
    """
    scope = _find_client_scope(compose_file, realm, "audience")
    scope_id = scope.get("id") if scope else None

    if not scope_id:
        print("➕ Creating 'audience' client scope")
        create_cmd = build_kcadm_command(
            compose_file,
            [
                "create",
                "client-scopes",
                "-r",
                realm,
                "-s",
                "name=audience",
                "-s",
                "description=Audience claim for access tokens",
                "-s",
                "protocol=openid-connect",
                "-s",
                "attributes.\"include.in.token.scope\"=true",
                "-s",
                "attributes.\"display.on.consent.screen\"=false",
            ],
        )
        run_command(create_cmd)
        scope = _find_client_scope(compose_file, realm, "audience")
        scope_id = scope.get("id") if scope else None

    if not scope_id:
        raise RuntimeError("Unable to create or locate 'audience' client scope")

    _ensure_audience_scope_mapper(compose_file, realm, scope_id, audience)
    _ensure_scope_in_client(compose_file, realm, client_id, "audience")
    print("✔ 'audience' client scope ensured")


def _ensure_audience_scope_mapper(
    compose_file: str,
    realm: str,
    scope_id: str,
    audience: str,
) -> None:
    """
    Ensure the OIDC audience mapper exists on the client scope.
    """
    mappers = wait_for_kcadm_json(
        compose_file,
        [
            "get",
            f"client-scopes/{scope_id}/protocol-mappers/models",
            "-r",
            realm,
        ],
        description=f"Protocol mappers for client scope {scope_id}",
    )
    mapper_list: List[Dict[str, Any]] = mappers if isinstance(mappers, list) else []
    mapper = next(
        (
            candidate
            for candidate in mapper_list
            if candidate.get("protocolMapper") == "oidc-audience-mapper"
        ),
        None,
    )

    desired_config = {
        "included.client.audience": audience,
        "id.token.claim": "false",
        "access.token.claim": "true",
    }

    if mapper:
        mapper_id = mapper.get("id")
        needs_update = any(
            mapper.get("config", {}).get(k) != v for k, v in desired_config.items()
        )
        if not needs_update:
            return
        print("♻ Updating audience mapper configuration")
        cmd = build_kcadm_command(
            compose_file,
            [
                "update",
                f"client-scopes/{scope_id}/protocol-mappers/models/{mapper_id}",
                "-r",
                realm,
            ]
            + [
                item
                for kv in desired_config.items()
                for item in ("-s", f'config."{kv[0]}"={kv[1]}')
            ],
        )
        try:
            run_command(cmd)
            return
        except RuntimeError as exc:
            print(
                f"⚠️  Mapper update failed ({exc}); deleting and recreating mapper instead"
            )
            delete_cmd = build_kcadm_command(
                compose_file,
                [
                    "delete",
                    f"client-scopes/{scope_id}/protocol-mappers/models/{mapper_id}",
                    "-r",
                    realm,
                ],
            )
            run_command(delete_cmd)

    print("➕ Adding audience mapper to scope")
    cmd = build_kcadm_command(
        compose_file,
        [
            "create",
            f"client-scopes/{scope_id}/protocol-mappers/models",
            "-r",
            realm,
            "-s",
            "name=audience",
            "-s",
            "protocol=openid-connect",
            "-s",
            "protocolMapper=oidc-audience-mapper",
        ]
        + [
            item
            for kv in desired_config.items()
            for item in ("-s", f'config."{kv[0]}"={kv[1]}')
        ],
    )
    run_command(cmd)


def ensure_subject_mapper(compose_file: str, realm: str, client_id: str) -> None:
    """
    Ensure the access token contains a stable subject (sub) claim by mapping the
    Keycloak user ID into the access token for the specified client.
    """
    clients = run_kcadm_json(
        compose_file,
        [
            "get",
            "clients",
            "-r",
            realm,
            "-q",
            f"clientId={client_id}",
        ],
    )
    if not isinstance(clients, list) or not clients:
        raise RuntimeError(f"Client '{client_id}' not found in realm {realm}")
    client_uuid = clients[0].get("id")

    mappers = wait_for_kcadm_json(
        compose_file,
        [
            "get",
            f"clients/{client_uuid}/protocol-mappers/models",
            "-r",
            realm,
        ],
        description=f"Protocol mappers for client {client_id}",
    )
    mapper_list: List[Dict[str, Any]] = mappers if isinstance(mappers, list) else []
    mapper = next(
        (
            candidate
            for candidate in mapper_list
            if candidate.get("name") == "subject"
            or candidate.get("config", {}).get("claim.name") == "sub"
        ),
        None,
    )

    desired_config = {
        "user.attribute": "id",
        "claim.name": "sub",
        "jsonType.label": "String",
        "id.token.claim": "true",
        "access.token.claim": "true",
        "userinfo.token.claim": "true",
        "introspection.token.claim": "true",
    }

    if mapper:
        mapper_id = mapper.get("id")
        needs_update = any(
            mapper.get("config", {}).get(k) != v for k, v in desired_config.items()
        )
        if not needs_update:
            return
        print("♻ Updating subject mapper configuration")
        config_args = [
            item
            for kv in desired_config.items()
            for item in (
                "-s",
                (
                    f'config."{kv[0]}"={kv[1]}'
                    if "." in kv[0]
                    else f"config.{kv[0]}={kv[1]}"
                ),
            )
        ]
        cmd = build_kcadm_command(
            compose_file,
            [
                "update",
                f"clients/{client_uuid}/protocol-mappers/models/{mapper_id}",
                "-r",
                realm,
                "-s",
                "name=subject",
                "-s",
                "protocol=openid-connect",
                "-s",
                "protocolMapper=oidc-usermodel-property-mapper",
            ]
            + config_args,
        )
        try:
            run_command(cmd)
            return
        except RuntimeError as exc:
            print(
                f"⚠️  Subject mapper update failed ({exc}); deleting and recreating mapper instead"
            )
            delete_cmd = build_kcadm_command(
                compose_file,
                [
                    "delete",
                    f"clients/{client_uuid}/protocol-mappers/models/{mapper_id}",
                    "-r",
                    realm,
                ],
            )
            run_command(delete_cmd)

    print("➕ Adding subject mapper to client")
    config_args = [
        item
        for kv in desired_config.items()
        for item in (
            "-s",
            (
                f'config."{kv[0]}"={kv[1]}'
                if "." in kv[0]
                else f"config.{kv[0]}={kv[1]}"
            ),
        )
    ]
    cmd = build_kcadm_command(
        compose_file,
        [
            "create",
            f"clients/{client_uuid}/protocol-mappers/models",
            "-r",
            realm,
            "-s",
            "name=subject",
            "-s",
            "protocol=openid-connect",
            "-s",
            "protocolMapper=oidc-usermodel-property-mapper",
        ]
        + config_args,
    )
    run_command(cmd)


def ensure_roles_mapper(compose_file: str, realm: str, client_id: str) -> None:
    """
    Ensure realm roles are included in tokens under the `roles` claim.
    """
    clients = run_kcadm_json(
        compose_file,
        [
            "get",
            "clients",
            "-r",
            realm,
            "-q",
            f"clientId={client_id}",
        ],
    )
    if not isinstance(clients, list) or not clients:
        raise RuntimeError(f"Client '{client_id}' not found in realm {realm}")
    client_uuid = clients[0].get("id")

    mappers = wait_for_kcadm_json(
        compose_file,
        [
            "get",
            f"clients/{client_uuid}/protocol-mappers/models",
            "-r",
            realm,
        ],
        description=f"Protocol mappers for client {client_id}",
    )
    mapper_list: List[Dict[str, Any]] = mappers if isinstance(mappers, list) else []
    mapper = next(
        (
            candidate
            for candidate in mapper_list
            if candidate.get("name") == "roles"
            or candidate.get("config", {}).get("claim.name") == "roles"
        ),
        None,
    )

    desired_config = {
        "multivalued": "true",
        "claim.name": "roles",
        "jsonType.label": "String",
        "access.token.claim": "true",
        "id.token.claim": "true",
        "userinfo.token.claim": "true",
        "introspection.token.claim": "true",
        "role.prefix": "",
    }
    config_args = [
        item
        for kv in desired_config.items()
        for item in (
            "-s",
            (
                f'config."{kv[0]}"={kv[1]}'
                if "." in kv[0]
                else f"config.{kv[0]}={kv[1]}"
            ),
        )
    ]

    if mapper:
        mapper_id = mapper.get("id")
        needs_update = any(
            mapper.get("config", {}).get(k) != v for k, v in desired_config.items()
        )
        if not needs_update:
            return
        print("♻ Updating roles mapper configuration")
        cmd = build_kcadm_command(
            compose_file,
            [
                "update",
                f"clients/{client_uuid}/protocol-mappers/models/{mapper_id}",
                "-r",
                realm,
                "-s",
                "name=roles",
                "-s",
                "protocol=openid-connect",
                "-s",
                "protocolMapper=oidc-usermodel-realm-role-mapper",
            ]
            + config_args,
        )
        try:
            run_command(cmd)
            return
        except RuntimeError as exc:
            print(
                f"⚠️  Roles mapper update failed ({exc}); deleting and recreating mapper instead"
            )
            delete_cmd = build_kcadm_command(
                compose_file,
                [
                    "delete",
                    f"clients/{client_uuid}/protocol-mappers/models/{mapper_id}",
                    "-r",
                    realm,
                ],
            )
            run_command(delete_cmd)

    print("➕ Adding roles mapper to client")
    cmd = build_kcadm_command(
        compose_file,
        [
            "create",
            f"clients/{client_uuid}/protocol-mappers/models",
            "-r",
            realm,
            "-s",
            "name=roles",
            "-s",
            "protocol=openid-connect",
            "-s",
            "protocolMapper=oidc-usermodel-realm-role-mapper",
        ]
        + config_args,
    )
    run_command(cmd)


def _find_client_scope(compose_file: str, realm: str, name: str) -> Optional[Dict[str, Any]]:
    scopes = run_kcadm_json(
        compose_file,
        [
            "get",
            "client-scopes",
            "-r",
            realm,
        ],
    )
    if isinstance(scopes, list):
        for scope in scopes:
            if scope.get("name") == name:
                return scope
    return None


def _ensure_org_scope_mapper(compose_file: str, realm: str, scope_id: str) -> None:
    """
    Ensure the OIDC organization membership mapper exists on the client scope.

    IMPORTANT:
        Keycloak stores protocol mapper config keys with dots (e.g. "claim.name")
        inside a flat string map. When using `kcadm.sh`, those keys must be sent
        as `config."claim.name"=...` (quoted) or Keycloak interprets them as
        nested JSON objects, which can lead to non-JSON error responses and
        `kcadm.sh` emitting `Cannot parse the JSON [unknown_error]`.
    """

    mappers = wait_for_kcadm_json(
        compose_file,
        [
            "get",
            f"client-scopes/{scope_id}/protocol-mappers/models",
            "-r",
            realm,
        ],
        description=f"Protocol mappers for client scope {scope_id}",
    )
    mapper_list: List[Dict[str, Any]] = mappers if isinstance(mappers, list) else []
    mapper = next(
        (
            candidate
            for candidate in mapper_list
            if candidate.get("protocolMapper") == "oidc-organization-membership-mapper"
        ),
        None,
    )

    # NOTE: protocol mapper config keys in Keycloak are literal strings like "claim.name".
    # When using kcadm, keys containing dots must be quoted (e.g. config."claim.name"=...),
    # otherwise they are interpreted as nested objects and Keycloak may return non-JSON errors.
    desired_config = {
        "claim.name": "organization",
        "jsonType.label": "JSON",
        "multivalued": "true",
        "id.token.claim": "true",
        "access.token.claim": "true",
        "userinfo.token.claim": "true",
        "introspection.token.claim": "true",
        "addOrganizationAttributes": "true",
        "addOrganizationId": "true",
    }

    def _config_cli_args(config: Dict[str, str]) -> List[str]:
        args: List[str] = []
        for key, value in config.items():
            # Keycloak expects a flat config map with dotted keys.
            args.extend(["-s", f'config."{key}"={value}'])
        return args

    if mapper:
        mapper_id = mapper.get("id")
        needs_update = any(
            mapper.get("config", {}).get(k) != v for k, v in desired_config.items()
        )
        if not needs_update:
            return
        print("♻ Updating organization membership mapper configuration")
        cmd = build_kcadm_command(
            compose_file,
            [
                "update",
                f"client-scopes/{scope_id}/protocol-mappers/models/{mapper_id}",
                "-r",
                realm,
            ]
            + [
                item
                for kv in desired_config.items()
                for item in ("-s", f'config."{kv[0]}"={kv[1]}')
            ],
        )
        try:
            run_command(cmd)
            return
        except RuntimeError as exc:
            print(
                f"⚠️  Mapper update failed ({exc}); deleting and recreating mapper instead"
            )
            delete_cmd = build_kcadm_command(
                compose_file,
                [
                    "delete",
                    f"client-scopes/{scope_id}/protocol-mappers/models/{mapper_id}",
                    "-r",
                    realm,
                ],
            )
            run_command(delete_cmd)

    print("➕ Adding organization membership mapper to scope")
    cmd = build_kcadm_command(
        compose_file,
        [
            "create",
            f"client-scopes/{scope_id}/protocol-mappers/models",
            "-r",
            realm,
            "-s",
            "name=organization",
            "-s",
            "protocol=openid-connect",
            "-s",
            "protocolMapper=oidc-organization-membership-mapper",
        ]
        + [
            item
            for kv in desired_config.items()
            for item in ("-s", f'config."{kv[0]}"={kv[1]}')
        ],
    )
    run_command(cmd)

    # Verify that the mapper is now present with the expected configuration.
    refreshed = wait_for_kcadm_json(
        compose_file,
        [
            "get",
            f"client-scopes/{scope_id}/protocol-mappers/models",
            "-r",
            realm,
        ],
        description=f"Protocol mappers for client scope {scope_id}",
    )
    refreshed_list: List[Dict[str, Any]] = (
        refreshed if isinstance(refreshed, list) else []
    )
    created = next(
        (
            candidate
            for candidate in refreshed_list
            if candidate.get("protocolMapper") == "oidc-organization-membership-mapper"
        ),
        None,
    )
    if not created:
        raise RuntimeError(
            "Organization membership mapper was not created; "
            "Keycloak may still be initializing."
        )


def _ensure_scope_in_client(
    compose_file: str, realm: str, client_id: str, scope_name: str
) -> None:
    clients = run_kcadm_json(
        compose_file,
        [
            "get",
            "clients",
            "-r",
            realm,
            "-q",
            f"clientId={client_id}",
        ],
    )
    if not isinstance(clients, list) or not clients:
        raise RuntimeError(f"Client '{client_id}' not found in realm {realm}")
    client = clients[0]
    client_uuid = client.get("id")
    scope = _find_client_scope(compose_file, realm, scope_name)
    if not scope:
        raise RuntimeError(f"Client scope '{scope_name}' not found in realm {realm}")
    scope_id = scope.get("id")

    existing = run_kcadm_json(
        compose_file,
        [
            "get",
            f"clients/{client_uuid}/default-client-scopes",
            "-r",
            realm,
        ],
    )
    if isinstance(existing, list) and any(
        scope_name == item.get("name") or scope_id == item.get("id") for item in existing
    ):
        return

    print(f"➕ Attaching '{scope_name}' scope to client '{client_id}'")
    cmd = build_kcadm_command(
        compose_file,
        [
            "update",
            f"clients/{client_uuid}/default-client-scopes/{scope_id}",
            "-r",
            realm,
            "-b",
            "{}",
        ],
    )
    run_command(cmd)


def _ensure_scope_in_realm_defaults(
    compose_file: str, realm: str, scope_name: str
) -> None:
    realm_cfg = run_kcadm_json(
        compose_file,
        [
            "get",
            f"realms/{realm}",
        ],
    ) or {}
    defaults = realm_cfg.get("defaultDefaultClientScopes") or []
    if scope_name in defaults:
        return
    print(f"➕ Adding '{scope_name}' to realm default client scopes")
    cmd = build_kcadm_command(
        compose_file,
        [
            "update",
            f"realms/{realm}",
            "-s",
            f"defaultDefaultClientScopes+={scope_name}",
        ],
    )
    run_command(cmd)

def _select_org(orgs: List[dict], name: str) -> Optional[dict]:
    for org in orgs:
        if org.get("name") == name or org.get("alias") == name:
            return org
    return None


def _create_organization_via_api(
    compose_file: str, realm: str, plan: OrganizationPlan
) -> Optional[str]:
    print(f"➕ Creating organization '{plan.name}' via API")
    args = [
        "create",
        "organizations",
        "-r",
        realm,
        "-s",
        f"name={plan.name}",
        "-s",
        f"alias={plan.name}",
        "-s",
        "enabled=true",
    ]
    attribute_fields = {}
    if plan.display_name:
        attribute_fields["displayName"] = plan.display_name
    if plan.description:
        attribute_fields["description"] = plan.description
    for key, value in attribute_fields.items():
        args += ["-s", f"attributes.{key}={json.dumps([value])}"]

    cmd = build_kcadm_command(compose_file, args)
    try:
        run_command(cmd)
    except RuntimeError as exc:
        print(
            f"✖ Failed to create organization '{plan.name}' via API: {exc}",
            file=sys.stderr,
        )
        return None

    refreshed = fetch_existing_orgs(compose_file, realm, plan.name)
    org = _select_org(refreshed, plan.name)
    return org.get("id") if org else None


def _ensure_org_attributes(
    compose_file: str, realm: str, org_id: str, plan: OrganizationPlan
) -> None:
    attributes_to_set = {}
    if plan.display_name:
        attributes_to_set["displayName"] = plan.display_name
    if plan.description:
        attributes_to_set["description"] = plan.description

    if not attributes_to_set:
        return

    current = _fetch_organization(compose_file, realm, org_id)
    current_attrs = current.get("attributes") or {}
    updates_needed: List[str] = []

    for key, value in attributes_to_set.items():
        desired = [value]
        existing = current_attrs.get(key)
        if existing != desired:
            updates_needed += ["-s", f"attributes.{key}={json.dumps(desired)}"]

    if not updates_needed:
        return

    cmd = build_kcadm_command(
        compose_file,
        [
            "update",
            f"organizations/{org_id}",
            "-r",
            realm,
            *updates_needed,
        ],
    )
    run_command(cmd)
    print(f"✔ Updated attributes for organization '{plan.name}'")


def _fetch_organization(compose_file: str, realm: str, org_id: str) -> dict:
    cmd = build_kcadm_command(
        compose_file,
        [
            "get",
            f"organizations/{org_id}",
            "-r",
            realm,
        ],
    )
    output = run_command(cmd, capture_output=True)
    try:
        data = json.loads(output or "{}")
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}

def _organizations_api_missing(error: RuntimeError) -> bool:
    """Detect when the Keycloak build lacks the Organizations preview feature."""
    text = str(error)
    return "Resource not found" in text or "unrecognized feature" in text


def _ensure_group_fallback(compose_file: str, realm: str, plan: OrganizationPlan) -> None:
    """
    When the Organizations feature is unavailable, emulate the hierarchy via groups:
    /orgs/<slug>. This preserves compatibility with the org_hierarchy mappers.
    """
    print(
        f"⚠️  Organizations API unavailable. Falling back to group hierarchy for '{plan.name}'."
    )
    ensure_group_path(compose_file, realm, "/orgs")
    tenant_group_id = ensure_group_path(compose_file, realm, f"/orgs/{plan.name}")
    print(f"✔ Ensured group-backed tenant '/orgs/{plan.name}'")

    if plan.member_emails:
        _ensure_plan_members(
            compose_file=compose_file,
            realm=realm,
            org_id=None,
            tenant_group_id=tenant_group_id,
            tenant_group_path=f"/orgs/{plan.name}",
            member_emails=plan.member_emails,
        )


def _ensure_plan_members(
    *,
    compose_file: str,
    realm: str,
    org_id: Optional[str],
    tenant_group_id: str,
    tenant_group_path: str,
    member_emails: List[str],
) -> None:
    """Ensure users are associated to the tenant group backing the organization.

    We model tenancy via group paths under `/orgs/<slug>`. While Keycloak Organizations provides
    a members API, adding existing realm users via that endpoint is not consistently supported
    across configurations. Group membership is the canonical, idempotent binding for the Motet stack.
    """
    for email in member_emails:
        user = _find_user_by_email(compose_file, realm, email)
        if not user:
            print(
                f"⚠️  User with email '{email}' not found in realm '{realm}'; skipping membership",
                file=sys.stderr,
            )
            continue
        user_id = user.get("id")
        if not user_id:
            continue

        # Prefer real Organization membership when possible (Keycloak 26.0.6+ expects the
        # member-add body to be the raw user id string, not an object).
        if org_id:
            _ensure_user_in_organization(
                compose_file=compose_file,
                realm=realm,
                org_id=org_id,
                user_id=user_id,
                user_email=email,
            )

        _ensure_user_in_group(
            compose_file=compose_file,
            realm=realm,
            user_id=user_id,
            group_id=tenant_group_id,
            group_path=tenant_group_path,
        )


def _ensure_user_in_organization(
    *,
    compose_file: str,
    realm: str,
    org_id: str,
    user_id: str,
    user_email: str,
) -> None:
    """Ensure the user is a member of the Keycloak Organization.

    Per upstream Keycloak discussions, POST /organizations/{id}/members expects the request body
    to be a JSON string containing the user id (e.g. "uuid") rather than an object.
    """
    # If already present, do nothing.
    existing = run_command(
        build_kcadm_command(
            compose_file,
            [
                "get",
                f"organizations/{org_id}/members",
                "-r",
                realm,
                "--fields",
                "id",
            ],
        ),
        capture_output=True,
    )
    try:
        members = json.loads(existing or "[]")
    except json.JSONDecodeError:
        members = []
    if isinstance(members, list) and any(
        isinstance(m, dict) and m.get("id") == user_id for m in members
    ):
        return

    # Add membership using raw string body.
    cmd = build_kcadm_command(
        compose_file,
        [
            "create",
            f"organizations/{org_id}/members",
            "-r",
            realm,
            "-h",
            "Content-Type=application/json",
            "-b",
            json.dumps(user_id),
        ],
    )
    try:
        run_command(cmd)
        print(f"✔ Added '{user_email}' to organization {org_id}")
    except RuntimeError as exc:
        # Don't fail bootstrap; group membership remains the canonical tenancy binding.
        print(
            f"⚠️  Failed to add '{user_email}' to organization {org_id}: {exc}",
            file=sys.stderr,
        )


def _find_user_by_email(compose_file: str, realm: str, email: str) -> Optional[dict]:
    """Return the first user matching the email in the given realm."""
    output = run_command(
        build_kcadm_command(
            compose_file,
            [
                "get",
                "users",
                "-r",
                realm,
                "-q",
                f"email={email}",
            ],
        ),
        capture_output=True,
    )
    try:
        data = json.loads(output or "[]")
        if isinstance(data, list) and data:
            return data[0]
        return None
    except json.JSONDecodeError:
        return None


def _ensure_user_in_group(
    *,
    compose_file: str,
    realm: str,
    user_id: str,
    group_id: str,
    group_path: str,
) -> None:
    """Ensure the given user is a member of the specified group."""
    if not group_id:
        return

    current = run_command(
        build_kcadm_command(
            compose_file,
            [
                "get",
                f"users/{user_id}/groups",
                "-r",
                realm,
                "--fields",
                "id,path",
            ],
        ),
        capture_output=True,
    )
    try:
        groups = json.loads(current or "[]")
    except json.JSONDecodeError:
        groups = []

    if isinstance(groups, list) and any(
        isinstance(g, dict) and g.get("id") == group_id for g in groups
    ):
        return

    # Joining a group is a PUT to /users/{id}/groups/{groupId} with an empty body.
    cmd = build_kcadm_command(
        compose_file,
        [
            "update",
            f"users/{user_id}/groups/{group_id}",
            "-r",
            realm,
            "-b",
            "{}",
        ],
    )
    run_command(cmd)
    print(f"✔ Added user {user_id} to group '{group_path}'")


def ensure_group_path(compose_file: str, realm: str, path: str) -> str:
    """
    Ensure a group path exists (e.g. /orgs/acme). Returns the final group's ID.
    """
    segments = [seg for seg in path.strip("/").split("/") if seg]
    if not segments:
        return ""

    parent_id: Optional[str] = None
    for segment in segments:
        existing = _find_child_group(compose_file, realm, parent_id, segment)
        if existing:
            parent_id = existing["id"]
            continue

        endpoint = "groups" if parent_id is None else f"groups/{parent_id}/children"
        cmd = build_kcadm_command(
            compose_file,
            [
                "create",
                endpoint,
                "-r",
                realm,
                "-s",
                f"name={segment}",
            ],
        )
        run_command(cmd)
        created = _find_child_group(compose_file, realm, parent_id, segment)
        if not created:
            raise RuntimeError(
                f"Failed to create group segment '{segment}' under parent '{parent_id}'."
            )
        parent_id = created["id"]

    return parent_id or ""


def _find_child_group(
    compose_file: str, realm: str, parent_id: Optional[str], name: str
) -> Optional[dict]:
    """Return the first child group matching the name under the given parent."""
    children = _list_child_groups(compose_file, realm, parent_id)
    for group in children:
        if group.get("name") == name:
            return group
    return None


def _list_child_groups(
    compose_file: str, realm: str, parent_id: Optional[str]
) -> List[dict]:
    """List groups at a given level (top-level when parent_id is None)."""
    if parent_id is None:
        cmd = build_kcadm_command(
            compose_file,
            [
                "get",
            "groups",
                "-r",
                realm,
                "--fields",
                "id,name,path",
            ],
        )
        output = run_command(cmd, capture_output=True)
        try:
            data = json.loads(output or "[]")
            return data if isinstance(data, list) else []
        except json.JSONDecodeError:
            return []

    cmd = build_kcadm_command(
        compose_file,
        [
            "get",
            f"groups/{parent_id}/children",
            "-r",
            realm,
            "--fields",
            "id,name,path",
        ],
    )
    output = run_command(cmd, capture_output=True)
    try:
        data = json.loads(output or "[]")
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


def configure_master_realm_ssl(compose_file: str, realm: str = "master") -> None:
    """
    Configure the master realm to not require SSL (for development).
    This fixes the "HTTPS required" error when accessing the admin console.
    """
    try:
        # Get current realm configuration
        current = run_kcadm_json(
            compose_file,
            [
                "get",
                f"realms/{realm}",
            ],
        )
        
        if not current:
            print(f"⚠ Could not fetch realm '{realm}' configuration")
            return
        
        # Check if SSL is already disabled
        if current.get("sslRequired") == "NONE":
            print(f"✔ Realm '{realm}' already has SSL requirement disabled")
            return
        
        # Update realm to disable SSL requirement
        cmd = build_kcadm_command(
            compose_file,
            [
                "update",
                f"realms/{realm}",
                "-s",
                "sslRequired=NONE",
            ],
        )
        run_command(cmd)
        print(f"✔ Disabled SSL requirement for realm '{realm}'")
    except RuntimeError as exc:
        print(f"⚠ Failed to configure master realm SSL: {exc}", file=sys.stderr)
        # Don't fail the entire bootstrap if this doesn't work
        pass


def parse_org_argument(raw: str) -> OrganizationPlan:
    """
    Parse an --org value formatted as slug:Display Name:Description.
    Description is optional and may be omitted.
    """
    parts = raw.split(":")
    if len(parts) < 2:
        raise argparse.ArgumentTypeError(
            "--org expects 'slug:Display Name[:Description]'"
        )
    name = parts[0].strip()
    display_name = parts[1].strip()
    description = parts[2].strip() if len(parts) > 2 else None
    if not name or not display_name:
        raise argparse.ArgumentTypeError("Organization slug and display name are required")
    return OrganizationPlan(name=name, display_name=display_name, description=description)


def default_orgs() -> List[OrganizationPlan]:
    """Predefined tenants used throughout demos and cross-tenant scenarios."""
    return [
        OrganizationPlan(
            name="demo-org",
            display_name="Demo Org",
            description="Default tenant used by the Motet demo chat UI.",
            member_emails=["demo@acme.localhost"],
        ),
        OrganizationPlan(
            name="motet-global",
            display_name="Motet Global",
            description="Cross-tenant operator scope for platform admins.",
            member_emails=["root@motet.localhost"],
        ),
    ]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bootstrap Keycloak organizations for the Motet."
    )
    parser.add_argument(
        "--compose-file",
        default="docker-compose.distributed.yml",
        help="Path to the docker-compose file that defines the keycloak service.",
    )
    parser.add_argument(
        "--server",
        default="http://localhost:8080",
        help="External Keycloak URL used by kcadm (default: %(default)s).",
    )
    parser.add_argument(
        "--admin-realm",
        default="master",
        help="Realm used for administrative login (default: %(default)s).",
    )
    parser.add_argument(
        "--realm",
        default="motet",
        help="Target realm that will own the organizations (default: %(default)s).",
    )
    parser.add_argument(
        "--user",
        default=os.getenv("KEYCLOAK_ADMIN", "admin"),
        help="Keycloak admin username (default: KEYCLOAK_ADMIN env or 'admin').",
    )
    parser.add_argument(
        "--password",
        default=os.getenv("KEYCLOAK_ADMIN_PASSWORD", "admin"),
        help="Keycloak admin password (default: KEYCLOAK_ADMIN_PASSWORD env or 'admin').",
    )
    parser.add_argument(
        "--org",
        action="append",
        type=parse_org_argument,
        help="Additional organization definition formatted as slug:Display[:Description]. "
        "May be provided multiple times.",
    )
    parser.add_argument(
        "--no-compose",
        action="store_true",
        help="Run kcadm directly (useful when already inside the Keycloak container).",
    )
    parser.add_argument(
        "--kcadm-path",
        default=DIRECT_KCADM_PATH_DEFAULT,
        help="Path to kcadm.sh when --no-compose is used (default: %(default)s).",
    )
    parser.add_argument(
        "--compose-binary",
        default=COMPOSE_BINARY_DEFAULT,
        help="docker compose binary to use when shelling into Keycloak (default: %(default)s).",
    )
    parser.add_argument(
        "--skip-scope-setup",
        action="store_true",
        help="Skip ensuring client scopes/mappers (useful when realm import already configured).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    global USE_DIRECT_KCADM, DIRECT_KCADM_PATH, COMPOSE_COMMAND
    USE_DIRECT_KCADM = args.no_compose
    DIRECT_KCADM_PATH = args.kcadm_path
    COMPOSE_COMMAND = shlex.split(args.compose_binary)

    plans = default_orgs()
    if args.org:
        plans.extend(args.org)

    try:
        login(
            compose_file=args.compose_file,
            server=args.server,
            admin_realm=args.admin_realm,
            username=args.user,
            password=args.password,
        )
    except RuntimeError as exc:
        print(f"✖ Failed to authenticate with kcadm: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        wait_for_realm_ready(args.compose_file, args.realm)
    except RuntimeError as exc:
        print(f"✖ Realm '{args.realm}' not ready: {exc}", file=sys.stderr)
        sys.exit(1)

    # Ensure the motet realm has Organizations enabled before any org API calls.
    try:
        ensure_organizations_enabled(args.compose_file, args.realm)
    except RuntimeError as exc:
        print(f"✖ Failed to enable Organizations for realm '{args.realm}': {exc}", file=sys.stderr)
        sys.exit(1)

    # Configure master realm to not require SSL (fixes "HTTPS required" error)
    try:
        configure_master_realm_ssl(
            compose_file=args.compose_file,
            realm=args.admin_realm,
        )
    except RuntimeError as exc:
        print(f"⚠ Failed to configure master realm SSL: {exc}", file=sys.stderr)
        # Continue anyway - this is a convenience feature

    if not args.skip_scope_setup:
        try:
            ensure_organization_scope(
                compose_file=args.compose_file,
                realm=args.realm,
                client_id="motet-ai-stack",
            )
            ensure_audience_scope(
                compose_file=args.compose_file,
                realm=args.realm,
                client_id="motet-ai-stack",
                audience="motet-ai-stack",
            )
            ensure_subject_mapper(
                compose_file=args.compose_file,
                realm=args.realm,
                client_id="motet-ai-stack",
            )
            ensure_roles_mapper(
                compose_file=args.compose_file,
                realm=args.realm,
                client_id="motet-ai-stack",
            )
        except RuntimeError as exc:
            print(f"✖ Failed to ensure client scopes: {exc}", file=sys.stderr)
            sys.exit(1)
    else:
        print("⏭ Skipping organization client scope setup per --skip-scope-setup flag")

    for plan in plans:
        try:
            ensure_organization(args.compose_file, args.realm, plan)
        except RuntimeError as exc:
            print(f"✖ Failed to ensure organization '{plan.name}': {exc}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()

