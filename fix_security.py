import ipaddress
import socket
import urllib.parse

from app.errors import InputSanitizationError


def sanitize_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    hostname = parsed.hostname

    if hostname.lower() == "localhost":
        raise InputSanitizationError("Localhost URLs not allowed")

    try:
        ipaddress.ip_address(hostname)
        raise InputSanitizationError(f"IP addresses not allowed in URLs: {hostname}")
    except ValueError:
        pass

    try:
        addr_info = socket.getaddrinfo(hostname, None)
        for _family, _type, _proto, _canonname, sockaddr in addr_info:
            ip_str = sockaddr[0]
            ip = ipaddress.ip_address(ip_str)
            if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_multicast or ip.is_unspecified:
                raise InputSanitizationError(f"URL resolves to restricted IP address: {ip_str}")
    except socket.gaierror as e:
        raise InputSanitizationError(f"Could not resolve hostname: {e}") from e

    return url
