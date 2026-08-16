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
import urllib3
import yaml

# The health-check step deliberately connects through proxies with
# skip-cert-verify semantics (verify=False), which is expected and not
# a bug. Silence the resulting InsecureRequestWarning noise in logs.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


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


# =============================================================================
# HEALTH CHECK / SPEED TEST CONFIG
# =============================================================================
#
# Why this exists:
#
# Each region can collect 100+ raw candidate nodes from the free providers.
# If ALL of them are shipped in the subscription file, Clash on the phone
# has to url-test every single one before it can pick a working proxy:
#   - startup / group selection becomes slow
#   - Clash frequently lands on a dead or slow node while testing is
#     still in progress
#
# The fix: do the speed testing here, once, on the server, against every
# candidate, then ship only the nodes that are actually fast and alive -
# already sorted fastest-first. Clash then only has to choose between a
# small, pre-vetted set instead of gambling across 100+ unknowns.

# Endpoint used to measure real latency THROUGH each proxy.
# Small, fast, CDN-backed, returns 204 with no body.
HEALTH_CHECK_URL = "https://www.gstatic.com/generate_204"

# Per-request timeout for a single health-check attempt.
HEALTH_CHECK_TIMEOUT = 6

# A node is retried once if the first attempt fails/times out, to avoid
# throwing away a genuinely good node because of one flaky connection.
HEALTH_CHECK_ATTEMPTS = 2

# How many nodes are speed-tested at once. This is independent of
# MAX_WORKERS (which limits provider scraping) because testing a live
# TLS proxy connection is a different, lighter workload.
HEALTH_CHECK_WORKERS = 40

# Anything slower than this (or that fails every attempt) is dropped
# entirely - it would never be a good pick on the phone anyway.
MAX_ACCEPTABLE_LATENCY_MS = 2500

# Final cap on how many *tested, working* nodes are shipped per region,
# fastest-first. This is the main lever for how quick Clash's own
# selection feels: fewer good nodes beats hundreds of unknown ones.
MAX_NODES_PER_REGION = 15


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
# HEALTH CHECK / SPEED TEST
# =============================================================================

def _build_proxy_url(node: dict[str, Any]) -> str:
    """
    Builds an http://user:pass@host:port URL suitable for requests'
    `proxies=` argument, matching the credentials of the Clash HTTP
    proxy entry itself (percent-encoded, since providers occasionally
    hand out passwords with special characters).
    """

    from urllib.parse import quote

    user = quote(node["username"], safe="")
    pwd = quote(node["password"], safe="")

    return f"http://{user}:{pwd}@{node['server']}:{node['port']}"


def measure_latency_ms(node: dict[str, Any]) -> float | None:
    """
    Makes a real request THROUGH the proxy and times it.

    Returns the latency in milliseconds on success, or None if the
    node is dead, refuses the connection, or is slower than
    MAX_ACCEPTABLE_LATENCY_MS.
    """

    proxy_url = _build_proxy_url(node)
    proxies = {"http": proxy_url, "https": proxy_url}

    best_latency: float | None = None

    for _ in range(HEALTH_CHECK_ATTEMPTS):
        start = time.monotonic()

        try:
            response = requests.get(
                HEALTH_CHECK_URL,
                proxies=proxies,
                timeout=HEALTH_CHECK_TIMEOUT,
                verify=False,
            )
        except requests.RequestException:
            continue

        elapsed_ms = (time.monotonic() - start) * 1000

        if response.status_code not in (200, 204):
            continue

        if best_latency is None or elapsed_ms < best_latency:
            best_latency = elapsed_ms

    if best_latency is None:
        return None

    if best_latency > MAX_ACCEPTABLE_LATENCY_MS:
        return None

    return best_latency


