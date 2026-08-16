"""Generate Wreath's bounded first-party client-facts databases.

Country inputs are normalized ``start,end,country`` CSV range tables. WGD2
stores selected ranges without widening them: sorted start gaps and inclusive
spans are unsigned varints, followed by one country-table index. IPv6 rows are
kept only when both ends align to a /64 boundary, which makes the same encoding
portable without requiring a compiler-specific 128-bit integer.
"""

from __future__ import annotations

import argparse
import csv
import socket
import struct
from pathlib import Path

MAX_GEO_BYTES = 20_000
MAX_UA_BYTES = 5_000
IPV6_RECORD_BYTES = 4_500

type Range = tuple[int, int, str]

# token, browser, platform, mobile (-1 unknown), bot, priority
UA_RULES = (
    ("edgios", "Microsoft Edge", "iOS", 1, False, 126),
    ("edga", "Microsoft Edge", "Android", 1, False, 126),
    ("edg", "Microsoft Edge", None, -1, False, 125),
    ("opx", "Opera GX", None, -1, False, 124),
    ("opt", "Opera Touch", None, 1, False, 124),
    ("opr", "Opera", None, -1, False, 123),
    ("opera", "Opera", None, -1, False, 122),
    ("vivaldi", "Vivaldi", None, -1, False, 121),
    ("brave", "Brave", None, -1, False, 121),
    ("samsungbrowser", "Samsung Internet", "Android", 1, False, 120),
    ("ucbrowser", "UC Browser", None, -1, False, 119),
    ("yabrowser", "Yandex Browser", None, -1, False, 118),
    ("silk", "Amazon Silk", None, -1, False, 117),
    ("crios", "Chrome", "iOS", 1, False, 116),
    ("headlesschrome", "Headless Chrome", None, -1, True, 116),
    ("chrome", "Chrome", None, -1, False, 115),
    ("chromium", "Chromium", None, -1, False, 114),
    ("fxios", "Firefox", "iOS", 1, False, 113),
    ("camoufox", "Camoufox", None, -1, False, 113),
    ("firefox", "Firefox", None, -1, False, 112),
    ("electron", "Electron", None, -1, False, 111),
    ("version", "Safari", None, -1, False, 80),
    ("duckduckgo", "DuckDuckGo", None, -1, False, 114),
    ("huaweibrowser", "Huawei Browser", "Android", 1, False, 114),
    ("mqqbrowser", "QQ Browser", None, -1, False, 114),
    ("qqbrowser", "QQ Browser", None, -1, False, 114),
    ("miuibrowser", "Mi Browser", "Android", 1, False, 114),
    ("heytapbrowser", "HeyTap Browser", "Android", 1, False, 114),
    ("quark", "Quark Browser", "Android", 1, False, 114),
    ("puffin", "Puffin", None, -1, False, 114),
    ("whale", "Naver Whale", None, -1, False, 114),
    ("focus", "Firefox Focus", None, -1, False, 114),
    ("waterfox", "Waterfox", None, -1, False, 114),
    ("palemoon", "Pale Moon", None, -1, False, 114),
    ("seamonkey", "SeaMonkey", None, -1, False, 114),
    ("epiphany", "GNOME Web", "Linux", 0, False, 114),
    ("konqueror", "Konqueror", "Linux", 0, False, 114),
    ("oculusbrowser", "Meta Quest Browser", "Android", 1, False, 114),
    ("playstation", "PlayStation Browser", "PlayStation", 0, False, 114),
    ("nintendobrowser", "Nintendo Browser", "Nintendo", 0, False, 114),
    ("tizen", None, "Tizen", 0, False, 94),
    ("webos", None, "webOS", 0, False, 94),
    ("fuchsia", None, "Fuchsia", 0, False, 94),
    ("freebsd", None, "FreeBSD", 0, False, 78),
    ("openbsd", None, "OpenBSD", 0, False, 78),
    ("fb4a", "Facebook", "Android", 1, False, 115),
    ("fbios", "Facebook", "iOS", 1, False, 115),
    ("instagram", "Instagram", None, 1, False, 115),
    ("micromessenger", "WeChat", None, 1, False, 115),
    ("line", "LINE", None, 1, False, 110),
    ("snapchat", "Snapchat", None, 1, False, 115),
    ("telegrambot", "TelegramBot", None, -1, True, 140),
    ("discordbot", "Discordbot", None, -1, True, 140),
    ("curl", "curl", None, -1, False, 90),
    ("wget", "Wget", None, -1, False, 90),
    ("python-requests", "Python Requests", None, -1, False, 96),
    ("python-httpx", "Python HTTPX", None, -1, False, 96),
    ("aiohttp", "aiohttp", None, -1, False, 96),
    ("urllib3", "urllib3", None, -1, False, 96),
    ("httpx", "HTTPX", None, -1, False, 95),
    ("okhttp", "OkHttp", None, -1, False, 96),
    ("go-http-client", "Go HTTP Client", None, -1, False, 96),
    ("apache-httpclient", "Apache HttpClient", None, -1, False, 96),
    ("java", "Java HTTP Client", None, -1, False, 70),
    ("libwww-perl", "libwww-perl", None, -1, False, 96),
    ("guzzlehttp", "Guzzle", None, -1, False, 96),
    ("node-fetch", "node-fetch", None, -1, False, 96),
    ("undici", "Undici", None, -1, False, 96),
    ("axios", "Axios", None, -1, False, 96),
    ("faraday", "Faraday", None, -1, False, 96),
    ("reqwest", "Reqwest", None, -1, False, 96),
    ("dart", "Dart HTTP", None, -1, False, 90),
    ("postmanruntime", "Postman", None, -1, False, 96),
    ("insomnia", "Insomnia", None, -1, False, 96),
    ("paw", "Paw", None, -1, False, 96),
    ("grpc", "gRPC", None, -1, False, 96),
    ("restsharp", "RestSharp", None, -1, False, 96),
    ("powershell", "PowerShell", None, -1, False, 96),
    ("winhttp", "WinHTTP", "Windows", 0, False, 96),
    ("cfnetwork", "CFNetwork", None, -1, False, 96),
    ("dalvik", "Dalvik", "Android", 1, False, 96),
    ("boto3", "AWS SDK for Python", None, -1, False, 96),
    ("botocore", "AWS SDK for Python", None, -1, False, 96),
    ("aws-sdk-go", "AWS SDK for Go", None, -1, False, 96),
    ("aws-sdk-java", "AWS SDK for Java", None, -1, False, 96),
    ("aws-sdk-js", "AWS SDK for JavaScript", None, -1, False, 96),
    ("google-api-python-client", "Google API Python Client", None, -1, False, 96),
    ("google-http-java-client", "Google HTTP Java Client", None, -1, False, 96),
    ("azure-sdk-for-python", "Azure SDK for Python", None, -1, False, 96),
    ("azure-core", "Azure SDK", None, -1, False, 95),
    ("windows", None, "Windows", 0, False, 80),
    ("harmonyos", None, "HarmonyOS", 1, False, 92),
    ("kaios", None, "KaiOS", 1, False, 92),
    ("android", None, "Android", 1, False, 90),
    ("iphone", None, "iOS", 1, False, 90),
    ("ipad", None, "iPadOS", 1, False, 91),
    ("macintosh", None, "macOS", 0, False, 80),
    ("cros", None, "ChromeOS", 0, False, 80),
    ("ubuntu", None, "Ubuntu", 0, False, 75),
    ("linux", None, "Linux", 0, False, 70),
    ("googlebot", "Googlebot", None, -1, True, 140),
    ("googleother", "GoogleOther", None, -1, True, 140),
    ("google-inspectiontool", "Google Inspection Tool", None, -1, True, 140),
    ("google-cloudvertexbot", "Google Cloud Vertex Bot", None, -1, True, 140),
    ("adsbot-google", "Google AdsBot", None, -1, True, 140),
    ("bingbot", "Bingbot", None, -1, True, 140),
    ("bingpreview", "BingPreview", None, -1, True, 140),
    ("duckduckbot", "DuckDuckBot", None, -1, True, 140),
    ("yandexbot", "YandexBot", None, -1, True, 140),
    ("baiduspider", "Baiduspider", None, -1, True, 140),
    ("facebookexternalhit", "Facebook crawler", None, -1, True, 140),
    ("facebookbot", "FacebookBot", None, -1, True, 140),
    ("meta-externalagent", "Meta External Agent", None, -1, True, 140),
    ("meta-externalfetcher", "Meta External Fetcher", None, -1, True, 140),
    ("twitterbot", "Twitterbot", None, -1, True, 140),
    ("linkedinbot", "LinkedInBot", None, -1, True, 140),
    ("slackbot-linkexpanding", "Slackbot", None, -1, True, 140),
    ("whatsapp", "WhatsApp crawler", None, -1, True, 140),
    ("gptbot", "GPTBot", None, -1, True, 140),
    ("oai-searchbot", "OpenAI SearchBot", None, -1, True, 140),
    ("chatgpt-user", "ChatGPT User", None, -1, True, 140),
    ("claudebot", "ClaudeBot", None, -1, True, 140),
    ("claude-searchbot", "Claude SearchBot", None, -1, True, 140),
    ("claude-user", "Claude User", None, -1, True, 140),
    ("anthropic-ai", "Anthropic crawler", None, -1, True, 140),
    ("perplexitybot", "PerplexityBot", None, -1, True, 140),
    ("perplexity-user", "Perplexity User", None, -1, True, 140),
    ("amazonbot", "Amazonbot", None, -1, True, 140),
    ("applebot", "Applebot", None, -1, True, 140),
    ("bytespider", "Bytespider", None, -1, True, 140),
    ("cohere-ai", "Cohere crawler", None, -1, True, 140),
    ("ai2bot", "AI2Bot", None, -1, True, 140),
    ("diffbot", "Diffbot", None, -1, True, 140),
    ("youbot", "YouBot", None, -1, True, 140),
    ("imagesiftbot", "ImagesiftBot", None, -1, True, 140),
    ("omgilibot", "Omgilibot", None, -1, True, 140),
    ("omgili", "Omgili", None, -1, True, 140),
    ("webzio-extended", "Webz.io crawler", None, -1, True, 140),
    ("timpibot", "Timpibot", None, -1, True, 140),
    ("petalbot", "PetalBot", None, -1, True, 140),
    ("semrushbot", "SemrushBot", None, -1, True, 140),
    ("mj12bot", "MJ12bot", None, -1, True, 140),
    ("ccbot", "CCBot", None, -1, True, 140),
    ("ia_archiver", "Internet Archive", None, -1, True, 140),
    ("slurp", "Yahoo crawler", None, -1, True, 140),
    ("ahrefsbot", "AhrefsBot", None, -1, True, 140),
    ("dotbot", "DotBot", None, -1, True, 140),
    ("blexbot", "BLEXBot", None, -1, True, 140),
    ("dataforseobot", "DataForSeoBot", None, -1, True, 140),
    ("rogerbot", "Moz crawler", None, -1, True, 140),
    ("screaming", "Screaming Frog", None, -1, True, 140),
    ("seznambot", "SeznamBot", None, -1, True, 140),
    ("qwantify", "Qwantify", None, -1, True, 140),
    ("mojeekbot", "MojeekBot", None, -1, True, 140),
    ("sogou", "Sogou crawler", None, -1, True, 140),
    ("exabot", "Exabot", None, -1, True, 140),
    ("coccocbot-web", "Coc Coc crawler", None, -1, True, 140),
    ("archive.org_bot", "Internet Archive", None, -1, True, 140),
    ("feedfetcher-google", "Google Feedfetcher", None, -1, True, 140),
    ("feedly", "Feedly", None, -1, True, 140),
    ("newsblur", "NewsBlur", None, -1, True, 140),
    ("freshrss", "FreshRSS", None, -1, True, 140),
    ("miniflux", "Miniflux", None, -1, True, 140),
    ("pinterestbot", "PinterestBot", None, -1, True, 140),
    ("redditbot", "RedditBot", None, -1, True, 140),
    ("skypeuripreview", "Skype Preview", None, -1, True, 140),
    ("vkshare", "VK Link Preview", None, -1, True, 140),
    ("embedly", "Embedly", None, -1, True, 140),
    ("quora", "Quora Link Preview", None, -1, True, 140),
    ("kube-probe", "Kubernetes Probe", None, -1, True, 140),
    ("elb-healthchecker", "AWS Load Balancer Health Check", None, -1, True, 140),
    ("googlehc", "Google Cloud Health Check", None, -1, True, 140),
    ("uptimerobot", "UptimeRobot", None, -1, True, 140),
    ("pingdom", "Pingdom", None, -1, True, 140),
    ("statuscake", "StatusCake", None, -1, True, 140),
    ("betteruptimebot", "Better Uptime", None, -1, True, 140),
    ("datadog", "Datadog Synthetics", None, -1, True, 140),
    ("newrelicpinger", "New Relic Monitor", None, -1, True, 140),
    ("site24x7", "Site24x7", None, -1, True, 140),
    ("checkly", "Checkly", None, -1, True, 140),
    ("updown.io", "updown.io", None, -1, True, 140),
    ("hetrixtools", "HetrixTools", None, -1, True, 140),
    ("prometheus", "Prometheus", None, -1, True, 140),
    ("blackbox_exporter", "Prometheus Blackbox", None, -1, True, 140),
    ("zgrab", "ZGrab", None, -1, True, 140),
    ("masscan", "masscan", None, -1, True, 140),
    ("nuclei", "Nuclei", None, -1, True, 140),
    ("nikto", "Nikto", None, -1, True, 140),
    ("scrapy", "Scrapy", None, -1, True, 140),
    ("apify", "Apify", None, -1, True, 140),
    ("crawlee", "Crawlee", None, -1, True, 140),
    ("browserless", "Browserless", None, -1, True, 140),
    ("firecrawl", "Firecrawl", None, -1, True, 140),
    ("xbox", "Xbox Browser", "Xbox", 0, False, 100),
    ("roku", "Roku Browser", "Roku", 0, False, 100),
    ("crkey", "Chromecast", "Chromecast", 0, False, 100),
    ("smart-tv", "Smart TV Browser", "Smart TV", 0, False, 100),
)


