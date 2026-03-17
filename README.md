# Vinted Order Tracker — Cloud Deployment

Live order tracking dashboard connected to Gmail IMAP. Auto-syncs every 15 minutes.

## Features
- Tracks orders from **Sold → Paid** with full status timeline  
- Zero duplicates — unique orders with merged events  
- Highlights **returns** (red) and **cancellations** (amber)  
- Downloads **shipping labels** and **return forms** per order  
- **Auto-syncs every 15 minutes** via background scheduler  
- Health check endpoint at `/health`

---

## Option 1: Railway (Recommended — Easiest)

**Cost:** Free tier available (500 hours/month)

### Steps:

1. **Create a GitHub repo:**
```bash
cd vinted-tracker
git init
git add .
git commit -m "Vinted order tracker"
```

2. **Push to GitHub:**
```bash
# Create a repo on github.com, then:
git remote add origin https://github.com/YOUR_USERNAME/vinted-tracker.git
git branch -M main
git push -u origin main
```

3. **Deploy on Railway:**
   - Go to [railway.app](https://railway.app) → Sign in with GitHub
   - Click **"New Project"** → **"Deploy from GitHub repo"**
   - Select your `vinted-tracker` repo
   - Railway auto-detects the Dockerfile

4. **Set environment variables:**
   - Go to your service → **Variables** tab
   - Add these:
     ```
     GMAIL_USER = lentujamoda@gmail.com
     GMAIL_APP_PASSWORD = ivcr zkol jpvd mvso
     SYNC_INTERVAL = 15
     SYNC_DAYS_BACK = 90
     ```

5. **Get your URL:**
   - Go to **Settings** → **Networking** → **Generate Domain**
   - Your dashboard is now live at `https://vinted-tracker-xxxxx.up.railway.app`

---

## Option 2: Render

**Cost:** Free tier available (750 hours/month, spins down after inactivity)

### Steps:

1. **Push to GitHub** (same as above)

2. **Deploy on Render:**
   - Go to [render.com](https://render.com) → Sign in with GitHub
   - Click **"New +"** → **"Web Service"**
   - Connect your `vinted-tracker` repo
   - Settings:
     - **Runtime:** Python 3
     - **Build Command:** `pip install -r requirements.txt`
     - **Start Command:** `gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 120 --preload`

3. **Set environment variables:**
   - Go to **Environment** tab → Add:
     ```
     GMAIL_USER = lentujamoda@gmail.com
     GMAIL_APP_PASSWORD = ivcr zkol jpvd mvso
     SYNC_INTERVAL = 15
     ```

4. **Done!** Your URL will be `https://vinted-tracker-xxxx.onrender.com`

> **Note:** Render free tier spins down after 15 min of inactivity. 
> For 24/7 uptime, use the Starter plan ($7/month).

---

## Option 3: Fly.io

**Cost:** Free tier available (3 shared VMs)

### Steps:

1. **Install Fly CLI:**
```bash
# macOS
brew install flyctl

# Windows
powershell -Command "iwr https://fly.io/install.ps1 -useb | iex"

# Linux
curl -L https://fly.io/install.sh | sh
```

2. **Login and deploy:**
```bash
cd vinted-tracker
fly auth login
fly launch    # Accept defaults, region: lhr (London)
```

3. **Set secrets (environment variables):**
```bash
fly secrets set GMAIL_USER="lentujamoda@gmail.com"
fly secrets set GMAIL_APP_PASSWORD="ivcr zkol jpvd mvso"
fly secrets set SYNC_INTERVAL="15"
fly secrets set SYNC_DAYS_BACK="90"
```

4. **Deploy:**
```bash
fly deploy
```

5. **Your URL:** `https://vinted-tracker.fly.dev`

6. **Keep it running 24/7:**
```bash
fly scale count 1    # Ensures at least 1 machine always runs
```

---

## After Deployment

1. Open your dashboard URL
2. Click **"Fetch Orders"** to trigger the first sync
3. After that, it auto-syncs every 15 minutes
4. The dashboard auto-polls every 5 minutes to pick up new data

## Health Check

All platforms can ping `/health` to verify the app is running:
```
GET https://your-app-url/health
```

Returns:
```json
{
  "status": "healthy",
  "orders": 31,
  "last_fetch": "2026-03-16T23:45:00",
  "sync_interval": "15m"
}
```

## Environment Variables Reference

| Variable | Default | Description |
|----------|---------|-------------|
| `GMAIL_USER` | *(required)* | Gmail address |
| `GMAIL_APP_PASSWORD` | *(required)* | Gmail app password |
| `SYNC_INTERVAL` | `15` | Minutes between auto-syncs |
| `SYNC_DAYS_BACK` | `90` | Days of email history to scan |
| `PORT` | `5000` | Server port (set by cloud platform) |
| `DATA_DIR` | app directory | Where to store cache & attachments |
