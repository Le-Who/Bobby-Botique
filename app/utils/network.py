"""
Network utilities for handling connection errors, timeouts, and retries.
"""

import asyncio
import logging
from collections.abc import Callable
from functools import wraps
from typing import Any

import httpx


class NetworkErrorHandler:
    """Handles network errors with retry logic and exponential backoff."""

    @staticmethod
    async def retry_with_backoff(
        func: Callable,
        max_retries: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 60.0,
        exceptions: tuple = (
            httpx.TimeoutException,
            httpx.ConnectError,
            httpx.ReadError,
        ),
        *args,
        **kwargs,
    ) -> Any:
        """
        Executes a function with retry logic and exponential backoff.

        Args:
            func: Async function to execute
            max_retries: Maximum number of retry attempts
            base_delay: Base delay between retries in seconds
            max_delay: Maximum delay between retries in seconds
            exceptions: Tuple of exceptions to catch and retry
            *args, **kwargs: Arguments to pass to the function

        Returns:
            Result of the function execution

        Raises:
            Last exception if all retries fail
        """
        last_exception = None

        for attempt in range(max_retries + 1):
            try:
                return await func(*args, **kwargs)
            except exceptions as e:
                last_exception = e

                if attempt == max_retries:
                    logging.error(
                        f"Max retries ({max_retries}) reached for {func.__name__}: {e}"
                    )
                    raise

                # Calculate delay with exponential backoff
                delay = min(base_delay * (2**attempt), max_delay)
                logging.warning(
                    f"Network error on attempt {attempt + 1}/{max_retries + 1} for {func.__name__}: {e}"
                )
                logging.info("Retrying in %s seconds...", delay)

                await asyncio.sleep(delay)

        # This should never be reached, but just in case
        raise last_exception

    @staticmethod
    def create_robust_http_client(
        connect_timeout: float = 10.0,
        read_timeout: float = 30.0,
        write_timeout: float = 30.0,
        pool_timeout: float = 60.0,
        max_keepalive_connections: int = 20,
        max_connections: int = 50,
        keepalive_expiry: float = 30.0,
    ) -> httpx.AsyncClient:
        """
        Creates an HTTP client with robust timeout and connection settings.

        Args:
            connect_timeout: Timeout for establishing connection
            read_timeout: Timeout for reading response
            write_timeout: Timeout for writing request
            pool_timeout: Timeout for getting connection from pool
            max_keepalive_connections: Maximum keepalive connections
            max_connections: Maximum total connections
            keepalive_expiry: Keepalive connection expiry time

        Returns:
            Configured httpx.AsyncClient
        """
        return httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=connect_timeout,
                read=read_timeout,
                write=write_timeout,
                pool=pool_timeout,
            ),
            limits=httpx.Limits(
                max_keepalive_connections=max_keepalive_connections,
                max_connections=max_connections,
                keepalive_expiry=keepalive_expiry,
            ),
        )

    @staticmethod
    async def check_connectivity(
        url: str = "https://api.telegram.org", timeout: float = 5.0
    ) -> bool:
        """
        Checks network connectivity to a given URL.

        Args:
            url: URL to check connectivity to
            timeout: Timeout for the check

        Returns:
            True if connectivity is available, False otherwise
        """
        try:
            timeout_config = httpx.Timeout(connect=timeout, read=timeout)
            async with httpx.AsyncClient(timeout=timeout_config) as client:
                response = await client.get(url)
                return response.status_code == 200
        except Exception as e:
            logging.info("Connectivity check failed for %s: %s", url, e)
            return False


def network_retry_decorator(
    max_retries: int = 3,
    base_delay: float = 1.0,
    exceptions: tuple = (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError),
):
    """
    Decorator for adding network retry logic to async functions.

    Args:
        max_retries: Maximum number of retry attempts
        base_delay: Base delay between retries in seconds
        exceptions: Tuple of exceptions to catch and retry
    """

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args, **kwargs):
            return await NetworkErrorHandler.retry_with_backoff(
                func, max_retries, base_delay, *args, exceptions=exceptions, **kwargs
            )

        return wrapper

    return decorator
