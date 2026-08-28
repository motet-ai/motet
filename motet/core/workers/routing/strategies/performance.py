"""
Motet - Performance-Based Routing Strategies

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Performance-based routing strategies for the Motet distributed framework.
    Optimizes for performance metrics like response time, throughput, and adaptive load balancing.

Dependencies:
    - time: Time measurement and statistics
    - typing: Type hints and annotations
    - Base routing strategy interface
    - Performance monitoring

Usage:
    from motet.core.workers.routing.strategies.performance import FastestResponseStrategy
    
    # Create performance strategy
    strategy = FastestResponseStrategy()
    
    # Route based on performance
    workers = await strategy.route(context)

Notes:
    - Provides performance-based routing algorithms
    - Includes response time optimization
    - Supports adaptive load balancing
    - Integrates with distributed architecture
"""

import time
from typing import Dict, List, Optional, Any
from .base import RoutingStrategy, RoutingContext, WorkerScore


class FastestResponseStrategy(RoutingStrategy):
    """
    Route to workers with the fastest historical response times.
    
    This strategy maintains response time statistics and prefers
    workers that have demonstrated fast execution.
    """
    
    def __init__(self, history_window: int = 100):
        """
        Initialize fastest response strategy.
        
        Args:
            history_window: Number of recent executions to consider
        """
        self.history_window = history_window
        self.response_history = {}  # worker_id -> list of response times
        self.avg_response_times = {}  # worker_id -> average response time
    
    def select_worker(self, 
                     workers: List[Dict[str, Any]], 
                     context: RoutingContext) -> Optional[Dict[str, Any]]:
        """Select worker with fastest average response time"""
        if not workers:
            return None
        
        # Score workers by response time
        scored_workers = []
        
        for worker in workers:
            worker_id = worker.get('worker_id')
            
            # Get average response time (lower is better)
            avg_response_time = self.avg_response_times.get(worker_id, float('inf'))
            
            # If no history, use current load as proxy
            if avg_response_time == float('inf'):
                current_load = worker.get('current_load', 1.0)
                estimated_response_time = 1000 + (current_load * 5000)  # Estimate in ms
                score = 1.0 / (estimated_response_time / 1000)  # Convert to score
            else:
                score = 1.0 / (avg_response_time / 1000)  # Convert to score (higher is better)
            
            scored_workers.append((score, worker, avg_response_time))
        
        # Sort by score (descending)
        scored_workers.sort(key=lambda x: x[0], reverse=True)
        
        if scored_workers:
            best_score, selected_worker, avg_time = scored_workers[0]
            selected = selected_worker.copy()
            
            if avg_time == float('inf'):
                selected['selection_reason'] = "Fastest response (estimated, no history)"
            else:
                selected['selection_reason'] = f"Fastest response (avg: {avg_time:.0f}ms)"
            
            selected['performance_score'] = best_score
            selected['avg_response_time_ms'] = avg_time if avg_time != float('inf') else None
            
            return selected
        
        return None
    
    def record_execution(self, worker_id: str, response_time_ms: float):
        """Record execution time for a worker"""
        if worker_id not in self.response_history:
            self.response_history[worker_id] = []
        
        # Add new response time
        self.response_history[worker_id].append(response_time_ms)
        
        # Limit history size
        if len(self.response_history[worker_id]) > self.history_window:
            self.response_history[worker_id] = self.response_history[worker_id][-self.history_window:]
        
        # Update average
        history = self.response_history[worker_id]
        self.avg_response_times[worker_id] = sum(history) / len(history)
    
    def get_worker_stats(self, worker_id: str) -> Dict[str, Any]:
        """Get performance statistics for a worker"""
        history = self.response_history.get(worker_id, [])
        if not history:
            return {"worker_id": worker_id, "no_data": True}
        
        return {
            "worker_id": worker_id,
            "avg_response_time_ms": self.avg_response_times.get(worker_id, 0),
            "min_response_time_ms": min(history),
            "max_response_time_ms": max(history),
            "execution_count": len(history),
            "recent_trend": history[-10:] if len(history) >= 10 else history
        }
    
    def get_strategy_name(self) -> str:
        return "Fastest Response"


