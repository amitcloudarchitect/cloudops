# Build every image with one command:
#
#   docker buildx bake -f docker-bake.hcl --load   # local images cloudops/<name>:dev
#   make images                        # build, then SBOM and scan each (the gate)
#
# Pushing to ACR is a separate, deliberate command (docs/containers.md).
#
# Not here yet: the telemetry processor (waits on LLD open item L1) and the
# execution worker (after the security review). Each gets a target when its code
# exists.

variable "REGISTRY" {
  default = "cloudops"
}

variable "TAG" {
  default = "dev"
}

variable "REVISION" {
  default = ""
}

group "default" {
  targets = ["api", "connector-servicenow", "worker-ai", "migrate", "web"]
}

target "_python" {
  context    = "."
  dockerfile = "infra/docker/python.Dockerfile"
  platforms  = ["linux/amd64"]
  labels = {
    "org.opencontainers.image.source"   = "https://github.com/amitcloudarchitect/cloudops"
    "org.opencontainers.image.revision" = REVISION
    "org.opencontainers.image.vendor"   = "Getronics"
  }
}

target "api" {
  inherits = ["_python"]
  target   = "api"
  args     = { PACKAGE = "cloudops-api" }
  tags     = ["${REGISTRY}/api:${TAG}"]
}

target "connector-servicenow" {
  inherits = ["_python"]
  target   = "connector-servicenow"
  args     = { PACKAGE = "cloudops-connectors" }
  tags     = ["${REGISTRY}/connector-servicenow:${TAG}"]
}

target "worker-ai" {
  inherits = ["_python"]
  target   = "worker-ai"
  args     = { PACKAGE = "cloudops-agents" }
  tags     = ["${REGISTRY}/worker-ai:${TAG}"]
}

target "migrate" {
  inherits = ["_python"]
  target   = "migrate"
  args     = { PACKAGE = "all" }
  tags     = ["${REGISTRY}/migrate:${TAG}"]
}

# The operations console (ADR 0017): its own Dockerfile, Node rather than Python.
# Built for Entra sign-in only; the environment's identifiers come from the pod.
target "web" {
  context    = "."
  dockerfile = "infra/docker/web.Dockerfile"
  target     = "web"
  platforms  = ["linux/amd64"]
  args       = { NEXT_PUBLIC_AUTH_MODE = "entra" }
  tags       = ["${REGISTRY}/web:${TAG}"]
  labels = {
    "org.opencontainers.image.source"   = "https://github.com/amitcloudarchitect/cloudops"
    "org.opencontainers.image.revision" = REVISION
    "org.opencontainers.image.vendor"   = "Getronics"
  }
}
