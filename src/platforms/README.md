## Platform Clients

Multi-cloud infrastructure integration clients for AIOps Orchestrator.

### Overview

The `platforms` package provides unified interfaces for managing resources across different cloud and virtualization platforms:

- **Nutanix Prism Central** - VM and cluster management via Prism Central API v3
- **VMware vCenter** - VM and ESXi host management via vCenter REST API 7.0+
- **OpenShift** - Project, Route, Build, and ImageStream management via OpenShift API

### Architecture

All platform clients implement the `BasePlatformClient` abstract interface, providing:

- **Unified VM Management**: List, get, start, stop, restart VMs across platforms
- **Host/Node Monitoring**: List and monitor infrastructure hosts
- **Cluster Operations**: Manage clusters and resource pools
- **Health Checks**: Monitor platform connectivity and health
- **Async/Await**: Full async support with context managers

### Quick Start

#### Using the Platform Factory

```python
from src.platforms import PlatformFactory, PlatformConfig, PlatformType

# Create configuration
config = PlatformConfig(
    platform_type=PlatformType.NUTANIX,
    endpoint="https://prism-central.example.com:9440",
    username="admin",
    password="your-password",
    verify_ssl=True,
)

# Create and initialize client
async with await PlatformFactory.create_and_initialize(config) as client:
    # List VMs
    vms = await client.list_vms()
    for vm in vms:
        print(f"{vm.name}: {vm.power_state}")

    # Start a VM
    await client.start_vm(vm_id="abc-123")

    # Check health
    health = await client.health_check()
    print(f"Status: {health.status}")
```

#### Direct Client Usage

```python
from src.platforms import NutanixClient, PlatformConfig, PlatformType

config = PlatformConfig(
    platform_type=PlatformType.NUTANIX,
    endpoint="https://prism-central.example.com:9440",
    username="admin",
    password="your-password",
)

client = NutanixClient(config)
await client.initialize()

try:
    vms = await client.list_vms(cluster="Production-Cluster")
    # ... work with VMs ...
finally:
    await client.close()
```

### Platform-Specific Examples

#### Nutanix Prism Central

```python
from src.platforms import NutanixClient, PlatformConfig, PlatformType

config = PlatformConfig(
    platform_type=PlatformType.NUTANIX,
    endpoint="https://prism-central.example.com:9440",
    username="admin",
    password="your-password",
    verify_ssl=True,
)

async with NutanixClient(config) as client:
    await client.initialize()

    # List all VMs
    vms = await client.list_vms()

    # Filter VMs by cluster
    prod_vms = await client.list_vms(cluster="Production")

    # Get VM details
    vm = await client.get_vm("vm-uuid-here")
    print(f"VM: {vm.name}, CPUs: {vm.cpu_count}, Memory: {vm.memory_mb}MB")

    # Power operations
    await client.start_vm(vm.id)
    await client.stop_vm(vm.id, force=False)
    await client.restart_vm(vm.id)

    # List hosts
    hosts = await client.list_hosts()
    for host in hosts:
        print(f"Host: {host.name}, Status: {host.status}")

    # List clusters
    clusters = await client.list_clusters()
```

**Nutanix Notes:**
- Uses HTTP Basic Auth or API token
- Power states: `running` (ON), `stopped` (OFF), `suspended` (PAUSED)
- Endpoint should include `:9440` port
- API is asynchronous (operations return task IDs)

#### VMware vCenter

```python
from src.platforms import VMwareClient, PlatformConfig, PlatformType

config = PlatformConfig(
    platform_type=PlatformType.VMWARE,
    endpoint="https://vcenter.example.com",
    username="administrator@vsphere.local",
    password="your-password",
    verify_ssl=True,
)

async with VMwareClient(config) as client:
    await client.initialize()

    # List VMs
    vms = await client.list_vms()

    # Filter by cluster
    cluster_vms = await client.list_vms(cluster="Production-Cluster")

    # Get VM details
    vm = await client.get_vm("vm-123")
    print(f"VM: {vm.name}, Host: {vm.host}")

    # Power operations
    await client.start_vm(vm.id)
    await client.stop_vm(vm.id, force=True)  # force=True for hard shutdown
    await client.restart_vm(vm.id)

    # List ESXi hosts
    hosts = await client.list_hosts()
    for host in hosts:
        print(f"ESXi Host: {host.name}, State: {host.status}")

    # List clusters
    clusters = await client.list_clusters()
    for cluster in clusters:
        print(f"Cluster: {cluster['name']}, DRS: {cluster['drs_enabled']}")
```

**VMware Notes:**
- Uses session-based authentication (creates session on init, deletes on close)
- Requires username/password (typically `administrator@vsphere.local`)
- Power states: `running` (POWERED_ON), `stopped` (POWERED_OFF), `suspended` (SUSPENDED)
- Session tokens expire after inactivity

#### OpenShift

