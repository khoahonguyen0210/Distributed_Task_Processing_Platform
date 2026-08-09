-- ARGV: prefix, id, lease_token, lease_ms
-- Extends a lease. Returns 0 if the lease was lost, so the worker knows its result will be refused.
local id, token, lease_ms = ARGV[2], ARGV[3], tonumber(ARGV[4])
local cur = redis.call('HMGET', job_key(id), 'state', 'lease_token')
if cur[1] ~= 'running' or cur[2] ~= token then return 0 end
redis.call('ZADD', LEASES, 'XX', now_ms() + lease_ms, id)
return 1
