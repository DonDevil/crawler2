-- heartbeat: ARGV[4] url_id, [5] token, [6] lease_ttl.
-- Returns the new lease expiry, or nil when the token is not current.
local id, token, ttl = ARGV[4], ARGV[5], tonumber(ARGV[6])
local tk = task_key(id)
local t = redis.call('HMGET', tk, 'tok', 'st')
if t[1] ~= token or t[2] ~= 'leased' then return false end
local lex = now + ttl
redis.call('HSET', tk, 'lex', fmt(lex))
redis.call('ZADD', P .. 'leases', lex, id)
return fmt(lex)
