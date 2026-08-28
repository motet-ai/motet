"""
Motet - Command Executor

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Command execution system for the Motet distributed framework.
    Provides distributed command execution with routing, monitoring, and error handling.
    all workers (cloud and local) share the Valkey broker and use
    standard Celery routing. Local workers reach Valkey via WireGuard tunnel.

Dependencies:
    - Worker router and communication
    - Routing strategies and filters
    - Command registry and management
    - Observability and metrics

Usage:
    from motet.core.workers.command_executor import CommandExecutor
    
    executor = CommandExecutor()
    result = await executor.execute(command)

Notes:
    - Supports distributed command execution
    - Includes intelligent worker routing
    - Provides comprehensive error handling
    - Integrates with observability system
"""

import time
from typing import Dict, List, Optional, Any
from datetime import datetime

from .routing import WorkerRouter
from .routing.worker_communicator import WorkerCommunicator
from motet.core.commands.distributed import DistributedCommand
from motet.core.commands.base import CommandStatus

class CommandExecutor:
    """
    Simplified command executor focused on command lifecycle management.
    
    This executor provides:
    - Clean command execution orchestration
    - Performance tracking and metrics
    - Circuit breaker integration
    - Event publishing
    - Error handling and recovery
    
    All routing logic is delegated to WorkerRouter for clean separation.
    """
    
    def __init__(self, 
                 worker_router: WorkerRouter,
                 worker_communicator: WorkerCommunicator,
                 enable_circuit_breaker: bool = True,
                 enable_metrics: bool = True):
        """
        Initialize the command executor.
        
        Args:
            worker_router: Router for worker selection
            worker_communicator: Communicator for worker interaction
            enable_circuit_breaker: Enable circuit breaker pattern
            enable_metrics: Enable performance metrics collection
        """
        self.worker_router = worker_router
        self.worker_communicator = worker_communicator
        self.enable_circuit_breaker = enable_circuit_breaker
        self.enable_metrics = enable_metrics
        
        # Execution statistics
        self.execution_stats = {
            'total_executions': 0,
            'successful_executions': 0,
            'failed_executions': 0,
            'total_execution_time_ms': 0,
            'avg_execution_time_ms': 0.0,
            'command_type_stats': {},
            'tenant_stats': {},
            'worker_stats': {},
            'error_stats': {}
        }
        
        # Circuit breaker state (simplified)
        self.circuit_breaker = {
            'state': 'closed',  # closed, open, half-open
            'failure_count': 0,
            'last_failure_time': 0,
            'failure_threshold': 10,
            'recovery_timeout': 15     # reduced: auto-recover quickly after worker restarts
        }
        
        # Command history for debugging
        self.command_history = []
        self.max_history_size = 100
    
    def execute_command(self, 
                            command: DistributedCommand,
                            target_worker_id: Optional[str] = None,
                            strategy_override: Optional[str] = None) -> Dict[str, Any]:
        """
        Execute a distributed command.
        
        This is the main entry point for command execution. It handles:
        - Command lifecycle management
        - Worker routing (delegated to WorkerRouter)
        - Performance tracking
        - Error handling and recovery
        
        Args:
            command: Command to execute
            target_worker_id: Optional specific worker to target
            strategy_override: Optional routing strategy override
            
        Returns:
            Execution result with comprehensive metadata
        """
        start_time = time.time()
        execution_id = f"exec-{int(start_time * 1000)}"
        
        # Update command status
        command.status = CommandStatus.EXECUTING
        
        try:
            # Check circuit breaker
            if self.enable_circuit_breaker and not self._check_circuit_breaker():
                return self._create_error_result(
                    command, start_time, execution_id,
                    "Circuit breaker is open - system is recovering from failures"
                )

            # Route command to optimal worker (ADR-0095: all workers use standard routing)
            routing_decision = self.worker_router.route_command(
                command, target_worker_id, strategy_override
            )

            if not routing_decision.selected_worker:
                error_msg = routing_decision.error or "No suitable workers available"
                command.status = CommandStatus.FAILED
                return self._create_error_result(command, start_time, execution_id, error_msg)

            execution_result = self.worker_communicator.send_command(
                routing_decision.selected_worker, command
            )

            if execution_result.get('status') == 'completed':
                command.status = CommandStatus.COMPLETED
                result = self._create_success_result(
                    command, start_time, execution_id, execution_result, routing_decision
                )
                if self.enable_circuit_breaker:
                    self._record_success()
            else:
                error_code = execution_result.get('error_code')
                cancelled_codes = {"task_cancelled", "workflow_cancelled"}
                if error_code in cancelled_codes:
                    command.status = CommandStatus.CANCELLED
                else:
                    command.status = CommandStatus.FAILED
                error_msg = execution_result.get('error', 'Command execution failed')
                result = self._create_error_result(command, start_time, execution_id, error_msg)
                if error_code in cancelled_codes and isinstance(result, dict):
                    result['error_code'] = error_code
                # Cooperative cancel is not a worker fault — do not trip the breaker.
                if self.enable_circuit_breaker and error_code not in cancelled_codes:
                    self._record_failure()

            if self.enable_metrics:
                self._update_execution_stats(command, result, routing_decision)
            self._add_to_history(command, result, routing_decision)
            
            # Result is already rehydrated by worker_communicator
            return result
            
        except Exception as e:
            # Unexpected error
            command.status = CommandStatus.FAILED
            error_result = self._create_error_result(
                command, start_time, execution_id, f"Execution error: {str(e)}"
            )
            
            # Update circuit breaker on failure
            if self.enable_circuit_breaker:
                self._record_failure()
            
            # Update statistics
            if self.enable_metrics:
                self._update_execution_stats(command, error_result, None)
            
            # Error result is already rehydrated by worker_communicator (if applicable)
            return error_result
    
    def execute_on_specific_worker(self, 
                                       command: DistributedCommand,
                                       target_worker_id: str) -> Dict[str, Any]:
        """
        Execute command on a specific worker.
        
        This is a convenience method that maintains API compatibility
        with the previous system while using the new clean implementation.
        """
        return self.execute_command(command, target_worker_id=target_worker_id)
    
    def execute_batch(self, 
                          commands: List[DistributedCommand],
                          max_concurrent: int = 5) -> List[Dict[str, Any]]:
        """
        Execute multiple commands concurrently.
        
        Args:
            commands: List of commands to execute
            max_concurrent: Maximum number of concurrent executions
            
        Returns:
            List of execution results
        """
        from ..workers.concurrency_primitives import WorkerExecutor
        
        def execute_single_command(cmd):
            try:
                return self.execute_command(cmd)
            except Exception as e:
                return e
        
        # Execute all commands concurrently (ADR-0033: pool-aware execution)
        with WorkerExecutor(max_workers=max_concurrent) as executor:
            future_to_cmd = {executor.submit(execute_single_command, cmd): cmd for cmd in commands}
            results = [None] * len(commands)
            
            for future, cmd in future_to_cmd.items():
                cmd_index = commands.index(cmd)
                results[cmd_index] = future.result()
        
        # Convert exceptions to error results
        processed_results = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                error_result = self._create_error_result(
                    commands[i], time.time(), f"batch-{i}", str(result)
                )
                processed_results.append(error_result)
            else:
                processed_results.append(result)
        
        return processed_results
    
    def get_execution_stats(self) -> Dict[str, Any]:
        """Get comprehensive execution statistics"""
        return {
            **self.execution_stats,
            'circuit_breaker': self.circuit_breaker.copy(),
            'command_history_size': len(self.command_history),
            'uptime_seconds': time.time() - getattr(self, '_start_time', time.time())
        }
    
    def get_command_history(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Get recent command execution history"""
        return self.command_history[-limit:]
    
    def reset_stats(self):
        """Reset execution statistics"""
        self.execution_stats = {
            'total_executions': 0,
            'successful_executions': 0,
            'failed_executions': 0,
            'total_execution_time_ms': 0,
            'avg_execution_time_ms': 0.0,
            'command_type_stats': {},
            'tenant_stats': {},
            'worker_stats': {},
            'error_stats': {}
        }
        self.command_history.clear()
        print("📊 Execution statistics reset")
    
    # Private methods
    
    def _create_success_result(self, 
                             command: DistributedCommand,
                             start_time: float,
                             execution_id: str,
                             execution_result: Dict[str, Any],
                             routing_decision) -> Dict[str, Any]:
        """Create successful execution result"""
        execution_time = int((time.time() - start_time) * 1000)
        
        return {
            'status': 'completed',
            'execution_id': execution_id,
            'command_id': command.command_id,
            'command_type': command.get_command_type(),
            'result': execution_result.get('result'),
            'timing': execution_result.get('result', {}).get('timing', {}) if execution_result.get('result') else {},
            'execution_time_ms': execution_time,
            'routing_info': {
                'selected_worker_id': routing_decision.selected_worker.get('worker_id') if routing_decision else 'upstream',
                'strategy_used': routing_decision.strategy_used if routing_decision else 'unknown',
                'selection_reason': routing_decision.selection_reason if routing_decision else 'no_routing_decision',
                'decision_time_ms': routing_decision.decision_time_ms if routing_decision else 0,
                'available_workers': routing_decision.available_workers if routing_decision else 0,
                'filtered_workers': routing_decision.filtered_workers if routing_decision else 0,
            },
            'metadata': {
                'tenant_id': getattr(command, 'tenant_id', None),
                'session_id': getattr(command, 'session_id', None),
                'priority': getattr(command.distributed_context, 'priority', None),
                'capabilities_required': list(getattr(command.distributed_context, 'required_capabilities', set())),
                'timestamp': datetime.now().isoformat()
            }
        }
    
    def _create_error_result(self, 
                           command: DistributedCommand,
                           start_time: float,
                           execution_id: str,
                           error: str) -> Dict[str, Any]:
        """Create error execution result"""
        execution_time = int((time.time() - start_time) * 1000)
        
        return {
            'status': 'error',
            'execution_id': execution_id,
            'command_id': command.command_id,
            'command_type': command.get_command_type(),
            'error': error,
            'execution_time_ms': execution_time,
            'metadata': {
                'tenant_id': getattr(command, 'tenant_id', None),
                'session_id': getattr(command, 'session_id', None),
                'priority': getattr(command.distributed_context, 'priority', None),
                'timestamp': datetime.now().isoformat()
            }
        }
    
    def _update_execution_stats(self, 
                              command: DistributedCommand,
                              result: Dict[str, Any],
                              routing_decision) -> None:
        """Update execution statistics"""
        # Basic stats
        self.execution_stats['total_executions'] += 1
        execution_time = result['execution_time_ms']
        self.execution_stats['total_execution_time_ms'] += execution_time
        
        if result['status'] == 'completed':
            self.execution_stats['successful_executions'] += 1
        else:
            self.execution_stats['failed_executions'] += 1
            
            # Track error types
            error = result.get('error', 'Unknown error')
            if error not in self.execution_stats['error_stats']:
                self.execution_stats['error_stats'][error] = 0
            self.execution_stats['error_stats'][error] += 1
        
        # Calculate average execution time
        total_execs = self.execution_stats['total_executions']
        total_time = self.execution_stats['total_execution_time_ms']
        self.execution_stats['avg_execution_time_ms'] = total_time / total_execs if total_execs > 0 else 0
        
        # Command type stats
        command_type = command.get_command_type()
        if command_type not in self.execution_stats['command_type_stats']:
            self.execution_stats['command_type_stats'][command_type] = {
                'count': 0,
                'success_count': 0,
                'total_time_ms': 0,
                'avg_time_ms': 0.0
            }
        
        type_stats = self.execution_stats['command_type_stats'][command_type]
        type_stats['count'] += 1
        type_stats['total_time_ms'] += execution_time
        type_stats['avg_time_ms'] = type_stats['total_time_ms'] / type_stats['count']
        
        if result['status'] == 'completed':
            type_stats['success_count'] += 1
        
        # Tenant stats
        tenant_id = getattr(command, 'tenant_id', None)
        if tenant_id:
            if tenant_id not in self.execution_stats['tenant_stats']:
                self.execution_stats['tenant_stats'][tenant_id] = {
                    'count': 0,
                    'success_count': 0,
                    'total_time_ms': 0,
                    'avg_time_ms': 0.0
                }
            
            tenant_stats = self.execution_stats['tenant_stats'][tenant_id]
            tenant_stats['count'] += 1
            tenant_stats['total_time_ms'] += execution_time
            tenant_stats['avg_time_ms'] = tenant_stats['total_time_ms'] / tenant_stats['count']
            
            if result['status'] == 'completed':
                tenant_stats['success_count'] += 1
        
        # Worker stats
        if routing_decision and routing_decision.selected_worker:
            worker_id = routing_decision.selected_worker.get('worker_id')
            if worker_id:
                if worker_id not in self.execution_stats['worker_stats']:
                    self.execution_stats['worker_stats'][worker_id] = {
                        'count': 0,
                        'success_count': 0,
                        'total_time_ms': 0,
                        'avg_time_ms': 0.0
                    }
                
                worker_stats = self.execution_stats['worker_stats'][worker_id]
                worker_stats['count'] += 1
                worker_stats['total_time_ms'] += execution_time
                worker_stats['avg_time_ms'] = worker_stats['total_time_ms'] / worker_stats['count']
                
                if result['status'] == 'completed':
                    worker_stats['success_count'] += 1
    
    def _add_to_history(self, 
                       command: DistributedCommand,
                       result: Dict[str, Any],
                       routing_decision) -> None:
        """Add execution to command history"""
        history_entry = {
            'timestamp': datetime.now().isoformat(),
            'execution_id': result['execution_id'],
            'command_id': command.command_id,
            'command_type': command.get_command_type(),
            'status': result['status'],
            'execution_time_ms': result['execution_time_ms'],
            'worker_id': routing_decision.selected_worker.get('worker_id') if routing_decision and routing_decision.selected_worker else None,
            'strategy_used': routing_decision.strategy_used if routing_decision else None,
            'tenant_id': getattr(command, 'tenant_id', None),
            'error': result.get('error')
        }
        
        self.command_history.append(history_entry)
        
        # Limit history size
        if len(self.command_history) > self.max_history_size:
            self.command_history = self.command_history[-self.max_history_size:]
    
    def _check_circuit_breaker(self) -> bool:
        """Check if circuit breaker allows execution"""
        if self.circuit_breaker['state'] == 'closed':
            return True
        elif self.circuit_breaker['state'] == 'open':
            # Check if recovery timeout has passed
            if time.time() - self.circuit_breaker['last_failure_time'] > self.circuit_breaker['recovery_timeout']:
                self.circuit_breaker['state'] = 'half-open'
                return True
            return False
        elif self.circuit_breaker['state'] == 'half-open':
            return True
        
        return False
    
    def _record_success(self):
        """Record successful execution for circuit breaker"""
        if self.circuit_breaker['state'] == 'half-open':
            # Recovery successful, close circuit
            self.circuit_breaker['state'] = 'closed'
            self.circuit_breaker['failure_count'] = 0
    
    def _record_failure(self):
        """Record failed execution for circuit breaker"""
        self.circuit_breaker['failure_count'] += 1
        self.circuit_breaker['last_failure_time'] = time.time()
        
        if self.circuit_breaker['failure_count'] >= self.circuit_breaker['failure_threshold']:
            self.circuit_breaker['state'] = 'open'
            print(f"🔴 Circuit breaker opened after {self.circuit_breaker['failure_count']} failures")
