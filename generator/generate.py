from __future__ import annotations

import concurrent.futures
import os
import sys
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
# COLLECTION CONFIG
# =============================================================================

# Maximum number of requests to each provider for each region.
MAX_REQUESTS_PER_PROVIDER_REGION = 20

# Stop querying a provider when this many consecutive requests
# return zero NEW nodes.
STOP_AFTER_NO_NEW = 5

DELAY_BETWEEN_CALLS = 0.4

HTTP_TIMEOUT = 15

# Number of provider/region jobs executed simultaneously.
MAX_WORKERS = 12


# =============================================================================
# OUTPUT
# =============================================================================

OUTPUT_DIR = (
    Path(__file__).resolve().parent.parent / "subscriptions"
)


# =============================================================================
# HTTP
# =============================================================================

COMMON_HEADERS = {
    "accept": "application/json",
    "accept-encoding": "gzip, deflate, br",
    "content-type": "application/json",
    "user-agent": PLATFORM_VERSION,
    "origin": (
        "chrome-extension://"
        "majdfhpaihoncoakbjgbdhglocklcgno"
    ),
}


# =============================================================================
# PROVIDER ICONS
# =============================================================================

# Explicit provider -> emoji overrides.
#
# The key is the first hostname component.
#
# Example:
#     https://bitphox.com
#
# becomes:
#     bitphox
#
# Then the icon below is used.
PROVIDER_EMOJIS = {
    "bitphox": "⚡",
}


# Fallback icons for unknown providers.
DEFAULT_PROVIDER_EMOJIS = [
    "🚀",
    "🛰️",
    "🌐",
    "🔥",
    "💎",
    "🦊",
    "☁️",
    "🔷",
    "🎯",
    "🛡️",
]


