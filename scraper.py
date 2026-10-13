import json
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


def crawler():
    DATA_DIR.mkdir(exist_ok=True)
    queue, visited = load_progress()
    queue = {url for url in queue if filter_url(url) is not None}
    save_progress(queue, visited)

    try:
        while queue:
            url = next(iter(queue))
            print(f"Crawling: {url}", flush=True)
            html = download(url)

            if html is not None:
                if filter_url(url):
                    plant_id = url.split("/")[4]
                    (DATA_DIR / f"{plant_id}.html").write_bytes(html)

                for link in extract_links(html, url):
                    link = link.split("#", 1)[0]
                    if filter_url(link) is not None and link not in visited:
                        queue.add(link)

            queue.remove(url)
            visited.add(url)
            save_progress(queue, visited)

    except KeyboardInterrupt:
        print("Stopped. Run again to resume.", flush=True)


def main():
    crawler()


if __name__ == "__main__":
    main()
