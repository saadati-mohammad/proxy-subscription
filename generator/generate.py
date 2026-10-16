from __future__ import annotations

import concurrent.futures
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

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


# =============================================================================
# SERVER-LIST ENDPOINTS  (NEW: v3 first, legacy as fallback)
# =============================================================================
#
# Every provider is called through the endpoints below, IN THIS ORDER:
#
#   1. v3      POST {domain}/v3/server/list/     <- new structure (PRIORITY)
#   2. legacy  POST {domain}/api/server/list/    <- old structure (kept)
#
# If v3 works for a provider, it is used. If v3 fails (404/405/..., network
# error, invalid JSON, or an empty answer), the legacy endpoint is tried, so
# nothing that worked before is lost.
#
# If a provider answers 404/405/410/501 on v3, v3 is remembered as
# "unsupported" for that provider for the rest of the run, so we do not waste
# requests on it again.
#
# =============================================================================

SERVER_LIST_ENDPOINTS: list[tuple[str, str]] = [
    ("v3", "/v3/server/list/"),
    ("legacy", "/api/server/list/"),
]

UNSUPPORTED_STATUS_CODES = {404, 405, 410, 501}

# Headers copied from the real browser-extension request to /v3/server/list/.
V3_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/155.0.0.0 Safari/537.36"
)

V3_EXTRA_HEADERS = {
    "accept-language": "en-US,en;q=0.9,fa-IR;q=0.8,fa;q=0.7,ko-KR;q=0.6,ko;q=0.5",
    "priority": "u=1, i",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "none",
    "user-agent": V3_USER_AGENT,
}

_unsupported_endpoints: set[tuple[str, str]] = set()
_unsupported_lock = threading.Lock()


def is_endpoint_unsupported(provider_domain: str, kind: str) -> bool:
    with _unsupported_lock:
        return (provider_domain, kind) in _unsupported_endpoints


def mark_endpoint_unsupported(provider_domain: str, kind: str) -> None:
    with _unsupported_lock:
        if (provider_domain, kind) in _unsupported_endpoints:
            return
        _unsupported_endpoints.add((provider_domain, kind))

    print(
        f"      [ENDPOINT] {kind} is not supported by "
        f"{provider_domain}; using the next endpoint."
    )


# =============================================================================
# REGIONS
# =============================================================================

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


# =============================================================================
# REGION DISPLAY
# =============================================================================

REGION_EMOJIS = {
    "nl": "🇳🇱",
    "fr-prs": "🇫🇷",
    "ru-msk": "🇷🇺",
    "ru-spb": "🇷🇺",
    "sg": "🇸🇬",
    "gb-lnd": "🇬🇧",
    "us-or": "🇺🇸",
    "us-va": "🇺🇸",
}


# =============================================================================
# CLASH GROUP NAMES
# =============================================================================

# These names are used directly in BOTH proxy-groups and rules, so a group
# and the rule that references it can never drift apart.

IRAN_DIRECT_GROUP = "🇮🇷 Iran Direct"


# =============================================================================
# PROVIDER DISPLAY CONFIG
# =============================================================================

PROVIDER_NAME_OVERRIDES = {
    "bitphox.com": "Bitphox",
    "tronyza.com": "Tronyza",
    "tronlit.com": "Tronlit",
    "freloop.com": "Freloop",
    "hisball.com": "Hisball",
    "hibchr.com": "Hibchr",
    "antpeak.com": "AntPeak",
}

PROVIDER_EMOJIS = {
    "bitphox.com": "⚡",
    "tronyza.com": "🔥",
    "tronlit.com": "🚀",
    "freloop.com": "🌐",
    "hisball.com": "🛰️",
    "hibchr.com": "💎",
    "antpeak.com": "👑",
}

DEFAULT_PROVIDER_EMOJIS = [
    "⚡",
    "🔥",
    "🚀",
    "🌐",
    "🛰️",
    "💎",
    "🎯",
    "🔷",
    "☁️",
    "🦊",
]


