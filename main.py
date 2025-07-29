#!/usr/bin/env python3
"""
Control D Sync
----------------------
A tiny helper that keeps your Control D folders in sync with a set of
remote block-lists.

It does three things:
1. Reads the folder names from the JSON files.
2. Deletes any existing folders with those names (so we start fresh).
3. Re-creates the folders and pushes all rules in batches.

Nothing fancy, just works.
"""

import os
import logging
import asyncio
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass

import httpx
from dotenv import load_dotenv

# --------------------------------------------------------------------------- #
# 0. Bootstrap – load secrets and configure logging
# --------------------------------------------------------------------------- #
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("control-d-sync")

# --------------------------------------------------------------------------- #
# 1. Constants – tweak only here
# --------------------------------------------------------------------------- #
API_BASE = "https://api.controld.com/profiles"
TOKEN = os.getenv("TOKEN")

# Accept either a single profile id or a comma-separated list
PROFILE_IDS = [p.strip() for p in os.getenv("PROFILE", "").split(",") if p.strip()]

# URLs of the JSON block-lists we want to import
FOLDER_URLS = [
    "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/controld/badware-hoster-folder.json",
    "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/controld/native-tracker-amazon-folder.json",
    "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/controld/native-tracker-microsoft-folder.json",
    "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/controld/native-tracker-tiktok-aggressive-folder.json",
    "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/controld/referral-allow-folder.json",
    "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/controld/spam-idns-folder.json",
    "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/controld/spam-tlds-allow-folder.json",
    "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/controld/spam-tlds-folder.json",
    "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/controld/ultimate-known_issues-allow-folder.json",
]

BATCH_SIZE = 500
MAX_RETRIES = 3
RETRY_DELAY = 1  # seconds

# --------------------------------------------------------------------------- #
# 2. Data Models
# --------------------------------------------------------------------------- #
@dataclass
class FolderConfig:
    name: str
    do: int
    status: int
    hostnames: List[str]

# --------------------------------------------------------------------------- #
# 3. Clients
# --------------------------------------------------------------------------- #
# Control-D API client (with auth)
_api = httpx.Client(
    headers={
        "Accept": "application/json",
        "Authorization": f"Bearer {TOKEN}",
    },
    timeout=30,
)

# GitHub raw client (no auth, no headers)
_gh = httpx.Client(timeout=30)

# --------------------------------------------------------------------------- #
# 4. Helpers
# --------------------------------------------------------------------------- #
# simple in-memory cache: url -> decoded JSON
_cache: Dict[str, Dict] = {}


def _api_get(url: str) -> httpx.Response:
    """GET helper for Control-D API with retries."""
    return _retry_request(lambda: _api.get(url))


def _api_delete(url: str) -> httpx.Response:
    """DELETE helper for Control-D API with retries."""
    return _retry_request(lambda: _api.delete(url))


def _api_post(url: str, data: Dict) -> httpx.Response:
    """POST helper for Control-D API with retries."""
    return _retry_request(lambda: _api.post(url, data=data))


def _retry_request(request_func, max_retries=MAX_RETRIES, delay=RETRY_DELAY):
    """Retry a request function with exponential backoff."""
    import time
    
    for attempt in range(max_retries):
        try:
            response = request_func()
            response.raise_for_status()
            return response
        except (httpx.HTTPError, httpx.TimeoutException) as e:
            if attempt == max_retries - 1:
                raise
            wait_time = delay * (2 ** attempt)
            log.warning(f"Request failed (attempt {attempt + 1}/{max_retries}): {e}. Retrying in {wait_time}s...")
            time.sleep(wait_time)


def _gh_get(url: str) -> Dict:
    """Fetch JSON from GitHub (cached)."""
    if url not in _cache:
        r = _gh.get(url)
        r.raise_for_status()
        _cache[url] = r.json()
    return _cache[url]


def list_existing_folders(profile_id: str) -> Dict[str, str]:
    """Return folder-name -> folder-id mapping."""
    try:
        data = _api_get(f"{API_BASE}/{profile_id}/groups").json()
        folders = data.get("body", {}).get("groups", [])
        return {
            f["group"].strip(): f["PK"]
            for f in folders
            if f.get("group") and f.get("PK")
        }
    except (httpx.HTTPError, KeyError) as e:
        log.error(f"Failed to list existing folders: {e}")
        return {}


def fetch_folder_config(url: str) -> FolderConfig:
    """Return folder configuration from GitHub JSON."""
    js = _gh_get(url)
    grp = js["group"]
    hostnames = [r["PK"] for r in js.get("rules", []) if r.get("PK")]
    
    return FolderConfig(
        name=grp["group"].strip(),
        do=grp["action"]["do"],
        status=grp["action"]["status"],
        hostnames=hostnames
    )


