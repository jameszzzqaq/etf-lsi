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
API_BASE = (os.environ.get('ETF_API_BASE') or 'https://stock-api.yygq.me').rstrip('/')
API_TIMEOUT = float(os.environ.get('ETF_API_TIMEOUT', '30'))


def fetch_csindex_closes(symbol, start, end):
    """从 Worker /api/csindex 取中证官网指数日K收盘（权威源），返回 {YYYY-MM-DD: close}。

    westock 的 csH30269 历史只到 2024-05-08，更早的 quote 返回「数据为空」；
    一旦用交易日历 sh000300 的 kline 点位兜底，就会把沪深300点位静默填成红利低波点位。
    红利低波的指数点位一律以中证官网为准，覆盖全 5 年。失败返回 {}，由调用方报警。
    """
    key = os.environ.get('A_STOCK_API_KEY', '')
    if not key:
        print('::warning::  A_STOCK_API_KEY 未配置，%s 指数日K权威源无法取数' % symbol)
        return {}
    # 【关键】中证官网要求 startDate/endDate 为 YYYYMMDD（无横杠）。
    # 传 YYYY-MM-DD 会被官网截断到 2025-12-31（实测），导致近端点位缺失、市值回填跳过、赛道消失。
    start_c = start.replace('-', ''); end_c = end.replace('-', '')
    url = '%s/api/csindex?symbol=%s&start=%s&end=%s' % (
        API_BASE, urllib.parse.quote(symbol), urllib.parse.quote(start_c), urllib.parse.quote(end_c))
    req = urllib.request.Request(url, headers={
        'X-API-Key': key,
        'User-Agent': 'etf-lsi/1.0',
        'Accept': 'application/json',
    })
    try:
        with urllib.request.urlopen(req, timeout=max(API_TIMEOUT, 60)) as r:
            j = json.load(r)
        rows = j.get('data') or []
        out = {}
        for row in rows:
            d = row.get('date') or ''
            c = common.fnum(row.get('close'))
            if d and c:
                out[d] = c
        print('[%s] 中证官网指数日K权威源 %d 天（%s ~ %s）'
              % (symbol, len(out), rows[0]['date'] if rows else '-', rows[-1]['date'] if rows else '-'))
        return out
    except Exception as e:
        print('::warning::  [%s] 中证官网指数日K取数失败：%s' % (symbol, str(e)[:120]))
        return {}


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
    # 【严禁】用 kline_close（sh000300 点位）回填市值：会与红利低波点位口径串味。
    # 一律用 daily 里已被权威源纠正的 idx_close 作 base。
    # 快照日若恰好无点位（如权威源截断），往前找最近一个有 idx_close 的交易日兜底，
    # 绝不因单日缺失就整段跳过（曾导致 hldb 赛道消失）。
    base = feature.get('trade_date') if feature.get('trade_date') in daily else dates[-1]
    base_close = (daily.get(base) or {}).get('idx_close')
    if not base_close:
        for d in reversed(dates):
            c = (daily.get(d) or {}).get('idx_close')
            if c:
                base, base_close = d, c
                break
    if not base_close:
        print('::warning::  [%s] 无任何交易日指数收盘价，跳过自由流通市值回填' % track)
        return
    n = 0
    for d in dates:
        rec = daily[d]
        close = rec.get('idx_close')
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


