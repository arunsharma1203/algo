import sys

with open('backend/app/api/ml_history.py', 'r') as f:
    content = f.read()

old_logic = """    # DEDUPLICATION LOGIC:
    # Prevent artificially inflating the Win Rate by saving the exact same trade 10 times a day
    cur = conn.execute(\"\"\"
        SELECT timestamp, confidence FROM ml_trade_history 
        WHERE ticker = ? AND trade_type = ? AND direction = ?
        ORDER BY id DESC LIMIT 1
    \"\"\", (ticker, trade_type, direction))
    
    last_trade = cur.fetchone()
    if last_trade:
        import pandas as pd
        last_time_str = last_trade[0]
        last_confidence = last_trade[1]
        try:
            last_time = datetime.fromisoformat(last_time_str)
        except:
            last_time = pd.to_datetime(last_time_str)
            
        # Only skip if it's the exact same day AND the EXACT SAME confidence score.
        # If the score changed, market conditions changed, so we log it as a new data point.
        if last_time.date() == now.date() and abs(last_confidence - float(confidence)) < 0.01:
            conn.close()
            return"""

content = content.replace(old_logic, "")

with open('backend/app/api/ml_history.py', 'w') as f:
    f.write(content)
