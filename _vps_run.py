# _vps_run.py — run diagnostic commands on the VPS over SSH (password from
# env var VPS_PW; never hardcoded, never written to files or reports).
# Usage:
#   py -3.11 _vps_run.py "command1" "command2" ...        # run commands
#   py -3.11 _vps_run.py --get <remote_path> <local_path> # download a file
import os
import sys

import paramiko

HOST = "69.10.45.213"
USER = "Administrator"
PW = os.environ.get("VPS_PW", "")


def main() -> int:
    if not PW:
        print("VPS_PW not set")
        return 1
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(HOST, username=USER, password=PW, timeout=30,
                   banner_timeout=30, auth_timeout=30, look_for_keys=False,
                   allow_agent=False)
    if sys.argv[1] == "--get":
        sftp = client.open_sftp()
        sftp.get(sys.argv[2].replace("/", "\\"), sys.argv[3])
        sftp.close()
        print(f"downloaded {sys.argv[2]} -> {sys.argv[3]}")
        client.close()
        return 0
    for i, cmd in enumerate(sys.argv[1:]):
        stdin, stdout, stderr = client.exec_command(cmd, timeout=120)
        out = stdout.read().decode("utf-8", "replace").strip()
        err = stderr.read().decode("utf-8", "replace").strip()
        print(f"===== [{i}] {cmd[:100]} =====")
        print(out[:12000] if out else "(empty)")
        if err:
            print(f"[stderr] {err[:500]}")
    client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
