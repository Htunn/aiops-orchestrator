"""
Stdio transport for MCP servers following the MCP specification.

This module handles JSON-RPC 2.0 communication over stdin/stdout.
"""

import json
import sys
from collections.abc import Callable
from typing import Any

import structlog

logger = structlog.get_logger()


class StdioTransport:
    """
    Handles JSON-RPC 2.0 communication over stdin/stdout.

    Follows the MCP specification for stdio-based servers:
    - Reads JSON-RPC requests from stdin (one per line)
    - Writes JSON-RPC responses to stdout (one per line)
    - Logs to stderr for debugging
    """

    def __init__(self) -> None:
        """Initialize stdio transport."""
        # Configure logger to write to stderr only
        self.running = False
        logger.info("stdio_transport_initialized")

    async def start(self, request_handler: Callable[[dict[str, Any]], Any]) -> None:
        """
        Start listening for requests on stdin.

        Args:
            request_handler: Async function that processes requests and returns responses
        """
        self.running = True
        logger.info("stdio_transport_started")

        try:
            # Read from stdin line by line
            for line in sys.stdin:
                if not self.running:
                    break

                line = line.strip()
                if not line:
                    continue

                try:
                    # Parse JSON-RPC request
                    request = json.loads(line)
                    logger.debug(
                        "received_request", method=request.get("method"), id=request.get("id")
                    )

                    # Process request
                    response = await request_handler(request)

                    # Write response to stdout
                    if response:
                        self._write_response(response)

                except json.JSONDecodeError as e:
                    logger.error("invalid_json", error=str(e), line=line[:100])
                    error_response = self._create_error_response(
                        request_id=None, code=-32700, message="Parse error", data=str(e)
                    )
                    self._write_response(error_response)

                except Exception as e:
                    logger.error("request_handler_error", error=str(e), error_type=type(e).__name__)
                    error_response = self._create_error_response(
                        request_id=request.get("id") if "request" in locals() else None,
                        code=-32603,
                        message="Internal error",
                        data=str(e),
                    )
                    self._write_response(error_response)

        except KeyboardInterrupt:
            logger.info("stdio_transport_interrupted")
        finally:
            self.running = False
            logger.info("stdio_transport_stopped")

    def _write_response(self, response: dict[str, Any]) -> None:
        """
        Write a JSON-RPC response to stdout.

        Args:
            response: JSON-RPC response object
        """
        try:
            output = json.dumps(response)
            print(output, flush=True)
            logger.debug("sent_response", id=response.get("id"))
        except Exception as e:
            logger.error("failed_to_write_response", error=str(e))

    def _create_error_response(
        self, request_id: Any | None, code: int, message: str, data: Any | None = None
    ) -> dict[str, Any]:
        """
        Create a JSON-RPC error response.

        Args:
            request_id: The ID from the request
            code: Error code
            message: Error message
            data: Additional error data

        Returns:
            JSON-RPC error response
        """
        response: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }

        if data is not None:
            response["error"]["data"] = data

        return response

    def stop(self) -> None:
        """Stop the transport."""
        self.running = False
