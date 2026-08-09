-- ARGV: prefix, id, type, payload, priority, max_attempts, delay_ms
local id, jtype, payload = ARGV[2], ARGV[3], ARGV[4]
local priority, max_attempts, delay = ARGV[5], ARGV[6], tonumber(ARGV[7])
local key = job_key(id)
if redis.call('EXISTS', key) == 1 then return 0 end

local now = now_ms()
local state, eligible = 'queued', now
if delay > 0 then state, eligible = 'scheduled', now + delay end

redis.call('HSET', key, 'id', id, 'type', jtype, 'payload', payload, 'state', state,
  'priority', priority, 'attempts', 0, 'max_attempts', max_attempts,
  'created_at', now, 'updated_at', now, 'eligible_at', eligible)
redis.call('ZADD', JOBS, now, id)
if state == 'queued' then
  redis.call('ZADD', READY, ready_score(priority), id)
  wake()
else
  redis.call('ZADD', SCHEDULED, eligible, id)
end
redis.call('HINCRBY', STATS, 'submitted', 1)
add_history(id, now, 'submitted', state, 0, '', '')
emit(id, '')
return 1
