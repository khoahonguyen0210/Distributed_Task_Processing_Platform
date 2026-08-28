-- Shared helpers. Every script receives the key prefix as ARGV[1].
local P = ARGV[1]
local READY = P .. 'ready'
local SCHEDULED = P .. 'scheduled'
local LEASES = P .. 'leases'
local DEAD = P .. 'dead'
local JOBS = P .. 'jobs'
local STATS = P .. 'stats'
local WAKE = P .. 'wake'

local function now_ms()
  local t = redis.call('TIME')
  return tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
end

local function job_key(id) return P .. 'job:' .. id end
local function history_key(id) return P .. 'job:' .. id .. ':history' end

-- Higher priority first, then the order jobs became ready. A counter keeps
-- that order exact, since two jobs can share a millisecond.
local function ready_score(priority)
  local seq = redis.call('INCR', P .. 'seq')
  return string.format('%.0f', (9 - tonumber(priority)) * 1e14 + seq)
end

local function add_history(id, now, event, state, attempt, worker, detail)
  local h = history_key(id)
  redis.call('RPUSH', h, cjson.encode({
    ts = now, event = event, state = state, attempt = tonumber(attempt) or 0,
    worker_id = worker or '', detail = detail or ''
  }))
  redis.call('LTRIM', h, -50, -1)
end

local function wake()
  redis.call('LPUSH', WAKE, '1')
  redis.call('LTRIM', WAKE, 0, 99)
end

local function emit(id, previous)
  local f = redis.call('HMGET', job_key(id),
    'type', 'state', 'attempts', 'max_attempts', 'priority', 'updated_at', 'worker_id', 'error')
  redis.call('PUBLISH', P .. 'events', cjson.encode({
    job_id = id, job_type = f[1], state = f[2], previous = previous or '',
    attempts = tonumber(f[3]), max_attempts = tonumber(f[4]), priority = tonumber(f[5]),
    ts = tonumber(f[6]), worker_id = f[7] or '', error = f[8] or ''
  }))
end

local function expire_job(id, seconds)
  redis.call('EXPIRE', job_key(id), seconds)
  redis.call('EXPIRE', history_key(id), seconds)
end

local function persist_job(id)
  redis.call('PERSIST', job_key(id))
  redis.call('PERSIST', history_key(id))
end
