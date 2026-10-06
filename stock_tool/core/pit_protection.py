from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class ProtectionProfile:
    name: str
    min_interval_sec: float
    retry_delays_sec: tuple[float, ...]
    jitter_ratio: float
    max_consecutive_retryable_failures: int
    cooldown_sec: float


PROTECTION_PROFILES: dict[str, ProtectionProfile] = {
    "balanced": ProtectionProfile(
        name="balanced",
        min_interval_sec=0.8,
        retry_delays_sec=(2.0, 5.0, 10.0),
        jitter_ratio=0.2,
        max_consecutive_retryable_failures=4,
        cooldown_sec=60.0,
    ),
    "conservative": ProtectionProfile(
        name="conservative",
        min_interval_sec=1.5,
        retry_delays_sec=(5.0, 10.0, 20.0),
        jitter_ratio=0.2,
        max_consecutive_retryable_failures=3,
        cooldown_sec=120.0,
    ),
}


def get_protection_profile(name: str | ProtectionProfile | None) -> ProtectionProfile:
    if isinstance(name, ProtectionProfile):
        return name
    key = (name or "balanced").strip().lower()
    if key not in PROTECTION_PROFILES:
        raise ValueError(f"unsupported protection profile: {name}")
    return PROTECTION_PROFILES[key]


class SourceCircuitOpen(RuntimeError):
    def __init__(self, source: str, reason: str, cooldown_sec: float, consecutive_failures: int) -> None:
        super().__init__(reason)
        self.source = source
        self.reason = reason
        self.cooldown_sec = float(cooldown_sec)
        self.consecutive_failures = int(consecutive_failures)


@dataclass(frozen=True)
class ProtectionEvent:
    kind: str
    source: str
    target: str
    attempt: int = 0
    delay_sec: float = 0.0
    error_class: str = ""
    message: str = ""
    duration_ms: int = 0


ProtectionEventSink = Callable[[ProtectionEvent], None]
ErrorClassifier = Callable[[BaseException], str]
RetryablePredicate = Callable[[BaseException], bool]


class RequestProtector:
    def __init__(
        self,
        source: str,
        profile: str | ProtectionProfile | None = None,
        *,
        sleep_func: Callable[[float], None] = time.sleep,
        monotonic_func: Callable[[], float] = time.monotonic,
        random_func: Callable[[], float] = random.random,
        event_sink: ProtectionEventSink | None = None,
    ) -> None:
        self.source = source
        self.profile = get_protection_profile(profile)
        self._sleep = sleep_func
        self._monotonic = monotonic_func
        self._random = random_func
        self._event_sink = event_sink
        self._last_request_at: float | None = None
        self._consecutive_retryable_failures = 0
        self._circuit_reason = ""

    @property
    def profile_name(self) -> str:
        return self.profile.name

    def set_event_sink(self, sink: ProtectionEventSink | None) -> None:
        self._event_sink = sink

    def call(
        self,
        target: str,
        fn: Callable[[], object],
        *,
        error_classifier: ErrorClassifier | None = None,
        retryable: RetryablePredicate | None = None,
    ) -> object:
        if self._circuit_reason:
            self._emit(
                "circuit_open",
                target,
                error_class="SOURCE_COOLDOWN",
                message=self._circuit_reason,
            )
            raise SourceCircuitOpen(
                self.source,
                self._circuit_reason,
                self.profile.cooldown_sec,
                self._consecutive_retryable_failures,
            )

        attempt = 1
        while True:
            self._wait_for_interval()
            started = self._monotonic()
            self._last_request_at = started
            self._emit("request", target, attempt=attempt)
            try:
                result = fn()
            except Exception as exc:
                duration_ms = int((self._monotonic() - started) * 1000)
                error_class = error_classifier(exc) if error_classifier else exc.__class__.__name__
                should_retry = retryable(exc) if retryable else True
                if should_retry:
                    self._consecutive_retryable_failures += 1
                self._emit(
                    "request_failed",
                    target,
                    attempt=attempt,
                    error_class=error_class,
                    message=str(exc),
                    duration_ms=duration_ms,
                )
                if should_retry and self._should_open_circuit():
                    reason = (
                        f"{self.source} cooled down after "
                        f"{self._consecutive_retryable_failures} consecutive retryable failures"
                    )
                    if error_class:
                        reason = f"{reason}: {error_class}"
                    if str(exc):
                        reason = f"{reason} - {str(exc)}"
                    self._circuit_reason = reason
                    self._emit(
                        "cooldown",
                        target,
                        attempt=attempt,
                        delay_sec=self.profile.cooldown_sec,
                        error_class="SOURCE_COOLDOWN",
                        message=reason,
                    )
                    self._emit(
                        "circuit_open",
                        target,
                        attempt=attempt,
                        error_class="SOURCE_COOLDOWN",
                        message=reason,
                    )
                    raise SourceCircuitOpen(
                        self.source,
                        reason,
                        self.profile.cooldown_sec,
                        self._consecutive_retryable_failures,
                    ) from exc
                if should_retry and attempt <= len(self.profile.retry_delays_sec):
                    delay = self._jittered_delay(self.profile.retry_delays_sec[attempt - 1])
                    self._emit(
                        "retry",
                        target,
                        attempt=attempt,
                        delay_sec=delay,
                        error_class=error_class,
                        message=str(exc),
                    )
                    self._sleep(delay)
                    attempt += 1
                    continue
                raise
            else:
                duration_ms = int((self._monotonic() - started) * 1000)
                self._consecutive_retryable_failures = 0
                self._emit("request_success", target, attempt=attempt, duration_ms=duration_ms)
                return result

    def _wait_for_interval(self) -> None:
        if self._last_request_at is None:
            return
        elapsed = self._monotonic() - self._last_request_at
        wait_sec = self.profile.min_interval_sec - elapsed
        if wait_sec > 0:
            self._sleep(wait_sec)

    def _jittered_delay(self, base_delay: float) -> float:
        ratio = max(0.0, self.profile.jitter_ratio)
        if ratio <= 0:
            return float(base_delay)
        offset = ((self._random() * 2.0) - 1.0) * ratio
        return max(0.0, float(base_delay) * (1.0 + offset))

    def _should_open_circuit(self) -> bool:
        return self._consecutive_retryable_failures >= self.profile.max_consecutive_retryable_failures

    def _emit(
        self,
        kind: str,
        target: str,
        *,
        attempt: int = 0,
        delay_sec: float = 0.0,
        error_class: str = "",
        message: str = "",
        duration_ms: int = 0,
    ) -> None:
        if self._event_sink is None:
            return
        self._event_sink(
            ProtectionEvent(
                kind=kind,
                source=self.source,
                target=target,
                attempt=attempt,
                delay_sec=delay_sec,
                error_class=error_class,
                message=message,
                duration_ms=duration_ms,
            )
        )