# =============================================================================
# MANUAL PROVIDERS
# =============================================================================
#
# Providers that are NOT part of payload.json's "free" list. They are fetched
# in a separate pass AFTER the regular providers, then merged into the same
# node pool. They use exactly the same endpoint logic (v3 first, then legacy).
#
# =============================================================================

ANTPEAK_PROVIDER_DOMAIN = "https://antpeak.com"


def get_manual_providers() -> list[str]:
    return [
        ANTPEAK_PROVIDER_DOMAIN,
    ]


def is_priority_provider(provider_domain: str) -> bool:
    """
    Providers that are always shown FIRST in the Clash proxy-group lists.
    """

    return (
        get_provider_hostname(provider_domain)
        == get_provider_hostname(ANTPEAK_PROVIDER_DOMAIN)
    )


# =============================================================================
# COLLECTION
# =============================================================================

MAX_REQUESTS_PER_PROVIDER_REGION = 20

STOP_AFTER_NO_NEW = 5

DELAY_BETWEEN_CALLS = 0.4

HTTP_TIMEOUT = 15

MAX_WORKERS = 12


# =============================================================================
# OUTPUT
# =============================================================================

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "subscriptions"


# =============================================================================
# HTTP
# =============================================================================

# NOTE: "br" / "zstd" are intentionally NOT advertised. `requests` can only
# decode them if optional packages (brotli / zstandard) are installed; if the
# server picked br/zstd, response.json() would fail with garbage. gzip/deflate
# are always supported.
COMMON_HEADERS = {
    "accept": "application/json",
    "accept-encoding": "gzip, deflate",
    "content-type": "application/json",
    "user-agent": PLATFORM_VERSION,
    "origin": "chrome-extension://majdfhpaihoncoakbjgbdhglocklcgno",
}


def build_headers(kind: str, token: str) -> dict[str, str]:
    headers = dict(COMMON_HEADERS)

    if kind == "v3":
        headers.update(V3_EXTRA_HEADERS)

    headers["authorization"] = f"Bearer {token}"

    return headers


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

        token = data.get("data", {}).get("accessToken")

        if not token:
            raise RuntimeError(
                f"accessToken was not found in launch response: {data}"
            )

        return token

    def get_token(
        self,
        force_refresh: bool = False,
        stale_token: str | None = None,
    ) -> str:
        """
        force_refresh : always request a new token.
        stale_token   : the token that just got a 401. If another thread has
                        already refreshed in the meantime, reuse that new
                        token instead of hammering the launch API again.
        """

        with self._lock:

            if self._token is None:
                self._token = self._request_new_token()

            elif force_refresh and (
                stale_token is None or self._token == stale_token
            ):
                self._token = self._request_new_token()

            return self._token


# =============================================================================
# PAYLOAD
# =============================================================================

def get_free_domains() -> list[str]:

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

    result: list[str] = []
    seen: set[str] = set()

    for domain in free_domains:

        if not isinstance(domain, str):
            continue

        domain = domain.strip().rstrip("/")

        if not domain:
            continue

        if domain not in seen:
            seen.add(domain)
            result.append(domain)

    return result


# =============================================================================
# PROVIDER HELPERS
# =============================================================================

def get_provider_hostname(provider_domain: str) -> str:
    """
    https://bitphox.com/  ->  bitphox.com
    """

    parsed = urlparse(provider_domain)

    hostname = parsed.hostname

    if not hostname:
        hostname = provider_domain

    hostname = hostname.strip().lower()

    if hostname.startswith("www."):
        hostname = hostname[4:]

    return hostname


def get_provider_name(provider_domain: str) -> str:

    hostname = get_provider_hostname(provider_domain)

    override = PROVIDER_NAME_OVERRIDES.get(hostname)

    if override:
        return override

    parts = hostname.split(".")

    if len(parts) >= 2:
        name = parts[-2]
    else:
        name = parts[0]

    name = name.replace("-", " ").replace("_", " ").strip()

    return name.title()


