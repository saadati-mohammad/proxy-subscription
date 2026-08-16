from __future__ import annotations

import concurrent.futures
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

import requests
import yaml


# =============================================================================
# CONFIG
# =============================================================================

PAYLOAD_URL = "https://proigor.com/payload.json"
LAUNCH_URL = "https://antpeak.com/api/launch/"

# These values are not the accessToken.
# The accessToken is generated dynamically by antpeak on every run.
APP_VERSION = "4.0.2"
PLATFORM = "chrome"
PLATFORM_VERSION = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/151.0.0.0 Safari/537.36"
)
TIME_ZONE = "Asia/Tehran"
DEVICE_NAME = "Chrome 151.0.0.0"

# Region definitions
REGIONS = {
    "nl": "Amsterdam",
    "fr-prs": "Paris",
    "ru-msk": "Moscow",
    "ru-spb": "Saint Petersburg",
    "sg": "Singapore",
    "gb-lnd": "London",
    "us-or": "US-Oregon",
    "us-va": "US-Virginia",
}

# Maximum number of requests to each provider for each region.
MAX_REQUESTS_PER_PROVIDER_REGION = 20

# Stop querying a provider when this many consecutive requests
# return zero NEW nodes.
STOP_AFTER_NO_NEW = 5

DELAY_BETWEEN_CALLS = 0.4

HTTP_TIMEOUT = 15

# Number of provider/region jobs executed simultaneously.
MAX_WORKERS = 12

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "subscriptions"

# Single combined subscription file. Regions are no longer shipped as
# separate files -- they are merged and reorganized into proxy-groups
# instead (see "GROUPING" section below).
OUTPUT_FILE = "subscription.yaml"


# =============================================================================
# GROUPING
# =============================================================================
#
# Old behaviour: one giant "Proxy" group of type "url-test" that
# health-checked EVERY single node on every Clash interval tick. With
# 500+ nodes that is slow, noisy, and pointless.
#
# New behaviour:
#   1. A single "خودکار" (Auto) group. This is the ONLY group that is
#      actually health-checked by Clash. It contains a small, curated
#      pool of nodes (sampled evenly across every region) wrapped in
#      two url-test sub-groups that race against real YouTube / Instagram
#      endpoints -- i.e. exactly the kind of request an Iranian user
#      would make when opening those apps. Clash keeps whichever node
#      answers fastest.
#   2. Everything else (nodes that were never health-checked) is sliced
#      into fixed-size batches and exposed as plain "select" groups with
#      creative names, so the user can browse/pick manually. No
#      background health-check traffic is generated for these at all.
#
# This keeps the script itself 100% passive/read-only towards the
# proxy providers (it only *lists* servers), the actual health-check
# traffic only ever happens on the end-user's own device, inside their
# own Clash client, against public YouTube/Instagram endpoints.

# Total number of nodes placed inside the "خودکار" (Auto) group.
AUTO_GROUP_SIZE = 30

# Every remaining node is grouped into fixed-size, manually selectable
# batches of this size.
BATCH_GROUP_SIZE = 20

# Endpoints used by the two url-test sub-groups inside "خودکار".
# Both are real, lightweight, platform-owned URLs that are blocked
# directly inside Iran but reachable through a working proxy -- so a
# successful check genuinely means "this node can load YouTube/Instagram
# from Iran right now", not just "this node can reach the internet".
YOUTUBE_TEST_URL = "https://www.youtube.com/generate_204"
INSTAGRAM_TEST_URL = "https://www.instagram.com/favicon.ico"

AUTO_GROUP_NAME = "🎯 خودکار (اتصال هوشمند)"
YOUTUBE_SUBGROUP_NAME = "📺 پینگ یوتیوب"
INSTAGRAM_SUBGROUP_NAME = "📸 پینگ اینستاگرام"
MAIN_SELECTOR_NAME = "🛡 انتخاب کانکشن"
IRAN_DIRECT_NAME = "Iran-Direct"

