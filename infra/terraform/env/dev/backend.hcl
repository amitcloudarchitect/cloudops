# State backend for dev. No key or SAS anywhere: use_azuread_auth makes
# Terraform authenticate to the state account with your Entra identity
# (az login). You need Storage Blob Data Contributor on the account.
#
#   terraform init -backend-config=../../env/dev/backend.hcl
#
# The state account is created by hand once (AZURE-ACCESS.md, Terraform state),
# with shared key access disabled.

resource_group_name  = "rg-cloudops-tfstate-dev-weu"
storage_account_name = "stcloudopstfstatedevweu"
container_name       = "tfstate"
use_azuread_auth     = true
