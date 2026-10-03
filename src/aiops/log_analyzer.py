"""
Log Analyzer - pattern matching and AI-powered log analysis.

Scans container log output for known error patterns (OOMKill, connection
refused, stack traces, etc.) and optionally enriches with AI classification.
"""

import asyncio
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import structlog

from src.config import get_settings

logger = structlog.get_logger()

# Default maximum log size allowed for analysis (10 MB)
_DEFAULT_MAX_LOG_BYTES = 10 * 1024 * 1024


class LogSeverity(StrEnum):
    CRITICAL = "CRITICAL"
    ERROR = "ERROR"
    WARNING = "WARNING"
    INFO = "INFO"
    NORMAL = "NORMAL"


@dataclass
class LogMatch:
    pattern_name: str
    severity: LogSeverity
    matched_lines: list[str]
    count: int = 0


@dataclass
class LogAnalysisResult:
    pod_name: str
    namespace: str
    total_lines: int
    error_count: int
    warning_count: int
    detected_patterns: list[LogMatch]
    summary: str
    raw_errors: list[str]
    ai_classification: str | None = None
    # Generic domain-agnostic identifier (SPEC-006) — e.g. "nutanix-prod/vm-42",
    # "prod-rg/web-01". Defaults to pod_name for back-compat K8s callers.
    source_id: str = ""

    def to_markdown(self) -> str:
        label = self.pod_name
        scope = f" (ns: `{self.namespace}`)" if self.namespace else ""
        if not self.detected_patterns:
            return f"✅ No critical patterns detected in logs for `{label}` ({self.total_lines} lines analyzed)"

        lines = [
            f"**📋 Log Analysis: `{label}`**{scope}",
            f"Lines analyzed: {self.total_lines} | Errors: {self.error_count} | Warnings: {self.warning_count}",
            "",
            "**Detected Patterns:**",
        ]
        for match in self.detected_patterns:
            severity_emoji = {"CRITICAL": "🔴", "ERROR": "🟠", "WARNING": "🟡"}.get(
                match.severity.value, "ℹ️"
            )
            lines.append(f"{severity_emoji} **{match.pattern_name}** ({match.count} occurrences)")
            for line in match.matched_lines[:2]:
                lines.append(f"  `{line[:120]}`")

        if self.ai_classification:
            lines += ["", f"**AI Analysis:** {self.ai_classification}"]

        return "\n".join(lines)


# ── Pattern definitions ────────────────────────────────────────────────────────

PATTERNS: list[tuple[str, LogSeverity, str]] = [
    # (pattern_name, severity, regex)
    (
        "OOMKill",
        LogSeverity.CRITICAL,
        r"(?i)(oom.?kill|out.?of.?memory|cannot allocate memory|kill process)",
    ),
    ("Segfault", LogSeverity.CRITICAL, r"(?i)(segmentation fault|SIGSEGV|core dumped)"),
    ("Panic", LogSeverity.CRITICAL, r"(?i)(panic:|PANIC |fatal error:|FATAL )"),
    (
        "Java StackTrace",
        LogSeverity.ERROR,
        r"(?i)(Exception in thread|java\.lang\.|at com\.|at org\.|Caused by:)",
    ),
    (
        "Python Traceback",
        LogSeverity.ERROR,
        r"(?i)(Traceback \(most recent call last\)|File \".*\", line \d+)",
    ),
    (
        "Connection Refused",
        LogSeverity.ERROR,
        r"(?i)(connection refused|ECONNREFUSED|could not connect)",
    ),
    (
        "Connection Timeout",
        LogSeverity.ERROR,
        r"(?i)(connection timed out|ETIMEDOUT|dial tcp.*timeout|context deadline exceeded)",
    ),
    (
        "DNS Failure",
        LogSeverity.ERROR,
        r"(?i)(no such host|DNS resolution failed|name resolution|getaddrinfo|NXDOMAIN)",
    ),
    (
        "TLS/SSL Error",
        LogSeverity.ERROR,
        r"(?i)(tls handshake|ssl error|certificate verify|x509:|bad certificate)",
    ),
    (
        "Authentication Failed",
        LogSeverity.ERROR,
        r"(?i)(authentication failed|unauthorized|invalid token|access denied|permission denied)",
    ),
    ("Disk Full", LogSeverity.CRITICAL, r"(?i)(no space left on device|disk full|ENOSPC)"),
    (
        "File Not Found",
        LogSeverity.WARNING,
        r"(?i)(no such file or directory|file not found|ENOENT)",
    ),
    (
        "Port Already In Use",
        LogSeverity.ERROR,
        r"(?i)(address already in use|EADDRINUSE|bind: address)",
    ),
    (
        "Database Error",
        LogSeverity.ERROR,
        r"(?i)(database error|db connection|SQL error|query failed|deadlock detected|too many connections)",
    ),
]


