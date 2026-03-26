# AI Audio Interview Frontend

Real-time AI-powered interview interface that connects to the Gemini Live Audio/Video API.

## Features

- 🎙️ Real-time voice conversation with AI interviewer
- 🎥 Video streaming support
- 📝 Live transcription of both user and AI
- 🎨 Modern glass morphism UI design
- 🔄 AI speaking indicator with animated orb

## Setup

1. Clone the repository:
```bash
git clone https://github.com/ajaybenii/AI-Audio-Interview-Frontend.git
```

2. **URLs (Backend `.env`):** Copy `Backend/.env.example` → `Backend/.env`, set `PUBLIC_API_BASE` (and optional `PUBLIC_WS_URL`). Then:
   ```bash
   cd Backend && python generate_frontend_config.py
   ```
   This writes `Frontend/interview-config.js`. Agar ye file na ho, `index.html` mein fallback URLs use hongi.

3. Open `index.html` in your browser or deploy to Netlify/Vercel (**`interview-config.js` deploy karna mat bhoolna**).

## Backend

This frontend connects to the AI Audio Interview Backend:
- Repository: https://github.com/ajaybenii/AI-Audio-only-Interview-Backend
- Live API: https://ai-audio-interview.onrender.com

## Deployment

### Netlify (Recommended)
1. Go to [netlify.com](https://netlify.com)
2. Drag and drop this folder
3. Done!

## License

MIT
