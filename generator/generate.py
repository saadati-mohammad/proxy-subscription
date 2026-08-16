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

# IMPORTANT:
# These names are used directly in BOTH:
#   1. proxy-groups
#   2. rules
#
# Therefore there is no chance of:
#
#   group = "🇮🇷 Iran Direct"
#   rule  = "Iran-Direct"
#
# =============================================================================

IRAN_DIRECT_GROUP = "🇮🇷 Iran Direct"


# =============================================================================
# PROVIDER DISPLAY CONFIG
# =============================================================================

# Explicit names for known providers.
#
# If a provider does not exist here, its hostname is automatically converted
# into a readable name.
#
# Example:
#
#   https://bitphox.com/
#       -> Bitphox
#
#   https://foo-bar.com/
#       -> Foo Bar
#
PROVIDER_NAME_OVERRIDES = {
    "bitphox.com": "Bitphox",
    "tronyza.com": "Tronyza",
    "tronlit.com": "Tronlit",
    "freloop.com": "Freloop",
    "hisball.com": "Hisball",
    "hibchr.com": "Hibchr",
}


# Provider-specific stickers / emojis.
PROVIDER_EMOJIS = {
    "bitphox.com": "⚡",
    "tronyza.com": "🔥",
    "tronlit.com": "🚀",
    "freloop.com": "🌐",
    "hisball.com": "🛰️",
    "hibchr.com": "💎",
}


# Fallback list for providers that are not explicitly configured above.
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

    def get_token(
        self,
        force_refresh: bool = False,
    ) -> str:

        with self._lock:

            if self._token is None or force_refresh:
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

    if (
        not isinstance(free_domains, list)
        or not free_domains
    ):
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

def get_provider_hostname(
    provider_domain: str,
) -> str:
    """
    Returns normalized provider hostname.

    Example:

        https://bitphox.com/
        -> bitphox.com
    """

    parsed = urlparse(provider_domain)

    hostname = parsed.hostname

    if not hostname:
        hostname = provider_domain

    hostname = (
        hostname
        .strip()
        .lower()
    )

    if hostname.startswith("www."):
        hostname = hostname[4:]

    return hostname


def get_provider_name(
    provider_domain: str,
) -> str:
    """
    Converts provider hostname into readable display name.
    """

    hostname = get_provider_hostname(
        provider_domain
    )

    override = PROVIDER_NAME_OVERRIDES.get(
        hostname
    )

    if override:
        return override

    parts = hostname.split(".")

    if len(parts) >= 2:
        name = parts[-2]
    else:
        name = parts[0]

    name = (
        name
        .replace("-", " ")
        .replace("_", " ")
        .strip()
    )

    return name.title()


def get_provider_emoji(
    provider_domain: str,
    provider_index: int,
) -> str:
    """
    Returns the display sticker for the provider.
    """

    hostname = get_provider_hostname(
        provider_domain
    )

    explicit = PROVIDER_EMOJIS.get(
        hostname
    )

    if explicit:
        return explicit

    return DEFAULT_PROVIDER_EMOJIS[
        provider_index % len(
            DEFAULT_PROVIDER_EMOJIS
        )
    ]


def get_provider_group_name(
    provider_domain: str,
    provider_index: int,
) -> str:
    """
    Returns the exact Clash proxy-group name.
    """

    emoji = get_provider_emoji(
        provider_domain=provider_domain,
        provider_index=provider_index,
    )

    provider_name = get_provider_name(
        provider_domain
    )

    return f"{emoji} {provider_name}"


def get_region_group_name(
    region: str,
) -> str:
    """
    Returns the exact main group name for a region.
    """

    emoji = REGION_EMOJIS.get(
        region,
        "🌍",
    )

    region_name = REGIONS.get(
        region,
        region,
    )

    return f"{emoji} {region_name}"


# =============================================================================
# PROVIDER REQUEST
# =============================================================================

