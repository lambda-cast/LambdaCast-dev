import sqlite3
import random
from datetime import datetime, timedelta

def seed_db(db_path, is_mock=True):
    print(f"Seeding {db_path}...")
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        
        # Check if table exists
        cur.execute("SELECT count(name) FROM sqlite_master WHERE type='table' AND name='real_time'")
        if cur.fetchone()[0] == 0:
            print("  Skipping (no real_time table)")
            return
            
        now = datetime.now()
        
        # We need exactly 1440 rows. Let's update them all to the last 24h
        for i in range(1440):
            # Going backward in time from now
            t = now - timedelta(minutes=(1439 - i))
            time_str = t.strftime("%H:%M")
            
            # Simple bell curve for solar production during the day (6 AM to 6 PM)
            hour = t.hour
            produced = 0.0
            fed_in = 0.0
            consumed = 1.0 # Base consumption
            
            if is_mock:
                if 6 <= hour <= 18:
                    # Peak at noon
                    peak = 5.0
                    distance_from_noon = abs(12 - hour)
                    produced = peak - (distance_from_noon * 0.8)
                    produced += random.uniform(-0.5, 0.5) # Add noise
                    produced = max(0.0, round(produced, 2))
                    
                    if produced > consumed:
                        fed_in = round(produced - consumed, 2)
            
            # Update the existing row by ID (1 to 1440)
            row_id = i + 1
            cur.execute(
                "UPDATE real_time SET time=?, produced=?, consumed=?, fed_in=? WHERE ID=?",
                (time_str, produced, consumed, fed_in, row_id)
            )
            
        conn.commit()
        conn.close()
        print(f"  Successfully seeded {db_path} with historical data.")
    except Exception as e:
        print(f"  Error: {e}")

if __name__ == "__main__":
    # Seed Dummy databases with realistic curves
    seed_db("/data/db_4.sqlite", is_mock=True)
    seed_db("/data/db_7.sqlite", is_mock=True)
    
    # Clean up Sfax database so it has proper timestamps instead of '...'
    seed_db("/data/db_2.sqlite", is_mock=False)
