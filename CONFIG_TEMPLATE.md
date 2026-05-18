# ⚙️ AngelFerno - Configuration Template

Use this template to set up AngelFerno for your specific domain/IP.

---

## Step 1: Update Frontend Configuration

### File: `Web/angel-dreamers-login.html`

Find this function (around line 1455):
```javascript
function getServerBaseUrl() {
    return window.location.origin;
}
```

### Replace With Your Configuration:

**Option A: HTTPS Domain (Recommended for Production)**
```javascript
function getServerBaseUrl() {
    return 'https://yourdomain.com';
}
```
Example:
```javascript
function getServerBaseUrl() {
    return 'https://angelferno.com';
}
```

**Option B: HTTP Static IP**
```javascript
function getServerBaseUrl() {
    return 'http://203.0.113.42:8080';
}
```

**Option C: HTTPS Static IP**
```javascript
function getServerBaseUrl() {
    return 'https://203.0.113.42:8080';
}
```

**Option D: Development (Keep As Is)**
```javascript
function getServerBaseUrl() {
    return window.location.origin;
}
```

---

## Step 2: Backend Configuration

### File: `fernotest.py`

#### Current Setup (HTTP on port 8080):
No changes needed. Bot runs on:
```
http://0.0.0.0:8080
```

#### For HTTPS with Reverse Proxy:
Keep port 8080, use Nginx/Apache in front

#### For Direct HTTPS:
Around line 2236, replace:
```python
web.run_app(app, host='0.0.0.0', port=8080)
```

With:
```python
import ssl

ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ssl_context.load_cert_chain(
    certfile='/etc/letsencrypt/live/yourdomain.com/fullchain.pem',
    keyfile='/etc/letsencrypt/live/yourdomain.com/privkey.pem'
)

web.run_app(app, host='0.0.0.0', port=443, ssl_context=ssl_context)
```

---

## Step 3: Nginx Reverse Proxy (Optional)

If using Nginx to handle HTTPS and forward to port 8080:

### File: `/etc/nginx/sites-available/angelferno`

```nginx
server {
    listen 443 ssl http2;
    server_name yourdomain.com;

    ssl_certificate /etc/letsencrypt/live/yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/yourdomain.com/privkey.pem;
    
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;
    ssl_prefer_server_ciphers on;

    location / {
        proxy_pass http://localhost:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;
        proxy_request_buffering off;
    }

    # WebSocket support (if needed)
    location /ws {
        proxy_pass http://localhost:8080/ws;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}

# Redirect HTTP to HTTPS
server {
    listen 80;
    server_name yourdomain.com;
    return 301 https://$server_name$request_uri;
}
```

Enable the site:
```bash
sudo ln -s /etc/nginx/sites-available/angelferno /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl restart nginx
```

---

## Step 4: DNS Configuration

### If Using Domain Name:

Go to your domain registrar (GoDaddy, Namecheap, etc.) and set:

**A Record:**
```
Name: @ (or subdomain like "panel")
Type: A
Value: your-server-ip
TTL: 3600
```

Or **CNAME Record** if using subdomain:
```
Name: panel
Type: CNAME
Value: yourdomain.com
TTL: 3600
```

Wait 24 hours for DNS to propagate.

Verify with:
```bash
nslookup yourdomain.com
```

---

## Step 5: SSL Certificate (Let's Encrypt - Free)

### Install Certbot:
```bash
apt-get update
apt-get install certbot python3-certbot-nginx
```

### Generate Certificate:
```bash
sudo certbot certonly --nginx -d yourdomain.com
```

Or for multiple domains:
```bash
sudo certbot certonly --nginx -d yourdomain.com -d www.yourdomain.com
```

Certificate files location:
```
/etc/letsencrypt/live/yourdomain.com/fullchain.pem
/etc/letsencrypt/live/yourdomain.com/privkey.pem
```

### Auto-Renewal:
```bash
sudo systemctl enable certbot.timer
sudo systemctl start certbot.timer
```