def fetch_provider_region(
    provider_domain: str,
    region: str,
    token_manager: TokenManager,
) -> list[dict[str, Any]]:

    api_url = (
        f"{provider_domain}"
        "/api/server/list/"
    )

    payload = {
        "protocol": "https",
        "region": region,
        "type": 0,
    }

    for attempt in range(2):

        token = token_manager.get_token(
            force_refresh=(
                attempt == 1
            )
        )

        headers = dict(
            COMMON_HEADERS
        )

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

            if not isinstance(
                entries,
                list,
            ):
                return []

            return entries

        except requests.RequestException as exc:

            print(
                f"      [ERROR] "
                f"{provider_domain} / {region}: "
                f"{exc}"
            )

            return []

        except ValueError as exc:

            print(
                f"      [ERROR] Invalid JSON from "
                f"{provider_domain} / {region}: "
                f"{exc}"
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
) -> dict[str, Any] | None:
    """
    Converts provider response into our internal node structure.

    Internal metadata starts with '_'.

    These fields are NOT written into the final Clash proxy object.
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

    port = entry.get(
        "port"
    )

    try:
        port = int(port)

    except (
        TypeError,
        ValueError,
    ):
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

    provider_name = get_provider_name(
        provider_domain
    )

    return {
        # ---------------------------------------------------------------------
        # Clash fields
        # ---------------------------------------------------------------------

        "name": "",
        "type": "http",
        "server": host,
        "port": port,
        "username": username,
        "password": password,
        "tls": True,
        "skip-cert-verify": True,

        # ---------------------------------------------------------------------
        # Internal metadata
        # ---------------------------------------------------------------------

        "_region": region,

        "_region_name": REGIONS.get(
            region,
            region,
        ),

        "_provider": provider_domain,

        "_provider_name": provider_name,

        # Future-ready health metadata.
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

def provider_node_key(
    node: dict[str, Any],
) -> tuple[Any, ...]:
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


def node_key(
    node: dict[str, Any],
) -> tuple[Any, ...]:
    """
    Global key.

    Provider is intentionally included.

    This means the same endpoint returned by two different providers
    remains associated with both provider groups.
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

    provider_name = get_provider_name(
        provider_domain
    )

    print(
        f"    [{region}] "
        f"{provider_name}"
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

            if not isinstance(
                entry,
                dict,
            ):
                continue

            node = normalize_entry(
                entry=entry,
                region=region,
                provider_domain=provider_domain,
            )

            if node is None:
                continue

            key = provider_node_key(
                node
            )

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
# COLLECT ONE REGION
# =============================================================================

def collect_region(
    region: str,
    providers: list[str],
    token_manager: TokenManager,
) -> list[dict[str, Any]]:

    print("")
    print("=" * 80)

    print(
        f"REGION: "
        f"{region} "
        f"({REGIONS[region]})"
    )

    print("=" * 80)

    all_unique: dict[
        tuple[Any, ...],
        dict[str, Any],
    ] = {}

    tasks: list[
        tuple[str, str]
    ] = []

    for provider in providers:

        tasks.append(
            (
                provider,
                region,
            )
        )

    worker_count = min(
        MAX_WORKERS,
        max(1, len(tasks)),
    )

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=worker_count
    ) as executor:

        future_map = {
            executor.submit(
                collect_provider_region,
                provider,
                region,
                token_manager,
            ): (
                provider,
                region,
            )

            for provider, region
            in tasks
        }

        for future in concurrent.futures.as_completed(
            future_map
        ):

            provider, _ = future_map[
                future
            ]

            try:

                nodes = future.result()

            except Exception as exc:

                print(
                    f"    [ERROR] "
                    f"Worker failed for "
                    f"{provider}: "
                    f"{exc}"
                )

                continue

            for node in nodes:

                key = node_key(
                    node
                )

                if key not in all_unique:

                    all_unique[key] = node

    # =========================================================================
    # DETERMINISTIC SORT
    # =========================================================================

    result = list(
        all_unique.values()
    )

    result.sort(
        key=lambda node: (
            get_provider_hostname(
                node.get(
                    "_provider",
                    "",
                )
            ),

            node["server"],

            node["port"],

            node["username"],
        )
    )

    # =========================================================================
    # SUMMARY
    # =========================================================================

    provider_counts: dict[
        str,
        int,
    ] = {}

    for node in result:

        provider = node.get(
            "_provider",
            "unknown",
        )

        provider_counts[
            provider
        ] = (
            provider_counts.get(
                provider,
                0,
            )
            + 1
        )

    print("")

    print(
        f"[RESULT] "
        f"{region}: "
        f"{len(result)} unique nodes"
    )

    for provider, count in sorted(
        provider_counts.items(),
        key=lambda item: (
            get_provider_hostname(
                item[0]
            )
        ),
    ):

        print(
            f"    "
            f"{get_provider_name(provider):<20} "
            f"{count:>4} nodes"
        )

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

    return (
        f"{region.upper()}-"
        f"{index:03d}-"
        f"{clean_server}"
    )


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

