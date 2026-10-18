# Crawling, parsing, storage and shared timing
import json
import re
import time
from html import unescape
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote, urljoin
from urllib.request import Request, urlopen

# Terminal UI
import ctypes
import os
import shutil
import sys
from textwrap import wrap


USER_AGENT = "VINFStudentPlantScraper/1.0"
DOMAIN = "https://www.rhs.org.uk"
BASE_URL = DOMAIN + "/plants"
DATA_DIR = Path("data")
DELAY = 0.1


class Scraper:
    def __init__(self, data_dir=DATA_DIR):
        self.run_started = time.perf_counter()
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
            "error_counts": {},
        }
        self.current_url = "-"
        self.last_saved_url = "-"
        self.last_error = "-"
        self.status = "Starting"
        self.active_started = self.run_started
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
        # Saved totals belong to earlier runs, not this run's throughput.
        self.initial_stats = {
            key: value for key, value in self.stats.items() if key != "error_counts"
        }
        self.initial_error_count = sum(self.stats["error_counts"].values())

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
                self.record_error(reason)
                if error.code in [404, 403]:
                    self.status = f"HTTP {error.code}: skipping page"
                    self.output_interface()
                    return None

            except Exception as error:
                reason = str(error)
                self.last_error = reason
                self.record_error(type(error).__name__)

            if retry < 4:
                self.stats["retries"] += 1
                self.status = f"{reason} on attempt {retry + 1}"
                self.output_interface()
                time.sleep(5 * retry)
            else:
                self.status = f"{reason}. Failed after 5 attempts"
                self.output_interface()

        return None

    def record_error(self, kind):
        # Count every failed attempt, including 404s and the final attempt.
        counts = self.stats["error_counts"]
        counts[kind] = counts.get(kind, 0) + 1

    def filter_url(self, url):
        # Only allow HTTPS URLs on the RHS domain.
        if not re.match(r"^https://www\.rhs\.org\.uk([/?#]|$)", url):
            return None

        # Skip the shop and all pages inside it.
        if re.match(r"^https://www\.rhs\.org\.uk/shop([/?#]|$)", url):
            return None

        # Exclude the requested robots.txt paths, even with queries or fragments.
        path = url[len(DOMAIN):].split("#", 1)[0].split("?", 1)[0]
        if path.startswith("/api/") or path.endswith((".doc", ".docx", "undefined", "thank-you")):
            return None

        # Match plant pages: /plants/<number>/<name>/details.
        return bool(re.fullmatch(
            r"https://www\.rhs\.org\.uk/plants/\d+/[^/?#]+/details/?([?#].*)?",
            url,
        ))

    def extract_links(self, html, page_url=BASE_URL):
        html = html.decode("utf-8", errors="replace")

        # Match the closing quote to the opening quote, allowing the other inside.
        links = re.finditer(r"""\bhref\s*=\s*(["'])(.*?)\1""", html, re.IGNORECASE | re.DOTALL)

        result = set()
        for match in links:
            # <a href="/plants?page=2&amp;sort=name"> -> <a href="/plants?page=2&amp;sort=name">
            link = unescape(match.group(2)).strip()
            if not link:
                continue

            try:
                result.add(urljoin(page_url, link))
            except ValueError:
                continue

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
        now = time.perf_counter()
        run_seconds = max(now - self.run_started, 0.0)
        total_seconds = max(self.stats["active_seconds"] + now - self.active_started, 0.0)
        run_stats = {
            key: self.stats[key] - value for key, value in self.initial_stats.items()
        }
        error_counts = self.stats["error_counts"]
        total_errors = sum(error_counts.values())
        border = cyan + "+" + "-" * (width - 2) + "+" + reset

        def row(text, colour=""):
            text = text.replace("\n", " ").replace("\r", " ").replace("\t", " ")
            if len(text) > width - 4:
                text = text[:width - 7] + "..."
            return f"| {colour}{text:<{width - 4}}{reset} |"

        def wrapped_rows(text, colour=""):
            return [row(line, colour) for line in wrap(text, width - 4)]

        def metric(label, total, current, colour=""):
            if width < 70:
                return [row(label, colour), *wrapped_rows(f"All runs: {total} | This run: {current}")]
            value_width = (width - 28) // 2
            return [row(f"{label:<24}{total:>{value_width}}{current:>{value_width}}", colour)]

        def duration(seconds):
            seconds = int(seconds)
            return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"

        def rate(count, seconds, per=1):
            return f"{count * per / seconds:,.1f}" if seconds > 0 else "-"

        lines = [
            border,
            row("RHS PLANT CRAWLER | V2", cyan),
            row(f"URLs queued: {len(self.queue):,}     Visited: {len(self.visited):,}", yellow),
            border,
            *(metric("", "ALL RUNS", "THIS RUN", cyan) if width >= 70 else []),
            *metric("Runtime", duration(total_seconds), duration(run_seconds)),
            *metric("Tokens parsed", f"{self.stats['parsed_tokens']:,}", f"{run_stats['parsed_tokens']:,}", green),
            *metric("Average tokens/s", rate(self.stats['parsed_tokens'], total_seconds), rate(run_stats['parsed_tokens'], run_seconds)),
            *metric("Pages downloaded", f"{self.stats['downloaded_urls']:,}", f"{run_stats['downloaded_urls']:,}"),
            *metric("Average pages/min", rate(self.stats['downloaded_urls'], total_seconds, 60), rate(run_stats['downloaded_urls'], run_seconds, 60)),
            *metric("Plant pages saved", f"{self.stats['saved_pages']:,}", f"{run_stats['saved_pages']:,}", green),
            *metric("URLs skipped (failed)", f"{self.stats['skipped_urls']:,}", f"{run_stats['skipped_urls']:,}"),
            *metric("HTML downloaded (MB)", f"{self.stats['downloaded_bytes'] / 1_000_000:,.2f}", f"{run_stats['downloaded_bytes'] / 1_000_000:,.2f}"),
            *metric("Plant HTML saved (MB)", f"{self.stats['saved_bytes'] / 1_000_000:,.2f}", f"{run_stats['saved_bytes'] / 1_000_000:,.2f}"),
            *metric("Failed attempts", f"{total_errors:,}", f"{total_errors - self.initial_error_count:,}", red if total_errors else ""),
            *wrapped_rows(
                "Errors by type (since tracking began): " + (
                    " | ".join(f"{kind}: {count:,}" for kind, count in sorted(error_counts.items(), key=lambda item: (-item[1], item[0])))
                    or "none"
                ),
                red if total_errors else "",
            ),
            border,
            row(f"Current URL: {self.current_url}", yellow),
            row(f"Last saved:  {self.last_saved_url}", green),
            row(f"Status: {self.status}", green if self.status == "Finished" else yellow),
            row(f"Last error: {self.last_error}", red if self.last_error != "-" else ""),
            border,
            *wrapped_rows("Ctrl+C saves and stops."),
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
