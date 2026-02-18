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

2. Update the WebSocket URL in `index.html` if needed:
```javascript
const WS_URL = 'wss://your-backend-url.com/ws/interview';
```

3. Open `index.html` in your browser or deploy to Netlify/Vercel.

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
