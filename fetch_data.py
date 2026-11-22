# -*- coding: utf-8 -*-
"""
模块一 · 取数
一次运行并发拉取三条赛道（A股三大主力）：
  沪深300：sh000300 + 510300/510310/510330/159919
  创业板 ：sz399006 + 159915/159949/159957/159948
  红利低波：csH30269 + 512890/563020/159547/159525（必须加 cs 前缀）
数据源：westock-data (腾讯自选股) CLI；红利低波指数自由流通市值走自建 Worker
输出：raw_data.json
"""
import json, os, time, datetime, urllib.request, urllib.error, urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

import common
from config import TRACKS, ORDER

CACHE = os.path.join(common.BASE, 'raw_data.json')

# ETF 份额/净值由基金公司在 T+1~T+2 披露，行情商对「尚未披露日」返回最近一次已披露值。
# 因此最后 REFRESH_TAIL 个交易日每次都强制重抓，等真实份额披露后自动回填，避免陈旧值被缓存固化。
# 现在份额/净值改由交易所官方源（fetch_exchange.py，T+0）覆盖，滞后天数大幅缩短，
# 故尾部重抓窗口从 8 缩到 5，省下 westock 的调用开销。
REFRESH_TAIL = int(os.environ.get('ETF_REFRESH_TAIL', '5'))
# 展示窗口：1250 个交易日 ≈ 5 年。
# 实测 westock kline 单次最多返回 1212 行（2021-11 起），5 年一次拉完，无需分段。
# 超出窗口的旧数据在写回缓存时会被裁掉（见 build_track 的滚动裁剪），缓存不会无限增长。
DAYS = int(os.environ.get('ETF_DAYS', '1250'))
CAL_DAYS = int(os.environ.get('ETF_CAL_DAYS', '2000'))      # 日历天回溯：要够 5 年，并留出长假余量
KLINE_LIMIT = int(os.environ.get('ETF_KLINE_LIMIT', '1500'))

# 自建 Worker：只给「中证官方自由流通市值」这类 westock 没有的字段兜底；密钥只从环境变量读。
API_BASE = (os.environ.get('ETF_API_BASE') or 'https://a-stock-api.wj4020410.workers.dev').rstrip('/')
API_TIMEOUT = float(os.environ.get('ETF_API_TIMEOUT', '30'))


def fetch_csindex_float_mv(symbol):
    """从 Worker 取中证官网指数自由流通市值（元）与快照日期；失败返回 None，不中断整轮。"""
    key = os.environ.get('A_STOCK_API_KEY', '')
    if not key:
        print('::warning::  A_STOCK_API_KEY 未配置，%s 自由流通市值无法取数' % symbol)
        return None
    url = '%s/api/csindex-feature?symbol=%s' % (API_BASE, urllib.parse.quote(symbol))
    req = urllib.request.Request(url, headers={
        'X-API-Key': key,
        'User-Agent': 'etf-lsi/1.0',
        'Accept': 'application/json',
    })
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT) as r:
            j = json.load(r)
        d = j.get('data') or {}
        mv = common.fnum(d.get('float_mv'))
        if not mv or mv <= 0:
            raise ValueError('float_mv 无效')
        return {'float_mv': mv, 'trade_date': d.get('trade_date') or ''}
    except Exception as e:
        print('::warning::  [%s] Worker 自由流通市值取数失败：%s' % (symbol, str(e)[:120]))
        return None