def health_check_and_rank(
    nodes: list[dict[str, Any]],
    region: str,
) -> list[dict[str, Any]]:
    """
    Speed-tests every candidate node concurrently, drops dead/slow
    ones, and returns only the fastest MAX_NODES_PER_REGION nodes,
    sorted fastest-first.
    """

    if not nodes:
        return []

    print("")
    print(
        f"    [HEALTH CHECK] {region}: testing {len(nodes)} "
        f"candidate node(s)..."
    )

    passed: list[dict[str, Any]] = []

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(HEALTH_CHECK_WORKERS, len(nodes))
    ) as executor:

        future_map = {
            executor.submit(measure_latency_ms, node): node
            for node in nodes
        }

        checked = 0

        for future in concurrent.futures.as_completed(future_map):
            node = future_map[future]
            checked += 1

            try:
                latency_ms = future.result()
            except Exception:
                latency_ms = None

            if latency_ms is not None:
                node["_latency_ms"] = latency_ms
                passed.append(node)

            if checked % 20 == 0 or checked == len(nodes):
                print(
                    f"        tested={checked:03d}/{len(nodes):03d} "
                    f"alive={len(passed):03d}"
                )

    passed.sort(key=lambda n: n["_latency_ms"])

    kept = passed[:MAX_NODES_PER_REGION]

    print(
        f"    [HEALTH CHECK] {region}: {len(passed)}/{len(nodes)} alive, "
        f"keeping fastest {len(kept)} "
        f"(cap={MAX_NODES_PER_REGION})"
    )

    if kept:
        best = kept[0]["_latency_ms"]
        worst = kept[-1]["_latency_ms"]
        print(
            f"    [HEALTH CHECK] {region}: latency range in final set "
            f"{best:.0f}ms - {worst:.0f}ms"
        )

    return kept


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

    collected = list(all_unique.values())

    print("")
    print(
        f"[RESULT] {region}: {len(collected)} unique candidate nodes "
        f"collected"
    )

    # Speed-test every candidate and keep only the fastest, working
    # subset. This is what keeps the final subscription small enough
    # for Clash to test quickly on the phone, and correct enough that
    # it doesn't land on a dead node while doing it.
    result = health_check_and_rank(collected, region)

    if not result:
        print(
            f"[WARNING] {region}: no candidate passed the health check."
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


def build_yaml(
    region: str,
    nodes: list[dict[str, Any]],
) -> str:

    proxies: list[dict[str, Any]] = []
    proxy_names: list[str] = []

    for index, node in enumerate(nodes, start=1):

        name = make_proxy_name(
            region=region,
            index=index,
            server=node["server"],
        )

        proxy_names.append(name)

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

    config: dict[str, Any] = {}

    config.update(DNS_CONFIG)

    config["proxies"] = proxies

    # "Auto" only ever contains the small, pre-vetted, speed-tested set
    # of nodes (see health_check_and_rank), so Clash's own url-test on
    # the phone has very little work left to do and converges fast.
    config["proxy-groups"] = [
        {
            "name": "Proxy",
            "type": "select",
            # proxy_names is already sorted fastest-first from the
            # server-side health check, so the first real entry here
            # (right after "Auto") is the fastest node we measured -
            # picking it manually needs zero on-device testing at all.
            "proxies": ["Auto"] + proxy_names,
        },
        {
            "name": "Auto",
            "type": "url-test",
            "proxies": proxy_names,
            "url": "https://www.gstatic.com/generate_204",
            "interval": 300,
            "tolerance": 50,
        },
        {
            "name": "Iran-Direct",
            "type": "select",
            "proxies": [
                "DIRECT",
            ],
        },
    ]

    config["rules"] = [
        "GEOIP,IR,Iran-Direct",
        "MATCH,Proxy",
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
    region: str,
    nodes: list[dict[str, Any]],
) -> Path:

    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    output_path = OUTPUT_DIR / f"{region}.yaml"

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

    generated: dict[str, int] = {}

    for region in REGIONS:

        nodes = collect_region(
            region=region,
            providers=providers,
            token_manager=token_manager,
        )

        # Important:
        # Never overwrite a previously generated good file with
        # an empty result.
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

        generated[region] = len(nodes)

        print(
            f"[SAVED] {output_path} "
            f"({len(nodes)} nodes)"
        )

    # -------------------------------------------------------------------------
    # Summary
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

    for region, count in generated.items():
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