def build_track(track, days, jobs, authoritative_closes=None):
    cfg = TRACKS[track]
    dates = [d for d, _ in days]
    kline_close = {d: c for d, c in days}
    authoritative_closes = authoritative_closes or {}
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
                # 【严禁】用 kline_close（交易日历 sh000300 的点位）给任何赛道兜底指数点位：
                # 跨标的口径不同，会静默把沪深300点位填成红利低波点位（2024-05-08 前的真实事故）。
                # 指数点位只认两条合规来源：本赛道 quote 的真实返回、或权威源 authoritative_closes。
                if authoritative_closes:
                    if d in authoritative_closes:
                        r['idx_close'] = authoritative_closes[d]
                    else:
                        # 权威源缺该日：清掉 quote 的脏值，绝不落库错误点位
                        r['idx_close'] = None
                results[d] = r
                done += 1
                if done % 25 == 0:
                    print('  [%s] %d/%d  %.0fs' % (track, done, len(todo), time.time() - t0), flush=True)
            except Exception as e:
                print('::warning::  [%s] 单日抓取失败 %s %s（留待下一轮回填）' % (track, d, e))
    print('[%s] fetched %d days, %.0fs' % (track, len(results), time.time() - t0))

    # 已有缓存（非本轮 todo）也需要用权威源纠正——否则历史脏点位会一直留在缓存里。
    if authoritative_closes:
        fixed = 0
        for d, r in results.items():
            if d in authoritative_closes and r.get('idx_close') != authoritative_closes[d]:
                r['idx_close'] = authoritative_closes[d]
                fixed += 1
        print('[%s] 权威源纠正历史点位 %d 天' % (track, fixed), flush=True)

    n_sh, n_nv = apply_exchange(track, results)
    print('[%s] 交易所源覆盖：份额 %d 处、净值 %d 处' % (track, n_sh, n_nv), flush=True)

    # 防呆：若仍有赛道指数点位与该日沪深300点位高度吻合且不该如此，报警。
    if cfg.get('index_symbol'):
        suspicious = [d for d, r in results.items()
                      if r.get('idx_close') and kline_close.get(d)
                      and abs(r['idx_close'] - kline_close[d]) < 0.01]
        if suspicious:
            print('::warning::  [%s] 有 %d 天 idx_close 与沪深300点位完全相同，疑似误填（示例 %s）'
                  % (track, len(suspicious), suspicious[:3]), flush=True)
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
    # 每条需要中证官方市值 / 权威点位的赛道只请求一次，避免按交易日重复打 Worker。
    features = {}
    idx_closes = {}
    span_start, span_end = days[0][0], days[-1][0]
    for tk in ORDER:
        sym = TRACKS[tk].get('float_mv_symbol')
        if sym and sym not in features:
            features[sym] = fetch_csindex_float_mv(sym)
        isym = TRACKS[tk].get('index_symbol')
        if isym and isym not in idx_closes:
            idx_closes[isym] = fetch_csindex_closes(isym, span_start, span_end)
        # 硬校验：声明了权威源的赛道若取数为空，宁可让流水线红掉，也绝不静默用脏点位上线。
        if isym and not idx_closes.get(isym):
            raise RuntimeError('[%s] 指数点位权威源 %s 取数为空，拒绝用不可信数据上线' % (tk, isym))
        # 硬校验：权威源最新日期必须覆盖到展示窗口末端附近。
        # 中证官网对 YYYY-MM-DD 格式会静默截断到上一年末（实测），曾导致 hldb 近端全空、赛道消失。
        if isym:
            last_auth = max(idx_closes[isym])
            if last_auth < span_end:
                raise RuntimeError('[%s] 指数点位权威源最新日 %s 早于窗口末端 %s，疑似上游截断，'
                                   '拒绝用不完整数据上线' % (tk, last_auth, span_end))

    for tk in ORDER:
        isym = TRACKS[tk].get('index_symbol')
        block = build_track(tk, days, None, authoritative_closes=idx_closes.get(isym) if isym else None)
        sym = TRACKS[tk].get('float_mv_symbol')
        if sym:
            apply_csindex_float_mv(tk, block, features.get(sym))
        out[tk] = block

    with open(CACHE, 'w', encoding='utf-8') as fp:
        json.dump(out, fp, ensure_ascii=False)
    print('saved', CACHE, '->', {k: len(v.get('daily', {})) for k, v in out.items()})


if __name__ == '__main__':
    main()
