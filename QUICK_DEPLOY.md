# QUICK DEPLOY - Copy & Paste Reference

## URLs to Know

| Service | URL |
|---------|-----|
| Render | https://render.com |
| Your Bot (After Deploy) | https://angelferno-bot.onrender.com |
| Telegram Bot Token | 7834224349:AAGwfsUecVS3jDj8YsIWB4hmSGLiql0txeQ |

---

## 1️⃣ Push to GitHub (One Time)

```bash
cd c:\Users\MUKASA\Desktop\nova\AngelInferno
git init
git add .
git commit -m "Deploy to Render"
git remote add origin https://github.com/YOUR_USERNAME/AngelInferno.git
git push -u origin main
```

---

## 2️⃣ Render Dashboard Settings

**When creating Web Service on Render:**

```
Name:           angelferno-bot
Environment:    Python 3
Build Command:  pip install -r requirements.txt
Start Command:  python fernotest.py
Region:         Choose closest
Plan:           Free
```

**Environment Variables:**
```
TELEGRAM_BOT_TOKEN = 7834224349:AAGwfsUecVS3jDj8YsIWB4hmSGLiql0txeQ
PORT = 8080
```

---

## 3️⃣ Test Your Bot

**In Telegram:** Send `/start`
- Should respond immediately

**In Browser:** Visit
```
https://angelferno-bot.onrender.com/credentials.json
```
- Should show JSON data

---

## 4️⃣ If Something's Wrong

**Check Render Logs:**
```
Render Dashboard → Your Service → Logs
```

**Common Errors:**
- "No module named telegram" → Build failed
- "Failed to connect to token" → Wrong/missing token
- "Connection refused" → Service not running

**Fix:** 
1. Check environment variables
2. Click Restart
3. Wait 2 minutes
4. Try again

---

## 5️⃣ Your Website Already Updated ✅

Code auto-detects:
- **Local:** `http://localhost:8080` (for testing on laptop)
- **Production:** `https://angelferno-bot.onrender.com` (for deployed version)

---

## Deploy Steps Recap

1. ✅ Push code to GitHub
2. ✅ Sign up Render.com (free)
3. ✅ Create Web Service
4. ✅ Add environment variables
5. ✅ Deploy (2-3 min)
6. ✅ Test bot + website
7. ✅ Done! 24/7 online

**Total Time:** 30 minutes
**Cost:** FREE
**Result:** Bot online forever! 🚀

---

*See RENDER_DEPLOYMENT.md or FREE_HOSTING_DEPLOYMENT_GUIDE.md for details*