def _ranges(path: Path, *, bits: int) -> list[Range]:
    rows: list[Range] = []
    with path.open(newline="", encoding="ascii") as source:
        for line, row in enumerate(csv.reader(source), 1):
            if len(row) != 3:
                raise ValueError(f"{path}:{line} needs start,end,country")
            start_text, end_text, country = row
            try:
                family = socket.AF_INET if bits == 32 else socket.AF_INET6
                start = int.from_bytes(socket.inet_pton(
                    family,
                    start_text,
                ), "big")
                end = int.from_bytes(socket.inet_pton(
                    family,
                    end_text,
                ), "big")
            except OSError as exc:
                raise ValueError(
                    f"{path}:{line} needs valid IPv{bits} range endpoints"
                ) from exc
            if start > end:
                raise ValueError(f"{path}:{line} range start exceeds its end")
            if len(country) != 2 or not country.isascii() or not country.isupper():
                raise ValueError(
                    f"{path}:{line} country must be two uppercase ASCII letters"
                )
            rows.append((start, end, country))
    rows.sort()
    previous_end = -1
    for current in rows:
        if current[0] <= previous_end:
            raise ValueError(f"{path} contains overlapping ranges")
        previous_end = current[1]
    return rows


def _varint(value: int) -> bytes:
    encoded = bytearray()
    while value >= 0x80:
        encoded.append((value & 0x7F) | 0x80)
        value >>= 7
    encoded.append(value)
    return bytes(encoded)


