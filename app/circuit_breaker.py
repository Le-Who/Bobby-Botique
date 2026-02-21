"""
Circuit Breaker pattern implementation for external API calls.
Provides automatic failure detection and recovery mechanisms.
"""

import asyncio
import time
import logging
from typing import Callable, Any, Optional, Dict
from enum import Enum
from dataclasses import dataclass
from app.exceptions import CircuitBreakerOpenError


class CircuitState(Enum):
    """Circuit breaker states."""

    CLOSED = "closed"  # Normal operation
    OPEN = "open"  # Failing, rejecting requests
    HALF_OPEN = "half_open"  # Testing if service recovered


@dataclass
class CircuitBreakerConfig:
    """Configuration for circuit breaker."""

    failure_threshold: int = 5  # Number of failures before opening
    recovery_timeout: float = 60.0  # Time to wait before half-open (seconds)
    expected_exception: type = Exception  # Exception type to consider as failure
    monitor_interval: float = 10.0  # Interval for monitoring (seconds)
    max_failures: int = 100  # Maximum failures to track


class CircuitBreaker:
    """
    Circuit Breaker implementation for external service calls.

    Provides automatic failure detection and recovery:
    - CLOSED: Normal operation, all requests pass through
    - OPEN: Service failing, all requests rejected immediately
    - HALF_OPEN: Testing if service recovered, limited requests allowed
    """

    def __init__(self, name: str, config: Optional[CircuitBreakerConfig] = None):
        self.name = name
        self.config = config or CircuitBreakerConfig()

        # State management
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._last_failure_time = 0.0
        self._last_success_time = time.time()

        # Monitoring
        self._total_requests = 0
        self._total_failures = 0
        self._total_successes = 0

        # Async lock for thread safety
        self._lock = asyncio.Lock()

        # Start monitoring task
        self._monitor_task: Optional[asyncio.Task] = None
        self._start_monitoring()

        logging.info(f"Circuit Breaker '{name}' initialized with config: {self.config}")

    async def call(self, func: Callable, *args, **kwargs) -> Any:
        """
        Executes a function with circuit breaker protection.

        Args:
            func: Function to execute
            *args, **kwargs: Arguments for the function

        Returns:
            Result of the function execution

        Raises:
            CircuitBreakerOpenError: When circuit is open
            Exception: Original exception from function execution
        """
        async with self._lock:
            # Check if circuit is open
            if self._state == CircuitState.OPEN:
                if self._should_attempt_reset():
                    await self._set_state(CircuitState.HALF_OPEN)
                    logging.info(
                        f"Circuit Breaker '{self.name}' moved to HALF_OPEN state"
                    )
                else:
                    raise CircuitBreakerOpenError(
                        f"Circuit Breaker '{self.name}' is OPEN. "
                        f"Last failure: {time.time() - self._last_failure_time:.1f}s ago. "
                        f"Reset in: {self.config.recovery_timeout - (time.time() - self._last_failure_time):.1f}s"
                    )

            # Execute function
            try:
                self._total_requests += 1
                result = await func(*args, **kwargs)

                # Success - close circuit if it was half-open
                if self._state == CircuitState.HALF_OPEN:
                    await self._set_state(CircuitState.CLOSED)
                    logging.info(
                        f"Circuit Breaker '{self.name}' recovered, moved to CLOSED state"
                    )

                self._failure_count = 0
                self._last_success_time = time.time()
                self._total_successes += 1

                return result

            except Exception as e:
                # Check if this exception should be considered a failure
                if isinstance(e, self.config.expected_exception):
                    await self._on_failure(e)
                    raise
                else:
                    # Unexpected exception - don't count as circuit breaker failure
                    logging.warning(
                        f"Circuit Breaker '{self.name}' received unexpected exception: {e}"
                    )
                    raise

    async def _on_failure(self, exception: Exception) -> None:
        """Handles function execution failure."""
        self._failure_count += 1
        self._total_failures += 1
        self._last_failure_time = time.time()

        logging.warning(
            f"Circuit Breaker '{self.name}' failure #{self._failure_count}: {exception}"
        )

        # Check if we should open the circuit
        if self._failure_count >= self.config.failure_threshold:
            if self._state != CircuitState.OPEN:
                await self._set_state(CircuitState.OPEN)
                logging.error(
                    f"Circuit Breaker '{self.name}' opened after {self._failure_count} failures"
                )

    async def _set_state(self, new_state: CircuitState) -> None:
        """Changes circuit breaker state."""
        old_state = self._state
        self._state = new_state

        if old_state != new_state:
            logging.info(
                f"Circuit Breaker '{self.name}' state changed: {old_state.value} -> {new_state.value}"
            )

    def _should_attempt_reset(self) -> bool:
        """Determines if circuit should attempt reset."""
        if self._state != CircuitState.OPEN:
            return False

        time_since_failure = time.time() - self._last_failure_time
        return time_since_failure >= self.config.recovery_timeout

    def _start_monitoring(self) -> None:
        """Starts the monitoring task."""
        if self._monitor_task and not self._monitor_task.done():
            return

        self._monitor_task = asyncio.create_task(self._monitor_loop())

    async def _monitor_loop(self) -> None:
        """Monitoring loop for circuit breaker metrics."""
        while True:
            try:
                await asyncio.sleep(self.config.monitor_interval)

                # Log periodic status
                if self._total_requests > 0:
                    success_rate = (self._total_successes / self._total_requests) * 100
                    logging.info(
                        f"Circuit Breaker '{self.name}' status: "
                        f"State={self._state.value}, "
                        f"Success Rate={success_rate:.1f}%, "
                        f"Total Requests={self._total_requests}, "
                        f"Failures={self._total_failures}"
                    )

                # Clean up old failure data if too many
                if self._failure_count > self.config.max_failures:
                    self._failure_count = min(
                        self._failure_count, self.config.max_failures
                    )

            except asyncio.CancelledError:
                break
            except Exception as e:
                logging.error(f"Circuit Breaker '{self.name}' monitoring error: {e}")

    def get_state(self) -> CircuitState:
        """Returns current circuit breaker state."""
        return self._state

    def get_stats(self) -> Dict[str, Any]:
        """Returns circuit breaker statistics."""
        return {
            "name": self.name,
            "state": self._state.value,
            "failure_count": self._failure_count,
            "total_requests": self._total_requests,
            "total_failures": self._total_failures,
            "total_successes": self._total_successes,
            "last_failure_time": self._last_failure_time,
            "last_success_time": self._last_success_time,
            "success_rate": (self._total_successes / self._total_requests * 100)
            if self._total_requests > 0
            else 0,
        }

    async def force_open(self) -> None:
        """Forces circuit breaker to open state."""
        async with self._lock:
            await self._set_state(CircuitState.OPEN)
            self._failure_count = self.config.failure_threshold
            self._last_failure_time = time.time()

    async def force_close(self) -> None:
        """Forces circuit breaker to closed state."""
        async with self._lock:
            await self._set_state(CircuitState.CLOSED)
            self._failure_count = 0

    async def reset(self) -> None:
        """Resets circuit breaker to initial state."""
        async with self._lock:
            await self._set_state(CircuitState.CLOSED)
            self._failure_count = 0
            self._last_failure_time = 0.0
            self._total_requests = 0
            self._total_failures = 0
            self._total_successes = 0

    async def shutdown(self) -> None:
        """Shuts down the circuit breaker."""
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass


