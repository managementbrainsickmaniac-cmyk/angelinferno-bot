import sqlite3
import json

# Connect to the database
conn = sqlite3.connect('wallets.db')
cursor = conn.cursor()

# Query the credentials
cursor.execute("SELECT panel_id, password_hash FROM credentials")
result = cursor.fetchone()

if result:
    panel_id, password_hash = result
    credentials = {
        "panel_id": panel_id,
        "password_hash": password_hash
    }
    # Write to credentials.json in Web folder
    with open('Web/credentials.json', 'w') as f:
        json.dump(credentials, f, indent=2)
    print(f"Credentials exported to Web/credentials.json")
    print(f"Panel ID: {panel_id}")
    print(f"Password Hash: {password_hash}")
else:
    print("No credentials found in database.")

conn.close()