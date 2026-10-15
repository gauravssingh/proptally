"""SQLite single-writer retry decorator for concurrency handling."""

import sqlite3
import time
from functools import wraps
from typing import Callable, Any


def with_sqlite_retry(max_retries: int = 5, backoff_base: float = 0.05) -> Callable:
    """Decorator ensuring that busy lock contentions retry gracefully with exponential backoff.
    
    Args:
        max_retries: Maximum number of retry attempts.
        backoff_base: Initial sleep duration in seconds before exponential doubling.
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except sqlite3.OperationalError as exc:
                    if "database is locked" in str(exc).lower() and attempt < max_retries - 1:
                        sleep_time = backoff_base * (2 ** attempt)
                        time.sleep(sleep_time)
                    else:
                        raise
        return wrapper
    return decorator
