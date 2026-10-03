.DEFAULT_GOAL := help
ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
ENV_FILE ?= $(ROOT)/.env
MODE ?= registry
DEVICE ?= cuda
GHCR_USER ?= pei9564
KUBECTL ?= kubectl
# Honor exported KUBECONFIG; otherwise prefer the user's readable K3s copy.
KUBE_CONFIG ?= $(if $(KUBECONFIG),$(KUBECONFIG),$(wildcard $(HOME)/.kube/finance-k3s.yaml))
KUBE_CONTEXT ?=
K8S_OVERLAY ?= homelab
K8S_DIR := $(ROOT)/k8s/overlays/$(K8S_OVERLAY)
K8S_NAMESPACE := finance
K8S_APP ?= finance-scraper-service
PYTHON ?= python3
CLUSTER_ARGS := --kubectl "$(KUBECTL)" --kubeconfig "$(KUBE_CONFIG)" --context "$(KUBE_CONTEXT)" --namespace "$(K8S_NAMESPACE)"
KUBE := $(if $(KUBE_CONFIG),KUBECONFIG="$(KUBE_CONFIG)") $(KUBECTL) $(if $(KUBE_CONTEXT),--context "$(KUBE_CONTEXT)")
SERVICES := postgres redis finance-scraper-service stt-ai-worker
APPS := finance-scraper-service stt-ai-worker

ifeq ($(filter $(MODE),registry local dev),)
$(error MODE must be registry, local or dev)
endif
ifeq ($(filter $(DEVICE),cuda cpu),)
$(error DEVICE must be cuda or cpu)
endif

BASE := docker compose --project-directory "$(ROOT)" --env-file "$(ENV_FILE)" -f "$(ROOT)/docker-compose.yml"
FILES :=
ifneq ($(MODE),registry)
FILES += -f "$(ROOT)/docker-compose.local.yml"
endif
ifeq ($(MODE),dev)
FILES += -f "$(ROOT)/docker-compose.dev.yml"
endif
ifeq ($(DEVICE),cpu)
FILES += -f "$(ROOT)/docker-compose.cpu.yml"
endif
COMPOSE := STT_DEVICE=$(DEVICE) $(BASE) $(FILES)
LOCAL := STT_DEVICE=$(DEVICE) $(BASE) -f "$(ROOT)/docker-compose.local.yml"

.PHONY: help init require-env config login images pull build up update down stop restart logs ps auto-update e2e e2e-setup
help:
	@printf '%s\n' \
	  'make init                  Create .env without overwriting an existing file' \
	  'make login                 Log in to ghcr.io (GHCR_USER=pei9564)' \
	  'make up                    Pull published images and start all four core services' \
	  'make up MODE=local         Build sibling repos and start the current local code' \
	  'make up MODE=dev           Build local code and mount source/config for reload' \
	  'make up DEVICE=cpu         Run STT on CPU without requesting NVIDIA devices' \
	  'make update                Pull published images and recreate changed services' \
	  'make build                 Build local application images' \
	  'make pull / images         Pull registry images / show the selected image names' \
	  'make ps / logs             Show status / follow logs (MODE and DEVICE supported)' \
	  'make down                  Stop and remove containers; keep database/upload volumes' \
	  'make stop / restart        Stop / restart the core services' \
	  'make auto-update           Start optional Watchtower for registry images' \
	  'make config                Validate the selected Compose configuration' \
	  'make e2e-setup / e2e       Install / run Playwright tests' \
	  'make k8s-render            Render homelab manifests (K8S_OVERLAY=cpu supported)' \
	  'make k8s-check             Check both Kustomize overlays without a cluster' \
	  'make k8s-diff / k8s-deploy Preview / apply to the selected Kubernetes context' \
	  'make k8s-target            Show context/API server (KUBE_CONFIG=/path supported)' \
	  'make k8s-doctor            Check nodes, GPU, Secrets and storage (read-only)' \
	  'make k8s-status            Show pods, services and persistent volumes' \
	  'make k8s-logs              Follow app logs (K8S_APP=stt-ai-worker supported)' \
	  'make k8s-forward           Open scraper on localhost:8080'

