-- ARGV: prefix, batch_size
-- Moves delayed and retry jobs whose time has come into the ready set.
local batch = tonumber(ARGV[2])
local now = now_ms()
local due = redis.call('ZRANGEBYSCORE', SCHEDULED, '-inf', now, 'LIMIT', 0, batch)
local moved = 0

for _, id in ipairs(due) do
  redis.call('ZREM', SCHEDULED, id)
  local key = job_key(id)
  local f = redis.call('HMGET', key, 'state', 'priority', 'attempts')
  if f[1] == 'scheduled' or f[1] == 'retry_scheduled' then
    redis.call('HSET', key, 'state', 'queued', 'updated_at', now)
    redis.call('ZADD', READY, ready_score(f[2]), id)
    add_history(id, now, 'queued', 'queued', f[3], '', '')
    emit(id, f[1])
    moved = moved + 1
  end
end

if moved > 0 then wake() end
return moved