# Creative, Silk-Road/caravan themed names for the manually selectable
# batch groups -- each one carried a different kind of good along the
# old trade routes, same idea here, just carrying your traffic instead.
CARAVAN_GOODS = [
    "ابریشم", "زعفران", "فیروزه", "یاقوت", "عاج", "ادویه", "عطر",
    "گلاب", "قالی", "لاجورد", "مروارید", "کهربا", "عقیق", "نقره",
    "طلا", "صندل", "کتان", "مخمل", "حریر", "چای", "قند", "نمک",
    "کندر", "مشک", "بلور", "پوست", "خرما", "انار", "پسته", "بادام",
]


# =============================================================================
# HTTP
# =============================================================================

COMMON_HEADERS = {
    "accept": "application/json",
    "accept-encoding": "gzip, deflate, br",
    "content-type": "application/json",
    "user-agent": PLATFORM_VERSION,
    "origin": "chrome-extension://majdfhpaihoncoakbjgbdhglocklcgno",
}


# =============================================================================
# TOKEN MANAGER
# =============================================================================

class TokenManager:
    """
    Gets a fresh access token from antpeak.

    If a provider returns 401 later, the token can be refreshed automatically.
    """

    def __init__(self, udid: str) -> None:
        self.udid = udid
        self._token: str | None = None
        self._lock = threading.Lock()

    def _request_new_token(self) -> str:
        payload = {
            "udid": self.udid,
            "appVersion": APP_VERSION,
            "platform": PLATFORM,
            "platformVersion": PLATFORM_VERSION,
            "timeZone": TIME_ZONE,
            "deviceName": DEVICE_NAME,
        }

        response = requests.post(
            LAUNCH_URL,
            headers=COMMON_HEADERS,
            json=payload,
            timeout=HTTP_TIMEOUT,
        )

        response.raise_for_status()

        data = response.json()

        if not data.get("success"):
            raise RuntimeError(
                f"Launch API returned success=false: {data}"
            )

        token = (
            data.get("data", {})
            .get("accessToken")
        )

        if not token:
            raise RuntimeError(
                f"accessToken was not found in launch response: {data}"
            )

        return token

    def get_token(self, force_refresh: bool = False) -> str:
        with self._lock:
            if self._token is None or force_refresh:
                self._token = self._request_new_token()

            return self._token


# =============================================================================
# PAYLOAD
# =============================================================================

def get_free_domains() -> list[str]:
    """
    Downloads proigor payload.json and returns ONLY domains.free.
    Premium domains are ignored completely.
    """

    print("[1/5] Downloading provider list...")

    response = requests.get(
        PAYLOAD_URL,
        headers={
            "accept": "application/json",
            "user-agent": PLATFORM_VERSION,
        },
        timeout=HTTP_TIMEOUT,
    )

    response.raise_for_status()

    payload = response.json()

    domains = payload.get("domains")

    if not isinstance(domains, dict):
        raise RuntimeError(
            "Invalid payload.json: 'domains' object was not found."
        )

    free_domains = domains.get("free")

    if not isinstance(free_domains, list) or not free_domains:
        raise RuntimeError(
            "No free domains were found in payload.json."
        )

    # Remove accidental duplicates while preserving original order.
    result: list[str] = []
    seen: set[str] = set()

    for domain in free_domains:
        if not isinstance(domain, str):
            continue

        domain = domain.rstrip("/")

        if domain and domain not in seen:
            seen.add(domain)
            result.append(domain)

    return result


# =============================================================================
# PROVIDER REQUEST
# =============================================================================

