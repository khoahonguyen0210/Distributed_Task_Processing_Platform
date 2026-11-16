local worker, lease_ms, token, job_ttl = ARGV[2], tonumber(ARGV[3]), ARGV[4], tonumber(ARGV[5])
local now = now_ms()

for _ = 1, 100 do
  local popped = redis.call('ZPOPMIN', READY)
  if #popped == 0 then return false end
  local id = popped[1]
  local key = job_key(id)
  local f = redis.call('HMGET', key, 'state', 'deadline_at', 'attempts')
  local deadline = tonumber(f[2])
  if f[1] == 'queued' and deadline and now > deadline then
    redis.call('HSET', key, 'state', 'expired', 'finished_at', now, 'updated_at', now)
    redis.call('HINCRBY', STATS, 'expired', 1)
    add_history(id, now, 'expired', 'expired', f[3], '', 'the deadline passed before a worker started it')
    expire_job(id, job_ttl)
    emit(id, 'queued')
  elseif f[1] == 'queued' then
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
