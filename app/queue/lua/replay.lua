-- ARGV: prefix, id
-- Sends a dead-letter job back to the queue with a fresh attempt count.
-- The last error and the full history are kept.
local id = ARGV[2]
local key = job_key(id)
if redis.call('HGET', key, 'state') ~= 'dead_letter' then return 0 end

local now = now_ms()
local priority = redis.call('HGET', key, 'priority')
redis.call('ZREM', DEAD, id)
redis.call('HSET', key, 'state', 'queued', 'attempts', 0, 'updated_at', now, 'eligible_at', now)
redis.call('HDEL', key, 'finished_at', 'worker_id')
redis.call('ZADD', READY, ready_score(priority), id)
redis.call('HINCRBY', STATS, 'replayed', 1)
persist_job(id)
add_history(id, now, 'replayed', 'queued', 0, '', '')
wake()
emit(id, 'dead_letter')
return 1