def get_provider_emoji(
    provider_domain: str,
    provider_index: int,
) -> str:

    hostname = get_provider_hostname(provider_domain)

    explicit = PROVIDER_EMOJIS.get(hostname)

    if explicit:
        return explicit

    return DEFAULT_PROVIDER_EMOJIS[
        provider_index % len(DEFAULT_PROVIDER_EMOJIS)
    ]


def get_provider_group_name(
    provider_domain: str,
    provider_index: int,
) -> str:

    emoji = get_provider_emoji(
        provider_domain=provider_domain,
        provider_index=provider_index,
    )

    provider_name = get_provider_name(provider_domain)

    return f"{emoji} {provider_name}"


def get_region_group_name(region: str) -> str:

    emoji = REGION_EMOJIS.get(region, "🌍")

    region_name = REGIONS.get(region, region)

    return f"{emoji} {region_name}"


# =============================================================================
# PROVIDER REQUEST
# =============================================================================

def extract_entries(data: Any) -> list[Any] | None:
    """
    Pulls the server list out of a provider response.

    Returns:
        list  -> a valid answer (possibly empty)
        None  -> failure / unrecognised shape

    Understands the legacy shape
        {"success": true, "data": [ ... ]}
    and is tolerant of a v3 shape that nests or renames the list, e.g.
        {"data": {"servers": [ ... ]}}   /   [ ... ]
    """

    if isinstance(data, list):
        return data

    if not isinstance(data, dict):
        return None

    if "success" in data and not data["success"]:
        return None

    keys = ("data", "servers", "list", "items", "result")

    for key in keys:

        value = data.get(key)

        if isinstance(value, list):
            return value

        if isinstance(value, dict):

            for inner_key in keys:

                inner = value.get(inner_key)

                if isinstance(inner, list):
                    return inner

    # success=true but no list at all -> treat as an empty answer.
    if data.get("success"):
        return []

    return None


def call_server_list_endpoint(
    provider_domain: str,
    region: str,
    kind: str,
    path: str,
    token_manager: TokenManager,
) -> tuple[list[Any] | None, bool]:
    """
    One call to one endpoint (with one automatic token refresh on 401).

    Returns (entries, unsupported):
        entries     list on success (maybe empty), None on failure
        unsupported True if the endpoint does not exist for this provider
    """

    url = f"{provider_domain}{path}"

    payload = {
        "protocol": "https",
        "region": region,
        "type": 0,
    }

    token = token_manager.get_token()

    for attempt in range(2):

        try:

            response = requests.post(
                url,
                headers=build_headers(kind, token),
                json=payload,
                timeout=HTTP_TIMEOUT,
            )

        except requests.RequestException as exc:

            print(
                f"      [ERROR] [{kind}] "
                f"{provider_domain} / {region}: {exc}"
            )

            return None, False

        # Token probably expired.
        if response.status_code == 401 and attempt == 0:

            print(
                f"      [TOKEN] 401 from [{kind}] "
                f"{provider_domain} / {region}, refreshing token..."
            )

            try:
                token = token_manager.get_token(
                    force_refresh=True,
                    stale_token=token,
                )
            except Exception as exc:
                print(f"      [ERROR] Token refresh failed: {exc}")
                return None, False

            continue

        if response.status_code in UNSUPPORTED_STATUS_CODES:
            return None, True

        if not response.ok:

            print(
                f"      [ERROR] [{kind}] "
                f"{provider_domain} / {region}: "
                f"HTTP {response.status_code}"
            )

            return None, False

        try:
            data = response.json()

        except ValueError as exc:

            print(
                f"      [ERROR] Invalid JSON from [{kind}] "
                f"{provider_domain} / {region}: {exc}"
            )

            return None, False

        return extract_entries(data), False

    return None, False


