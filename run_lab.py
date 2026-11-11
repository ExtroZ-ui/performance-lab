import argparse
import base64
import csv
import json
import math
import os
import secrets
import subprocess
import time
from contextlib import ExitStack
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
APP = "http://127.0.0.1:18080"
PROMETHEUS = "http://127.0.0.1:19090"
GRAFANA = "http://127.0.0.1:13000"
WINDOW_SECONDS = 10
CLIENTS = 96
DURATION = 42


def request(url, payload=None, auth=None):
    headers = {}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    if auth:
        headers["Authorization"] = f"Basic {auth}"
    with urlopen(Request(url, data=data, headers=headers), timeout=5) as response:
        return response.read()


def get_json(url, **kwargs):
    return json.loads(request(url, **kwargs))


def wait_ready(url, process, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"{url}: process exited with {process.returncode}")
        try:
            return request(url)
        except (URLError, TimeoutError):
            time.sleep(0.5)
    raise TimeoutError(url)


def start(stack, args, log, **kwargs):
    stream = stack.enter_context(log.open("wb"))
    process = subprocess.Popen(
        [str(arg) for arg in args], stdout=stream, stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        **kwargs,
    )
    stack.callback(stop, process)
    return process


def stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def snapshot(mode):
    url = APP + "/actuator/metrics/http.server.requests?" + urlencode({"tag": f"uri:/work/{mode}"})
    clock_start, wall_start = time.monotonic(), time.time()
    response = get_json(url)
    clock_end = time.monotonic()
    elapsed = clock_end - clock_start
    count = next(int(point["value"]) for point in response["measurements"] if point["statistic"] == "COUNT")
    return {
        "count": count, "unix_seconds": wall_start + elapsed / 2,
        "monotonic_seconds": clock_start + elapsed / 2,
        "poll_ms": elapsed * 1000, "url": url, "response": response,
    }


def sample_metrics(path, before, after):
    with path.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    measured = []
    for row in rows:
        finished = (int(row["timeStamp"]) + int(row["elapsed"])) / 1000
        if before["unix_seconds"] <= finished < after["unix_seconds"]:
            measured.append(int(row["elapsed"]))
    if not measured:
        raise RuntimeError(f"No JMeter samples in measurement window: {path}")
    failures = sum(row["success"].lower() != "true" for row in rows)
    return {
        "total": len(rows), "failures": failures,
        "window_count": len(measured),
        "window_p95_ms": sorted(measured)[math.ceil(0.95 * len(measured)) - 1],
    }