```python
from src.platforms import OpenShiftClient, PlatformConfig, PlatformType

config = PlatformConfig(
    platform_type=PlatformType.OPENSHIFT,
    endpoint="https://api.openshift.example.com:6443",
    token="sha256~your-token-here",
    verify_ssl=True,
)

async with OpenShiftClient(config) as client:
    await client.initialize()

    # List projects
    projects = await client.list_projects()
    for project in projects:
        print(f"Project: {project['name']}, Phase: {project['phase']}")

    # Get project details
    project = await client.get_project("production")

    # List routes (ingress)
    routes = await client.list_routes("production")
    for route in routes:
        print(f"Route: {route['host']} -> {route['service']}")

    # List builds
    builds = await client.list_builds("production")
    for build in builds:
        print(f"Build: {build['name']}, Phase: {build['phase']}")

    # List image streams
    imagestreams = await client.list_imagestreams("production")

    # List buildconfigs
    buildconfigs = await client.list_buildconfigs("production")

    # List nodes (as hosts)
    nodes = await client.list_hosts()
    for node in nodes:
        print(f"Node: {node.name}, Ready: {node.is_healthy()}")
```

**OpenShift Notes:**
- Uses Bearer token authentication (get token via `oc login` and `oc whoami -t`)
- OpenShift is container-based (no traditional VMs)
- `list_vms()` returns empty list (use Kubernetes client for Pods)
- `list_hosts()` returns OpenShift nodes
- Supports OpenShift-specific resources: Projects, Routes, Builds, ImageStreams

### Configuration

#### PlatformConfig Options

```python
PlatformConfig(
    platform_type: PlatformType,    # Required: NUTANIX, VMWARE, or OPENSHIFT
    endpoint: str,                  # Required: Platform API endpoint URL
    username: str | None = None,    # Username for Basic Auth
    password: str | None = None,    # Password for Basic Auth
    token: str | None = None,       # Bearer token (alternative to user/pass)
    verify_ssl: bool = True,        # SSL certificate verification
    timeout: int = 30,              # Request timeout in seconds
    max_retries: int = 3,           # Max retry attempts
    extra: dict | None = None       # Platform-specific extra config
)
```

**Authentication Requirements:**
- **Nutanix**: `username` + `password` OR `token`
- **VMware**: `username` + `password` (required for session creation)
- **OpenShift**: `token` (required)

### Unified Resource Models

#### VMResource

Represents a virtual machine across platforms:

```python
@dataclass
class VMResource:
    id: str                         # Platform-specific VM identifier
    name: str                       # VM name
    power_state: str                # running, stopped, suspended, unknown
    cpu_count: int | None           # Number of CPUs
    memory_mb: int | None           # Memory in MB
    host: str | None                # Host/node where VM runs
    cluster: str | None             # Cluster name
    ip_addresses: list[str] | None  # IP addresses
    platform: str                   # Platform identifier
    metadata: dict | None           # Platform-specific metadata
    
    def is_running(self) -> bool:   # Check if VM is running
```

#### HostResource

Represents a hypervisor host/node:

```python
@dataclass
class HostResource:
    id: str                         # Platform-specific host identifier
    name: str                       # Host name
    status: str                     # ready, not_ready, maintenance, unknown
    cpu_capacity: int | None        # Total CPU capacity
    memory_capacity_mb: int | None  # Total memory in MB
    cpu_usage_percent: float | None # CPU usage percentage
    memory_usage_percent: float | None  # Memory usage percentage
    vm_count: int | None            # Number of VMs on host
    cluster: str | None             # Cluster name
    platform: str                   # Platform identifier
    metadata: dict | None           # Platform-specific metadata
    
    def is_healthy(self) -> bool:   # Check if host is healthy
```

#### PlatformHealth

Health check result:

```python
@dataclass
class PlatformHealth:
    platform: str  # Platform identifier
    status: str  # healthy, degraded, unreachable
    message: str | None  # Status message
    response_time_ms: float | None  # Response time in milliseconds
    last_check: str | None  # Last check timestamp
```

### Error Handling

All clients raise standard Python exceptions:

```python
from src.platforms import NutanixClient, PlatformConfig, PlatformType

config = PlatformConfig(...)
client = NutanixClient(config)

try:
    await client.initialize()
    vm = await client.get_vm("invalid-id")
except ConnectionError as e:
    print(f"Connection failed: {e}")
except ValueError as e:
    print(f"VM not found or invalid request: {e}")
except Exception as e:
    print(f"Unexpected error: {e}")
finally:
    await client.close()
```

**Common Exceptions:**
- `ConnectionError`: Failed to connect or initialize client
- `ValueError`: Resource not found or invalid parameters (404, 400)
- `httpx.HTTPStatusError`: HTTP errors (401, 403, 500, etc.)
- `RuntimeError`: Client not initialized (forgot to call `initialize()`)

### Testing Against Mock Servers

The platform clients can be tested against mock servers without real infrastructure:

