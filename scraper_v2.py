import ctypes
import json
import os
import re
import shutil
import sys
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
DELAY = 0.1


class Scraper:
    def __init__(self, data_dir=DATA_DIR):
        self.data_dir = Path(data_dir)
        self.progress_dir = self.data_dir / "progress"
        self.html_dir = self.data_dir / "html"
        self.progress_file = self.progress_dir / "progress.json"
        self.last_downloaded_file = self.progress_dir / "last_downloaded.txt"
        self.queue = {BASE_URL}
        self.visited = set()
        self.stats = {
            "downloaded_urls": 0,
            "saved_pages": 0,
            "saved_bytes": 0,
            "downloaded_bytes": 0,
            "downloaded_tokens": 0,
            "parsed_tokens": 0,
            "download_seconds": 0.0,
            "parse_seconds": 0.0,
            "active_seconds": 0.0,
            "skipped_urls": 0,
            "retries": 0,
        }
        self.current_url = "-"
        self.last_saved_url = "-"
        self.last_error = "-"
        self.status = "Starting"
        self.active_started = time.perf_counter()
        self.screen_started = False
        self.use_ansi = sys.stdout.isatty()

        # Enable ANSI colours once in Windows cmd.
        if os.name == "nt" and self.use_ansi:
            console = ctypes.windll.kernel32
            console.GetStdHandle.restype = ctypes.c_void_p
            handle = ctypes.c_void_p(console.GetStdHandle(-11))
            mode = ctypes.c_ulong()
            self.use_ansi = bool(
                console.GetConsoleMode(handle, ctypes.byref(mode))
                and console.SetConsoleMode(handle, mode.value | 4)
            )

        self.progress_dir.mkdir(parents=True, exist_ok=True)
        self.html_dir.mkdir(parents=True, exist_ok=True)
        self.load_progress()

    def download(self, url):
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
                self.last_error = reason
                if error.code == 404:
                    self.status = "HTTP 404: skipping page"
                    self.output_interface()
                    return None

            except Exception as error:
                reason = str(error)
                self.last_error = reason

            if retry < 4:
                self.stats["retries"] += 1
                self.status = f"{reason}. Attempt {retry + 2}/5 in 5 seconds"
                self.output_interface()
                time.sleep(5)
            else:
                self.status = f"{reason}. Failed after 5 attempts"
                self.output_interface()

        return None

    def filter_url(self, url):
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

    def extract_links(self, html, page_url=BASE_URL):
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

    def save_progress(self):
        now = time.perf_counter()
        self.stats["active_seconds"] += now - self.active_started
        self.active_started = now
        progress = {
            "queue": list(self.queue),
            "visited": list(self.visited),
            "stats": self.stats,
            "last_saved_url": self.last_saved_url,
            "last_error": self.last_error,
        }
        temporary_file = self.progress_dir / "progress.tmp"
        temporary_file.write_text(json.dumps(progress), encoding="utf-8")
        temporary_file.replace(self.progress_file)

    def load_progress(self):
        # Read the old layout too, so changing folders does not reset progress.
        progress_file = self.progress_file
        if not progress_file.exists():
            progress_file = self.data_dir / "progress.json"
        if progress_file.exists():
            progress = json.loads(progress_file.read_text(encoding="utf-8"))
            self.queue = set(progress["queue"])
            self.visited = set(progress["visited"])
            self.stats.update(progress.get("stats", {}))
            self.last_saved_url = progress.get("last_saved_url", "-")
            self.last_error = progress.get("last_error", "-")

        last_downloaded_file = self.last_downloaded_file
        if not last_downloaded_file.exists():
            last_downloaded_file = self.data_dir / "last_downloaded.txt"
        if self.last_saved_url == "-" and last_downloaded_file.exists():
            self.last_saved_url = last_downloaded_file.read_text(encoding="utf-8")

        return self.queue, self.visited

    def output_interface(self):
        width = max(20, min(shutil.get_terminal_size((100, 25)).columns - 1, 110))
        cyan = "\033[96m" if self.use_ansi else ""
        green = "\033[92m" if self.use_ansi else ""
        yellow = "\033[93m" if self.use_ansi else ""
        red = "\033[91m" if self.use_ansi else ""
        reset = "\033[0m" if self.use_ansi else ""
        download_rate = self.stats["downloaded_tokens"] / max(self.stats["download_seconds"], 0.001)
        parse_rate = self.stats["parsed_tokens"] / max(self.stats["parse_seconds"], 0.001)
        seconds = int(self.stats["active_seconds"] + time.perf_counter() - self.active_started)
        active_time = f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"
        border = cyan + "+" + "-" * (width - 2) + "+" + reset

        def row(text, colour=""):
            text = " ".join(text.split())
            if len(text) > width - 4:
                text = text[:width - 7] + "..."
            return f"| {colour}{text:<{width - 4}}{reset} |"

        lines = [
            border,
            row("RHS PLANT CRAWLER | V2 totals across restarts", cyan),
            border,
            row(f"Queue: {len(self.queue):,}     Visited: {len(self.visited):,}", yellow),
            row(f"URLs downloaded: {self.stats['downloaded_urls']:,}     Skipped: {self.stats['skipped_urls']:,}"),
            row(f"Plant pages saved: {self.stats['saved_pages']:,}     Retries: {self.stats['retries']:,}", green),
            row(f"Downloaded MB: {self.stats['downloaded_bytes'] / 1_000_000:.2f}     Plant pages saved MB: {self.stats['saved_bytes'] / 1_000_000:.2f}"),
            row(f"Tokens downloaded: {self.stats['downloaded_tokens']:,}     Parsed: {self.stats['parsed_tokens']:,}"),
            row(f"Average tokens/s - download: {download_rate:,.0f}     parse: {parse_rate:,.0f}"),
            row(f"Active time: {active_time} (paused time excluded)"),
            border,
            row(f"Current URL: {self.current_url}", yellow),
            row(f"Last saved:  {self.last_saved_url}", green),
            row(f"Status: {self.status}", green if self.status == "Finished" else yellow),
            row(f"Last error: {self.last_error}", red if self.last_error != "-" else ""),
            border,
            row("Tokens = whitespace-separated HTML pieces. Ctrl+C saves and stops."),
            border,
        ]

        if self.use_ansi:
            # Refresh in place; clear the whole screen only on the first draw.
            prefix = "\033[H" if self.screen_started else "\033[2J\033[H\033[?25l"
            print(prefix + "\n".join(lines) + "\033[J", end="", flush=True)
            self.screen_started = True
        else:
            print("\n".join(lines), flush=True)

    def crawler(self):
        self.queue = {url for url in self.queue if self.filter_url(url) is not None}
        self.save_progress()

        try:
            while self.queue:
                url = next(iter(self.queue))
                self.current_url = url
                self.status = "Downloading"
                self.output_interface()
                started = time.perf_counter()
                try:
                    html = self.download(url)
                finally:
                    self.stats["download_seconds"] += time.perf_counter() - started

                # Mark failed URLs as visited and continue.
                if html is None:
                    self.status = f"Skipped: {self.last_error}"
                    self.stats["skipped_urls"] += 1
                    self.queue.remove(url)
                    self.visited.add(url)
                    self.save_progress()
                    self.output_interface()
                    continue

                tokens = len(html.split())
                self.stats["downloaded_urls"] += 1
                self.stats["downloaded_bytes"] += len(html)
                self.stats["downloaded_tokens"] += tokens

                if self.filter_url(url):
                    plant_id = url.split("/")[4]
                    (self.html_dir / f"{plant_id}.html").write_bytes(html)
                    self.stats["saved_bytes"] += len(html)
                    self.last_downloaded_file.write_text(url, encoding="utf-8")
                    self.stats["saved_pages"] += 1
                    self.last_saved_url = url

                self.status = "Parsing links"
                self.output_interface()
                started = time.perf_counter()
                try:
                    for link in self.extract_links(html, url):
                        link = link.split("#", 1)[0]
                        if self.filter_url(link) is not None and link not in self.visited:
                            self.queue.add(link)
                finally:
                    self.stats["parse_seconds"] += time.perf_counter() - started

                self.stats["parsed_tokens"] += tokens
                self.queue.remove(url)
                self.visited.add(url)
                self.save_progress()

            self.current_url = "-"
            self.status = "Finished"

        except KeyboardInterrupt:
            self.status = "Stopped. Run again to resume."
        finally:
            self.save_progress()
            self.output_interface()
            if self.use_ansi:
                print("\033[?25h", flush=True)


def main():
    Scraper().crawler()


if __name__ == "__main__":
    main()
