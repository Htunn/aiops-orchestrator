#!/usr/bin/env python3
"""
Launcher script for the Platform MCP Server.

This script starts the MCP server which communicates via stdio using JSON-RPC 2.0.
It can be used as the command in MCP configuration files.
"""

import asyncio
import logging
import os
import sys

# Ensure all logs go to stderr, not stdout
logging.basicConfig(stream=sys.stderr, level=logging.INFO)

# Add the project root to the Python path
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from src.mcp.platform_server import main

if __name__ == "__main__":
    asyncio.run(main())
