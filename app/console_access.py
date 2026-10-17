"""Recognize direct local console access without trusting proxy headers."""

from ipaddress import ip_address, ip_network


_LOCAL_NETWORKS = tuple(ip_network(net) for net in (
    "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "::1/128", "fc00::/7",
))
_PROXY_HEADERS = ("forwarded", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto",
                  "x-real-ip", "cf-connecting-ip", "true-client-ip")


def _local_ip(value: str) -> bool:
    try:
        address = ip_address(value)
        address = getattr(address, "ipv4_mapped", None) or address
        return any(address in net for net in _LOCAL_NETWORKS if net.version == address.version)
    except ValueError:
        return False


def _local_transport_reason(request, mode: str) -> str:
    if mode != "local":
        return "login_required"
    if not request.client or not _local_ip(request.client.host):
        return "nonlocal_client"
    host = (request.url.hostname or "").lower()
    if host != "localhost" and not _local_ip(host):
        return "nonlocal_host"
    # Public tunnels and reverse proxies continue to require a console session.
    if any(name in request.headers for name in _PROXY_HEADERS):
        return "proxy_headers"
    return "local"


def local_console_access_reason(request, mode: str) -> str:
    reason = _local_transport_reason(request, mode)
    if reason != "local":
        return reason
    if request.headers.get("sec-fetch-site") == "cross-site":
        return "cross_site"
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
        return "foreign_origin"
    return "local"


def is_local_console_request(request, mode: str) -> bool:
    return local_console_access_reason(request, mode) == "local"


def is_local_console_navigation(request, mode: str) -> bool:
    """Allow a local top-level page visit; this never authorizes console APIs.

    Chromium can retain Sec-Fetch-Site: cross-site when reloading a page opened
    from another site. Only the dashboard HTML is allowed here; its subsequent
    same-origin API calls still use the strict request check above.
    """
    return (
        request.method == "GET"
        and request.url.path == "/"
        and request.headers.get("sec-fetch-mode") == "navigate"
        and request.headers.get("sec-fetch-dest") == "document"
        and _local_transport_reason(request, mode) == "local"
    )