def fetch_provider_region(
    provider_domain: str,
    region: str,
    token_manager: TokenManager,
) -> list[dict[str, Any]]:
    """
    Fetches nodes from:
        https://provider/api/server/list/

    using:
        Authorization: Bearer <accessToken>

    and JSON body:
        {
            "protocol": "https",
            "region": "...",
            "type": 0
        }
    """

    api_url = f"{provider_domain}/api/server/list/"

    payload = {
        "protocol": "https",
        "region": region,
        "type": 0,
    }

    for attempt in range(2):
        token = token_manager.get_token(
            force_refresh=(attempt == 1)
        )

        headers = dict(COMMON_HEADERS)
        headers["authorization"] = f"Bearer {token}"

        try:
            response = requests.post(
                api_url,
                headers=headers,
                json=payload,
                timeout=HTTP_TIMEOUT,
            )

            # Token probably expired.
            if response.status_code == 401 and attempt == 0:
                print(
                    f"      [TOKEN] 401 from {provider_domain} / {region}, "
                    f"refreshing token..."
                )
                continue

            response.raise_for_status()

            data = response.json()

            if not data.get("success"):
                return []

            entries = data.get("data", [])

            if not isinstance(entries, list):
                return []

            return entries

        except requests.RequestException as exc:
            print(
                f"      [ERROR] {provider_domain} / {region}: {exc}"
            )
            return []

        except ValueError as exc:
            print(
                f"      [ERROR] Invalid JSON from "
                f"{provider_domain} / {region}: {exc}"
            )
            return []

    return []


# =============================================================================
# NODE NORMALIZATION
# =============================================================================

def normalize_entry(entry: dict[str, Any], region: str) -> dict[str, Any] | None:
    """
    Converts provider response into the minimum information required
    to create a Clash HTTP proxy.
    """

    addresses = entry.get("addresses")

    if not isinstance(addresses, list) or not addresses:
        return None

    host = addresses[0]

    if not isinstance(host, str) or not host.strip():
        return None

    host = host.strip()

    port = entry.get("port")

    try:
        port = int(port)
    except (TypeError, ValueError):
        return None

    if port <= 0:
        return None

    username = entry.get("username")
    password = entry.get("password")

    if not isinstance(username, str) or not username:
        return None

    if not isinstance(password, str) or not password:
        return None

    protocol = entry.get("protocol") or "https"

    if protocol != "https":
        return None

    result = {
        "name": "",
        "type": "http",
        "server": host,
        "port": port,
        "username": username,
        "password": password,
        "tls": True,
        "skip-cert-verify": True,
        "_region": region,
        "_region_name": REGIONS.get(region, region),
    }

    return result


# =============================================================================
# UNIQUE KEY
# =============================================================================

def node_key(node: dict[str, Any]) -> tuple[Any, ...]:
    """
    The old script deduplicated only by host.

    Here we use:
        protocol + host + port + username + password

    so different ports are preserved.
    """

    return (
        node["type"],
        node["server"],
        node["port"],
        node["username"],
        node["password"],
    )


# =============================================================================
# COLLECT FROM ONE PROVIDER / ONE REGION
# =============================================================================

def collect_provider_region(
    provider_domain: str,
    region: str,
    token_manager: TokenManager,
) -> list[dict[str, Any]]:

    provider_name = provider_domain.replace("https://", "").rstrip("/")

    print(
        f"    [{region}] {provider_name}"
    )

    unique: dict[tuple[Any, ...], dict[str, Any]] = {}

    no_new_streak = 0

    for call_number in range(1, MAX_REQUESTS_PER_PROVIDER_REGION + 1):

        entries = fetch_provider_region(
            provider_domain=provider_domain,
            region=region,
            token_manager=token_manager,
        )

        new_count = 0

        for entry in entries:
            if not isinstance(entry, dict):
                continue

            node = normalize_entry(entry, region)

            if node is None:
                continue

            key = node_key(node)

            if key not in unique:
                unique[key] = node
                new_count += 1

        print(
            f"        call={call_number:02d} "
            f"returned={len(entries):02d} "
            f"new={new_count:02d} "
            f"unique={len(unique):02d}"
        )

        if new_count == 0:
            no_new_streak += 1
        else:
            no_new_streak = 0

        if no_new_streak >= STOP_AFTER_NO_NEW:
            break

        if call_number < MAX_REQUESTS_PER_PROVIDER_REGION:
            time.sleep(DELAY_BETWEEN_CALLS)

    return list(unique.values())


# =============================================================================
# COLLECT ONE REGION FROM ALL FREE PROVIDERS
# =============================================================================

