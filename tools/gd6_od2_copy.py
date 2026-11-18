# -*- coding: utf-8 -*-
"""gd6:老  ->  od2:老   单向备份（copy，源端只读，不删不移）。

幂等：目标端「同名 + 字节数一致」= 已备份 → 跳过，所以可以反复跑。
增量：靠两边 lsjson 比对；state.json 只是记账，不是判断依据（判断看目标端真实状态）。

跨盘没有 server-side copy，rclone 只能「下载到 runner → 上传到目标」：
  * 小于 --streaming-over-gb 的文件：正常 copyto（落 /tmp 再传，可断点重来）
  * 大于该值的文件：加 --streaming-upload 边读边写，不吃磁盘（速度低些但放得下 30GB+）
  * 剩余磁盘不够又不走 streaming 的，直接跳过并记 oversize，明说原因

用法：
  python3 tools/gd6_od2_copy.py                     # 演练：只打计划
  python3 tools/gd6_od2_copy.py --apply             # 真拷
  python3 tools/gd6_od2_copy.py --apply --only SHKD-583 --max-gb 10
环境：
  RCLONE_CONF_PATH   rclone.conf 路径（CI 由 Secret RCLONE_CONF 落到 /tmp/rclone.conf）
  GO_SRC / GO_DST    默认 gd6:老 / od2:老
  TG_TOKEN / TG_CHAT_ID  可选，发摘要
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time

GB = 1024 ** 3
MB = 1024 ** 2
RCLONE = os.environ.get('RCLONE_BIN') or shutil.which('rclone') or 'rclone'
CONF = os.environ.get('RCLONE_CONF_PATH', '/tmp/rclone.conf')
STATE = os.environ.get('GO_STATE', 'state.json')
SRC = os.environ.get('GO_SRC', 'gd6:老')
DST = os.environ.get('GO_DST', 'od2:老')


def log(m):
    print('%s %s' % (time.strftime('%H:%M:%S'), m), flush=True)


def human(b):
    return '%.2f GiB' % (b / GB) if b >= GB else '%.1f MiB' % (b / MB)


def rclone(args, timeout=7200, check=True):
    cmd = [RCLONE, '--config', CONF] + args
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError('rclone %s 失败: %s' % (args[0], (r.stderr or r.stdout or '')[:300]))
    return r


def lsjson(remote, timeout=1800):
    r = rclone(['lsjson', remote, '--files-only', '--no-modtime', '--no-mimetype'], timeout=timeout)
    return json.loads(r.stdout or '[]')


def copy_one(name, size, streaming=False, retries=3):
    src = '%s/%s' % (SRC.rstrip('/'), name)
    dst = '%s/%s' % (DST.rstrip('/'), name)
    args = ['copyto', src, dst,
            '--drive-chunk-size', '128M',
            '--onedrive-chunk-size', '100M',
            '--transfers', '1',
            '--retries', str(retries),
            '--low-level-retries', '10',
            '--timeout', '1h',
            '--contimeout', '1m',
            '--stats-one-line', '--stats', '30s']
    if streaming:
        args += ['--streaming-upload', '--ignore-checksum']
    t0 = time.time()
    r = rclone(args, timeout=18000, check=False)
    return (r.returncode == 0), time.time() - t0, (r.stderr or '')[-300:]


def load_state():
    if os.path.exists(STATE):
        try:
            return json.load(open(STATE, encoding='utf-8'))
        except Exception:
            pass
    return {'done': {}, 'failed': {}, 'oversize': []}


def save_state(st):
    json.dump(st, open(STATE, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)


def tg(text):
    tok = os.environ.get('TG_TOKEN', '')
    chat = os.environ.get('TG_CHAT_ID', '')
    if not tok or not chat:
        return
    import urllib.parse
    import urllib.request
    data = urllib.parse.urlencode({'chat_id': chat, 'text': text[:3500]}).encode()
    try:
        urllib.request.urlopen('https://api.telegram.org/bot%s/sendMessage' % tok, data=data, timeout=30)
    except Exception as e:
        log('TG 发送失败: %s' % str(e)[:80])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true', help='真拷；不加=只演练')
    ap.add_argument('--only', default='', help='只处理名字含该子串的')
    ap.add_argument('--max-gb', type=float, default=0, help='本轮总量上限(GiB)，0=不限')
    ap.add_argument('--max-files', type=int, default=0, help='本轮文件数上限，0=不限')
    ap.add_argument('--budget-min', type=int, default=300, help='本轮时间预算(分钟)')
    ap.add_argument('--reserve-gb', type=float, default=8, help='给 runner 留的磁盘余量')
    ap.add_argument('--streaming-over-gb', type=float, default=6,
                    help='大于该值的文件走 --streaming-upload（不吃磁盘）')
    ap.add_argument('--order', default='small', choices=['small', 'large', 'name'])
    a = ap.parse_args()

    if not os.path.exists(CONF):
        raise SystemExit('找不到 rclone.conf: %s' % CONF)
    log('源 %s  →  目标 %s   (%s)' % (SRC, DST, '真拷' if a.apply else '演练'))

    src = [x for x in lsjson(SRC) if not x.get('IsDir')]
    dst = {x['Name']: (x.get('Size') or 0) for x in lsjson(DST) if not x.get('IsDir')}
    log('源 %d 个 / %s   目标 %d 个 / %s'
        % (len(src), human(sum(x.get('Size') or 0 for x in src)),
           len(dst), human(sum(dst.values()))))

    todo = []
    for x in src:
        n, s = x['Name'], x.get('Size') or 0
        if a.only and a.only not in n:
            continue
        if n in dst:
            if dst[n] == s:
                continue
            log('  大小不符(目标 %s / 源 %s) → 重传: %s' % (human(dst[n]), human(s), n[:56]))
        todo.append((n, s))

    if a.order == 'small':
        todo.sort(key=lambda t: t[1])
    elif a.order == 'large':
        todo.sort(key=lambda t: -t[1])
    else:
        todo.sort(key=lambda t: t[0])

    free = shutil.disk_usage('/tmp').free
    log('待备份 %d 个 / %s；runner /tmp 剩余 %s' % (len(todo), human(sum(s for _, s in todo)), human(free)))

    if not a.apply:
        for n, s in todo[:25]:
            mode = 'stream' if s > a.streaming_over_gb * GB else 'copyto'
            log('   [计划] %9s  %-9s %s' % (human(s), mode, n[:60]))
        if len(todo) > 25:
            log('   … 还有 %d 个' % (len(todo) - 25))
        log('演练结束（加 --apply 才真拷）')
        return

    st = load_state()
    t0 = time.time()
    done_n = done_b = 0
    fails = []
    for n, s in todo:
        el_min = (time.time() - t0) / 60.0
        if el_min > a.budget_min:
            log('时间预算用完(%d 分钟)，本轮收工' % a.budget_min)
            break
        if a.max_files and done_n >= a.max_files:
            log('文件数到上限(%d)，本轮收工' % a.max_files)
            break
        if a.max_gb and (done_b + s) > a.max_gb * GB and done_n > 0:
            log('总量到上限(%.0f GiB)，本轮收工' % a.max_gb)
            break

        streaming = s > a.streaming_over_gb * GB
        need = s if not streaming else 0
        if need and need > (free - a.reserve_gb * GB):
            log('  跳过(磁盘不够且未走 streaming): %s  %s' % (human(s), n[:56]))
            if n not in st['oversize']:
                st['oversize'].append(n)
            save_state(st)
            continue

        log('→ %s %s  (%s)' % (human(s), n[:64], 'streaming' if streaming else 'copyto'))
        ok, dt, err = copy_one(n, s, streaming)
        if ok:
            sp = s / MB / max(dt, 1e-6)
            log('  ✓ %s / %.0fs = %.2f MiB/s' % (human(s), dt, sp))
            st['done'][n] = {'size': s, 'ts': time.strftime('%Y-%m-%d %H:%M'), 'mibps': round(sp, 2)}
            st['failed'].pop(n, None)
            done_n += 1
            done_b += s
            free -= 0 if streaming else s
        else:
            log('  ✗ 失败: %s' % err.replace('\n', ' ')[:200])
            st['failed'][n] = {'ts': time.strftime('%Y-%m-%d %H:%M'), 'err': err[-200:]}
            fails.append(n)
        save_state(st)

    el = (time.time() - t0) / 60.0
    left = len(todo) - done_n - len(fails)
    log('本轮完成 %d 个 / %s，失败 %d，剩余待办 %d，用时 %.1f 分钟'
        % (done_n, human(done_b), len(fails), max(0, left), el))
    tg('gd6:老 → od2:老\n本轮 %d 个 / %s，失败 %d，待办剩 %d，用时 %.0f 分钟'
       % (done_n, human(done_b), len(fails), max(0, left), el))


if __name__ == '__main__':
    main()
