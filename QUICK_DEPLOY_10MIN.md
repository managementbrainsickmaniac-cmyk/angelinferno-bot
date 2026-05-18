# ⚡ QUICK START - Deploy to Render in 10 Minutes

## Your Current Setup
- ✅ **Website**: InfinityFree (already online 24/7)
- ❌ **Bot**: Running locally only (offline when laptop shuts down)

## Solution: Deploy Bot to Render (Free)
Render provides 750 free hours/month = **always online**

---

## Step 1: Create GitHub Account (2 min)
```
1. Go to github.com
2. Sign up
3. Verify email
```

## Step 2: Push Your Code to GitHub (3 min)
Open PowerShell in your project folder:

```powershell
# Initialize git repo
git init

# Configure git (one time)
git config --global user.name "Your Name"
git config --global user.email "your@email.com"

# Add files
git add .

# Commit
git commit -m "Initial commit: AngelFerno bot"

# Add remote (replace YOUR_USERNAME)
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/angelinferno-bot.git

# Push to GitHub
git push -u origin main
```

## Step 3: Create Render Account & Deploy (3 min)
```
1. Go to render.com
2. Click "Sign Up" → Select "GitHub"
3. Authorize Render to access your repos
4. Click "New +" → "Web Service"
5. Select your angelinferno-bot repo
6. Fill in:
   - Name: angelinferno-bot
   - Environment: Python 3
   - Build Command: pip install -r requirements.txt
   - Start Command: python fernotest.py
   - Plan: Free
7. Click "Create Web Service" → Wait 3-5 minutes
```

## Step 4: Add Environment Variables (1 min)
In Render dashboard:
```
1. Go to your service
2. Click Settings → Environment
3. Add these variables:

   TELEGRAM_BOT_TOKEN = your_token_from_botfather
   TELEGRAM_MODE = webhook
   TELEGRAM_WEBHOOK_URL = https://angelinferno-bot.onrender.com/webhook
   OCRS_SECRET_KEY = generate_random_32_char_string_here
```

## Step 5: Activate Webhook (1 min)
Run in PowerShell:

```powershell
$botToken = "YOUR_TOKEN_HERE"
$webhookUrl = "https://angelinferno-bot.onrender.com/webhook"

Invoke-WebRequest `
  -Uri "https://api.telegram.org/bot$botToken/setWebhook" `
  -Method POST `
  -Body @{url = $webhookUrl; drop_pending_updates = "true"} `
  -ContentType "application/x-www-form-urlencoded"
```

Expected response: `"ok": true`

## Step 6: Test It Works (1 min)
```
1. Open Telegram
2. Message your bot
3. Send: /start
4. Check Render logs for activity
```

---

## ✅ YOU'RE DONE!

Your bot is now:
- ✅ Online 24/7
- ✅ Always responsive
- ✅ Auto-deployed when you push to GitHub
- ✅ Connected to your website on InfinityFree

---

## How It Works

```
User on Telegram
       ↓
   Telegram API
       ↓
Render Webhook (Your Bot)
       ↓
Your Website on InfinityFree
```

---

## Key Points

| Item | Status | URL |
|------|--------|-----|
| Bot | Always Online | https://angelinferno-bot.onrender.com |
| Website | Always Online | Your InfinityFree domain |
| Health Check | Enabled | https://angelinferno-bot.onrender.com/health |

---

## Future Updates

To update your bot, just push to GitHub:
```powershell
git add .
git commit -m "Update features"
git push origin main
```

Render automatically deploys within 30 seconds! 🚀

---

## Troubleshooting

**Bot offline after 15 minutes?**
- Normal on free tier
- Webhook mode keeps it alive with health checks

**Website can't reach bot?**
- Check PRODUCTION_API_URL in HTML matches Render URL
- Verify environment variables in Render settings

**Need help?**
- Check Render logs: Dashboard → Your Service → Logs
- See GITHUB_DEPLOYMENT_GUIDE.md for detailed guide
