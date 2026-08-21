-- ARGV: prefix, id, ttl_seconds
-- Only jobs that are waiting can be cancelled. A running job is never interrupted.
-- Returns 1 on success, the current state if it cannot be cancelled, or false if missing.
local id, ttl = ARGV[2], tonumber(ARGV[3])
local key = job_key(id)
local state = redis.call('HGET', key, 'state')
if state ~= 'queued' and state ~= 'scheduled' and state ~= 'retry_scheduled' then
  return state or false
end

local now = now_ms()
redis.call('ZREM', READY, id)
redis.call('ZREM', SCHEDULED, id)
redis.call('HSET', key, 'state', 'cancelled', 'finished_at', now, 'updated_at', now)
redis.call('HINCRBY', STATS, 'cancelled', 1)
add_history(id, now, 'cancelled', 'cancelled', redis.call('HGET', key, 'attempts'), '', '')
expire_job(id, ttl)
emit(id, state)
return 1
