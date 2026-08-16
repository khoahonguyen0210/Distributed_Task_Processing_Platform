-- ARGV: prefix, id, lease_token
-- Puts a running job back in the queue when its worker must stop before the job ends.
-- The stopped run does not count as an attempt.
local id, token = ARGV[2], ARGV[3]
local key = job_key(id)
local cur = redis.call('HMGET', key, 'state', 'lease_token', 'priority', 'worker_id')
if cur[1] ~= 'running' or cur[2] ~= token then return 0 end

local now = now_ms()
local attempts = redis.call('HINCRBY', key, 'attempts', -1)
redis.call('ZREM', LEASES, id)
redis.call('HDEL', key, 'lease_token')
redis.call('HSET', key, 'state', 'queued', 'updated_at', now, 'eligible_at', now)
redis.call('ZADD', READY, ready_score(cur[3]), id)
add_history(id, now, 'released', 'queued', attempts, cur[4], 'worker stopped before the job ended')
wake()
emit(id, 'running')
return 1
