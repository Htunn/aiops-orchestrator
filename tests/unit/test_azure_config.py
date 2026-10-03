"""
Unit tests validating Azure config loading + credential selection WITHOUT real
Azure credentials or network calls (SPEC-002).

Credential constructors (ClientSecretCredential/DefaultAzureCredential) are lazy —
they don't contact Entra ID until a token is actually requested — so these tests
exercise the full config → credential → subscription-registration flow offline.
Use this instead of the live `AzureResourceClient.get_instance()` smoke test in
docs/azure-integration.md when you just want to confirm your .env/config shape
is wired correctly.
"""

from pathlib import Path

import pytest
from azure.identity.aio import ClientSecretCredential, DefaultAzureCredential

from src.azure.client import AzureResourceClient
from src.config import get_settings, load_azure_resources_config


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    """Settings is @lru_cache'd — clear it so monkeypatched env vars take effect."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _write_config(tmp_path: Path, content: str) -> str:
    config_file = tmp_path / "azure_resources.yml"
    config_file.write_text(content)
    return str(config_file)


class TestLoadAzureResourcesConfig:
    def test_missing_file_returns_empty_dict(self, tmp_path):
        result = load_azure_resources_config(str(tmp_path / "does_not_exist.yml"))
        assert result == {}

    def test_malformed_yaml_returns_empty_dict(self, tmp_path):
        path = _write_config(tmp_path, "azure: [this is not: valid: yaml")
        assert load_azure_resources_config(path) == {}

    def test_env_var_substitution_in_subscription_id(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TEST_AZURE_SUB_ID", "11111111-2222-3333-4444-555555555555")
        path = _write_config(
            tmp_path,
            """
            azure:
              subscriptions:
                - subscription_id: ${TEST_AZURE_SUB_ID}
                  display_name: test
                  resource_group_scope: [prod-rg]
            """,
        )
        data = load_azure_resources_config(path)
        assert (
            data["azure"]["subscriptions"][0]["subscription_id"]
            == "11111111-2222-3333-4444-555555555555"
        )

    def test_env_var_default_used_when_unset(self, tmp_path, monkeypatch):
        monkeypatch.delenv("TEST_AZURE_SUB_ID_UNSET", raising=False)
        path = _write_config(
            tmp_path,
            """
            azure:
              subscriptions:
                - subscription_id: ${TEST_AZURE_SUB_ID_UNSET:-}
                  display_name: test
            """,
        )
        data = load_azure_resources_config(path)
        assert data["azure"]["subscriptions"][0]["subscription_id"] == ""

    def test_use_managed_identity_resolved_to_bool(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TEST_USE_MI", "true")
        path = _write_config(
            tmp_path,
            """
            azure:
              subscriptions: []
              auth:
                use_managed_identity: ${TEST_USE_MI:-false}
            """,
        )
        data = load_azure_resources_config(path)
        assert data["azure"]["auth"]["use_managed_identity"] is True


class TestBuildCredential:
    def test_service_principal_env_vars_select_client_secret_credential(self, monkeypatch):
        monkeypatch.setenv("AZURE_TENANT_ID", "tenant-123")
        monkeypatch.setenv("AZURE_CLIENT_ID", "client-456")
        monkeypatch.setenv("AZURE_CLIENT_SECRET", "fake-secret-not-real")
        get_settings.cache_clear()

        credential = AzureResourceClient._build_credential({"azure": {"auth": {}}})
        assert isinstance(credential, ClientSecretCredential)

    def test_managed_identity_flag_selects_default_azure_credential(self, monkeypatch):
        monkeypatch.delenv("AZURE_CLIENT_SECRET", raising=False)
        get_settings.cache_clear()

        credential = AzureResourceClient._build_credential(
            {"azure": {"auth": {"use_managed_identity": True}}}
        )
        assert isinstance(credential, DefaultAzureCredential)

    def test_missing_service_principal_vars_falls_back_to_default_credential(self, monkeypatch):
        # Patch the Settings instance directly rather than deleting env vars —
        # a real local .env (pydantic-settings' env_file) would otherwise still
        # supply these values even after monkeypatch.delenv().
        get_settings.cache_clear()
        settings = get_settings()
        monkeypatch.setattr(settings, "azure_tenant_id", None)
        monkeypatch.setattr(settings, "azure_client_id", None)
        monkeypatch.setattr(settings, "azure_client_secret", None)

        credential = AzureResourceClient._build_credential({"azure": {"auth": {}}})
        assert isinstance(credential, DefaultAzureCredential)


class TestInitializeEndToEnd:
    """Exercises the real _initialize() flow offline — no ARM calls are made."""

    @pytest.mark.asyncio
    async def test_disabled_integration_stays_unavailable(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AZURE_INTEGRATION_ENABLED", "false")
        get_settings.cache_clear()
        path = _write_config(
            tmp_path,
            """
            azure:
              subscriptions:
                - subscription_id: sub-1
                  resource_group_scope: [prod-rg]
            """,
        )
        monkeypatch.setenv("AZURE_RESOURCES_CONFIG_PATH", path)
        get_settings.cache_clear()

        client = AzureResourceClient()
        await client._initialize()

        assert client.is_available is False

    @pytest.mark.asyncio
    async def test_enabled_with_no_subscriptions_stays_unavailable(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AZURE_INTEGRATION_ENABLED", "true")
        path = _write_config(tmp_path, "azure:\n  subscriptions: []\n")
        monkeypatch.setenv("AZURE_RESOURCES_CONFIG_PATH", path)
        get_settings.cache_clear()

        client = AzureResourceClient()
        await client._initialize()

        assert client.is_available is False

    @pytest.mark.asyncio
    async def test_enabled_with_subscription_and_fake_sp_creds_becomes_available(
        self, tmp_path, monkeypatch
    ):
        """
        Mirrors what happens once you fill in .env per docs/azure-integration.md —
        fake values prove the wiring works; no real Entra ID call happens here
        because ClientSecretCredential is lazy (only authenticates on first token
        request, which only happens on an actual ARM SDK call).
        """
        monkeypatch.setenv("AZURE_INTEGRATION_ENABLED", "true")
        monkeypatch.setenv("AZURE_TENANT_ID", "fake-tenant")
        monkeypatch.setenv("AZURE_CLIENT_ID", "fake-client")
        monkeypatch.setenv("AZURE_CLIENT_SECRET", "fake-secret")
        path = _write_config(
            tmp_path,
            """
            azure:
              subscriptions:
                - subscription_id: sub-1
                  display_name: production
                  resource_group_scope: [prod-rg, prod-aks-rg]
              auth:
                use_managed_identity: false
            """,
        )
        monkeypatch.setenv("AZURE_RESOURCES_CONFIG_PATH", path)
        get_settings.cache_clear()

        client = AzureResourceClient()
        await client._initialize()

        assert client.is_available is True
        sub = client._subscriptions["sub-1"]
        assert sub.display_name == "production"
        assert sub.resource_group_scope == {"prod-rg", "prod-aks-rg"}
        assert isinstance(sub.credential, ClientSecretCredential)

        await client.close()
