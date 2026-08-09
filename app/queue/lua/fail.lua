-- ARGV: prefix, id, lease_token, error, retryable (0/1), retry_delay_ms, job_ttl, dead_ttl, duration_ms
-- Returns 'retry', 'dead', 'failed', or false when the caller no longer holds the lease.
local id, token, err = ARGV[2], ARGV[3], ARGV[4]
local retryable, delay = ARGV[5] == '1', tonumber(ARGV[6])
local job_ttl, dead_ttl, duration = tonumber(ARGV[7]), tonumber(ARGV[8]), ARGV[9]
local key = job_key(id)
local cur = redis.call('HMGET', key, 'state', 'lease_token', 'attempts', 'max_attempts', 'worker_id')
if cur[1] ~= 'running' or cur[2] ~= token then return false end

local now = now_ms()
local attempts, max_attempts = tonumber(cur[3]), tonumber(cur[4])
redis.call('ZREM', LEASES, id)
redis.call('HDEL', key, 'lease_token')
redis.call('HSET', key, 'error', err, 'updated_at', now)
local detail = duration .. ' ms: ' .. err

if retryable and attempts < max_attempts then
  local eligible = now + delay
  redis.call('HSET', key, 'state', 'retry_scheduled', 'eligible_at', eligible)
  redis.call('ZADD', SCHEDULED, eligible, id)
  redis.call('HINCRBY', STATS, 'retried', 1)
  add_history(id, now, 'retry_scheduled', 'retry_scheduled', attempts, cur[5], detail)
  emit(id, 'running')
  return 'retry'
end

if retryable then
  redis.call('HSET', key, 'state', 'dead_letter', 'finished_at', now)
  redis.call('ZADD', DEAD, now, id)
  redis.call('HINCRBY', STATS, 'dead_letter', 1)
  add_history(id, now, 'dead_letter', 'dead_letter', attempts, cur[5], detail)
  expire_job(id, dead_ttl)
  emit(id, 'running')
  return 'dead'
end

redis.call('HSET', key, 'state', 'failed', 'finished_at', now)
redis.call('HINCRBY', STATS, 'failed', 1)
add_history(id, now, 'failed', 'failed', attempts, cur[5], detail)
expire_job(id, job_ttl)
emit(id, 'running')
return 'failed'
