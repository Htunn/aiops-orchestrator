"""
MCP Server for API Backend Diagnostic Tools.

Provides tools for diagnosing API backend issues:
- DNS lookup
- curl/HTTP test
- SSL certificate check
- Traceroute
"""

import asyncio
import json
import socket
import ssl
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import httpx
import structlog

logger = structlog.get_logger()


class ApiDiagnosticTools:
    """Collection of API diagnostic tools for MCP."""

    @staticmethod
    async def dns_lookup(hostname: str) -> dict[str, Any]:
        """
        Perform DNS lookup for a hostname.

        Args:
            hostname: Domain name to resolve (e.g., "api.example.com" or "https://api.example.com")

        Returns:
            Dict with resolved IP addresses and timing
        """
        # Extract hostname from URL if provided
        if hostname.startswith(("http://", "https://")):
            parsed = urlparse(hostname)
            hostname = parsed.hostname or hostname

        try:
            start = datetime.now(UTC)
            loop = asyncio.get_event_loop()
            addrs = await loop.getaddrinfo(hostname, None, family=socket.AF_UNSPEC)
            duration_ms = (datetime.now(UTC) - start).total_seconds() * 1000

            # Extract unique IP addresses
            ips: list[str] = list({str(addr[4][0]) for addr in addrs})

            return {
                "success": True,
                "hostname": hostname,
                "ip_addresses": ips,
                "count": len(ips),
                "lookup_time_ms": round(duration_ms, 2),
                "message": f"Resolved {hostname} to {len(ips)} address(es): {', '.join(ips)}",
            }
        except socket.gaierror as e:
            return {
                "success": False,
                "hostname": hostname,
                "error": str(e),
                "message": f"DNS lookup failed for {hostname}: {e}",
            }
        except Exception as e:
            return {
                "success": False,
                "hostname": hostname,
                "error": str(e),
                "message": f"DNS lookup error: {e}",
            }

    @staticmethod
    async def curl_test(
        url: str,
        timeout: int = 10,
        repeat: int = 1,
        verbose: bool = False,
        method: str = "GET",
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """
        Perform HTTP request(s) to test API endpoint connectivity and latency.

        Args:
            url: Full URL to test
            timeout: Request timeout in seconds
            repeat: Number of times to repeat the request (for latency analysis)
            verbose: Include response body and headers in output
            method: HTTP method (GET, POST, etc.)
            headers: Custom headers to include

        Returns:
            Dict with response status, latency, and optional body
        """
        results = []
        errors = []

        for i in range(repeat):
            try:
                start = datetime.now(UTC)
                async with httpx.AsyncClient(
                    timeout=float(timeout), follow_redirects=True
                ) as client:
                    response = await client.request(method, url, headers=headers or {})
                duration_ms = (datetime.now(UTC) - start).total_seconds() * 1000

                result: dict[str, Any] = {
                    "attempt": i + 1,
                    "status_code": response.status_code,
                    "latency_ms": round(duration_ms, 2),
                    "success": 200 <= response.status_code < 300,
                }

                if verbose:
                    result["response_headers"] = dict(response.headers)
                    result["response_body"] = response.text[:1000]  # First 1KB

                results.append(result)

            except httpx.TimeoutException:
                errors.append(f"Attempt {i + 1}: Request timed out after {timeout}s")
            except httpx.ConnectError as e:
                errors.append(f"Attempt {i + 1}: Connection failed: {e}")
            except Exception as e:
                errors.append(f"Attempt {i + 1}: {type(e).__name__}: {e}")

        # Calculate summary statistics
        if results:
            latencies = [r["latency_ms"] for r in results]
            avg_latency = sum(latencies) / len(latencies)
            min_latency = min(latencies)
            max_latency = max(latencies)
            success_count = sum(1 for r in results if r["success"])

            return {
                "success": success_count > 0,
                "url": url,
                "method": method,
                "attempts": len(results),
                "successes": success_count,
                "failures": len(errors),
                "avg_latency_ms": round(avg_latency, 2),
                "min_latency_ms": round(min_latency, 2),
                "max_latency_ms": round(max_latency, 2),
                "results": results if verbose else results[:1],  # Show all in verbose mode
                "errors": errors if errors else None,
                "message": f"{method} {url}: {success_count}/{repeat} successful, avg latency {avg_latency:.0f}ms",
            }
        else:
            return {
                "success": False,
                "url": url,
                "method": method,
                "errors": errors,
                "message": f"All {repeat} attempt(s) failed: {'; '.join(errors)}",
            }

    @staticmethod
    async def ssl_check(url: str) -> dict[str, Any]:
        """
        Check SSL/TLS certificate validity for an HTTPS endpoint.

        Args:
            url: HTTPS URL to check

        Returns:
            Dict with certificate details and expiration status
        """
        if not url.startswith("https://"):
            return {
                "success": False,
                "url": url,
                "message": "URL must use HTTPS protocol",
            }

        parsed = urlparse(url)
        hostname = parsed.hostname
        port = parsed.port or 443

        try:
            # Create SSL context
            context = ssl.create_default_context()

            # Connect and get certificate
            reader, writer = await asyncio.open_connection(hostname, port, ssl=context)

            # Get peer certificate
            sock = writer.get_extra_info("ssl_object")
            cert = sock.getpeercert()

            writer.close()
            await writer.wait_closed()

            # Parse certificate details
            not_after = datetime.strptime(cert["notAfter"], "%b %d %H:%M:%S %Y %Z")
            not_before = datetime.strptime(cert["notBefore"], "%b %d %H:%M:%S %Y %Z")
            now = datetime.now()

            days_until_expiry = (not_after - now).days
            is_valid = not_before <= now <= not_after
            is_expiring_soon = 0 < days_until_expiry <= 30

            # Extract subject alternative names (SANs)
            san_list = []
            for ext in cert.get("subjectAltName", []):
                if ext[0] == "DNS":
                    san_list.append(ext[1])

            return {
                "success": True,
                "url": url,
                "hostname": hostname,
                "is_valid": is_valid,
                "is_expiring_soon": is_expiring_soon,
                "days_until_expiry": days_until_expiry,
                "not_before": not_before.isoformat(),
                "not_after": not_after.isoformat(),
                "issuer": dict(cert["issuer"]),
                "subject": dict(cert["subject"]),
                "subject_alt_names": san_list,
                "message": (
                    f"Valid certificate, expires in {days_until_expiry} days"
                    if is_valid
                    else "Certificate invalid or expired"
                ),
            }

        except ssl.SSLError as e:
            return {
                "success": False,
                "url": url,
                "error": f"SSL error: {e}",
                "message": f"SSL certificate validation failed: {e}",
            }
        except Exception as e:
            return {
                "success": False,
                "url": url,
                "error": str(e),
                "message": f"Unable to retrieve SSL certificate: {e}",
            }

    @staticmethod
    async def traceroute(hostname: str, max_hops: int = 30, timeout: int = 2) -> dict[str, Any]:
        """
        Perform a simple traceroute to diagnose network path.

        Note: This is a simplified implementation using DNS lookups
        rather than actual ICMP/UDP traceroute (which requires root privileges).

        Args:
            hostname: Target hostname
            max_hops: Maximum number of hops to trace
            timeout: Timeout per hop in seconds

        Returns:
            Dict with route information
        """
        # Extract hostname from URL if provided
        if hostname.startswith(("http://", "https://")):
            parsed = urlparse(hostname)
            hostname = parsed.hostname or hostname

        # This is a placeholder implementation
        # Real traceroute requires raw sockets (root privileges) or external tools
        try:
            # Attempt DNS lookup as a basic connectivity check
            dns_result = await ApiDiagnosticTools.dns_lookup(hostname)

            if dns_result["success"]:
                return {
                    "success": True,
                    "hostname": hostname,
                    "destination_ips": dns_result["ip_addresses"],
                    "message": f"Destination {hostname} is reachable (DNS resolved to {', '.join(dns_result['ip_addresses'])})",
                    "note": "Full traceroute requires privileged access; use system 'traceroute' command for detailed path analysis",
                }
            else:
                return {
                    "success": False,
                    "hostname": hostname,
                    "error": dns_result["error"],
                    "message": f"Destination {hostname} is not reachable (DNS lookup failed)",
                }

        except Exception as e:
            return {
                "success": False,
                "hostname": hostname,
                "error": str(e),
                "message": f"Traceroute failed: {e}",
            }


# ── Tool Definitions for MCP ──────────────────────────────────────────────────


TOOLS = [
    {
        "name": "api_dns_lookup",
        "description": "Perform DNS lookup to resolve hostname to IP address(es). Useful for diagnosing API connectivity issues.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "hostname": {
                    "type": "string",
                    "description": "Hostname or URL to resolve (e.g., 'api.example.com' or 'https://api.example.com')",
                }
            },
            "required": ["hostname"],
        },
    },
    {
        "name": "api_curl_test",
        "description": "Perform HTTP request(s) to test API endpoint connectivity, latency, and response. Supports multiple attempts for latency analysis.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Full URL to test (must include http:// or https://)",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Request timeout in seconds (default: 10)",
                    "default": 10,
                },
                "repeat": {
                    "type": "integer",
                    "description": "Number of times to repeat the request for latency analysis (default: 1)",
                    "default": 1,
                },
                "verbose": {
                    "type": "boolean",
                    "description": "Include response headers and body in output (default: false)",
                    "default": False,
                },
                "method": {
                    "type": "string",
                    "description": "HTTP method (default: GET)",
                    "default": "GET",
                },
                "headers": {
                    "type": "object",
                    "description": "Custom HTTP headers to include",
                    "default": {},
                },
            },
            "required": ["url"],
        },
    },
    {
        "name": "api_ssl_check",
        "description": "Check SSL/TLS certificate validity, expiration date, and issuer for an HTTPS endpoint.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "HTTPS URL to check (must start with https://)",
                }
            },
            "required": ["url"],
        },
    },
    {
        "name": "api_traceroute",
        "description": "Perform basic network path analysis to diagnose connectivity to API endpoint. Note: This is a simplified implementation.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "hostname": {
                    "type": "string",
                    "description": "Target hostname or URL",
                },
                "max_hops": {
                    "type": "integer",
                    "description": "Maximum number of hops to trace (default: 30)",
                    "default": 30,
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout per hop in seconds (default: 2)",
                    "default": 2,
                },
            },
            "required": ["hostname"],
        },
    },
]


async def call_tool(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """
    Call an API diagnostic tool by name with given arguments.

    Args:
        tool_name: Name of the tool to call
        arguments: Tool arguments

    Returns:
        Tool execution result
    """
    tools_map: dict[str, Callable[..., Any]] = {
        "api_dns_lookup": ApiDiagnosticTools.dns_lookup,
        "api_curl_test": ApiDiagnosticTools.curl_test,
        "api_ssl_check": ApiDiagnosticTools.ssl_check,
        "api_traceroute": ApiDiagnosticTools.traceroute,
    }

    if tool_name not in tools_map:
        return {"error": f"Unknown tool: {tool_name}"}

    try:
        result = await tools_map[tool_name](**arguments)
        # Convert result to string for MCP response
        return {"content": [{"type": "text", "text": json.dumps(result, indent=2)}]}
    except Exception as e:
        logger.error("api_diagnostic_tool_error", tool=tool_name, error=str(e))
        return {"error": f"Tool execution failed: {e}"}
