"""The versioned queue's atomic state machine, executed by Redis, never by mocks."""

QUEUE_SCRIPT = r"""
local P=KEYS[1]
local q=cjson.decode(ARGV[1])
local tm=redis.call('TIME')
local now=tonumber(tm[1])+tonumber(tm[2])/1000000
local active={queued=true,delayed=true,running=true,cancel_requested=true}
local workers={deferred=3,maintenance=1,daily=2,broadcast=1}
local function k(t,id) return P..':'..t..':'..tostring(id) end
local function enc(v) return cjson.encode(v) end
local function get(id)
 local raw=redis.call('GET',k('job',id))
 return raw and cjson.decode(raw) or nil
end
local function save(m)
 redis.call('SET',k('job',m.task_id),enc(m))
 if m.metadata_expires_at and m.metadata_expires_at~=cjson.null then
  redis.call('PEXPIREAT',k('job',m.task_id),math.ceil(m.metadata_expires_at*1000))
 end
end
local function admit_result(state,id,reason)
 return {state=state,task_id=id or cjson.null,reason_code=reason or cjson.null,storage_mode='redis'}
end
local function drop_payload(m)
 redis.call('DEL',k('payload',m.task_id))
 redis.call('ZREM',k('expiry','payload'),m.task_id)
 if m.payload_accounted then
  redis.call('HINCRBY',k('counts',m.lane),'payload_bytes',-m.payload_bytes)
  m.payload_accounted=false
 end
 m.retry_eligible=false; m.retry_payload_available=false
end
local function release_running(m)
 if m.state=='running' or m.state=='cancel_requested' then
  redis.call('HINCRBY',k('counts',m.lane),'running',-1)
 end
 redis.call('ZREM',k('running','all'),m.task_id)
 if m.owner_token and m.owner_token~=cjson.null and redis.call('GET',k('permit','image'))==m.owner_token then
  redis.call('DEL',k('permit','image'))
 end
end
local function ready(m)
 redis.call('ZADD',k('ready',m.lane),(5-m.priority)*1000000000000+m.sequence,m.task_id)
end
local function transition(m,state)
 redis.call('HINCRBY',k('counts',m.lane),'state:'..m.state,-1)
 redis.call('HINCRBY',k('counts',m.lane),'state:'..state,1)
 m.state=state
 redis.call('HSET',k('owners','metadata'),m.task_id,enc({user_id=m.user_id,lane=m.lane,state=state}))
end
local function release_conflicts(m)
 for _,id in ipairs(m.conflict_keys or {}) do
  local conflict=k('conflict',m.lane..':'..id)
  if redis.call('GET',conflict)==m.task_id then redis.call('DEL',conflict) end
 end
end
local function terminal(m,state,reason,safe)
 if active[m.state] then redis.call('HINCRBY',k('counts',m.lane),'nonterminal',-1) end
 release_running(m)
 release_conflicts(m)
 redis.call('ZREM',k('ready',m.lane),m.task_id)
 redis.call('ZREM',k('delayed',m.lane),m.task_id)
 redis.call('ZREM',k('deadline','all'),m.task_id)
 transition(m,state); m.reason_code=reason or cjson.null; m.finished_at=now
 m.owner_token=cjson.null; m.revision=m.revision+1; m.metadata_expires_at=now+86400
 local retain=safe and m.retry_grants>0 and redis.call('EXISTS',k('payload',m.task_id))==1
 if retain then
  m.retry_eligible=true; m.retry_payload_available=true; m.payload_expires_at=now+900
  redis.call('PEXPIRE',k('payload',m.task_id),900000)
  redis.call('ZADD',k('expiry','payload'),m.payload_expires_at,m.task_id)
 else drop_payload(m) end
 for _,identity in ipairs(redis.call('SMEMBERS',k('identities',m.task_id))) do redis.call('EXPIRE',identity,86400) end
 redis.call('EXPIRE',k('identities',m.task_id),86400)
 redis.call('ZADD',k('expiry','metadata'),m.metadata_expires_at,m.task_id)
 redis.call('ZADD',k('terminal',m.lane..':'..state),m.metadata_expires_at,m.task_id)
 if reason=='invalid_job_payload' then redis.call('ZADD',k('malformed',m.lane),m.metadata_expires_at,m.task_id) end
 save(m)
end
local function uncertain_reason(m)
 return m.kind=='document_processing' and 'document_storage_unconfirmed' or 'interrupted_delivery'
end
local function sweep()
 for _,id in ipairs(redis.call('ZRANGEBYSCORE',k('expiry','payload'),'-inf',now,'LIMIT',0,256)) do
  local m=get(id)
  if m then
   drop_payload(m)
   if active[m.state] then
    local uncertain=(m.state=='running' or m.state=='cancel_requested') and m.effectful
    terminal(m,uncertain and 'unknown_delivery' or 'expired','payload_expired',false)
   else save(m) end
  else redis.call('ZREM',k('expiry','payload'),id) end
 end
 for _,id in ipairs(redis.call('ZRANGEBYSCORE',k('deadline','all'),'-inf',now,'LIMIT',0,256)) do
  local m=get(id)
  if m and active[m.state] then
   local uncertain=(m.state=='running' or m.state=='cancel_requested') and m.effectful
   terminal(m,uncertain and 'unknown_delivery' or 'expired',uncertain and uncertain_reason(m) or 'deadline_exceeded',false)
  else redis.call('ZREM',k('deadline','all'),id) end
 end
 for _,id in ipairs(redis.call('ZRANGEBYSCORE',k('running','all'),'-inf',now,'LIMIT',0,256)) do
  local m=get(id)
  if m and (m.state=='running' or m.state=='cancel_requested') then
   if m.effectful then terminal(m,'unknown_delivery',uncertain_reason(m),false)
   elseif m.state=='cancel_requested' then terminal(m,'cancelled','cancel_requested',false)
   elseif m.attempts_started>=m.max_attempts then terminal(m,'failed','attempts_exhausted',true)
   elseif now>=(m.queue_deadline_at or m.deadline_at) then terminal(m,'expired','deadline_exceeded',true)
   else release_running(m);transition(m,'queued');m.owner_token=cjson.null;m.revision=m.revision+1;m.deadline_at=m.queue_deadline_at or m.deadline_at;redis.call('ZADD',k('deadline','all'),m.deadline_at,id);ready(m);save(m) end
  else redis.call('ZREM',k('running','all'),id) end
 end
 for lane,_ in pairs(workers) do
  for _,id in ipairs(redis.call('ZRANGEBYSCORE',k('delayed',lane),'-inf',now,'LIMIT',0,128)) do
   redis.call('ZREM',k('delayed',lane),id)
   local m=get(id)
   if m and m.state=='delayed' then transition(m,'queued');m.revision=m.revision+1;ready(m);save(m) end
  end
 end
 for _,id in ipairs(redis.call('ZRANGEBYSCORE',k('expiry','metadata'),'-inf',now,'LIMIT',0,256)) do
  local owner=redis.call('HGET',k('owners','metadata'),id)
  if owner then
   owner=cjson.decode(owner)
   redis.call('SREM',k('user',owner.user_id),id)
   redis.call('HINCRBY',k('counts',owner.lane),'state:'..owner.state,-1)
   redis.call('ZREM',k('terminal',owner.lane..':'..owner.state),id)
   redis.call('ZREM',k('malformed',owner.lane),id)
  end
  redis.call('HDEL',k('owners','metadata'),id)
  redis.call('ZREM',k('index','all'),id)
  redis.call('ZREM',k('expiry','metadata'),id)
  redis.call('DEL',k('commands',id))
  for _,identity in ipairs(redis.call('SMEMBERS',k('identities',id))) do redis.call('DEL',identity) end
  redis.call('DEL',k('identities',id))
 end
end
local function lane_state(lane,policy)
 local raw=redis.call('GET',k('lane',lane))
 return raw and cjson.decode(raw) or {revision=0,admission_paused=false,claim_paused=false,policy=policy}
end
local function admission(j,p,identity,fingerprint,payload_credit)
 local idem=k('idem',identity)
 local old=redis.call('GET',idem)
 if old then
  old=cjson.decode(old)
  return admit_result(old.fingerprint==fingerprint and 'existing' or 'rejected',old.task_id,
   old.fingerprint~=fingerprint and 'idempotency_conflict' or nil)
 end
 if not workers[j.lane] then return admit_result('rejected',nil,'unknown_lane') end
 p.max_jobs=math.min(p.max_jobs,j.lane=='daily' and 32 or (j.lane=='broadcast' and 10 or 100))
 p.max_payload_bytes=math.min(p.max_payload_bytes,262144)
 p.max_total_payload_bytes=math.min(p.max_total_payload_bytes,8388608)
 p.max_age_seconds=math.min(p.max_age_seconds,j.lane=='daily' and 1800 or 900);p.max_attempts=math.min(p.max_attempts,3)
 for _,id in ipairs(j.conflict_keys or {}) do
  local existing=redis.call('GET',k('conflict',j.lane..':'..id))
  if existing then
   if redis.call('SCARD',k('identities',existing))>=256 then return admit_result('rejected',nil,'idempotency_capacity') end
   redis.call('SET',idem,enc({task_id=existing,fingerprint=fingerprint}),'EX',86400)
   redis.call('SADD',k('identities',existing),idem)
   return admit_result('existing',existing,'conflict_existing')
  end
 end
 local lane=lane_state(j.lane,p)
 for name,limit in pairs(lane.policy) do p[name]=math.min(p[name],limit) end
 local counts=k('counts',j.lane)
 local size=string.len(j.payload)
 if lane.admission_paused then return admit_result('rejected',nil,'admission_paused') end
 if size>p.max_payload_bytes then return admit_result('rejected',nil,'payload_too_large') end
 if tonumber(redis.call('HGET',counts,'nonterminal') or '0')>=p.max_jobs then return admit_result('rejected',nil,'job_capacity') end
 if tonumber(redis.call('HGET',counts,'payload_bytes') or '0')+size-(payload_credit or 0)>p.max_total_payload_bytes then return admit_result('rejected',nil,'byte_capacity') end
 local id,payload=j.task_id,j.payload
 j.payload=nil;j.idempotency_key=nil;j.schema_version=2;j.revision=1
 j.state=j.delay_seconds>0 and 'delayed' or 'queued';j.attempts_started=0;j.max_attempts=p.max_attempts
 j.submitted_at=now;j.started_at=cjson.null;j.finished_at=cjson.null;j.heartbeat_at=cjson.null
 j.not_before=now+math.max(0,j.delay_seconds);j.deadline_at=now+p.max_age_seconds;j.reason_code=cjson.null
 j.queue_deadline_at=j.deadline_at
 j.retry_eligible=false;j.retry_payload_available=false;j.result_summary=j.public_summary or {};j.public_summary=nil;j.payload_bytes=size;j.payload_accounted=true
 j.sequence=redis.call('INCR',k('sequence','all'));j.identity=identity;j.fingerprint=fingerprint
 j.owner_token=cjson.null;j.payload_expires_at=now+(j.user_id>0 and math.min(900,p.max_age_seconds) or p.max_age_seconds);j.metadata_expires_at=cjson.null
 save(j)
 redis.call('SET',k('payload',id),payload,'PX',math.ceil((j.payload_expires_at-now)*1000))
 redis.call('SET',idem,enc({task_id=id,fingerprint=fingerprint}),'EX',86400)
 redis.call('SADD',k('identities',id),idem)
 for _,conflict in ipairs(j.conflict_keys or {}) do redis.call('SET',k('conflict',j.lane..':'..conflict),id) end
 redis.call('ZADD',k('index','all'),j.sequence,id)
 redis.call('SADD',k('user',j.user_id),id)
 redis.call('HSET',k('owners','metadata'),id,enc({user_id=j.user_id,lane=j.lane,state=j.state}))
 redis.call('ZADD',k('deadline','all'),j.deadline_at,id)
 redis.call('ZADD',k('expiry','payload'),j.payload_expires_at,id)
 redis.call('HINCRBY',counts,'nonterminal',1);redis.call('HINCRBY',counts,'payload_bytes',size)
 redis.call('HINCRBY',counts,'state:'..j.state,1)
 redis.call('SET',k('lane',j.lane),enc(lane))
 if j.state=='delayed' then redis.call('ZADD',k('delayed',j.lane),j.not_before,id) else ready(j) end
 return admit_result('accepted',id)
end
sweep()
if q.action=='admit' then return enc(admission(q.job,q.policy,q.identity,q.fingerprint)) end
if q.action=='lookup_admission' then
 local old=redis.call('GET',k('idem',q.identity))
 if not old then return enc(admit_result('unknown',q.job.task_id,'admission_unconfirmed')) end
 old=cjson.decode(old)
 return enc(admit_result(old.fingerprint==q.fingerprint and 'existing' or 'rejected',old.task_id,
  old.fingerprint~=q.fingerprint and 'idempotency_conflict' or nil))
end
if q.action=='read' or q.action=='private' then return enc(get(q.task_id)) end
if q.action=='sweep' then return enc({observed_at=now}) end
if q.action=='claim' then
 local lane=lane_state(q.lane,q.policy)
 if lane.claim_paused or tonumber(redis.call('HGET',k('counts',q.lane),'running') or '0')>=workers[q.lane] then return 'null' end
 for _,id in ipairs(redis.call('ZRANGE',k('ready',q.lane),0,127)) do
  local m=get(id)
  if not m or m.state~='queued' then redis.call('ZREM',k('ready',q.lane),id)
  elseif m.attempts_started>=m.max_attempts then terminal(m,'failed','attempts_exhausted',true)
  elseif not m.image_work or redis.call('EXISTS',k('permit','image'))==0 then
   local present=redis.call('EXISTS',k('payload',id))
   if present==0 then terminal(m,'expired','payload_expired',false)
   else
    redis.call('ZREM',k('ready',q.lane),id)
    if m.lane=='daily' and m.user_id==0 then
     local budget=m.execution_timeout_seconds
     if not budget or budget==cjson.null then budget=string.sub(m.kind,1,6)=='tarot.' and 1800 or 600 end
     m.deadline_at=now+math.min(1800,budget)+10;m.payload_expires_at=m.deadline_at
     redis.call('ZADD',k('deadline','all'),m.deadline_at,id)
     redis.call('ZADD',k('expiry','payload'),m.payload_expires_at,id)
     redis.call('PEXPIRE',k('payload',id),math.ceil((m.payload_expires_at-now)*1000))
    end
    transition(m,'running');m.owner_token=q.owner_token;m.started_at=now;m.heartbeat_at=now
    m.attempts_started=m.attempts_started+1;m.revision=m.revision+1;m.finish_token=nil
    redis.call('ZADD',k('running','all'),now+60,id)
    redis.call('HINCRBY',k('counts',q.lane),'running',1)
    if m.image_work then redis.call('SET',k('permit','image'),q.owner_token,'EX',60) end
    save(m);return enc({record=m})
   end
  end
 end
 return 'null'
end
local m=q.task_id and get(q.task_id) or nil
if q.action=='load' then
 if not m or m.state~='running' or m.owner_token~=q.owner_token then return enc({error='payload_not_available'}) end
 local payload=redis.call('GET',k('payload',q.task_id))
 if not payload then return enc({error='payload_not_available'}) end
 return enc(payload)
end
if q.action=='progress' then
 if not m or m.state~='running' or m.owner_token~=q.owner_token or m.revision~=q.expected_revision then return enc({error='owner_or_revision_conflict'}) end
 for name,value in pairs(q.summary) do m.result_summary[name]=value end
 save(m);return enc(m)
end
if q.action=='heartbeat' or q.action=='finish' then
 if q.action=='finish' and m and m.finish_token==q.owner_token then return enc(m) end
 if not m or (m.state~='running' and m.state~='cancel_requested') or m.owner_token~=q.owner_token or m.revision~=q.expected_revision then return enc({error='owner_or_revision_conflict'}) end
 if q.action=='heartbeat' then
  m.heartbeat_at=now
  redis.call('ZADD',k('running','all'),now+60,m.task_id)
  if m.image_work and redis.call('GET',k('permit','image'))==q.owner_token then redis.call('EXPIRE',k('permit','image'),60) end
  save(m);return enc(m)
 end
 for name,value in pairs(q.result.result_summary) do m.result_summary[name]=value end
 if q.result.outcome=='unknown_delivery' then terminal(m,'unknown_delivery',q.result.reason_code,false)
 elseif m.state=='cancel_requested' then terminal(m,'cancelled','cancel_requested',false)
 elseif q.result.outcome=='retryable_failure' then
  if m.attempts_started>=m.max_attempts then terminal(m,'failed',q.result.reason_code or 'attempts_exhausted',true)
  elseif now+q.delay_seconds>=(m.queue_deadline_at or m.deadline_at) then terminal(m,'expired','retry_after_deadline',true)
  else
   release_running(m);transition(m,q.delay_seconds>0 and 'delayed' or 'queued');m.owner_token=cjson.null
   m.not_before=now+q.delay_seconds;m.reason_code=q.result.reason_code;m.revision=m.revision+1
   m.deadline_at=m.queue_deadline_at or m.deadline_at
   m.payload_expires_at=m.user_id>0 and math.min(m.deadline_at,m.submitted_at+900) or m.deadline_at
   redis.call('ZADD',k('deadline','all'),m.deadline_at,m.task_id)
   redis.call('ZADD',k('expiry','payload'),m.payload_expires_at,m.task_id)
   redis.call('PEXPIRE',k('payload',m.task_id),math.max(1,math.ceil((m.payload_expires_at-now)*1000)))
   if m.state=='delayed' then redis.call('ZADD',k('delayed',m.lane),m.not_before,m.task_id) else ready(m) end
  end
 else
  local states={succeeded='succeeded',permanent_failure='failed',unknown_delivery='unknown_delivery'}
  terminal(m,states[q.result.outcome],q.result.reason_code,false)
 end
 m.finish_token=q.owner_token;save(m);return enc(m)
end
if q.action=='cancel' or q.action=='retry' or q.action=='set_lane' then
 local op=k('operation',q.operation_key)
 local old=redis.call('GET',op)
 if old then
  old=cjson.decode(old)
  if old.fingerprint~=q.command_fingerprint then return enc({error='operation_id_conflict'}) end
  if q.action=='retry' then return enc(admit_result('existing',old.target)) end
  if q.action=='cancel' then return enc(get(old.target)) end
  return enc(lane_state(q.lane,q.policy))
 end
 local target=q.task_id or q.lane
 if q.action=='set_lane' then
  local lane=lane_state(q.lane,q.policy)
  if lane.revision~=q.expected_revision then return enc({error='revision_conflict'}) end
  lane.revision=lane.revision+1;lane.admission_paused=q.admission_paused;lane.claim_paused=q.claim_paused
  redis.call('SET',k('lane',q.lane),enc(lane))
  redis.call('SET',op,enc({fingerprint=q.command_fingerprint,target=target}),'EX',86400)
  return enc(lane)
 end
 if not m or m.revision~=q.expected_revision then return enc({error='revision_conflict'}) end
 if q.action=='cancel' then
  if m.state=='running' or m.state=='cancel_requested' then transition(m,'cancel_requested');m.revision=m.revision+1;m.reason_code='cancel_requested';save(m)
  elseif active[m.state] then terminal(m,'cancelled','cancel_requested',false) end
 else
  local payload=redis.call('GET',k('payload',m.task_id))
  if (m.state~='failed' and m.state~='expired') or not m.retry_eligible or m.retry_grants<1 or not payload or m.privacy_epoch~=q.privacy_epoch then return enc({error='retry_not_available'}) end
  local child={task_id=q.child_id,kind=m.kind,lane=m.lane,user_id=m.user_id,payload=payload,idempotency_key=q.operation_key,privacy_epoch=q.privacy_epoch,
   priority=m.priority,delay_seconds=0,effectful=m.effectful,image_work=m.image_work,parent_task_id=m.task_id,retry_grants=0,
   conflict_keys=m.conflict_keys or {},public_summary=m.result_summary,execution_timeout_seconds=m.execution_timeout_seconds}
  local accepted=admission(child,q.policy,q.child_identity,q.command_fingerprint,m.payload_accounted and m.payload_bytes or 0)
  if accepted.state~='accepted' and accepted.state~='existing' then return enc(accepted) end
  if accepted.task_id~=q.child_id then
   redis.call('SET',op,enc({fingerprint=q.command_fingerprint,target=accepted.task_id}),'EX',86400)
   redis.call('SADD',k('commands',m.task_id),op);return enc(accepted)
  end
  m.retry_grants=0;drop_payload(m);save(m);target=q.child_id
  redis.call('SET',op,enc({fingerprint=q.command_fingerprint,target=target}),'EX',86400)
  redis.call('SADD',k('commands',m.task_id),op);return enc(accepted)
 end
 redis.call('SET',op,enc({fingerprint=q.command_fingerprint,target=target}),'EX',86400)
 redis.call('SADD',k('commands',m.task_id),op);return enc(m)
end
if q.action=='lane' then return enc(lane_state(q.lane,q.policy)) end
if q.action=='list' then
 local items={}
 local offset=q.offset
 for _,id in ipairs(redis.call('ZRANGE',k('index','all'),q.offset,q.offset+255)) do
  offset=offset+1
  local record=get(id)
  if record and (not q.lane or q.lane==cjson.null or record.lane==q.lane) and (not q.state or q.state==cjson.null or record.state==q.state) then table.insert(items,record) end
  if #items>=q.limit then break end
 end
 return enc({items=items,next_cursor=redis.call('ZCARD',k('index','all'))>offset and tostring(offset) or cjson.null})
end
if q.action=='erase' then
 local ids=redis.call('SRANDMEMBER',k('user',q.user_id),100)
 for _,id in ipairs(ids) do
  local record=get(id)
  if record then
   if active[record.state] then redis.call('HINCRBY',k('counts',record.lane),'nonterminal',-1) end
   redis.call('HINCRBY',k('counts',record.lane),'state:'..record.state,-1)
   release_running(record);release_conflicts(record);drop_payload(record)
   redis.call('ZREM',k('terminal',record.lane..':'..record.state),id)
   redis.call('ZREM',k('malformed',record.lane),id)
   redis.call('ZREM',k('ready',record.lane),id);redis.call('ZREM',k('delayed',record.lane),id)
   redis.call('DEL',k('idem',record.identity));redis.call('DEL',k('job',id))
  end
  for _,command in ipairs(redis.call('SMEMBERS',k('commands',id))) do redis.call('DEL',command) end
  for _,identity in ipairs(redis.call('SMEMBERS',k('identities',id))) do redis.call('DEL',identity) end
  redis.call('DEL',k('identities',id))
  redis.call('DEL',k('commands',id));redis.call('ZREM',k('deadline','all'),id)
  redis.call('ZREM',k('index','all'),id);redis.call('ZREM',k('expiry','metadata'),id)
  redis.call('HDEL',k('owners','metadata'),id);redis.call('SREM',k('user',q.user_id),id)
 end
 return enc({remaining=redis.call('SCARD',k('user',q.user_id))})
end
if q.action=='stats' then
 local result={nonterminal=tonumber(redis.call('HGET',k('counts',q.lane),'nonterminal') or '0'),payload_bytes=tonumber(redis.call('HGET',k('counts',q.lane),'payload_bytes') or '0'),
  observed_at=now,available=true,scope='shared',oldest_age_seconds=0,healthy_owners=0,retrying=0}
 result.malformed_payloads=redis.call('ZCOUNT',k('malformed',q.lane),now,'+inf')
 for _,state in ipairs({'queued','delayed','running','cancel_requested','succeeded','failed','cancelled','expired','unknown_delivery'}) do
  result[state]=active[state] and tonumber(redis.call('HGET',k('counts',q.lane),'state:'..state) or '0')
      or redis.call('ZCOUNT',k('terminal',q.lane..':'..state),now,'+inf')
 end
 local ids=redis.call('ZRANGE',k('ready',q.lane),0,127)
 for _,id in ipairs(redis.call('ZRANGE',k('delayed',q.lane),0,127)) do table.insert(ids,id) end
 for _,id in ipairs(redis.call('ZRANGE',k('running','all'),0,127)) do table.insert(ids,id) end
 for _,id in ipairs(ids) do
  local record=get(id)
  if record and record.lane==q.lane then
   if (record.state=='queued' or record.state=='delayed') and record.attempts_started>0 then result.retrying=result.retrying+1 end
   if active[record.state] then result.oldest_age_seconds=math.max(result.oldest_age_seconds,now-record.submitted_at) end
   if (record.state=='running' or record.state=='cancel_requested') and record.heartbeat_at+60>now then result.healthy_owners=result.healthy_owners+1 end
  end
 end
 return enc(result)
end
return enc({error='unknown_command'})
"""
