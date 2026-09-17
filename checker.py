#!/usr/bin/env python3
"""TikTok Tracker V2 presence worker.

This worker is deliberately state-oriented:
- UNKNOWN is not OFFLINE.
- OFFLINE is only written after an explicit timeout/absence rule.
- Presence history is written separately from current state.
- Viewer/guest detection is source-dependent; this worker does not bypass
  authentication or private TikTok controls.

A source adapter can provide authorized observations. The included public-host
adapter only detects whether a public profile is currently presenting a LIVE
room; it does not pretend that absence from a room means the account is offline.
"""

import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, Optional

import requests
from supabase import Client, create_client

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("TikTokTrackerV2")

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "30"))
UNKNOWN_AFTER_SECONDS = int(os.environ.get("UNKNOWN_AFTER_SECONDS", "90"))
OFFLINE_AFTER_SECONDS = int(os.environ.get("OFFLINE_AFTER_SECONDS", "300"))

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_URL dan SUPABASE_KEY wajib di-set sebagai environment variable.")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

HEADERS = {
    "User-Agent": "TikTokTracker/2.0",
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "id-ID,id;q=0.9,en;q=0.8",
}


@dataclass(frozen=True)
class Observation:
    username: str
    state: str
    room_id: Optional[str] = None
    room_host_username: Optional[str] = None
    role: Optional[str] = None
    source: str = "unknown"
    confidence: Optional[float] = None
    details: Optional[dict] = None


class PublicHostAdapter:
    """Detect public LIVE-host state only.

    This adapter intentionally does not enumerate audience members or access
    private/session-only data. Viewer/guest observations should come from an
    authorized source and can be fed into the same state engine.
    """

    def get_host_live(self, username: str) -> Optional[Observation]:
        clean = username.strip().lstrip("@").lower()
        url = f"https://www.tiktok.com/@{clean}/live"
        try:
            r = requests.get(url, headers=HEADERS, timeout=8)
            if r.status_code != 200:
                return None
            # We intentionally keep parsing conservative. This is only a
            # best-effort public-page observation, not an authenticated API.
            if '"status":2' in r.text:
                return Observation(
                    username=clean,
                    state="LIVE_HOST",
                    room_host_username=clean,
                    role="HOST",
                    source="public_live_page",
                    confidence=0.80,
                )
        except requests.RequestException as exc:
            log.debug("host observation failed for @%s: %s", clean, exc)
        return None


class StateStore:
    def __init__(self, client: Client):
        self.client = client

    def targets(self) -> list[dict]:
        return self.client.table("targets").select("id,username,display_name").execute().data or []

    def hosts(self) -> list[dict]:
        return self.client.table("hosts").select("id,username,display_name").execute().data or []

    def current_presence(self) -> dict[str, dict]:
        rows = self.client.table("target_presence").select("*").execute().data or []
        return {str(r["target_id"]): r for r in rows}

    def write_observation(self, target: dict, obs: Observation) -> None:
        now = datetime.now(timezone.utc).isoformat()
        payload = {
            "target_id": target["id"],
            "state": obs.state,
            "room_id": obs.room_id,
            "room_host_username": obs.room_host_username,
            "role": obs.role,
            "last_seen": now,
            "detected_at": now,
            "source": obs.source,
            "confidence": obs.confidence,
            "details": obs.details or {},
            "updated_at": now,
        }
        self.client.table("target_presence").upsert(payload, on_conflict="target_id").execute()

    def write_event(self, target: dict, obs: Observation, event_type: str) -> None:
        self.client.table("presence_events").insert({
            "target_id": target["id"],
            "event_type": event_type,
            "state": obs.state,
            "room_id": obs.room_id,
            "room_host_username": obs.room_host_username,
            "role": obs.role,
            "source": obs.source,
            "confidence": obs.confidence,
            "metadata": obs.details or {},
        }).execute()

    def write_compat_log(self, target: dict, obs: Observation) -> None:
        # Keep the existing report screen working while V2 is rolled out.
        compat = {
            "UNKNOWN": "UNKNOWN",
            "OFFLINE": "OFFLINE",
            "ONLINE": "ONLINE",
            "LIVE_HOST": "LIVE_HOST",
            "LIVE_GUEST": "LIVE_GUEST",
            "LIVE_VIEWER": "LIVE_VIEWER",
        }.get(obs.state, "UNKNOWN")
        self.client.table("activity_logs").insert({
            "target_username": target["username"],
            "status": compat,
            "host_username": obs.room_host_username,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }).execute()

    def heartbeat(self, target_count: int, host_count: int, details: str) -> None:
        self.client.table("bot_heartbeat").upsert({
            "id": "presence_worker",
            "last_ping": datetime.now(timezone.utc).isoformat(),
            "status": "ACTIVE",
            "targets_checked": target_count,
            "hosts_checked": host_count,
            "details": details,
        }).execute()


