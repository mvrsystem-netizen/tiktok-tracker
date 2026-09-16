#!/usr/bin/env python3
"""
TikTok Activity Tracker - Background Checker
Production-ready background worker for GitHub Actions.
Checks host live rooms and tracks target user statuses (OFFLINE, LIVE_HOST, TAMU, PENONTON)
then stores structured logs to Supabase and broadcasts free push notifications.
"""

import os
import sys
import json
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple, Set
import requests
from supabase import create_client, Client

# Konfigurasi Logging Ringkas & Bersih
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("TikTokChecker")

# Kredensial Environment dari GitHub Secrets atau Default
SUPABASE_URL = os.getenv("SUPABASE_URL", "https://kxdiqgjeqjrlvxwscavy.supabase.co")
SUPABASE_KEY = os.getenv(
    "SUPABASE_KEY",
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Imt4ZGlxZ2plcWpybHZ4d3NjYXZ5Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODk1NzUwMzIsImV4cCI6MjEwNTE1MTAzMn0.DeYH7w_fx9XQIpTx5IJccJBFKPOmlccloWbscpuD1eU"
)
NTFY_TOPIC = os.getenv("NTFY_TOPIC") # Layanan notifikasi push mobile 100% gratis via ntfy.sh
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

if not SUPABASE_URL or not SUPABASE_KEY:
    logger.error("Error: SUPABASE_URL dan SUPABASE_KEY harus disetel di Environment Variables / GitHub Secrets.")
    sys.exit(1)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
}


def send_free_push_notification(title: str, message: str, tags: Optional[List[str]] = None) -> None:
    """Mengirim push notification gratis ke HP pengguna via ntfy.sh dan/atau Telegram."""
    # 1. Notifikasi ntfy.sh (Aplikasi gratis di Play Store tanpa perlu login/bayar)
    if NTFY_TOPIC:
        try:
            url = f"https://ntfy.sh/{NTFY_TOPIC}"
            headers = {
                "Title": title.encode("utf-8"),
                "Priority": "high",
                "Tags": ",".join(tags or ["rotating_light", "tiktok"]),
            }
            requests.post(url, data=message.encode("utf-8"), headers=headers, timeout=5)
            logger.info("Notifikasi ntfy.sh berhasil terkirim.")
        except Exception as e:
            logger.warning("Gagal mengirim ntfy: %s", str(e))

    # 2. Notifikasi Telegram Bot (Gratis tanpa batas)
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            tg_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            payload = {
                "chat_id": TELEGRAM_CHAT_ID,
                "text": f"*{title}*\n{message}",
                "parse_mode": "Markdown"
            }
            requests.post(tg_url, json=payload, timeout=5)
            logger.info("Notifikasi Telegram berhasil terkirim.")
        except Exception as e:
            logger.warning("Gagal mengirim Telegram: %s", str(e))


def get_live_room_data(username: str) -> Optional[Dict]:
    """
    Memeriksa status live room host di TikTok melalui endpoint API webcast publik.
    Mengembalikan dictionary data room jika sedang live aktif (status == 2), atau None jika offline.
    """
    clean_username = username.strip().lstrip("@").lower()
    # Parameter sourceType=54 dan aid=1988 wajib ada agar TikTok tidak mengembalikan params_error
    endpoint = f"https://www.tiktok.com/api-live/user/room/?aid=1988&sourceType=54&uniqueId={clean_username}"

    try:
        response = requests.get(endpoint, headers=HTTP_HEADERS, timeout=10)
        if response.status_code == 200:
            res_json = response.json()
            data = res_json.get("data") or {}
            live_room = data.get("liveRoom") or {}
            status = live_room.get("status")
            
            # Status 2 menandakan room sedang LIVE aktif
            if status == 2:
                return live_room
            # Status 4 atau lainnya menandakan offline / siaran telah selesai
            if status == 4:
                return None
    except Exception as exc:
        logger.debug("Fetch live error untuk @%s: %s", clean_username, str(exc))

    # Fallback: Periksa via HTML page live
    try:
        web_url = f"https://www.tiktok.com/@{clean_username}/live"
        web_res = requests.get(web_url, headers=HTTP_HEADERS, timeout=8, allow_redirects=True)
        if web_res.status_code == 200:
            # HANYA anggap live jika benar-benar ada tanda live aktif (status 2).
            # JANGAN cek string 'liveRoom' karena teks tersebut selalu ada di semua halaman profil TikTok.
            if '"status":2' in web_res.text:
                return {"live": True, "owner": clean_username}
    except Exception:
        pass

    return None


