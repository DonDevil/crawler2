-- fail: ARGV[4] url_id, [5] token, [6] reason, [7] next queue ('' = same),
-- [8] max_attempts, [9] base backoff, [10] max backoff, [11] default in-flight limit.
-- The frontier alone decides retry vs exhausted (D4).
-- Returns {outcome, retry_at}.
local id, token, reason, next_q = ARGV[4], ARGV[5], ARGV[6], ARGV[7]
local max_att, base, cap = tonumber(ARGV[8]), tonumber(ARGV[9]), tonumber(ARGV[10])
local tk = task_key(id)
local t = redis.call('HMGET', tk, 'tok', 'st', 'q', 'att', 'dom')
if t[1] ~= token or t[2] ~= 'leased' then
  count('stale')
  return {'stale', ''}
end
redis.call('ZREM', P .. 'leases', id)
redis.call('HDEL', tk, 'tok', 'lex', 'cat')
release(t[5], tonumber(ARGV[11]))
local q = t[3]
if next_q ~= '' and next_q ~= q then
  -- Queue move of an admitted task: never checks max_depth (ADR-016).
  redis.call('HINCRBY', P .. 'depth', q, -1)
  redis.call('HINCRBY', P .. 'depth', next_q, 1)
  redis.call('HSET', tk, 'q', next_q)
  q = next_q
end
local att = tonumber(t[4])
if att < max_att then
  local due = now + backoff(att, base, cap)
  redis.call('HSET', tk, 'err', reason)
  schedule_at(id, due)
  count('retried')
  return {'retry_scheduled', fmt(due)}
end
redis.call('DEL', tk)
redis.call('HINCRBY', P .. 'depth', q, -1)
count('exhausted')
return {'exhausted', ''}