def event_for_transition(old: Optional[dict], new: Observation) -> str:
    old_state = old.get("state") if old else None
    if old_state == new.state and (old.get("room_id") or None) == (new.room_id or None):
        return "STATE_CHANGED"  # caller can skip identical states
    if new.state == "LIVE_HOST" and old_state != "LIVE_HOST":
        return "LIVE_STARTED"
    if new.state == "LIVE_GUEST" and old_state != "LIVE_GUEST":
        return "BECAME_GUEST"
    if new.state == "LIVE_VIEWER" and old_state != "LIVE_VIEWER":
        return "VIEWER_DETECTED"
    if new.state == "OFFLINE":
        return "PRESENCE_LOST"
    return "STATE_CHANGED"


def build_observations(targets: Iterable[dict], adapter: PublicHostAdapter) -> Dict[str, Observation]:
    observations: Dict[str, Observation] = {}
    for target in targets:
        username = target["username"].strip().lstrip("@").lower()
        obs = adapter.get_host_live(username)
        if obs:
            observations[username] = obs
    return observations


def cycle(store: StateStore, adapter: PublicHostAdapter) -> None:
    targets = store.targets()
    hosts = store.hosts()
    current = store.current_presence()
    observed = build_observations(targets, adapter)

    for target in targets:
        username = target["username"].strip().lstrip("@").lower()
        old = current.get(str(target["id"]))
        obs = observed.get(username)

        if obs:
            same = bool(old and old.get("state") == obs.state and (old.get("room_id") or None) == (obs.room_id or None))
            store.write_observation(target, obs)
            if not same:
                event_type = event_for_transition(old, obs)
                store.write_event(target, obs, event_type)
                store.write_compat_log(target, obs)
                log.info("@%s -> %s", username, obs.state)
            continue

        # Critical V2 behavior: a missing observation is UNKNOWN first.
        # We never label a target OFFLINE merely because it wasn't a host.
        if old:
            last_seen_raw = old.get("last_seen") or old.get("updated_at")
            try:
                last_seen = datetime.fromisoformat(last_seen_raw.replace("Z", "+00:00")) if last_seen_raw else None
            except ValueError:
                last_seen = None
        else:
            last_seen = None

        age = (datetime.now(timezone.utc) - last_seen).total_seconds() if last_seen else float("inf")
        if age >= OFFLINE_AFTER_SECONDS:
            next_state = "OFFLINE"
        else:
            next_state = "UNKNOWN"

        if not old or old.get("state") != next_state:
            unknown = Observation(username=username, state=next_state, source="absence_timeout", confidence=None)
            store.write_observation(target, unknown)
            store.write_event(target, unknown, "PRESENCE_LOST" if next_state == "OFFLINE" else "STATE_CHANGED")
            store.write_compat_log(target, unknown)
            log.info("@%s -> %s (no host observation)", username, next_state)

    store.heartbeat(len(targets), len(hosts), f"V2 cycle; poll={POLL_SECONDS}s; host-only public observation")


def main() -> None:
    store = StateStore(supabase)
    adapter = PublicHostAdapter()
    log.info("TikTok Tracker V2 worker started; poll=%ss", POLL_SECONDS)
    while True:
        started = time.monotonic()
        try:
            cycle(store, adapter)
        except Exception:
            log.exception("worker cycle failed")
        elapsed = time.monotonic() - started
        time.sleep(max(1, POLL_SECONDS - int(elapsed)))


if __name__ == "__main__":
    main()