def fetch_provider_region(
    provider_domain: str,
    region: str,
    token_manager: TokenManager,
) -> list[dict[str, Any]]:
    """
    Tries every endpoint in SERVER_LIST_ENDPOINTS in order (v3 first, then
    legacy) and returns the first non-empty answer.
    """

    for kind, path in SERVER_LIST_ENDPOINTS:

        if is_endpoint_unsupported(provider_domain, kind):
            continue

        entries, unsupported = call_server_list_endpoint(
            provider_domain=provider_domain,
            region=region,
            kind=kind,
            path=path,
            token_manager=token_manager,
        )

        if unsupported:
            mark_endpoint_unsupported(provider_domain, kind)
            continue

        if entries:
            return entries

        # Failed or empty -> fall through to the next endpoint.

    return []


# =============================================================================
# NODE NORMALIZATION
# =============================================================================

def _first_address(entry: dict[str, Any]) -> str | None:

    addresses = entry.get("addresses")

    if isinstance(addresses, list):

        for address in addresses:

            if isinstance(address, str) and address.strip():
                return address.strip()

    for key in ("address", "host", "server", "ip", "domain"):

        value = entry.get(key)

        if isinstance(value, str) and value.strip():
            return value.strip()

    return None


def normalize_entry(
    entry: dict[str, Any],
    region: str,
    provider_domain: str,
) -> dict[str, Any] | None:
    """
    Converts provider response into our internal node structure.

    Internal metadata starts with '_' and is NOT written into the final
    Clash proxy object.
    """

    host = _first_address(entry)

    if host is None:
        return None

    try:
        port = int(entry.get("port"))
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

    provider_name = get_provider_name(provider_domain)

    return {
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
        "_provider": provider_domain,
        "_provider_name": provider_name,

        "_health": {
            "status": "unknown",
            "latency_ms": None,
            "success_rate": None,
            "score": None,
            "checked_at": None,
        },
    }


# =============================================================================
# NODE KEYS
# =============================================================================

def provider_node_key(node: dict[str, Any]) -> tuple[Any, ...]:
    """
    Duplicate key inside one provider.
    """

    return (
        node["type"],
        node["server"],
        node["port"],
        node["username"],
        node["password"],
    )


def node_key(node: dict[str, Any]) -> tuple[Any, ...]:
    """
    Global key. Provider is intentionally included.
    """

    return (
        node["type"],
        node["server"],
        node["port"],
        node["username"],
        node["password"],
        node.get("_provider"),
    )


# =============================================================================
# COLLECT ONE PROVIDER / ONE REGION
# =============================================================================

def collect_provider_region(
    provider_domain: str,
    region: str,
    token_manager: TokenManager,
) -> list[dict[str, Any]]:

    provider_name = get_provider_name(provider_domain)

    print(f"    [{region}] {provider_name}")

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

            node = normalize_entry(
                entry=entry,
                region=region,
                provider_domain=provider_domain,
            )

            if node is None:
                continue

            key = provider_node_key(node)

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
# SORT
# =============================================================================

def sort_nodes(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:

    result = list(nodes)

    result.sort(
        key=lambda node: (
            get_provider_hostname(node.get("_provider", "")),
            node["server"],
            node["port"],
            node["username"],
        )
    )

    return result


def print_provider_counts(
    region: str,
    nodes: list[dict[str, Any]],
) -> None:

    provider_counts: dict[str, int] = {}

    for node in nodes:

        provider = node.get("_provider", "unknown")

        provider_counts[provider] = provider_counts.get(provider, 0) + 1

    print("")
    print(f"[RESULT] {region}: {len(nodes)} unique nodes")

    for provider, count in sorted(
        provider_counts.items(),
        key=lambda item: get_provider_hostname(item[0]),
    ):

        print(
            f"    {get_provider_name(provider):<20} "
            f"{count:>4} nodes"
        )


# =============================================================================
# COLLECT ONE REGION
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

    worker_count = min(MAX_WORKERS, max(1, len(providers)))

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=worker_count
    ) as executor:

        future_map = {
            executor.submit(
                collect_provider_region,
                provider,
                region,
                token_manager,
            ): provider
            for provider in providers
        }

        for future in concurrent.futures.as_completed(future_map):

            provider = future_map[future]

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

    result = sort_nodes(list(all_unique.values()))

    print_provider_counts(region, result)

    return result


