-- claim: ARGV[4] queue, [5] token, [6] lease_ttl, [7] default interval,
-- [8] promote batch.
-- Returns {url_id, url, domain_id, priority, attempt, lease_expiry,
-- claimed_at, reason} or nil when the queue has no eligible work now.
local q, token = ARGV[4], ARGV[5]
local ttl, default_interval, batch = tonumber(ARGV[6]), tonumber(ARGV[7]), tonumber(ARGV[8])

promote_scheduled(batch)
promote_gates(batch)

local rk = ready_key(q)
-- Bounded self-heal: an index entry whose queue is empty is dropped (never
-- expected; counted as an anomaly) and the next best domain is tried.
for _ = 1, 16 do
  local best = redis.call('ZRANGE', rk, 0, 0)
  if #best == 0 then return false end
  local dom = best[1]
  local qk = queue_key(q, dom)
  local head = redis.call('ZRANGE', qk, 0, 0)
  if #head == 0 then
    redis.call('ZREM', rk, dom)
    count('anomaly')
  else
    local id = head[1]
    redis.call('ZREM', qk, id)
    local interval = redis.call('HGET', P .. 'interval', dom)
    if interval then interval = tonumber(interval) else interval = default_interval end
    if interval > 0 then
      close_gate(dom, now + interval)
    else
      sync_ready(q, dom)
    end
    local tk = task_key(id)
    local att = redis.call('HINCRBY', tk, 'att', 1)
    local lex = now + ttl
    redis.call('HSET', tk, 'st', 'leased', 'tok', token, 'lex', fmt(lex), 'cat', fmt(now))
    redis.call('ZADD', P .. 'leases', lex, id)
    count('claimed')
    local t = redis.call('HMGET', tk, 'url', 'pri', 'rsn')
    return {id, t[1], dom, t[2], att, fmt(lex), fmt(now), t[3] or ''}
  end
end
return false