---

## Step 6: Firewall Configuration

### Allow Ports:
```bash
# HTTP (redirect to HTTPS)
sudo ufw allow 80/tcp

# HTTPS
sudo ufw allow 443/tcp

# SSH (for management)
sudo ufw allow 22/tcp

# Internal (if needed)
sudo ufw allow from 192.168.0.0/16 to any port 8080

# Enable firewall
sudo ufw enable
```

### Close Port 8080 from Outside:
```bash
sudo ufw deny from any to any port 8080
sudo ufw allow from 127.0.0.1 to any port 8080
```

---

## Step 7: Start Services

### Start Bot:
```bash
cd /path/to/AngelFerno
python fernotest.py &
```

### Or Use PM2 (Recommended):
```bash
npm install -g pm2
pm2 start fernotest.py --name "angelferno"
pm2 startup
pm2 save
```

### Verify Running:
```bash
curl http://localhost:8080
# Should return HTML content
```

---

## Step 8: Test Access

### Local Test:
```bash
curl https://yourdomain.com
```

### Browser Test:
Go to: `https://yourdomain.com`
Should see login page ✅

### From Another Network:
Use your phone's 4G/5G (not WiFi)
Go to: `https://yourdomain.com`
Should work ✅

---

## Configuration Checklist

- [ ] Updated `getServerBaseUrl()` in HTML
- [ ] Backend running on correct port (8080 or 443)
- [ ] Domain/IP configured
- [ ] DNS records set (if using domain)
- [ ] SSL certificate installed (if using HTTPS)
- [ ] Nginx/Reverse proxy configured (if needed)
- [ ] Firewall rules set
- [ ] Port 8080/443 open
- [ ] Bot service running
- [ ] Tested from outside network
- [ ] Backups configured
- [ ] Monitoring enabled

---

## Example Configurations

### Config 1: Simple (No Domain)
```javascript
// fernotest.py lines 2236:
web.run_app(app, host='0.0.0.0', port=8080)

// HTML line 1459:
return 'http://203.0.113.42:8080';
```
Users access: `http://203.0.113.42:8080`

### Config 2: With Domain (Nginx + Let's Encrypt)
```javascript
// HTML line 1459:
return 'https://yourdomain.com';

// fernotest.py lines 2236:
web.run_app(app, host='0.0.0.0', port=8080)

// Nginx handles HTTPS
```
Users access: `https://yourdomain.com`

### Config 3: Direct HTTPS
```javascript
// HTML line 1459:
return 'https://yourdomain.com:443';

// fernotest.py:
web.run_app(app, host='0.0.0.0', port=443, ssl_context=ssl_context)
```
Users access: `https://yourdomain.com`

---

## Troubleshooting Configuration

### "Invalid configuration" error
- Check Python syntax: `python -m py_compile fernotest.py`
- Check HTTPS URL format (must be https://)
- Check port number is correct

### "Connection refused"
- Check bot is running: `ps aux | grep fernotest`
- Check port is open: `netstat -tlnp | grep 8080`
- Check firewall: `sudo ufw status`

### "Certificate error"
- Check certificate path: `ls /etc/letsencrypt/live/yourdomain.com/`
- Check certificate is valid: `openssl x509 -in /path/to/cert.pem -noout -dates`
- Check domain matches certificate

---

## Production Readiness Checklist

- [ ] HTTPS enabled (not HTTP)
- [ ] Certificate valid and not expired
- [ ] All ports properly configured
- [ ] Firewall restricts unnecessary ports
- [ ] Backups running daily
- [ ] Monitoring alerts configured
- [ ] Rate limiting enabled
- [ ] Logging enabled
- [ ] Database backups tested
- [ ] Tested from outside network
- [ ] Documentation updated
- [ ] Support process defined

---

**Your system is now configured for global access!** 🚀

Users only need URL + credentials. No configuration on their end.
