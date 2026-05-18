"""
Webhook Testing Script
Run this to verify your bot's webhook setup on Render
"""

import requests
import json
import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
WEBHOOK_URL = os.getenv('TELEGRAM_WEBHOOK_URL', 'https://angelinferno-bot.onrender.com/webhook')
HEALTH_CHECK_URL = f"{WEBHOOK_URL.replace('/webhook', '')}/health"

print("=" * 60)
print("🔧 WEBHOOK TESTING SCRIPT")
print("=" * 60)

# Test 1: Check Bot Token
print("\n[1] Validating Bot Token...")
if not BOT_TOKEN:
    print("❌ ERROR: TELEGRAM_BOT_TOKEN not found in .env")
    exit(1)
print(f"✅ Bot token loaded: {BOT_TOKEN[:20]}...")

# Test 2: Health Check
print("\n[2] Checking Render Health Endpoint...")
try:
    response = requests.get(HEALTH_CHECK_URL, timeout=5)
    if response.status_code == 200:
        print(f"✅ Health Check PASSED")
        print(f"   Response: {response.json()}")
    else:
        print(f"❌ Health Check FAILED (HTTP {response.status_code})")
except requests.exceptions.ConnectionError:
    print(f"❌ Cannot connect to {HEALTH_CHECK_URL}")
    print("   Render service may still be starting. Wait 2-3 minutes and try again.")
except Exception as e:
    print(f"❌ Error: {e}")

# Test 3: Get Webhook Info
print("\n[3] Getting Current Webhook Info...")
try:
    api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/getWebhookInfo"
    response = requests.get(api_url, timeout=10)
    webhook_info = response.json()
    
    if webhook_info.get('ok'):
        result = webhook_info.get('result', {})
        print(f"✅ Webhook Info Retrieved:")
        print(f"   URL: {result.get('url', 'Not set')}")
        print(f"   Pending Updates: {result.get('pending_update_count', 0)}")
        print(f"   Has Custom Certificate: {result.get('has_custom_certificate', False)}")
        if result.get('last_error_date'):
            print(f"   ⚠️  Last Error: {result.get('last_error_message', 'Unknown')}")
    else:
        print(f"❌ Failed: {webhook_info.get('description', 'Unknown error')}")
except Exception as e:
    print(f"❌ Error: {e}")

# Test 4: Set Webhook (if needed)
print("\n[4] Setting Webhook to Render...")
try:
    api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook"
    payload = {
        'url': WEBHOOK_URL,
        'drop_pending_updates': True
    }
    response = requests.post(api_url, json=payload, timeout=10)
    result = response.json()
    
    if result.get('ok'):
        print(f"✅ Webhook SET Successfully!")
        print(f"   URL: {WEBHOOK_URL}")
    else:
        print(f"❌ Failed: {result.get('description', 'Unknown error')}")
except Exception as e:
    print(f"❌ Error: {e}")

# Test 5: Get Bot Info
print("\n[5] Getting Bot Info...")
try:
    api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/getMe"
    response = requests.get(api_url, timeout=10)
    bot_info = response.json()
    
    if bot_info.get('ok'):
        result = bot_info.get('result', {})
        print(f"✅ Bot is Active:")
        print(f"   Username: @{result.get('username', 'unknown')}")
        print(f"   Name: {result.get('first_name', 'unknown')}")
        print(f"   ID: {result.get('id', 'unknown')}")
    else:
        print(f"❌ Failed: {bot_info.get('description', 'Unknown error')}")
except Exception as e:
    print(f"❌ Error: {e}")

print("\n" + "=" * 60)
print("✅ Testing Complete!")
print("=" * 60)
print("\nNext Steps:")
print("1. Go to Telegram and send a message to your bot")
print("2. Check Render logs: Dashboard → Logs")
print("3. Verify the message arrives in your bot")
print("\nIf webhook failed, wait 2-3 minutes for Render to fully start up.")
print("=" * 60)