init:
	@if [ -f "$(ENV_FILE)" ]; then \
	  printf '%s\n' '.env already exists; left unchanged.'; \
	else \
	  cp "$(ROOT)/.env.example" "$(ENV_FILE)"; \
	  printf '%s\n' 'Created .env. Fill in database password, GitHub token and LLM settings.'; \
	fi

require-env:
	@test -f "$(ENV_FILE)" || { printf '%s\n' 'Missing .env: run make init and fill in your settings.' >&2; exit 1; }

config: require-env
	@$(COMPOSE) config --quiet

login:
	docker login ghcr.io --username "$(GHCR_USER)"

images: require-env
	@$(COMPOSE) config --images

pull: require-env
	@STT_DEVICE=$(DEVICE) $(BASE) pull $(SERVICES)

build: require-env
	@$(LOCAL) build $(APPS)

up: config
ifeq ($(MODE),registry)
	@$(COMPOSE) up -d --pull always $(SERVICES)
else
	@$(COMPOSE) up -d --build $(SERVICES)
endif
	@$(COMPOSE) ps
	@printf '%s\n' 'Scraper: http://localhost:8080 (unless SCRAPER_PORT is changed)' 'STT: http://localhost:8001'

update: up

down: require-env
	@$(COMPOSE) down

stop: require-env
	@$(COMPOSE) stop $(SERVICES)

restart: require-env
	@$(COMPOSE) restart $(SERVICES)

logs: require-env
	@$(COMPOSE) logs -f --tail=100 $(SERVICES)

ps: require-env
	@$(COMPOSE) ps

auto-update: config
	@test "$(MODE)" = registry || { printf '%s\n' 'Watchtower is only supported with MODE=registry.' >&2; exit 1; }
	@$(COMPOSE) up -d watchtower

e2e-setup:
	@cd "$(ROOT)/e2e" && npm ci && uv venv .venv --python 3.12 && uv pip install --python .venv/bin/python -r requirements.lock && npx playwright install --with-deps chromium

e2e:
	@cd "$(ROOT)/e2e" && npm test

# These targets do not use Compose .env. App secrets are provisioned separately.
.PHONY: k8s-render k8s-check k8s-target k8s-diff k8s-deploy k8s-status k8s-logs k8s-forward
k8s-render:
	@KUBECONFIG=/dev/null $(KUBECTL) kustomize "$(K8S_DIR)"

k8s-check:
	@KUBECONFIG=/dev/null $(KUBECTL) kustomize "$(ROOT)/k8s/overlays/homelab" > /dev/null
	@KUBECONFIG=/dev/null $(KUBECTL) kustomize "$(ROOT)/k8s/overlays/cpu" > /dev/null
	@KUBECONFIG=/dev/null $(KUBECTL) kustomize "$(ROOT)/k8s/optional/vllm-external" > /dev/null
	@printf '%s\n' 'Kustomize configurations render successfully (offline check).'

k8s-target:
	@$(KUBE) config view --minify -o 'jsonpath=Context: {.contexts[0].name}{"\n"}API server: {.clusters[0].cluster.server}{"\n"}'
	@printf '%s\n' 'Overlay: $(K8S_OVERLAY) / Namespace: $(K8S_NAMESPACE)'

k8s-diff: k8s-target
	@$(KUBE) diff -k "$(K8S_DIR)"

k8s-deploy: k8s-target
	@$(KUBE) apply -k "$(K8S_DIR)"

k8s-status:
	@$(KUBE) -n $(K8S_NAMESPACE) get pods,services,pvc -o wide

k8s-logs:
	@$(KUBE) -n $(K8S_NAMESPACE) logs deployment/$(K8S_APP) --all-containers=true --tail=100 -f

k8s-forward:
	@$(KUBE) -n $(K8S_NAMESPACE) port-forward service/finance-scraper-service 8080:8080

.PHONY: k8s-doctor
k8s-doctor:
	@$(PYTHON) "$(ROOT)/scripts/k8s_doctor.py" $(CLUSTER_ARGS) --overlay "$(K8S_OVERLAY)"