def delete_folder(profile_id: str, name: str, folder_id: str) -> bool:
    """Delete a single folder by its ID. Returns True if successful."""
    try:
        _api_delete(f"{API_BASE}/{profile_id}/groups/{folder_id}")
        log.info("Deleted folder '%s' (ID %s)", name, folder_id)
        return True
    except httpx.HTTPError as e:
        log.error(f"Failed to delete folder '{name}' (ID {folder_id}): {e}")
        return False


def create_folder(profile_id: str, folder_config: FolderConfig) -> Optional[str]:
    """
    Create a new folder and return its ID.
    The API returns the full list of groups, so we look for the one we just added.
    """
    try:
        _api_post(
            f"{API_BASE}/{profile_id}/groups",
            data={"name": folder_config.name, "do": folder_config.do, "status": folder_config.status},
        )
        
        # Re-fetch the list and pick the folder we just created
        data = _api_get(f"{API_BASE}/{profile_id}/groups").json()
        for grp in data["body"]["groups"]:
            if grp["group"].strip() == folder_config.name.strip():
                log.info("Created folder '%s' (ID %s)", folder_config.name, grp["PK"])
                return str(grp["PK"])
        
        log.error(f"Folder '{folder_config.name}' was not found after creation")
        return None
    except (httpx.HTTPError, KeyError) as e:
        log.error(f"Failed to create folder '{folder_config.name}': {e}")
        return None


def push_rules(
    profile_id: str,
    folder_config: FolderConfig,
    folder_id: str,
) -> bool:
    """Push hostnames in batches to the given folder. Returns True if successful."""
    if not folder_config.hostnames:
        log.info("Folder '%s' - no rules to push", folder_config.name)
        return True
    
    try:
        for i, start in enumerate(range(0, len(folder_config.hostnames), BATCH_SIZE), 1):
            batch = folder_config.hostnames[start : start + BATCH_SIZE]
            _api_post(
                f"{API_BASE}/{profile_id}/rules",
                data={
                    "do": folder_config.do,
                    "status": folder_config.status,
                    "group": folder_id,
                    "hostnames[]": batch,
                },
            )
            log.info(
                "Folder '%s' – batch %d: added %d rules",
                folder_config.name,
                i,
                len(batch),
            )
        
        log.info("Folder '%s' – finished (%d total rules)", folder_config.name, len(folder_config.hostnames))
        return True
    except httpx.HTTPError as e:
        log.error(f"Failed to push rules for folder '{folder_config.name}': {e}")
        return False


# --------------------------------------------------------------------------- #
# 5. Main workflow
# --------------------------------------------------------------------------- #
def sync_profile(profile_id: str) -> bool:
    """One-shot sync: delete old, create new, push rules. Returns True if successful."""
    try:
        # Fetch all folder configurations first
        folder_configs = []
        for url in FOLDER_URLS:
            try:
                folder_configs.append(fetch_folder_config(url))
            except (httpx.HTTPError, KeyError) as e:
                log.error(f"Failed to fetch folder config from {url}: {e}")
                continue
        
        if not folder_configs:
            log.error("No valid folder configurations found")
            return False
        
        # Get existing folders
        existing = list_existing_folders(profile_id)
        
        # Delete existing folders that match our target names
        for config in folder_configs:
            if config.name in existing:
                delete_folder(profile_id, config.name, existing[config.name])
        
        # Create new folders and push rules
        success_count = 0
        for config in folder_configs:
            folder_id = create_folder(profile_id, config)
            if folder_id and push_rules(profile_id, config, folder_id):
                success_count += 1
        
        log.info(f"Sync complete: {success_count}/{len(folder_configs)} folders processed successfully")
        return success_count == len(folder_configs)
    
    except Exception as e:
        log.error(f"Unexpected error during sync for profile {profile_id}: {e}")
        return False


# --------------------------------------------------------------------------- #
# 6. Entry-point
# --------------------------------------------------------------------------- #
def main():
    if not TOKEN or not PROFILE_IDS:
        log.error("TOKEN and/or PROFILE missing - check your .env file")
        exit(1)
    
    success_count = 0
    for profile_id in PROFILE_IDS:
        log.info("Starting sync for profile %s", profile_id)
        if sync_profile(profile_id):
            success_count += 1
    
    log.info(f"All profiles processed: {success_count}/{len(PROFILE_IDS)} successful")
    exit(0 if success_count == len(PROFILE_IDS) else 1)


if __name__ == "__main__":
    main()
