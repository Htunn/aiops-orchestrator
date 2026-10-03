# Azure Credential Setup for AIOps Orchestrator

Note: `AZURE_INTEGRATION_ENABLED` now actually gates `AzureResourceClient` (wired
2026-10-03) — previously this setting existed but was unused; the client only
checked whether `config/azure_resources.yml` had subscriptions.

## 1. Choose an auth mode

- **Service Principal** (recommended for local dev / this orchestrator running
  outside Azure): create an Entra ID app registration with a client secret.
- **Managed Identity** (use this instead if/when the orchestrator runs inside
  Azure — AKS, a VM, or Container Apps): no secret needed, skip to step 4.

## 2. Create a least-privilege Service Principal (Azure CLI)

### Option A: automated script (recommended)

`az login` first, then edit `RESOURCE_GROUPS=(...)` at the top of
[`scripts/setup_azure_credentials.sh`](../scripts/setup_azure_credentials.sh)
to list your real resource group(s), and run:

```bash
./scripts/setup_azure_credentials.sh
```

This creates (or reuses) the app registration, assigns least-privilege RBAC
roles scoped to those resource groups, and safely **upserts** the `AZURE_*`
keys into `.env` and regenerates `config/azure_resources.yml` — it never
clobbers unrelated `.env` values and backs up both files before writing
(`*.bak.<timestamp>`). Safe to re-run; it reuses the existing app registration
instead of creating duplicates.

### Option B: manual steps

Run these yourself in a terminal — **do not paste secrets into chat**.

```bash
az login
az account show --query id -o tsv   # <- your AZURE_SUBSCRIPTION_ID
az account show --query tenantId -o tsv  # <- your AZURE_TENANT_ID

# Pick the resource group(s) you want AIOps to see/act on
az group list --query "[].name" -o tsv

SUB_ID="<subscription-id-from-above>"
RG1="prod-rg"
RG2="prod-aks-rg"

# Read-only identity for list/health/metrics tools (azure_list_*, azure_resource_health, etc.)
az ad sp create-for-rbac \
  --name "aiops-orchestrator-azure" \
  --role "Reader" \
  --scopes "/subscriptions/$SUB_ID/resourceGroups/$RG1" "/subscriptions/$SUB_ID/resourceGroups/$RG2"
```

This prints JSON with `appId` (→ `AZURE_CLIENT_ID`), `password` (→
`AZURE_CLIENT_SECRET`), and `tenant` (→ `AZURE_TENANT_ID`). Copy these directly
into your `.env` file — never share them with me.

> If you used **Option A** (the script), steps 3-5 below are already done —
> skip to step 6 to verify.

## 3. Grant mutating permissions only if you want restart/scale/deallocate tools to work

Still scoped to the same resource groups, never at subscription level:

```bash
APP_ID="<appId-from-step-2>"

az role assignment create --assignee "$APP_ID" --role "Virtual Machine Contributor" \
  --scope "/subscriptions/$SUB_ID/resourceGroups/$RG1"

az role assignment create --assignee "$APP_ID" --role "Website Contributor" \
  --scope "/subscriptions/$SUB_ID/resourceGroups/$RG1"

az role assignment create --assignee "$APP_ID" --role "Azure Kubernetes Service Contributor Role" \
  --scope "/subscriptions/$SUB_ID/resourceGroups/$RG2"
```

Per SPEC-002's Security Requirements, use a **separate app registration per
environment** (prod vs non-prod) if you'll manage more than one.

## 4. Create `.env` and fill in the values

No `.env` exists yet in this repo — create it first:

```bash
cp .env.example .env
```

Then edit `.env` (yours, not shared) and set:

```ini
AZURE_INTEGRATION_ENABLED=true
AZURE_USE_MANAGED_IDENTITY=false   # true instead if using Managed Identity — then skip client_id/secret
AZURE_SUBSCRIPTION_ID=<from step 2>
AZURE_TENANT_ID=<from step 2>
AZURE_CLIENT_ID=<appId from step 2>
AZURE_CLIENT_SECRET=<password from step 2>
```

If using **Managed Identity** instead, set `AZURE_USE_MANAGED_IDENTITY=true` and
leave `AZURE_CLIENT_ID`/`AZURE_CLIENT_SECRET` empty — the orchestrator's Azure
Identity (assigned to the AKS pod / VM / Container App) still needs the same
RBAC role assignments from steps 2-3.

## 5. Update `config/azure_resources.yml` to match your real resource groups

Edit the `subscriptions` entry (currently a placeholder with `prod-rg`/`prod-aks-rg`):

```yaml
azure:
  subscriptions:
    - subscription_id: ${AZURE_SUBSCRIPTION_ID}
      display_name: production
      resource_group_scope:
        - prod-rg        # <- replace with your actual resource group name(s)
        - prod-aks-rg
```

Any resource group not listed here is invisible to AIOps even if the identity's
RBAC role would technically allow access (enforced in `src/azure/client.py`).

## 6. Verify the wiring without real credentials (recommended first)

`tests/unit/test_azure_config.py` validates config loading, credential selection,
and the full `_initialize()` flow entirely offline — `ClientSecretCredential`/
`DefaultAzureCredential` are lazy and never contact Entra ID until an actual ARM
call is made, so these tests prove your config shape and env var wiring work
without touching Azure or needing real secrets:

```bash
.venv/bin/python -m pytest tests/unit/test_azure_config.py -v --no-cov
```

If you want to sanity-check your *actual* `.env`/`config/azure_resources.yml`
values (not just the wiring logic) without a live ARM call either, run:

```bash
.venv/bin/python -c "
import asyncio
from src.azure.client import AzureResourceClient

async def main():
    client = AzureResourceClient()
    await client._initialize()
    print('is_available:', client.is_available)
    print('subscriptions:', list(client._subscriptions.keys()))
    await client.close()

asyncio.run(main())
"
```

This only constructs the credential object and registers subscriptions from your
real config — it does not call Azure, so a typo'd secret won't be caught here.

## 7. Verify against live Azure (only once you're ready to test real credentials)

```bash
.venv/bin/python -c "
import asyncio
from src.azure.client import AzureResourceClient

async def main():
    client = await AzureResourceClient.get_instance()
    print('is_available:', client.is_available)
    if client.is_available:
        print(await client.list_resource_groups())

asyncio.run(main())
"
```

Expect `is_available: True` and a list of your scoped resource groups. If
`False`, check the printed/log warning (`azure_client_init_failed` or
`azure_client_integration_disabled`/`azure_client_no_subscriptions_configured`)
for the exact reason — most commonly a wrong tenant/client/secret or an RBAC
role that hasn't propagated yet (can take a minute or two).

## Security reminders
- Never paste `AZURE_CLIENT_SECRET` (or any secret) into chat — type it directly
  into `.env`.
- `.env` is already gitignored — confirm before committing anything.
- Rotate the client secret at most every 180 days (Entra ID enforced max);
  prefer Managed Identity wherever the orchestrator runs inside Azure to avoid
  secrets entirely.
