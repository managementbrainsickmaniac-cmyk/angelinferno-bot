# 🚀 AngelFerno - Provider Setup Guide

## For Bot Operators/Service Providers

This guide explains how to set up AngelFerno to serve users globally with zero configuration needed on their end.

---

## What You Provide to Users

Each user gets **ONLY**:
1. **Panel URL** (e.g., `https://yourpanel.com`)
2. **Panel ID** (e.g., `abc123xyz`)
3. **Password** (e.g., `SecurePass@123`)

That's all they need. No setup, no configuration.

---

## Your Setup (One-Time Only)

### Prerequisites
- Server with 24/7 uptime (dedicated machine, VPS, or cloud)
- Static IP address or domain name
- Python 3.8+
- ~100-500MB disk space

### Installation

1. **Copy files to your server:**
```bash
# Copy the entire AngelFerno folder
scp -r AngelFerno/ user@your-server:/home/user/
```

2. **Install dependencies:**
```bash
cd AngelFerno
pip install -r requirements.txt
```

3. **Configure domain/IP:**

Open `Web/angel-dreamers-login.html` and find:
```javascript
function getServerBaseUrl() {
    return window.location.origin;
}
```

Replace with your actual domain:
```javascript
function getServerBaseUrl() {
    return 'https://yourdomain.com';
}
```

Or your IP:
```javascript
function getServerBaseUrl() {
    return 'http://203.0.113.42:8080';
}
```

4. **Start the bot:**
```bash
python fernotest.py
```

**That's it! You're ready to onboard users.**

---

## How Users Get Credentials

### Method 1: Via Telegram Bot
Users interact with your bot and get credentials:
```
/start → /panel → Creates account → Receives credentials
```

### Method 2: Manual Admin Creation
You create credentials in the database:
```python
from fernotest import store_credentials

store_credentials('user123', 'password123', 123)  # user_id should be the actual user ID
```

### Method 3: API Endpoint
If you built an API, users can register:
```bash
POST /register
{
  "panel_id": "user123",
  "password": "password123"
}
```

---

## Sharing Panel Access

### Option 1: Direct Link
Send each user:
```
Login here: https://yourdomain.com

Use these credentials:
Panel ID: user123
Password: SecurePass@123
```

### Option 2: Invite System
Create a simple invite system:
```
https://yourdomain.com/invite?code=abc123
```

### Option 3: QR Code
Generate QR code pointing to:
```
https://yourdomain.com
```
Share QR code + credentials separately

---

## Security Checklist

- [ ] Use HTTPS (install SSL certificate)
- [ ] Firewall configured (only needed ports open)
- [ ] Regular database backups
- [ ] Strong passwords enforced
- [ ] Rate limiting on login (prevent brute force)
- [ ] Monitoring enabled (check logs daily)
- [ ] Failed login alerting set up
- [ ] Database encryption enabled

---

## Scaling to Many Users

### For 10-100 Users
- Your current server setup works fine
- Monitor CPU/memory usage
- Regular backups sufficient

### For 100-1000 Users
- Consider load balancing
- Add database indexing
- Implement caching
- Monitor performance

### For 1000+ Users
- Use cloud infrastructure (AWS, GCP, Azure)
- Implement auto-scaling
- Use CDN for frontend
- Separate database server
- Add monitoring/alerting

---

## Monitoring Your Panel

### Check Server Health
```bash
# Is the bot running?
ps aux | grep fernotest.py

# Is port 8080 open?
netstat -tlnp | grep 8080

# Recent logs
tail -100 bot_output.log

# Disk space
df -h
```

### Common Metrics to Monitor
- Server uptime
- Active user sessions
- Failed login attempts
- Response time
- Database size
- Disk space available

### Set Up Alerts
Alert yourself if:
- Server stops responding
- Disk space < 10%
- Failed logins spike
- Database grows unexpectedly
- CPU/Memory > 80%

---

## Troubleshooting

