-- ARGV: prefix, id, lease_token, result, ttl_seconds, duration_ms
local id, token, result = ARGV[2], ARGV[3], ARGV[4]
local ttl, duration = tonumber(ARGV[5]), tonumber(ARGV[6])
local key = job_key(id)
local cur = redis.call('HMGET', key, 'state', 'lease_token', 'attempts', 'worker_id')
-- A worker that lost its lease cannot finish the job, because its lease token no longer matches.
if cur[1] ~= 'running' or cur[2] ~= token then return 0 end

local now = now_ms()
redis.call('HSET', key, 'state', 'succeeded', 'result', result, 'finished_at', now, 'updated_at', now)
redis.call('HDEL', key, 'lease_token', 'error')
redis.call('ZREM', LEASES, id)
redis.call('HINCRBY', STATS, 'succeeded', 1)
redis.call('LPUSH', P .. 'durations', duration)
redis.call('LTRIM', P .. 'durations', 0, 999)
add_history(id, now, 'succeeded', 'succeeded', cur[3], cur[4], duration .. ' ms')
expire_job(id, ttl)
emit(id, 'running')
return 1