class LogAnalyzer:
    """Regex + AI log pattern analyzer."""

    # Class-level compiled pattern cache — built once, shared across all instances.
    _compiled: list[tuple[str, LogSeverity, re.Pattern[str]]] | None = None

    @classmethod
    def _get_compiled(cls) -> list[tuple[str, LogSeverity, re.Pattern[str]]]:
        if cls._compiled is None:
            cls._compiled = [(n, s, re.compile(p)) for n, s, p in PATTERNS]
        return cls._compiled

    def analyze(
        self,
        pod_name: str,
        namespace: str,
        logs: str,
        ai_client: Any = None,
    ) -> LogAnalysisResult:
        """Analyze log text synchronously (regex only).

        Back-compat Kubernetes-shaped wrapper around `analyze_text()`.
        """
        source_id = f"{namespace}/{pod_name}" if namespace else pod_name
        result = self.analyze_text(source_id, pod_name, logs)
        result.namespace = namespace
        return result

    def analyze_text(self, source_id: str, source_label: str, logs: str) -> LogAnalysisResult:
        """Analyze log-like text from any domain (K8s pod logs, Azure Activity Log
        entries, VM platform console/health logs) synchronously (regex only).

        Args:
            source_id: Generic domain-agnostic identifier, e.g. "payments/web-7f9c",
                "nutanix-prod/vm-42", "prod-rg/web-01".
            source_label: Short display name shown in markdown output.
            logs: Raw log/activity-log text to scan.
        """
        settings = get_settings()
        max_bytes = getattr(settings, "max_log_bytes", _DEFAULT_MAX_LOG_BYTES)
        if len(logs.encode("utf-8", errors="replace")) > max_bytes:
            logger.warning(
                "log_analyzer_oversized_log",
                source=source_id,
                size_bytes=len(logs),
                max_bytes=max_bytes,
            )
            # Truncate to last max_bytes worth of characters (approximate)
            logs = logs[-max_bytes:]

        lines = logs.strip().split("\n") if logs else []
        total_lines = len(lines)
        error_count = 0
        warning_count = 0
        detected: list[LogMatch] = []
        raw_errors: list[str] = []

        for name, severity, regex in self._get_compiled():
            matched_lines = [line for line in lines if regex.search(line)]
            if matched_lines:
                if severity in (LogSeverity.CRITICAL, LogSeverity.ERROR):
                    error_count += len(matched_lines)
                    raw_errors.extend(matched_lines[:3])
                elif severity == LogSeverity.WARNING:
                    warning_count += len(matched_lines)
                detected.append(
                    LogMatch(
                        pattern_name=name,
                        severity=severity,
                        matched_lines=matched_lines[:5],
                        count=len(matched_lines),
                    )
                )

        # Sort by severity
        severity_order = {LogSeverity.CRITICAL: 0, LogSeverity.ERROR: 1, LogSeverity.WARNING: 2}
        detected.sort(key=lambda m: severity_order.get(m.severity, 3))

        if detected:
            top = detected[0]
            summary = f"{top.pattern_name} detected ({top.count}x) in {source_label} — {total_lines} lines analyzed"
        else:
            summary = f"No critical patterns detected in {total_lines} log lines"

        return LogAnalysisResult(
            pod_name=source_label,
            namespace="",
            total_lines=total_lines,
            error_count=error_count,
            warning_count=warning_count,
            detected_patterns=detected,
            summary=summary,
            raw_errors=list(dict.fromkeys(raw_errors))[:10],
            source_id=source_id,
        )

    async def analyze_with_ai(
        self,
        pod_name: str,
        namespace: str,
        logs: str,
        ai_client: Any,
        context_label: str | None = None,
    ) -> LogAnalysisResult:
        """Analyze logs with regex first, then enrich with AI classification."""
        result = self.analyze(pod_name, namespace, logs)
        label = context_label or f"Kubernetes pod logs for {namespace}/{pod_name}"
        return await self._enrich_with_ai(result, logs, ai_client, label)

    async def analyze_text_with_ai(
        self,
        source_id: str,
        source_label: str,
        logs: str,
        ai_client: Any,
        context_label: str | None = None,
    ) -> LogAnalysisResult:
        """Domain-agnostic analyze_text() + AI enrichment entrypoint (SPEC-006)."""
        result = self.analyze_text(source_id, source_label, logs)
        return await self._enrich_with_ai(result, logs, ai_client, context_label or source_label)

    async def _enrich_with_ai(
        self,
        result: LogAnalysisResult,
        logs: str,
        ai_client: Any,
        context_label: str,
    ) -> LogAnalysisResult:
        if not ai_client:
            return result

        settings = get_settings()
        timeout = getattr(settings, "log_ai_timeout_seconds", 15)
        try:
            log_sample = "\n".join(logs.strip().split("\n")[-30:])
            patterns_found = ", ".join(m.pattern_name for m in result.detected_patterns) or "none"
            prompt = (
                f"You are an SRE. Analyze this {context_label} and provide a 2-3 sentence "
                f"diagnosis. Already detected patterns: {patterns_found}.\n\n"
                f"Log sample:\n```\n{log_sample}\n```\n\n"
                f"Provide: failure cause, impact, and immediate remediation suggestion."
            )
            ai_response, _tokens = await asyncio.wait_for(
                ai_client.generate_response(
                    messages=[{"role": "user", "content": prompt}],
                    model="gpt-4o-mini",
                    max_tokens=300,
                ),
                timeout=float(timeout),
            )
            result.ai_classification = ai_response.strip()
        except TimeoutError:
            logger.warning("log_ai_analysis_timeout", timeout_seconds=timeout)
        except Exception as e:
            logger.warning("log_ai_analysis_failed", error=str(e))

        return result