class StateAwareStrategy(RoutingStrategy):
    """
    Route based on comprehensive worker state analysis.
    
    This strategy considers multiple state factors including load,
    queue depth, recent performance, and worker health.
    """
    
    def __init__(self):
        self.worker_states = {}  # worker_id -> state metrics
        self.last_update = {}    # worker_id -> last update timestamp
    
    def select_worker(self, 
                     workers: List[Dict[str, Any]], 
                     context: RoutingContext) -> Optional[Dict[str, Any]]:
        """Select worker based on comprehensive state analysis"""
        if not workers:
            return None
        
        # Update worker states
        current_time = time.time()
        for worker in workers:
            self._update_worker_state(worker, current_time)
        
        # Score workers based on state
        scored_workers = []
        
        for worker in workers:
            state_score = self._calculate_state_score(worker, context)
            scored_workers.append((state_score, worker))
        
        # Sort by score (descending)
        scored_workers.sort(key=lambda x: x[0], reverse=True)
        
        if scored_workers:
            best_score, selected_worker = scored_workers[0]
            selected = selected_worker.copy()
            selected['selection_reason'] = f"State-aware selection (score: {best_score:.2f})"
            selected['state_score'] = best_score
            
            return selected
        
        return None
    
    def _update_worker_state(self, worker: Dict[str, Any], current_time: float):
        """Update state metrics for a worker"""
        worker_id = worker.get('worker_id')
        if not worker_id:
            return
        
        if worker_id not in self.worker_states:
            self.worker_states[worker_id] = {
                'load_history': [],
                'performance_trend': 0.0,
                'stability_score': 1.0,
                'health_score': 1.0
            }
        
        state = self.worker_states[worker_id]
        
        # Update load history
        current_load = worker.get('current_load', 0.0)
        state['load_history'].append((current_time, current_load))
        
        # Keep only recent history (last 5 minutes)
        cutoff_time = current_time - 300
        state['load_history'] = [(t, load) for t, load in state['load_history'] if t > cutoff_time]
        
        # Calculate performance trend
        if len(state['load_history']) >= 2:
            recent_loads = [load for _, load in state['load_history'][-10:]]
            if len(recent_loads) >= 2:
                # Simple trend calculation (negative = improving, positive = degrading)
                state['performance_trend'] = (recent_loads[-1] - recent_loads[0]) / len(recent_loads)
        
        # Update health score based on worker metadata
        health_factors = []
        
        # Warmup completion
        if worker.get('warmup_completed', False):
            health_factors.append(1.0)
        else:
            health_factors.append(0.5)
        
        # Recent heartbeat
        last_heartbeat = worker.get('last_heartbeat', 0)
        if current_time - last_heartbeat < 30:  # Within 30 seconds
            health_factors.append(1.0)
        elif current_time - last_heartbeat < 60:  # Within 1 minute
            health_factors.append(0.8)
        else:
            health_factors.append(0.3)
        
        # Tool availability
        tool_count = worker.get('tool_count', 0)
        mcp_tool_count = worker.get('mcp_tool_count', 0)
        if tool_count > 0 or mcp_tool_count > 0:
            health_factors.append(1.0)
        else:
            health_factors.append(0.7)
        
        state['health_score'] = sum(health_factors) / len(health_factors) if health_factors else 0.5
        
        # Update stability score based on load variance
        if len(state['load_history']) >= 5:
            loads = [load for _, load in state['load_history']]
            avg_load = sum(loads) / len(loads)
            variance = sum((load - avg_load) ** 2 for load in loads) / len(loads)
            state['stability_score'] = max(0.1, 1.0 - variance)  # Lower variance = higher stability
        
        self.last_update[worker_id] = current_time
    
    def _calculate_state_score(self, worker: Dict[str, Any], context: RoutingContext) -> float:
        """Calculate comprehensive state score for a worker"""
        worker_id = worker.get('worker_id')
        if not worker_id or worker_id not in self.worker_states:
            # No state data, use basic load score
            return 1.0 - worker.get('current_load', 1.0)
        
        state = self.worker_states[worker_id]
        
        # Base load score (40% weight)
        current_load = worker.get('current_load', 1.0)
        load_score = max(0.0, 1.0 - current_load) * 0.4
        
        # Performance trend score (20% weight)
        # Negative trend (improving) is good, positive trend (degrading) is bad
        trend = state['performance_trend']
        trend_score = max(0.0, 1.0 - abs(trend)) * 0.2
        
        # Stability score (20% weight)
        stability_score = state['stability_score'] * 0.2
        
        # Health score (20% weight)
        health_score = state['health_score'] * 0.2
        
        total_score = load_score + trend_score + stability_score + health_score
        
        return total_score
    
    def get_worker_state(self, worker_id: str) -> Dict[str, Any]:
        """Get detailed state information for a worker"""
        if worker_id not in self.worker_states:
            return {"worker_id": worker_id, "no_state_data": True}
        
        state = self.worker_states[worker_id]
        return {
            "worker_id": worker_id,
            "performance_trend": state['performance_trend'],
            "stability_score": state['stability_score'],
            "health_score": state['health_score'],
            "load_history_points": len(state['load_history']),
            "last_update": self.last_update.get(worker_id, 0)
        }
    
    def get_strategy_name(self) -> str:
        return "State-Aware"


