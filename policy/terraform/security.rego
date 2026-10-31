# conftest policies on `terraform show -json` plans (LLD, Infrastructure as code:
# "conftest at plan time"). Run by CI on every stack's plan; tested by
# security_test.rego (`conftest verify`). Tags are checked by
# tools/ci/check_terraform_tags.py, role-assignment placement by test_terraform.py.
package main

# Resources this plan creates or changes, with their planned settings.
changed contains rc if {
	some rc in input.resource_changes
	some action in rc.change.actions
	action in {"create", "update"}
}

# Resources this plan deletes, including replacements (delete then create).
deleted contains rc if {
	some rc in input.resource_changes
	"delete" in rc.change.actions
}

# LLD, AI platform: managed identity only, private endpoint only.
deny contains msg if {
	some rc in changed
	rc.type == "azurerm_cognitive_account"
	rc.change.after.local_auth_enabled == true
	msg := sprintf("%s: key authentication must stay off (local_auth_enabled = false)", [rc.address])
}

# LLD, Policy: PostgreSQL and Redis public network access disabled; every PaaS
# service by private endpoint, public access disabled at the resource.
deny contains msg if {
	some rc in changed
	rc.type in {
		"azurerm_cognitive_account",
		"azurerm_postgresql_flexible_server",
		"azurerm_redis_cache",
		"azurerm_key_vault",
	}
	rc.change.after.public_network_access_enabled == true
	msg := sprintf("%s: public network access must be disabled", [rc.address])
}

# LLD, Policy: Key Vault purge protection and soft delete.
deny contains msg if {
	some rc in changed
	rc.type == "azurerm_key_vault"
	rc.change.after.purge_protection_enabled != true
	msg := sprintf("%s: purge protection must be on", [rc.address])
}

# LLD, Policy: storage HTTPS-only with TLS 1.2, no public blob; Entra auth only.
deny contains msg if {
	some rc in changed
	rc.type == "azurerm_storage_account"
	rc.change.after.shared_access_key_enabled == true
	msg := sprintf("%s: shared key access must be disabled (Entra ID only)", [rc.address])
}

deny contains msg if {
	some rc in changed
	rc.type == "azurerm_storage_account"
	rc.change.after.allow_nested_items_to_be_public == true
	msg := sprintf("%s: blobs must never be public", [rc.address])
}

deny contains msg if {
	some rc in changed
	rc.type == "azurerm_storage_account"
	rc.change.after.min_tls_version != "TLS1_2"
	msg := sprintf("%s: minimum TLS must be 1.2", [rc.address])
}

# LLD, Identity: assignments at resource group scope or narrower, never
# subscription-wide. Two read-only roles are the recorded exception (ADR 0023): Cost
# Management's month-to-date total exists only per subscription, and CI must read
# that assignment back to refresh stack 04b. Neither reads a resource or its data.
subscription_roles_allowed := {
	"Cost Management Reader",
	"CloudOps role assignment reader (dev)",
	"CloudOps role assignment reader (uat)",
	"CloudOps role assignment reader (prd)",
}

deny contains msg if {
	some rc in changed
	rc.type == "azurerm_role_assignment"
	regex.match(`^/subscriptions/[^/]+/?$`, rc.change.after.scope)
	not rc.change.after.role_definition_name in subscription_roles_allowed
	msg := sprintf("%s: subscription-wide role assignments are not allowed", [rc.address])
}

# The exception's custom role reads role assignments and definitions, nothing more.
deny contains msg if {
	some rc in changed
	rc.type == "azurerm_role_definition"
	startswith(rc.change.after.name, "CloudOps role assignment reader")
	some p in rc.change.after.permissions
	some field in ["actions", "data_actions"]
	listed := object.get(p, field, [])
	is_array(listed) # the plan says null for an empty list
	some action in listed
	not action in {"Microsoft.Authorization/roleAssignments/read",
		"Microsoft.Authorization/roleDefinitions/read"}
	msg := sprintf("%s: the role assignment reader may only read role assignments", [rc.address])
}

# Data a plan must never destroy unreviewed: a delete or replacement of a stateful
# resource fails CI. Doing it on purpose is a deliberate apply by a human, outside CI.
stateful := {
	"azurerm_postgresql_flexible_server",
	"azurerm_key_vault",
	"azurerm_key_vault_key",
	"azurerm_storage_account",
	"azurerm_cognitive_account",
	"azurerm_redis_cache",
	"azurerm_eventhub_namespace",
}

deny contains msg if {
	some rc in deleted
	rc.type in stateful
	msg := sprintf("%s: plan deletes or replaces a stateful resource", [rc.address])
}
