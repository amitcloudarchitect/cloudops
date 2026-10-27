# LLD naming: {type}-cloudops-{workload}-{env}-{region}, lowercase, hyphenated;
# tokens concatenated where a resource type forbids hyphens. One place, so a
# name can never drift between the stack that creates a resource and the stack
# that looks it up.

locals {
  e = var.env
  r = var.region_short

  resource_groups = {
    for p in ["net", "sec", "obs", "data", "msg", "aks", "ai", "exec"] :
    p => "rg-cloudops-${p}-${local.e}-${local.r}"
  }

  subnets = {
    aks_system = "snet-aks-system-${local.e}"
    aks_app    = "snet-aks-app-${local.e}"
    data       = "snet-data-${local.e}"
    msg        = "snet-msg-${local.e}"
    sec        = "snet-sec-${local.e}"
    ai         = "snet-ai-${local.e}"
    telemetry  = "snet-telemetry-${local.e}"
    egress     = "snet-egress-${local.e}"
  }

  # LLD, App registrations and identities. The execution identity is part of
  # the execution plane and is deliberately not created before its review.
  workload_identities = ["api", "ingest", "telemetry", "ai", "workflow"]
}
