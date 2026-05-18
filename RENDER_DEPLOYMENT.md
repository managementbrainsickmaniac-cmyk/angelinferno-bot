# 🚀 FREE HOSTING SETUP - Render.com Deployment

**Get your bot and website online 24/7 without your laptop!**

---

## Overview

Your current setup:
- ❌ Bot runs only when laptop is on
- ❌ ngrok URL changes frequently
- ❌ Unreliable connection
- ❌ Cannot be accessed reliably from mobile

**Solution: Deploy to Render.com (FREE)**
- ✅ Bot always online
- ✅ Permanent stable URL
- ✅ Automatic scaling
- ✅ 99.9% uptime

---

## Step 1: Prepare GitHub Repository

### If you DON'T have a GitHub repo yet:

```bash
cd c:\Users\MUKASA\Desktop\nova\AngelInferno
git init
git add .
git commit -m "Initial commit - ready for deployment"
```

Then create a repo on GitHub and push:
```bash
git remote add origin https://github.com/YOUR_USERNAME/AngelInferno.git
git branch -M main
git push -u origin main
```

### If you ALREADY have a GitHub repo:
```bash
cd c:\Users\MUKASA\Desktop\nova\AngelInferno
git add .
git commit -m "Update for free hosting deployment"
git push
```

---

## Step 2: Sign Up for Render.com

