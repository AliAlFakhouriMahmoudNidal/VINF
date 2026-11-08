import os
import subprocess
import sys
from pathlib import Path


def run_spark(folder, args):
    runtime = Path.home() / "vinf-spark"
    binaries = runtime / ".venv" / "bin"
    java = Path("/usr/lib/jvm/java-17-openjdk-amd64")
    if not os.access(binaries / "pyspark", os.X_OK):
        raise OSError("Spark is not installed. See doc/spark/README.md.")

    scratch = runtime / "tmp"
    warehouse = runtime / "warehouse"
    scratch.mkdir(parents=True, exist_ok=True)
    warehouse.mkdir(parents=True, exist_ok=True)
    local_ip = subprocess.check_output(["hostname", "-I"], text=True).strip().partition(" ")[0]
    environment = dict(os.environ)
    environment.update({
        "JAVA_HOME": str(java),
        "PATH": f"{binaries}:{java / 'bin'}:{os.environ.get('PATH', os.defpath)}",
        "PYSPARK_PYTHON": str(binaries / "python"),
        "PYSPARK_DRIVER_PYTHON": str(binaries / "python"),
        "SPARK_CONF_DIR": str(folder / "conf"),
        "SPARK_LOCAL_IP": local_ip,
        "SPARK_PUBLIC_DNS": local_ip,
        "SPARK_LOCAL_DIRS": str(scratch),
    })

    programs = {"shell": "pyspark", "submit": "spark-submit", "python": "python"}
    if args and args[0] in programs:
        mode, *args = args
    else:
        print("Starting Wikipedia extraction in Spark...", flush=True)
        mode = "submit"
        args = ["extract_wiki.py", *args]

    if mode == "shell":
        print(f"Spark UI (after startup): http://{local_ip}:4040", flush=True)
    program = str(binaries / programs[mode])
    command = [program]
    if mode != "python":
        command += ["--conf", f"spark.sql.warehouse.dir={warehouse}"]

    os.chdir(folder)
    os.execve(program, command + args, environment)


def main():
    folder = Path(__file__).resolve().parent
    try:
        if os.name == "nt":
            wsl = ["wsl.exe", "-d", "Ubuntu-24.04"]
            linux_folder = subprocess.check_output(
                [*wsl, "--exec", "wslpath", "-a", "-u", folder.as_posix()],
                text=True,
            ).strip()
            return subprocess.call([*wsl, "--cd", linux_folder, "--exec",
                                    "python3", "./run_wiki.py", *sys.argv[1:]])
        run_spark(folder, sys.argv[1:])
    except (OSError, subprocess.CalledProcessError) as error:
        print(f"Could not start Spark: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
