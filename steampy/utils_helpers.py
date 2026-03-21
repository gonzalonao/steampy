import os
import requests
from pathlib import Path

from steampy.client import SteamClient
from steampy.async_client import AsyncClient
from steampy.utils import ping_proxy


def start_clients(file, proxies=None, async_client: bool = False):
    """Parse a cookies file and return a dict of Steam clients.

    Args:
        file: An open file handle to the cookies file.
        proxies: Optional proxy list to pass to each client.
        async_client: If True, create ``AsyncClient`` instances instead
            of the default ``SteamClient``.

    Returns:
        dict[str, SteamClient | AsyncClient]: Mapping of account name to client.
    """
    client_cls = AsyncClient if async_client else SteamClient
    clients = {}
    login_cookies = {}
    name = None
    print("[DEBUG] Starting client creation from cookies file...")

    for line in file:
        cookie = line.split()
        if cookie[0] == 'sessionid':
            login_cookies['sessionid'] = cookie[2].strip()
        elif cookie[0] == 'steamCountry':
            login_cookies['steamCountry'] = cookie[2].strip()
        elif cookie[0] == 'steamLoginSecure':
            login_cookies['steamLoginSecure'] = cookie[2].strip()
        else:
            name = line.strip()

        if 'sessionid' in login_cookies and 'steamCountry' in login_cookies and 'steamLoginSecure' in login_cookies and name:
            print(f"[DEBUG] Creating client for account: {name}")
            try:
                if proxies:
                    client = client_cls(login_cookies=login_cookies, proxies=proxies, account_name=name)
                else:
                    client = client_cls(login_cookies=login_cookies, account_name=name)
                clients[name] = client
                print(f"[DEBUG] Successfully created client for {name}")
            except Exception as e:
                print(f"[ERROR] Failed to create client for {name}: {e}")
                raise
            login_cookies = {}
            name = None

    print(f"[DEBUG] Created {len(clients)} clients")
    return clients

PROXY_URL = os.getenv("PROXY_URL")

def get_proxies(url: str | None = PROXY_URL, test: bool = True) -> list[dict]:
    """Fetch proxies from a web URL and validate their reachability.
    
    Args:
        url (str): The URL to fetch proxies from. Expected response format:
                   Plain text with one proxy per line: ip:port:user:password
    
    Returns:
        list: List of validated proxy dictionaries with 'http' and 'https' keys
    
    Raises:
        ValueError: If no reachable proxies are found
        requests.RequestException: If the URL fetch fails
    """
    print(f"[DEBUG] Fetching proxies from URL: {url}")

    proxies = []
    
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
    except requests.RequestException as e:
        raise requests.RequestException(f'Failed to fetch proxies from URL: {e}')
    
    # Parse the proxy list from response text
    for line in response.text.strip().split('\n'):
        line = line.strip()
        if not line:
            continue
        
        try:
            ip, port, user, password = line.split(':')
            proxy_dict = {
                "http": f"http://{user}:{password}@{ip}:{port}",
                "https": f"http://{user}:{password}@{ip}:{port}"
            }
            proxies.append(proxy_dict)
        except ValueError:
            print(f"[WARNING] Skipping malformed proxy line: {line}")
            continue

    if not test:
        return proxies
    
    # Filter reachable proxies once to avoid re-checking for each client
    print(f"[DEBUG] Testing {len(proxies)} proxies for reachability...")
    working = [p for p in proxies if ping_proxy(p)]
    print(f"[DEBUG] {len(working)} proxies are reachable out of {len(proxies)}")
    if not working:
        raise ValueError('No reachable proxies found from the provided URL')
    return working

def get_desktop_path():
    home = Path.home()

    # List the possible names your desktop might have
    possible_names = ["Desktop", "Escritorio", "Arbeitsplatz"]

    for name in possible_names:
        desktop = home / name
        if desktop.exists():
            return desktop

    # Fallback to home directory if no desktop is found
    return home


def get_results_path() -> Path:
    """Return the directory where all script outputs should be stored.

    Reads from the RESULTS_PATH environment variable. Defaults to a
    ``results/`` folder next to this package (i.e. the project root).
    Creates the directory if it does not exist.
    """
    default = Path(__file__).resolve().parent.parent / "results"
    results = Path(os.environ.get("RESULTS_PATH", str(default)))
    results.mkdir(parents=True, exist_ok=True)
    return results
