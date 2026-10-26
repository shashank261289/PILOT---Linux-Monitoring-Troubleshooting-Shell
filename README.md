# PILOT --- Linux Monitoring & Troubleshooting CLI

**PILOT** is a command-line helper for Linux administrators and DevOps
engineers. It brings common system checks and issue-focused
troubleshooting into one interactive terminal interface, with reports
you can review or share.

> **Status:** Active development. Diagnostic coverage depends on the
> tools installed on the target Linux system and the permissions of the
> account running PILOT.

## Features

-   **General troubleshooting:** describe the problem in plain language,
    for example `troubleshoot apache is down`.
-   **CPU and memory:** inspect system resource usage and high-consuming
    processes.
-   **Disk and filesystems:** check filesystem usage, inode usage,
    mounts, and available storage information.
-   **Processes:** inspect running processes and their resource
    consumption.
-   **Services:** check systemd service status and related information.
-   **Large files:** find files consuming significant disk space.
-   **Logs and system information:** review selected system logs and
    performance information where supported.
-   **Permission diagnostics:** inspect ownership, permissions, path
    components, ACLs, and SELinux context where relevant.
-   **Troubleshooting reports:** save a readable text report and
    structured JSON output, including the checks performed and their
    command results.

PILOT is designed to collect diagnostic information. The `troubleshoot`
workflow is intended to be read-only and does not automatically restart
services, change permissions, or modify system configuration.

## Installation

Install the published package from PyPI:

``` bash
python3 -m pip install pilot-linux
```

PyPI project: <https://pypi.org/project/pilot-linux/>

If your Linux distribution manages system Python packages, use a virtual
environment rather than forcing a system-wide `pip` installation:

``` bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install pilot-linux
```

Confirm that the command is available:

``` bash
pilot
```

If the shell says `pilot: command not found`, check where Python
installs command-line scripts and ensure that directory is in your
`PATH`.

## Quick start

Start PILOT:

``` bash
pilot
```

At the PILOT prompt, run a command:

``` text
pilot> help
pilot> cpu
pilot> ram
pilot> disk
pilot> service sshd
pilot> ps
pilot> bigfiles
pilot> troubleshoot apache is down
```

### Troubleshoot a problem in plain language

Pass a description after `troubleshoot`:

``` text
pilot> troubleshoot DNS resolution failing
pilot> troubleshoot application is slow
pilot> troubleshoot disk full on /var
pilot> troubleshoot SSH login failing
pilot> troubleshoot high CPU usage
pilot> troubleshoot Docker container keeps restarting
pilot> troubleshoot permission denied for user admin in /etc/ssh/sshd_config
```

PILOT performs baseline checks and selects additional checks based on
keywords in the problem description. Depending on the issue and the
installed system utilities, checks may include CPU and memory usage,
disk and inode usage, failed systemd units, service status and logs,
network configuration, storage/LVM information, permissions, ACLs,
SELinux status, or recent system errors.

**Important:** PILOT gathers evidence; it does not guarantee that every
issue will be diagnosed automatically. Review the findings and
recommendations, and verify any proposed corrective action before making
changes.

## Troubleshooting reports

The troubleshooting command creates a `pilot_reports/` directory
relative to the directory from which PILOT is running.

Typical outputs include:

-   `pilot_troubleshoot_<timestamp>.txt` --- human-readable summary,
    findings, recommendations, and diagnostic command output.
-   `pilot_troubleshoot_<timestamp>.json` --- structured diagnostic
    details.
-   `pilot_troubleshoot.log` --- append-only troubleshooting log.

Report files may contain hostnames, usernames, paths, process arguments,
service details, and log excerpts. Review and redact sensitive
information before sharing reports publicly or attaching them to an
issue.

## Permissions and supported systems

-   Intended for Linux systems with Python 3 and common administration
    utilities.
-   Python 3.8+ is recommended by the current script.
-   Some checks use utilities such as `ps`, `systemctl`, `journalctl`,
    `df`, `findmnt`, `lsblk`, `getfacl`, or SELinux tools when
    available.
-   Commands and output vary by distribution and installed packages.
-   Running as an unprivileged user is supported for many checks, but
    some system logs and administrative details may be unavailable. Use
    elevated privileges only when appropriate and permitted by your
    organization.

## Development

Clone your repository, then install the package in editable mode from
the project root:

``` bash
git clone <YOUR-GITHUB-REPOSITORY-URL>
cd <YOUR-REPOSITORY-DIRECTORY>
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
```

If the source file is `src/pilot/cli.py`, run a syntax check with:

``` bash
python -m py_compile src/pilot/cli.py
```

Replace the repository URL and directory placeholders with your actual
GitHub repository details.

## Safety notes

-   Review diagnostic reports before sharing them; they may contain
    sensitive system information.
-   Do not run commands or apply recommendations you do not understand.
-   Avoid unsafe permission workarounds such as `chmod -R 777` or
    disabling SELinux to bypass an access issue.
-   PILOT is a diagnostic aid, not a replacement for your organization's
    incident-response, change-management, or security procedures.

## License

This project is distributed under the **Apache License 2.0**. Include
the project's `LICENSE` file in the repository and ensure it contains
the complete Apache 2.0 license text.

