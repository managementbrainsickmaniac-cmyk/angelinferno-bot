# Free Hosting Deployment Guide

## Problem
Your bot and website are currently running on your laptop using ngrok (unstable, changes URLs), which means they go offline when your laptop turns off.

## Solution
Deploy fernotest.py to **Render.com** for free. Both the Telegram bot and web API will stay online 24/7.

---

## Step 1: Prepare Your GitHub Repository

If you don't have a GitHub repository:

```bash
git init
git add .
git commit -m "Initial commit"
```

Push to GitHub:
```bash
git remote add origin https://github.com/YOUR_USERNAME/AngelInferno.git
git branch -M main
git push -u origin main
```

---

## Step 2: Deploy to Render.com

### Option A: Using Render Dashboard (Recommended)

1. **Sign up for free at** [render.com](https://render.com)
2. Click **New +** → **Web Service**
3. **Connect your GitHub repository**
   - Select your AngelInferno repo
   - Authorize if needed
4. **Configure the service:**
   - **Name:** `angelferno-bot` (or any name)
   - **Environment:** Python 3
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `python fernotest.py`
   - **Region:** Choose closest to you
   - **Plan:** Free
5. **Add Environment Variables:**
   - Click **Environment**
   - Add these variables:
     ```
     TELEGRAM_BOT_TOKEN=7834224349:AAGwfsUecVS3jDj8YsIWB4hmSGLiql0txeQ
     PORT=8080
     ```
6. **Create Web Service**
   - Wait for deployment (2-3 minutes)
   - Your bot will be online!

### Your New URL will be:
```
https://angelferno-bot.onrender.com
```

---

## Step 3: Update Your Website

Replace the hardcoded ngrok URL in `angelferno_merged.html`:

**Find this line (around line 2000):**
```javascript
const BOT_API_BASE = window.BOT_API_BASE = 'https://kennel-squeeze-ipad.ngrok-free.dev';
```

**Replace with:**
```javascript
const BOT_API_BASE = window.BOT_API_BASE = 'https://angelferno-bot.onrender.com';
```

Or, to make it automatically detect local vs production:
```javascript
const BOT_API_BASE = window.BOT_API_BASE = 
    window.location.hostname === 'localhost' 
        ? 'http://localhost:8080' 
        : 'https://angelferno-bot.onrender.com';
```

---

## Step 4: Verify Deployment

1. **Check bot status:**
   - Open Telegram and send `/start` to your bot
   - You should see responses (bot is online!)

2. **Check web API:**
   - Open browser: `https://angelferno-bot.onrender.com/credentials.json`
   - You should see your credentials (API is working!)

3. **Check website:**
   - Open your website in browser
   - Try logging in - should connect to the hosted bot

---

## Key Endpoints (All Working on Render)

- `https://angelferno-bot.onrender.com/verify_credentials` - Login verification
- `https://angelferno-bot.onrender.com/logout` - Session logout
- `https://angelferno-bot.onrender.com/credentials.json` - Get credentials
- `https://angelferno-bot.onrender.com/ocrs/rotate` - OCRS token rotation

---

## Important Notes

### Free Tier Limitations (Still Great!)
- ✅ **Always running** (unlike ngrok)
- ✅ **No inactivity spin-down** for Python apps
- ✅ **No credit card required**
- ✅ **Unlimited data transfer**
- ⚠️ Shared CPU (but fine for a bot)

### After Updating Website

Edit [angelferno_merged.html](../Web/angelferno_merged.html) at line 2000:

```diff
- const BOT_API_BASE = window.BOT_API_BASE = 'https://kennel-squeeze-ipad.ngrok-free.dev';
+ const BOT_API_BASE = window.BOT_API_BASE = 'https://angelferno-bot.onrender.com';
```

---

## Troubleshooting

### Bot not responding
- Check Telegram bot token in Render environment variables
- Go to Render dashboard → Logs and check for errors
- Restart the service: Dashboard → Restart

### API returning 404
- Verify endpoint URL matches: `https://angelferno-bot.onrender.com/verify_credentials`
- Check CORS is enabled (it is by default in fernotest.py)
- Open browser developer tools (F12) and check network requests

### Website shows connection error
- Clear browser cache (Ctrl+Shift+Delete)
- Verify BOT_API_BASE URL in website code
- Check browser console for actual error messages

---

## Alternative Free Hosting Options

If Render has issues, try these alternatives:

1. **Railway.app** - Similar to Render, very reliable
   - https://railway.app
   - Free tier: $5/month credit
   
2. **Koyeb** - Good uptime
   - https://koyeb.com
   - Free tier available
   
3. **PythonAnywhere** - Python-specific
   - https://pythonanywhere.com
   - Free tier with limitations

---

## Local Development

To test locally before deploying:

```bash
# Create .env file with your token
echo "TELEGRAM_BOT_TOKEN=7834224349:AAGwfsUecVS3jDj8YsIWB4hmSGLiql0txeQ" > .env

# Install dependencies
pip install -r requirements.txt

# Run locally
python fernotest.py

# Website will use: http://localhost:8080
```

---

## Next Steps

1. ✅ Create Render account
2. ✅ Deploy fernotest.py to Render
3. ✅ Update website BOT_API_BASE URL
4. ✅ Test bot and website
5. ✅ Delete ngrok tunnels (no longer needed)

**Your bot will now stay online 24/7 for free!** 🚀
