-- Shared prelude, prepended to every frontier script (docs/phases/
-- p03-frontier-scheduling/p3-frontier-scheduling.md §6, §18).
--
-- ARGV[1]  key prefix, e.g. "crawler2:fr:"
-- ARGV[2]  clock override in epoch seconds; '' = Redis TIME (production)
-- ARGV[3]  comma-separated execution queues (closed registry, ADR-015)
-- ARGV[4]+ operation arguments
--
-- Keys are derived from UrlId/DomainId/queue names inside the script: valid
-- for one Redis primary (ADR-006), deliberately not Redis Cluster safe.
-- Numbers are passed to redis.call as numbers (Redis formats them without
-- precision loss); never concatenate a number into a string.

local P = ARGV[1]
local now
if ARGV[2] ~= '' then
  now = tonumber(ARGV[2])
else
  local t = redis.call('TIME')
  now = tonumber(t[1]) + tonumber(t[2]) / 1000000
end
local QUEUES = {}
for q in string.gmatch(ARGV[3], '[^,]+') do QUEUES[#QUEUES + 1] = q end

-- rank = (100 - priority) * BAND + seq: priority major (higher first), FIFO minor.
local BAND = 10000000000000

local function fmt(x) return string.format('%.6f', x) end
local function task_key(id) return P .. 'task:' .. id end
local function queue_key(q, dom) return P .. 'q:' .. q .. ':' .. dom end
local function ready_key(q) return P .. 'ready:' .. q end
local function count(field) redis.call('HINCRBY', P .. 'stats', field, 1) end

-- A domain with any gate entry (open or not yet promoted) is in no ready index.
local function gated(dom) return redis.call('ZSCORE', P .. 'gate', dom) ~= false end

-- Cross-queue turn-taking: after a gate reopens, the queue that claimed last
-- yields for one interval to other queues with work on the domain
-- (yield ZSET member 'queue|domain', score = end of the yield).
local function held(q, dom) return redis.call('ZSCORE', P .. 'yield', q .. '|' .. dom) ~= false end
local function eligible(q, dom) return not gated(dom) and not held(q, dom) end

local function domain_interval(dom, default_interval)
  local iv = redis.call('HGET', P .. 'interval', dom)
  if iv then return tonumber(iv) end
  return default_interval
end

-- Resync dom's entry in ready:q to the current head of q:q:dom (dom not gated).
local function sync_ready(q, dom)
  local head = redis.call('ZRANGE', queue_key(q, dom), 0, 0, 'WITHSCORES')
  if #head == 0 then
    redis.call('ZREM', ready_key(q), dom)
  else
    redis.call('ZADD', ready_key(q), head[2], dom)
  end
end

-- Make a task ready in its queue; index its domain unless the domain is gated.
local function enqueue(id, q, dom, pri)
  local seq = redis.call('INCR', P .. 'seq')
  local rank = (100 - pri) * BAND + seq
  redis.call('ZADD', queue_key(q, dom), rank, id)
  redis.call('HSET', task_key(id), 'st', 'ready', 'seq', seq)
  if eligible(q, dom) then
    local cur = redis.call('ZSCORE', ready_key(q), dom)
    if (not cur) or rank < tonumber(cur) then
      redis.call('ZADD', ready_key(q), rank, dom)
    end
  end
end

local function schedule_at(id, due)
  redis.call('HSET', task_key(id), 'st', 'scheduled', 'due', fmt(due))
  redis.call('ZADD', P .. 'scheduled', due, id)
end

-- Due scheduled tasks -> ready. Atomic per call; a task no longer
-- 'scheduled' (already promoted) is left alone.
local function promote_scheduled(limit)
  local due = redis.call('ZRANGEBYSCORE', P .. 'scheduled', '-inf', now, 'LIMIT', 0, limit)
  for i = 1, #due do
    local id = due[i]
    redis.call('ZREM', P .. 'scheduled', id)
    local t = redis.call('HMGET', task_key(id), 'st', 'q', 'dom', 'pri')
    if t[1] == 'scheduled' then
      redis.call('HDEL', task_key(id), 'due')
      enqueue(id, t[2], t[3], tonumber(t[4]))
    else
      count('anomaly')
    end
  end
  return #due
end

-- Expired domain gates -> the domain re-enters every queue it has work in,
-- at that queue's current head; the queue that claimed last is held back for
-- one interval if another queue has work there (bounded, work-conserving).
local function promote_gates(limit, default_interval)
  local due = redis.call('ZRANGEBYSCORE', P .. 'gate', '-inf', now, 'LIMIT', 0, limit)
  for i = 1, #due do
    local dom = due[i]
    redis.call('ZREM', P .. 'gate', dom)
    local last = redis.call('HGET', P .. 'gateq', dom)
    redis.call('HDEL', P .. 'gateq', dom)
    local heads, others = {}, false
    for j = 1, #QUEUES do
      local head = redis.call('ZRANGE', queue_key(QUEUES[j], dom), 0, 0, 'WITHSCORES')
      if #head > 0 then
        heads[j] = head[2]
        if QUEUES[j] ~= last then others = true end
      end
    end
    for j = 1, #QUEUES do
      if heads[j] then
        if QUEUES[j] == last and others then
          redis.call('ZADD', P .. 'yield', now + domain_interval(dom, default_interval),
            last .. '|' .. dom)
        else
          redis.call('ZADD', ready_key(QUEUES[j]), heads[j], dom)
        end
      end
    end
  end
end

-- Expired yields -> the held queue becomes eligible again (unless re-gated).
local function promote_yields(limit)
  local due = redis.call('ZRANGEBYSCORE', P .. 'yield', '-inf', now, 'LIMIT', 0, limit)
  for i = 1, #due do
    redis.call('ZREM', P .. 'yield', due[i])
    local sep = string.find(due[i], '|', 1, true)
    local q, dom = string.sub(due[i], 1, sep - 1), string.sub(due[i], sep + 1)
    if not gated(dom) then sync_ready(q, dom) end
  end
end

-- Shared politeness gate: closes dom for every queue; remembers who claimed.
local function close_gate(dom, until_t, q)
  redis.call('ZADD', P .. 'gate', until_t, dom)
  redis.call('HSET', P .. 'gateq', dom, q)
  for j = 1, #QUEUES do
    redis.call('ZREM', ready_key(QUEUES[j]), dom)
    redis.call('ZREM', P .. 'yield', QUEUES[j] .. '|' .. dom)
  end
end

local function backoff(att, base, cap)
  local b = base * 2 ^ (att - 1)
  if b > cap then b = cap end
  return b
end