# Keyword based icon detection.
#
# This gives a more meaningful icon even when the provider
# is not explicitly listed in PROVIDER_EMOJIS.
PROVIDER_ICON_KEYWORDS = {
    "speed": "⚡",
    "fast": "⚡",
    "rapid": "⚡",
    "turbo": "🚀",
    "rocket": "🚀",
    "cloud": "☁️",
    "sky": "☁️",
    "proxy": "🌐",
    "node": "🛰️",
    "server": "🖥️",
    "net": "🌐",
    "vpn": "🛡️",
    "secure": "🛡️",
    "shield": "🛡️",
    "fire": "🔥",
    "star": "⭐",
    "diamond": "💎",
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

        domain = domain.strip().rstrip("/")

        if domain and domain not in seen:
            seen.add(domain)
            result.append(domain)

    return result


# =============================================================================
# PROVIDER NAME HELPERS
# =============================================================================

def get_provider_host(provider_domain: str) -> str:
    """
    Extracts normalized hostname from provider URL.

    Example:
        https://bitphox.example.com/
        -> bitphox.example.com
    """

    normalized = provider_domain.strip()

    if not normalized.startswith(
        ("http://", "https://")
    ):
        normalized = f"https://{normalized}"

    parsed = urlparse(normalized)

    hostname = parsed.hostname

    if not hostname:
        return provider_domain.strip().rstrip("/")

    hostname = hostname.lower().strip()

    if hostname.startswith("www."):
        hostname = hostname[4:]

    return hostname


def get_provider_key(provider_domain: str) -> str:
    """
    Returns the first hostname component as a stable provider key.

    Example:
        bitphox.com
        -> bitphox
    """

    hostname = get_provider_host(provider_domain)

    first_component = hostname.split(".")[0]

    return (
        first_component
        .strip()
        .replace("-", "_")
        .lower()
    )


def get_provider_display_name(
    provider_domain: str,
) -> str:
    """
    Converts provider hostname into a readable display name.

    Examples:
        bitphox.com
            -> Bitphox

        my-proxy.net
            -> My Proxy
    """

    provider_key = get_provider_key(provider_domain)

    if not provider_key:
        return "Provider"

    words = provider_key.replace(
        "_",
        " ",
    ).split()

    return " ".join(
        word.capitalize()
        for word in words
    )


def get_provider_emoji(
    provider_domain: str,
    fallback_index: int,
) -> str:
    """
    Selects the best available emoji for a provider.

    Priority:
        1. Explicit provider mapping
        2. Keyword detection
        3. Rotating fallback icon
    """

    provider_key = get_provider_key(
        provider_domain
    )

    # Explicit mapping.
    explicit = PROVIDER_EMOJIS.get(
        provider_key
    )

    if explicit:
        return explicit

    # Keyword based mapping.
    for keyword, emoji in PROVIDER_ICON_KEYWORDS.items():
        if keyword in provider_key:
            return emoji

    # Fallback.
    return DEFAULT_PROVIDER_EMOJIS[
        fallback_index % len(
            DEFAULT_PROVIDER_EMOJIS
        )
    ]


def make_unique_group_name(
    base_name: str,
    used_names: set[str],
) -> str:
    """
    Makes proxy-group name unique.

    Example:
        "⚡ Bitphox"
        "⚡ Bitphox 2"
        "⚡ Bitphox 3"
    """

    if base_name not in used_names:
        used_names.add(base_name)
        return base_name

    suffix = 2

    while True:
        candidate = (
            f"{base_name} {suffix}"
        )

        if candidate not in used_names:
            used_names.add(candidate)
            return candidate

        suffix += 1


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

    api_url = (
        f"{provider_domain}/api/server/list/"
    )

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

        headers["authorization"] = (
            f"Bearer {token}"
        )

        try:
            response = requests.post(
                api_url,
                headers=headers,
                json=payload,
                timeout=HTTP_TIMEOUT,
            )

            # Token probably expired.
            if (
                response.status_code == 401
                and attempt == 0
            ):
                print(
                    f"      [TOKEN] 401 from "
                    f"{provider_domain} / {region}, "
                    f"refreshing token..."
                )
                continue

            response.raise_for_status()

            data = response.json()

            if not data.get("success"):
                return []

            entries = data.get(
                "data",
                [],
            )

            if not isinstance(entries, list):
                return []

            return entries

        except requests.RequestException as exc:
            print(
                f"      [ERROR] "
                f"{provider_domain} / {region}: {exc}"
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

def normalize_entry(
    entry: dict[str, Any],
    region: str,
    provider_domain: str,
    provider_index: int,
) -> dict[str, Any] | None:
    """
    Converts provider response into the minimum information required
    to create a Clash HTTP proxy.

    Internal metadata fields beginning with "_" are used only during
    generation and are NOT written into the final YAML proxy object.
    """

    addresses = entry.get(
        "addresses"
    )

    if (
        not isinstance(addresses, list)
        or not addresses
    ):
        return None

    host = addresses[0]

    if (
        not isinstance(host, str)
        or not host.strip()
    ):
        return None

    host = host.strip()

    port = entry.get("port")

    try:
        port = int(port)
    except (TypeError, ValueError):
        return None

    if port <= 0:
        return None

    username = entry.get(
        "username"
    )

    password = entry.get(
        "password"
    )

    if (
        not isinstance(username, str)
        or not username
    ):
        return None

    if (
        not isinstance(password, str)
        or not password
    ):
        return None

    protocol = (
        entry.get("protocol")
        or "https"
    )

    if protocol != "https":
        return None

    return {
        "name": "",
        "type": "http",
        "server": host,
        "port": port,
        "username": username,
        "password": password,
        "tls": True,
        "skip-cert-verify": True,

        # Internal metadata.
        "_region": region,
        "_region_name": REGIONS.get(
            region,
            region,
        ),
        "_provider": provider_domain,
        "_provider_index": provider_index,
    }


# =============================================================================
# UNIQUE KEY
# =============================================================================

def node_key(
    node: dict[str, Any],
) -> tuple[Any, ...]:
    """
    Deduplicates a node inside its provider identity.

    Provider is intentionally part of the key.

    Therefore, if exactly the same endpoint appears under
    two different providers, it remains available in BOTH
    provider groups.
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
# COLLECT FROM ONE PROVIDER / ONE REGION
# =============================================================================

def collect_provider_region(
    provider_domain: str,
    provider_index: int,
    region: str,
    token_manager: TokenManager,
) -> list[dict[str, Any]]:

    provider_name = (
        provider_domain
        .replace(
            "https://",
            "",
        )
        .replace(
            "http://",
            "",
        )
        .rstrip("/")
    )

    print(
        f"    [{region}] {provider_name}"
    )

    unique: dict[
        tuple[Any, ...],
        dict[str, Any],
    ] = {}

    no_new_streak = 0

    for call_number in range(
        1,
        MAX_REQUESTS_PER_PROVIDER_REGION + 1,
    ):

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
                provider_index=provider_index,
            )

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

        if (
            no_new_streak
            >= STOP_AFTER_NO_NEW
        ):
            break

        if (
            call_number
            < MAX_REQUESTS_PER_PROVIDER_REGION
        ):
            time.sleep(
                DELAY_BETWEEN_CALLS
            )

    return list(
        unique.values()
    )


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
    print(
        f"REGION: {region} "
        f"({REGIONS[region]})"
    )
    print("=" * 80)

    all_unique: dict[
        tuple[Any, ...],
        dict[str, Any],
    ] = {}

    tasks = [
        (
            provider,
            index,
            region,
        )
        for index, provider in enumerate(
            providers
        )
    ]

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(
            MAX_WORKERS,
            len(tasks),
        )
    ) as executor:

        future_map = {
            executor.submit(
                collect_provider_region,
                provider,
                provider_index,
                region,
                token_manager,
            ): (
                provider,
                provider_index,
                region,
            )
            for provider, provider_index, region
            in tasks
        }

        for future in concurrent.futures.as_completed(
            future_map
        ):

            provider, provider_index, _ = (
                future_map[future]
            )

            try:
                nodes = future.result()

            except Exception as exc:
                print(
                    f"    [ERROR] Worker failed "
                    f"for {provider}: {exc}"
                )
                continue

            for node in nodes:

                key = node_key(node)

                if key not in all_unique:
                    all_unique[key] = node

    # Stable sorting:
    #
    # 1. Provider order from payload.json
    # 2. Server
    # 3. Port
    # 4. Username
    result = list(
        all_unique.values()
    )

    result.sort(
        key=lambda x: (
            x.get(
                "_provider_index",
                999999,
            ),
            x["server"],
            x["port"],
            x["username"],
        )
    )

    print("")
    print(
        f"[RESULT] {region}: "
        f"{len(result)} unique nodes"
    )

    # Provider summary.
    provider_counts: dict[str, int] = {}

    for node in result:
        provider = node.get(
            "_provider",
            "unknown",
        )

        provider_counts[provider] = (
            provider_counts.get(
                provider,
                0,
            )
            + 1
        )

    print("")
    print("Provider groups:")

    for provider in providers:
        count = provider_counts.get(
            provider,
            0,
        )

        if count > 0:
            print(
                f"    {get_provider_display_name(provider):<25}"
                f" {count:>4} nodes"
            )

    return result


# =============================================================================
# PROXY NAMES
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

    return (
        f"{region.upper()}-"
        f"{index:03d}-"
        f"{clean_server}"
    )


# =============================================================================
# DNS
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
# YAML GENERATION
# =============================================================================

def build_yaml(
    region: str,
    nodes: list[dict[str, Any]],
) -> str:
    """
    Builds a manual, provider-grouped Clash configuration.

    Structure:

        🎯 Proxy
          ├── ⚡ Provider A
          │     ├── NODE 1
          │     ├── NODE 2
          │     └── NODE 3
          │
          ├── 🚀 Provider B
          │     ├── NODE 4
          │     └── NODE 5
          │
          └── 🌐 Provider C
                ├── NODE 6
                └── NODE 7

    There is NO url-test / fallback / load-balance.
    Everything is manually selectable.
    """

    # -------------------------------------------------------------------------
    # PROXIES
    # -------------------------------------------------------------------------

    proxies: list[dict[str, Any]] = []

    # provider_domain -> proxy names
    provider_groups: dict[
        str,
        list[str],
    ] = {}

    # provider order
    provider_order: dict[
        str,
        int,
    ] = {}

    # -------------------------------------------------------------------------
    # CREATE PROXY DEFINITIONS
    # -------------------------------------------------------------------------

    for index, node in enumerate(
        nodes,
        start=1,
    ):

        name = make_proxy_name(
            region=region,
            index=index,
            server=node["server"],
        )

        proxy = {
            "name": name,
            "type": "http",
            "server": node["server"],
            "port": node["port"],
            "username": node["username"],
            "password": node["password"],
            "tls": True,
            "skip-cert-verify": True,
        }

        proxies.append(proxy)

        provider = node.get(
            "_provider",
            "unknown",
        )

        provider_groups.setdefault(
            provider,
            [],
        ).append(name)

        provider_order[
            provider
        ] = node.get(
            "_provider_index",
            999999,
        )

    # -------------------------------------------------------------------------
    # SORT PROVIDERS BY ORIGINAL PAYLOAD ORDER
    # -------------------------------------------------------------------------

    ordered_providers = sorted(
        provider_groups.keys(),
        key=lambda provider: (
            provider_order.get(
                provider,
                999999,
            ),
            provider,
        ),
    )

    # -------------------------------------------------------------------------
    # PROVIDER DISPLAY NAMES
    # -------------------------------------------------------------------------

    provider_group_names: dict[
        str,
        str,
    ] = {}

    used_group_names: set[str] = set()

    for emoji_index, provider in enumerate(
        ordered_providers
    ):

        display_name = (
            get_provider_display_name(
                provider
            )
        )

        emoji = get_provider_emoji(
            provider_domain=provider,
            fallback_index=emoji_index,
        )

        base_name = (
            f"{emoji} {display_name}"
        )

        unique_name = make_unique_group_name(
            base_name=base_name,
            used_names=used_group_names,
        )

        provider_group_names[
            provider
        ] = unique_name

    # -------------------------------------------------------------------------
    # CONFIG
    # -------------------------------------------------------------------------

    config: dict[str, Any] = {}

    config.update(
        DNS_CONFIG
    )

    config["proxies"] = proxies

    # -------------------------------------------------------------------------
    # PROXY GROUPS
    # -------------------------------------------------------------------------

    proxy_groups: list[
        dict[str, Any]
    ] = []

    # -------------------------------------------------------------------------
    # MAIN MANUAL GROUP
    # -------------------------------------------------------------------------
    #
    # IMPORTANT:
    # This group contains ONLY provider groups.
    #
    # Therefore all nodes are NOT flattened into one big list.
    #
    # Example:
    #
    # 🎯 Proxy
    #   ├── ⚡ Bitphox
    #   ├── 🚀 Provider A
    #   └── 🌐 Provider B
    #
    # The user manually chooses the provider group here.
    #

    main_group_proxies = [
        provider_group_names[
            provider
        ]
        for provider in ordered_providers
    ]

    # Always allow direct traffic manually.
    main_group_proxies.append(
        "DIRECT"
    )

    proxy_groups.append(
        {
            "name": "🎯 Proxy",
            "type": "select",
            "proxies": main_group_proxies,
        }
    )

    # -------------------------------------------------------------------------
    # ONE SELECT GROUP PER PROVIDER
    # -------------------------------------------------------------------------
    #
    # Each provider group contains ONLY the nodes originating
    # from that provider.
    #
    # Example:
    #
    # ⚡ Bitphox
    #   ├── NL-001-...
    #   ├── NL-002-...
    #   └── NL-003-...
    #

    for provider in ordered_providers:

        group_name = provider_group_names[
            provider
        ]

        proxy_groups.append(
            {
                "name": group_name,
                "type": "select",
                "proxies": provider_groups[
                    provider
                ],
            }
        )

    # -------------------------------------------------------------------------
    # IRAN DIRECT
    # -------------------------------------------------------------------------

    proxy_groups.append(
        {
            "name": "🇮🇷 Iran-Direct",
            "type": "select",
            "proxies": [
                "DIRECT",
            ],
        }
    )

    config["proxy-groups"] = (
        proxy_groups
    )

    # -------------------------------------------------------------------------
    # RULES
    # -------------------------------------------------------------------------

    config["rules"] = [
        "GEOIP,IR,Iran-Direct",
        "MATCH,🎯 Proxy",
    ]

    # -------------------------------------------------------------------------
    # YAML SERIALIZATION
    # -------------------------------------------------------------------------

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
    region: str,
    nodes: list[dict[str, Any]],
) -> Path:

    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    output_path = (
        OUTPUT_DIR / f"{region}.yaml"
    )

    yaml_content = build_yaml(
        region,
        nodes,
    )

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
    print(
        "AUTOMATED CLASH SUBSCRIPTION GENERATOR"
    )
    print("=" * 80)
    print("")

    # -------------------------------------------------------------------------
    # UDID
    # -------------------------------------------------------------------------

    udid = os.getenv(
        "ANTPEAK_UDID"
    )

    if not udid:
        print(
            "[FATAL] "
            "ANTPEAK_UDID environment variable is missing."
        )
        return 1

    # -------------------------------------------------------------------------
    # PROVIDER LIST
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

    for index, provider in enumerate(
        providers
    ):
        print(
            f"  [{index:02d}] "
            f"{provider}"
        )

    # -------------------------------------------------------------------------
    # TOKEN
    # -------------------------------------------------------------------------

    token_manager = TokenManager(
        udid
    )

    try:
        token_manager.get_token()

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
    # REGIONS
    # -------------------------------------------------------------------------

    print("")
    print(
        "[3/5] Collecting servers..."
    )

    generated: dict[
        str,
        int,
    ] = {}

    for region in REGIONS:

        nodes = collect_region(
            region=region,
            providers=providers,
            token_manager=token_manager,
        )

        # Important:
        #
        # Never overwrite a previously generated good file
        # with an empty result.
        #
        if not nodes:
            print(
                f"[WARNING] No nodes found for {region}. "
                f"Existing file will be preserved."
            )
            continue

        output_path = save_subscription(
            region=region,
            nodes=nodes,
        )

        generated[
            region
        ] = len(nodes)

        print(
            f"[SAVED] {output_path} "
            f"({len(nodes)} nodes)"
        )

    # -------------------------------------------------------------------------
    # SUMMARY
    # -------------------------------------------------------------------------

    print("")
    print("=" * 80)
    print("[4/5] SUMMARY")
    print("=" * 80)

    if not generated:

        print(
            "[FATAL] No subscription files were generated."
        )

        return 1

    for region, count in (
        generated.items()
    ):

        print(
            f"  {region:<8} "
            f"{REGIONS[region]:<18} "
            f"{count:>4} nodes"
        )

    print("=" * 80)
    print("[5/5] DONE")
    print("=" * 80)

    return 0


if __name__ == "__main__":
    sys.exit(main())