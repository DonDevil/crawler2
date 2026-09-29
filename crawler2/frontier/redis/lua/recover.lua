-- recover: ARGV[4] lease batch, [5] max_attempts, [6] base backoff,
-- [7] max backoff, [8] dead-letter ttl, [9] dead-letter cap, [10] promote batch.
-- Expired lease = failed attempt (V1 semantics). Returns {recovered, dead, promoted}.
local batch, max_att = tonumber(ARGV[4]), tonumber(ARGV[5])
local base, cap = tonumber(ARGV[6]), tonumber(ARGV[7])
local dead_ttl, dead_max, promote_batch = tonumber(ARGV[8]), tonumber(ARGV[9]), tonumber(ARGV[10])

local promoted = promote_scheduled(promote_batch)

local recovered, dead = 0, 0
local expired = redis.call('ZRANGEBYSCORE', P .. 'leases', '-inf', now, 'LIMIT', 0, batch)
for i = 1, #expired do
  local id = expired[i]
  local tk = task_key(id)
  redis.call('ZREM', P .. 'leases', id)
  local t = redis.call('HMGET', tk, 'st', 'att', 'q')
  if t[1] ~= 'leased' then
    count('anomaly')
  else
    -- Clearing the token makes every later call of the old owner 'stale'.
    redis.call('HDEL', tk, 'tok', 'lex', 'cat')
    redis.call('HSET', tk, 'err', 'lease expired')
    local att = tonumber(t[2])
    if att < max_att then
      schedule_at(id, now + backoff(att, base, cap))
      count('recovered')
      recovered = recovered + 1
    else
      -- Nobody is alive to report this outcome: keep a bounded dead letter.
      redis.call('HSET', tk, 'st', 'dead', 'died', fmt(now))
      redis.call('ZADD', P .. 'dead', now, id)
      redis.call('EXPIRE', tk, dead_ttl)
      redis.call('HINCRBY', P .. 'depth', t[3], -1)
      count('dead')
      dead = dead + 1
    end
  end
end

-- Dead-letter retention: age, then count (bounded work per call).
local function drop_dead(ids)
  for i = 1, #ids do
    if redis.call('HGET', task_key(ids[i]), 'st') == 'dead' then
      redis.call('DEL', task_key(ids[i]))
    end
    redis.call('ZREM', P .. 'dead', ids[i])
  end
end
drop_dead(redis.call('ZRANGEBYSCORE', P .. 'dead', '-inf', now - dead_ttl, 'LIMIT', 0, batch))
local excess = redis.call('ZCARD', P .. 'dead') - dead_max
if excess > 0 then
  if excess > batch then excess = batch end
  drop_dead(redis.call('ZRANGE', P .. 'dead', 0, excess - 1))
end

return {recovered, dead, promoted}
