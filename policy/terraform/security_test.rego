# Unit tests for security.rego: `conftest verify -p policy/terraform`.
package main

plan(type, actions, after) := {"resource_changes": [{
	"address": sprintf("%s.this", [type]),
	"type": type,
	"change": {"actions": actions, "after": after},
}]}

test_keyless_private_openai_passes if {
	count(deny) == 0 with input as plan("azurerm_cognitive_account", ["create"],
		{"local_auth_enabled": false, "public_network_access_enabled": false})
}

test_openai_with_keys_is_denied if {
	count(deny) == 1 with input as plan("azurerm_cognitive_account", ["update"],
		{"local_auth_enabled": true, "public_network_access_enabled": false})
}

test_public_postgres_is_denied if {
	count(deny) == 1 with input as plan("azurerm_postgresql_flexible_server", ["create"],
		{"public_network_access_enabled": true})
}

test_vault_without_purge_protection_is_denied if {
	count(deny) == 1 with input as plan("azurerm_key_vault", ["create"],
		{"purge_protection_enabled": false, "public_network_access_enabled": false})
}

test_storage_with_shared_keys_is_denied if {
	count(deny) == 1 with input as plan("azurerm_storage_account", ["create"], {
		"shared_access_key_enabled": true,
		"allow_nested_items_to_be_public": false,
		"min_tls_version": "TLS1_2",
	})
}

test_locked_down_storage_passes if {
	count(deny) == 0 with input as plan("azurerm_storage_account", ["update"], {
		"shared_access_key_enabled": false,
		"allow_nested_items_to_be_public": false,
		"min_tls_version": "TLS1_2",
	})
}

test_subscription_wide_role_is_denied if {
	count(deny) == 1 with input as plan("azurerm_role_assignment", ["create"],
		{"scope": "/subscriptions/00000000-0000-0000-0000-000000000000"})
}

test_resource_group_role_passes if {
	count(deny) == 0 with input as plan("azurerm_role_assignment", ["create"],
		{"scope": "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-x"})
}

test_deleting_a_database_is_denied if {
	count(deny) == 1 with input as plan("azurerm_postgresql_flexible_server", ["delete"], null)
}

test_replacing_a_vault_is_denied if {
	count(deny) >= 1 with input as plan("azurerm_key_vault", ["delete", "create"],
		{"purge_protection_enabled": true, "public_network_access_enabled": false})
}

test_deleting_a_public_ip_passes if {
	count(deny) == 0 with input as plan("azurerm_public_ip", ["delete"], null)
}
