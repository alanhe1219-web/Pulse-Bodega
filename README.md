# pulse_yay

Live Super Bowl buzz → sentiment → meme-ready marketing suggestion.

## What’s implemented

- **Live API data**: pulls real-time posts from **Reddit JSON endpoints** (no credentials required)
- **AI-ish processing**: sentiment scoring (VADER) + simple “moment keyword” detection
- **Celeb/player enrichment (live)**: extracts names from live posts and enriches them with **Wikipedia summary + thumbnail**
- **Meme generation**: generates a shareable **PNG** (optionally using celeb/player photo background) with food/bev promo copy
- **Optional X/Twitter posting**: posts the generated meme to X if credentials are configured

## Quickstart (local)

```bash
cd /Users/hezongqian/Pulse_NYC/pulse_yay-1
python -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000 --reload
```

Then open:

- `http://localhost:8000/health`
- `http://localhost:8000/buzz?source=reddit&subreddit=nfl&q=super%20bowl`
- `http://localhost:8000/meme_suggestion?business=coffee%20shop&offer=BOGO&q=super%20bowl`
- **Meme image (PNG)**: `http://localhost:8000/meme.png?business=coffee%20shop&offer=BOGO&q=super%20bowl`
- **Meme (JSON w/ data URL)**: `http://localhost:8000/meme?business=coffee%20shop&offer=BOGO&q=super%20bowl`
- **Celebrities (live)**: `http://localhost:8000/celebs?q=super%20bowl&subreddit=nfl`
- **Meme w/ celeb off**: `http://localhost:8000/meme.png?business=coffee%20shop&offer=BOGO&q=super%20bowl&with_celeb=false`
- **Random meme**: `http://localhost:8000/meme.png?business=coffee%20shop&offer=BOGO&q=super%20bowl&with_celeb=true&randomize=true`
- **Random meme (repeatable)**: `http://localhost:8000/meme.png?business=coffee%20shop&offer=BOGO&q=super%20bowl&with_celeb=true&randomize=true&seed=123`
- **Trend (one-call dashboard)**: `http://localhost:8000/trend?q=super%20bowl&subreddit=nfl`

## Post to X/Twitter (optional)

You need user-context credentials (OAuth1.0a). Set env vars:

- `X_API_KEY`
- `X_API_SECRET`
- `X_ACCESS_TOKEN`
- `X_ACCESS_TOKEN_SECRET`

Then call:

- `POST http://localhost:8000/x/post_latest?business=bagel%20shop&offer=FREE%20COFFEE&q=super%20bowl&with_celeb=true`

## Deploy (Render)

- **Recommended**: Render (fastest for hackathons).

### Option A — one-click with `render.yaml` (recommended)
- Push this repo to GitHub.
- In Render: **New → Blueprint** and select your repo.
- Render will read `render.yaml` and create the service automatically.
- When it’s live you’ll get a public URL like: `https://pulse-yay.onrender.com`

### Option B — manual Render web service
- **Build command**: `pip install -r requirements.txt`
- **Start command**: `uvicorn app:app --host 0.0.0.0 --port $PORT`

## Live demo flow (judging script)

Replace `BASE_URL` with your deployed URL (or `http://localhost:8000`).

### 1) Real Data → AI processing (sentiment + keywords)
```bash
BASE_URL="https://YOUR-SERVICE.onrender.com"
curl -sS "$BASE_URL/buzz?source=reddit&subreddit=nfl&q=super%20bowl&limit=25" | python -m json.tool | head -n 60
```

### 2) Business output (meme PNG generated from live Reddit images)
```bash
curl -sS "$BASE_URL/meme.png?style=classic&focus=true&year=2026&subreddit=pics&q=super%20bowl%202026&business=bagel%20shop&offer=FREE%20COFFEE" -o meme.png
```

### 3) (Optional) Post to X/Twitter
If X credentials are not set, the endpoint returns a ready-to-post payload.
```bash
curl -sS -X POST "$BASE_URL/x/post_latest?business=bagel%20shop&offer=FREE%20COFFEE&q=super%20bowl&with_celeb=false" | python -m json.tool
```

### 🏈 Pulse NYC SB LX Hackathon: Big Wins, Small Businesses
Every year, the Super Bowl attracts 100+ million viewers in the U.S. alone. It’s one of the rare events where sports fans, casual viewers, and even non-sports watchers all tune in together. 
That attention is incredibly valuable yet incredibly expensive. A single 30-second Super Bowl ad costs millions of dollars, and brands often spend even more on production, celebrities, and cross-platform campaigns. But small businesses are shut out of that moment. Yet their customers watch the same game, scroll the same feeds, and react to the same moments.
Madison Avenue has 6 months and 500 people to plan for this game. You have 5 hours, your team, and your imagination to beat them.
### 🎯 The Challenge
Build an AI-powered tool that helps medium sized businesses capitalize on Super Bowl buzz in real time. 
This is not about producing a Super Bowl ad. It’s about real-time reaction, smart automation, and real business impact.
Your AI system should help a small business:
React quickly to what’s happening during the Super Bowl
Generate timely, relevant marketing content
Launch or suggest promotions tied to live moments
Turn cultural buzz into customer engagement or sales
Think in terms of: “Something just happened — what should this business do right now?”

### 🌍 Requirement #1: Real-World Data
Your project must use real data from live APIs OR process visual data for actionable insights.
Bonus points for both.

### 🚀 Requirement #2: Deployed and Working Live
Your project must be publicly accessible and functional during judging.
✅ You must:
Deploy your application to a live environment (not just localhost)
Provide a working URL or access method
Demonstrate the full flow live: Real Data → AI Processing → Business Output
### 🧠 What Judges Will Look For
1. Real-World Data Integration
Did the team successfully use live external data?
2. Live Deployment
Is the product working in a real, deployed environment?
3. Latency
How long elapsed between the TV "moment" and the AI's "response"?  (Target: < 45 sec.)
4. Business Impact
Would this realistically help a small business grow engagement or revenue?
5. Smart Use of AI
Is AI meaningfully powering content, decisions, or automation?
6. Usability
Could a busy small business owner actually use this without 2 million steps?
7. Demo Quality
Does the team clearly show: Live Data → AI Output → Business Action

### 🛠️ Possible Tech Stack
Ingestion (Multimodal): Gemini 1.5 Flash (or 2.0 Flash). Feed live video frames/audio chunks directly to the model to detect events without stitching disparate APIs.
Reasoning (Speed): DeepSeek R1 via Groq. Use this for sub-second strategic decisions ("It's a fumble -> Generate a 'Fumble-Proof' insurance ad").
Orchestration (Control): LangGraph. Build a state machine that maintains "Game Context" and only triggers expensive generation steps when specific confidence thresholds are met.
Generation (Visuals): Flux 1.1 Pro (via Fal.ai). The current standard for speed + text rendering capabilities (crucial for legible discount codes/slogans).
Frontend (The "Wow" Factor): Vercel AI SDK. Use the Data Stream Protocol to visualize the AI's "thought process" live on the dashboard, proving it's reacting in real-time.

Pro Tip for the Demo: Use a YouTube clip of a past Super Bowl and feed that into your system live during the pitch. Don't rely on an actual live broadcast during your demo (nothing might happen!). Show it processing "The Catch" or a "Fumble" to prove it works instantly.
