#!/usr/bin/env python3
"""
PILOT - Linux system monitoring and troubleshooting shell.
Python 3.8+ recommended. Linux system utilities: ps, systemctl, free.
"""
import cmd
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path


REPORT_DIR = Path.cwd() / "pilot_reports"


def run_command(args, timeout=15):
    """Run a command without a shell, returning (returncode, stdout, stderr)."""
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout, check=False
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", f"Command timed out after {timeout} seconds: {args[0]}"
    except FileNotFoundError:
        return 127, "", f"Required command not found: {args[0]}"
    except OSError as exc:
        return 1, "", str(exc)


def parse_meminfo():
    values = {}
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                match = re.match(r"^(\w+):\s+(\d+)\s+kB", line)
                if match:
                    values[match.group(1)] = int(match.group(2)) * 1024
    except OSError as exc:
        return None, str(exc)

    total = values.get("MemTotal", 0)
    available = values.get("MemAvailable", values.get("MemFree", 0))
    used = max(total - available, 0)
    percent = (used / total * 100) if total else 0
    return {
        "total_bytes": total,
        "available_bytes": available,
        "used_bytes": used,
        "used_percent": percent,
    }, None


def human_bytes(number):
    value = float(number)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"


def get_cpu_utilization(interval=0.5):
    """Measure aggregate CPU busy percentage using /proc/stat."""
    def read_cpu():
        with open("/proc/stat", encoding="utf-8") as handle:
            fields = handle.readline().split()
        numbers = [int(x) for x in fields[1:]]
        idle = numbers[3] + (numbers[4] if len(numbers) > 4 else 0)
        total = sum(numbers)
        return total, idle

    try:
        total1, idle1 = read_cpu()
        time.sleep(interval)
        total2, idle2 = read_cpu()
        total_delta = total2 - total1
        idle_delta = idle2 - idle1
        if total_delta <= 0:
            return None, "Unable to calculate CPU utilization."
        return max(0.0, min(100.0, (1 - idle_delta / total_delta) * 100)), None
    except (OSError, ValueError, IndexError) as exc:
        return None, str(exc)


def get_processes():
    """
    Get process information using ps. The command output is parsed by columns
    so process names and arguments containing spaces remain intact.
    """
    rc, out, err = run_command(
        ["ps", "-eo", "pid=,ppid=,user=,%mem=,%cpu=,comm=,args="], timeout=10
    )
    if rc != 0:
        return [], err or "Unable to read process list."

    processes = []
    for line in out.splitlines():
        # Parse first five whitespace-separated fields; preserve rest as command.
        parts = line.strip().split(None, 6)
        if len(parts) < 6:
            continue
        try:
            processes.append({
                "pid": int(parts[0]),
                "ppid": int(parts[1]),
                "user": parts[2],
                "mem_percent": float(parts[3]),
                "cpu_percent": float(parts[4]),
                "comm": parts[5],
                "command": parts[6] if len(parts) > 6 else parts[5],
            })
        except ValueError:
            continue
    return processes, None


def filter_processes(processes, app_name=None):
    if not app_name:
        return processes
    term = app_name.casefold()
    return [
        p for p in processes
        if term in p["comm"].casefold() or term in p["command"].casefold()
    ]


