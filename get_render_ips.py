#!/usr/bin/env python3
"""
Скрипт для получения IP-адресов Render
"""

import requests
import json

def get_render_ips():
    """Получает IP-адреса Render из их API"""
    try:
        # Render предоставляет информацию о своих IP-адресах
        response = requests.get('https://api.render.com/v1/ips')
        if response.status_code == 200:
            data = response.json()
            print("Render IP addresses:")
            for ip_info in data:
                print(f"  - {ip_info['ip']} ({ip_info.get('description', 'No description')})")
        else:
            print(f"Failed to get Render IPs: {response.status_code}")
            
    except Exception as e:
        print(f"Error getting Render IPs: {e}")
        print("\nAlternative: Check Render documentation for current IP ranges")
        print("https://render.com/docs/ip-addresses")

def get_alternative_sources():
    """Альтернативные источники информации об IP Render"""
    print("\n=== ALTERNATIVE SOURCES ===")
    print("1. Render Documentation: https://render.com/docs/ip-addresses")
    print("2. Render Status Page: https://status.render.com/")
    print("3. Contact Render Support for current IP ranges")
    
    print("\n=== COMMON RENDER IP RANGES (may be outdated) ===")
    print("Note: These are examples and may not be current")
    print("  - 3.120.0.0/16 (AWS Frankfurt)")
    print("  - 3.121.0.0/16 (AWS Frankfurt)")
    print("  - 18.157.0.0/16 (AWS Frankfurt)")
    print("  - 18.158.0.0/16 (AWS Frankfurt)")

if __name__ == "__main__":
    print("=== RENDER IP ADDRESSES ===")
    get_render_ips()
    get_alternative_sources()
    
    print("\n=== NEXT STEPS ===")
    print("1. Use the IP addresses above to whitelist in Neon.tech")
    print("2. Or use Neon.tech's connection pooling (pooler mode)")
    print("3. Check if your Neon.tech project has IP restrictions enabled")
