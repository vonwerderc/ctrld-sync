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
import time
from typing import Dict, List, Optional, Any

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
FOLDER_CREATION_DELAY = 2  # seconds to wait after creating a folder

# --------------------------------------------------------------------------- #
# 2. Clients
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
# 3. Helpers
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


def _api_post_form(url: str, data: Dict) -> httpx.Response:
    """POST helper for form data with retries."""
    return _retry_request(lambda: _api.post(url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"}))


def _retry_request(request_func, max_retries=MAX_RETRIES, delay=RETRY_DELAY):
    """Retry a request function with exponential backoff."""
    for attempt in range(max_retries):
        try:
            response = request_func()
            response.raise_for_status()
            return response
        except (httpx.HTTPError, httpx.TimeoutException) as e:
            if attempt == max_retries - 1:
                # Log the response content if available
                if hasattr(e, 'response') and e.response is not None:
                    log.error(f"Response content: {e.response.text}")
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


def fetch_folder_data(url: str) -> Dict[str, Any]:
    """Return folder data from GitHub JSON."""
    js = _gh_get(url)
    return js


def delete_folder(profile_id: str, name: str, folder_id: str) -> bool:
    """Delete a single folder by its ID. Returns True if successful."""
    try:
        _api_delete(f"{API_BASE}/{profile_id}/groups/{folder_id}")
        log.info("Deleted folder '%s' (ID %s)", name, folder_id)
        return True
    except httpx.HTTPError as e:
        log.error(f"Failed to delete folder '{name}' (ID {folder_id}): {e}")
        return False


def create_folder(profile_id: str, name: str, do: int, status: int) -> Optional[str]:
    """
    Create a new folder and return its ID.
    The API returns the full list of groups, so we look for the one we just added.
    """
    try:
        _api_post(
            f"{API_BASE}/{profile_id}/groups",
            data={"name": name, "do": do, "status": status},
        )
        
        # Re-fetch the list and pick the folder we just created
        data = _api_get(f"{API_BASE}/{profile_id}/groups").json()
        for grp in data["body"]["groups"]:
            if grp["group"].strip() == name.strip():
                log.info("Created folder '%s' (ID %s)", name, grp["PK"])
                # Add a delay after folder creation to ensure it's fully processed
                time.sleep(FOLDER_CREATION_DELAY)
                return str(grp["PK"])
        
        log.error(f"Folder '{name}' was not found after creation")
        return None
    except (httpx.HTTPError, KeyError) as e:
        log.error(f"Failed to create folder '{name}': {e}")
        return None


def handle_duplicate_error(profile_id: str, batch: List[str], do: int, error_text: str) -> List[str]:
    """
    Handle duplicate rule errors by extracting the conflicting hostname and deciding what to do.
    Returns the list of hostnames that should be retried (empty list means skip the batch).
    """
    # Extract hostname from error message like "Custom Rule already exists: canva.site"
    import re
    match = re.search(r"Custom Rule already exists: (.+)", error_text)
    if not match:
        log.error("Could not extract hostname from duplicate error")
        return []
    
    conflicting_hostname = match.group(1).strip()
    log.info(f"Found conflicting rule: {conflicting_hostname}")
    
    if do == 1:
        # For blocking rules (do=1), delete the existing rule and retry
        try:
            _api_delete(f"{API_BASE}/{profile_id}/rules/{conflicting_hostname}")
            log.info(f"Deleted existing rule: {conflicting_hostname}")
            return batch  # Retry the whole batch
        except httpx.HTTPError as delete_error:
            log.error(f"Failed to delete existing rule {conflicting_hostname}: {delete_error}")
            return []
    else:
        # For allow rules (do!=1), skip this batch since the rule already exists
        log.info(f"Skipping batch due to existing allow rule: {conflicting_hostname}")
        return []


def push_rules(
    profile_id: str,
    folder_name: str,
    folder_id: str,
    do: int,
    status: int,
    hostnames: List[str],
) -> bool:
    """Push hostnames in batches to the given folder. Returns True if successful."""
    if not hostnames:
        log.info("Folder '%s' - no rules to push", folder_name)
        return True
    
    successful_batches = 0
    total_batches = len(range(0, len(hostnames), BATCH_SIZE))
    
    for i, start in enumerate(range(0, len(hostnames), BATCH_SIZE), 1):
        batch = hostnames[start : start + BATCH_SIZE]
        
        # Try to push the batch
        max_attempts = 2  # Original attempt + 1 retry for duplicates
        for attempt in range(max_attempts):
            data = {
                "do": str(do),
                "status": str(status),
                "group": str(folder_id),
            }
            
            for j, hostname in enumerate(batch):
                data[f"hostnames[{j}]"] = hostname
            
            try:
                _api_post_form(
                    f"{API_BASE}/{profile_id}/rules",
                    data=data,
                )
                log.info(
                    "Folder '%s' – batch %d: added %d rules%s",
                    folder_name,
                    i,
                    len(batch),
                    " (after handling duplicates)" if attempt > 0 else ""
                )
                successful_batches += 1
                break  # Success, move to next batch
                
            except httpx.HTTPError as e:
                error_text = ""
                if hasattr(e, 'response') and e.response is not None:
                    error_text = e.response.text
                
                # Check if it's a duplicate rule error (code 40003)
                if "already exists" in error_text or "40003" in error_text:
                    if attempt < max_attempts - 1:  # Not the last attempt
                        batch = handle_duplicate_error(profile_id, batch, do, error_text)
                        if not batch:
                            # Skip this batch
                            successful_batches += 1
                            break
                        # Continue to retry with the same batch
                    else:
                        # Last attempt failed, but it's a duplicate so count as success
                        log.warning(f"Folder '{folder_name}' – batch {i}: final attempt failed due to duplicates, counting as success")
                        successful_batches += 1
                        break
                else:
                    # Different error, log and fail
                    log.error(f"Failed to push batch {i} for folder '{folder_name}': {e}")
                    if error_text:
                        log.error(f"Response content: {error_text}")
                    break  # Don't retry for non-duplicate errors
    
    if successful_batches == total_batches:
        log.info("Folder '%s' – finished (%d total rules)", folder_name, len(hostnames))
        return True
    else:
        log.error(f"Folder '{folder_name}' – only {successful_batches}/{total_batches} batches succeeded")
        return False


# --------------------------------------------------------------------------- #
# 4. Main workflow
# --------------------------------------------------------------------------- #
def sync_profile(profile_id: str) -> bool:
    """One-shot sync: delete old, create new, push rules. Returns True if successful."""
    try:
        # Fetch all folder data first
        folder_data_list = []
        for url in FOLDER_URLS:
            try:
                folder_data_list.append(fetch_folder_data(url))
            except (httpx.HTTPError, KeyError) as e:
                log.error(f"Failed to fetch folder data from {url}: {e}")
                continue
        
        if not folder_data_list:
            log.error("No valid folder data found")
            return False
        
        # Get existing folders
        existing = list_existing_folders(profile_id)
        
        # Delete existing folders that match our target names
        for folder_data in folder_data_list:
            name = folder_data["group"]["group"].strip()
            if name in existing:
                delete_folder(profile_id, name, existing[name])
        
        # Create new folders and push rules
        success_count = 0
        for folder_data in folder_data_list:
            grp = folder_data["group"]
            name = grp["group"].strip()
            do = grp["action"]["do"]
            status = grp["action"]["status"]
            hostnames = [r["PK"] for r in folder_data.get("rules", []) if r.get("PK")]
            
            folder_id = create_folder(profile_id, name, do, status)
            if folder_id and push_rules(profile_id, name, folder_id, do, status, hostnames):
                success_count += 1
        
        log.info(f"Sync complete: {success_count}/{len(folder_data_list)} folders processed successfully")
        return success_count == len(folder_data_list)
    
    except Exception as e:
        log.error(f"Unexpected error during sync for profile {profile_id}: {e}")
        return False


# --------------------------------------------------------------------------- #
# 5. Entry-point
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