# =============================================================================
# COLLECT MANUAL PROVIDERS FOR ONE REGION
# =============================================================================

def collect_manual_providers_for_region(
    region: str,
    token_manager: TokenManager,
) -> list[dict[str, Any]]:
    """
    Fetches providers that are not part of payload.json.
    Called AFTER the regular providers have been collected for the region.
    """

    manual_providers = get_manual_providers()

    if not manual_providers:
        return []

    print("")
    print(f"    [MANUAL] Collecting manual providers for {region}...")

    all_nodes: list[dict[str, Any]] = []

    for provider in manual_providers:

        nodes = collect_provider_region(
            provider_domain=provider,
            region=region,
            token_manager=token_manager,
        )

        all_nodes.extend(nodes)

    return all_nodes


def merge_manual_nodes(
    region: str,
    nodes: list[dict[str, Any]],
    manual_nodes: list[dict[str, Any]],
) -> list[dict[str, Any]]:

    if not manual_nodes:
        return nodes

    combined = {node_key(node): node for node in nodes}

    added = 0

    for node in manual_nodes:

        key = node_key(node)

        if key not in combined:
            combined[key] = node
            added += 1

    result = sort_nodes(list(combined.values()))

    print(f"    [MANUAL] {added} new node(s) merged in for {region}.")

    print_provider_counts(region, result)

    return result


# =============================================================================
# PROXY NAME
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
        .replace(" ", "-")
    )

    return f"{region.upper()}-{index:03d}-{clean_server}"


# =============================================================================
# DNS CONFIG
# =============================================================================

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


# =============================================================================
# CLASH VALIDATION
# =============================================================================

def validate_yaml_structure(config: dict[str, Any]) -> None:
    """
    Structural validation before writing YAML:

      - all proxy names are unique
      - all proxy-group names are unique
      - all group members exist
      - all rule targets exist
      - all groups use select
      - no empty group exists
    """

    proxies = config.get("proxies", [])
    proxy_groups = config.get("proxy-groups", [])
    rules = config.get("rules", [])

    proxy_names: list[str] = []

    for proxy in proxies:

        if not isinstance(proxy, dict):
            raise RuntimeError("Invalid proxy entry.")

        name = proxy.get("name")

        if not isinstance(name, str):
            raise RuntimeError("Proxy name must be a string.")

        proxy_names.append(name)

    if len(proxy_names) != len(set(proxy_names)):
        raise RuntimeError("Duplicate proxy names detected.")

    proxy_name_set = set(proxy_names)

    group_names: list[str] = []

    for group in proxy_groups:

        if not isinstance(group, dict):
            raise RuntimeError("Invalid proxy-group entry.")

        name = group.get("name")
        group_type = group.get("type")
        group_members = group.get("proxies")

        if not isinstance(name, str):
            raise RuntimeError("Proxy-group name must be a string.")

        if group_type != "select":
            raise RuntimeError(
                f"Proxy-group [{name}] must use type=select."
            )

        if not isinstance(group_members, list) or not group_members:
            raise RuntimeError(f"Proxy-group [{name}] is empty.")

        group_names.append(name)

    if len(group_names) != len(set(group_names)):
        raise RuntimeError("Duplicate proxy-group names detected.")

    group_name_set = set(group_names)

    valid_targets = (
        proxy_name_set
        | group_name_set
        | {"DIRECT", "REJECT"}
    )

    for group in proxy_groups:

        for member in group["proxies"]:

            if member not in valid_targets:

                raise RuntimeError(
                    f"Proxy-group [{group['name']}] "
                    f"references missing target [{member}]."
                )

    for rule in rules:

        if not isinstance(rule, str):
            raise RuntimeError("Rule must be a string.")

        parts = rule.split(",")

        if len(parts) < 2:
            continue

        target = parts[-1]

        if target in {"DIRECT", "REJECT"}:
            continue

        if target not in group_name_set:

            raise RuntimeError(
                f"Rule references missing proxy-group [{target}]. "
                f"Rule: {rule}"
            )