### Users Can't Login
```
Checklist:
[ ] Server is running (ps aux | grep fernotest)
[ ] Port 8080 is open (netstat -tlnp)
[ ] Credentials are correct (check database)
[ ] CORS headers are present (check browser console)
[ ] Database is accessible (check logs)
```

### Users Get SSL Certificate Error
```
[ ] Certificate is valid
[ ] Domain matches certificate
[ ] Certificate not expired (check: date)
[ ] Proper redirect from HTTP → HTTPS
```

### Slow Response Times
```
[ ] Check server CPU usage
[ ] Check memory usage
[ ] Check database queries
[ ] Check network bandwidth
[ ] Consider upgrading hardware
```

### Database Issues
```
[ ] Database file exists and is readable
[ ] Database is not corrupted
[ ] Backup database regularly
[ ] Monitor database size growth
```

---

## Maintenance Tasks

### Daily
- Check server is running
- Review failed login attempts
- Verify uptime

### Weekly
- Backup database
- Check disk space
- Review error logs
- Monitor response times

### Monthly
- Update dependencies
- Review security settings
- Optimize database
- Plan scaling needs

---

## Distributing the Service

### What to Share with Users
✅ Panel URL  
✅ Their credentials  
✅ USER_GUIDE.md (how to use)  
✅ Contact for support  

### What NOT to Share
❌ Server IP addresses  
❌ Database passwords  
❌ Server credentials  
❌ Configuration files  
❌ Internal logs  

---

## Cost Estimation

### Server Options

**Budget:** $5-10/month
- Shared hosting or small VPS
- 1GB RAM, 1 CPU
- Up to 50 users

**Standard:** $10-25/month
- VPS (DigitalOcean, Linode, Vultr)
- 2GB RAM, 1-2 CPU
- Up to 200 users

**High Performance:** $25-100/month
- Dedicated VPS or managed hosting
- 4GB+ RAM, 2+ CPU
- 200+ users

**Enterprise:** $100+/month
- Cloud auto-scaling (AWS, GCP)
- Multi-region redundancy
- Unlimited users

### Domain Name
- **Free:** Use IP address (less professional)
- **Paid:** $8-12/year (recommended)

### SSL Certificate
- **Free:** Let's Encrypt (recommended)
- **Paid:** $10-100/year (if needed)

---

## Automating Operations

### Keep Bot Running 24/7 (PM2)
```bash
npm install -g pm2
pm2 start fernotest.py --name "angelferno"
pm2 startup
pm2 save
```

### Daily Backups
```bash
# Add to crontab (crontab -e)
0 2 * * * cp /path/to/wallets.db /backups/wallets.db.$(date +\%Y\%m\%d)
```

### Monitor Uptime
```bash
# Use services like UptimeRobot
# Configure to ping: https://yourdomain.com
```

---

## Support Flow

### User Reports Issue
1. Collect error details
2. Check server status
3. Review logs
4. Fix issue
5. Update user

### Most Common Issues
- Wrong credentials (remind them to check)
- Browser cache (clear cache)
- Network firewall (check with IT)
- Server maintenance (notify users in advance)

---

## Future Improvements

Consider adding:
- [ ] User account recovery via email
- [ ] 2FA (Two-Factor Authentication)
- [ ] Activity audit logs
- [ ] API for automation
- [ ] Mobile app
- [ ] Multi-language support
- [ ] Dark mode toggle
- [ ] Custom branding

---

## Summary

**Your Role:**
1. Set up server once
2. Give users: URL + credentials
3. Monitor panel 24/7
4. Handle support issues

**User's Role:**
1. Open browser
2. Go to URL
3. Enter credentials
4. Use panel
5. Done!

**Zero user configuration. Maximum ease of use.**

---

## Questions?

Refer to:
- `GLOBAL_ACCESS_SETUP.md` - Detailed setup
- `USER_GUIDE.md` - User instructions
- `fernotest.py` - Backend code
- `Web/angel-dreamers-login.html` - Frontend code

**Your panel is ready for global deployment!** 🚀
