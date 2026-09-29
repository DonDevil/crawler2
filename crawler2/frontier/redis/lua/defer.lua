-- defer: ARGV[4] url_id, [5] token, [6] delay. The attempt was not really
-- made (local network outage): attempt budget net zero, fixed delay.
-- Returns {outcome, retry_at}.
local id, token, delay = ARGV[4], ARGV[5], tonumber(ARGV[6])
local tk = task_key(id)
local t = redis.call('HMGET', tk, 'tok', 'st')
if t[1] ~= token or t[2] ~= 'leased' then
  count('stale')
  return {'stale', ''}
end
redis.call('ZREM', P .. 'leases', id)
redis.call('HDEL', tk, 'tok', 'lex', 'cat')
redis.call('HINCRBY', tk, 'att', -1)
local due = now + delay
schedule_at(id, due)
count('deferred')
return {'deferred', fmt(due)}