def collect_region(
    region: str,
    providers: list[str],
    token_manager: TokenManager,
) -> list[dict[str, Any]]:

    print("")
    print("=" * 80)
    print(f"REGION: {region} ({REGIONS[region]})")
    print("=" * 80)

    all_unique: dict[tuple[Any, ...], dict[str, Any]] = {}

    tasks = []

    for provider in providers:
        tasks.append(
            (provider, region)
        )

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(MAX_WORKERS, len(tasks))
    ) as executor:

        future_map = {
            executor.submit(
                collect_provider_region,
                provider,
                region,
                token_manager,
            ): (provider, region)
            for provider, region in tasks
        }

        for future in concurrent.futures.as_completed(future_map):

            provider, _ = future_map[future]

            try:
                nodes = future.result()
            except Exception as exc:
                print(
                    f"    [ERROR] Worker failed for "
                    f"{provider}: {exc}"
                )
                continue

            for node in nodes:
                key = node_key(node)

                if key not in all_unique:
                    all_unique[key] = node

    # Stable sorting.
    result = list(all_unique.values())

    result.sort(
        key=lambda x: (
            x["server"],
            x["port"],
            x["username"],
        )
    )

    print("")
    print(
        f"[RESULT] {region}: {len(result)} unique nodes"
    )

    return result


# =============================================================================
# YAML GENERATION
# =============================================================================

def make_proxy_name(
    region: str,
    index: int,
    server: str,
) -> str:

    clean_server = (
        server
        .replace(".", "-")
        .replace("_", "-")
        .replace("/", "-")
        .replace(":", "-")
    )

    return f"{region.upper()}-{index:03d}-{clean_server}"


def pick_auto_nodes(
    nodes_by_region: dict[str, list[dict[str, Any]]],
    total: int,
) -> list[dict[str, Any]]:
    """
    Deterministically samples `total` nodes evenly across every region
    (round-robin), so the Auto group always has geographic diversity
    instead of being dominated by whichever region happened to return
    the most nodes.

    Deterministic on purpose: this script re-runs unattended on a
    schedule via GitHub Actions, so the same input should always
    produce the same shape of output (no randomness to chase).
    """

    picked: list[dict[str, Any]] = []
    pools = {region: list(nodes) for region, nodes in nodes_by_region.items()}
    regions_cycle = [r for r in nodes_by_region if pools[r]]

    while len(picked) < total and regions_cycle:
        for region in list(regions_cycle):
            if len(picked) >= total:
                break

            if pools[region]:
                picked.append(pools[region].pop(0))

            if not pools[region]:
                regions_cycle.remove(region)

    return picked


def chunked(
    items: list[dict[str, Any]],
    size: int,
) -> list[list[dict[str, Any]]]:
    """Splits `items` into consecutive chunks of at most `size` elements."""

    return [items[i:i + size] for i in range(0, len(items), size)]


DNS_CONFIG = {
    "mixed-port": 7890,
    "allow-lan": False,
    "mode": "rule",
    "log-level": "silent",
    "ipv6": False,
    "dns": {
        "enable": True,
        "ipv6": False,
        "enhanced-mode": "fake-ip",
        "fake-ip-range": "198.18.0.1/16",
        "nameserver": [
            "1.1.1.1",
            "9.9.9.9",
            "8.8.8.8",
            "64.6.64.6",
            "78.157.42.100",
            "78.157.42.101",
        ],
        "fallback": [
            "178.22.122.100",
            "185.51.200.2",
            "10.202.10.10",
            "10.202.10.11",
            "10.202.10.102",
            "10.202.10.202",
            "208.67.222.222",
            "208.67.220.220",
        ],
        "nameserver-policy": {
            "+.ir": "78.157.42.100",
        },
        "fallback-filter": {
            "geoip": True,
            "geoip-code": "IR",
            "ipcidr": [
                "240.0.0.0/4",
            ],
        },
    },
}


def batch_group_name(index: int) -> str:
    """
    Creative, caravan/Silk-Road themed name for batch group #index
    (1-based). Cycles through CARAVAN_GOODS if there are ever more
    batches than goods.
    """

    good = CARAVAN_GOODS[(index - 1) % len(CARAVAN_GOODS)]
    return f"🐫 کاروان {index:02d} - {good}"


