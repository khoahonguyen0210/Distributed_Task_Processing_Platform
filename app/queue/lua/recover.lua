-- ARGV: prefix, batch_size, dead_ttl
-- Finds running jobs whose lease ran out (the worker died or hung) and takes them back.
-- A lost run counts as an attempt, so a job that always kills its worker ends in dead letter.
local batch, dead_ttl = tonumber(ARGV[2]), tonumber(ARGV[3])
local now = now_ms()
local expired = redis.call('ZRANGEBYSCORE', LEASES, '-inf', now, 'LIMIT', 0, batch)
local recovered = 0

for _, id in ipairs(expired) do
  redis.call('ZREM', LEASES, id)
  local key = job_key(id)
  local f = redis.call('HMGET', key, 'state', 'attempts', 'max_attempts', 'worker_id', 'priority')
  if f[1] == 'running' then
    local worker = f[4] or ''
    local err = 'Lease expired: worker ' .. worker .. ' stopped renewing it'
    redis.call('HDEL', key, 'lease_token')
    redis.call('HSET', key, 'error', err, 'updated_at', now)
    redis.call('HINCRBY', STATS, 'recovered', 1)
    if tonumber(f[2]) < tonumber(f[3]) then
      redis.call('HSET', key, 'state', 'queued', 'eligible_at', now)
      redis.call('ZADD', READY, ready_score(f[5]), id)
      add_history(id, now, 'recovered', 'queued', f[2], worker, err)
    else
      redis.call('HSET', key, 'state', 'dead_letter', 'finished_at', now)
      redis.call('ZADD', DEAD, now, id)
      redis.call('HINCRBY', STATS, 'dead_letter', 1)
      add_history(id, now, 'dead_letter', 'dead_letter', f[2], worker, err)
      expire_job(id, dead_ttl)
    end
    emit(id, 'running')
    recovered = recovered + 1
  end
end

if recovered > 0 then wake() end
return recovered