1. Visit **[render.com](https://render.com)**
2. Click **Sign up**
3. Use GitHub account for easiest setup
4. Verify email

**Takes 2 minutes, completely FREE**

---

## Step 3: Deploy Your Bot

### On Render Dashboard:

1. Click **New** → **Web Service**
2. Click **Deploy existing repo** and select your GitHub repo
3. **Configure:**
   - **Name:** `angelferno-bot` (no spaces)
   - **Environment:** Python 3
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `python fernotest.py`
   - **Region:** Select closest to you
   - **Plan:** Free (default)

4. **Add Environment Variables:**
   Click **Environment** and add:
   ```
   TELEGRAM_BOT_TOKEN = 7834224349:AAGwfsUecVS3jDj8YsIWB4hmSGLiql0txeQ
   PORT = 8080
   ```

5. Click **Create Web Service**

**Wait 2-3 minutes for deployment...**

### You'll see:
```
✅ Build successful
✅ Deploy successful
Your service is live at: https://angelferno-bot.onrender.com
```

---

## Step 4: Verify Deployment

### Test 1: Check Website Shows Correct URL

Open **angelferno_merged.html** and look at line ~2012:
```javascript
PRODUCTION_API_URL: 'https://angelferno-bot.onrender.com',
```

✅ Should already be correct (code was updated)

### Test 2: Verify Bot is Online

Open Telegram and send `/start` to your bot
- Should get instant response
- If not, check Render logs for errors

### Test 3: Check API Endpoints

Open in your browser:
```
https://angelferno-bot.onrender.com/credentials.json
```

Should show: JSON with your credentials

---

## Step 5: Update Website (If Needed)

The website code was already updated to auto-detect URLs, but if you need to manually update:

**In angelferno_merged.html around line 2000:**
```javascript
// Old (ngrok - unreliable):
const BOT_API_BASE = 'https://kennel-squeeze-ipad.ngrok-free.dev';

// New (Render - reliable):
const BOT_API_BASE = API_CONFIG.getAPIBase(); // Auto-detects
```

The new code automatically uses:
- `http://localhost:8080` for local testing
- `https://angelferno-bot.onrender.com` for production

---

## Step 6: Test Everything Works

### Test Login Flow:
1. Open your website
2. Enter Customer ID and password
3. Should connect to your Render-hosted bot
4. Should load your panel

### Test Bot Commands:
1. Open Telegram
2. Send `/start`
3. Send `/panel`
4. All commands should work instantly

### Test from Mobile:
1. Open website on your phone
2. Try to login
3. Should work even if laptop is off

---

## Your New Setup

### Old (Local + ngrok):
```
Laptop running 24/7
    ↓
Local fernotest.py on port 8080
    ↓
ngrok tunnel (unreliable)
    ↓
Website
```

### New (Render.com):
```
Render.com servers (always online)
    ↓
Your fernotest.py hosted
    ↓
Permanent URL: https://angelferno-bot.onrender.com
    ↓
Website + Bot + API (all working 24/7)
```

---

## What Render Gives You (FREE)

✅ **Always Running**
- No spin-down on free tier
- Your bot runs continuously

✅ **Permanent URL**
- https://angelferno-bot.onrender.com
- Never changes (unlike ngrok)

✅ **HTTPS Included**
- Secure by default

✅ **Global CDN**
- Fast connections worldwide

✅ **Logs & Monitoring**
- See what's happening in real-time

✅ **Auto-Restart**
- Service restarts if it crashes

---

## Important Endpoints

All these now work at your new URL:

```
https://angelferno-bot.onrender.com/verify_credentials    ← Login
https://angelferno-bot.onrender.com/logout                ← Logout
https://angelferno-bot.onrender.com/credentials.json      ← Get credentials
https://angelferno-bot.onrender.com/ocrs/rotate           ← Token rotation
```

---

## Troubleshooting

### "Bot not responding"
1. Go to Render Dashboard
2. Find `angelferno-bot` service
3. Click it
4. Check **Logs** tab
5. Look for any error messages
6. Click **Restart** if needed

### "Connection refused" on website
1. Open browser DevTools (F12)
2. Go to **Network** tab
3. Refresh website
4. Look for failed requests
5. Check if URL is `https://angelferno-bot.onrender.com`

### "504 Bad Gateway"
- Service might still be starting (wait 1 minute)
- Or service crashed (check Render logs)
- Click Restart in Render dashboard

### "Credentials not loading"
1. Make sure you set `TELEGRAM_BOT_TOKEN` in Render environment
2. Check Bot token is correct (should start with 7834224349)
3. Restart service

---

## Monitoring & Maintenance

### Monitor Your Service:
- Render Dashboard → Select service → Logs
- Check for errors regularly
- Bot will auto-restart if issues occur

### Monitor Costs:
- Free tier: Always free
- No credit card charges
- No surprise bills

### Backup Your Data:
The database and credentials are stored on the server. Make sure to:
1. Regularly export credentials
2. Back up your database

---

## Next Steps

- [ ] Push code to GitHub
- [ ] Create Render account
- [ ] Deploy service
- [ ] Test bot online
- [ ] Test website login
- [ ] Delete ngrok tunnels (no longer needed)
- [ ] Share your new stable URL with users

---

## Alternative Free Hosting Services

If Render has issues, try:

1. **Railway.app**
   - Similar to Render
   - $5/month free credit
   - https://railway.app

2. **Koyeb**
   - Good performance
   - Free tier available
   - https://koyeb.com

3. **Fly.io**
   - Distributed globally
   - Generous free tier
   - https://fly.io

---

## File Updates Made

✅ **angelferno_merged.html**
- Added flexible API configuration
- Auto-detects local vs production

✅ **angel-dreamers-login.html**
- Updated to use flexible configuration

✅ **FREE_HOSTING_DEPLOYMENT_GUIDE.md** (Created)
- Comprehensive deployment guide
- All troubleshooting steps

✅ **RENDER_DEPLOYMENT.md** (This file)
- Step-by-step Render.com deployment

---

## Support

For help with:
- **Render issues:** https://docs.render.com
- **Code issues:** Check this guide and logs
- **Bot questions:** Check fernotest.py docs

---

**Your bot is now ready to be hosted on the cloud!** 🎉

*Setup time: ~30 minutes (mostly waiting for deployment)*

*Result: Bot online 24/7 without laptop needed!*
