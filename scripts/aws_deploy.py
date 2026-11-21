"""Deploy Chinku to one EC2 instance (ap-south-1) with a stable HTTPS URL.

- Elastic IP  -> stable address; HTTPS via Caddy + Let's Encrypt on <ip-with-dashes>.sslip.io (no domain needed)
- App: git clone of the public repo, venv, uvicorn on 127.0.0.1:8000 (systemd), Caddy reverse proxy :443
- .env is copied over SSH (never via user-data, never committed)

Run:   python -m scripts.aws_deploy create     # first time: key pair, security group, instance, Elastic IP
       python -m scripts.aws_deploy push       # copy .env + pull latest code + restart (after each commit)
       python -m scripts.aws_deploy status
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import boto3

from app.store.dynamo import aws_env

aws_env()
from app.config import settings

REGION = settings().aws_region  # workshop: us-west-2 only
NAME = "chinku"
REPO = "https://github.com/hanish-8880/equal.ai-hakathon.git"
STATE = Path("data/aws_state.json")
KEY_FILE = Path.home() / ".ssh" / "chinku-aws.pem"

USER_DATA = f"""#!/bin/bash
set -eux
apt-get update -y
apt-get install -y python3-venv python3-pip git debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
apt-get update -y && apt-get install -y caddy
sudo -u ubuntu git clone {REPO} /home/ubuntu/chinku
cd /home/ubuntu/chinku && sudo -u ubuntu python3 -m venv .venv && sudo -u ubuntu .venv/bin/pip install -q -r requirements.txt
cat > /etc/systemd/system/chinku.service <<'EOF'
[Unit]
Description=Chinku
After=network.target
[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/chinku
ExecStart=/home/ubuntu/chinku/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
Restart=always
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
"""


def ec2():
    return boto3.client("ec2", region_name=REGION)


def load() -> dict:
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def save(d: dict) -> None:
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(d, indent=2))


def host(ip: str) -> str:
    return ip.replace(".", "-") + ".sslip.io"


ROLE = "chinku-ec2"


def instance_role() -> str | None:
    """Instance profile with DynamoDB access so the server never needs (expiring) keys. None if IAM is blocked."""
    iam = boto3.client("iam")
    try:
        try:
            iam.get_role(RoleName=ROLE)
        except iam.exceptions.NoSuchEntityException:
            iam.create_role(RoleName=ROLE, AssumeRolePolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": [
                {"Effect": "Allow", "Principal": {"Service": "ec2.amazonaws.com"}, "Action": "sts:AssumeRole"}]}))
            iam.put_role_policy(RoleName=ROLE, PolicyName="chinku-dynamo", PolicyDocument=json.dumps({
                "Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "dynamodb:*",
                "Resource": f"arn:aws:dynamodb:{REGION}:*:table/chinku_*"}]}))
        try:
            iam.get_instance_profile(InstanceProfileName=ROLE)
        except iam.exceptions.NoSuchEntityException:
            iam.create_instance_profile(InstanceProfileName=ROLE)
            iam.add_role_to_instance_profile(InstanceProfileName=ROLE, RoleName=ROLE)
            time.sleep(12)  # IAM propagation
        return ROLE
    except Exception as e:  # noqa: BLE001
        print("instance role NOT possible:", str(e)[:200])
        return None


def create() -> None:
    c, st = ec2(), load()
    if "instance_id" in st:
        print("already created:", st)
        return
    if not KEY_FILE.exists():
        KEY_FILE.parent.mkdir(exist_ok=True)
        try:
            c.delete_key_pair(KeyName=NAME)
        except Exception:  # noqa: BLE001
            pass
        k = c.create_key_pair(KeyName=NAME)
        KEY_FILE.write_text(k["KeyMaterial"])
        print("key saved", KEY_FILE)
    vpc = c.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"][0]["VpcId"]
    sgs = c.describe_security_groups(Filters=[{"Name": "group-name", "Values": [NAME]}])["SecurityGroups"]
    if sgs:
        sg = sgs[0]["GroupId"]
    else:
        sg = c.create_security_group(GroupName=NAME, Description="chinku web+ssh", VpcId=vpc)["GroupId"]
        c.authorize_security_group_ingress(GroupId=sg, IpPermissions=[
            {"IpProtocol": "tcp", "FromPort": p, "ToPort": p, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]} for p in (22, 80, 443)])
    ami = boto3.client("ssm", region_name=REGION).get_parameter(
        Name="/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id")["Parameter"]["Value"]
    role = instance_role()
    extra = {"IamInstanceProfile": {"Name": role}} if role else {}
    try:
        inst = c.run_instances(ImageId=ami, InstanceType="t3.small", MinCount=1, MaxCount=1, KeyName=NAME, **extra,
                                   SecurityGroupIds=[sg], UserData=USER_DATA,
                               BlockDeviceMappings=[{"DeviceName": "/dev/sda1", "Ebs": {"VolumeSize": 16, "VolumeType": "gp3"}}],
                               TagSpecifications=[{"ResourceType": "instance", "Tags": [{"Key": "Name", "Value": NAME}]}])
    except Exception as e:  # noqa: BLE001 — iam:PassRole blocked → launch without role, file store on server
        if not role:
            raise
        print("PassRole blocked, launching without role:", str(e)[:200])
        role = None
        inst = c.run_instances(ImageId=ami, InstanceType="t3.small", MinCount=1, MaxCount=1, KeyName=NAME,
                               SecurityGroupIds=[sg], UserData=USER_DATA,
                               BlockDeviceMappings=[{"DeviceName": "/dev/sda1", "Ebs": {"VolumeSize": 16, "VolumeType": "gp3"}}],
                               TagSpecifications=[{"ResourceType": "instance", "Tags": [{"Key": "Name", "Value": NAME}]}])
    iid = inst["Instances"][0]["InstanceId"]
    print("instance", iid, "starting…")
    c.get_waiter("instance_running").wait(InstanceIds=[iid])
    alloc = c.allocate_address(Domain="vpc")
    c.associate_address(InstanceId=iid, AllocationId=alloc["AllocationId"])
    st = {"role": role, "store": "dynamo" if role else "file", "instance_id": iid, "sg": sg, "allocation_id": alloc["AllocationId"], "ip": alloc["PublicIp"],
          "host": host(alloc["PublicIp"])}
    save(st)
    print(json.dumps(st, indent=2))
    print("Wait ~3 min for setup, then: python -m scripts.aws_deploy push")


def ssh(cmd: str, st: dict) -> int:
    return subprocess.call(["ssh", "-i", str(KEY_FILE), "-o", "StrictHostKeyChecking=accept-new",
                            f"ubuntu@{st['ip']}", cmd])


def push() -> None:
    # SAFETY GATE: never deploy code whose tests fail (a broken deploy took the server down once — DECISIONS Sat 21:20)
    rc = subprocess.call([sys.executable, "-m", "pytest", "-q", "-x"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if rc != 0:
        print("DEPLOY ABORTED: tests fail locally — run `python -m pytest -q` and fix first.")
        sys.exit(1)
    st = load()
    url = f"https://{st['host']}"
    env = Path(".env").read_text(encoding="utf-8")
    import re
    env = re.sub(r"^PUBLIC_BASE_URL=.*$", f"PUBLIC_BASE_URL={url}", env, flags=re.M)
    env = re.sub(r"^AWS_(ACCESS_KEY_ID|SECRET_ACCESS_KEY|SESSION_TOKEN)=.*\n?", "", env, flags=re.M)  # never ship temp keys
    env = re.sub(r"^STORE_BACKEND=.*$", f"STORE_BACKEND={'dynamo' if st.get('role') else ''}", env, flags=re.M)
    tmp = Path("data/.env.remote")
    tmp.write_text(env, encoding="utf-8")
    subprocess.check_call(["scp", "-i", str(KEY_FILE), "-o", "StrictHostKeyChecking=accept-new", str(tmp),
                           f"ubuntu@{st['ip']}:/home/ubuntu/chinku/.env"])
    tmp.unlink()
    caddy = f"{st['host']} {{\\n  reverse_proxy 127.0.0.1:8000\\n}}\\n"
    # sync EXACTLY to GitHub: files the server generated (eval results) can never block the update (Sun 01:40: a silent
    # `git pull` failure left the old code running while the deploy said OK). .env, media and data are untracked → untouched.
    ssh("cd ~/chinku && git fetch -q origin && git reset -q --hard origin/main && .venv/bin/pip install -q -r requirements.txt && "
        f"printf '{caddy}' | sudo tee /etc/caddy/Caddyfile >/dev/null && sudo systemctl reload-or-restart caddy && "
        "sudo systemctl enable -q chinku && sudo systemctl restart chinku && sleep 3 && curl -s 127.0.0.1:8000/health", st)
    local = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    remote = subprocess.run(["ssh", "-i", str(KEY_FILE), f"ubuntu@{st['ip']}", "cd ~/chinku && git rev-parse HEAD"],
                            capture_output=True, text=True).stdout.strip()
    if remote != local:
        print(f"\n!!! DEPLOY FAILED: server is on {remote[:7] or '?'}, laptop is on {local[:7]} — did you `git push`?")
        sys.exit(3)
    import httpx
    for _ in range(10):
        try:
            if httpx.get(f"{url}/health", timeout=10).status_code == 200:
                print("\nPUBLIC URL:", url, f"— health OK — running {local[:7]}")
                return
        except httpx.HTTPError:
            pass
        time.sleep(2)
    print("\n!!! DEPLOYED BUT HEALTH CHECK FAILED — check `sudo journalctl -u chinku -n 30` on the server")
    sys.exit(2)


def status() -> None:
    st = load()
    print(json.dumps(st, indent=2))
    if st:
        r = ec2().describe_instances(InstanceIds=[st["instance_id"]])["Reservations"][0]["Instances"][0]
        print("state:", r["State"]["Name"])


if __name__ == "__main__":
    {"create": create, "push": push, "status": status}[sys.argv[1]]()