def build_yaml(
    nodes_by_region: dict[str, list[dict[str, Any]]],
) -> str:
    """
    Builds the single combined Clash config from nodes collected across
    ALL regions.

    Proxy-group layout:
      🛡 انتخاب کانکشن   (top-level selector, this is what rules point to)
        ├─ 🎯 خودکار (اتصال هوشمند)   (select, wraps the two health-checked groups below)
        │    ├─ 📺 پینگ یوتیوب        (url-test against YouTube, 15 nodes)
        │    └─ 📸 پینگ اینستاگرام     (url-test against Instagram, 15 nodes)
        ├─ 🐫 کاروان 01 - ...          (select, 20 nodes, no health-check)
        ├─ 🐫 کاروان 02 - ...          (select, 20 nodes, no health-check)
        └─ ... one batch group per 20 remaining nodes
    """

    proxies: list[dict[str, Any]] = []
    all_named_nodes: list[dict[str, Any]] = []

    # Assign final Clash names to every node, per-region numbering
    # preserved (e.g. NL-001, FR-PRS-002, ...) so origin stays visible.
    for region, region_nodes in nodes_by_region.items():
        for index, node in enumerate(region_nodes, start=1):
            name = make_proxy_name(
                region=region,
                index=index,
                server=node["server"],
            )

            named_node = dict(node)
            named_node["_name"] = name
            all_named_nodes.append(named_node)

            proxies.append({
                "name": name,
                "type": "http",
                "server": node["server"],
                "port": node["port"],
                "username": node["username"],
                "password": node["password"],
                "tls": True,
                "skip-cert-verify": True,
            })

    # ---- Auto group: small, curated, health-checked pool -------------

    named_by_region: dict[str, list[dict[str, Any]]] = {}
    for node in all_named_nodes:
        named_by_region.setdefault(node["_region"], []).append(node)

    auto_pool = pick_auto_nodes(named_by_region, AUTO_GROUP_SIZE)
    auto_names = {n["_name"] for n in auto_pool}

    half = len(auto_pool) // 2
    youtube_names = [n["_name"] for n in auto_pool[:half or len(auto_pool)]]
    instagram_names = [n["_name"] for n in auto_pool[half:]] or youtube_names

    proxy_groups: list[dict[str, Any]] = []

    proxy_groups.append({
        "name": YOUTUBE_SUBGROUP_NAME,
        "type": "url-test",
        "proxies": youtube_names,
        "url": YOUTUBE_TEST_URL,
        "interval": 180,
        "tolerance": 50,
    })

    proxy_groups.append({
        "name": INSTAGRAM_SUBGROUP_NAME,
        "type": "url-test",
        "proxies": instagram_names,
        "url": INSTAGRAM_TEST_URL,
        "interval": 180,
        "tolerance": 50,
    })

    proxy_groups.append({
        "name": AUTO_GROUP_NAME,
        "type": "select",
        "proxies": [YOUTUBE_SUBGROUP_NAME, INSTAGRAM_SUBGROUP_NAME],
    })

    # ---- Batch groups: everything else, no health-check --------------

    remaining_nodes = [n for n in all_named_nodes if n["_name"] not in auto_names]

    batch_group_names: list[str] = []

    for batch_index, batch in enumerate(chunked(remaining_nodes, BATCH_GROUP_SIZE), start=1):
        name = batch_group_name(batch_index)
        batch_group_names.append(name)

        proxy_groups.append({
            "name": name,
            "type": "select",
            "proxies": [n["_name"] for n in batch],
        })

    # ---- Top-level selector + Iran direct -----------------------------

    proxy_groups.append({
        "name": MAIN_SELECTOR_NAME,
        "type": "select",
        "proxies": [AUTO_GROUP_NAME, *batch_group_names],
    })

    proxy_groups.append({
        "name": IRAN_DIRECT_NAME,
        "type": "select",
        "proxies": ["DIRECT"],
    })

    config: dict[str, Any] = {}

    config.update(DNS_CONFIG)

    config["proxies"] = proxies
    config["proxy-groups"] = proxy_groups

    config["rules"] = [
        f"GEOIP,IR,{IRAN_DIRECT_NAME}",
        f"MATCH,{MAIN_SELECTOR_NAME}",
    ]

    return yaml.safe_dump(
        config,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=200,
    )


