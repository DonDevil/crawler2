-- admit: ARGV[4] url_id, [5] url, [6] domain_id, [7] queue, [8] priority,
-- [9] due ('' = now), [10] reason, [11] max_depth of the queue.
-- Returns {outcome, existing_state}.
local id, url, dom, q = ARGV[4], ARGV[5], ARGV[6], ARGV[7]
local pri = tonumber(ARGV[8])
local due = now
if ARGV[9] ~= '' then due = tonumber(ARGV[9]) end
local reason, max_depth = ARGV[10], tonumber(ARGV[11])

local tk = task_key(id)
local t = redis.call('HMGET', tk, 'st', 'pri', 'q', 'dom', 'due', 'seq', 'att')
local st = t[1]
-- A dead letter is a report, not a dedup record; it is replaced only if the
-- new task is accepted.
local was_dead = st == 'dead'
if was_dead then st = false end

if st then
  -- One active task per URL; merge per url_id, highest priority wins.
  local changed = false
  if st == 'ready' then
    if pri > tonumber(t[2]) then
      redis.call('ZADD', queue_key(t[3], t[4]), 'XX', (100 - pri) * BAND + tonumber(t[6]), id)
      redis.call('HSET', tk, 'pri', pri)
      if eligible(t[3], t[4]) then sync_ready(t[3], t[4]) end
      changed = true
    end
  elseif st == 'scheduled' then
    if pri > tonumber(t[2]) then
      redis.call('HSET', tk, 'pri', pri)
      changed = true
    end
    -- Only a never-attempted task moves earlier: a retry backoff is the
    -- frontier's decision (D4) and is not shortened by producers.
    if due < tonumber(t[5]) and tonumber(t[7]) == 0 then
      schedule_at(id, due)
      changed = true
    end
  end
  if changed then
    count('merged')
    return {'merged', st}
  end
  count('duplicate')
  return {'duplicate', st}
end

local depth = tonumber(redis.call('HGET', P .. 'depth', q) or '0')
if depth >= max_depth then
  count('rejected')
  return {'rejected_full', ''}
end
if was_dead then
  redis.call('DEL', tk)
  redis.call('ZREM', P .. 'dead', id)
end
redis.call('HINCRBY', P .. 'depth', q, 1)
redis.call('HSET', tk, 'url', url, 'dom', dom, 'q', q, 'pri', pri, 'att', 0,
  'rsn', reason, 'adm', fmt(now))
count('admitted')
if due > now then
  schedule_at(id, due)
  return {'scheduled', ''}
end
enqueue(id, q, dom, pri)
return {'ready', ''}
