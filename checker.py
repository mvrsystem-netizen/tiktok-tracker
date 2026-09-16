import requests
import json
from datetime import datetime
from supabase import create_client, Client

# Konfigurasi Supabase
SUPABASE_URL = "https://PROJECT_ID.supabase.co"
SUPABASE_KEY = "YOUR_SUPABASE_ANON_KEY"
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

TARGET_USER = "anggun"

def check_tiktok_status(username):
    # Metode sederhana: Cek status room live via URL Web TikTok
    url = f"https://www.tiktok.com/@{username}/live"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    
    try:
        response = requests.get(url, headers=headers, timeout=10)
        # Jika halaman mengandung player room live
        if '"liveRoom":' in response.text or 'room_id' in response.text:
            return {"status": "Host", "host_name": username}
        
        # Tambahan: Untuk cek status Tamu/Penonton di room lain, 
        # membutuhkan parser WebSocket / API room host lawan.
        return {"status": "Offline", "host_name": None}
    except Exception as e:
        print(f"Error checking: {e}")
        return {"status": "Offline", "host_name": None}

def log_to_supabase(data):
    payload = {
        "target_name": TARGET_USER,
        "role": data["status"],
        "host_name": data["host_name"],
        "timestamp": datetime.utcnow().isoformat()
    }
    supabase.table("tiktok_logs").insert(payload).execute()
    print(f"Log tersimpan: {payload}")

if __name__ == "__main__":
    result = check_tiktok_status(TARGET_USER)
    log_to_supabase(result)