# =============================================================================
# BUILD YAML
# =============================================================================

def build_yaml(
    region: str,
    nodes: list[dict[str, Any]],
) -> str:

    if not nodes:
        raise RuntimeError(f"Cannot build YAML for {region}: no nodes.")

    proxies: list[dict[str, Any]] = []

    provider_proxy_names: dict[str, list[str]] = {}

    provider_domains: list[str] = []

    seen_providers: set[str] = set()

    for index, node in enumerate(nodes, start=1):

        proxy_name = make_proxy_name(
            region=region,
            index=index,
            server=node["server"],
        )

        proxies.append(
            {
                "name": proxy_name,
                "type": "http",
                "server": node["server"],
                "port": node["port"],
                "username": node["username"],
                "password": node["password"],
                "tls": True,
                "skip-cert-verify": True,
            }
        )

        provider_domain = node.get("_provider") or "unknown"

        if provider_domain not in seen_providers:
            seen_providers.add(provider_domain)
            provider_domains.append(provider_domain)

        provider_proxy_names.setdefault(provider_domain, []).append(
            proxy_name
        )

    # Priority providers (antpeak.com) always first. sort() is stable, so the
    # relative order of all other providers is preserved.
    provider_domains.sort(
        key=lambda domain: 0 if is_priority_provider(domain) else 1
    )

    provider_group_names: dict[str, str] = {}

    used_group_names: set[str] = set()

    for provider_index, provider_domain in enumerate(provider_domains):

        group_name = get_provider_group_name(
            provider_domain=provider_domain,
            provider_index=provider_index,
        )

        if group_name in used_group_names:

            provider_name = get_provider_name(provider_domain)

            group_name = f"{provider_name} #{provider_index + 1}"

        used_group_names.add(group_name)

        provider_group_names[provider_domain] = group_name

    region_group_name = get_region_group_name(region)

    config: dict[str, Any] = {}

    config.update(DNS_CONFIG)

    config["proxies"] = proxies

    proxy_groups: list[dict[str, Any]] = []

    provider_groups_for_region = [
        provider_group_names[provider_domain]
        for provider_domain in provider_domains
    ]

    if not provider_groups_for_region:
        raise RuntimeError(f"Region [{region}] has no provider groups.")

    proxy_groups.append(
        {
            "name": region_group_name,
            "type": "select",
            "proxies": provider_groups_for_region,
        }
    )

    for provider_domain in provider_domains:

        provider_nodes = provider_proxy_names[provider_domain]

        if not provider_nodes:
            continue

        proxy_groups.append(
            {
                "name": provider_group_names[provider_domain],
                "type": "select",
                "proxies": provider_nodes,
            }
        )

    proxy_groups.append(
        {
            "name": IRAN_DIRECT_GROUP,
            "type": "select",
            "proxies": ["DIRECT"],
        }
    )

    config["proxy-groups"] = proxy_groups

    config["rules"] = [
        f"GEOIP,IR,{IRAN_DIRECT_GROUP}",
        f"MATCH,{region_group_name}",
    ]

    validate_yaml_structure(config)

    return yaml.safe_dump(
        config,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=200,
    )


# =============================================================================
# ATOMIC SAVE
# =============================================================================

