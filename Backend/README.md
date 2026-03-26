# AI Audio Interview Backend

Real-time AI-powered interview bot using Google's Gemini Live Audio/Video API via WebSocket.

## Features

- 🎙️ Real-time voice conversations with AI interviewer
- 🎥 Video support for visual context
- 📝 Live transcription of both user and AI speech
- 🔄 Session resumption for handling connection resets
- 📊 Connection management and statistics

## Tech Stack

- **FastAPI** - Modern async Python web framework
- **WebSocket** - Real-time bidirectional communication
- **Google Gemini Live API** - AI model for voice/video interviews
- **Uvicorn** - ASGI server

## Setup

1. Clone the repository:
```bash
git clone https://github.com/ajaybenii/AI-Audio-only-Interview-Backend.git
cd AI-Audio-only-Interview-Backend
```

2. Create virtual environment:
```bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
```

3. Install dependencies:
```bash
pip install -r requirements.txt
```

4. Configure environment variables (create `.env` file):
```env
GOOGLE_APPLICATION_CREDENTIALS=your-credentials.json
PROJECT_ID=your-gcp-project-id
LOCATION=us-central1
LOG_LEVEL=INFO
```

5. Run the server:
```bash
python main.py
```

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | API information |
| `/health` | GET | Health check |
| `/stats` | GET | Connection statistics |
| `/ws/interview` | WebSocket | Main interview endpoint |

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `GOOGLE_APPLICATION_CREDENTIALS` | Path to GCP service account JSON | - |
| `PROJECT_ID` | GCP Project ID | `propvr-ai-1` |
| `LOCATION` | GCP Region | `us-central1` |
| `LOG_LEVEL` | Logging level (DEBUG/INFO/WARNING/ERROR) | `INFO` |
| `MAX_CONCURRENT_CONNECTIONS` | Max WebSocket connections | `1000` |
| `CONNECTION_TIMEOUT` | Session timeout in seconds | `1800` |

## Deployment (Render)

1. Create a new Web Service on Render
2. Connect your GitHub repository
3. Set the following:
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `uvicorn main:app --host 0.0.0.0 --port $PORT`
4. Add environment variables in Render dashboard

## License

MIT