def fetch_room_participants(room_id: str, anchor_id: str) -> Tuple[Set[str], Set[str]]:
    """
    Mengambil daftar co-host/guest (TAMU) dan penonton (PENONTON) di dalam live room.
    Mengembalikan (set_tamu, set_penonton).
    """
    guests: Set[str] = set()
    viewers: Set[str] = set()

    if not room_id:
        return guests, viewers

    # 1. Cek Komal / Link-Mic / Co-Host
    try:
        cohost_url = f"https://webcast.tiktok.com/webcast/linkmic/user_list/?room_id={room_id}&aid=1988"
        res = requests.get(cohost_url, headers=HTTP_HEADERS, timeout=5)
        if res.status_code == 200:
            data = res.json().get("data", {})
            user_list = data.get("users", []) or data.get("linkmic_users", [])
            for u in user_list:
                uname = u.get("display_id") or u.get("unique_id") or u.get("nickname")
                if uname:
                    guests.add(str(uname).lower().strip().lstrip("@"))
    except Exception:
        pass

    # 2. Cek Penonton / Audience Rank List
    try:
        rank_url = f"https://webcast.tiktok.com/webcast/ranklist/audience/?room_id={room_id}&anchor_id={anchor_id}&aid=1988"
        res = requests.get(rank_url, headers=HTTP_HEADERS, timeout=5)
        if res.status_code == 200:
            ranks = res.json().get("data", {}).get("ranks", [])
            for r in ranks:
                user = r.get("user", {})
                uname = user.get("display_id") or user.get("unique_id")
                if uname:
                    viewers.add(str(uname).lower().strip().lstrip("@"))
    except Exception:
        pass

    return guests, viewers


def fetch_targets_and_hosts() -> Tuple[List[Dict], List[Dict]]:
    """Mengambil daftar targets dan hosts dari database Supabase."""
    targets_res = supabase.table("targets").select("id, username, display_name").execute()
    hosts_res = supabase.table("hosts").select("id, username, display_name").execute()

    targets = targets_res.data or []
    hosts = hosts_res.data or []
    return targets, hosts


def get_latest_logged_status(target_username: str) -> Optional[Dict]:
    """Mengambil log terakhir dari target untuk membandingkan perubahan status."""
    res = supabase.table("activity_logs") \
        .select("status, host_username, timestamp") \
        .eq("target_username", target_username) \
        .order("timestamp", desc=True) \
        .limit(1) \
        .execute()
    
    if res.data and len(res.data) > 0:
        return res.data[0]
    return None


