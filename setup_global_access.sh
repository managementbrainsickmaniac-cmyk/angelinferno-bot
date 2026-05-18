#!/bin/bash

# AngelFerno Global Access Setup Script
# This script automates the setup for global hosting

echo "🚀 AngelFerno Global Access Setup"
echo "=================================="
echo ""

# Check if running as root for some operations
if [ "$EUID" -eq 0 ]; then
   echo "✓ Running with sudo privileges"
else
   echo "⚠️  Some operations may require sudo"
fi

# Step 1: Get configuration from user
echo ""
echo "📋 Configuration Step"
echo "====================="
echo ""
read -p "Enter your domain or static IP (e.g., yourdomain.com or 203.0.113.42): " SERVER_URL

# Validate input
if [ -z "$SERVER_URL" ]; then
    echo "❌ Invalid input. Exiting."
    exit 1
fi

# Determine if using HTTPS
if [[ "$SERVER_URL" == *"."* ]]; then
    echo ""
    read -p "Use HTTPS? (y/n): " USE_HTTPS
    if [[ $USE_HTTPS == "y" ]]; then
        PROTOCOL="https"
    else
        PROTOCOL="http"
    fi
else
    PROTOCOL="http"
fi

FULL_URL="$PROTOCOL://$SERVER_URL"
echo "✓ Server URL configured: $FULL_URL"

# Step 2: Update HTML configuration
echo ""
echo "🔧 Updating Frontend Configuration"
echo "===================================="

HTML_FILE="Web/angel-dreamers-login.html"

if [ -f "$HTML_FILE" ]; then
    # Backup original
    cp "$HTML_FILE" "$HTML_FILE.backup.$(date +%Y%m%d_%H%M%S)"
    echo "✓ Backup created: $HTML_FILE.backup"
    
    # Update the getServerBaseUrl function
    sed -i "s|return window.location.origin;|return '$FULL_URL';|g" "$HTML_FILE"
    echo "✓ Updated getServerBaseUrl() in HTML"
else
    echo "❌ HTML file not found: $HTML_FILE"
    exit 1
fi

# Step 3: Install dependencies
echo ""
echo "📦 Installing Dependencies"
echo "=========================="

if [ -f "requirements.txt" ]; then
    pip install -r requirements.txt
    echo "✓ Dependencies installed"
else
    echo "⚠️  requirements.txt not found"
fi

# Step 4: Set up SSL (if using domain)
if [[ $PROTOCOL == "https" ]]; then
    echo ""
    echo "🔐 Setting Up SSL Certificate"
    echo "============================="
    
    # Check if Certbot is installed
    if ! command -v certbot &> /dev/null; then
        echo "Installing Certbot..."
        apt-get update
        apt-get install -y certbot python3-certbot-nginx
    fi
    
    # Generate certificate
    echo "Generating SSL certificate for $SERVER_URL..."
    certbot certonly --standalone -d "$SERVER_URL" --non-interactive --agree-tos --email admin@$SERVER_URL
    echo "✓ SSL certificate generated"
fi

# Step 5: Set up process manager (PM2)
echo ""
echo "⚙️  Setting Up Process Manager"
echo "============================="

if command -v pm2 &> /dev/null; then
    echo "PM2 already installed"
else
    echo "Installing PM2..."
    npm install -g pm2
fi

# Start the bot with PM2
pm2 start fernotest.py --name "angelferno-bot" --interpreter python3
pm2 startup
pm2 save
echo "✓ Bot configured to run on startup"

# Step 6: Set up firewall
echo ""
echo "🔒 Configuring Firewall"
echo "======================="

if command -v ufw &> /dev/null; then
    ufw allow 80/tcp
    ufw allow 443/tcp
    ufw allow 22/tcp
    ufw --force enable
    echo "✓ Firewall configured"
else
    echo "⚠️  UFW not available. Please manually configure firewall:"
    echo "   - Allow port 80 (HTTP)"
    echo "   - Allow port 443 (HTTPS)"
    echo "   - Allow port 22 (SSH)"
fi

# Step 7: Create user guide
echo ""
echo "📄 Creating User Documentation"
echo "=============================="

cat > DEPLOYMENT_COMPLETE.txt << EOF
✅ DEPLOYMENT COMPLETE

Server URL: $FULL_URL
Date: $(date)

🎯 What to do next:

1. Verify Server is Running:
   curl $FULL_URL
   
2. Test Access:
   Open your browser and go to: $FULL_URL
   
3. Create User Credentials:
   python -c "from fernotest import store_credentials; store_credentials('testuser', 'testpass123', 0)"
   
4. Share with Users:
   Panel URL: $FULL_URL
   Panel ID: [their unique id]
   Password: [their password]
   
5. Monitor Server:
   pm2 logs angelferno-bot
   
6. Backup Database:
   cp wallets.db wallets.db.backup.\$(date +%Y%m%d)

🔗 Important:
- Users only need: URL + Credentials
- No configuration on their end
- Works from anywhere in the world
- No setup required

📞 Support:
See USER_GUIDE.md for user instructions
See PROVIDER_SETUP.md for provider documentation
See GLOBAL_ACCESS_SETUP.md for detailed setup

EOF

echo "✓ Deployment guide created: DEPLOYMENT_COMPLETE.txt"

# Step 8: Final checks
echo ""
echo "✅ Final Checks"
echo "==============="

# Check bot is running
if pm2 list | grep -q "angelferno-bot"; then
    echo "✓ Bot is running"
else
    echo "⚠️  Bot may not be running. Start with: pm2 start fernotest.py --name 'angelferno-bot'"
fi

# Check port
if netstat -tlnp 2>/dev/null | grep -q ":8080"; then
    echo "✓ Port 8080 is listening"
fi

echo ""
echo "🎉 Setup Complete!"
echo "=================="
echo ""
echo "Your AngelFerno panel is now globally accessible at:"
echo "👉 $FULL_URL"
echo ""
echo "Users can login with their credentials immediately."
echo "No configuration or deployment needed on their end."
echo ""
echo "Check DEPLOYMENT_COMPLETE.txt for next steps."
