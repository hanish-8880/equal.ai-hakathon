"""Put a logged-in Swiggy account on the server (private folder, chmod 600), then park the laptop copy
(Swiggy refresh tokens rotate, so only ONE place may use them). Run: python -m scripts.swiggy_upload <account>"""
import subprocess
import sys
from pathlib import Path

from scripts import aws_deploy
from scripts.swiggy_login_test import HOME

name = (sys.argv[1] if len(sys.argv) > 1 else "").lower()
src = HOME / "swiggy" / name / "tokens.json"
assert name and name != "main" and src.exists(), f"no logged-in account “{name}” at {src} — run: python -m scripts.swiggy_login_test --account {name}"
st = aws_deploy.load()
key = str(Path.home() / ".ssh" / "chinku-aws.pem")
dest = f"/home/ubuntu/.chinku/swiggy/{name}"
aws_deploy.ssh(f"mkdir -p {dest} && chmod 700 /home/ubuntu/.chinku /home/ubuntu/.chinku/swiggy {dest}", st)
subprocess.run(["scp", "-q", "-i", key, str(src), f"ubuntu@{st['ip']}:{dest}/tokens.json"], check=True)
aws_deploy.ssh(f"chmod 600 {dest}/tokens.json && ls -l {dest}", st)
src.rename(src.with_name("tokens.moved_to_server.json"))
print(f"Account “{name}” is on the server. Pick it in Chinku → Settings → Swiggy account.")
