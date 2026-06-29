from __future__ import annotations

import threading
import time
from collections import defaultdict
from collections.abc import Callable
from functools import wraps
from contextlib import contextmanager
from typing import ParamSpec, TypeVar

import titan_state as S

P = ParamSpec("P")
R = TypeVar("R")

_active_jobs: defaultdict[str, int] = defaultdict(int)
_lock = threading.Lock()


def monitored_job(
    name: str,
    warn_after: float = 5.0,
    log_label: str | None = None,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        @wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            start = time.perf_counter()
            thread_name = threading.current_thread().name
            context_label = log_label or name

            with _lock:
                _active_jobs[name] += 1
                active_count = _active_jobs[name]

                S._log(
                    f"OVERLAP detected: job={name} active_instances={active_count} thread={thread_name}",
                    "WARN",
                )

            with S.log_context(context_label):
                S._log(f"START job={name} thread={thread_name}", "INFO")

                try:
                    return func(*args, **kwargs)
                except Exception as exc:
                    S._log(f"ERROR job={name} thread={thread_name} error={exc}", "ERR")
                    raise
                finally:
                    elapsed = time.perf_counter() - start
                    with _lock:
                        _active_jobs[name] -= 1

                    level = "WARN" if elapsed > warn_after else "INFO"
                    S._log(
                        f"END job={name} duration={elapsed:.3f}s thread={thread_name}",
                        level,
                    )

        return wrapper

    return decorator


@contextmanager
def monitored_step(name: str, warn_after: float = 5.0, *, level: str = "DIAG"):
    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed = time.perf_counter() - start
        end_level = "WARN" if elapsed > warn_after else level
        S._log(f"STEP {name} duration={elapsed:.3f}s", end_level)


def start_monitored_thread(
    *,
    job_name: str,
    target: Callable[[], object],
    warn_after: float = 5.0,
    daemon: bool = True,
    thread_name: str | None = None,
    log_label: str | None = None,
) -> threading.Thread | None:
    name = thread_name or job_name
    for t in threading.enumerate():
        if t.name == name and t.is_alive():
            S._log(f"[{log_label or job_name}] still running, skipping new spawn", "DIAG")
            return None
    wrapped_target = monitored_job(job_name, warn_after, log_label)(target)
    thread = threading.Thread(
        target=wrapped_target,
        daemon=daemon,
        name=name,
    )
    thread.start()
    return thread
