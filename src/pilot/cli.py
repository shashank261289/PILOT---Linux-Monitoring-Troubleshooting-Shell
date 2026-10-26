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
import shlex
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


def path_from_arg(value):
    """Expand a user path without invoking a shell."""
    return Path(os.path.expandvars(os.path.expanduser(value)))


def readable_command(args, timeout=15):
    rc, out, err = run_command(args, timeout=timeout)
    return {"command": args, "returncode": rc, "stdout": out, "stderr": err}


def lvm_advice_for_mount(mountpoint):
    """Return read-only LVM mapping and safe suggested growth command."""
    info = {"mountpoint": mountpoint, "lvm": False, "suggestion": None}
    rc, out, err = run_command(["findmnt", "-n", "-o", "SOURCE,FSTYPE", "--target", mountpoint])
    if rc != 0 or not out:
        info["error"] = err or "findmnt could not resolve the mount."
        return info
    fields = out.split(None, 1)
    source = fields[0]
    fstype = fields[1] if len(fields) > 1 else "unknown"
    info.update({"source": source, "filesystem_type": fstype})
    if not source.startswith("/dev/") or not shutil.which("lvs") or not shutil.which("vgs"):
        info["note"] = "This mount does not appear to be an LVM logical volume, or LVM tools are unavailable."
        return info
    rc, lvout, lverr = run_command(["lvs", "--noheadings", "--separator", "|", "-o", "vg_name,lv_name,lv_path", source])
    if rc != 0 or not lvout.strip():
        # Resolve symlink/device aliases using all LV paths.
        rc, all_lvs, _ = run_command(["lvs", "--noheadings", "--separator", "|", "-o", "vg_name,lv_name,lv_path"])
        matched = None
        if rc == 0:
            for line in all_lvs.splitlines():
                cols = [x.strip() for x in line.split("|")]
                if len(cols) >= 3 and cols[2] and os.path.realpath(cols[2]) == os.path.realpath(source):
                    matched = cols
                    break
        if not matched:
            info["note"] = "Unable to map this device to an LVM logical volume."
            info["lvm_error"] = lverr
            return info
        vg, lv, lvpath = matched[:3]
    else:
        cols = [x.strip() for x in lvout.splitlines()[0].split("|")]
        if len(cols) < 3:
            info["note"] = "Could not parse LVM mapping."
            return info
        vg, lv, lvpath = cols[:3]
    info.update({"lvm": True, "vg_name": vg, "lv_name": lv, "lv_path": lvpath or source})
    rc, vgout, vgerr = run_command(["vgs", "--noheadings", "--units", "g", "--nosuffix", "--separator", "|", "-o", "vg_name,vg_free", vg])
    if rc != 0 or not vgout.strip():
        info["vg_error"] = vgerr or "Unable to query volume group free space."
        return info
    cols = [x.strip() for x in vgout.splitlines()[0].split("|")]
    if len(cols) >= 2:
        try:
            free_gb = float(cols[1])
        except ValueError:
            free_gb = 0.0
        info["vg_free_gb"] = free_gb
        if free_gb > 0:
            # Suggest a conservative 1G step or all available space when less than 1G.
            add_gb = min(1.0, free_gb)
            amount = f"{add_gb:g}G"
            info["suggestion"] = f"lvextend -r -L +{amount} {lvpath or source}"
            info["suggestion_note"] = "Review the LV/VG and backup status before running. -r requests filesystem resizing where supported; this command is not executed by PILOT."
        else:
            info["suggestion_note"] = "The volume group has no reported free space. Extend the underlying PV/VG or free VG space before growing this LV."
    return info