def validate_yaml_structure(
    config: dict[str, Any],
) -> None:
    """
    Performs structural validation before writing YAML.

    Validates:

      - all proxy names are unique
      - all proxy-group names are unique
      - all group members exist
      - all rule targets exist
      - all groups use select
      - no empty provider group exists
    """

    proxies = config.get(
        "proxies",
        [],
    )

    proxy_groups = config.get(
        "proxy-groups",
        [],
    )

    rules = config.get(
        "rules",
        [],
    )

    # =========================================================================
    # PROXY NAMES
    # =========================================================================

    proxy_names: list[str] = []

    for proxy in proxies:

        if not isinstance(
            proxy,
            dict,
        ):
            raise RuntimeError(
                "Invalid proxy entry."
            )

        name = proxy.get(
            "name"
        )

        if not isinstance(
            name,
            str,
        ):
            raise RuntimeError(
                "Proxy name must be a string."
            )

        proxy_names.append(
            name
        )

    if len(proxy_names) != len(
        set(proxy_names)
    ):
        raise RuntimeError(
            "Duplicate proxy names detected."
        )

    proxy_name_set = set(
        proxy_names
    )

    # =========================================================================
    # GROUP NAMES
    # =========================================================================

    group_names: list[str] = []

    for group in proxy_groups:

        if not isinstance(
            group,
            dict,
        ):
            raise RuntimeError(
                "Invalid proxy-group entry."
            )

        name = group.get(
            "name"
        )

        group_type = group.get(
            "type"
        )

        group_members = group.get(
            "proxies"
        )

        if not isinstance(
            name,
            str,
        ):
            raise RuntimeError(
                "Proxy-group name must be a string."
            )

        if group_type != "select":
            raise RuntimeError(
                f"Proxy-group [{name}] "
                "must use type=select."
            )

        if (
            not isinstance(
                group_members,
                list,
            )
            or not group_members
        ):
            raise RuntimeError(
                f"Proxy-group [{name}] "
                "is empty."
            )

        group_names.append(
            name
        )

    if len(group_names) != len(
        set(group_names)
    ):
        raise RuntimeError(
            "Duplicate proxy-group names detected."
        )

    group_name_set = set(
        group_names
    )

    # =========================================================================
    # GROUP MEMBERS
    # =========================================================================

    valid_targets = (
        proxy_name_set
        | group_name_set
        | {
            "DIRECT",
            "REJECT",
        }
    )

    for group in proxy_groups:

        group_name = group[
            "name"
        ]

        members = group[
            "proxies"
        ]

        for member in members:

            if member not in valid_targets:

                raise RuntimeError(
                    f"Proxy-group "
                    f"[{group_name}] "
                    f"references missing target "
                    f"[{member}]."
                )

    # =========================================================================
    # RULE TARGETS
    # =========================================================================

    for rule in rules:

        if not isinstance(
            rule,
            str,
        ):
            raise RuntimeError(
                "Rule must be a string."
            )

        parts = rule.split(",")

        if len(parts) < 2:
            continue

        target = parts[-1]

        if target in {
            "DIRECT",
            "REJECT",
        }:
            continue

        if target not in group_name_set:

            raise RuntimeError(
                "Rule references missing "
                f"proxy-group [{target}]. "
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
        raise RuntimeError(
            f"Cannot build YAML for "
            f"{region}: no nodes."
        )

    # =========================================================================
    # PROXIES
    # =========================================================================

    proxies: list[
        dict[str, Any]
    ] = []

    # provider_domain -> proxy names
    provider_proxy_names: dict[
        str,
        list[str],
    ] = {}

    # Keep provider order deterministic.
    provider_domains: list[
        str
    ] = []

    seen_providers: set[str] = set()

    for index, node in enumerate(
        nodes,
        start=1,
    ):

        proxy_name = make_proxy_name(
            region=region,
            index=index,
            server=node[
                "server"
            ],
        )

        proxy = {
            "name": proxy_name,

            "type": "http",

            "server": node[
                "server"
            ],

            "port": node[
                "port"
            ],

            "username": node[
                "username"
            ],

            "password": node[
                "password"
            ],

            "tls": True,

            "skip-cert-verify": True,
        }

        proxies.append(
            proxy
        )

        provider_domain = (
            node.get(
                "_provider"
            )
            or "unknown"
        )

        if provider_domain not in (
            seen_providers
        ):

            seen_providers.add(
                provider_domain
            )

            provider_domains.append(
                provider_domain
            )

        provider_proxy_names.setdefault(
            provider_domain,
            [],
        ).append(
            proxy_name
        )

    # =========================================================================
    # PROVIDER GROUP NAMES
    # =========================================================================

    provider_group_names: dict[
        str,
        str,
    ] = {}

    used_group_names: set[str] = set()

    for provider_index, provider_domain in enumerate(
        provider_domains
    ):

        group_name = (
            get_provider_group_name(
                provider_domain=provider_domain,
                provider_index=provider_index,
            )
        )

        # Collision protection.
        if group_name in used_group_names:

            provider_name = get_provider_name(
                provider_domain
            )

            group_name = (
                f"{provider_name} "
                f"#{provider_index + 1}"
            )

        used_group_names.add(
            group_name
        )

        provider_group_names[
            provider_domain
        ] = group_name

    # =========================================================================
    # MAIN REGION GROUP
    # =========================================================================

    region_group_name = (
        get_region_group_name(
            region
        )
    )

    # =========================================================================
    # CONFIG
    # =========================================================================

    config: dict[str, Any] = {}

    config.update(
        DNS_CONFIG
    )

    config[
        "proxies"
    ] = proxies

    # =========================================================================
    # PROXY GROUPS
    # =========================================================================

    proxy_groups: list[
        dict[str, Any]
    ] = []

    # -------------------------------------------------------------------------
    # REGION MAIN SELECT GROUP
    # -------------------------------------------------------------------------
    #
    # Example:
    #
    # 🇳🇱 Amsterdam
    #   -> ⚡ Bitphox
    #   -> 🔥 Tronyza
    #   -> 🚀 Tronlit
    #
    # User manually chooses a provider.
    #
    # -------------------------------------------------------------------------

    provider_groups_for_region = [
        provider_group_names[
            provider_domain
        ]

        for provider_domain
        in provider_domains
    ]

    if not provider_groups_for_region:

        raise RuntimeError(
            f"Region [{region}] has no provider groups."
        )

    proxy_groups.append(
        {
            "name": region_group_name,

            "type": "select",

            "proxies":
                provider_groups_for_region,
        }
    )

    # -------------------------------------------------------------------------
    # PROVIDER GROUPS
    # -------------------------------------------------------------------------

    for provider_domain in provider_domains:

        provider_group_name = (
            provider_group_names[
                provider_domain
            ]
        )

        provider_nodes = (
            provider_proxy_names[
                provider_domain
            ]
        )

        if not provider_nodes:
            continue

        proxy_groups.append(
            {
                "name": provider_group_name,

                "type": "select",

                "proxies":
                    provider_nodes,
            }
        )

    # -------------------------------------------------------------------------
    # IRAN DIRECT
    # -------------------------------------------------------------------------

    proxy_groups.append(
        {
            "name": IRAN_DIRECT_GROUP,

            "type": "select",

            "proxies": [
                "DIRECT",
            ],
        }
    )

    config[
        "proxy-groups"
    ] = proxy_groups

    # =========================================================================
    # RULES
    # =========================================================================
    #
    # EXACT group names are referenced.
    #
    # Example:
    #
    # GEOIP,IR,🇮🇷 Iran Direct
    # MATCH,🇳🇱 Amsterdam
    #
    # =========================================================================

    config[
        "rules"
    ] = [
        f"GEOIP,IR,{IRAN_DIRECT_GROUP}",
        f"MATCH,{region_group_name}",
    ]

    # =========================================================================
    # VALIDATE
    # =========================================================================

    validate_yaml_structure(
        config
    )

    # =========================================================================
    # YAML SERIALIZATION
    # =========================================================================

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
    Writes the new YAML atomically.

    If something goes wrong while writing, the existing file remains intact.
    """

    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    output_path = (
        OUTPUT_DIR
        / f"{region}.yaml"
    )

    yaml_content = build_yaml(
        region,
        nodes,
    )

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

            temp_file.write(
                yaml_content
            )

            temp_file.flush()

            temp_path = Path(
                temp_file.name
            )

        # Atomic replacement.
        os.replace(
            temp_path,
            output_path,
        )

        temp_path = None

    finally:

        if (
            temp_path is not None
            and temp_path.exists()
        ):

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
    print(
        "AUTOMATED CLASH "
        "SUBSCRIPTION GENERATOR"
    )
    print("=" * 80)
    print("")

    # =========================================================================
    # UDID
    # =========================================================================

    udid = os.getenv(
        "ANTPEAK_UDID"
    )

    if not udid:

        print(
            "[FATAL] "
            "ANTPEAK_UDID environment variable "
            "is missing."
        )

        return 1

    # =========================================================================
    # PROVIDERS
    # =========================================================================

    try:

        providers = get_free_domains()

    except Exception as exc:

        print(
            f"[FATAL] "
            f"Could not load free domains: "
            f"{exc}"
        )

        return 1

    print("")
    print("Free providers:")

    for provider in providers:

        print(
            f"  - "
            f"{get_provider_name(provider)} "
            f"({provider})"
        )

    # =========================================================================
    # TOKEN
    # =========================================================================

    token_manager = TokenManager(
        udid
    )

    try:

        token_manager.get_token()

        # DO NOT print token.

        print("")
        print(
            "[2/5] "
            "accessToken successfully obtained."
        )

    except Exception as exc:

        print(
            f"[FATAL] "
            f"Could not obtain accessToken: "
            f"{exc}"
        )

        return 1

    # =========================================================================
    # REGIONS
    # =========================================================================

    print("")
    print(
        "[3/5] "
        "Collecting servers..."
    )

    generated: dict[
        str,
        int,
    ] = {}

    failed_regions: list[
        str
    ] = []

    for region in REGIONS:

        try:

            nodes = collect_region(
                region=region,
                providers=providers,
                token_manager=token_manager,
            )

        except Exception as exc:

            print(
                f"[ERROR] "
                f"Region [{region}] failed: "
                f"{exc}"
            )

            failed_regions.append(
                region
            )

            continue

        # Never overwrite good old data with an empty result.
        if not nodes:

            print(
                f"[WARNING] "
                f"No nodes found for {region}. "
                f"Existing file will be preserved."
            )

            failed_regions.append(
                region
            )

            continue

        try:

            output_path = (
                save_subscription(
                    region=region,
                    nodes=nodes,
                )
            )

        except Exception as exc:

            print(
                f"[ERROR] "
                f"Could not save "
                f"{region}: {exc}"
            )

            failed_regions.append(
                region
            )

            continue

        generated[
            region
        ] = len(nodes)

        print(
            f"[SAVED] "
            f"{output_path} "
            f"({len(nodes)} nodes)"
        )

    # =========================================================================
    # SUMMARY
    # =========================================================================

    print("")
    print("=" * 80)
    print(
        "[4/5] SUMMARY"
    )
    print("=" * 80)

    if not generated:

        print(
            "[FATAL] "
            "No subscription files were generated."
        )

        return 1

    for region, count in (
        generated.items()
    ):

        print(
            f"  {region:<8} "
            f"{REGIONS[region]:<20} "
            f"{count:>4} nodes"
        )

    if failed_regions:

        print("")

        print(
            "[WARNING] "
            "The following regions were not updated:"
        )

        for region in failed_regions:

            print(
                f"  - "
                f"{region:<8} "
                f"{REGIONS.get(region, region)}"
            )

        print(
            "Existing files for those regions "
            "were preserved."
        )

    # =========================================================================
    # DONE
    # =========================================================================

    print("=" * 80)
    print(
        "[5/5] DONE"
    )
    print("=" * 80)

    return 0


# =============================================================================
# ENTRY POINT
# =============================================================================

if __name__ == "__main__":
    sys.exit(main())