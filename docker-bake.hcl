// Build with: docker buildx bake
// Override a variable with either form:
//   docker buildx bake --set cuda-devbox.args.JAVA_TAG=25-jdk-resolute
//   JAVA_TAG=25-jdk-resolute docker buildx bake

variable "IMAGE_NAME" {
  default = "cuda-devbox"
}

// Date tag: the build's year and month in Beijing time (UTC+8), e.g. 202609.
variable "IMAGE_VERSION" {
  default = 202609
}

// Pinned base images, repository and tag as separate variables. Mirrors the
// Dockerfile ARG defaults, so `docker buildx bake` with no overrides matches
// a plain `docker build .`. The *_REPO variables let you point at a
// different registry/mirror without touching the tag, and vice versa.
variable "UV_REPO" {
  default = "astral/uv"
}

variable "UV_TAG" {
  default = "0.12.0"
}

variable "GO_REPO" {
  default = "golang"
}

variable "GO_TAG" {
  default = "1-trixie"
}

variable "JAVA_REPO" {
  default = "eclipse-temurin"
}

variable "JAVA_TAG" {
  default = "25-jdk-resolute"
}

variable "MAVEN_REPO" {
  default = "maven"
}

variable "MAVEN_TAG" {
  default = "3-eclipse-temurin-25-noble"
}

variable "GRADLE_REPO" {
  default = "gradle"
}

variable "GRADLE_TAG" {
  default = "9-jdk25-resolute"
}

variable "NODE_REPO" {
  default = "node"
}

variable "NODE_TAG" {
  default = "lts-trixie"
}

variable "DOCKER_REPO" {
  default = "docker"
}

variable "DOCKER_TAG" {
  default = "29"
}

variable "RUST_REPO" {
  default = "rust"
}

// Only /usr/local/cargo and /usr/local/rustup are copied, so the slim variant
// gives the same toolchain as the full one for a smaller pull.
variable "RUST_TAG" {
  default = "1-slim-trixie"
}

variable "BASE_REPO" {
  default = "nvidia/cuda"
}

variable "BASE_TAG" {
  default = "13.3.1-cudnn-devel-ubuntu26.04"
}

variable "KOTLIN_VERSION" {
  default = "2.3.21"
}

variable "PYTHON_VERSION" {
  default = "3.14"
}

variable "CODEX_VERSION" {
  default = "latest"
}

variable "CLAUDE_CODE_VERSION" {
  default = "latest"
}

group "default" {
  targets = ["cuda-devbox"]
}

target "cuda-devbox" {
  context    = "."
  dockerfile = "Dockerfile"
  tags       = ["${IMAGE_NAME}:${IMAGE_VERSION}", "${IMAGE_NAME}:latest"]
  labels     = { "org.opencontainers.image.version" = IMAGE_VERSION }
  args = {
    UV_REPO             = UV_REPO
    UV_TAG               = UV_TAG
    GO_REPO              = GO_REPO
    GO_TAG                = GO_TAG
    JAVA_REPO            = JAVA_REPO
    JAVA_TAG              = JAVA_TAG
    MAVEN_REPO           = MAVEN_REPO
    MAVEN_TAG             = MAVEN_TAG
    GRADLE_REPO          = GRADLE_REPO
    GRADLE_TAG            = GRADLE_TAG
    NODE_REPO            = NODE_REPO
    NODE_TAG              = NODE_TAG
    DOCKER_REPO          = DOCKER_REPO
    DOCKER_TAG            = DOCKER_TAG
    RUST_REPO            = RUST_REPO
    RUST_TAG              = RUST_TAG
    BASE_REPO            = BASE_REPO
    BASE_TAG              = BASE_TAG
    KOTLIN_VERSION       = KOTLIN_VERSION
    PYTHON_VERSION       = PYTHON_VERSION
    CODEX_VERSION        = CODEX_VERSION
    CLAUDE_CODE_VERSION  = CLAUDE_CODE_VERSION
  }
}
