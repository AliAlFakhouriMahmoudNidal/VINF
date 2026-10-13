import ctypes
import json
import os
import re
import time
from html import unescape
from pathlib import Path
from posixpath import normpath
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen


USER_AGENT = "VINFStudentPlantScraper/1.0"
DOMAIN = "https://www.rhs.org.uk"
BASE_URL = DOMAIN + "/plants"
DATA_DIR = Path("data")
PROGRESS_FILE = DATA_DIR / "progress.json"
DELAY = 0.1


def download(url):
    for retry in range(5):
        time.sleep(DELAY)

        try:
            # Encode spaces and non-ASCII characters without changing URL separators.
            encoded_url = quote(url, safe=":/?#[]@!$&'()*+,;=%")
            request = Request(encoded_url, headers={"User-Agent": USER_AGENT})
            with urlopen(request, timeout=5) as response:
                return response.read()

        except HTTPError as error:
            error.close()
            reason = f"HTTP {error.code}"
            if error.code == 404:
                print(f"Download failed: {reason}.", flush=True)
                return None

        except Exception as error:
            reason = str(error)

        print(f"Download failed: {reason}.", flush=True)
        if retry < 4:
            print("Retrying in 5 seconds.", flush=True)
            time.sleep(5)
    
    return None


def filter_url(url):
    # Only allow HTTPS URLs on the RHS domain.
    if not re.match(r"^https://www\.rhs\.org\.uk([/?#]|$)", url):
        return None

    # Skip the shop and all pages inside it.
    if re.match(r"^https://www\.rhs\.org\.uk/shop([/?#]|$)", url):
        return None

    # Match plant pages: /plants/<number>/<name>/details.
    return bool(re.fullmatch(
        r"https://www\.rhs\.org\.uk/plants/\d+/[^/?#]+/details/?([?#].*)?",
        url,
    ))


def extract_links(html, page_url=BASE_URL):
    html = html.decode("utf-8", errors="replace")

    # Find href values in single or double quotes.
    links = re.findall(r"""\bhref\s*=\s*["']([^"']+)["']""", html, re.IGNORECASE)

    result = set()
    for link in links:
        link = unescape(link).strip()

        if link.startswith("//"):
            link = "https:" + link
        elif link.startswith("/"):
            link = DOMAIN + link
        elif link.startswith("?"):
            link = page_url.split("?", 1)[0] + link
        elif link.startswith("#"):
            link = page_url + link
        # Links with a scheme, such as https: or mailto:, are already absolute.
        elif not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", link):
            directory = page_url[len(DOMAIN):].split("?", 1)[0].rsplit("/", 1)[0]
            path, separator, query = link.partition("?")
            link = DOMAIN + normpath(directory + "/" + path) + separator + query

        result.add(link)

    return result


def save_progress(queue, visited):
    progress = {"queue": list(queue), "visited": list(visited)}
    temporary_file = DATA_DIR / "progress.tmp"
    temporary_file.write_text(json.dumps(progress), encoding="utf-8")
    temporary_file.replace(PROGRESS_FILE)


def load_progress():
    if not PROGRESS_FILE.exists():
        return {BASE_URL}, set()

    progress = json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
    return set(progress["queue"]), set(progress["visited"])


def output_interface(queue, visited, stats):
    # Enable ANSI colours in Windows cmd.
    if os.name == "nt":
        console = ctypes.windll.kernel32
        console.GetStdHandle.restype = ctypes.c_void_p
        handle = ctypes.c_void_p(console.GetStdHandle(-11))
        mode = ctypes.c_ulong()
        if console.GetConsoleMode(handle, ctypes.byref(mode)):
            console.SetConsoleMode(handle, mode.value | 4)

    cyan = "\033[96m"
    green = "\033[92m"
    yellow = "\033[93m"
    reset = "\033[0m"
    download_rate = stats["downloaded_tokens"] / max(stats["download_seconds"], 0.001)
    parse_rate = stats["parsed_tokens"] / max(stats["parse_seconds"], 0.001)

    # Clear the screen and move back to the top.
    print("\033[2J\033[H", end="")
    print(f"""{cyan}RHS CRAWLER{reset}
Queue:                      {yellow}{len(queue):,}{reset}
Visited:                    {green}{len(visited):,}{reset}

{cyan}Current run (tokens = whitespace-separated HTML pieces){reset}
URLs downloaded:            {stats['downloaded_urls']:,}
Plant pages saved:          {stats['saved_pages']:,}
Plant pages saved MB:       {stats['saved_bytes'] / 1_000_000:.2f}
Downloaded MB:              {stats['downloaded_bytes'] / 1_000_000:.2f}
Downloaded tokens total:    {stats['downloaded_tokens']:,}
Parsed tokens total:        {stats['parsed_tokens']:,}
Download tokens/s (average): {download_rate:,.0f}
Parse tokens/s (average):    {parse_rate:,.0f}

Current URL: {yellow}{stats['current_url']}{reset}
Last saved:  {green}{stats['last_saved_url']}{reset}
Status:      {stats['status']}
""", flush=True)


def crawler():
    DATA_DIR.mkdir(exist_ok=True)
    queue, visited = load_progress()
    queue = {url for url in queue if filter_url(url) is not None}
    save_progress(queue, visited)

    last_downloaded_file = DATA_DIR / "last_downloaded.txt"
    stats = {
        "downloaded_urls": 0,
        "saved_pages": 0,
        "saved_bytes": 0,
        "downloaded_bytes": 0,
        "downloaded_tokens": 0,
        "parsed_tokens": 0,
        "download_seconds": 0.0,
        "parse_seconds": 0.0,
        "current_url": "-",
        "last_saved_url": last_downloaded_file.read_text(encoding="utf-8") if last_downloaded_file.exists() else "-",
        "status": "Starting",
    }

    try:
        while queue:
            url = next(iter(queue))
            stats["current_url"] = url
            stats["status"] = "Downloading"
            output_interface(queue, visited, stats)
            started = time.perf_counter()
            html = download(url)
            stats["download_seconds"] += time.perf_counter() - started

            # Mark failed URLs as visited and continue.
            if html is None:
                stats["status"] = "Skipped: download failed"
                queue.remove(url)
                visited.add(url)
                save_progress(queue, visited)
                output_interface(queue, visited, stats)
                continue

            tokens = len(html.split())
            stats["downloaded_urls"] += 1
            stats["downloaded_bytes"] += len(html)
            stats["downloaded_tokens"] += tokens

            if filter_url(url):
                plant_id = url.split("/")[4]
                (DATA_DIR / f"{plant_id}.html").write_bytes(html)
                stats["saved_bytes"] += len(html)
                last_downloaded_file.write_text(url, encoding="utf-8")
                stats["saved_pages"] += 1
                stats["last_saved_url"] = url

            stats["status"] = "Parsing links"
            output_interface(queue, visited, stats)
            started = time.perf_counter()
            for link in extract_links(html, url):
                link = link.split("#", 1)[0]
                if filter_url(link) is not None and link not in visited:
                    queue.add(link)

            stats["parse_seconds"] += time.perf_counter() - started
            stats["parsed_tokens"] += tokens
            queue.remove(url)
            visited.add(url)
            save_progress(queue, visited)

        if not queue:
            stats["current_url"] = "-"
            stats["status"] = "Finished"

    except KeyboardInterrupt:
        stats["status"] = "Stopped. Run again to resume."
    finally:
        output_interface(queue, visited, stats)


def main():
    crawler()


if __name__ == "__main__":
    main()
