#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# setup_azure_credentials.sh  –  Create a least-privilege Entra ID service
#                                 principal for AIOps Orchestrator's Azure ARM
#                                 integration (SPEC-002) and wire it into
#                                 .env + config/azure_resources.yml.
#
# Usage: ./scripts/setup_azure_credentials.sh
#
# Safe to re-run: reuses an existing app registration with the same display
# name instead of creating duplicates, and never overwrites unrelated .env
# values — only the AZURE_* keys are added/updated.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT"

# --- CONFIGURATION ---
RESOURCE_GROUPS=("htunn-ai")
SP_NAME="aiops-orchestrator-azure"

echo -e "${GREEN}🔐  AIOps Orchestrator – Azure Credential Setup${NC}"
echo "======================================================"

# ── 0. Preflight checks ─────────────────────────────────────────────────────
if ! command -v az &>/dev/null; then
  echo -e "${RED}❌  Azure CLI ('az') not found. Install it first: https://aka.ms/azure-cli${NC}"
  exit 1
fi

if ! az account show &>/dev/null; then
  echo -e "${RED}❌  Not logged in. Run 'az login' first.${NC}"
  exit 1
fi

# ── 1. Resolve Azure IDs ─────────────────────────────────────────────────────
echo "==> Fetching Azure account information..."
SUB_ID=$(az account show --query id -o tsv)
TENANT_ID=$(az account show --query tenantId -o tsv)

if [[ -z "$SUB_ID" || -z "$TENANT_ID" ]]; then
  echo -e "${RED}❌  Unable to fetch Subscription or Tenant ID.${NC}"
  exit 1
fi

echo "Subscription ID : $SUB_ID"
echo "Tenant ID       : $TENANT_ID"

# ── 2. Create (or reuse) Entra ID App Registration & Service Principal ──────
echo "==> Looking for an existing App Registration named '$SP_NAME'..."
APP_ID=$(az ad app list --display-name "$SP_NAME" --query "[0].appId" -o tsv)

if [[ -z "$APP_ID" || "$APP_ID" == "null" ]]; then
  echo "==> None found — creating a new App Registration..."
  APP_ID=$(az ad app create --display-name "$SP_NAME" --query appId -o tsv)
else
  echo -e "${YELLOW}==> Reusing existing App Registration (appId: $APP_ID)${NC}"
fi

echo "==> Generating a new client secret (old secrets, if any, stay valid until they expire)..."
CLIENT_SECRET=$(az ad app credential reset --id "$APP_ID" --append --query password -o tsv)

echo "==> Ensuring a Service Principal exists for this app..."
az ad sp show --id "$APP_ID" &>/dev/null || az ad sp create --id "$APP_ID" -o none

echo "==> Waiting 10 seconds for Entra ID replication..."
sleep 10

# ── 3. Assign RBAC roles, scoped to each resource group (never subscription-wide) ──
echo "==> Assigning Azure RBAC roles on scoped Resource Groups..."
echo -e "${YELLOW}    Note: Reader is sufficient for list/health/metrics tools only.${NC}"
echo -e "${YELLOW}    The Contributor roles below are only needed for restart/scale/deallocate${NC}"
echo -e "${YELLOW}    actions — all still gated by AIOps's human-approval flow regardless of RBAC.${NC}"
for rg in "${RESOURCE_GROUPS[@]}"; do
  SCOPE="/subscriptions/$SUB_ID/resourceGroups/$rg"

  echo "  - Assigning 'Reader' on $rg..."
  az role assignment create --assignee "$APP_ID" --role "Reader" --scope "$SCOPE" -o none

  echo "  - Assigning 'Virtual Machine Contributor' on $rg..."
  az role assignment create --assignee "$APP_ID" --role "Virtual Machine Contributor" --scope "$SCOPE" -o none

  echo "  - Assigning 'Website Contributor' on $rg..."
  az role assignment create --assignee "$APP_ID" --role "Website Contributor" --scope "$SCOPE" -o none

  echo "  - Assigning 'Azure Kubernetes Service Contributor Role' on $rg..."
  az role assignment create --assignee "$APP_ID" --role "Azure Kubernetes Service Contributor Role" --scope "$SCOPE" -o none
done

# ── 4. Upsert AZURE_* keys into .env (never clobber unrelated settings) ─────
echo "==> Writing credentials to .env..."
if [[ ! -f .env ]]; then
  echo -e "${YELLOW}⚠️   .env not found — creating it from .env.example${NC}"
  cp .env.example .env
fi
cp .env ".env.bak.$(date +%Y%m%d%H%M%S)"

_upsert_env() {
  local key="$1" value="$2"
  if grep -q "^${key}=" .env; then
    # Escape & and | so sed's replacement text can't misinterpret the secret
    local escaped
    escaped=$(printf '%s' "$value" | sed -e 's/[&|\]/\\&/g')
    sed -i.tmp "s|^${key}=.*|${key}=${escaped}|" .env && rm -f .env.tmp
  else
    printf '%s=%s\n' "$key" "$value" >> .env
  fi
}

_upsert_env "AZURE_INTEGRATION_ENABLED" "true"
_upsert_env "AZURE_USE_MANAGED_IDENTITY" "false"
_upsert_env "AZURE_SUBSCRIPTION_ID" "$SUB_ID"
_upsert_env "AZURE_TENANT_ID" "$TENANT_ID"
_upsert_env "AZURE_CLIENT_ID" "$APP_ID"
_upsert_env "AZURE_CLIENT_SECRET" "$CLIENT_SECRET"
chmod 600 .env

echo -e "${GREEN}.env updated (backup saved alongside it).${NC}"

# ── 5. Update config/azure_resources.yml (backed up, full schema preserved) ─
echo "==> Updating config/azure_resources.yml..."
mkdir -p config
if [[ -f config/azure_resources.yml ]]; then
  cp config/azure_resources.yml "config/azure_resources.yml.bak.$(date +%Y%m%d%H%M%S)"
fi

RG_YAML=""
for rg in "${RESOURCE_GROUPS[@]}"; do
  RG_YAML="${RG_YAML}        - ${rg}
"
done
RG_YAML="${RG_YAML%$'\n'}"  # drop the trailing newline from the last entry

cat > config/azure_resources.yml <<EOF
# Azure Resource Management Configuration (SPEC-002)
# Regenerated by scripts/setup_azure_credentials.sh on $(date +%Y-%m-%d)
azure:
  subscriptions:
    - subscription_id: \${AZURE_SUBSCRIPTION_ID}
      display_name: production
      resource_group_scope:
${RG_YAML}

  monitoring:
    activity_log_lookback_hours: 24
    metrics_poll_interval_seconds: 300

  auth:
    use_managed_identity: \${AZURE_USE_MANAGED_IDENTITY:-false}
EOF

echo -e "${GREEN}config/azure_resources.yml updated (backup saved alongside it).${NC}"

echo ""
echo -e "${GREEN}✅  Setup complete.${NC}"
echo "Verify without touching Azure again:"
echo "  .venv/bin/python -m pytest tests/unit/test_azure_config.py -v --no-cov"
echo "Verify against live Azure (read-only):"
echo "  .venv/bin/python -c \"import asyncio; from src.azure.client import AzureResourceClient; asyncio.run((lambda: AzureResourceClient.get_instance())())\""
echo ""
echo -e "${YELLOW}⚠️  .env now contains a live client secret. It is gitignored, but treat it${NC}"
echo -e "${YELLOW}   as sensitive — rotate via 'az ad app credential reset' if ever exposed.${NC}"