# =============================================================================
# SAVE
# =============================================================================

def save_subscription(
    nodes_by_region: dict[str, list[dict[str, Any]]],
) -> Path:

    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    output_path = OUTPUT_DIR / OUTPUT_FILE

    yaml_content = build_yaml(nodes_by_region)

    output_path.write_text(
        yaml_content,
        encoding="utf-8",
    )

    return output_path


# =============================================================================
# MAIN
# =============================================================================

def main() -> int:

    print("")
    print("=" * 80)
    print("AUTOMATED CLASH SUBSCRIPTION GENERATOR")
    print("=" * 80)
    print("")

    # -------------------------------------------------------------------------
    # UDID
    # -------------------------------------------------------------------------

    udid = os.getenv("ANTPEAK_UDID")

    if not udid:
        print(
            "[FATAL] ANTPEAK_UDID environment variable is missing."
        )
        return 1

    # -------------------------------------------------------------------------
    # Provider list
    # -------------------------------------------------------------------------

    try:
        providers = get_free_domains()
    except Exception as exc:
        print(
            f"[FATAL] Could not load free domains: {exc}"
        )
        return 1

    print("")
    print("Free providers:")
    for provider in providers:
        print(f"  - {provider}")

    # -------------------------------------------------------------------------
    # Token
    # -------------------------------------------------------------------------

    token_manager = TokenManager(udid)

    try:
        token = token_manager.get_token()

        # DO NOT print the token.
        print("")
        print(
            "[2/5] accessToken successfully obtained."
        )

    except Exception as exc:
        print(
            f"[FATAL] Could not obtain accessToken: {exc}"
        )
        return 1

    # -------------------------------------------------------------------------
    # Regions
    # -------------------------------------------------------------------------

    print("")
    print("[3/5] Collecting servers...")

    nodes_by_region: dict[str, list[dict[str, Any]]] = {}

    for region in REGIONS:

        nodes = collect_region(
            region=region,
            providers=providers,
            token_manager=token_manager,
        )

        if not nodes:
            print(
                f"[WARNING] No nodes found for {region}."
            )
            continue

        nodes_by_region[region] = nodes

    # -------------------------------------------------------------------------
    # Build + save the single combined subscription file
    # -------------------------------------------------------------------------

    print("")
    print("[4/5] Building combined subscription...")

    total_nodes = sum(len(v) for v in nodes_by_region.values())

    # Important:
    # Never overwrite a previously generated good file with an empty
    # result -- if every region came back empty (provider outage,
    # token issue, etc.) just keep whatever subscription.yaml already
    # exists in the repo.
    if total_nodes == 0:
        print(
            "[FATAL] No nodes were found in any region. "
            "Existing subscription.yaml will be preserved."
        )
        return 1

    output_path = save_subscription(nodes_by_region)

    print(
        f"[SAVED] {output_path} "
        f"({total_nodes} nodes total)"
    )

    # -------------------------------------------------------------------------
    # Summary
    # -------------------------------------------------------------------------

    print("")
    print("=" * 80)
    print("SUMMARY")
    print("=" * 80)

    for region, nodes in nodes_by_region.items():
        print(
            f"  {region:<8} "
            f"{REGIONS[region]:<18} "
            f"{len(nodes):>4} nodes"
        )

    auto_count = min(AUTO_GROUP_SIZE, total_nodes)
    batch_count = -(-(total_nodes - auto_count) // BATCH_GROUP_SIZE)  # ceil div

    print("-" * 80)
    print(f"  {AUTO_GROUP_NAME:<28} {auto_count:>4} nodes (health-checked)")
    print(f"  {batch_count} caravan batch group(s) of up to {BATCH_GROUP_SIZE} nodes each (manual, no health-check)")

    print("=" * 80)
    print("[5/5] DONE")
    print("=" * 80)

    return 0


if __name__ == "__main__":
    sys.exit(main())
