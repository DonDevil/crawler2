#!/usr/bin/env python3
"""WP1 read-only system monitor. Writes timestamped JSONL samples; changes no system settings.

usage: wp1_monitor.py OUT.jsonl [--interval 2] [--docker-every 10] [--abort-flag PATH]
Stop with SIGTERM/SIGINT. Sets --abort-flag (file) on hard limits; CPU package >= 95 C writes <flag>.thermal (advisory):
free disk < 20 GiB, swap used > 2 GiB, MemAvailable < 1.5 GiB.
Sources: /proc, /sys (hwmon, cpufreq, thermal_throttle), nvidia-smi, docker (stats/inspect).
"""

# ruff: noqa: BLE001, S603, S607, E501, E741, E731, SIM115, RUF005
# (measurement tooling: broad excepts keep sampling alive when a sensor is missing; fixed argv lists only;
#  asserts guard the bench directory/bucket names; PRNG is for I/O offsets, not security.)
import argparse
import glob
import json
import os
import signal
import subprocess
import threading
import time

DISK = "nvme0n1"
STOP = False


def _stop(*_):
    global STOP
    STOP = True


signal.signal(signal.SIGTERM, _stop)
signal.signal(signal.SIGINT, _stop)


def rd(p, default=None):
    try:
        with open(p) as f:
            return f.read().strip()
    except Exception:
        return default


def cpu_times():
    out = {}
    for line in open("/proc/stat"):
        if line.startswith("cpu"):
            p = line.split()
            v = list(map(int, p[1:9]))
            out[p[0]] = (sum(v), v[3] + v[4], v[4])  # total, idle+iowait, iowait
    return out


def diskstats():
    for line in open("/proc/diskstats"):
        p = line.split()
        if p[2] == DISK:
            return dict(
                rd=int(p[3]),
                rsec=int(p[5]),
                rms=int(p[6]),
                wr=int(p[7]),
                wsec=int(p[9]),
                wms=int(p[10]),
                busy=int(p[12]),
            )
    return None


def hwmon(name):
    for d in glob.glob("/sys/class/hwmon/hwmon*"):
        if rd(d + "/name") == name:
            return d


def temp_c(path):
    v = rd(path)
    return int(v) / 1000 if v else None


HW_CORE, HW_NVME = hwmon("coretemp"), hwmon("nvme")


def temps():
    t = {}
    if HW_CORE:
        for f in glob.glob(HW_CORE + "/temp*_input"):
            lab = rd(f.replace("_input", "_label"), os.path.basename(f))
            t[lab] = temp_c(f)
    return t


def throttle():
    pk = co = 0
    for d in glob.glob("/sys/devices/system/cpu/cpu*/thermal_throttle"):
        pk += int(rd(d + "/package_throttle_count", 0))
        co += int(rd(d + "/core_throttle_count", 0))
    return pk, co