```python
# Start mock servers
./scripts/mock_servers.sh start

# Use mock endpoints in tests
config = PlatformConfig(
    platform_type=PlatformType.NUTANIX,
    endpoint="http://localhost:5001",  # Nutanix mock
    username="admin",
    password="password",
    verify_ssl=False  # Mock servers don't use SSL
)

# Test client operations
async with NutanixClient(config) as client:
    await client.initialize()
    vms = await client.list_vms()  # Returns mock data
```

**Mock Server Ports:**
- Nutanix: `http://localhost:5001`
- VMware: `http://localhost:5002`
- OpenShift: `http://localhost:5003`
- Kubernetes: `http://localhost:5004`

See [dev/README.md](../../dev/README.md) for mock server setup.

### Advanced Usage

#### Custom Platform Registration

```python
from src.platforms import PlatformFactory, BasePlatformClient, PlatformType

class CustomPlatformClient(BasePlatformClient):
    async def initialize(self):
        # Custom initialization
        pass
    
    # Implement all abstract methods...

# Register custom platform
PlatformFactory.register_platform(
    PlatformType.CUSTOM,
    CustomPlatformClient
)

# Use factory to create custom client
config = PlatformConfig(platform_type=PlatformType.CUSTOM, ...)
client = PlatformFactory.create_client(config)
```

#### Health Monitoring

```python
from src.platforms import PlatformFactory, PlatformConfig

configs = [
    PlatformConfig(PlatformType.NUTANIX, ...),
    PlatformConfig(PlatformType.VMWARE, ...),
    PlatformConfig(PlatformType.OPENSHIFT, ...),
]

for config in configs:
    async with await PlatformFactory.create_and_initialize(config) as client:
        health = await client.health_check()
        print(f"{health.platform}: {health.status} ({health.response_time_ms:.1f}ms)")
```

#### Parallel Operations

```python
import asyncio
from src.platforms import PlatformFactory, PlatformConfig


async def check_all_platforms(configs):
    tasks = []
    for config in configs:

        async def check_platform(cfg):
            async with await PlatformFactory.create_and_initialize(cfg) as client:
                return await client.health_check()

        tasks.append(check_platform(config))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    return results


# Check all platforms in parallel
configs = [...]
health_results = await check_all_platforms(configs)
```

### Best Practices

1. **Always Use Context Managers**: Ensures proper cleanup
   ```python
   async with client:
       await client.initialize()
       # ... operations ...
   # Automatically closes connection
   ```

2. **Handle Connection Failures**: Platforms may be unreachable
   ```python
   try:
       async with await PlatformFactory.create_and_initialize(config) as client:
           vms = await client.list_vms()
   except ConnectionError:
       # Handle platform unavailable
       pass
   ```

3. **Check Resource Existence**: VMs/Hosts may be deleted
   ```python
   try:
       vm = await client.get_vm(vm_id)
   except ValueError:
       # VM not found
       pass
   ```

4. **Use Health Checks**: Monitor platform availability
   ```python
   health = await client.health_check()
   if health.status == "healthy":
       # Safe to perform operations
       vms = await client.list_vms()
   ```

5. **Test with Mocks First**: Validate logic without real infrastructure
   ```python
   # Development/testing
   config = PlatformConfig(
       platform_type=PlatformType.NUTANIX,
       endpoint="http://localhost:5001",  # Mock server
       verify_ssl=False,
   )

   # Production
   config = PlatformConfig(
       platform_type=PlatformType.NUTANIX,
       endpoint="https://real-prism.company.com:9440",
       verify_ssl=True,
   )
   ```

### Logging

All clients use `structlog` for structured logging:

```python
import structlog

# Configure logging
structlog.configure(
    processors=[structlog.processors.TimeStamper(fmt="iso"), structlog.processors.JSONRenderer()]
)

# Client operations automatically log
async with client:
    await client.initialize()
    # Logs: nutanix_client_initialized, endpoint=...

    vms = await client.list_vms()
    # Logs: listed_vms, count=10
```

**Log Events:**
- `{platform}_client_initialized` - Client initialized successfully
- `{platform}_client_closed` - Client connection closed
- `listed_vms` - VMs listed (count, cluster)
- `vm_started` - VM powered on (vm_id, vm_name)
- `vm_stopped` - VM powered off (vm_id, vm_name, force)
- `vm_restarted` - VM restarted (vm_id, vm_name)
- `list_vms_failed` - Failed to list VMs (error, status_code)
- And more...

### API Reference

See individual client docstrings for detailed API documentation:

- [BasePlatformClient](base_client.py) - Abstract interface
- [NutanixClient](nutanix_client.py) - Nutanix implementation
- [VMwareClient](vmware_client.py) - VMware implementation
- [OpenShiftClient](openshift_client.py) - OpenShift implementation
- [PlatformFactory](platform_factory.py) - Factory pattern

### Contributing

When adding a new platform client:

1. Inherit from `BasePlatformClient`
2. Implement all abstract methods
3. Add to `PlatformFactory._registry`
4. Add unit tests in `tests/unit/test_platform_clients.py`
5. Update this README with platform-specific examples
