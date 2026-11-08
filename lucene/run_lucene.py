import argparse
import subprocess
import sys
from pathlib import Path


IMAGE = "vinf-pylucene:10.0.0"
FOLDER = Path(__file__).resolve().parent
COMMANDS = {
    "build": ["lucene/lucene_index.py", "build"],
    "search": ["lucene/lucene_index.py", "interactive"],
    "query": ["lucene/lucene_index.py", "search"],
    "test": ["-m", "unittest", "discover", "-v", "-s", "tests", "-p", "test_*.py"],
    "python": [],
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", nargs="?", default="search", choices=["setup", *COMMANDS])
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    try:
        if args.mode == "setup":
            with (FOLDER / "Dockerfile.lucene").open("rb") as dockerfile:
                return subprocess.call(["docker", "build", "-t", IMAGE, "-"], stdin=dockerfile)

        mount = f"type=bind,source={FOLDER.parent},target=/app"
        command = ["docker", "run", "--rm", "-i", "--mount", mount]
        if args.mode == "search" and sys.stdin.isatty():
            command.append("-t")
        return subprocess.call([*command, IMAGE, *COMMANDS[args.mode], *args.arguments])
    except OSError as error:
        print(f"Could not start Lucene: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
