import json
import os
import socket
import ssl
import sys
import time
import urllib.request
from collections import Counter

REPO = "passerby7890/cfips"
PROBE_HOST = "nodejp.snowcat77.com"
PROBE_PORT = 2053
PROBE_PATH = "/ws?ed=2048"


def fetch_gh_actions_latest(limit=5):
    url = f"https://api.github.com/repos/{REPO}/actions/runs?per_page={limit}"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0", "Accept": "application/vnd.github.v3+json"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data.get("workflow_runs", [])


def fetch_gh_job_logs_summary(run_id):
    url = f"https://api.github.com/repos/{REPO}/actions/runs/{run_id}/jobs"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0", "Accept": "application/vnd.github.v3+json"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    jobs = data.get("jobs", [])
    result = []
    for job in jobs:
        steps = [
            f"{s['name']}:{s['conclusion']}" for s in job.get("steps", [])
        ]
        result.append({
            "name": job.get("name"),
            "status": job.get("status"),
            "conclusion": job.get("conclusion"),
            "started_at": job.get("started_at"),
            "completed_at": job.get("completed_at"),
            "steps": steps,
        })
    return result


def test_ws_101(ip, timeout=3.5):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    t0 = time.time()
    try:
        sock = socket.create_connection((ip, PROBE_PORT), timeout=timeout)
        ssock = ctx.wrap_socket(sock, server_hostname=PROBE_HOST)
        req = (
            f"GET {PROBE_PATH} HTTP/1.1\r\n"
            f"Host: {PROBE_HOST}:{PROBE_PORT}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        ssock.sendall(req.encode("ascii"))
        resp = ssock.recv(1024).decode("utf-8", errors="ignore")
        dt = round((time.time() - t0) * 1000, 1)
        ssock.close()
        ok = "101 Switching Protocols" in resp
        return ok, dt, resp.splitlines()[0] if resp else "empty"
    except Exception as e:
        return False, round((time.time() - t0) * 1000, 1), str(e)


def analyze_output_dir(output_dir="output"):
    best_path = os.path.join(output_dir, "best_ips.json")
    best = {}
    if os.path.exists(best_path):
        with open(best_path, "r", encoding="utf-8") as f:
            best = json.load(f)

    summary = {"updated_at": best.get("updated_at"), "carriers": {}}
    for c in ("ct", "cu", "cm"):
        txt_path = os.path.join(output_dir, f"{c}.txt")
        lines = []
        if os.path.exists(txt_path):
            with open(txt_path, "r", encoding="utf-8") as f:
                lines = [ln.strip() for ln in f if ln.strip()]

        ips = [ln.split("#")[0] for ln in lines]
        regions = Counter(ln.split("#")[1] if "#" in ln else "UNKNOWN" for ln in lines)
        subnets = sorted(set(".".join(ip.split(".")[:3]) + ".0/24" for ip in ips))

        details = best.get("carriers", {}).get(c, [])
        rtts = [d.get("real_cn_ms", d.get("cn_latency_ms")) for d in details if isinstance(d.get("real_cn_ms", d.get("cn_latency_ms")), (int, float))]
        losses = [d.get("real_cn_loss", d.get("cn_loss")) for d in details if isinstance(d.get("real_cn_loss", d.get("cn_loss")), (int, float))]

        # Verify every IP passes 2053/ws 101 locally
        ws_ok = 0
        ws_fail = []
        for ip in ips:
            ok, dt, msg = test_ws_101(ip)
            if ok:
                ws_ok += 1
            else:
                ws_fail.append(f"{ip} ({msg})")

        summary["carriers"][c] = {
            "count": len(ips),
            "subnet_count": len(subnets),
            "subnets": subnets,
            "regions": dict(regions),
            "rtt_min": min(rtts) if rtts else None,
            "rtt_median": sorted(rtts)[len(rtts) // 2] if rtts else None,
            "rtt_max": max(rtts) if rtts else None,
            "loss_max": max(losses) if losses else None,
            "loss_zero_count": sum(1 for x in losses if x == 0),
            "ws_101_ok": ws_ok,
            "ws_101_total": len(ips),
            "ws_101_fails": ws_fail,
            "ips": lines,
        }
    return summary


def main():
    runs = fetch_gh_actions_latest(5)
    print("=== Latest GitHub Actions Runs ===")
    for r in runs:
        print(
            f"Run {r['id']} | {r['status']:<11} | {str(r['conclusion']):<10} | "
            f"{r['event']:<10} | {r['head_sha'][:7]} | {r['created_at']} | {r.get('display_title')}"
        )
    if runs:
        latest = runs[0]
        jobs = fetch_gh_job_logs_summary(latest["id"])
        print("\n=== Latest Run Jobs ===")
        for j in jobs:
            print(f"Job: {j['name']} | {j['status']} / {j['conclusion']} | {j['started_at']} -> {j['completed_at']}")
            for st in j["steps"]:
                print("  -", st)

    print("\n=== Local Output Verification ===")
    summary = analyze_output_dir("output")
    print(f"best_ips.json updated_at: {summary['updated_at']}")
    for c, info in summary["carriers"].items():
        print(
            f"[{c.upper()}] count={info['count']} subnets={info['subnet_count']} "
            f"regions={info['regions']} RTT(min/med/max)={info['rtt_min']}/{info['rtt_median']}/{info['rtt_max']}ms "
            f"loss_max={info['loss_max']}% (0%-loss={info['loss_zero_count']}) "
            f"ws101={info['ws_101_ok']}/{info['ws_101_total']}"
        )
        if info["ws_101_fails"]:
            print("  FAILS:", info["ws_101_fails"])
        print("  IPs:", ", ".join(info["ips"]))


if __name__ == "__main__":
    main()
