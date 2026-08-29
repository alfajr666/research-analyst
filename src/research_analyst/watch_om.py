import time
import subprocess
import datetime
import config

print("=== FIXED WATCH START", datetime.datetime.now(), "===")

for i in range(1, 13):  # short 2min re-watch for demo
    time.sleep(10)
    print(f"=== SAMPLE {i} at {datetime.datetime.now()} ===")
    print("--- orchestrator-out tail ---")
    try:
        with open("logs/orchestrator-out.log") as f:
            lines = f.readlines()[-8:]
            print(''.join(lines))
    except:
        print("no log")
    print("--- pm2 ---")
    subprocess.run(["pm2", "list"], capture_output=True)
    print("--- openmarket in log ---")
    try:
        res = subprocess.run(["grep", "-i", "openmarket", "logs/orchestrator-out.log"], capture_output=True, text=True)
        print(res.stdout[-500:] if res.stdout else "none")
    except:
        print("none")
    print("--- recent openmarket request_log ---")
    try:
        conn = config.get_db_connection(read_only=True)
        rows = conn.execute(
            "SELECT source, request_type, status, cutoff_id FROM source_request_log WHERE source = ? ORDER BY requested_at DESC LIMIT 3",
            ("openmarket",)
        ).fetchall()
        print(rows)
        conn.close()
    except Exception as e:
        print("db err:", str(e)[:100])
print("=== WATCH END ===")