def _encode_geo(v4: set[Range], v6: set[Range]) -> bytes:
    ordered_v4 = sorted(v4)
    ordered_v6 = sorted(v6)
    countries = sorted({row[2] for row in (*ordered_v4, *ordered_v6)})
    if len(countries) > 255:
        raise ValueError("compact GeoIP format supports at most 255 countries")
    country_index = {country: index for index, country in enumerate(countries)}
    encoded = bytearray(struct.pack(
        "<4sBHH", b"WGD2", len(countries), len(ordered_v4), len(ordered_v6)
    ))
    encoded.extend("".join(countries).encode("ascii"))
    previous_end = -1
    for start, end, country in ordered_v4:
        encoded.extend(_varint(start - previous_end - 1))
        encoded.extend(_varint(end - start))
        encoded.append(country_index[country])
        previous_end = end
    previous_end = -1
    for start, end, country in ordered_v6:
        start_high = start >> 64
        end_high = end >> 64
        encoded.extend(_varint(start_high - previous_end - 1))
        encoded.extend(_varint(end_high - start_high))
        encoded.append(country_index[country])
        previous_end = end_high
    return bytes(encoded)


def build_geo(ipv4: Path, ipv6: Path) -> bytes:
    rows_v4 = _ranges(ipv4, bits=32)
    low_mask = (1 << 64) - 1
    rows_v6 = [row for row in _ranges(ipv6, bits=128)
               if row[0] & low_mask == 0 and row[1] & low_mask == low_mask]

    ranked_v6 = sorted(rows_v6, key=lambda row: (row[1] - row[0], row),
                       reverse=True)
    chosen_v6: set[Range] = set()
    for row in ranked_v6:
        candidate = {*chosen_v6, row}
        if len(_encode_geo(set(), candidate)) > IPV6_RECORD_BYTES:
            break
        chosen_v6 = candidate

    largest_per_country: dict[str, Range] = {}
    for row in rows_v4:
        country = row[2]
        previous = largest_per_country.get(country)
        if previous is None or row[1] - row[0] > previous[1] - previous[0]:
            largest_per_country[country] = row
    chosen_v4 = set(largest_per_country.values())
    ranked_v4 = sorted(rows_v4, key=lambda row: (row[1] - row[0], row),
                       reverse=True)
    for row in ranked_v4:
        if row in chosen_v4:
            continue
        candidate = {*chosen_v4, row}
        if len(_encode_geo(candidate, chosen_v6)) <= MAX_GEO_BYTES:
            chosen_v4 = candidate
        else:
            break
    image = _encode_geo(chosen_v4, chosen_v6)
    if len(image) > MAX_GEO_BYTES:
        raise ValueError(
            f"Geo database is {len(image)} bytes; maximum is {MAX_GEO_BYTES}"
        )
    return image