def measure(stack, jmeter_home, mode, run):
    path = run / f"{mode}.jtl"
    java = "java"
    started = time.time()
    process = start(stack, [
        java, "-Xms128m", "-Xmx512m", "-jar", jmeter_home / "bin" / "ApacheJMeter.jar",
        "-n", "-t", ROOT / "load" / "throughput.jmx", "-l", path,
        "-j", run / f"{mode}-jmeter.log", f"-Jmode={mode}", f"-Jclients={CLIENTS}", f"-Jduration={DURATION}",
    ], run / f"{mode}-console.log")
    time.sleep(15)
    if process.poll() is not None:
        raise RuntimeError(f"JMeter stopped during warm-up. See {mode}-console.log")
    before = snapshot(mode)
    time.sleep(WINDOW_SECONDS)
    after = snapshot(mode)
    elapsed = after["monotonic_seconds"] - before["monotonic_seconds"]
    count = after["count"] - before["count"]
    if count <= 0:
        raise RuntimeError(f"No requests finished in Actuator measurement window: {mode}")
    if process.wait(timeout=40) != 0:
        raise RuntimeError(f"JMeter failed: {mode}")
    ended = time.time()
    return {
        "mode": mode, "started": started, "ended": ended,
        "before": before, "after": after,
        "window_seconds": elapsed, "requests": count, "requests_per_second": count / elapsed,
        "jmeter": sample_metrics(path, before, after),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--prometheus", type=Path, required=True)
    parser.add_argument("--grafana-home", type=Path, required=True)
    parser.add_argument("--jmeter-home", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--capture", nargs="+")
    parser.add_argument("--keep-open", action="store_true")
    options = parser.parse_args()
    run = options.run_dir.resolve()
    run.mkdir(parents=True, exist_ok=False)
    for directory in ("grafana-data", "grafana-logs", "grafana-plugins"):
        (run / directory).mkdir()
    dashboard = json.loads((ROOT / "monitoring" / "grafana-dashboard.json").read_text(encoding="utf-8"))
    password = secrets.token_urlsafe(18)
    auth = base64.b64encode(f"admin:{password}".encode()).decode()
    environment = os.environ | {
        "GF_SERVER_HTTP_ADDR": "127.0.0.1",
        "GF_SERVER_HTTP_PORT": "13000",
        "GF_AUTH_ANONYMOUS_ENABLED": "true",
        "GF_SECURITY_ADMIN_PASSWORD": password,
        "GF_ANALYTICS_REPORTING_ENABLED": "false",
        "GF_ANALYTICS_CHECK_FOR_UPDATES": "false",
        "GF_ANALYTICS_CHECK_FOR_PLUGIN_UPDATES": "false",
        "GF_LOG_LEVEL": "warn",
        "GF_PATHS_DATA": str(run / "grafana-data"),
        "GF_PATHS_LOGS": str(run / "grafana-logs"),
        "GF_PATHS_PLUGINS": str(run / "grafana-plugins"),
    }
    jmeter_home = options.jmeter_home.resolve()
    grafana_home = options.grafana_home.resolve()
    grafana = grafana_home / "bin" / ("grafana-server.exe" if os.name == "nt" else "grafana-server")
    with ExitStack() as stack:
        app = start(stack, ["java", "-jar", ROOT / "target" / "performance-lab-1.0.0.jar"], run / "app.log")
        prometheus = start(stack, [
            options.prometheus.resolve(), f"--config.file={ROOT / 'monitoring' / 'prometheus.yml'}",
            f"--storage.tsdb.path={run / 'prometheus-data'}", "--web.listen-address=127.0.0.1:19090",
        ], run / "prometheus.log")
        grafana_process = start(stack, [grafana, "--homepath", grafana_home], run / "grafana.log", env=environment)
        wait_ready(APP + "/actuator/health", app)
        wait_ready(PROMETHEUS + "/-/ready", prometheus)
        wait_ready(GRAFANA + "/api/health", grafana_process)
        grafana_version = get_json(GRAFANA + "/api/health")["version"]
        get_json(GRAFANA + "/api/datasources", auth=auth, payload={
            "uid": "lab-prom", "name": "Prometheus", "type": "prometheus",
            "access": "proxy", "url": PROMETHEUS, "isDefault": True, "jsonData": {"timeInterval": "2s"},
        })
        get_json(GRAFANA + "/api/dashboards/db", auth=auth, payload={"dashboard": dashboard, "overwrite": False})
        flags = {mode: get_json(f"{APP}/work/{mode}") for mode in ("platform", "virtual")}
        assert flags == {"platform": {"virtual": False}, "virtual": {"virtual": True}}
        for _ in range(15):
            targets = get_json(PROMETHEUS + "/api/v1/targets")["data"]["activeTargets"]
            if any(target["health"] == "up" and target["labels"]["job"] == "performance-lab" for target in targets):
                break
            time.sleep(1)
        else:
            raise RuntimeError("Prometheus could not scrape application metrics")
        print("Actuator, Prometheus and Grafana are ready.", flush=True)

        platform = measure(stack, jmeter_home, "platform", run)
        print(f"Platform threads: {platform['requests_per_second']:.2f} req/s", flush=True)
        time.sleep(12)
        virtual = measure(stack, jmeter_home, "virtual", run)
        print(f"Virtual threads: {virtual['requests_per_second']:.2f} req/s", flush=True)
        time.sleep(7)
        evidence = {
            "clients": CLIENTS, "load_seconds": DURATION, "runtime_flags": flags, "grafana_version": grafana_version,
            "platform": platform, "virtual": virtual,
            "grafana_from_ms": int((platform["started"] - 2) * 1000),
            "grafana_to_ms": int(time.time() * 1000),
        }
        for panel in dashboard["panels"]:
            query = panel["targets"][0]["expr"]
            params = urlencode({
                "query": query, "start": evidence["grafana_from_ms"] / 1000,
                "end": evidence["grafana_to_ms"] / 1000, "step": "2s",
            })
            results = get_json(PROMETHEUS + "/api/v1/query_range?" + params)["data"]["result"]
            if len(results) != 2:
                raise RuntimeError(f"Expected both thread types in panel {panel['id']}")
            (run / f"prometheus-panel-{panel['id']}.json").write_text(json.dumps(results), encoding="utf-8")
        (run / "measurements.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
        if options.capture:
            subprocess.run([*options.capture, str(run)], check=True)
        if options.keep_open:
            bounds = urlencode({"from": evidence["grafana_from_ms"], "to": evidence["grafana_to_ms"]})
            print(f"{GRAFANA}/d/performance-lab/throughput?{bounds}")
            input("Press Enter to stop the lab. ")


if __name__ == "__main__":
    main()
