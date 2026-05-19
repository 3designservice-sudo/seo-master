<?php
/* DS_BOT_API_v1 — endpoint for external blog-bot
   Actions:
     ?action=get_planned&date=YYYY-MM-DD|today|tomorrow [&limit=N&sort=freq|priority]
     ?action=get_article&id=N
     ?action=stats
     POST ?action=mark_status&id=N — body JSON: {status, published_url?, published_date?, block_reason?, notes?}
   Auth: ?k=<bot_api.key from tokens.json>
   Storage: data/seo/article_roadmap.json (flock-protected for writes)
*/
header('Content-Type: application/json; charset=utf-8');
header('Access-Control-Allow-Origin: *');

$ROADMAP = __DIR__ . '/data/seo/article_roadmap.json';
$TOKENS  = __DIR__ . '/data/tokens.json';
$LOG     = __DIR__ . '/data/seo/bot_api_log.json';

function out($code, $payload) {
    http_response_code($code);
    echo json_encode($payload, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
    exit;
}

function load_key($tokens_file) {
    if (!is_file($tokens_file)) return null;
    $t = json_decode(file_get_contents($tokens_file), true);
    return $t['bot_api']['key'] ?? null;
}

$expected = load_key($TOKENS);
if (!$expected) out(500, ['error' => 'bot_api key not configured in tokens.json']);

$k = $_GET['k'] ?? '';
if (!hash_equals($expected, $k)) out(403, ['error' => 'forbidden']);

$action = $_GET['action'] ?? '';

function read_roadmap($file) {
    if (!is_file($file)) return [];
    $raw = file_get_contents($file);
    $data = json_decode($raw, true);
    return is_array($data) ? $data : [];
}

function write_roadmap_atomic($file, $data) {
    $fp = fopen($file, 'c+');
    if (!$fp) return false;
    if (!flock($fp, LOCK_EX)) { fclose($fp); return false; }
    ftruncate($fp, 0);
    rewind($fp);
    fwrite($fp, json_encode($data, JSON_UNESCAPED_UNICODE | JSON_PRETTY_PRINT));
    fflush($fp);
    flock($fp, LOCK_UN);
    fclose($fp);
    return true;
}

function log_action($logfile, $entry) {
    $entry['ts'] = date('c');
    $entry['ip'] = $_SERVER['REMOTE_ADDR'] ?? '';
    @file_put_contents($logfile, json_encode($entry, JSON_UNESCAPED_UNICODE) . "\n", FILE_APPEND);
}

$roadmap = read_roadmap($ROADMAP);
if (empty($roadmap)) out(500, ['error' => 'roadmap empty or unreadable']);

switch ($action) {
    case 'get_planned': {
        $date = $_GET['date'] ?? 'today';
        if ($date === 'today')    $date = date('Y-m-d');
        if ($date === 'tomorrow') $date = date('Y-m-d', strtotime('+1 day'));
        $limit = (int)($_GET['limit'] ?? 0);
        $sort  = $_GET['sort'] ?? 'freq';

        $items = array_values(array_filter($roadmap, function($a) use ($date) {
            return ($a['planned_date'] ?? '') === $date && ($a['status'] ?? '') === 'planned';
        }));

        if ($sort === 'freq') {
            usort($items, function($a, $b) {
                return (int)($b['kw_total_freq'] ?? 0) - (int)($a['kw_total_freq'] ?? 0);
            });
        }
        if ($limit > 0) $items = array_slice($items, 0, $limit);

        out(200, ['date' => $date, 'count' => count($items), 'articles' => $items]);
    }

    case 'get_article': {
        $id = (int)($_GET['id'] ?? 0);
        if (!$id) out(400, ['error' => 'id required']);
        foreach ($roadmap as $a) {
            if ((int)($a['id'] ?? 0) === $id) out(200, $a);
        }
        out(404, ['error' => "article id=$id not found"]);
    }

    case 'stats': {
        $by_status = []; $by_service = []; $by_kind = []; $by_date = [];
        $total = count($roadmap);
        foreach ($roadmap as $a) {
            $s = $a['status'] ?? 'unknown';
            $by_status[$s] = ($by_status[$s] ?? 0) + 1;
            $svc = $a['service'] ?? '?';
            $by_service[$svc] = ($by_service[$svc] ?? 0) + 1;
            $k = $a['kind'] ?? '?';
            $by_kind[$k] = ($by_kind[$k] ?? 0) + 1;
            if (($a['status'] ?? '') === 'planned') {
                $d = $a['planned_date'] ?? '?';
                $by_date[$d] = ($by_date[$d] ?? 0) + 1;
            }
        }
        ksort($by_date);
        $today = date('Y-m-d');
        $overdue = 0;
        foreach ($roadmap as $a) {
            if (($a['status'] ?? '') === 'planned' &&
                ($a['planned_date'] ?? '') < $today) $overdue++;
        }
        out(200, [
            'total'       => $total,
            'by_status'   => $by_status,
            'by_service'  => $by_service,
            'by_kind'     => $by_kind,
            'planned_by_date' => $by_date,
            'overdue_planned' => $overdue,
        ]);
    }

    case 'get_next_for_pipeline': {
        // Returns the next planned article for today with service rotation —
        // excludes services of the last 3 published articles.
        $today = date('Y-m-d');
        // 1. Get services of last 3 published, sorted by published_date desc
        $with_date = [];
        foreach ($roadmap as $a) {
            if (($a['status'] ?? '') === 'published') {
                $with_date[] = $a;
            }
        }
        usort($with_date, function($x, $y) {
            $tx = $x['last_updated'] ?? $x['published_date'] ?? '';
            $ty = $y['last_updated'] ?? $y['published_date'] ?? '';
            return strcmp($ty, $tx);  // desc — newest first
        });
        $recent_services = [];
        for ($i = 0; $i < min(3, count($with_date)); $i++) {
            $svc = $with_date[$i]['service'] ?? '';
            if ($svc && !in_array($svc, $recent_services, true)) {
                $recent_services[] = $svc;
            }
        }
        // 2. Find candidates: planned today, sorted by freq desc
        $today_pool = array_values(array_filter($roadmap, function($a) use ($today) {
            return ($a['status'] ?? '') === 'planned' && ($a['planned_date'] ?? '') === $today;
        }));
        $any_pool = array_values(array_filter($roadmap, function($a) {
            return ($a['status'] ?? '') === 'planned';
        }));
        $sort_by_freq = function(&$arr) {
            usort($arr, function($a, $b) {
                return ((int)($b['kw_total_freq'] ?? 0)) - ((int)($a['kw_total_freq'] ?? 0));
            });
        };
        $sort_by_freq($today_pool);
        $sort_by_freq($any_pool);

        // 3. First TRY: planned today with rotation
        foreach ($today_pool as $c) {
            if (!in_array($c['service'] ?? '', $recent_services, true)) {
                out(200, [
                    'article' => $c,
                    'recent_services_excluded' => $recent_services,
                    'pool' => 'today',
                    'rotation_applied' => true,
                ]);
            }
        }
        // 4. Today exists but all in exclude — fallback today, no rotation
        if (!empty($today_pool)) {
            out(200, [
                'article' => $today_pool[0],
                'recent_services_excluded' => $recent_services,
                'pool' => 'today',
                'rotation_applied' => false,
                'note' => 'all today planned services in recent — fallback to highest freq',
            ]);
        }
        // 5. BACKFILL — no today planned at all. Take any planned with rotation.
        foreach ($any_pool as $c) {
            if (!in_array($c['service'] ?? '', $recent_services, true)) {
                out(200, [
                    'article' => $c,
                    'recent_services_excluded' => $recent_services,
                    'pool' => 'backfill',
                    'rotation_applied' => true,
                    'note' => 'no planned today — backfilled from full pool',
                ]);
            }
        }
        // 6. Backfill fallback — no rotation possible at all
        if (!empty($any_pool)) {
            out(200, [
                'article' => $any_pool[0],
                'recent_services_excluded' => $recent_services,
                'pool' => 'backfill',
                'rotation_applied' => false,
                'note' => 'no planned today and all services in recent — full fallback',
            ]);
        }
        out(404, ['error' => 'no planned articles at all (roadmap exhausted)']);
    }

    case 'recent_published': {
        // Returns last N published articles, sorted by published_date/last_updated desc.
        // Used by pipeline 'Читать дальше' block.
        $limit = max(1, min(20, (int)($_GET['limit'] ?? 3)));
        $exclude_id = (int)($_GET['exclude_id'] ?? 0);
        $published = array_values(array_filter($roadmap, function($a) use ($exclude_id) {
            return ($a['status'] ?? '') === 'published'
                && !empty($a['published_url'])
                && ($exclude_id === 0 || ($a['id'] ?? 0) !== $exclude_id);
        }));
        usort($published, function($a, $b) {
            $tx = $a['last_updated'] ?? $a['published_date'] ?? '';
            $ty = $b['last_updated'] ?? $b['published_date'] ?? '';
            return strcmp($ty, $tx);  // desc — newest first
        });
        $items = array_slice($published, 0, $limit);
        out(200, ['count' => count($items), 'articles' => $items]);
    }

    case 'mark_status': {
        if ($_SERVER['REQUEST_METHOD'] !== 'POST') out(405, ['error' => 'POST required']);
        $id = (int)($_GET['id'] ?? 0);
        if (!$id) out(400, ['error' => 'id required']);
        $body = file_get_contents('php://input');
        $patch = json_decode($body, true);
        if (!is_array($patch)) out(400, ['error' => 'invalid JSON body']);
        $allowed = ['status', 'published_url', 'published_date', 'block_reason', 'notes', 'humanizer_score', 'word_count'];
        $found_idx = null;
        foreach ($roadmap as $i => $a) {
            if ((int)($a['id'] ?? 0) === $id) { $found_idx = $i; break; }
        }
        if ($found_idx === null) out(404, ['error' => "article id=$id not found"]);
        $valid_statuses = ['planned', 'writing', 'published', 'blocked', 'indexed_low'];
        if (isset($patch['status']) && !in_array($patch['status'], $valid_statuses, true)) {
            out(400, ['error' => 'invalid status', 'valid' => $valid_statuses]);
        }
        foreach ($allowed as $k) {
            if (array_key_exists($k, $patch)) $roadmap[$found_idx][$k] = $patch[$k];
        }
        $roadmap[$found_idx]['last_updated'] = date('c');

        if (!write_roadmap_atomic($ROADMAP, $roadmap)) {
            out(500, ['error' => 'write failed']);
        }
        log_action($LOG, ['action' => 'mark_status', 'id' => $id, 'patch' => $patch]);
        out(200, ['ok' => true, 'article' => $roadmap[$found_idx]]);
    }

    default:
        out(400, ['error' => 'unknown action', 'valid' => ['get_planned', 'get_article', 'stats', 'mark_status', 'get_next_for_pipeline', 'recent_published']]);
}