def build_ua() -> bytes:
    strings: list[str] = []
    for _, browser, platform, *_ in UA_RULES:
        for value in (browser, platform):
            if value is not None and value not in strings:
                strings.append(value)
    index = {value: at for at, value in enumerate(strings)}
    encoded = bytearray(struct.pack("<4sBH", b"WUA1", len(strings), len(UA_RULES)))
    for value in strings:
        raw = value.encode("utf-8")
        encoded.extend((len(raw),))
        encoded.extend(raw)
    for token, browser, platform, mobile, bot, priority in UA_RULES:
        raw = token.encode("ascii")
        mobile_bits = 0 if mobile < 0 else mobile + 1
        flags = mobile_bits | (4 if bot else 0)
        encoded.extend((len(raw),))
        encoded.extend(raw)
        encoded.extend((
            255 if browser is None else index[browser],
            255 if platform is None else index[platform],
            flags,
            priority,
        ))
    if len(encoded) > MAX_UA_BYTES:
        raise ValueError(
            f"UA database is {len(encoded)} bytes; maximum is {MAX_UA_BYTES}"
        )
    return bytes(encoded)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("ipv4", type=Path)
    parser.add_argument("ipv6", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    geo = build_geo(args.ipv4, args.ipv6)
    ua = build_ua()
    (args.output / "country.wgd").write_bytes(geo)
    (args.output / "user_agent.wua").write_bytes(ua)
    print(f"country.wgd: {len(geo)} bytes")
    print(f"user_agent.wua: {len(ua)} bytes")


if __name__ == "__main__":
    main()