class AdaptiveStrategy(RoutingStrategy):
    """
    Adaptive routing that learns and adjusts based on system performance.
    
    This strategy dynamically adjusts its behavior based on observed
    system performance and changing conditions.
    """
    
    def __init__(self, learning_rate: float = 0.1):
        """
        Initialize adaptive strategy.
        
        Args:
            learning_rate: How quickly to adapt to new information
        """
        self.learning_rate = learning_rate
        self.worker_weights = {}      # worker_id -> learned weight
        self.success_rates = {}       # worker_id -> success rate
        self.adaptation_history = []  # History of adaptations
        self.current_strategy = "least_loaded"  # Current base strategy
        
        # Available base strategies to adapt between
        self.base_strategies = {
            "least_loaded": self._least_loaded_selection,
            "fastest_response": self._fastest_response_selection,
            "round_robin": self._round_robin_selection
        }
        
        self.strategy_performance = {name: 0.0 for name in self.base_strategies.keys()}
        self.strategy_usage_count = {name: 0 for name in self.base_strategies.keys()}
    
    def select_worker(self, 
                     workers: List[Dict[str, Any]], 
                     context: RoutingContext) -> Optional[Dict[str, Any]]:
        """Select worker using adaptive algorithm"""
        if not workers:
            return None
        
        # Determine which base strategy to use
        best_strategy = self._select_best_strategy()
        
        # Use the selected strategy
        strategy_func = self.base_strategies[best_strategy]
        selected_worker = strategy_func(workers, context)
        
        if selected_worker:
            selected = selected_worker.copy()
            selected['selection_reason'] = f"Adaptive strategy using {best_strategy}"
            selected['adaptive_strategy'] = best_strategy
            selected['adaptation_confidence'] = self._get_confidence_score(best_strategy)
            
            # Update usage count
            self.strategy_usage_count[best_strategy] += 1
            
            return selected
        
        return None
    
    def record_execution_result(self, 
                              worker_id: str, 
                              strategy_used: str,
                              success: bool, 
                              response_time_ms: float):
        """Record execution result for learning"""
        # Update worker weights
        if worker_id not in self.worker_weights:
            self.worker_weights[worker_id] = 1.0
        
        if worker_id not in self.success_rates:
            self.success_rates[worker_id] = []
        
        # Record success/failure
        self.success_rates[worker_id].append(success)
        
        # Keep only recent history
        if len(self.success_rates[worker_id]) > 50:
            self.success_rates[worker_id] = self.success_rates[worker_id][-50:]
        
        # Update worker weight based on performance
        current_success_rate = sum(self.success_rates[worker_id]) / len(self.success_rates[worker_id])
        performance_factor = current_success_rate * (1000.0 / max(response_time_ms, 100))  # Success rate * speed factor
        
        # Adaptive weight update
        self.worker_weights[worker_id] = (
            (1 - self.learning_rate) * self.worker_weights[worker_id] + 
            self.learning_rate * performance_factor
        )
        
        # Update strategy performance
        if strategy_used in self.strategy_performance:
            current_perf = self.strategy_performance[strategy_used]
            new_perf = performance_factor
            self.strategy_performance[strategy_used] = (
                (1 - self.learning_rate) * current_perf + 
                self.learning_rate * new_perf
            )
        
        # Record adaptation
        self.adaptation_history.append({
            'timestamp': time.time(),
            'worker_id': worker_id,
            'strategy': strategy_used,
            'success': success,
            'response_time_ms': response_time_ms,
            'new_weight': self.worker_weights[worker_id],
            'success_rate': current_success_rate
        })
        
        # Limit adaptation history
        if len(self.adaptation_history) > 1000:
            self.adaptation_history = self.adaptation_history[-1000:]
    
    def _select_best_strategy(self) -> str:
        """Select the best performing base strategy"""
        if not self.strategy_performance:
            return "least_loaded"
        
        # Use epsilon-greedy approach for exploration vs exploitation
        import random
        epsilon = 0.1  # 10% exploration
        
        if random.random() < epsilon:
            # Exploration: randomly select a strategy
            return random.choice(list(self.base_strategies.keys()))
        else:
            # Exploitation: select best performing strategy
            best_strategy = max(self.strategy_performance.keys(), 
                              key=lambda k: self.strategy_performance[k])
            return best_strategy
    
    def _get_confidence_score(self, strategy: str) -> float:
        """Get confidence score for a strategy"""
        if strategy not in self.strategy_usage_count:
            return 0.0
        
        usage_count = self.strategy_usage_count[strategy]
        performance = self.strategy_performance.get(strategy, 0.0)
        
        # Confidence increases with usage and performance
        confidence = min(1.0, (usage_count / 100.0) * (performance / 10.0))
        return confidence
    
    def _least_loaded_selection(self, workers: List[Dict[str, Any]], context: RoutingContext) -> Optional[Dict[str, Any]]:
        """Least loaded selection with adaptive weights"""
        if not workers:
            return None
        
        # Apply learned weights to load calculation
        weighted_workers = []
        for worker in workers:
            worker_id = worker.get('worker_id')
            base_load = worker.get('current_load', 1.0)
            weight = self.worker_weights.get(worker_id, 1.0)
            
            # Adjust load based on learned weight (higher weight = lower effective load)
            adjusted_load = base_load / max(weight, 0.1)
            weighted_workers.append((adjusted_load, worker))
        
        # Select worker with lowest adjusted load
        weighted_workers.sort(key=lambda x: x[0])
        return weighted_workers[0][1]
    
    def _fastest_response_selection(self, workers: List[Dict[str, Any]], context: RoutingContext) -> Optional[Dict[str, Any]]:
        """Fastest response selection with adaptive weights"""
        if not workers:
            return None
        
        # Use worker weights as performance indicators
        weighted_workers = []
        for worker in workers:
            worker_id = worker.get('worker_id')
            weight = self.worker_weights.get(worker_id, 1.0)
            weighted_workers.append((weight, worker))
        
        # Select worker with highest weight (best performance)
        weighted_workers.sort(key=lambda x: x[0], reverse=True)
        return weighted_workers[0][1]
    
    def _round_robin_selection(self, workers: List[Dict[str, Any]], context: RoutingContext) -> Optional[Dict[str, Any]]:
        """Round robin selection with adaptive weights"""
        if not workers:
            return None
        
        # Weight-based round robin
        total_weight = sum(self.worker_weights.get(w.get('worker_id'), 1.0) for w in workers)
        if total_weight == 0:
            return workers[0]
        
        import random
        target = random.uniform(0, total_weight)
        current = 0
        
        for worker in workers:
            worker_id = worker.get('worker_id')
            weight = self.worker_weights.get(worker_id, 1.0)
            current += weight
            if current >= target:
                return worker
        
        return workers[-1]  # Fallback
    
    def get_adaptation_stats(self) -> Dict[str, Any]:
        """Get comprehensive adaptation statistics"""
        return {
            "current_strategy": self.current_strategy,
            "strategy_performance": self.strategy_performance.copy(),
            "strategy_usage_count": self.strategy_usage_count.copy(),
            "worker_weights": self.worker_weights.copy(),
            "learning_rate": self.learning_rate,
            "adaptation_history_size": len(self.adaptation_history),
            "total_adaptations": sum(self.strategy_usage_count.values())
        }
    
    def get_strategy_name(self) -> str:
        return f"Adaptive (current: {self.current_strategy})"
