import httpx
import os
import logging
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%H:%M:%S",
)

logging.getLogger("httpx").setLevel(logging.WARNING)

log = logging.getLogger(__name__)

BASE_URL = "https://api.controld.com/profiles/"
TOKEN = os.environ.get("TOKEN")
PROFILE = os.environ.get("PROFILE")

FOLDERS = [
    "https://raw.githubusercontent.com/hagezi/dns-blocklists/refs/heads/main/controld/badware-hoster-folder.json",
    "https://raw.githubusercontent.com/hagezi/dns-blocklists/refs/heads/main/controld/native-tracker-amazon-folder.json",
    "https://raw.githubusercontent.com/hagezi/dns-blocklists/refs/heads/main/controld/native-tracker-microsoft-folder.json",
    "https://raw.githubusercontent.com/hagezi/dns-blocklists/refs/heads/main/controld/native-tracker-tiktok-aggressive-folder.json",
    "https://raw.githubusercontent.com/hagezi/dns-blocklists/refs/heads/main/controld/referral-allow-folder.json",
    "https://raw.githubusercontent.com/hagezi/dns-blocklists/refs/heads/main/controld/spam-idns-folder.json",
    "https://raw.githubusercontent.com/hagezi/dns-blocklists/refs/heads/main/controld/spam-tlds-allow-folder.json",
    "https://raw.githubusercontent.com/hagezi/dns-blocklists/refs/heads/main/controld/spam-tlds-folder.json",
    "https://raw.githubusercontent.com/hagezi/dns-blocklists/refs/heads/main/controld/ultimate-known_issues-allow-folder.json",
]


def batch(iterable, n=1):
    l = len(iterable)
    for ndx in range(0, l, n):
        yield iterable[ndx : min(ndx + n, l)]


def get_profile_folders_map(profile_id):
    with httpx.Client() as client:
        response = client.get(
            f"{BASE_URL}{profile_id}/groups",
            headers={"accept": "application/json", "authorization": f"Bearer {TOKEN}"},
        )
        response.raise_for_status()
        data = response.json()
        folders = data.get("body", {}).get("groups", [])
        folder_map = {}
        for folder in folders:
            if not isinstance(folder, dict):
                log.warning(f"Skipping non-dict folder entry: {folder}")
                continue
            name = folder.get("group")
            pk = folder.get("PK")
            if name and pk:
                folder_map[name.strip().lower()] = pk
        return folder_map


def get_folder_names_from_jsons():
    names = []
    for folder_url in FOLDERS:
        try:
            with httpx.Client() as client:
                resp = client.get(folder_url)
                resp.raise_for_status()
                folder_json = resp.json()
                group = folder_json.get("group", {})
                folder_name = group.get("group")
                if folder_name:
                    names.append(folder_name.strip().lower())
        except Exception as e:
            log.error(f"Could not fetch folder name from {folder_url}: {e}")
    return names


def delete_folders(profile_id, folder_names):
    folder_map = get_profile_folders_map(profile_id)
    for name in folder_names:
        group_id = folder_map.get(name)
        if group_id:
            try:
                with httpx.Client() as client:
                    response = client.delete(
                        f"{BASE_URL}{profile_id}/groups/{group_id}",
                        headers={
                            "accept": "application/json",
                            "authorization": f"Bearer {TOKEN}",
                        },
                    )
                    response.raise_for_status()
                    log.info(f"Deleted folder '{name}' (ID {group_id})")
            except Exception as e:
                log.error(f"Failed to delete folder '{name}': {e}")
        else:
            log.info(f"Folder '{name}' not found, skipping delete.")


def create_profile_folders_and_rules(profile_id):
    folder_map = get_profile_folders_map(profile_id)
    log.info(f"Available folders: {folder_map}")

    BATCH_SIZE = 500  # Adjust as needed

    for folder_url in FOLDERS:
        try:
            with httpx.Client() as client:
                folder_response = client.get(folder_url)
                folder_response.raise_for_status()
                folder_json = folder_response.json()

                group = folder_json.get("group", {})
                folder_name = group.get("group")
                action = group.get("action", {})
                do = action.get("do")
                status = action.get("status")

                if not folder_name or do is None or status is None:
                    log.error(
                        f"Missing required fields in {folder_url}: "
                        f"name={folder_name}, do={do}, status={status}"
                    )
                    continue

                search_name = folder_name.strip().lower()
                group_id = folder_map.get(search_name)
                if not group_id:
                    data = {"name": folder_name, "do": do, "status": status}
                    create_response = client.post(
                        f"{BASE_URL}{profile_id}/groups",
                        headers={
                            "accept": "application/json",
                            "authorization": f"Bearer {TOKEN}",
                        },
                        data=data,
                    )
                    create_response.raise_for_status()
                    folder_map = get_profile_folders_map(profile_id)
                    group_id = folder_map.get(search_name)
                    if not group_id:
                        for name, gid in folder_map.items():
                            if name.strip().lower() == search_name:
                                group_id = gid
                                break
                    if not group_id:
                        log.error(
                            f"Could not determine group ID for '{folder_name}' after creation."
                        )
                        log.error(f"Available folders: {list(folder_map.keys())}")
                        continue

                log.info(f"Using folder '{folder_name}' with ID {group_id}")

                rules = folder_json.get("rules", [])
                hostnames = [rule.get("PK") for rule in rules if rule.get("PK")]
                if not hostnames:
                    log.warning(f"No hostnames found in {folder_url}")
                    continue

                for host_batch in batch(hostnames, BATCH_SIZE):
                    rule_data = {
                        "do": do,
                        "status": status,
                        "group": group_id,
                        "hostnames[]": host_batch,
                    }
                    try:
                        rule_response = client.post(
                            f"{BASE_URL}{profile_id}/rules",
                            headers={
                                "accept": "application/json",
                                "authorization": f"Bearer {TOKEN}",
                            },
                            data=rule_data,
                        )
                        rule_response.raise_for_status()
                        log.info(
                            f"Added {len(host_batch)} rules to folder '{folder_name}': {rule_response.json()}"
                        )
                    except httpx.HTTPStatusError as e:
                        log.error(
                            f"HTTP error for '{folder_url}' (batch): {e.response.text}"
                        )
        except httpx.HTTPStatusError as e:
            log.error(f"HTTP error for '{folder_url}': {e.response.text}")
        except Exception as e:
            log.error(f"Unexpected error for '{folder_url}': {e}")


def main():
    folder_names = get_folder_names_from_jsons()
    delete_folders(PROFILE, folder_names)
    create_profile_folders_and_rules(PROFILE)


if __name__ == "__main__":
    main()
