# Network Access Guide - AngelFerno Panel

## Quick Start

### For Server Admin (Machine Running the Bot)

1. **Get your server's IP address:**
   ```powershell
   ipconfig | findstr IPv4
   ```
   Look for the IPv4 Address (e.g., `192.168.1.100`)

2. **Share this URL with users:**
   ```
   http://<YOUR-IP>:8080
   ```
   Example: `http://192.168.1.100:8080`

3. **Ensure firewall allows port 8080:**
   ```powershell
   netsh advfirewall firewall add rule name="AngelFerno Panel" dir=in action=allow protocol=tcp localport=8080
   ```

---

## For End Users (Accessing from Another Machine)

### Step 1: Get the Server URL
Ask your admin for the URL, typically:
```
http://<server-ip>:8080
```

### Step 2: Open in Browser
1. Open your web browser (Chrome, Firefox, Edge, Safari)
2. Type the URL in the address bar
3. Press Enter

### Step 3: Login
1. Enter your **Panel ID** (provided by admin via Telegram)
2. Enter your **Password** (provided by admin via Telegram)
3. Click **🔐 Access Panel**

### Step 4: Use the Dashboard
Once logged in, you'll see:
- **Dashboard**: View stats and metrics
- **Configurations**: Manage your settings
- **Settings**: Adjust preferences
- **Documentation**: Quick help guide
- **Activity**: View recent logs

---

## Connection Methods

### Method 1: IP Address (Most Common)
```
http://192.168.1.100:8080
```
- Works if you can ping the server
- Requires knowing the server's IP

### Method 2: Hostname (If Configured)
```
http://mycomputer:8080
```
- More user-friendly if hostname is set up
- Requires DNS configuration

### Method 3: DNS Name
```
http://angel-inferno.local:8080
```
- If your admin set up a DNS name
- Most user-friendly option

---

## Troubleshooting

### "Connection Error" or "Cannot Connect"

**Check 1: Server is Running**
- Ask your admin to verify the bot is running
- The server should print: `Started aiohttp web server`

**Check 2: Firewall**
- Windows Firewall might be blocking port 8080
- Admin can allow it with:
  ```powershell
  netsh advfirewall firewall add rule name="AngelFerno Panel" dir=in action=allow protocol=tcp localport=8080
  ```

**Check 3: Network Connection**
- Ensure both machines are on the same network
- Try pinging the server:
  ```
  ping 192.168.1.100
  ```

**Check 4: Wrong URL**
- Make sure you're using `http://` not `https://`
- Make sure port is `:8080`
- Example: `http://192.168.1.100:8080` ✅
- Not: `https://192.168.1.100` ❌

### "Invalid Credentials"

- Double-check Panel ID and Password
- Make sure CAPS LOCK is not on
- Ask your admin to regenerate credentials

### "The site's security certificate is not trusted"

This is normal for local network access. You can:
1. Click "Advanced" or "Details"
2. Click "Proceed anyway" or "Continue"

---

## Features After Login

### Dashboard
- Real-time statistics
- Hit price tracking
- Success rate monitoring
- Live status indicators

### Configurations
- Create custom configurations
- Edit existing settings
- Download compiled scripts
- Tag and organize configs

### Settings
- Security preferences
- Session management
- Interface options
- Auto-refresh controls

### Documentation
- Getting started guide
- Best practices
- Feature descriptions

### Activity Log
- Recent login history
- Configuration changes
- System events

---

## Security Tips

✅ **Do:**
- Keep your Panel ID and Password secret
- Sign out after using the panel
- Use a strong password
- Don't share your credentials

❌ **Don't:**
- Share your login details with others
- Leave the panel open on shared computers
- Use the same password as other accounts
- Write credentials on physical notes

---

## Accessing on Mobile Devices

### iPhone/iPad
1. Open Safari
2. Type the URL: `http://192.168.1.100:8080`
3. Tap "Open"

### Android
1. Open Chrome or Firefox
2. Type the URL: `http://192.168.1.100:8080`
3. Tap Go

**Note:** Mobile view is responsive and will adapt to your screen size.

---

## Common Questions

**Q: Why do I see "Not Secure" in the browser?**
A: The panel uses HTTP for local network access. This is secure for local-only connections.

**Q: Can I access from outside my network?**
A: By default, no. The server only listens on your local network. For external access, your admin would need to set up port forwarding or a VPN.

**Q: What if I forget my password?**
A: Contact your admin to regenerate new credentials for you.

**Q: Does the panel work on mobile?**
A: Yes! The interface is fully responsive and works great on phones and tablets.

**Q: Can multiple users login at the same time?**
A: Yes! The panel supports concurrent sessions.

---

## What's New in This Update

✨ **Improvements:**
1. **Network Accessible**: Works from any device on your network
2. **Modern Design**: Glassmorphic UI with smooth animations
3. **Non-Clickable Branding**: Logo stays in place (doesn't redirect)
4. **Better Performance**: Optimized animations and transitions
5. **Responsive**: Works on desktop, tablet, and mobile

---

## Support

If you encounter issues:
1. Check the troubleshooting section above
2. Verify your connection with `ping`
3. Ask your admin to check the server logs
4. Ensure your Panel ID and Password are correct

---

**Version:** 2.0  
**Last Updated:** May 2026  
**Status:** ✅ Network Access Fully Operational
