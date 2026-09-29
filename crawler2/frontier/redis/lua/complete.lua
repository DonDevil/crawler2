-- complete: ARGV[4] url_id, [5] token. Ends the task; the URL is admittable
-- again immediately (no permanent "visited" record, ADR-016).
local id, token = ARGV[4], ARGV[5]
local tk = task_key(id)
local t = redis.call('HMGET', tk, 'tok', 'st', 'q')
if t[1] ~= token or t[2] ~= 'leased' then
  count('stale')
  return 'stale'
end
redis.call('ZREM', P .. 'leases', id)
redis.call('DEL', tk)
redis.call('HINCRBY', P .. 'depth', t[3], -1)
count('completed')
return 'completed'
