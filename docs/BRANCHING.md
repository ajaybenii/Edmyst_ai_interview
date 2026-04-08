# Branching & Deployment Strategy

This repository uses a strict 3-branch promotion model corresponding to our 3 deployment environments (`Dev`, `Stage`, and `Prod`). 

## Part 1 — Git Branches & Promotion

| Git branch | Purpose | Typical merge path |
|------------|---------|---------------------|
| `dev` | Daily integration / dev deploy | Feature branches → PR → `dev` |
| `stage` | Pre-prod / UAT | PR `dev` → `stage` |
| `master` | Production | PR `stage` → `master` |

*   **Remote default branch:** `master`
*   **Long-lived branches:** `dev`, `stage`, `master`.
*   **Secrets:** Never commit `.env` or secrets. Use GitHub Actions Secrets for deployment bindings.

## Part 2 — Environment Variable (`Stage`)

The application code reads the `Stage` environment variable to determine logical resource suffixes (e.g. S3 buckets, SNS topics).

| Git branch | `Stage` env value (exact string) |
|------------|----------------------------------|
| `dev` | `Dev` |
| `stage` | `Stage` |
| `master` | `Prod` |

## Part 3 — API Base URLs & Nginx Proxy Paths

Unlike the `edy-apis` repository (which runs on serverless API Gateway mapping to `/Dev/`, etc.), **this module runs on pure EC2 via Docker + Nginx.**

To maintain exact URL consistency, Nginx handles paths the same way:

| Environment | `Stage` | Base URL Pattern | Internally Routes To |
|-------------|---------|----------------------|----------------------|
| Development | `Dev` | `https://dev-edy-audio-be.edmyst.com/` | `localhost:8002` |
| Staging | `Stage` | `https://stage-edy-audio-be.edmyst.com/` | `localhost:8001` |
| Production | `Prod` | `https://edy-audio-be.edmyst.com/` | `localhost:8000` |

*(Note: While Nginx routes via subdomain instead of `/Dev` path purely for compatibility with WebSocket namespaces, the application `Stage` correctly triggers any internal environment suffix required).*

## Part 4 - Deployment Flow

Deployment is strictly automated via GitHub Actions (`.github/workflows/deploy.yml`):
1. Code push to a monitored branch.
2. Checks trigger.
3. SSH deployment pulling the latest respective branch on the EC2 host.
4. Starts the associated Docker container named `fastapi-app-{env}` exposing its unique port.