def service_candidates(query):
    """Search systemd unit names and descriptions for a service query."""
    if not shutil.which("systemctl"):
        return [], "systemctl was not found on this system."

    rc, out, err = run_command(
        ["systemctl", "list-units", "--type=service", "--all",
         "--no-legend", "--no-pager"], timeout=20
    )
    if rc != 0:
        return [], err or "Could not list systemd services."

    # Also search installed unit files, including inactive services not loaded.
    rc2, out2, _ = run_command(
        ["systemctl", "list-unit-files", "--type=service",
         "--no-legend", "--no-pager"], timeout=20
    )

    query_norm = re.sub(r"[^a-z0-9]", "", query.casefold())
    found = {}

    for line in (out + "\n" + out2).splitlines():
        parts = line.split()
        if not parts:
            continue
        unit = parts[0]
        # Match query against the unit name, ignoring punctuation and .service.
        unit_norm = re.sub(r"[^a-z0-9]", "", unit.casefold())
        if query_norm and (query_norm in unit_norm or unit_norm in query_norm):
            found[unit] = unit

    # For queries that match descriptions (e.g. "transit agent"), use systemd
    # show metadata for candidate services. Limit work to avoid huge scans.
    if query_norm and not found:
        rc3, units_out, _ = run_command(
            ["systemctl", "list-unit-files", "--type=service", "--no-legend",
             "--no-pager"], timeout=20
        )
        units = [line.split()[0] for line in units_out.splitlines() if line.split()]
        for unit in units[:250]:
            _, desc, _ = run_command(
                ["systemctl", "show", unit, "--property=Description", "--value"],
                timeout=3
            )
            desc_norm = re.sub(r"[^a-z0-9]", "", desc.casefold())
            if query_norm in desc_norm:
                found[unit] = unit

    candidates = sorted(found.keys())
    return candidates, None


def service_details(unit):
    details = {}
    for key in ("Id", "Description", "LoadState", "ActiveState",
                "SubState", "UnitFileState", "MainPID", "Result"):
        rc, value, _ = run_command(
            ["systemctl", "show", unit, f"--property={key}", "--value"],
            timeout=5
        )
        details[key] = value if rc == 0 else "unavailable"

    rc, status, err = run_command(
        ["systemctl", "status", unit, "--no-pager", "--full"], timeout=10
    )
    details["status_exit_code"] = rc
    details["status_output"] = status or err or "(No status output)"
    return details


