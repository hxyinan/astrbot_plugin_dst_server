#!/usr/bin/env python3
"""DST 分片单核占用查询。

读取 /proc/stat 与 /proc/<pid>/cmdline，输出每个分片的单核 CPU 占用百分比、
内存占用、绑定核心。无第三方依赖，Linux 专用（依赖 /proc）。

原版把集群名写死成 Cluster_1 / Cluster_2，这里改成从环境变量读取，
未指定的集群不会出现在结果里。

用法：
    python3 get_dst_cpu.py
    DST_CLUSTERS=Cluster_1 python3 get_dst_cpu.py
    DST_CLUSTERS=Cluster_A,Cluster_B python3 get_dst_cpu.py

输出：JSON 对象，key 为 "<序号>_<分片>"，如 "1_Master"、"2_Caves"。
"""
import glob
import json
import os
import time


def _read_cpu_stats():
    """返回 {cpuN: (累计总时间, 累计空闲时间)}"""
    stats = {}
    with open('/proc/stat') as f:
        for line in f:
            if line.startswith('cpu') and line[3:4].isdigit():
                parts = line.split()
                values = [float(v) for v in parts[1:8]]
                stats[parts[0]] = (sum(values), values[3] + values[4])
    return stats


def _iter_dst_processes():
    """遍历 /proc，产出 (pid, cmdline)，只保留 DST 分片进程。"""
    for path in glob.glob('/proc/[0-9]*/cmdline'):
        try:
            with open(path, 'rb') as f:
                raw = f.read().decode('utf-8', 'ignore')
        except (OSError, IOError):
            continue
        if not raw or raw.startswith('SCREEN'):
            continue
        parts = raw.split('\x00')
        if not parts or 'dontstarve_dedicated_server' not in parts[0]:
            continue
        if '-cluster' not in raw or '-shard' not in raw:
            continue
        yield path.split('/')[2], raw


def _shard_of(cmd):
    if '-shard\x00Master' in cmd or '-shard Master' in cmd:
        return 'Master'
    if '-shard\x00Caves' in cmd or '-shard Caves' in cmd:
        return 'Caves'
    return ''


def _cluster_of(cmd, clusters):
    for name in clusters:
        if name in cmd:
            return name
    return ''


def main():
    clusters = [c.strip() for c in
                os.environ.get('DST_CLUSTERS', 'Cluster_1,Cluster_2').split(',')
                if c.strip()]

    worlds = {}
    for idx, cluster in enumerate(clusters, start=1):
        for shard, label in (('Master', '地面'), ('Caves', '洞穴')):
            worlds[f'{idx}_{shard}'] = {
                'name': f'{idx}服{label}',
                'cluster': cluster,
                'clusterIndex': idx,
                'shard': shard,
                'online': False,
                'load': 0.0,
                'core': '',
                'mem': '0 MB',
            }

    before = _read_cpu_stats()
    time.sleep(0.4)
    after = _read_cpu_stats()

    for pid, cmd in _iter_dst_processes():
        shard = _shard_of(cmd)
        cluster = _cluster_of(cmd, clusters)
        if not shard or not cluster:
            continue
        key = f'{clusters.index(cluster) + 1}_{shard}'
        state = worlds.get(key)
        if state is None:
            continue
        state['online'] = True
        try:
            with open(f'/proc/{pid}/status') as f:
                for line in f:
                    if line.startswith('VmRSS:'):
                        kb = int(line.split()[1])
                        gb = kb / 1048576
                        state['mem'] = f'{gb:.2f} GB' if gb >= 1 else f'{int(kb / 1024)} MB'
                    elif line.startswith('Cpus_allowed_list:'):
                        raw_cores = line.split(':', 1)[1].strip()
                        cores = [f'cpu{x.strip()}' for x in raw_cores.split(',')
                                 if x.strip().isdigit()]
                        if cores:
                            state['core'] = f'Core {cores[0][3:]}'
                        if cores and cores[0] in before and cores[0] in after:
                            active = sum(
                                (after[c][0] - before[c][0]) - (after[c][1] - before[c][1])
                                for c in cores if c in after and c in before
                            )
                            total = after[cores[0]][0] - before[cores[0]][0]
                            pct = round(active / total * 100, 1) if total > 0 else 0.0
                            state['load'] = min(pct, 100.0)
                        break
        except (OSError, IOError):
            pass

    print(json.dumps(worlds, ensure_ascii=False))


if __name__ == '__main__':
    main()
