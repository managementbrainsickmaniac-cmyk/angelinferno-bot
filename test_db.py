import sqlite3

conn = sqlite3.connect('wallets.db')
cursor = conn.cursor()
cursor.execute('PRAGMA table_info(credentials)')
columns = [col[1] for col in cursor.fetchall()]
print('Credentials table columns:', columns)
conn.close()