# Global circuit breaker instances
_circuit_breakers: Dict[str, CircuitBreaker] = {}


def get_circuit_breaker(
    name: str, config: Optional[CircuitBreakerConfig] = None
) -> CircuitBreaker:
    """
    Gets or creates a circuit breaker instance.

    Args:
        name: Unique name for the circuit breaker
        config: Configuration for the circuit breaker

    Returns:
        CircuitBreaker instance
    """
    if name not in _circuit_breakers:
        _circuit_breakers[name] = CircuitBreaker(name, config)

    return _circuit_breakers[name]


async def shutdown_all_circuit_breakers() -> None:
    """Shuts down all circuit breaker instances."""
    for cb in _circuit_breakers.values():
        await cb.shutdown()
    _circuit_breakers.clear()
    logging.info("All circuit breakers shut down")


# Predefined configurations for common use cases
GEMINI_API_CONFIG = CircuitBreakerConfig(
    failure_threshold=3,
    recovery_timeout=30.0,
    expected_exception=Exception,
    monitor_interval=5.0,
)

TAVILY_API_CONFIG = CircuitBreakerConfig(
    failure_threshold=5,
    recovery_timeout=60.0,
    expected_exception=Exception,
    monitor_interval=10.0,
)

TELEGRAM_API_CONFIG = CircuitBreakerConfig(
    failure_threshold=10,
    recovery_timeout=120.0,
    expected_exception=Exception,
    monitor_interval=15.0,
)