class PilotShell(cmd.Cmd):
    intro = """
=================================================
 PILOT - Linux Monitoring & Troubleshooting Shell
 Type 'help' to see commands. Type 'exit' to quit.
 Created by :- Shashank joshi
 Pilot version :- 1.3.0
=================================================
"""
    prompt = "pilot> "

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
        """Show mounted filesystems; optionally filter by usage. Usage: disk | disk 70% | disk /var 80%"""
        parts = shlex.split(arg)
        mountpoint = None
        threshold = None
        for part in parts:
            candidate = part.rstrip("%")
            if part.startswith("/"):
                mountpoint = part
            else:
                try:
                    threshold = float(candidate)
                except ValueError:
                    print("Usage: disk [mountpoint] [threshold%] (e.g. disk, disk 70%, disk /var 80%)")
                    return
        if threshold is not None and not 1 <= threshold <= 100:
            print("Threshold must be between 1 and 100.")
            return
        rc, output, error = run_command(["df", "-P", "-T", "-x", "tmpfs", "-x", "devtmpfs"], timeout=15)
        if rc != 0:
            print(f"Disk check failed: {error or output or 'df command failed'}")
            return
        rows = []
        for line in output.splitlines()[1:]:
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
            if threshold is not None and percent <= threshold:
                continue
            row = {"filesystem": filesystem, "type": fstype, "size": size, "used": used,
                   "available": available, "used_percent": percent, "mountpoint": mount,
                   "threshold_percent": threshold, "status": "WARNING" if threshold is not None else "INFO"}
            if threshold is not None:
                row["lvm_advice"] = lvm_advice_for_mount(mount) if percent > threshold else None
            rows.append(row)
        label = f"filesystems above {threshold:g}%" if threshold is not None else "all mounted filesystems"
        print(f"\nDISK UTILIZATION: {label}")
        if not rows:
            print("No matching mounted filesystems found.")
            self.add_report("Disk utilization", {"mountpoint_filter": mountpoint, "threshold_percent": threshold, "partitions": []})
            return
        print(f"{'USE%':>6} {'SIZE':>10} {'USED':>10} {'AVAIL':>10} {'MOUNT':<24} {'TYPE':<10} FILESYSTEM")
        for row in sorted(rows, key=lambda item: item["used_percent"], reverse=True):
            print(f"{row['used_percent']:>5.0f}% {row['size']:>10} {row['used']:>10} {row['available']:>10} {row['mountpoint']:<24} {row['type']:<10} {row['filesystem']}")
            advice = row.get("lvm_advice")
            if advice:
                if advice.get("lvm"):
                    print(f"  LVM: LV={advice.get('lv_name')} VG={advice.get('vg_name')} device={advice.get('lv_path')}")
                    if "vg_free_gb" in advice:
                        print(f"  VG free space: {advice['vg_free_gb']:.2f} GiB")
                    if advice.get("suggestion"):
                        print(f"  Suggested only (not executed): {advice['suggestion']}")
                    if advice.get("suggestion_note"):
                        print(f"  Note: {advice['suggestion_note']}")
                else:
                    print(f"  LVM advice: {advice.get('note', advice.get('error', 'Unable to map LVM details.'))}")
        self.add_report("Disk utilization", {"mountpoint_filter": mountpoint, "threshold_percent": threshold, "partitions": rows})

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

    def do_check(self, arg):
        """Search a log file/directory for errors. Usage: check error <log-file-or-path>"""
        try:
            parts = shlex.split(arg)
        except ValueError as exc:
            print(f"Invalid arguments: {exc}")
            return
        if len(parts) < 2 or parts[0].lower() != "error":
            print("Usage: check error <log-file-or-path>")
            return
        target = path_from_arg(" ".join(parts[1:]))
        if not target.exists():
            print(f"Path does not exist: {target}")
            return
        patterns = r"error|critical|fatal|failed|failure|exception|traceback|segfault|denied"
        if target.is_file():
            cmdline = ["grep", "-Ein", "--color=never", patterns, str(target)]
        elif target.is_dir():
            cmdline = ["grep", "-RIn", "-E", "--color=never", "--include=*.log", "--include=*.log.*", patterns, str(target)]
        else:
            print("Target is neither a regular file nor a directory.")
            return
        rc, out, err = run_command(cmdline, timeout=45)
        print(f"\nLOG ERROR SCAN: {target}")
        if out:
            lines = out.splitlines()
            for line in lines[-200:]:
                print(line)
            if len(lines) > 200:
                print(f"Showing last 200 of {len(lines)} matching lines.")
        elif rc == 1:
            print("No matching error patterns found.")
        else:
            print(err or "Search failed; check file permissions and grep availability.")
        self.add_report("Log error scan", {"path": str(target), "returncode": rc, "matches": out.splitlines()[-200:]})

    def do_bigfiles(self, arg):
        """Show largest files and immediate directories. Usage: bigfiles [path-or-mount]"""
        target = path_from_arg(arg.strip() or "/")
        if not target.exists() or not target.is_dir():
            print(f"Directory or mount point not found: {target}")
            return
        print(f"\nLARGEST FILES UNDER {target} (top 5; same filesystem only)")
        rc, out, err = run_command(["find", str(target), "-xdev", "-type", "f", "-printf", "%s\\t%p\\n"], timeout=90)
        files = []
        if rc == 0:
            for line in out.splitlines():
                size, sep, name = line.partition("\t")
                if sep:
                    try:
                        files.append((int(size), name))
                    except ValueError:
                        pass
        for size, name in sorted(files, reverse=True)[:5]:
            print(f"{human_bytes(size):>10}  {name}")
        if not files:
            print(err or "No files found, or access was denied.")
        print(f"\nLARGEST IMMEDIATE DIRECTORIES UNDER {target}")
        rc, out, err = run_command(["du", "-x", "-B1", "--max-depth=1", str(target)], timeout=90)
        dirs = []
        if rc == 0:
            for line in out.splitlines():
                size, sep, name = line.partition("\t")
                if sep:
                    try:
                        dirs.append((int(size), name))
                    except ValueError:
                        pass
        for size, name in sorted(dirs, reverse=True)[:5]:
            print(f"{human_bytes(size):>10}  {name}")
        if rc != 0:
            print(err or "Could not calculate directory sizes; check permissions.")
        self.add_report("Large files and directories", {"path": str(target), "top_files": [{"size_bytes": size, "path": name} for size, name in sorted(files, reverse=True)[:5]], "top_directories": [{"size_bytes": size, "path": name} for size, name in sorted(dirs, reverse=True)[:5]]})

    def do_lastlog(self, arg):
        """Show last login records. Usage: lastlog [username]"""
        args = ["lastlog"]
        if arg.strip():
            args += ["-u", arg.strip()]
        rc, out, err = run_command(args, timeout=20)
        print(out or err or "No lastlog output.")
        self.add_report("Last login records", {"user_filter": arg.strip() or None, "returncode": rc, "output": out, "error": err})

    def do_sar(self, arg):
        """Run sysstat sar. Usage: sar [options], e.g. sar -u 1 3 or sar -r"""
        if not shutil.which("sar"):
            print("The 'sar' command is not installed. Install the sysstat package (e.g. sudo dnf install sysstat).")
            return
        try:
            extra = shlex.split(arg)
        except ValueError as exc:
            print(f"Invalid arguments: {exc}")
            return
        # Do not allow shell execution; arguments are passed directly to sar.
        rc, out, err = run_command(["sar", *extra], timeout=90)
        print(out or err or "sar returned no output.")
        self.add_report("SAR performance history", {"arguments": extra, "returncode": rc, "output": out, "error": err})

    def do_findfile(self, arg):
        """Find files by name. Usage: findfile <name-pattern> [start-path]"""
        try:
            parts = shlex.split(arg)
        except ValueError as exc:
            print(f"Invalid arguments: {exc}")
            return
        if not parts:
            print("Usage: findfile <name-pattern> [start-path]")
            return
        pattern = parts[0]
        start = path_from_arg(parts[1]) if len(parts) > 1 else Path.cwd()
        if not start.exists():
            print(f"Search path does not exist: {start}")
            return
        rc, out, err = run_command(["find", str(start), "-name", pattern, "-print", "-quit"], timeout=60)
        # If the first match is found, run again without -quit to return multiple matches, bounded by output display.
        if rc == 0 and out:
            rc, out, err = run_command(["find", str(start), "-name", pattern, "-print"], timeout=90)
        matches = out.splitlines() if out else []
        print(f"\nFILE SEARCH: name={pattern!r}, path={start}")
        for item in matches[:200]:
            print(item)
        if not matches:
            print(err or "No matching files found (or search permissions were denied).")
        if len(matches) > 200:
            print(f"Showing first 200 of {len(matches)} matches.")
        self.add_report("Find files", {"pattern": pattern, "start_path": str(start), "matches": matches[:200], "returncode": rc})

    def do_findtext(self, arg):
        """Find a word/text in files. Usage: findtext <text> <file-or-directory>"""
        try:
            parts = shlex.split(arg)
        except ValueError as exc:
            print(f"Invalid arguments: {exc}")
            return
        if len(parts) < 2:
            print("Usage: findtext <text> <file-or-directory>")
            return
        needle = parts[0]
        target = path_from_arg(" ".join(parts[1:]))
        if not target.exists():
            print(f"Path does not exist: {target}")
            return
        args = ["grep", "-RIn", "-F", "--color=never", "--", needle, str(target)] if target.is_dir() else ["grep", "-In", "-F", "--color=never", "--", needle, str(target)]
        rc, out, err = run_command(args, timeout=90)
        print(f"\nTEXT SEARCH: {needle!r} in {target}")
        lines = out.splitlines()
        for line in lines[:200]:
            print(line)
        if not lines:
            print("No matches found." if rc == 1 else (err or "Search failed."))
        if len(lines) > 200:
            print(f"Showing first 200 of {len(lines)} matches.")
        self.add_report("Find text in files", {"text": needle, "path": str(target), "matches": lines[:200], "returncode": rc, "error": err})

    def do_load(self, arg):
        """Show load average and normalized load percentage relative to CPU count. Usage: load"""
        try:
            loads = os.getloadavg()
        except (AttributeError, OSError) as exc:
            print(f"Could not read load average: {exc}")
            return
        cpus = os.cpu_count() or 1
        one, five, fifteen = loads
        normalized = one / cpus * 100
        print("\nSYSTEM LOAD AVERAGE")
        print(f"Logical CPUs:       {cpus}")
        print(f"1-minute load:      {one:.2f} ({normalized:.1f}% of CPU-count baseline)")
        print(f"5-minute load:      {five:.2f} ({five / cpus * 100:.1f}% of CPU-count baseline)")
        print(f"15-minute load:     {fifteen:.2f} ({fifteen / cpus * 100:.1f}% of CPU-count baseline)")
        print("Note: load average is runnable/uninterruptible task pressure, not direct CPU utilization; values above 100% can be normal on multi-core systems under contention.")
        self.add_report("Load average", {"cpu_count": cpus, "load_1m": one, "load_5m": five, "load_15m": fifteen, "normalized_percent": {"1m": one / cpus * 100, "5m": five / cpus * 100, "15m": fifteen / cpus * 100}})

    def do_scpget(self, arg):
        """Copy a remote file to this machine. Usage: scpget user@host:/remote/file [local-path]"""
        try:
            parts = shlex.split(arg)
        except ValueError as exc:
            print(f"Invalid arguments: {exc}")
            return
        if not parts or len(parts) > 2 or ":" not in parts[0]:
            print("Usage: scpget user@host:/remote/file [local-path]")
            return
        if not shutil.which("scp"):
            print("scp is not installed. Install the OpenSSH client package.")
            return
        remote = parts[0]
        local = str(path_from_arg(parts[1])) if len(parts) == 2 else "."
        print(f"Copying {remote} to {local}. You may be prompted for SSH credentials.")
        try:
            # Inherit terminal streams so interactive SSH/password prompts work.
            result = subprocess.run(["scp", "--", remote, local], timeout=300, check=False)
            rc = result.returncode
        except subprocess.TimeoutExpired:
            rc = 124
            print("SCP timed out after 300 seconds.")
        except OSError as exc:
            rc = 1
            print(f"SCP failed: {exc}")
        if rc == 0:
            print("SCP completed successfully.")
        else:
            print(f"SCP failed with exit code {rc}. Check hostname, SSH access, remote path and destination permissions.")
        self.add_report("SCP remote-to-local", {"remote": remote, "local": local, "returncode": rc})

    def do_troubleshoot(self, arg):
        """Run targeted diagnostics and save a concise summary. Usage: troubleshoot <problem description>"""
        query = arg.strip()
        if not query:
            print("Usage: troubleshoot <problem description>")
            print("Examples: troubleshoot permission denied for user admin in /etc")
            print("          troubleshoot apache is down")
            return

        qlow = query.casefold()
        checks = []
        findings = []
        recommendations = []

        def check(title, command, timeout=15, interpretation=None):
            rc, out, err = run_command(command, timeout=timeout)
            item = {"title": title, "command": command, "returncode": rc,
                    "stdout": out, "stderr": err}
            if interpretation:
                item["interpretation"] = interpretation(rc, out, err)
            checks.append(item)
            return item

        def show_check(item):
            print(f"\n--- {item['title']} ---")
            output = item.get("stdout") or item.get("stderr") or "(no output)"
            print(output[:6000])
            if item.get("interpretation"):
                print("Finding: " + item["interpretation"])

        print(f"\nPILOT TROUBLESHOOTING: {query}")
        check("Host and current identity", ["bash", "-lc", "hostname; date; id"], 10)

        # Targeted permission diagnostics.
        permission_terms = ("permission" in qlow or "denied" in qlow or "access denied" in qlow)
        if permission_terms:
            user_match = re.search(r"\buser\s+([a-zA-Z_][a-zA-Z0-9_.-]*)", query, re.I)
            username = user_match.group(1) if user_match else ("admin" if re.search(r"\badmin\b", qlow) else None)
            path_match = re.search(r"(/[A-Za-z0-9_./@:+-]+)", query)
            target_text = path_match.group(1).rstrip(".,;:") if path_match else None
            if not target_text and re.search(r"\b(in|under|at)\s+/?etc\b", qlow):
                target_text = "/etc"
            if not target_text and re.search(r"\betc\b", qlow):
                target_text = "/etc"
            target = path_from_arg(target_text) if target_text else None

            if username:
                item = check("Account identity and group membership", ["id", username], 10)
                if item["returncode"] == 0:
                    findings.append(f"Account '{username}' exists and its identity/groups were retrieved.")
                else:
                    findings.append(f"Could not resolve user '{username}' using id; verify the account name and identity provider.")
            else:
                findings.append("No specific affected username was identified in the request.")

            if target:
                item = check("Target ownership and permissions", ["ls", "-ld", "--", str(target)])
                show_check(item)
                item = check("Permissions on every path component", ["namei", "-l", str(target)])
                show_check(item)
                if shutil.which("getfacl"):
                    item = check("Access control list (ACL)", ["getfacl", "-p", str(target)])
                    show_check(item)
                else:
                    findings.append("getfacl is unavailable; ACL details were not checked.")
                if shutil.which("ls"):
                    item = check("SELinux file context", ["ls", "-Zd", "--", str(target)])
                    show_check(item)
                if shutil.which("findmnt"):
                    item = check("Filesystem mount options", ["findmnt", "-T", str(target), "-o", "TARGET,SOURCE,FSTYPE,OPTIONS"])
                    show_check(item)

                if username:
                    # runuser runs the permission tests as the affected user, rather than as PILOT's caller.
                    if shutil.which("runuser"):
                        for access, flag in (("read/list", "-r"), ("write", "-w"), ("traverse/execute", "-x")):
                            test_item = check(f"Access test as {username}: {access}",
                                              ["runuser", "-u", username, "--", "test", flag, str(target)], 10)
                            if test_item["returncode"] == 0:
                                test_item["interpretation"] = f"User {username} has {access} access to {target}."
                            elif test_item["returncode"] == 1:
                                test_item["interpretation"] = f"User {username} does not have {access} access to {target} (or the access test is blocked)."
                            else:
                                test_item["interpretation"] = f"Access test could not be completed (exit {test_item['returncode']}); check account resolution and privileges."
                            findings.append(test_item["interpretation"])
                            show_check(test_item)
                    else:
                        findings.append("runuser is unavailable, so effective access could not be tested as the affected user.")

                # /etc itself is usually readable/traversable but not writable by ordinary users.
                if str(target) == "/etc":
                    findings.append("The target is /etc itself. It is normal for a non-root account to be unable to modify this system directory; specify the exact file and operation to diagnose a particular failure.")
                recommendations.extend([
                    "Check the exact file and operation (read, edit, execute, or directory access); /etc alone may be too broad to identify the cause.",
                    "Review ownership/mode bits, every parent directory, ACL entries/masks, SELinux AVC records, and mount options before changing permissions.",
                    "Do not use chmod -R 777 /etc or disable SELinux as a troubleshooting shortcut."
                ])
            else:
                findings.append("No exact path was identified, so path ownership, ACL, SELinux context, and effective access could not be tested.")
                recommendations.append("Re-run with the exact path, for example: troubleshoot permission denied for user admin in /etc/ssh/sshd_config")

            if shutil.which("getenforce"):
                item = check("SELinux enforcement mode", ["getenforce"])
                show_check(item)
            if shutil.which("ausearch"):
                item = check("Recent SELinux AVC denials", ["ausearch", "-m", "AVC", "-ts", "recent", "-i"], 20)
                show_check(item)
                if "denied" in (item.get("stdout", "") + item.get("stderr", "")).casefold():
                    findings.append("Recent SELinux AVC records were queried; correlate their process and path with the reported failure before attributing cause.")

        # Service-specific diagnostics, including RHEL/CentOS Apache's httpd unit.
        service = None
        if any(word in qlow for word in ("apache", "httpd", "web server")):
            service = "httpd"
        elif "nginx" in qlow:
            service = "nginx"
        elif re.search(r"\b(ssh|sshd)\b", qlow):
            service = "sshd"
        elif "docker" in qlow or "container" in qlow:
            service = "docker"
        if service:
            item = check(f"{service} service status", ["systemctl", "status", service, "--no-pager", "--full"], 15)
            show_check(item)
            if item["returncode"] != 0:
                findings.append(f"systemctl status for {service} returned exit code {item['returncode']}; inspect the status output for inactive, failed, or missing-unit details.")
            item = check(f"{service} recent journal", ["journalctl", "-u", service, "-n", "80", "--no-pager"], 15)
            show_check(item)
            if service == "httpd":
                if shutil.which("httpd"):
                    item = check("Apache configuration syntax", ["httpd", "-t"], 15)
                    show_check(item)
                    if item["returncode"] != 0:
                        findings.append("Apache configuration syntax test failed; fix the reported configuration error before restarting httpd.")
                elif shutil.which("apachectl"):
                    item = check("Apache configuration syntax", ["apachectl", "configtest"], 15)
                    show_check(item)
                item = check("Listening TCP ports", ["ss", "-lntp"], 10)
                show_check(item)
                if shutil.which("curl"):
                    item = check("Local HTTP response", ["curl", "-sS", "-I", "--max-time", "5", "http://127.0.0.1/"], 8)
                    show_check(item)
                    if item["returncode"] != 0:
                        findings.append("Local HTTP request to 127.0.0.1 failed; check whether httpd is running, listening, and configured for HTTP on port 80.")
            elif service == "nginx" and shutil.which("nginx"):
                item = check("Nginx configuration syntax", ["nginx", "-t"], 15)
                show_check(item)
            elif service == "docker":
                item = check("Docker daemon info", ["docker", "info"], 15)
                show_check(item)
            if not findings:
                findings.append(f"Collected service-specific checks for {service}; review the command output for the cause.")

        # General, read-only system checks for other problem descriptions.
        if not permission_terms and not service:
            for title, command in [
                ("CPU/load", ["bash", "-lc", "uptime; ps -eo pid,ppid,user,%cpu,%mem,stat,comm --sort=-%cpu | head -n 6"]),
                ("Memory", ["free", "-h"]),
                ("Disk usage", ["df", "-hT"]),
                ("Failed systemd units", ["systemctl", "--failed", "--no-pager"]),
                ("Recent system journal errors", ["journalctl", "-p", "err", "-n", "40", "--no-pager"]),
            ]:
                item = check(title, command, 20)
                show_check(item)
            findings.append("General system checks completed; no single root cause is inferred without a symptom-specific test.")

        # Concise user-facing summary and durable JSON/text report.
        print("\nTROUBLESHOOTING SUMMARY")
        print(f"Problem: {query}")
        print(f"Diagnostic checks run: {len(checks)}")
        for finding in findings:
            print(f"- {finding}")
        if recommendations:
            print("Recommended next steps:")
            for recommendation in dict.fromkeys(recommendations):
                print(f"- {recommendation}")

        result = {"query": query, "checks": checks, "findings": findings,
                  "recommendations": list(dict.fromkeys(recommendations))}
        self.add_report("Targeted troubleshooting", result)
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        json_path = REPORT_DIR / f"pilot_troubleshoot_{stamp}.json"
        text_path = REPORT_DIR / f"pilot_troubleshoot_{stamp}.txt"
        try:
            json_path.write_text(json.dumps({"tool": "PILOT", "generated_at": dt.datetime.now().astimezone().isoformat(), **result}, indent=2, default=str) + "\n", encoding="utf-8")
            summary_lines = ["PILOT TROUBLESHOOTING SUMMARY", f"Generated: {dt.datetime.now().astimezone().isoformat()}", f"Problem: {query}", f"Diagnostic checks run: {len(checks)}", "", "FINDINGS:"]
            summary_lines.extend(f"- {x}" for x in findings)
            if recommendations:
                summary_lines.extend(["", "RECOMMENDED NEXT STEPS:"])
                summary_lines.extend(f"- {x}" for x in dict.fromkeys(recommendations))
            summary_lines.extend(["", "CHECK DETAILS:"])
            for item in checks:
                summary_lines.extend([f"\n[{item['title']}]", f"Command: {' '.join(item['command'])}", f"Exit code: {item['returncode']}", item.get("stdout") or item.get("stderr") or "(no output)"])
                if item.get("interpretation"):
                    summary_lines.append("Finding: " + item["interpretation"])
            text_path.write_text("\n".join(summary_lines) + "\n", encoding="utf-8")
            print(f"\nJSON report: {json_path.resolve()}")
            print(f"Text summary: {text_path.resolve()}")
        except OSError as exc:
            print(f"Could not save troubleshooting report: {exc}")

    def do_reason(self, arg):
        """Diagnose permission denied on a file/directory. Usage: reason permission denied <path>"""
        try:
            parts = shlex.split(arg)
        except ValueError as exc:
            print(f"Invalid arguments: {exc}")
            return
        if len(parts) < 3 or parts[0].lower() != "permission" or parts[1].lower() != "denied":
            print("Usage: reason permission denied <file-or-directory-path>")
            return
        target = path_from_arg(" ".join(parts[2:]))
        print(f"\nPERMISSION-DENIED TROUBLESHOOTING: {target}")
        if not target.exists() and not target.is_symlink():
            print("Path does not currently exist or is not visible to this user; check spelling, parent path, and whether the mount is available.")
        checks = [
            ("Path metadata and ownership", ["ls", "-ld", "--", str(target)]),
            ("Parent directory traversal permissions", ["namei", "-l", str(target)]),
            ("Access control lists", ["getfacl", "-p", str(target)]),
            ("SELinux context", ["ls", "-Zd", "--", str(target)]),
            ("Mount and filesystem options", ["findmnt", "-T", str(target), "-o", "TARGET,SOURCE,FSTYPE,OPTIONS"]),
        ]
        report_checks = []
        for title, command in checks:
            print(f"\n--- {title} ---")
            rc, out, err = run_command(command, timeout=15)
            print(out or err or f"Command returned {rc} with no output.")
            report_checks.append({"title": title, "command": command, "returncode": rc, "stdout": out, "stderr": err})
        if shutil.which("getenforce"):
            rc, out, err = run_command(["getenforce"])
            print("\n--- SELinux mode ---")
            print(out or err)
            report_checks.append({"title": "SELinux mode", "returncode": rc, "stdout": out, "stderr": err})
        print("\nCHECK THESE COMMON CAUSES")
        print("1. File permissions: the effective user/group lacks read, write, or execute access.")
        print("2. Parent directories: every parent directory needs execute (search) permission.")
        print("3. ACLs: getfacl may show a restrictive named-user/group entry or mask.")
        print("4. SELinux: inspect AVC denials with: sudo ausearch -m AVC -ts recent (if audit tools are installed).")
        print("5. Mount options: read-only, NFS root-squash, CIFS credentials, or stale network mounts may block access.")
        print("6. Identity: compare id, groups, and the process's effective user; sudo may change the identity context.")
        print("7. Symlinks/attributes: inspect readlink -f PATH and lsattr PATH when relevant.")
        print("Do not blindly run chmod 777 or disable SELinux. Apply the narrowest approved ownership/ACL/policy fix after confirming the cause.")
        self.add_report("Permission denied troubleshooting", {"path": str(target), "checks": report_checks})

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
  disk                         Show all mounted filesystems
  disk 70%                     Show filesystems above 70% and LVM growth advice
  disk /var 80%                Show /var only if usage is above 80%
  bigfiles /var                Top 5 large files and immediate directories under /var
  check error /var/log         Search log files for error/failure patterns
  check error /var/log/messages
  findfile '*.conf' /etc       Find files by name (quote wildcard patterns)
  findtext 'connection refused' /var/log
  lastlog                      Show last login records
  lastlog root                 Show last login for root
  sar -u 1 3                   Show CPU stats for 3 samples (requires sysstat)
  load                         Load averages and normalized load percentages
  reason permission denied /var/www/html
  troubleshoot permission denied for user admin in /etc
  troubleshoot apache is down
  scpget admin@server:/var/log/app.log ./app.log
  ram [application]            Overall RAM + top 5 RAM processes
  cpu [application]            Overall CPU + top 5 CPU processes
  ps -ef Apache                List processes matching Apache
  service apache               Find/check Apache-related systemd service
  report                       Save collected checks as JSON
  exit                         Exit PILOT
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
    """Entry point for the `pilot` console command."""
    PilotShell().cmdloop()


if __name__ == "__main__":
    main()
