# -*- coding: utf-8 -*-
"""gd6:out3 根目录 → od2:字幕   srt 备份（copy，源端只读，不删不移）

选片规则（三个条件同时成立）：
  1) 只在 out3 的**根目录**（lsjson 非递归，不进子目录）
  2) 文件名以 已处理_ 开头，且以 .srt 结尾（大小写不敏感）
  3) 字节数 > --min-kb * 1024（默认 min-kb=1 → 大于 1 KiB；小残片不搬）

幂等：目标端「同名 + 字节数一致」= 已备份 → 跳过，可反复跑。
增量：靠两边 lsjson 比对；state_srt.json 只是记账，不是判断依据。

用法：
  python3 tools/srt_od2_subtitle.py                     # 演练：只打计划
  python3 tools/srt_od2_subtitle.py --apply             # 真拷
  python3 tools/srt_od2_subtitle.py --apply --only IBW-832 --min-kb 2
环境：
  RCLONE_CONF_PATH   rclone.conf 路径（CI 由 Secret RCLONE_CONF 落到 /tmp/rclone.conf）
  SRT_SRC / SRT_DST  默认 gd6:out3 / od2:字幕
  TG_TOKEN / TG_CHAT_ID  可选，发摘要
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time

KB = 1024
MB = 1024 ** 2
RCLONE = os.environ.get('RCLONE_BIN') or shutil.which('rclone') or 'rclone'
CONF = os.environ.get('RCLONE_CONF_PATH', '/tmp/rclone.conf')
STATE = os.environ.get('SRT_STATE', 'state_srt.json')
REPORT = os.environ.get('SRT_REPORT', 'srt_report.txt')
SRC = os.environ.get('SRT_SRC', 'gd6:out3')
DST = os.environ.get('SRT_DST', 'od2:字幕')
PREFIX = os.environ.get('SRT_PREFIX', '已处理_')

# Windows 控制台/CI 日志都用 utf-8 更保险
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass


def log(m):
    print('%s %s' % (time.strftime('%H:%M:%S'), m), flush=True)


def human(b):
    return '%.1f KiB' % (b / KB) if b < MB else '%.2f MiB' % (b / MB)


def rclone(args, timeout=3600, check=True):
    cmd = [RCLONE, '--config', CONF] + args
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError('rclone %s 失败: %s' % (args[0], (r.stderr or r.stdout or '')[:400]))
    return r


def lsjson(remote, timeout=1800, must_exist=True):
    """列出远端某个目录（非递归）。目录不存在时按空处理。"""
    r = rclone(['lsjson', remote, '--files-only', '--no-modtime', '--no-mimetype'],
               timeout=timeout, check=False)
    if r.returncode != 0:
        err = (r.stderr or '') + (r.stdout or '')
        if 'not found' in err.lower() or 'directory not found' in err.lower() or 'error 404' in err.lower():
            log('  (目标目录还不存在，按空处理): %s' % remote)
            return []
        if must_exist:
            raise RuntimeError('rclone lsjson %s 失败: %s' % (remote, err[:400]))
        return []
    try:
        return json.loads(r.stdout or '[]')
    except Exception:
        return []


def pick(src_items, prefix, min_bytes, only=''):
    """按三条规则筛出要备份的 srt。"""
    out = []
    for x in src_items:
        if x.get('IsDir'):
            continue
        n = x.get('Name') or ''
        s = x.get('Size') or 0
        if not n.lower().endswith('.srt'):
            continue
        if prefix and not n.startswith(prefix):
            continue
        if s <= min_bytes:            # 严格大于
            continue
        if only and only not in n:
            continue
        out.append((n, s))
    out.sort(key=lambda t: t[0])
    return out


def load_state():
    if os.path.exists(STATE):
        try:
            return json.load(open(STATE, encoding='utf-8'))
        except Exception:
            pass
    return {'done': {}, 'failed': {}, 'skipped_small': 0}


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


def bulk_copy(todo, transfers=8, retries=3):
    """一次 rclone copy --files-from 批量搬；返回 (ok, out, err)。"""
    lst = '/tmp/srt_todo_list.txt'
    with open(lst, 'w', encoding='utf-8', newline='\n') as f:
        for n, _ in todo:
            f.write(n + '\n')
    args = ['copy', SRC, DST,
            '--files-from', lst,
            '--no-traverse',
            '--transfers', str(transfers),
            '--checkers', str(transfers),
            '--retries', str(retries),
            '--low-level-retries', '10',
            '--timeout', '10m',
            '--contimeout', '1m',
            '--onedrive-chunk-size', '10M',
            '--stats', '15s', '--stats-one-line']
    t0 = time.time()
    r = rclone(args, timeout=5400, check=False)
    return r.returncode == 0, time.time() - t0, (r.stderr or '')[-400:]


def copy_one(name, retries=3):
    src = '%s/%s' % (SRC.rstrip('/'), name)
    dst = '%s/%s' % (DST.rstrip('/'), name)
    args = ['copyto', src, dst,
            '--transfers', '1',
            '--retries', str(retries),
            '--low-level-retries', '10',
            '--timeout', '10m',
            '--contimeout', '1m',
            '--onedrive-chunk-size', '10M',
            '--stats', '15s', '--stats-one-line']
    t0 = time.time()
    r = rclone(args, timeout=1800, check=False)
    return r.returncode == 0, time.time() - t0, (r.stderr or '')[-300:]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true', help='真拷；不加=只演练')
    ap.add_argument('--min-kb', type=float, default=1.0, help='文件必须大于该 KiB 数（默认 1）')
    ap.add_argument('--prefix', default=PREFIX, help='文件名前缀（默认 已处理_）')
    ap.add_argument('--only', default='', help='只处理名字含该子串的（调试）')
    ap.add_argument('--max-files', type=int, default=0, help='本轮文件数上限，0=不限')
    ap.add_argument('--budget-min', type=int, default=20, help='本轮时间预算(分钟)')
    ap.add_argument('--transfers', type=int, default=8, help='并发传输数')
    a = ap.parse_args()

    if not os.path.exists(CONF):
        raise SystemExit('找不到 rclone.conf: %s' % CONF)
    min_bytes = int(a.min_kb * KB)
    log('源 %s  →  目标 %s   (%s)' % (SRC, DST, '真拷' if a.apply else '演练'))
    log('规则: 根目录 · 前缀 "%s" · 后缀 .srt · 大小 > %s' % (a.prefix, human(min_bytes)))

    src_items = lsjson(SRC)
    dst_items = lsjson(DST, must_exist=False)
    dst = {x['Name']: (x.get('Size') or 0) for x in dst_items if not x.get('IsDir')}
    log('源根目录 %d 个对象；目标已有 %d 个文件' % (len(src_items), len(dst)))

    cand = pick(src_items, a.prefix, min_bytes, a.only)
    log('命中规则 %d 个 / %s' % (len(cand), human(sum(s for _, s in cand))))

    dropped_small = sum(1 for x in src_items
                        if not x.get('IsDir') and (x.get('Name') or '').startswith(a.prefix)
                        and (x.get('Name') or '').lower().endswith('.srt')
                        and (x.get('Size') or 0) <= min_bytes)
    if dropped_small:
        log('  另有 %d 个 已处理_*.srt 小于等于阈值，按规则不搬' % dropped_small)

    todo, already = [], []
    for n, s in cand:
        if n in dst:
            if dst[n] == s:
                already.append(n)
                continue
            log('  大小不符(目标 %s / 源 %s) → 重传: %s' % (human(dst[n]), human(s), n[:60]))
        todo.append((n, s))

    if a.max_files:
        todo = todo[:a.max_files]
    log('已在目标(同名同大小) %d 个；本轮待备份 %d 个 / %s'
        % (len(already), len(todo), human(sum(s for _, s in todo))))

    if not a.apply:
        for n, s in todo[:40]:
            log('   [计划] %9s  %s' % (human(s), n[:70]))
        if len(todo) > 40:
            log('   … 还有 %d 个' % (len(todo) - 40))
        log('演练结束（加 --apply 才真拷）')
        return

    if not todo:
        log('没有要搬的文件，收工')
        return

    st = load_state()
    t0 = time.time()
    ok_bulk, dt, err = bulk_copy(todo, transfers=a.transfers)
    log('批量搬完成=%s 用时 %.1fs %s' % (ok_bulk, dt, err.replace('\n', ' ')[:200] if not ok_bulk else ''))

    # 复核：以目标端真实状态为准
    dst2 = {x['Name']: (x.get('Size') or 0) for x in lsjson(DST, must_exist=False) if not x.get('IsDir')}
    ok_names, bad = [], []
    for n, s in todo:
        if dst2.get(n) == s:
            ok_names.append((n, s))
        else:
            bad.append((n, s))
    log('复核: 到位 %d / 待补救 %d' % (len(ok_names), len(bad)))

    # 逐条补救
    still_fail = []
    for n, s in bad:
        if (time.time() - t0) / 60.0 > a.budget_min:
            log('时间预算用完，剩余 %d 个下轮继续' % (len(bad) - len(still_fail)))
            still_fail.extend([x for x in bad if x not in still_fail])
            break
        ok, dt1, e1 = copy_one(n)
        if ok:
            ok_names.append((n, s))
            log('  ✓ 补救成功 %s (%.1fs)' % (n[:64], dt1))
        else:
            still_fail.append((n, s))
            log('  ✗ 补救失败 %s: %s' % (n[:64], e1.replace('\n', ' ')[:160]))
            st['failed'][n] = {'ts': time.strftime('%Y-%m-%d %H:%M'), 'err': e1[-200:]}

    ts = time.strftime('%Y-%m-%d %H:%M')
    for n, s in ok_names:
        st['done'][n] = {'size': s, 'ts': ts}
        st['failed'].pop(n, None)
    save_state(st)

    el = (time.time() - t0) / 60.0
    summary = ('srt → od2:字幕\n源 %s 根目录 已处理_*.srt (>%s)\n'
               '本轮搬 %d 个 / %s，已在 %d，失败 %d，用时 %.1f 分钟'
               % (SRC, human(min_bytes), len(ok_names),
                  human(sum(s for _, s in ok_names)), len(already), len(still_fail), el))
    log(summary.replace('\n', ' | '))
    try:
        with open(REPORT, 'w', encoding='utf-8') as f:
            f.write('# %s\n' % ts)
            f.write(summary + '\n\n')
            if still_fail:
                f.write('## 失败\n')
                for n, s in still_fail:
                    f.write('- %s (%s)\n' % (n, human(s)))
            f.write('\n## 本轮已备份\n')
            for n, s in ok_names:
                f.write('- %s (%s)\n' % (n, human(s)))
    except Exception as e:
        log('报告写入失败: %s' % e)
    tg(summary)
    if still_fail:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
