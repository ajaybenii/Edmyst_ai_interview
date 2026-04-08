# AI Audio Interview Backend

Real-time AI-powered interview bot using Google's Gemini Live Audio/Video API via WebSocket. This repository employs a strict 3-branch Git CI/CD strategy deployed on AWS EC2.

## Features

- 🎙️ Real-time voice conversations with AI interviewer
- 🎥 Video support for visual context
- 📝 Live transcription of both user and AI speech
- 🔄 Session resumption for handling connection resets
- 📊 Connection management and statistics
- 🚀 Automated 3-Environment CI/CD deployment on EC2

## Tech Stack

- **FastAPI** - Async Python web framework
- **WebSocket** - Real-time bidirectional communication
- **Google Gemini Live API** - AI model for interviews
- **Docker & Nginx** - Application containerization and Reverse Proxy
- **AWS EC2** - Hosting infrastructure

## Deployment Strategy & Git Architecture

This repository uses a strict 3-branch promotion model corresponding to 3 deployment environments (`Dev`, `Stage`, and `Prod`) hosted concurrently on a single AWS EC2 instance.

| Git branch | `Stage` Env | Base URL Pattern | Docker Port |
|------------|-------------|----------------------|-------------|
| `dev` | `Dev` | `https://dev-edy-audio-be.edmyst.com/` | `8002` |
| `stage` | `Stage` | `https://stage-edy-audio-be.edmyst.com/` | `8001` |
| `master` | `Prod` | `https://edy-audio-be.edmyst.com/` | `8000` |

*Please review `docs/BRANCHING.md` for a complete breakdown of the branching and environment mapping logic.*

## Automated CI/CD (GitHub Actions)

Deployment is fully automated using GitHub Actions (`.github/workflows/deploy.yml`). 
When code is pushed or merged into `dev`, `stage`, or `master`:
1. The GitHub action logs into the EC2 instance via SSH.
2. It fetches the specific branch.
3. Builds a tailored Docker Image (`fastapi-app-img-{branch}`).
4. Runs the Docker Container mapping the internal FastApi `8000` port to the respective host environment port (`8002`, `8001`, `8000`).

### Required GitHub Repo Secrets
To enable the pipeline, the following secrets must be defined in your GitHub repository before deploying:
- `GH_PAT`: GitHub Personal Access Token to pull the repository securely on the server.
- `EC2_HOST`: The IP address of the EC2 Server (e.g., `44.223.22.140`)
- `EC2_USERNAME`: The SSH username (e.g., `ubuntu`)
- `EC2_SSH_KEY`: The raw `.pem` private key content used to authenticate with the EC2 target.

## Environment Config

The application relies heavily on the `Stage` environment variable to determine which AWS resources (S3, SNS) to interact with to ensure separation of concerns between environments.

| Variable | Description |
|----------|-------------|
| `Stage` | The environment logical name (`Dev`, `Stage`, or `Prod`). Injected automatically during CI/CD. |
| `GOOGLE_APPLICATION_CREDENTIALS` | Path to GCP service account JSON |
| `LAMBDA_SESSION_URL` | URL to the Lambda lookup for conversation config |
| `TAVUS_RECORDINGS_BUCKET` | The bucket used for screen recordings (defaults to `edy-tavus`) |
| `MAX_CONCURRENT_CONNECTIONS` | Max WebSocket connections (Default `1000`) |
| `CONNECTION_TIMEOUT` | Default WebSocket timeout (Default `1800`) |

*A dummy template is provided in `Backend/.env.example`.*
