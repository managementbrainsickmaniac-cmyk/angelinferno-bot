# 🌍 AngelFerno - Global Access Setup Guide

## Overview
Your bot and panel are now ready for **global hosting**. Users from anywhere in the world can login with just credentials - no configuration, no deployment needed.

---

## Architecture

```
Your Server (24/7 running)
    ↓
fernotest.py (bot + web server on port 8080)
    ↓
Static IP / Domain Name (your-domain.com or IP:8080)
    ↓
Users worldwide can access and login
```

---

## Setup Steps

### Step 1: Configure Your Server URL

Edit the HTML file and set your actual domain/IP:

**Option A: Using a Domain Name (Recommended)**
```
https://yourpanel.com
```

**Option B: Using Static IP**
```
http://your-static-ip:8080
or
https://your-static-ip:8080 (if using HTTPS)
```

### Step 2: Update the Frontend

In `Web/angel-dreamers-login.html`, find the `getServerBaseUrl()` function:

**Current (for development):**
```javascript
return window.location.origin;
```

**For production, change to:**
```javascript
return 'https://yourpanel.com'; // Use your actual domain
// OR
return 'http://your-static-ip:8080'; // Use your static IP
```

### Step 3: Configure Backend for HTTPS (Optional but Recommended)

If using HTTPS, update `fernotest.py`:

```python
# Around line 2236, change:
web.run_app(app, host='0.0.0.0', port=8080)

# To (if using SSL certificates):
web.run_app(app, host='0.0.0.0', port=8080, 
            ssl_context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER))
```

### Step 4: Configure Firewall

Allow port 8080 (or 443 for HTTPS) through your router:
- Forward port 8080 to your server's internal IP
- Or use reverse proxy (Nginx) to handle HTTPS

### Step 5: Start the Bot

```bash
python fernotest.py
```

The server now listens on `0.0.0.0:8080` (all network interfaces)

---

## How Users Access the Panel

### Step 1: Get Credentials from Bot
Users interact with your Telegram bot and get credentials:
```
panel_id: abc123xyz
password: SecurePass@123
```

### Step 2: Access Panel
Users go to:
```
https://yourpanel.com
or
http://your-static-ip:8080
```

### Step 3: Login
Users enter their credentials → Access their panel ✅

**No setup. No configuration. Just login.**

---

## Configuration Options

### Option 1: Auto-Detect (Development)
```javascript
function getServerBaseUrl() {
    return window.location.origin;
}
```
**Use for:** Testing locally  
**Limitation:** Only works on same domain/port

### Option 2: Fixed Domain (Recommended for Production)
```javascript
function getServerBaseUrl() {
    return 'https://yourpanel.com';
}
```
**Use for:** Production with domain  
**Benefit:** Works from anywhere, even if accessed via different domain

### Option 3: Fixed IP
```javascript
function getServerBaseUrl() {
    return 'http://203.0.113.42:8080';
}
```
**Use for:** Static IP without domain  
**Note:** Less reliable if IP ever changes

---

## HTTPS Setup (Recommended)

### Using Let's Encrypt (Free SSL)

1. Install Certbot:
```bash
apt-get install certbot
```

2. Generate certificate:
```bash
certbot certonly --standalone -d yourpanel.com
```

3. Update fernotest.py:
```python
import ssl

ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ssl_context.load_cert_chain(
    certfile='/etc/letsencrypt/live/yourpanel.com/fullchain.pem',
    keyfile='/etc/letsencrypt/live/yourpanel.com/privkey.pem'
)

web.run_app(app, host='0.0.0.0', port=443, ssl_context=ssl_context)
```

### Using a Reverse Proxy (Nginx)

```nginx
server {
    listen 443 ssl;
    server_name yourpanel.com;
    
    ssl_certificate /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;
    
    location / {
        proxy_pass http://localhost:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

---

## Distribute to Users

### What to Share:
```
🔐 AngelFerno Panel Access

Login URL: https://yourpanel.com

Your Credentials:
├─ Panel ID: abc123xyz
└─ Password: SecurePass@123

Just open the URL and login - no setup needed!
```

### What NOT to Share:
❌ Server IP addresses  
❌ Configuration details  
❌ Internal setup  
❌ Database access  

---

## Monitoring & Maintenance

### Check Server Status
```bash
# See if bot is running
ps aux | grep fernotest.py

# Check port 8080 is listening
netstat -tlnp | grep 8080

# View logs
tail -f bot_output.log
```

### Keep Running 24/7
Use a process manager:

```bash
# Install PM2
npm install -g pm2

# Start bot
pm2 start fernotest.py --name "angelferno-bot"

# Restart on boot
pm2 startup
pm2 save
```

### Database Backups
```bash
# Backup credentials
cp wallets.db wallets.db.backup.$(date +%Y%m%d)
```

---

## Security Best Practices

### ✅ Do This:
- [x] Use HTTPS in production (not HTTP)
- [x] Keep credentials database encrypted
- [x] Use strong passwords (12+ characters)
- [x] Monitor failed login attempts
- [x] Regular backups of credentials database
- [x] Use firewall to limit access if needed

### ❌ Don't Do This:
- [ ] Share server credentials with users
- [ ] Use HTTP in production
- [ ] Store passwords in plain text
- [ ] Open all ports to the world
- [ ] Disable CORS without reason

---

## Troubleshooting

### "Connection Refused" from User
1. Check bot is running: `python fernotest.py`
2. Check port 8080 is open: `netstat -tlnp`
3. Check firewall allows port 8080
4. Check domain/IP resolves: `nslookup yourpanel.com`

### "SSL Certificate Error"
1. Verify HTTPS is configured
2. Check certificate is valid
3. Use `https://` not `http://`
4. Check certificate expiration

### "Login Not Working"
1. Check credentials are correct in bot
2. Verify database is accessible
3. Check CORS headers in response
4. Review bot_output.log for errors

---

## What Users See

### Step 1: Access Panel
```
Browser: https://yourpanel.com
         ↓
Login Page Loads (Modern Dark UI with Red Accents)
```

### Step 2: Enter Credentials
```
Panel ID: [          ]
Password: [          ]
[     LOGIN     ]
```

### Step 3: Access Dashboard
```
✅ Login successful
↓
Dashboard with stats, commands, history
```

---

## Deployment Checklist

- [ ] Bot running on your server 24/7
- [ ] Port 8080 open and forwarded
- [ ] Domain name configured (or static IP)
- [ ] HTTPS certificate installed
- [ ] Frontend URL updated to your domain
- [ ] Database accessible
- [ ] Backups configured
- [ ] Monitoring set up
- [ ] Users have credentials
- [ ] Users can login and access panel

---

## Support for Users

When users say "I can't login":

1. **Check credentials:** Verify panel_id and password are correct
2. **Check connection:** Can they reach the domain?
3. **Check browser:** Try incognito/private mode
4. **Check firewall:** Personal firewall blocking?
5. **Check CAPS LOCK:** Is it on?

**Most common issue:** Wrong credentials or copy-paste errors

---

## Summary

✅ **Bot runs on your server 24/7**  
✅ **Users access from anywhere in the world**  
✅ **No setup or configuration needed for users**  
✅ **Just credentials + URL = instant access**  
✅ **Works on any device, any location, any time**  

**You deployed once. Users never deploy. Perfect.**
