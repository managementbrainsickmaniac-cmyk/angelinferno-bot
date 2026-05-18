# 🤖 Angel Inferno Bot - Setup Guide for Multiple Devices

This guide explains how to set up the Angel Inferno bot on any device by configuring environment variables.

## 📋 Prerequisites

- Python 3.8 or higher
- pip (Python package manager)
- A Telegram Bot Token (get from [@BotFather](https://t.me/botfather))
- API keys for blockchain providers (free tier available)

## 🚀 Quick Setup

### 1. **Install Dependencies**

```bash
pip install -r requirements.txt
```

### 2. **Configure Environment Variables**

Copy the example configuration file and customize it:

```bash
cp .env.example .env
```

Edit `.env` with your own configuration:

```env
TELEGRAM_BOT_TOKEN=your_token_here
ALCHEMY_URL=your_alchemy_url_here
CRYPTOCOMPARE_API_KEY=your_api_key_here
# ... other configurations
```

### 3. **Run the Bot**

```bash
python fernotest.py
```

## 🔑 Getting API Keys

### Telegram Bot Token
1. Open Telegram and search for [@BotFather](https://t.me/botfather)
2. Create a new bot with `/newbot`
3. Copy the token provided
4. Paste it in `.env` as `TELEGRAM_BOT_TOKEN`

### Alchemy API Key (Ethereum)
1. Visit [alchemy.com](https://www.alchemy.com/)
2. Sign up for free account
3. Create a new app for Ethereum Mainnet
4. Copy your API key
5. Paste in `.env` as `ALCHEMY_URL`

### CryptoCompare API Key
1. Visit [cryptocompare.com](https://www.cryptocompare.com/)
2. Sign up for free account
3. Generate an API key
4. Paste in `.env` as `CRYPTOCOMPARE_API_KEY`

### NOWNodes API Key
1. Visit [nownodes.io](https://nownodes.io/)
2. Sign up for free account
3. Get your API key
4. Paste in `.env` as `NOWNODES_API_KEY`

## 💰 Wallet Configuration

Replace the default wallet addresses with your own:

```env
BTC_WALLET_ADDRESS=your_btc_address_here
ETH_WALLET_ADDRESS=your_eth_address_here
USDT_WALLET_ADDRESS=your_usdt_address_here
```

## 🔐 Security Best Practices

✅ **DO:**
- Keep `.env` file locally and never commit it
- Use strong, unique API keys
- Rotate credentials regularly
- Set proper file permissions on `.env`

❌ **DON'T:**
- Share API keys or bot tokens
- Commit `.env` to version control
- Use test/demo keys in production
- Store sensitive data in code

## 📦 Directory Structure

```
AngelInferno/
├── fernotest.py          # Main bot file
├── requirements.txt       # Python dependencies
├── .env                  # Your configuration (CREATE THIS)
├── .env.example          # Configuration template
├── .gitignore            # Git ignore rules
├── wallets/              # User wallet storage
│   └── user_*.txt        # Wallet files
└── Web/                  # Web dashboard files
```

## 🐛 Troubleshooting

### Bot not starting?
- Check if `TELEGRAM_BOT_TOKEN` is correct
- Verify Python version (3.8+)
- Install all dependencies: `pip install -r requirements.txt`

### Connection errors?
- Verify `ALCHEMY_URL` is correct
- Check internet connection
- Ensure API keys haven't expired

### Port already in use?
- Change `DATABASE_PATH` in `.env` to a different location
- Or kill process using port 8080

## 🌍 Deploying to Multiple Devices

### Device 1 (Your PC)
```bash
# Copy current .env
cp .env ~/.backup_env
```

### Device 2 (Server/Cloud)
```bash
# Create .env with your device's configuration
# Use the .env.example as template
nano .env  # Edit with your values
python fernotest.py
```

### Device 3 (Another PC)
```bash
# Same process as Device 2
# Each device needs its own .env file
```

## ✅ Verification

Test if the bot is working:

```python
# Simple test
python -c "import fernotest; print('✅ Bot imports successfully')"
```

## 📞 Support

If you encounter issues:
1. Check that all required environment variables are set
2. Verify API keys are valid and not expired
3. Ensure wallet addresses are in correct format
4. Check logs for specific error messages

---

**Note:** The bot now works on any device as long as proper `.env` configuration is provided. No hard-coded values in the source code!