def apply_csindex_float_mv(track, block, feature):
    """把官方快照按收盘价比例摊到全部交易日，给 analyze_report 一个稳定口径。

    calMkvSum 是快照日的自由流通市值；历史值 = 快照市值 × 当日收盘 / 快照日收盘。
    westock 的 csH30269 没有市值字段，不能用 circulating_market_cap 兜底。
    """
    if not feature:
        return
    daily = block.get('daily') or {}
    dates = sorted(daily)
    if not dates:
        return
    base = feature.get('trade_date') if feature.get('trade_date') in daily else dates[-1]
    base_close = (block.get('kline_close') or {}).get(base) or (daily.get(base) or {}).get('idx_close')
    if not base_close:
        print('::warning::  [%s] 无快照日收盘价，跳过自由流通市值回填' % track)
        return
    n = 0
    for d in dates:
        rec = daily[d]
        close = (block.get('kline_close') or {}).get(d) or rec.get('idx_close')
        if close:
            rec['idx_float_mv'] = feature['float_mv'] * close / base_close
            n += 1
    print('[%s] 中证自由流通市值回填 %d 天（%s，%.2f 亿元）'
          % (track, n, feature.get('trade_date') or '最新', feature['float_mv'] / 1e8))


def get_trade_days(end=None, days=None, limit=None):
    """A 股交易日历（三大主力赛道共用）"""
    days = CAL_DAYS if days is None else days
    limit = KLINE_LIMIT if limit is None else limit
    end = end or datetime.date.today().isoformat()
    start = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    out = common.run(['kline', 'sh000300', '--start', start, '--end', end, '--limit', str(limit)])
    rows = common.parse_kline(out)
    if not rows:
        raise RuntimeError('kline failed：取不到交易日历')
    return rows


def fetch_one(date, track):
    """抓取某赛道单个交易日的指数 + 4 只 ETF"""
    cfg = TRACKS[track]
    rec = {'date': date}
    o = common.run(['quote', cfg['index_code'], '--date', date])
    t = common.first_table(o)
    rec['idx_close'] = common.fnum(t.get('price'))
    rec['idx_float_mv'] = common.fnum(t.get('circulating_market_cap')) or common.fnum(t.get('total_market_cap'))
    o2 = common.run(['etf', 'detail', ','.join(cfg['etfs'].keys()), '--date', date])
    parts = common.split_by_code(o2)
    for code in cfg['etfs']:
        p = parts.get(code, '')
        tb = common.first_table(p) if p else {}
        rec[code] = {
            'close': common.fnum(tb.get('closePrice')),
            'nav': common.fnum(tb.get('nav')),
            'size': common.fnum(tb.get('size')),
            'shares': common.fnum(tb.get('shares')),
            'sharesChg': common.fnum(tb.get('sharesChg')),
            'sharesChgRatio': common.fnum(tb.get('sharesChgRatio')),
        }
    return rec


def apply_exchange(track, results):
    """用交易所官方份额/净值覆盖 westock 的滞后值。

    westock 的 ETF 份额/净值在基金公司披露前会冻结成上一次披露值（T+1~T+2），
    交易所官方披露是 T+0（当日收盘后即出当日份额），因此优先采用交易所值。
    取不到时保留 westock 原值 —— 只会变好，不会变差。
    份额被覆盖后，sharesChg / sharesChgRatio / size 必须按新份额重算。
    """
    try:
        import fetch_exchange
        ex = fetch_exchange.load_cache()
    except Exception as e:
        print('::warning::  [%s] 交易所缓存不可用，沿用 westock: %s' % (track, str(e)[:80]))
        return 0, 0
    if not ex.get('shares') and not ex.get('nav'):
        print('  [%s] 交易所缓存为空，沿用 westock' % track)
        return 0, 0

    codes = list(TRACKS[track]['etfs'].keys())
    n_sh = n_nv = 0
    for d in sorted(results):
        rec = results[d]
        for key in codes:
            e = rec.get(key) or {}
            v = fetch_exchange.shares_of(ex, d, key[2:])
            if v is not None and v != e.get('shares'):
                e['shares'] = v
                n_sh += 1
            nv = fetch_exchange.nav_of(ex, d, key[2:])
            if nv is not None and nv != e.get('nav'):
                e['nav'] = nv
                n_nv += 1
            rec[key] = e

    days = sorted(results)
    for i in range(1, len(days)):
        prev, cur = results[days[i - 1]], results[days[i]]
        for key in codes:
            c = cur.get(key) or {}
            p = prev.get(key) or {}
            cs, ps = c.get('shares'), p.get('shares')
            if cs is not None and ps is not None:
                c['sharesChg'] = cs - ps
                c['sharesChgRatio'] = (cs - ps) / ps * 100 if ps else None
            if cs and c.get('nav'):
                c['size'] = cs * c['nav']
            cur[key] = c
    return n_sh, n_nv


