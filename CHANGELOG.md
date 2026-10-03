# Changelog

All notable changes to AIOps Orchestrator are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).  
This project uses [Semantic Versioning](https://semver.org/).

---

## [2.5.0] — 2026-10-03

### 🚀 Minor Release: Azure Resource Management via ARM API

AIOps Orchestrator can now discover and remediate Azure resources (VMs, VM Scale Sets, App Services, AKS clusters) from chat, following the same client/handler/approval pattern already used for Kubernetes and the VM platforms. Entra ID–authenticated, RBAC-scoped, and approval-gated for every mutating action.

### Added

#### ☁️ Azure Resource Manager (ARM) Integration
- **`AzureResourceClient`** (`src/azure/client.py`) — async singleton wrapping `azure-identity` + `azure-mgmt-*`, with lazy init, graceful degrade, and background retry (mirrors `KubernetesClient`)
- **15 MCP tools** (`src/mcp/azure_server.py`) exposed over stdio JSON-RPC, same convention as the Kubernetes MCP server:
  - LOW risk (auto-execute): `azure_list_resource_groups`, `azure_list_vms`, `azure_get_vm`, `azure_list_vmss`, `azure_list_app_services`, `azure_list_aks_clusters`, `azure_resource_health`, `azure_activity_log`, `azure_resource_metrics`
  - MEDIUM risk (approval required): `azure_restart_vm`, `azure_scale_vmss`, `azure_scale_app_service_plan`, `azure_restart_aks_nodepool`
  - HIGH risk (approval required, explicit warning): `azure_deallocate_vm`, `azure_delete_resource`
- **`AzureHandler`** (`src/services/azure_handler.py`) — natural-language command parsing wired into chat (`"list vms in resource group prod-rg"`, `"restart vm web-01 in resource group prod-rg"`, etc.), fail-closed when the approval system is unavailable
- **Resource-group scoping** — `config/azure_resources.yml` restricts discovery/actions to explicitly listed resource groups, enforced in code regardless of the identity's broader RBAC grants
- **Entra ID auth** — Managed Identity (when running inside Azure) or Service Principal via `AZURE_TENANT_ID`/`AZURE_CLIENT_ID`/`AZURE_CLIENT_SECRET`, selectable via `AZURE_USE_MANAGED_IDENTITY`
- **`scripts/setup_azure_credentials.sh`** — idempotent setup script: creates/reuses the Entra ID app registration, assigns least-privilege RBAC roles scoped to your resource groups, and safely upserts `.env`/`config/azure_resources.yml` without clobbering unrelated settings (backs up both files first)
- **`docs/azure-integration.md`** — full setup guide (automated script or manual `az` CLI steps), offline verification, and live verification instructions
- **60 new unit tests** (`tests/unit/test_azure_{client,server,handler,config}.py`) — scope enforcement, name validation, risk classification, approval gating, error mapping (403/404/429), and fully offline config/credential-selection validation (no live Azure needed)

### Fixed
- **`main.py`**: `ApprovalManager` was constructed with a reference to `mcp_manager`, which was never instantiated anywhere in the file — a silently-caught `NameError` meant **no approval-gated action of any kind** (Kubernetes included) could actually execute once approved. Now `MCPManager()` is instantiated and started before `ApprovalManager`.
- **`config.py`**: `.env` values were invisible to the `${VAR_NAME}` substitution used by all three YAML config loaders (`agents.yml`, `api_backends.yml`, `azure_resources.yml`) because `pydantic-settings` parses `.env` without exporting it into `os.environ`. Added a single `load_dotenv()` call to fix this for all three loaders.
- **`src/azure/client.py`**: `AZURE_INTEGRATION_ENABLED` previously had no effect — now gates `AzureResourceClient` initialization as documented.

### Security
- Every Azure mutating action routes through the existing `ApprovalManager` human-in-the-loop gate — RBAC permissions bound *what's possible*, approval bounds *what's allowed without a human*.
- Least-privilege, resource-group-scoped RBAC roles (never subscription-wide); separate Entra ID identity recommended per environment (prod/non-prod).
- Resource group/VM names validated against Azure's naming pattern before ARM SDK interpolation.

---

## [2.2.0] — 2026-08-01

### 🚀 Minor Release: Custom Fine-tuned Model Support & Ollama Thinking Model Streaming

This release integrates support for custom HuggingFace GGUF models hosted via Ollama, adds full routing support for `hf.co/` model refs, fixes streaming for thinking models (reasoning-first architectures like Gemma 4), and ships a live end-to-end test suite for Ollama integration.

### Added

#### 🧠 Custom Fine-tuned AIOps Model
- **`hf.co/htunn/gemma-4-e2b-aiops-gguf:Q4_K_M`** — Gemma 4 E2B (2B params) LoRA fine-tuned for AIOps scenarios
  - Trained on K8s, Nutanix, VMware, Active Directory, ADFS, and PKI incident playbooks
  - Q4_K_M quantisation (3.2 GB) for on-device inference on Apple Silicon and consumer GPUs
  - Outputs structured JSON action commands for autonomous remediation
  - Available via `ollama run hf.co/htunn/gemma-4-e2b-aiops-gguf:Q4_K_M`
  - Mirrored as `aiops-orchestrator:latest` via custom `Modelfile`
- **`Modelfile`** — Ollama agent configuration at repo root
  - Sets AIOps system prompt, temperature 0.3, top-p 0.9, repeat_penalty 1.1
  - `ollama create aiops-orchestrator -f Modelfile` to build locally

#### 🦙 Ollama HuggingFace Model Ref Support
- **`hf.co/` prefix routing** — HuggingFace-format Ollama model refs are now correctly dispatched to `OllamaClient`
  - Previously misrouted to `VLLMClient` (vLLM also uses `/` in model paths)
  - `AIRouter._is_vllm_model()` now explicitly excludes `hf.co/` prefix
  - `AIRouter._is_ollama_model()` now recognises `hf.co/` prefix as an Ollama ref
- **`gemma` pattern** added to Ollama model detection (`gemma4:e2b`, `gemma:7b`, etc.)
- `OllamaClient.is_model_supported()` and `list_supported_models()` updated accordingly

#### 🌊 Thinking Model Streaming
- **`delta.reasoning` fallback** in `OllamaClient.stream_response()`
  - Gemma 4 and other reasoning-first models emit tokens via `delta.reasoning`, not `delta.content`, during the thinking phase
  - Streaming now yields `delta.reasoning` when `delta.content` is empty
  - Empty-choices keep-alive chunks are safely skipped

#### 🐳 docker-compose Ollama Integration
- `OLLAMA_BASE_URL` and `DEFAULT_MODEL` environment variables added to the `app` service
  - Default: `OLLAMA_BASE_URL=http://host.docker.internal:11434/v1`
  - Default: `DEFAULT_MODEL=aiops-orchestrator`
  - Container reaches host Ollama via `extra_hosts: host.docker.internal:host-gateway`

#### 🧪 Live End-to-End Test Suite
- **`tests/e2e/test_ollama_aiops_live.py`** — tests against the real docker-compose stack and local Ollama
  - `TestOllamaAIOpsModelLive` — direct OllamaClient inference with the fine-tuned model
    - `test_generate_response_aiops_model` — K8s + AD failure prompt → JSON action
    - `test_generate_response_pki_scenario` — PKI cert expiry prompt
    - `test_stream_response_aiops_model` — streaming with reasoning-phase token capture
    - `test_hf_model_ref_routing` — verifies `hf.co/` routes to OllamaClient
  - `TestDockerStackHealth` — stack health and metrics endpoints
    - `test_health_endpoint_ok` — all services healthy
    - `test_metrics_endpoint_ok` — Prometheus metrics present
  - Auto-skip when Ollama is unreachable
- **6 new routing unit tests** in `tests/test_vllm_ollama_e2e.py::TestHuggingFaceOllamaRouting`

### Fixed

- **`src/main.py` — `UnboundLocalError` on startup** — Stray `, agent_registry` expression after the `lifespan()` docstring caused Python to treat `agent_registry` as a local variable, crashing every app startup. Removed the stray expression and added `agent_registry` to the `global` declaration.
- **`OllamaClient.stream_response()` — 0 chunks for thinking models** — Changed content check from `if chunk.choices[0].delta.content:` to also capture `delta.reasoning`, fixing streams that produce no `content` tokens during the thinking phase.
- **JSON markdown fence stripping** — `str.strip("\`\`\`json")` strips individual characters; replaced with `re.sub()` for correct fence removal from LLM responses.

---

## [2.1.0] — 2026-07-28

### 🚀 Minor Release: Multi-Backend LLM Support (vLLM & Ollama)

This release expands LLM backend support from 2 to **4 backends**, adding **vLLM** (self-hosted high-performance inference) and **Ollama** (local LLM runner) alongside existing GitHub Models and Gemini support. This provides flexibility for production (cloud), self-hosted (vLLM), and local development (Ollama) deployments.

### Added

#### 🤖 vLLM Integration
- **VLLMClient** - OpenAI-compatible client for vLLM servers
  - Async `generate_response()` and `stream_response()` methods
  - Retry logic with exponential backoff (3 attempts, 4-10s delay)
  - Model detection for Llama, Mistral, Qwen, Phi, DeepSeek, Vicuna, Yi, Mixtral, CodeLlama
  - Structured logging with request/response context
  - Custom base URL support for self-hosted deployments
- **Supported Models**
  - Meta Llama: `meta-llama/Llama-2-7b-chat-hf`, `meta-llama/Meta-Llama-3-8B-Instruct`, `meta-llama/Meta-Llama-3-70B-Instruct`
  - Mistral: `mistralai/Mistral-7B-Instruct-v0.2`, `mistralai/Mixtral-8x7B-Instruct-v0.1`
  - Qwen: `Qwen/Qwen-7B-Chat`, `Qwen/Qwen-14B-Chat`
  - DeepSeek: `deepseek-ai/deepseek-coder-6.7b-instruct`
  - And many more HuggingFace models
- **Configuration**
  - `VLLM_BASE_URL` - vLLM server URL (default: `http://localhost:8000/v1`)
  - `VLLM_API_KEY` - Optional API key (depends on server configuration)

#### 🦙 Ollama Integration
- **OllamaClient** - OpenAI-compatible client for Ollama servers
  - Async `generate_response()` and `stream_response()` methods
  - Retry logic with exponential backoff (3 attempts, 4-10s delay)
  - Model detection for llama2, llama3, mistral, mixtral, codellama, phi, qwen, etc.
  - Fallback token counting (word split) when usage stats not provided
  - Structured logging with request/response context
- **Supported Models**
  - Llama: `llama2`, `llama2:7b`, `llama2:13b`, `llama2:70b`, `llama3:8b`, `llama3:70b`
  - Mistral: `mistral`, `mistral:7b`, `mixtral:8x7b`
  - Code: `codellama`, `codellama:7b`, `codellama:13b`, `deepseek-coder:6.7b`
  - Other: `phi`, `phi:2.7b`, `neural-chat`, `vicuna`, `qwen:7b`, `solar:10.7b`, `yi:6b`
- **Configuration**
  - `OLLAMA_BASE_URL` - Ollama server URL (default: `http://localhost:11434/v1`)

#### 🎯 Enhanced AI Routing
- **AIRouter Updates** - Intelligent 4-backend routing
  - **Routing Priority**:
    1. `gemini-*` → GeminiClient
    2. `*/` or `vllm:*` → VLLMClient (HuggingFace paths or explicit prefix)
    3. Ollama patterns or `ollama:*` → OllamaClient (simple names or explicit prefix)
    4. Everything else → GitHubModelsClient (default)
  - **Model Prefix Stripping** - `_strip_provider_prefix()` method
    - Strips `vllm:` prefix before passing to backend
    - Strips `ollama:` prefix before passing to backend
    - Preserves original model names for other backends
  - **Lazy Initialization** - Backends only initialized if configured
    - vLLM: enabled when `VLLM_BASE_URL` is set
    - Ollama: enabled when `OLLAMA_BASE_URL` is set
    - Gemini: enabled when `GEMINI_API_KEY` is set
    - GitHub Models: always enabled (default)
  - **Backend Status Logging** - Structured logs on router initialization
    - `ai_router_vllm_enabled` - vLLM base URL logged
    - `ai_router_ollama_enabled` - Ollama base URL logged
    - `ai_router_initialized` - List of active backends

#### 📚 Comprehensive Testing Infrastructure
- **Unit Tests** - `tests/unit/test_vllm_ollama_clients.py` (211 lines)
  - 16 test cases covering both VLLMClient and OllamaClient
  - Tests: initialization, settings, API calls, streaming, model detection, error handling
  - Fully mocked - no external dependencies required
- **E2E Tests** - `tests/test_vllm_ollama_e2e.py` (450 lines)
  - Router routing logic validation
  - Model detection pattern tests
  - Prefix stripping tests
  - Backend dispatch verification
- **Mock Servers** - `tests/mock_llm_servers.py` (270 lines)
  - FastAPI-based mock vLLM server (port 8000)
  - FastAPI-based mock Ollama server (port 11434)
  - OpenAI-compatible API endpoints
  - Streaming and non-streaming support
  - Health check endpoints
- **Integration Tests** - `tests/test_vllm_ollama_e2e_real.py` (250 lines)
  - Tests against real mock servers
  - HTTP communication validation
  - Router→client→server→response flow testing
- **Validation Script** - `tests/validate_implementation.sh` (150 lines)
  - 36 automated validation checks
  - File existence, syntax, content, configuration, documentation
  - Comprehensive pass/fail reporting

#### 📖 Extensive Documentation
- **Integration Guide** - `docs/vllm-ollama-integration.md` (531 lines)
  - Architecture overview and routing rules
  - Setup instructions for vLLM and Ollama servers
  - Usage examples (direct client + router)
  - Model support reference
  - Error handling and troubleshooting
  - Performance comparison across backends
  - Production deployment (Docker, Kubernetes)
  - Migration guide from existing backends
- **Testing Guide** - `docs/vllm-ollama-testing-guide.md` (600+ lines)
  - Unit test instructions
  - Integration test setup
  - Manual testing with curl
  - Real server setup guide
  - Troubleshooting common issues
- **Implementation Summary** - `docs/vllm-ollama-implementation-summary.md` (355 lines)
  - Complete change log
  - Files created/modified
  - Configuration reference
  - Architecture diagrams
  - Testing summary
- **Environment Config** - `docs/vllm-ollama-env-config.md` (90 lines)
  - Environment variable examples
  - Configuration scenarios (dev, staging, production)
  - Backend selection verification

### Changed
- **AIRouter** - Complete rewrite (211 lines, previously 170 lines)
  - Added `_vllm` and `_ollama` client attributes
  - Added `_is_vllm_model()` and `_is_ollama_model()` detection methods
  - Added `_strip_provider_prefix()` for model name normalization
  - Updated `_backend_for()` to route to all 4 backends
  - Updated `generate_response()` and `stream_response()` to strip prefixes
  - Updated `is_model_supported()` to check all backends
  - Updated `list_supported_models()` to include vLLM/Ollama models with prefixes
- **Configuration** - `src/config.py`
  - Added `vllm_base_url: str | None` field
  - Added `vllm_api_key: str | None` field
  - Added `ollama_base_url: str | None` field
- **README.md** - Updated LLM backends section
  - Changed "dual-backend" to "multi-backend" (4 backends)
  - Added vLLM section (Section 3) with model examples
  - Added Ollama section (Section 4) with model examples
  - Updated routing rules documentation
  - Added model selection examples for all 4 backends

### Performance
- **vLLM Latency**: 50-500ms (vs 1-3s for cloud providers)
  - Ideal for high-throughput, low-latency scenarios
  - Self-hosted infrastructure with full control
  - No per-token costs (infrastructure cost only)
- **Ollama Latency**: 100-1000ms (local hardware dependent)
  - Perfect for development and offline work
  - No network latency or API costs
  - Privacy-focused (all data stays local)

### Benefits
- **Flexibility** - Choose backend per use case:
  - Production: GitHub Models or Gemini (cloud, variety)
  - Self-hosted: vLLM (performance, control, cost)
  - Development: Ollama (local, offline, free)
- **Cost Optimization**
  - vLLM: No per-token cost (infrastructure only)
  - Ollama: Completely free (local hardware)
- **Privacy** - vLLM and Ollama run entirely on-premises
- **Development Workflow** - Use Ollama during development, switch to cloud for production

### Migration Notes

#### Configuration (Optional)
Both vLLM and Ollama are **optional** and disabled by default. To enable:

**vLLM**:
```bash
export VLLM_BASE_URL=http://localhost:8000/v1  # Or your vLLM server URL
export VLLM_API_KEY=your-api-key  # Optional, depends on server config
```

**Ollama**:
```bash
export OLLAMA_BASE_URL=http://localhost:11434/v1  # Or your Ollama server URL
```

#### Backend Status Verification
Check logs during startup to verify backend initialization:
```
ai_router_gemini_enabled
ai_router_vllm_enabled base_url=http://localhost:8000/v1
ai_router_ollama_enabled base_url=http://localhost:11434/v1
ai_router_initialized backends=['github_models', 'gemini', 'vllm', 'ollama']
```

#### Model Selection
Use model names to route to specific backends:

```bash
# GitHub Models (default)
/model gpt-4
/model claude-3-opus

# Gemini
/model gemini-2.0-flash

# vLLM (HuggingFace path or explicit prefix)
/model meta-llama/Llama-2-7b-chat-hf
/model vllm:mistral

# Ollama (simple name or explicit prefix)
/model llama2
/model ollama:codellama
```

### Backward Compatibility
✅ **100% Backward Compatible** - No breaking changes
- Existing GitHub Models and Gemini usage unchanged
- vLLM and Ollama are opt-in via environment variables
- Routing logic preserves existing behavior for configured backends
- No database migrations required

### Documentation Updates
- Updated `README.md` with 4-backend architecture
- Added 4 comprehensive guides (2,500+ lines total)
- Updated architecture documentation
- Created production readiness report

---

## [2.0.0] — 2026-07-26

### 🚀 Major Release: Multi-Agent Orchestration & API Backend Monitoring

This is a **major release** introducing Agent-to-Agent (A2A) integration and external API backend monitoring capabilities, transforming the AIOps Orchestrator into a complete multi-agent orchestration platform.

### Added

#### 🤖 Agent-to-Agent (A2A) Integration
- **Agent Registry** with PostgreSQL persistence and Redis caching (5min TTL)
  - Dynamic agent registration via REST API
  - Auto-registration from `config/agents.yml`
  - Agent health tracking and status monitoring
- **Capability Discovery & Matching**
  - Intelligent capability scoring (0.0-1.0 scale)
  - JSON Schema parameter validation
  - Tag-based fallback matching
- **Task Delegation Framework**
  - Synchronous mode: wait for task completion (default 60s timeout)
  - Asynchronous mode: webhook callbacks for long-running tasks
  - Automatic agent selection by capability score
- **Natural Language Delegation**
  - `@agent-name <task description>` syntax in chat
  - AI-powered capability and parameter extraction (Gemini 2.0 Flash)
  - Structured syntax: `@capability: param1=val1, param2=val2`
- **A2A Chat Commands**
  - `/a2a agents [capability]` - List registered agents
  - `/a2a agent <id>` - Show detailed agent info
  - `/a2a status` - System status and metrics
  - `/a2a help` - Command reference
- **JWT Authentication**
  - HS256 token signing with 1-hour expiry
  - API key generation (SHA-256 hashed storage)
  - Capability-based access control (CBAC)
- **A2A REST API** (7 endpoints)
  - `POST /api/a2a/register` - Agent registration (returns API key once)
  - `GET /api/a2a/agents` - List agents with filters
  - `GET /api/a2a/agents/{agent_id}` - Agent details
  - `DELETE /api/a2a/agents/{agent_id}` - Deregister (self-only)
  - `POST /api/a2a/delegate` - Receive task delegations
  - `GET /api/a2a/tasks/{task_id}` - Task status
  - `POST /api/a2a/webhook` - Async completion callbacks
- **Database Schema**
  - `agents` table with JSONB capabilities, indexed status
  - `agent_tasks` table for delegation audit trail
  - `agent_messages` table for message history
- **Prometheus Metrics** (9 A2A metrics)
  - `aiagent_a2a_agents_registered` (Gauge)
  - `aiagent_a2a_agents_online` (Gauge)
  - `aiagent_a2a_tasks_delegated_total` (Counter with labels)
  - `aiagent_a2a_tasks_received_total` (Counter)
  - `aiagent_a2a_task_duration_seconds` (Histogram)
  - `aiagent_a2a_webhooks_sent_total` (Counter)
  - `aiagent_a2a_webhooks_received_total` (Counter)
  - `aiagent_a2a_auth_failures_total` (Counter with reason)
  - `aiagent_a2a_capability_requests_total` (Counter)
- **Health Endpoint**: `GET /health/a2a` for system status

#### 🌐 API Backend Monitoring
- **API Backend Watch-Loop**
  - Background polling for external service health
  - Configurable check intervals (default: 60s)
  - Timeout and retry handling
- **Monitoring Capabilities**
  - Downtime detection (HTTP errors, timeouts, connection failures)
  - High latency alerts (P95 response time tracking)
  - Elevated error rate detection (4xx/5xx responses)
  - SSL certificate expiration warnings
- **Configuration** (`config/api_backends.yml`)
  - Per-endpoint thresholds (latency, error rate, SSL days)
  - Environment variable substitution: `${VAR:-default}`
  - Custom timeout and check intervals
- **Alert Rules** (4 new conditions)
  - `API_BACKEND_DOWN` - Service unavailable
  - `API_HIGH_LATENCY` - P95 latency exceeds threshold
  - `API_HIGH_ERROR_RATE` - Error percentage too high
  - `API_SSL_EXPIRING` - Certificate expiring soon
- **Remediation Playbooks** (3 new playbooks)
  - `api_backend_down_remediation` - Diagnostic playbook (DNS, curl, SSL, traceroute)
  - `api_latency_investigation` - Latency analysis with timing breakdown
  - `api_error_rate_analysis` - Error pattern investigation
- **MCP Diagnostic Tools** (4 tools)
  - `api_dns_lookup` - DNS resolution testing
  - `api_curl_test` - HTTP connectivity and timing
  - `api_ssl_check` - SSL certificate validation
  - `api_traceroute` - Network path analysis
- **Prometheus Metrics** (4 API metrics)
  - `aiagent_api_backend_up` (Gauge) - 1=up, 0=down
  - `aiagent_api_backend_latency_seconds` (Histogram)
  - `aiagent_api_backend_errors_total` (Counter with error_type)
  - `aiagent_api_backend_checks_total` (Counter with status)
- **Health Endpoints**
  - Extended `/health` with `api_backends` section
  - New `/health/api-backends` for detailed status

### Changed
- **Message Handler** - Extended with A2A delegation support
  - `@agent` syntax detection and routing
  - `/a2a` command handler (130+ lines)
  - Natural language extraction via AI
- **Main Application** - A2A initialization in lifespan
  - Agent registry setup
  - Task delegator exposure to message handler
  - Auto-registration from config
- **Development Status** - Upgraded from "Beta" to "Production/Stable"

### Documentation
- **New Documentation** (~52KB total)
  - `docs/a2a-integration.md` (20KB) - Complete A2A guide
  - `docs/a2a-sequence-diagrams.md` (19KB) - 8 Mermaid sequence diagrams
  - `docs/api-backend-monitoring.md` (13KB) - API monitoring guide
- **Updated Documentation**
  - `docs/architecture.md` - Added A2A architecture section
  - `README.md` - Added A2A features and updated doc index

### Dependencies
- Added `PyJWT==2.8.0` for JWT authentication

### Migration Notes

#### Database Migration Required
Run Alembic migration `003_a2a_tables` to create A2A schema:

```bash
docker exec -it aiops-orchestrator alembic upgrade head
```

This creates:
- `agents` table
- `agent_tasks` table  
- `agent_messages` table

#### Configuration

**Enable A2A** (optional, disabled by default):

```bash
export A2A_ENABLED=true
export A2A_AGENT_ID="aiops-orchestrator"
export A2A_AGENT_NAME="AIOps Orchestrator"
export A2A_JWT_SECRET="your-secret-key-here"  # CHANGE IN PRODUCTION
export A2A_WEBHOOK_URL="https://your-domain.com/api/a2a/webhook"
```

**Enable API Backend Monitoring**:

```bash
export API_BACKEND_MONITORING_ENABLED=true
# Edit config/api_backends.yml to add endpoints
```

### Security Considerations

⚠️ **IMPORTANT**: Change default JWT secret in production:

```bash
export A2A_JWT_SECRET="$(openssl rand -hex 32)"
```

### Breaking Changes

**None** - All new features are opt-in and backward compatible.

---

## [Unreleased]

### Added
- Future features will be listed here

---

## [0.4.0] — 2026-03-02

### Added
- **AIOps Engine** — full proactive monitoring subsystem
  - `K8sWatchLoop` background task polling cluster every 30s (configurable)
  - Detects: `CrashLoopBackOff`, `OOMKilled`, `NotReady` nodes, zero-replica deployments
  - `RuleEngine` for severity-mapped alert rule matching
  - `PlaybookExecutor` for ordered remediation step sequences
  - `RCAEngine` — LLM-powered root-cause analysis with structured JSON output (SRE prompt)
  - `LogAnalyzer` — pattern recognition on pod/container logs
- **Approval Manager** — human-in-the-loop gate for MEDIUM/HIGH risk playbook steps
  - Redis-backed pending approvals with configurable TTL (default 5 min)
  - Chat-native approval: `approve <id>` / `reject <id>`
  - Risk levels: `LOW` (auto), `MEDIUM` (approve), `HIGH` (warn + approve)
- **Alertmanager webhook receiver** — `POST /api/alert/webhook` ingests Prometheus alerts
- **Enhanced `/health` endpoint** — now reports K8s, Prometheus, watchloop status, pending approvals, active incidents
- Grafana monitoring integration helper (`src/monitoring/grafana.py`)
- Prometheus metrics integration (`src/monitoring/prometheus.py`)
- `config/` directory with Prometheus, Alertmanager, and Grafana provisioning configs

### Changed
- Application name: **AIOps Orchestrator**
- Database defaults updated to use `aiagent` database name
- `lifespan()` now initialises ApprovalManager and PlaybookExecutor at startup

---

## [0.3.0] — 2026-02-28

### Added
- **Multi-transport MCP (Model Context Protocol)** architecture
  - `MCPManager` with lifecycle management for multiple servers
  - `StdioTransport` — subprocess-based local servers (Kubernetes)
  - `SSETransport` — HTTP/SSE for cloud services (SimplePortChecker)
  - `.mcp-config.json` for declarative server configuration
- **Security scanning** via SimplePortChecker MCP SSE server (8 tools)
  - `scan_ports`, `analyze_certificate`, `detect_l7_protection`
  - `check_mtls`, `check_security_headers`, `scan_owasp_vulnerabilities`
  - `full_security_scan`, `check_hybrid_identity`
- **Slack bot** adapter (`slack_bolt`) with Events API, app-mention, IM history
- `MCP_CONFIG_PATH` environment variable support
- Kubernetes kubectl bundled in Docker image (v1.28)
- Multi-stage Docker build with OCI labels and non-root user

### Changed
- MCP server initialisation migrated from single server to `MCPManager` pattern
- Docker Compose: kubeconfig now mounted from `./data/kube/config`

---

## [0.2.0] — 2026-02-20

### Added
- **Kubernetes integration** with 13 MCP tools
  - `k8s_get_pods`, `k8s_get_nodes`, `k8s_get_deployments`, `k8s_get_services`
  - `k8s_scale_deployment`, `k8s_describe_resource`, `k8s_get_logs`
  - `k8s_get_events`, `k8s_top_pods`, `k8s_top_nodes`
  - `k8s_get_namespaces`, `k8s_get_contexts`, `k8s_current_context`
- **Natural language Kubernetes queries** — intent detection with status filters
  - Filters: `error/failed/crash`, `unhealthy/not ready`, `pending`, `running`
- `KubernetesHandler` service for NL-to-kubectl translation
- `/k8s` command routing in `MessageHandler`
- Redis-backed session manager with TTL expiry
- Alembic migration support

### Changed
- `MessageHandler` refactored to support K8s intent routing alongside AI responses

---

## [0.1.0] — 2026-02-10

### Added
- Initial release
- **FastAPI** application with async lifespan management
- **Telegram** adapter (python-telegram-bot, webhook + long-poll)
- Channel router for fan-out/fan-in message routing
- **GitHub Models API** client (openai-compatible SDK)
  - Models: GPT-4o, Claude-3 Opus, Llama-3-70B
- Per-user and per-channel model selection (`/model` command)
- Conversation history stored in **PostgreSQL 16**
- **Redis 7** session caching with TTL
- SQLAlchemy async models: `User`, `Conversation`, `Message`, `ChannelConfig`
- Docker Compose stack: app + PostgreSQL + Redis
- Rate limiting via `slowapi`
- Structured JSON logging via `structlog`
- `/health`, `/ready` endpoints
- Pydantic Settings for all configuration
- `.env.example` template

---

[Unreleased]: https://github.com/YOUR_USERNAME/aiops-orchestrator/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/YOUR_USERNAME/aiops-orchestrator/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/YOUR_USERNAME/aiops-orchestrator/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/YOUR_USERNAME/aiops-orchestrator/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/YOUR_USERNAME/aiops-orchestrator/releases/tag/v0.1.0