class PilotShell(cmd.Cmd):
    intro = """
=================================================
 PILOT - Linux Monitoring & Troubleshooting Shell
 Type 'help' to see commands. Type 'exit' to quit.
=================================================
"""
    BG_BLUE_TXT_WHITE = "\033[44m\033[37m"
    RESET = "\033[0m"

    prompt = f"{BG_BLUE_TXT_WHITE}pilot>{RESET}  "

    def __init__(self):
        super().__init__()
        self.report = {
            "tool": "PILOT",
            "generated_at": dt.datetime.now().astimezone().isoformat(),
            "hostname": os.uname().nodename,
            "sections": [],
        }

    def add_report(self, title, data):
        self.report["sections"].append({
            "title": title,
            "timestamp": dt.datetime.now().astimezone().isoformat(),
            "data": data,
        })


    def do_disk(self, arg):
        """Check partition usage and flag filesystems above 70%. Usage: disk [mountpoint] [threshold_percent]"""
        parts = arg.split()
        mountpoint = None
        threshold = 70.0

        for part in parts:
            if part.startswith("--threshold="):
                try:
                    threshold = float(part.split("=", 1)[1])
                except ValueError:
                    print("Invalid threshold. Use a number between 1 and 100.")
                    return
            elif part.startswith("/"):
                mountpoint = part
            else:
                try:
                    threshold = float(part)
                except ValueError:
                    print(f"Unrecognized argument: {part}")
                    print("Usage: disk [mountpoint] [threshold_percent]")
                    return

        if not 1 <= threshold <= 100:
            print("Threshold must be between 1 and 100.")
            return

        rc, output, error = run_command(
            ["df", "-P",'-T',"-x", "tmpfs", "-x", "devtmpfs"],
            timeout=15
        )
        if rc != 0:
            print(f"Disk check failed: {error or output or 'df command failed'}")
            return

        rows = []
        lines = output.splitlines()
        for line in lines[1:]:
            fields = line.split(None, 6)
            if len(fields) < 7:
                continue
            filesystem, fstype, size, used, available, use_percent, mount = fields
            try:
                percent = float(use_percent.rstrip("%"))
            except ValueError:
                continue
            if mountpoint and mount != mountpoint:
                continue
            rows.append({
                "filesystem": filesystem,
                "type": fstype,
                "size": size,
                "used": used,
                "available": available,
                "used_percent": percent,
                "mountpoint": mount,
                "status": "WARNING" if percent > threshold else "OK",
                "threshold_percent": threshold,
            })

        print(f"\nDISK PARTITION UTILIZATION (warning threshold: > {threshold:.0f}%)")
        if not rows:
            print(f"No matching mounted filesystem found{f' for {mountpoint}' if mountpoint else ''}.")
            self.add_report("Disk utilization", {
                "mountpoint_filter": mountpoint, "threshold_percent": threshold,
                "partitions": [], "error": "No matching mounted filesystem found",
            })
            return

        print(f"{'STATUS':<8} {'USE%':>5} {'SIZE':>10} {'USED':>10} {'AVAIL':>10} {'MOUNT':<24} FILESYSTEM")
        for row in sorted(rows, key=lambda item: item["used_percent"], reverse=True):
            status = row["status"]
            print(f"{status:<8} {row['used_percent']:>4.0f}% {row['size']:>10} "
                  f"{row['used']:>10} {row['available']:>10} "
                  f"{row['mountpoint']:<24} {row['filesystem']}")
        warnings = [row for row in rows if row["used_percent"] > threshold]
        if warnings:
            print(f"\nWARNING: {len(warnings)} filesystem(s) are above {threshold:.0f}% utilization.")
        else:
            print(f"\nOK: No matching filesystem is above {threshold:.0f}% utilization.")

        self.add_report("Disk utilization", {
            "mountpoint_filter": mountpoint,
            "threshold_percent": threshold,
            "partitions": rows,
            "warning_count": len(warnings),
        })

    def do_ram(self, arg):
        """Show RAM utilization and top 5 RAM processes. Usage: ram [application]"""
        mem, error = parse_meminfo()
        if error:
            print(f"RAM check failed: {error}")
            return
        print("\nRAM UTILIZATION")
        print(f"Total:     {human_bytes(mem['total_bytes'])}")
        print(f"Used:      {human_bytes(mem['used_bytes'])} ({mem['used_percent']:.1f}%)")
        print(f"Available: {human_bytes(mem['available_bytes'])}")

        processes, error = get_processes()
        if error:
            print(f"Process list unavailable: {error}")
            return
        app = arg.strip() or None
        matches = filter_processes(processes, app)
        top = sorted(matches, key=lambda p: p["mem_percent"], reverse=True)[:5]
        print(f"\nTOP 5 RAM PROCESSES{f' matching {app!r}' if app else ''}")
        print(f"{'PID':>7} {'USER':<14} {'RAM%':>7} {'CPU%':>7} COMMAND")
        for p in top:
            print(f"{p['pid']:>7} {p['user'][:14]:<14} {p['mem_percent']:>6.1f}% "
                  f"{p['cpu_percent']:>6.1f}% {p['command']}")
        data = {"memory": mem, "filter": app, "top_processes": top}
        self.add_report("RAM utilization", data)

    def do_cpu(self, arg):
        """Show overall CPU utilization and top 5 CPU processes. Usage: cpu [application]"""
        cpu, error = get_cpu_utilization()
        print("\nCPU UTILIZATION")
        if error:
            print(f"CPU check failed: {error}")
        else:
            print(f"Overall CPU busy: {cpu:.1f}%")

        processes, process_error = get_processes()
        if process_error:
            print(f"Process list unavailable: {process_error}")
            return
        app = arg.strip() or None
        matches = filter_processes(processes, app)
        top = sorted(matches, key=lambda p: p["cpu_percent"], reverse=True)[:5]
        print(f"\nTOP 5 CPU PROCESSES{f' matching {app!r}' if app else ''}")
        print(f"{'PID':>7} {'USER':<14} {'RAM%':>7} {'CPU%':>7} COMMAND")
        for p in top:
            print(f"{p['pid']:>7} {p['user'][:14]:<14} {p['mem_percent']:>6.1f}% "
                  f"{p['cpu_percent']:>6.1f}% {p['command']}")
        self.add_report("CPU utilization", {
            "cpu_busy_percent": cpu, "filter": app, "top_processes": top,
            "error": error,
        })

    def do_ps(self, arg):
        """List processes, optionally filtered. Usage: ps -ef [application] or ps [application]"""
        # Accept common forms: ps -ef Apache, ps Apache, or ps -ef
        app = arg.strip()
        app = re.sub(r"^(?:-ef|-e\s+-f)\s*", "", app).strip() or None

        processes, error = get_processes()
        if error:
            print(f"Process check failed: {error}")
            return
        matches = filter_processes(processes, app)
        print(f"\nPROCESSES{f' matching {app!r}' if app else ''} ({len(matches)} found)")
        print(f"{'PID':>7} {'PPID':>7} {'USER':<14} {'RAM%':>7} {'CPU%':>7} COMMAND")
        for p in matches[:100]:
            print(f"{p['pid']:>7} {p['ppid']:>7} {p['user'][:14]:<14} "
                  f"{p['mem_percent']:>6.1f}% {p['cpu_percent']:>6.1f}% {p['command']}")
        if len(matches) > 100:
            print("Showing first 100 matching processes.")
        self.add_report("Process listing", {"filter": app, "count": len(matches),
                                            "processes": matches[:100]})

    def do_service(self, arg):
        """Check service status. Usage: service Apache | service transit agent"""
        query = arg.strip()
        if not query:
            print("Usage: service <service name or description>")
            return

        candidates, error = service_candidates(query)
        if error:
            print(error)
            return
        if not candidates:
            print(f"No matching systemd service found for {query!r}.")
            self.add_report("Service lookup", {"query": query, "matches": []})
            return

        if len(candidates) > 1:
            print(f"Found {len(candidates)} matching services for {query!r}:")
            for index, unit in enumerate(candidates[:20], 1):
                print(f"  {index}. {unit}")
            if len(candidates) > 20:
                print("Showing first 20 matches.")
            print("Run 'service <exact-unit-name>' to inspect one service.")
            self.add_report("Service lookup", {"query": query, "matches": candidates})
            return

        unit = candidates[0]
        details = service_details(unit)
        print(f"\nSERVICE REPORT: {unit}")
        print(f"Description: {details['Description']}")
        print(f"Load state:  {details['LoadState']}")
        print(f"Active:      {details['ActiveState']}")
        print(f"Sub-state:   {details['SubState']}")
        print(f"Enabled:     {details['UnitFileState']}")
        print(f"Main PID:    {details['MainPID']}")
        print("\nSYSTEMCTL STATUS OUTPUT")
        print(details["status_output"])
        self.add_report("Service status", {"query": query, "unit": unit, **details})

    def do_report(self, arg):
        """Write collected diagnostic results to a report. Usage: report [filename]"""
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        filename = arg.strip()
        if filename:
            path = Path(filename)
            if not path.is_absolute():
                path = REPORT_DIR / path
            if not path.suffix:
                path = path.with_suffix(".json")
        else:
            stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
            path = REPORT_DIR / f"pilot_report_{stamp}.json"

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self.report, indent=2, default=str) + "\n",
                            encoding="utf-8")
            print(f"Report saved: {path.resolve()}")
        except OSError as exc:
            print(f"Could not save report: {exc}")

    def do_helpme(self, arg):
        """Show common PILOT examples."""
        print("""
Examples:
  disk                        Check all mounted partitions; warn above 70%
  disk /                     Check the root partition
  disk /var 80               Check /var and warn above 80%
  disk --threshold=75        Check all partitions with a 75% threshold
  ram                         Overall RAM + top 5 RAM processes
  ram apache                  RAM processes matching 'apache'
  cpu                         Overall CPU + top 5 CPU processes
  cpu python                  CPU processes matching 'python'
  ps                          List processes
  ps -ef Apache               List processes matching 'Apache'
  service apache              Find/check Apache-related systemd service
  service transit agent       Search service names/descriptions
  report                      Save collected checks as JSON
  report health-check.json    Save report with a chosen filename
  exit                        Exit PILOT
""")

    def do_exit(self, arg):
        """Exit the PILOT shell."""
        print("Exiting PILOT.")
        return True

    def do_EOF(self, arg):
        """Exit with Ctrl+D."""
        print()
        return True

    def emptyline(self):
        pass
def main():
    PilotShell().cmdloop()


if __name__ == "__main__":
    main()