def run_checker():
    """Fungsi utama pengecekan background."""
    logger.info("Memulai siklus pengecekan TikTok Activity Tracker...")
    
    targets, hosts = fetch_targets_and_hosts()
    logger.info("Ditemukan %d target dan %d host terdaftar.", len(targets), len(hosts))

    if not targets:
        logger.info("Tidak ada target yang dipantau. Selesai.")
        return

    # Kumpulkan peta target untuk pencarian cepat (case-insensitive)
    target_map: Dict[str, Dict] = {
        t["username"].strip().lower().lstrip("@"): t for t in targets
    }
    
    # State deteksi sementara: target_username -> (status, host_username)
    detected_state: Dict[str, Tuple[str, Optional[str]]] = {}

    # 1. Cek apakah ada Target yang sedang LIVE_HOST sendiri
    for uname, target_data in target_map.items():
        room_data = get_live_room_data(uname)
        if room_data:
            detected_state[uname] = ("LIVE_HOST", uname)
            logger.info("Target @%s terdeteksi sedang LIVE_HOST!", uname)

    # 2. Cek semua Host yang sedang Live dan periksa keberadaan Target di room mereka
    for h in hosts:
        host_uname = h["username"].strip().lower().lstrip("@")
        room_data = get_live_room_data(host_uname)

        if not room_data:
            continue

        logger.info("Host @%s sedang LIVE aktif.", host_uname)
        
        # Jika host ini juga merupakan salah satu target kita
        if host_uname in target_map and host_uname not in detected_state:
            detected_state[host_uname] = ("LIVE_HOST", host_uname)

        room_id = str(room_data.get("roomId") or room_data.get("room_id") or "")
        anchor_id = str(room_data.get("ownerInfo", {}).get("uid") or "")
        
        guests, viewers = fetch_room_participants(room_id, anchor_id)

        # Cek apakah target ada di daftar Tamu atau Penonton room host ini
        for uname in target_map.keys():
            if uname in detected_state and detected_state[uname][0] == "LIVE_HOST":
                continue

            if uname in guests:
                detected_state[uname] = ("TAMU", host_uname)
                logger.info("Target @%s terdeteksi sebagai TAMU di live @%s", uname, host_uname)
            elif uname in viewers:
                detected_state[uname] = ("PENONTON", host_uname)
                logger.info("Target @%s terdeteksi sebagai PENONTON di live @%s", uname, host_uname)

    # 3. Target yang tidak terdeteksi di room manapun berstatus OFFLINE
    for uname in target_map.keys():
        if uname not in detected_state:
            detected_state[uname] = ("OFFLINE", None)

    # 4. Sinkronisasi ke Supabase & Kirim Push Notification jika terjadi perubahan status
    new_logs_to_insert = []
    
    for uname, (status, host_uname) in detected_state.items():
        target_info = target_map[uname]
        raw_target_username = target_info["username"]
        display_name = target_info.get("display_name") or raw_target_username

        latest_log = get_latest_logged_status(raw_target_username)
        last_status = latest_log.get("status") if latest_log else None
        last_host = latest_log.get("host_username") if latest_log else None

        status_changed = (last_status != status) or (last_host != host_uname)

        if status_changed:
            log_item = {
                "target_username": raw_target_username,
                "status": status,
                "host_username": host_uname,
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
            new_logs_to_insert.append(log_item)

            # Format pesan notifikasi human-readable
            time_str = datetime.now().strftime("%H:%M")
            if status == "LIVE_HOST":
                msg = f"{display_name} (@{raw_target_username}) memulai Live Streaming sendiri - {time_str}"
                title = "🔴 Target Sedang Live"
            elif status == "TAMU":
                msg = f"{display_name} sebagai Tamu di live {host_uname} - {time_str}"
                title = "🎤 Target Komal/Tamu"
            elif status == "PENONTON":
                msg = f"{display_name} menonton di live {host_uname} - {time_str}"
                title = "👀 Target Menonton Live"
            else:
                msg = f"{display_name} (@{raw_target_username}) sekarang Offline - {time_str}"
                title = "⚪ Target Offline"

            logger.info("Perubahan status: %s", msg)
            send_free_push_notification(title, msg)

    # Simpan batch logs baru jika ada
    if new_logs_to_insert:
        supabase.table("activity_logs").insert(new_logs_to_insert).execute()
        logger.info("Berhasil menyimpan %d log aktivitas baru ke Supabase.", len(new_logs_to_insert))
    else:
        logger.info("Tidak ada perubahan status pada target. Database tetap teratur.")

    # 5. Update bot heartbeat untuk status koneksi di APK
    try:
        heartbeat_payload = {
            "id": "checker_worker",
            "last_ping": datetime.now(timezone.utc).isoformat(),
            "status": "ACTIVE",
            "targets_checked": len(targets),
            "hosts_checked": len(hosts),
            "details": f"Checked {len(targets)} targets and {len(hosts)} hosts at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        }
        supabase.table("bot_heartbeat").upsert(heartbeat_payload).execute()
        logger.info("Heartbeat bot berhasil diperbarui ke Supabase.")
    except Exception as hb_err:
        logger.debug("Heartbeat optional info (tabel bot_heartbeat belum dibuat): %s", str(hb_err))

    logger.info("Pengecekan selesai.")


if __name__ == "__main__":
    run_checker()