def build_track(track, days, jobs):
    cfg = TRACKS[track]
    dates = [d for d, _ in days]
    kline_close = {d: c for d, c in days}
    results = {}
    old = {}
    if os.path.exists(CACHE):
        try:
            with open(CACHE, encoding='utf-8') as fp:
                old = json.load(fp).get(track, {}).get('daily', {}) or {}
        except Exception:
            old = {}
    results = dict(old)
    # 滚动窗口：只保留展示窗口内的交易日，超出 5 年的旧数据直接丢弃（避免缓存与产物无限膨胀）
    win = set(dates)
    stale = [d for d in results if d not in win]
    if stale:
        for d in stale:
            results.pop(d, None)
        print('[%s] 裁剪窗口外旧数据 %d 天（保留 %d 天）' % (track, len(stale), len(results)))
    print('[%s] cache loaded %d days' % (track, len(results)))

    todo = set(d for d in dates if d not in results)
    tail = dates[-REFRESH_TAIL:]
    todo |= set(tail)
    todo = sorted(todo)
    print('[%s] to fetch %d (含尾部强制刷新 %d 天: %s ~ %s)'
          % (track, len(todo), len(tail), tail[0], tail[-1]))

    done, t0 = 0, time.time()
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(fetch_one, d, track): d for d in todo}
        for f in as_completed(futs):
            d = futs[f]
            try:
                r = f.result()
                r['idx_close'] = r.get('idx_close') or kline_close.get(d)
                results[d] = r
                done += 1
                if done % 25 == 0:
                    print('  [%s] %d/%d  %.0fs' % (track, done, len(todo), time.time() - t0), flush=True)
            except Exception as e:
                print('::warning::  [%s] 单日抓取失败 %s %s（留待下一轮回填）' % (track, d, e))
    print('[%s] fetched %d days, %.0fs' % (track, len(results), time.time() - t0))

    n_sh, n_nv = apply_exchange(track, results)
    print('[%s] 交易所源覆盖：份额 %d 处、净值 %d 处' % (track, n_sh, n_nv), flush=True)
    return {'kline_close': kline_close, 'daily': results}


def main():
    alldays = sorted(get_trade_days(), key=lambda x: x[0])
    print('trading days total:', len(alldays), alldays[0][0], '->', alldays[-1][0])
    days = alldays[-DAYS:]
    print('use last %d: %s -> %s' % (len(days), days[0][0], days[-1][0]))

    out = {}
    if os.path.exists(CACHE):
        try:
            with open(CACHE, encoding='utf-8') as fp:
                out = json.load(fp)
        except Exception:
            out = {}
    # 每条需要中证官方市值的赛道只请求一次，避免按交易日重复打 Worker。
    features = {}
    for tk in ORDER:
        sym = TRACKS[tk].get('float_mv_symbol')
        if sym and sym not in features:
            features[sym] = fetch_csindex_float_mv(sym)

    for tk in ORDER:
        block = build_track(tk, days, None)
        sym = TRACKS[tk].get('float_mv_symbol')
        if sym:
            apply_csindex_float_mv(tk, block, features.get(sym))
        out[tk] = block

    with open(CACHE, 'w', encoding='utf-8') as fp:
        json.dump(out, fp, ensure_ascii=False)
    print('saved', CACHE, '->', {k: len(v.get('daily', {})) for k, v in out.items()})


if __name__ == '__main__':
    main()
