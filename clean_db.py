import sqlite3
import pandas as pd
from datetime import datetime

conn = sqlite3.connect('backend/market_data.db')
cur = conn.cursor()

# Get all trades
cur.execute("SELECT id, timestamp, ticker, direction, trade_type FROM ml_trade_history ORDER BY id ASC")
rows = cur.fetchall()

seen = set()
to_delete = []

for row in rows:
    row_id, timestamp_str, ticker, direction, trade_type = row
    
    try:
        dt = datetime.fromisoformat(timestamp_str)
    except:
        dt = pd.to_datetime(timestamp_str)
        
    date_str = dt.date().isoformat()
    
    # Unique signature for a trade on a given day
    sig = (date_str, ticker, direction, trade_type)
    
    if sig in seen:
        to_delete.append(row_id)
    else:
        seen.add(sig)

print(f"Found {len(to_delete)} duplicate trades to delete.")

# Delete them
for row_id in to_delete:
    cur.execute("DELETE FROM ml_trade_history WHERE id = ?", (row_id,))

conn.commit()
conn.close()
print("Database cleaned successfully!")