def save_subscription(
    region: str,
    nodes: list[dict[str, Any]],
) -> Path:
    """
    Writes the new YAML atomically. If something goes wrong while writing,
    the existing file remains intact.
    """

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    output_path = OUTPUT_DIR / f"{region}.yaml"

    yaml_content = build_yaml(region, nodes)

    temp_path: Path | None = None

    try:

        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=OUTPUT_DIR,
            prefix=f".{region}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:

            temp_file.write(yaml_content)

            temp_file.flush()

            temp_path = Path(temp_file.name)

        os.replace(temp_path, output_path)

        temp_path = None

    finally:

        if temp_path is not None and temp_path.exists():

            try:
                temp_path.unlink()
            except OSError:
                pass

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

    udid = os.getenv("ANTPEAK_UDID")

    if not udid:
        print("[FATAL] ANTPEAK_UDID environment variable is missing.")
        return 1

    try:
        providers = get_free_domains()

    except Exception as exc:
        print(f"[FATAL] Could not load free domains: {exc}")
        return 1

    print("")
    print("Free providers (from payload.json):")

    for provider in providers:
        print(f"  - {get_provider_name(provider)} ({provider})")

    manual_providers = get_manual_providers()

    if manual_providers:

        print("")
        print("Manual providers (not in payload.json yet):")

        for provider in manual_providers:
            print(f"  - {get_provider_name(provider)} ({provider})")

    print("")
    print("Server-list endpoints (in priority order):")

    for kind, path in SERVER_LIST_ENDPOINTS:
        print(f"  - {kind:<7} {path}")

    token_manager = TokenManager(udid)

    try:
        token_manager.get_token()

        # DO NOT print token.
        print("")
        print("[2/5] accessToken successfully obtained.")

    except Exception as exc:
        print(f"[FATAL] Could not obtain accessToken: {exc}")
        return 1

    print("")
    print("[3/5] Collecting servers...")

    generated: dict[str, int] = {}

    failed_regions: list[str] = []

    for region in REGIONS:

        try:

            nodes = collect_region(
                region=region,
                providers=providers,
                token_manager=token_manager,
            )

        except Exception as exc:

            print(f"[ERROR] Region [{region}] failed: {exc}")

            failed_regions.append(region)

            continue

        # Manual providers: a failure here never wipes out nodes already
        # collected above.
        try:

            manual_nodes = collect_manual_providers_for_region(
                region=region,
                token_manager=token_manager,
            )

            nodes = merge_manual_nodes(
                region=region,
                nodes=nodes,
                manual_nodes=manual_nodes,
            )

        except Exception as exc:

            print(
                f"[WARNING] Manual providers failed for {region}: {exc}. "
                f"Continuing with regular providers only."
            )

        # Never overwrite good old data with an empty result.
        if not nodes:

            print(
                f"[WARNING] No nodes found for {region}. "
                f"Existing file will be preserved."
            )

            failed_regions.append(region)

            continue

        try:

            output_path = save_subscription(region=region, nodes=nodes)

        except Exception as exc:

            print(f"[ERROR] Could not save {region}: {exc}")

            failed_regions.append(region)

            continue

        generated[region] = len(nodes)

        print(f"[SAVED] {output_path} ({len(nodes)} nodes)")

    print("")
    print("=" * 80)
    print("[4/5] SUMMARY")
    print("=" * 80)

    if not generated:
        print("[FATAL] No subscription files were generated.")
        return 1

    for region, count in generated.items():

        print(
            f"  {region:<8} "
            f"{REGIONS[region]:<20} "
            f"{count:>4} nodes"
        )

    if failed_regions:

        print("")
        print("[WARNING] The following regions were not updated:")

        for region in failed_regions:
            print(f"  - {region:<8} {REGIONS.get(region, region)}")

        print("Existing files for those regions were preserved.")

    print("=" * 80)
    print("[5/5] DONE")
    print("=" * 80)

    return 0


# =============================================================================
# ENTRY POINT
# =============================================================================

if __name__ == "__main__":
    sys.exit(main())