def freqs():
    v = [
        int(x)
        for x in (
            rd(f) for f in glob.glob("/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq")
        )
        if x
    ]
    return (min(v) // 1000, sum(v) // len(v) // 1000, max(v) // 1000) if v else (None,) * 3


def meminfo():
    m = {l.split(":")[0]: int(l.split()[1]) for l in open("/proc/meminfo")}
    return dict(
        total_mib=m["MemTotal"] // 1024,
        avail_mib=m["MemAvailable"] // 1024,
        used_mib=(m["MemTotal"] - m["MemAvailable"]) // 1024,
        swap_used_mib=(m["SwapTotal"] - m["SwapFree"]) // 1024,
        dirty_mib=m["Dirty"] // 1024,
    )


def psi(name):
    out = {}
    for l in (rd("/proc/pressure/" + name) or "").splitlines():
        p = l.split()
        out[p[0]] = float(p[1].split("=")[1])
    return out


def oom_kills():
    for l in open("/proc/vmstat"):
        if l.startswith("oom_kill "):
            return int(l.split()[1])


def gpu():
    try:
        q = "utilization.gpu,memory.used,temperature.gpu,power.draw,clocks.sm,clocks_event_reasons.active"
        o = (
            subprocess.run(
                ["nvidia-smi", f"--query-gpu={q}", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            .stdout.strip()
            .split(", ")
        )
        f = lambda x: float(x) if x.replace(".", "", 1).isdigit() else x
        return dict(
            util=f(o[0]),
            vram_mib=f(o[1]),
            temp=f(o[2]),
            power_w=f(o[3]),
            sm_mhz=f(o[4]),
            throttle=o[5],
        )
    except Exception as e:
        return dict(error=str(e)[:80])


docker_state = {}


def docker_loop(every):
    while not STOP:
        try:
            st = (
                subprocess.run(
                    [
                        "docker",
                        "stats",
                        "--no-stream",
                        "--format",
                        "{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}|{{.BlockIO}}",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                .stdout.strip()
                .splitlines()
            )
            names = [l.split("|")[0] for l in st]
            ins = {}
            if names:
                o = (
                    subprocess.run(
                        [
                            "docker",
                            "inspect",
                            "--format",
                            "{{.Name}}|{{.RestartCount}}|{{if .State.Health}}{{.State.Health.Status}}{{end}}|{{.State.OOMKilled}}",
                        ]
                        + names,
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                    .stdout.strip()
                    .splitlines()
                )
                for l in o:
                    n, rc, h, oom = l.split("|")
                    ins[n.lstrip("/")] = (int(rc), h, oom)
            docker_state.clear()
            for l in st:
                n, cpu, mem, blk = l.split("|")
                rc, h, oom = ins.get(n, (None, None, None))
                docker_state[n] = dict(cpu=cpu, mem=mem, blk=blk, restarts=rc, health=h, oom=oom)
        except Exception as e:
            docker_state.clear()
            docker_state["error"] = str(e)[:80]
        for _ in range(int(every * 2)):
            if STOP:
                break
            time.sleep(0.5)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--interval", type=float, default=2)
    ap.add_argument("--docker-every", type=float, default=10)
    ap.add_argument("--abort-flag")
    a = ap.parse_args()
    threading.Thread(target=docker_loop, args=(a.docker_every,), daemon=True).start()
    prev_c, prev_d, prev_t = cpu_times(), diskstats(), time.time()
    with open(a.out, "a", buffering=1) as out:
        while not STOP:
            time.sleep(a.interval)
            now = time.time()
            c, d = cpu_times(), diskstats()
            dt = now - prev_t

            def pct(k, c=c, prev_c=prev_c):
                return round(
                    100 * (1 - (c[k][1] - prev_c[k][1]) / max(1, c[k][0] - prev_c[k][0])), 1
                )

            iow = round(
                100 * (c["cpu"][2] - prev_c["cpu"][2]) / max(1, c["cpu"][0] - prev_c["cpu"][0]), 1
            )
            dd = {k: d[k] - prev_d[k] for k in d}
            io = dict(
                r_mbps=round(dd["rsec"] * 512 / 1e6 / dt, 1),
                w_mbps=round(dd["wsec"] * 512 / 1e6 / dt, 1),
                r_iops=round(dd["rd"] / dt),
                w_iops=round(dd["wr"] / dt),
                r_await_ms=round(dd["rms"] / dd["rd"], 3) if dd["rd"] else None,
                w_await_ms=round(dd["wms"] / dd["wr"], 3) if dd["wr"] else None,
                util_pct=round(min(100, dd["busy"] / (dt * 10)), 1),
            )
            sv = os.statvfs("/")
            free_gib = round(sv.f_bavail * sv.f_frsize / 2**30, 2)
            t = temps()
            pkg = t.get("Package id 0")
            pk_thr, co_thr = throttle()
            mem = meminfo()
            fmin, favg, fmax = freqs()
            s = dict(
                ts=time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now)),
                epoch=round(now, 1),
                cpu_pct=pct("cpu"),
                cpu_iowait_pct=iow,
                cpu_core_pct=[pct(k) for k in sorted(c) if k != "cpu"],
                load1=os.getloadavg()[0],
                freq_mhz=dict(min=fmin, avg=favg, max=fmax),
                temp_c=t,
                nvme_temp_c=temp_c(HW_NVME + "/temp1_input") if HW_NVME else None,
                throttle_pkg=pk_thr,
                throttle_core=co_thr,
                mem=mem,
                psi_mem=psi("memory"),
                psi_io=psi("io"),
                oom_kill=oom_kills(),
                nvme=io,
                root_free_gib=free_gib,
                gpu=gpu(),
                docker=dict(docker_state),
            )
            out.write(json.dumps(s) + "\n")
            prev_c, prev_d, prev_t = c, d, now
            why = []
            if free_gib < 20:
                why.append(f"free_disk={free_gib}GiB<20")
            if (
                pkg and pkg >= 95 and a.abort_flag
            ):  # thermal: advisory flag only (harness handles it per case)
                open(a.abort_flag + ".thermal", "w").write(s["ts"] + f" cpu_pkg={pkg}C>=95\n")
            if mem["swap_used_mib"] > 2048:
                why.append("swap>2GiB")
            if mem["avail_mib"] < 1536:
                why.append(f"mem_avail={mem['avail_mib']}MiB<1536")
            if why and a.abort_flag:
                open(a.abort_flag, "w").write(s["ts"] + " " + ",".join(why) + "\n")


if __name__ == "__main__":
    main()
