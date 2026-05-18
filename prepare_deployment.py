"""
QUICK DEPLOYMENT SCRIPT
Run this script to automatically prepare your bot for Render deployment
"""

import os
import shutil
from pathlib import Path

def main():
    print("=" * 70)
    print("🚀 ANGELINFERNO BOT - QUICK DEPLOYMENT SETUP")
    print("=" * 70)
    
    project_root = Path(__file__).parent
    
    # Step 1: Check required files
    print("\n[1] Checking Required Files...")
    required_files = {
        'fernotest.py': 'Bot script',
        'requirements.txt': 'Python dependencies',
        'Procfile': 'Render deployment config',
        '.env.template': 'Environment template',
        'Web/angelferno_merged.html': 'Website'
    }
    
    for file, desc in required_files.items():
        file_path = project_root / file
        if file_path.exists():
            print(f"   ✅ {desc}: {file}")
        else:
            print(f"   ❌ MISSING: {desc}: {file}")
    
    # Step 2: Create .env from template if not exists
    print("\n[2] Setting Up Environment Variables...")
    env_file = project_root / '.env'
    env_template = project_root / '.env.template'
    
    if not env_file.exists() and env_template.exists():
        print("   📋 Creating .env from template...")
        shutil.copy(env_template, env_file)
        print("   ✅ .env created (edit with your credentials)")
    elif env_file.exists():
        print("   ✅ .env already exists")
    else:
        print("   ⚠️  Neither .env nor .env.template found")
    
    # Step 3: Check Git setup
    print("\n[3] Git Repository Check...")
    git_dir = project_root / '.git'
    if git_dir.exists():
        print("   ✅ Git repository already initialized")
    else:
        print("   ⚠️  Git not initialized yet")
        print("   Run: git init")
    
    # Step 4: Check requirements.txt
    print("\n[4] Checking Python Dependencies...")
    req_file = project_root / 'requirements.txt'
    if req_file.exists():
        with open(req_file) as f:
            deps = f.read().strip().split('\n')
            print(f"   ✅ Found {len(deps)} dependencies")
            critical = ['flask', 'python-telegram-bot', 'python-dotenv']
            for dep in critical:
                found = any(dep in d for d in deps)
                print(f"      {'✅' if found else '❌'} {dep}")
    
    # Step 5: Summary
    print("\n" + "=" * 70)
    print("📋 NEXT STEPS:")
    print("=" * 70)
    print("""
1. ✏️  EDIT YOUR CREDENTIALS:
   - Open .env file
   - Add your TELEGRAM_BOT_TOKEN and other credentials
   - DO NOT commit .env to GitHub

2. 📤 PUSH TO GITHUB:
   git add .
   git commit -m "Initial commit: AngelFerno bot"
   git push -u origin main

3. 🚀 DEPLOY TO RENDER:
   - Go to render.com
   - Connect GitHub account
   - Create new Web Service
   - Select your repository
   - Set environment variables from .env
   - Click "Create Web Service"

4. 🧪 TEST WEBHOOK:
   python test_webhook.py

5. ✅ VERIFY DEPLOYMENT:
   - Message your bot on Telegram
   - Visit https://angelinferno-bot.onrender.com/health
   - Check Render logs

📚 FULL GUIDE:
   See GITHUB_DEPLOYMENT_GUIDE.md for complete instructions

""")
    print("=" * 70)
    print("✅ Setup preparation complete!")
    print("=" * 70)

if __name__ == '__main__':
    main()
