-- ARGV: prefix, worker_id, lease_ms, lease_token
-- Pops the best ready job and leases it to one worker in a single atomic step.
local worker, lease_ms, token = ARGV[2], tonumber(ARGV[3]), ARGV[4]
local now = now_ms()

for _ = 1, 20 do
  local popped = redis.call('ZPOPMIN', READY)
  if #popped == 0 then return false end
  local id = popped[1]
  local key = job_key(id)
  -- Skip ids whose job expired or changed state; they are stale index entries.
  if redis.call('HGET', key, 'state') == 'queued' then
    local attempt = redis.call('HINCRBY', key, 'attempts', 1)
    redis.call('HSET', key, 'state', 'running', 'worker_id', worker, 'lease_token', token,
      'started_at', now, 'updated_at', now)
    redis.call('ZADD', LEASES, now + lease_ms, id)
    add_history(id, now, 'started', 'running', attempt, worker, '')
    emit(id, 'queued')
    return redis.call('HGETALL', key)
  end
end
return false
