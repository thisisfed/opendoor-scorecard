<?php
// Latest OPEN share price, pre-market and after-hours included, for the private page at opendoor.thisisfed.xyz/me/.
// It relays public prices only (no account, key or personal data) and answers only that site.
// Sources: CNBC and Nasdaq, the fresher of the two; Yahoo Finance if both fail. Results are cached for 2 seconds so the sources are asked at most
// every 2 seconds however many tabs are open. Add ?debug=1 to see what each source returns.

$ALLOWED = ['https://opendoor.thisisfed.xyz'];
$SYM = 'OPEN';

$origin = $_SERVER['HTTP_ORIGIN'] ?? '';
if (in_array($origin, $ALLOWED, true)) {
    header("Access-Control-Allow-Origin: $origin");
}
header('Vary: Origin');
header('Content-Type: application/json; charset=utf-8');
header('Cache-Control: no-store');
header('X-Robots-Tag: noindex');

$debug = isset($_GET['debug']);
$cache = sys_get_temp_dir() . '/opendoor_quote_' . $SYM . '.json';
if (!$debug && is_file($cache) && (microtime(true) - filemtime($cache)) < 2) {
    readfile($cache);
    exit;
}

function fetch_url($url, $extra = []) {
    $ch = curl_init($url);
    curl_setopt_array($ch, [
        CURLOPT_RETURNTRANSFER => true, CURLOPT_TIMEOUT => 5, CURLOPT_CONNECTTIMEOUT => 3, CURLOPT_FOLLOWLOCATION => true,
        CURLOPT_ENCODING => '',
        CURLOPT_HTTPHEADER => array_merge([
            'User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36',
            'Accept: application/json, text/plain, */*', 'Accept-Language: en-US,en;q=0.9'], $extra),
    ]);
    $body = curl_exec($ch);
    $code = curl_getinfo($ch, CURLINFO_HTTP_CODE);
    curl_close($ch);
    return [$code, $body];
}

function num($v) {
    if ($v === null) return null;
    $v = preg_replace('/[^0-9.\-]/', '', (string)$v);
    return $v === '' ? null : (float)$v;
}

// New York trading session for a moment in time
function session_at($ms) {
    $t = new DateTime('@' . intdiv((int)$ms, 1000));
    $t->setTimezone(new DateTimeZone('America/New_York'));
    $d = (int)$t->format('N');
    $m = (int)$t->format('G') * 60 + (int)$t->format('i');
    if ($d >= 6) return 'closed';
    if ($m >= 240 && $m < 570) return 'pre';
    if ($m >= 570 && $m < 960) return 'regular';
    if ($m >= 960 && $m < 1200) return 'after';
    return 'closed';
}

function from_cnbc($sym, &$log) {
    [$code, $b] = fetch_url("https://quote.cnbc.com/quote-html-webservice/restQuote/symbolType/symbol?symbols=$sym"
        . "&requestMethod=itv&noform=1&partnerId=2&fund=1&exthrs=1&output=json&events=1");
    $log['cnbc'] = $code;
    if ($code != 200) return null;
    $j = json_decode($b, true);
    $q = $j['FormattedQuoteResult']['FormattedQuote'][0] ?? null;
    if (!$q || num($q['last'] ?? null) === null) return null;
    $close = num($q['last']);
    $prev = num($q['previous_day_closing'] ?? null);
    $px = $close;
    $t = isset($q['last_time_msec']) ? (int)$q['last_time_msec'] : (strtotime($q['last_time'] ?? '') * 1000);
    $ext = $q['ExtendedMktQuote'] ?? null;
    if ($ext && num($ext['last'] ?? null) !== null) {
        $et = isset($ext['last_time_msec']) ? (int)$ext['last_time_msec'] : (strtotime($ext['last_time'] ?? '') * 1000);
        if ($et >= $t) { $px = num($ext['last']); $t = $et; }
    }
    return ['src' => 'CNBC', 'px' => $px, 't' => $t, 'close' => $close, 'prev' => $prev];
}

function from_yahoo($sym, &$log) {
    foreach (['query1', 'query2'] as $h) {
        [$code, $b] = fetch_url("https://$h.finance.yahoo.com/v8/finance/chart/$sym?interval=1m&range=1d&includePrePost=true");
        $log["yahoo_$h"] = $code;
        if ($code != 200) continue;
        $r = json_decode($b, true)['chart']['result'][0] ?? null;
        if (!$r) continue;
        $meta = $r['meta'];
        $ts = $r['timestamp'] ?? [];
        $cl = $r['indicators']['quote'][0]['close'] ?? [];
        $px = null; $t = null;
        for ($i = count($ts) - 1; $i >= 0; $i--) {
            if (isset($cl[$i]) && $cl[$i] !== null) { $px = (float)$cl[$i]; $t = $ts[$i] * 1000; break; }
        }
        if ($px === null) { $px = (float)$meta['regularMarketPrice']; $t = (int)$meta['regularMarketTime'] * 1000; }
        return ['src' => 'Yahoo Finance', 'px' => $px, 't' => $t,
                'close' => (float)$meta['regularMarketPrice'], 'prev' => (float)($meta['chartPreviousClose'] ?? $meta['previousClose'] ?? 0)];
    }
    return null;
}

function from_nasdaq($sym, &$log) {
    [$code, $b] = fetch_url("https://api.nasdaq.com/api/quote/$sym/info?assetclass=stocks",
        ['Origin: https://www.nasdaq.com', 'Referer: https://www.nasdaq.com/']);
    $log['nasdaq'] = $code;
    if ($code != 200) return null;
    $d = json_decode($b, true)['data'] ?? null;
    if (!$d || num($d['primaryData']['lastSalePrice'] ?? null) === null) return null;
    $px = num($d['primaryData']['lastSalePrice']);
    $ts = strtotime(preg_replace('/ ET.*$/', '', $d['primaryData']['lastTradeTimestamp'] ?? '') . ' America/New_York');
    $close = num($d['secondaryData']['lastSalePrice'] ?? null) ?? $px;
    return ['src' => 'Nasdaq', 'px' => $px, 't' => $ts ? $ts * 1000 : (int)(microtime(true) * 1000), 'close' => $close, 'prev' => null];
}

// Ask CNBC and Nasdaq (both answer from this server) and keep the fresher price; Yahoo only if both fail.
$log = [];
$res = null;
$all = [];
foreach (['from_cnbc', 'from_nasdaq', 'from_yahoo'] as $f) {
    if ($f === 'from_yahoo' && $res && !$debug) break;
    try { $r = $f($SYM, $log); } catch (Throwable $e) { $r = null; $log[$f . '_error'] = $e->getMessage(); }
    $all[$f] = $r;
    if ($r && (!$res || $r['t'] > $res['t'])) {
        // keep the regular-session close from CNBC when it answered: Nasdaq's is not always the same day's
        if ($res && $res['src'] === 'CNBC') { $r['close'] = $res['close']; $r['prev'] = $res['prev']; }
        $res = $r;
    }
}

if (!$res) {
    http_response_code(502);
    echo json_encode(['error' => 'no source answered', 'log' => $log]);
    exit;
}
$res['session'] = session_at($res['t']);
$res['sym'] = $SYM;
$res['now'] = (int)(microtime(true) * 1000);
$out = json_encode($debug ? ['use' => $res, 'all' => $all, 'log' => $log] : $res + ['srcs' => array_keys(array_filter($all))]);
if (!$debug) @file_put_contents($cache, $out, LOCK_EX);
echo $out;
