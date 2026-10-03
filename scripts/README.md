# Server Scripts Usage Guide

## Quick Start Scripts

This directory contains convenient scripts for starting and stopping the AI Agent server.

## start_server.sh

Start the uvicorn server with automatic virtual environment activation and health checks.

### Basic Usage

```bash
# Start with default settings (0.0.0.0:8000 with reload)
./scripts/start_server.sh

# Start with custom host
./scripts/start_server.sh 127.0.0.1

# Start with custom host and port
./scripts/start_server.sh 127.0.0.1 3000

# Start without auto-reload (production-like)
./scripts/start_server.sh 0.0.0.0 8000 ""
```

### What It Does

1. ✅ Checks if virtual environment exists
2. ✅ Activates the virtual environment automatically
3. ✅ Checks if .env file exists (warns if missing)
4. ✅ Detects if port is already in use (offers to kill existing process)
5. ✅ Starts uvicorn with specified settings
6. ✅ Database is initialized automatically by the application
7. ✅ Handles graceful shutdown on Ctrl+C

### Features

- **Color-coded output** for easy reading
- **Port conflict detection** - automatically detects if port 8000 is busy
- **Interactive prompts** - asks before killing processes or continuing without .env
- **Auto-reload** - enabled by default for development

## stop_server.sh

Stop the running uvicorn server.

### Basic Usage

```bash
# Stop the server
./scripts/stop_server.sh
```

### What It Does

1. ✅ Finds all uvicorn processes running src.main:app
2. ✅ Gracefully terminates them (SIGTERM)
3. ✅ Force kills if they don't stop (SIGKILL)
4. ✅ Frees port 8000 if still occupied
5. ✅ Confirms when server is stopped

## test_mcp.py

Test the MCP integration.

### Basic Usage

```bash
# Run MCP integration tests
python scripts/test_mcp.py
```

### What It Tests

1. ✅ MCP client initialization
2. ✅ MCP server startup
3. ✅ Tool listing (should find 13 K8s tools)
4. ✅ Tool execution (tests k8s_current_context)
5. ✅ Cleanup and shutdown

Expected output:
```
✅ All tests passed!
```

## mcp_server.py

Launch the MCP server standalone (usually called by MCPClient automatically).

### Basic Usage

```bash
# Start MCP server (waits for JSON-RPC requests on stdin)
python scripts/mcp_server.py

# Test with echo
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"1.0.0"}}}' | python scripts/mcp_server.py
```

## init_db.py

Initialize the database and run migrations **manually**.

> **Note**: The application automatically initializes the database on startup. You only need to run this script manually if you want to run migrations before starting the server or for testing purposes.

### Basic Usage

```bash
# Run database migrations manually
python scripts/init_db.py
```

### When to Use

- **Testing migrations** before deploying
- **Pre-running migrations** in CI/CD pipelines
- **Seeding initial data** without starting the server
- **Database setup** in isolated environments

### What It Does

1. Runs Alembic migrations (upgrade to head)
2. Seeds initial channel configurations
3. Creates default data if needed

> ⚠️ **Warning**: Do not run this while the application is running, as it may cause database lock issues.

## azure_mcp_server.py

Launch the Azure ARM MCP server standalone (usually called by `MCPManager` automatically; see `.mcp-config.json`).

### Basic Usage

```bash
# Start Azure MCP server (waits for JSON-RPC requests on stdin)
python scripts/azure_mcp_server.py
```

### What It Tests

Exposes 15 Azure Resource Manager tools (list/inspect resources, restart/scale, deallocate/delete) over the same stdio JSON-RPC protocol as `mcp_server.py`. Degrades gracefully — returns an error string from each tool call — when `config/azure_resources.yml` has no subscriptions or Entra ID credentials are not set.

## setup_azure_credentials.sh

Create (or reuse) a least-privilege Entra ID service principal for the Azure integration, assign scoped RBAC roles, and wire `.env` + `config/azure_resources.yml` automatically.

### Basic Usage

```bash
az login
# Edit RESOURCE_GROUPS=(...) at the top of the script first
./scripts/setup_azure_credentials.sh
```

### What It Does

1. Resolves your subscription/tenant ID via `az account show`
2. Creates (or reuses, by display name) an App Registration + Service Principal
3. Assigns `Reader` + `Virtual Machine Contributor` + `Website Contributor` + `Azure Kubernetes Service Contributor Role`, scoped to each listed resource group (never subscription-wide)
4. **Upserts** `AZURE_*` keys into `.env` — never touches unrelated settings, backs up the file first
5. Regenerates `config/azure_resources.yml` with your resource groups — backs up the file first

> Safe to re-run. See [docs/azure-integration.md](../docs/azure-integration.md) for the full guide.

## Examples

### Development Workflow

```bash
# 1. Start dependencies
docker-compose up -d postgres redis

# 2. Start server
./scripts/start_server.sh

# Server is now running with auto-reload at http://0.0.0.0:8000

# 3. Make code changes... server auto-reloads

# 4. Stop server when done
./scripts/stop_server.sh
```

### Production-like Testing

```bash
# Start without auto-reload
./scripts/start_server.sh 0.0.0.0 8000 ""

# Server runs in production mode
```

### Port Conflict Resolution

If you get "Port already in use":

```bash
# Option 1: Let start_server.sh handle it
./scripts/start_server.sh
# Press 'y' when prompted to kill existing process

# Option 2: Manually stop first
./scripts/stop_server.sh
./scripts/start_server.sh
```

## Troubleshooting

### Script not executable

```bash
chmod +x scripts/start_server.sh
chmod +x scripts/stop_server.sh
```

### Virtual environment not found

```bash
python3 -m venv .venv
pip install -r requirements.txt
```

### Database connection error

```bash
# Make sure PostgreSQL is running
docker-compose up -d postgres

# Check connection
psql postgresql://aiagent:aiagent_password@localhost:5432/aiagent
```

### Port still busy after stop_server.sh

```bash
# Manually kill process on port 8000
lsof -ti:8000 | xargs kill -9
```

## Environment Variables

The scripts respect these environment variables from `.env`:

- `DATABASE_URL` - PostgreSQL connection string
- `REDIS_URL` - Redis connection string
- `GITHUB_TOKEN` - GitHub API token
- `TELEGRAM_TOKEN` - Telegram bot token (optional)
- `SLACK_BOT_TOKEN` - Slack bot token (optional)

## Notes

- Scripts are designed for **development use** on macOS/Linux
- For Windows, use WSL or manually run the commands
- For production, use `docker-compose` or proper process managers like systemd/supervisor
- Auto-reload watches for file changes and restarts the server automatically
