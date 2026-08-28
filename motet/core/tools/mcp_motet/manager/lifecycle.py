"""
Motet - MCP Instance Lifecycle

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-13

Description:
    Create/destroy MCP instances, HTTP singleton ownership (attach copies the
    owner's rewritten Docker base_url), and bootstrap.

Dependencies:
    - asyncio: per-instance locks and background loops
    - structlog: structured logging

Usage:
    Mixed into MCPInstanceManager in instance_manager.py. Do not instantiate alone.

Notes:
    - Public import path remains motet.core.tools.mcp_motet.proxy.mcp_instance_manager
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Dict, List, Optional, Tuple

import structlog

from motet.core.tools.mcp_motet.manager.config import (
    MCPInstance,
    MCPInstanceConfig,
    apply_stdio_discovery_bearer_placeholder,
)
from motet.core.tools.mcp_motet.protocol import (
    CredentialScope,
    LifecycleDuration,
    Visibility,
    generate_instance_key,
    parse_instance_key,
    validate_instance_spec,
)

logger = structlog.get_logger(__name__)
_apply_stdio_discovery_bearer_placeholder = apply_stdio_discovery_bearer_placeholder

class LifecycleMixin:
    def _mcp_startup_max_parallel_tasks(self) -> int:
        """
        Max concurrent service bootstraps. 0 or unset = run all services at once.

        Env: MOTET_MCP_STARTUP_MAX_PARALLEL (integer >= 1); any other value = unlimited.
        """
        raw = (os.getenv("MOTET_MCP_STARTUP_MAX_PARALLEL") or "").strip()
        if not raw:
            return 0
        try:
            n = int(raw)
        except ValueError:
            return 0
        return n if n >= 1 else 0

    async def _bootstrap_configured_services(self, per_service_timeout: int) -> None:
        """
        Create discovery (then at most one shared) instances for each configured service.

        Parallelizes across services so worker MCP startup does not serialize
        Docker image pulls and MCP handshakes (previously ~sum of per-service times).
        """
        items: List[Tuple[str, MCPInstanceConfig]] = list(self.service_configs.items())
        if not items:
            return

        max_parallel = self._mcp_startup_max_parallel_tasks()
        sem: Optional[asyncio.Semaphore] = (
            asyncio.Semaphore(max_parallel) if max_parallel else None
        )

        async def _one(
            service_id: str,
            service_config: MCPInstanceConfig,
        ) -> None:
            if sem:
                async with sem:
                    await asyncio.wait_for(
                        self._create_initial_instances(service_id, service_config),
                        timeout=per_service_timeout,
                    )
            else:
                await asyncio.wait_for(
                    self._create_initial_instances(service_id, service_config),
                    timeout=per_service_timeout,
                )

        logger.info(
            "mcp_startup_bootstrap_begin",
            services=[sid for sid, _ in items],
            per_service_timeout_seconds=per_service_timeout,
            max_parallel=max_parallel or None,
        )
        results = await asyncio.gather(
            *(_one(sid, cfg) for sid, cfg in items),
            return_exceptions=True,
        )
        for (service_id, _service_config), res in zip(items, results):
            if res is None:
                continue
            if isinstance(res, asyncio.TimeoutError):
                logger.error(
                    "mcp_service_init_timeout",
                    service_id=service_id,
                    timeout_seconds=per_service_timeout,
                    note=(
                        f"Service {service_id} initialization timed out after {per_service_timeout}s — "
                        "continuing with remaining services"
                    ),
                )
                await self._cleanup_failed_service(service_id)
            elif isinstance(res, BaseException):
                logger.error(
                    "mcp_service_init_failed",
                    service_id=service_id,
                    error=str(res),
                    error_type=type(res).__name__,
                    note=(
                        f"Service {service_id} initialization failed — continuing with remaining services"
                    ),
                    exc_info=(type(res), res, res.__traceback__),
                )
                await self._cleanup_failed_service(service_id)

        logger.info(
            "mcp_startup_bootstrap_done",
            services=[sid for sid, _ in items],
        )

    def _is_fixed_port_http_singleton_candidate(self, service_config: MCPInstanceConfig) -> bool:
        """Return True when service should use worker-local singleton HTTP process semantics."""
        return (
            service_config.transport in ["http", "streamable-http"]
            and service_config.start_server
            and service_config.port is not None
        )

    def _http_singleton_attach_client_settings(
        self,
        service_config: MCPInstanceConfig,
        owner_instance: MCPInstance,
    ) -> Dict[str, Any]:
        """
        Client settings for an attach-to-singleton HTTP instance.

        Copy the owner's rewritten Docker ``base_url`` / mapped host port so
        the attach transport does not probe ``127.0.0.1`` inside mcp-manager
        (that miss burns the 30s MotetMCPClient timeout).
        """
        base_url = service_config.base_url
        port = service_config.port
        owner_transport = owner_instance.transport
        if owner_transport is not None:
            owner_url = getattr(owner_transport, "base_url", None)
            if owner_url:
                base_url = owner_url
            owner_proc = getattr(owner_transport, "_process", None)
            mapped = getattr(owner_proc, "host_port", None)
            if mapped is not None:
                port = int(mapped)
        configured_timeout = int(service_config.startup_timeout_seconds or 45)
        return {
            "base_url": base_url,
            "port": port,
            "startup_timeout_seconds": min(configured_timeout, 10),
        }

    def _find_live_http_singleton_owner(self, service_id: str) -> Optional[MCPInstance]:
        """
        Find a live owner instance for fixed-port HTTP singleton process reuse.

        Ownership is primarily tracked in `_http_singleton_owner_by_service`. If tracking
        is stale, this falls back to scanning live service instances and repairs ownership.
        """
        owner_instance_id = self._http_singleton_owner_by_service.get(service_id)
        if owner_instance_id:
            owner = self.instances.get(owner_instance_id)
            if owner and owner.process and owner.process.returncode is None:
                return owner
            self._http_singleton_owner_by_service.pop(service_id, None)

        for instance in self.instances.values():
            if instance.service_id != service_id:
                continue
            if instance.process and instance.process.returncode is None:
                self._http_singleton_owner_by_service[service_id] = instance.instance_id
                instance.owns_http_singleton_process = True
                return instance

        return None

    async def create_instance(
        self,
        service_id: str,
        context_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
        task_id: Optional[str] = None,
        session_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        principal_id: Optional[str] = None,
        motet_id: Optional[str] = None,
        command_context: Optional[Any] = None,
        reason: str = "unknown",
        origin: str = "mcp_instance_manager",
        discovery_mode: bool = False,
        skip_validation: bool = False,
    ) -> MCPInstance:
        """
        Create a new MCP server instance using transport abstraction.
        
        Args:
            service_id: Service identifier
            context_id: Optional context identifier (for stateful services)
            conversation_id: Optional conversation identifier for context scoping
            task_id: Optional task identifier for context scoping
            tenant_id: Optional tenant identifier for context scoping
            principal_id: Optional principal identifier for per-user instances (ADR-0057)
            command_context: Optional CommandContext for vault credential retrieval
            reason: High-level reason the instance is being created (e.g., tool_execution, tool_discovery)
            origin: Component/path initiating creation (e.g., mcp_proxy_observer, _context_monitor_loop)
            
        Returns:
            Created MCPInstance
        """
        if service_id in self._disabled_services:
            raise RuntimeError(f"MCP service {service_id} is disabled")
        if service_id not in self.service_configs:
            raise ValueError(f"Service {service_id} not registered")
        
        service_config = self.service_configs[service_id]
        
        # Normalize credential_scope alias 'principal' -> 'user'
        if service_config.credential_scope == CredentialScope("user") or service_config.credential_scope == CredentialScope.USER:
            normalized_credential_scope = CredentialScope.USER
        elif str(service_config.credential_scope) == "principal":
            normalized_credential_scope = CredentialScope.USER
        else:
            normalized_credential_scope = service_config.credential_scope

        if not skip_validation:
            validate_instance_spec(
                state_model=service_config.state_model,
                credential_scope=normalized_credential_scope,
                visibility=service_config.visibility,
                lifecycle_duration=service_config.lifecycle_duration,
                instances=service_config.instances,
                shared_state_allowed=service_config.shared_state_allowed,
            )

        # Enforce required IDs by credential scope to ensure correct vault lookup
        # IMPORTANT: Must be BEFORE generate_instance_key() so synthetic IDs are used
        # Skip validation if requested (e.g., for context monitor backup mechanism)
        if not discovery_mode and not skip_validation:
            if normalized_credential_scope == CredentialScope.USER:
                if not principal_id or not tenant_id:
                    raise ValueError(f"credential_scope=user requires principal_id and tenant_id for service {service_id}")
            elif normalized_credential_scope in (CredentialScope.MOTET, CredentialScope.TENANT):
                if not tenant_id:
                    raise ValueError(f"credential_scope={normalized_credential_scope.value} requires tenant_id for service {service_id}")
            elif normalized_credential_scope == CredentialScope.GLOBAL:
                pass  # no additional IDs required
        else:
            # Discovery mode: supply synthetic IDs where needed to allow bootstrapping
            # Handle visibility requirements
            if service_config.visibility in (Visibility.MOTET, Visibility.TENANT, Visibility.USER) and not tenant_id:
                tenant_id = tenant_id or "discovery-tenant"
            if service_config.visibility == Visibility.USER and not principal_id:
                principal_id = principal_id or "discovery-user"
            # Handle lifecycle requirements (just like visibility)
            if service_config.lifecycle_duration == LifecycleDuration.CONVERSATION and not conversation_id:
                conversation_id = "discovery-conversation"
            if service_config.lifecycle_duration == LifecycleDuration.TASK and not task_id:
                task_id = "discovery-task"
            if service_config.lifecycle_duration == LifecycleDuration.SESSION and not session_id:
                session_id = "discovery-session"

        # Build instance key using ADR-0058 fields (after discovery mode synthetic ID injection)
        motet_id = motet_id or os.getenv("MOTET_MOTET_ID", "default")
        instance_id = generate_instance_key(
            service_id=service_id,
            visibility=service_config.visibility,
            lifecycle_duration=service_config.lifecycle_duration,
            motet_id=motet_id,
            tenant_id=tenant_id,
            principal_id=principal_id,
            conversation_id=conversation_id,
            task_id=task_id,
            session_id=session_id,
        )
        effective_context_id = instance_id
        await self._instance_lock(instance_id).acquire()
        try:
            return await self._create_instance_locked(
                service_id=service_id,
                instance_id=instance_id,
                effective_context_id=effective_context_id,
                service_config=service_config,
                conversation_id=conversation_id,
                task_id=task_id,
                session_id=session_id,
                tenant_id=tenant_id,
                principal_id=principal_id,
                motet_id=motet_id,
                command_context=command_context,
                reason=reason,
                origin=origin,
                discovery_mode=discovery_mode,
            )
        finally:
            self._instance_lock(instance_id).release()

    async def _create_instance_locked(
        self,
        *,
        service_id: str,
        instance_id: str,
        effective_context_id: str,
        service_config: MCPInstanceConfig,
        conversation_id: Optional[str],
        task_id: Optional[str],
        session_id: Optional[str],
        tenant_id: Optional[str],
        principal_id: Optional[str],
        motet_id: Optional[str],
        command_context: Optional[Any],
        reason: str,
        origin: str,
        discovery_mode: bool,
    ) -> MCPInstance:
        """Spawn or reuse one instance. Caller holds ``_instance_lock(instance_id)``."""
        if instance_id in self.instances:
            existing_instance = self.instances[instance_id]
            existing_instance.last_used = time.time()
            logger.info("mcp_instance_manager_reusing_instance",
                       instance_id=instance_id,
                       service_id=service_id,
                       has_transport=existing_instance.transport is not None,
                       is_transport_running=existing_instance.transport.is_running if existing_instance.transport and hasattr(existing_instance.transport, 'is_running') else None,
                       has_process=existing_instance.process is not None,
                       process_pid=existing_instance.process.pid if existing_instance.process else None,
                       process_alive=existing_instance.process.returncode is None if existing_instance.process else None)
            return existing_instance
        
        logger.info(f"🔧 Creating instance: {instance_id} (transport: {service_config.transport})")
        
        try:
            # Import transport factory
            from motet.core.tools.mcp_motet.transports import MCPTransportFactory
            
            # Start with environment variables from YAML config
            env_vars = dict(service_config.env or {})
            
            # Inject vault credentials if context provided (for stdio transport)
            if service_config.transport == "stdio":
                # Discovery mode: skip all vault lookups to allow unauthenticated tool listing/bootstrap
                if discovery_mode:
                    logger.info(f"🔐 Discovery mode: skipping ALL vault credential lookups for {service_id}; using YAML/env only")
                    vault_env = {}
                else:
                    # In normal mode, retrieve vault credentials (user/motet scoped)
                    skip_oauth_tokens = service_config.auth and service_config.auth.type == "oauth2" and False  # kept for clarity; not used now
                    try:
                        logger.info(f"🔐 Retrieving vault credentials for {service_id}")
                        from motet.core.security.vault_mcp_integration import get_mcp_env_vars_from_vault
                        # Ensure we have a context with the right IDs for vault lookup
                        if command_context:
                            ctx_for_vault = command_context
                            logger.info(
                                f"🔐 Using command_context for vault lookup",
                                service_id=service_id,
                                ctx_principal_id=getattr(command_context, 'principal_id', 'N/A'),
                                ctx_tenant_id=getattr(command_context, 'tenant_id', 'N/A'),
                                ctx_motet_id=getattr(command_context, 'motet_id', 'N/A')
                            )
                        else:
                            from motet.core.commands.base import CommandContext
                            ctx_for_vault = CommandContext(
                                task_id="credential_lookup",
                                principal_id=principal_id or "",
                                tenant_id=tenant_id or "",
                                motet_id=motet_id or ""
                            )
                            logger.info(
                                f"🔐 Created CommandContext for vault lookup",
                                service_id=service_id,
                                principal_id=principal_id or "",
                                tenant_id=tenant_id or "",
                                motet_id=motet_id or ""
                            )
                        vault_env = get_mcp_env_vars_from_vault(service_id, ctx_for_vault)
                        if vault_env:
                            env_vars.update(vault_env)  # Vault credentials override YAML
                            logger.info(f"✅ Injected {len(vault_env)} vault credentials for {service_id}")
                        else:
                            logger.info(f"ℹ️ No vault credentials found for {service_id}, using YAML config only")
                    except Exception as e:
                        logger.warning(f"⚠️ Failed to retrieve vault credentials for {service_id}: {e}", exc_info=True)
                        logger.info(f"ℹ️ Continuing with YAML config environment variables")
                _apply_stdio_discovery_bearer_placeholder(
                    discovery_mode=discovery_mode,
                    service_id=service_id,
                    service_config=service_config,
                    env_vars=env_vars,
                )
            
            # Determine HTTP process ownership mode for fixed-port local services.
            # ADR-0058 logical instance keys remain per-scope, but physical process
            # ownership is worker-local singleton for fixed-port start_server services.
            attach_to_http_singleton = False
            http_singleton_owner_instance_id: Optional[str] = None
            attach_client: Optional[Dict[str, Any]] = None
            if self._is_fixed_port_http_singleton_candidate(service_config):
                owner_instance = self._find_live_http_singleton_owner(service_id)
                if owner_instance:
                    attach_to_http_singleton = True
                    http_singleton_owner_instance_id = owner_instance.instance_id
                    attach_client = self._http_singleton_attach_client_settings(
                        service_config, owner_instance
                    )
                    logger.info(
                        "mcp_http_singleton_attach_mode",
                        service_id=service_id,
                        instance_id=instance_id,
                        owner_instance_id=http_singleton_owner_instance_id,
                        worker_id=self.worker_id,
                        base_url=attach_client["base_url"],
                        port=attach_client["port"],
                    )

            # Build transport configuration
            transport_config = {
                "command": service_config.command,
                "args": service_config.args,
                "env": env_vars,
                "exec_image": service_config.exec_image,
                # ADR-0058: Pass the effective instance key through to transports/proxies.
                # Without this, MotetMCPProxy may infer CONVERSATION lifecycle purely from
                # presence of conversation_id and create conversation-scoped streams even
                # when the service is configured as idle_timeout/per-user.
                "context_id": effective_context_id,
                "conversation_id": conversation_id,
                "task_id": task_id,
                "tenant_id": tenant_id,
                "principal_id": principal_id,  # ADR-0057: For per-user credential lookup
                "motet_id": motet_id,  # Required for MOTET visibility services
            }
            
            # Add HTTP-specific configuration
            if service_config.transport in ["http", "streamable-http"]:
                http_base_url = service_config.base_url
                http_port = service_config.port
                http_startup_timeout = service_config.startup_timeout_seconds
                if attach_client is not None:
                    http_base_url = attach_client["base_url"]
                    http_port = attach_client["port"]
                    http_startup_timeout = attach_client["startup_timeout_seconds"]
                transport_config.update({
                    "start_server": service_config.start_server and not attach_to_http_singleton,
                    "base_url": http_base_url,
                    "port": http_port,
                    "streamable_http_sse": service_config.streamable_http_sse,
                    "startup_timeout_seconds": http_startup_timeout,
                    "startup_probe_interval_seconds": service_config.startup_probe_interval_seconds,
                    "use_vault_token": service_config.use_vault_token,
                    "vault_credential_key": service_config.vault_credential_key,
                    "token_field": service_config.token_field,
                })
            
            # Create transport instance using factory
            logger.info(f"🔍 Creating {service_config.transport} transport for {service_id}...")
            # ADR-0105 §R2: pass the manager-keyed routing prefix through the
            # transport stack so the proxy reads requests / writes responses
            # on the same streams the worker addresses.
            transport_routing_prefix = self.manager_id or self.worker_id
            transport = MCPTransportFactory.create_transport(
                transport_type=service_config.transport,
                service_id=service_id,
                config=transport_config,
                worker_id=transport_routing_prefix,
                startup_command_context=command_context or self.startup_command_context
            )
            
            # Start the transport
            logger.info(f"🔍 Starting {service_config.transport} transport...")
            success = await transport.start()
            
            if not success:
                raise RuntimeError(f"Failed to start {service_config.transport} transport for {service_id}")
            
            logger.info(f"✅ {service_config.transport.upper()} transport started for {service_id}")
            
            # Get process reference (if stdio transport)
            process = None
            if service_config.transport == "stdio" and hasattr(transport, '_process'):
                process = transport._process  # type: ignore[attr-defined]
            elif service_config.transport in ["http", "streamable-http"] and hasattr(transport, '_process'):
                process = transport._process  # type: ignore[attr-defined]
            
            # Create instance record
            instance = MCPInstance(
                instance_id=instance_id,
                service_id=service_id,
                context_id=effective_context_id or "default",
                transport=transport,  # New transport-based approach
                proxy=None,  # Deprecated (legacy field)
                process=process,
                created_at=time.time(),
                last_used=time.time(),
                owns_http_singleton_process=bool(
                    self._is_fixed_port_http_singleton_candidate(service_config)
                    and transport_config.get("start_server")
                    and process is not None
                ),
                http_singleton_owner_instance_id=http_singleton_owner_instance_id,
            )
            
            # Store instance
            self.instances[instance_id] = instance
            if instance.owns_http_singleton_process:
                self._http_singleton_owner_by_service[service_id] = instance_id
            elif attach_to_http_singleton and http_singleton_owner_instance_id:
                instance.http_singleton_owner_instance_id = http_singleton_owner_instance_id
            self.stats["instances_created"] += 1
            
            pid_info = f"(PID: {process.pid})" if process else ""
            logger.info("mcp_instance_manager_instance_created",
                       instance_id=instance_id,
                       service_id=service_id,
                       process_pid=process.pid if process else None,
                       transport_type=service_config.transport,
                       has_transport=instance.transport is not None,
                       is_transport_running=instance.transport.is_running if instance.transport and hasattr(instance.transport, 'is_running') else None,
                       context_id=effective_context_id,
                       tenant_id=tenant_id,
                       principal_id=principal_id,
                       conversation_id=conversation_id,
                       task_id=task_id)

            # Emit lifecycle event for observability (non-user-facing) (ADR-0058).
            try:
                from motet.core.workers.events import global_bus
                from motet.core.workers.observers import EventPriority

                global_bus.publish({
                    "kind": "mcp.instance_created",
                    "source": "mcp_instance_manager",
                    "priority": EventPriority.LOW.value,
                    "data": {
                        "service_id": service_id,
                        "instance_id": instance_id,
                        "context_id": effective_context_id,
                        "worker_id": self.worker_id,
                        "transport_type": service_config.transport,
                        "process_pid": process.pid if process else None,
                        "visibility": service_config.visibility.value,
                        "lifecycle_duration": service_config.lifecycle_duration.value,
                        "tenant_id": tenant_id,
                        "motet_id": motet_id,
                        "principal_id": principal_id,
                        "conversation_id": conversation_id,
                        "task_id": task_id,
                        "session_id": session_id,
                        "discovery_mode": discovery_mode,
                        "reason": reason,
                        "origin": origin,
                    }
                })
            except Exception as e:
                logger.debug("Failed to emit mcp.instance_created event",
                             service_id=service_id,
                             instance_id=instance_id,
                             error=str(e))
            
            return instance
            
        except Exception as e:
            logger.error(
                "mcp_create_instance_failed",
                instance_id=instance_id,
                service_id=service_id,
                error=str(e),
                exc_info=True,
            )
            self._service_last_error[service_id] = str(e)[:500]
            raise

    async def destroy_instance(self, instance_id: str, reason: str = "unknown") -> None:
        """
        Destroy an instance and clean up resources.
        
        Args:
            instance_id: Instance identifier
            reason: Reason for destruction (e.g., shutdown, idle_timeout, credential_refresh)
        """
        async with self._instance_lock(instance_id):
            await self._destroy_instance_locked(instance_id, reason=reason)

    async def _destroy_instance_locked(self, instance_id: str, reason: str = "unknown") -> None:
        """Destroy one instance. Caller holds ``_instance_lock(instance_id)``."""
        if instance_id not in self.instances:
            logger.warning(f"⚠️ Instance {instance_id} not found")
            return
        
        instance = self.instances[instance_id]
        logger.info(f"🗑️ Destroying instance: {instance_id}")

        # Capture metadata before removal for event emission
        service_id = instance.service_id
        svc_cfg = self.service_configs.get(service_id)
        transport_type = getattr(svc_cfg, "transport", None)
        process_pid = instance.process.pid if instance.process is not None else None
        parsed: Dict[str, Any] = {}
        if svc_cfg:
            try:
                parsed = parse_instance_key(
                    service_id=service_id,
                    visibility=svc_cfg.visibility,
                    instance_key=instance_id,
                )
            except Exception:
                parsed = {}
        
        # CRITICAL: Remove from tracking IMMEDIATELY to prevent race conditions
        # where a new request grabs the instance while it's being destroyed
        del self.instances[instance_id]
        self.stats["instances_destroyed"] += 1

        # ADR-0069: If no instances remain for this service, notify worker to unregister tools
        remaining_for_service = sum(1 for inst in self.instances.values() if inst.service_id == service_id)
        if remaining_for_service == 0:
            try:
                await self._publish_per_service_signal(service_id, "service_removed")
            except Exception as e:
                logger.debug(
                    "Failed to publish service_removed signal",
                    service_id=service_id,
                    error=str(e),
                )

        # For fixed-port local HTTP singleton services, preserve the shared subprocess
        # by transferring ownership before stopping the destroyed instance transport.
        owner_instance_id = self._http_singleton_owner_by_service.get(service_id)
        is_owner_instance = owner_instance_id == instance_id
        if is_owner_instance:
            successor: Optional[MCPInstance] = None
            if remaining_for_service > 0:
                for candidate in self.instances.values():
                    if candidate.service_id == service_id and candidate.transport:
                        successor = candidate
                        break

            if successor and instance.process and instance.process.returncode is None:
                shared_process = instance.process
                successor.process = shared_process
                successor.owns_http_singleton_process = True
                successor.http_singleton_owner_instance_id = successor.instance_id
                if hasattr(successor.transport, "_process"):
                    setattr(successor.transport, "_process", shared_process)
                if hasattr(successor.transport, "start_server"):
                    setattr(successor.transport, "start_server", True)

                # Detach process from the instance being destroyed so stop() only
                # cleans stream consumers and does not terminate the shared server.
                instance.process = None
                if instance.transport and hasattr(instance.transport, "_process"):
                    setattr(instance.transport, "_process", None)
                if instance.transport and hasattr(instance.transport, "start_server"):
                    setattr(instance.transport, "start_server", False)

                self._http_singleton_owner_by_service[service_id] = successor.instance_id
                logger.info(
                    "mcp_http_singleton_owner_transferred",
                    service_id=service_id,
                    from_instance_id=instance_id,
                    to_instance_id=successor.instance_id,
                    process_pid=shared_process.pid,
                )
            else:
                self._http_singleton_owner_by_service.pop(service_id, None)

        # Emit lifecycle event for observability (non-user-facing)
        try:
            from motet.core.workers.events import global_bus
            from motet.core.workers.observers import EventPriority

            global_bus.publish({
                "kind": "mcp.instance_destroyed",
                "source": "mcp_instance_manager",
                "priority": EventPriority.LOW.value,
                "data": {
                    "service_id": service_id,
                    "instance_id": instance_id,
                    "context_id": instance.context_id,
                    "worker_id": self.worker_id,
                    "transport_type": transport_type,
                    "process_pid": process_pid,
                    "visibility": svc_cfg.visibility.value if svc_cfg else None,
                    "lifecycle_duration": svc_cfg.lifecycle_duration.value if svc_cfg else None,
                    "tenant_id": parsed.get("tenant_id"),
                    "motet_id": parsed.get("motet_id"),
                    "principal_id": parsed.get("principal_id"),
                    "conversation_id": parsed.get("conversation_id"),
                    "task_id": parsed.get("task_id"),
                    "session_id": parsed.get("session_id"),
                    "reason": reason,
                }
            })
        except Exception as e:
            logger.debug("Failed to emit mcp.instance_destroyed event",
                         service_id=service_id,
                         instance_id=instance_id,
                         error=str(e))
        
        try:
            # Stop transport (new approach) or proxy (legacy)
            if instance.transport:
                await instance.transport.stop()
            elif instance.proxy:
                # Backward compatibility: legacy proxy-based instances
                await instance.proxy.stop()
            
            logger.info(f"✅ Destroyed instance: {instance_id}")
            
        except Exception as e:
            logger.error(f"❌ Failed to destroy instance {instance_id}: {e}", exc_info=True)

    async def _cleanup_failed_service(self, service_id: str) -> None:
        """Clean up any partially-created instances for a service that failed to initialize.
        
        When asyncio.wait_for cancels a timed-out _create_initial_instances coroutine,
        any subprocess already spawned (e.g. npx) may still be alive. This method
        finds and stops those zombie instances so they don't leak resources.
        """
        instance_ids_to_remove = [
            iid for iid, inst in self.instances.items()
            if inst.service_id == service_id
        ]
        for iid in instance_ids_to_remove:
            try:
                inst = self.instances.pop(iid, None)
                if inst and inst.transport:
                    logger.info(
                        "mcp_cleanup_failed_service_stopping_transport",
                        instance_id=iid,
                        service_id=service_id,
                    )
                    await asyncio.wait_for(inst.transport.stop(), timeout=10.0)
                elif inst and inst.process:
                    logger.info(
                        "mcp_cleanup_failed_service_killing_process",
                        instance_id=iid,
                        service_id=service_id,
                        pid=inst.process.pid if hasattr(inst.process, 'pid') else None,
                    )
                    try:
                        inst.process.terminate()
                    except Exception:
                        pass  # best-effort process cleanup; may already be dead
                    try:
                        inst.process.kill()
                    except Exception:
                        pass  # best-effort kill; process may already be gone  # best-effort process cleanup; may already be dead
            except Exception as cleanup_err:
                logger.warning(
                    "mcp_cleanup_failed_service_error",
                    instance_id=iid,
                    service_id=service_id,
                    error=str(cleanup_err),
                )
        if instance_ids_to_remove:
            logger.info(
                "mcp_cleanup_failed_service_done",
                service_id=service_id,
                cleaned_up=len(instance_ids_to_remove),
            )
        try:
            await self._publish_per_service_signal(service_id, "service_removed")
        except Exception as cleanup_signal_err:
            logger.warning(
                "mcp_cleanup_failed_service_remove_signal_failed",
                service_id=service_id,
                error=str(cleanup_signal_err),
                exc_info=True,
            )

    async def _create_initial_instances(
        self,
        service_id: str,
        service_config: MCPInstanceConfig
    ) -> None:
        """Create discovery instance, then at most one shared non-USER instance."""
        logger.info(
            "mcp_bootstrap_service",
            service_id=service_id,
            state_model=str(service_config.state_model),
            visibility=str(service_config.visibility),
            lifecycle=str(service_config.lifecycle_duration),
            discovery_only=service_config.discovery_only(),
        )
        try:
            await asyncio.wait_for(
                self.create_instance(
                    service_id,
                    tenant_id="discovery-tenant",
                    principal_id="discovery-user" if service_config.visibility == Visibility.USER else None,
                    motet_id=os.getenv("MOTET_MOTET_ID", "default"),
                    discovery_mode=True,
                    skip_validation=True,
                    reason="tool_discovery",
                    origin="_create_initial_instances",
                ),
                timeout=self._create_timeout_seconds(),
            )
            await self._capture_discovery_tools(service_id)
            await self._publish_per_service_signal(service_id, "service_ready")
            self._service_last_ready_at[service_id] = time.time()
            self._service_last_error.pop(service_id, None)
        except Exception as e:
            logger.warning(
                "mcp_discovery_instance_failed",
                service_id=service_id,
                error=str(e),
                exc_info=True,
            )
            self._service_last_error[service_id] = str(e)[:500]
            await self._publish_one_service_status(service_id, status_override="failed")
            return

        if service_config.discovery_only():
            logger.info("mcp_bootstrap_discovery_only", service_id=service_id)
            await self._publish_one_service_status(service_id)
            return

        try:
            await asyncio.wait_for(
                self.create_instance(
                    service_id,
                    motet_id=os.getenv("MOTET_MOTET_ID", "default"),
                    tenant_id=os.getenv("MOTET_TENANT_ID", "default-tenant"),
                    reason="startup",
                    origin="_create_initial_instances",
                ),
                timeout=self._create_timeout_seconds(),
            )
        except Exception as e:
            logger.error(
                "mcp_shared_instance_failed",
                service_id=service_id,
                error=str(e),
                exc_info=True,
            )
            self._service_last_error[service_id] = str(e)[:500]
        await self._publish_one_service_status(service_id)

    async def _capture_discovery_tools(self, service_id: str) -> None:
        """Best-effort tool name capture after discovery start."""
        names: List[str] = []
        for inst in self.instances.values():
            if inst.service_id != service_id or inst.transport is None:
                continue
            list_tools = getattr(inst.transport, "list_tools", None)
            if not callable(list_tools):
                continue
            try:
                tools = await asyncio.wait_for(list_tools(timeout_seconds=15), timeout=20)
                for tool in tools or []:
                    name = getattr(tool, "name", None) or (tool.get("name") if isinstance(tool, dict) else None)
                    if name:
                        names.append(str(name))
            except Exception as e:
                logger.debug(
                    "mcp_capture_discovery_tools_failed",
                    service_id=service_id,
                    error=str(e),
                )
            break
        if names:
            self._service_tool_names[service_id] = names
