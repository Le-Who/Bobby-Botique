/* Durable job IDs survive reload; drafts and provider inputs never enter storage. */
(function (root) {
  'use strict';
  const api=root.AdminApi;
  const terminal=new Set(['succeeded','failed','cancelled','expired','unknown_delivery']);
  const jobs=new Map(), callbacks=new Map(), inFlight=new Set(), startingScopes=new Set();
  const storageKey='admin-daily-jobs-v1';
  let timer=null, delay=2000, calendarBusy=false, polling=false;
  const node=(tag,text)=>{const el=document.createElement(tag); if(text !== undefined) el.textContent=String(text); return el;};
  const scope=cmd=>[cmd.kind,cmd.date,cmd.difficulty||'',cmd.part||'all'].join('|');

  function save() {
    try {
      sessionStorage.setItem(storageKey,JSON.stringify([...jobs.values()].slice(-32).map(({operationId,jobId,kind,date,difficulty,part})=>
        ({operationId,jobId,kind,date,difficulty,part}))));
    } catch (_) { /* Job IDs remain visible even when browser storage is unavailable. */ }
  }

  function announce(value) {
    const status=document.getElementById('daily-jobs-notice');
    if(status) status.textContent=value;
  }

  function render() {
    const list=document.getElementById('daily-jobs-list');
    if(!list) return;
    list.replaceChildren();
    for(const record of [...jobs.values()].reverse()) {
      const job=record.snapshot, row=node('li');
      const summary=job?.result_summary || {};
      row.append(node('strong',`${record.date} · ${record.kind}: ${job?.state || 'результат приёма неизвестен'}`));
      row.append(node('p',`ID: ${record.jobId || record.operationId}`));
      row.append(node('p',`Создано: ${summary.generated ?? 0}; пропущено: ${summary.skipped ?? 0}; ошибок: ${summary.failed ?? 0}; осталось: ${summary.remaining ?? '—'}.`));
      if(summary.total) {
        const progress=node('progress'); progress.max=summary.total; progress.value=summary.completed || 0;
        progress.setAttribute('aria-label',`Завершено ${progress.value} из ${progress.max}`); row.append(progress);
      }
      if(job?.reason_code) row.append(node('p',job.reason_code));
      if(job?.storage_mode === 'local') row.append(node('p','Состояние хранится только в этом процессе.'));
      if(record.error) row.append(node('p',record.error));
      const refresh=node('button','Проверить статус'); refresh.type='button';
      refresh.addEventListener('click',()=>pollRecord(record)); row.append(refresh);
      if(terminal.has(job?.state)) {
        const dismiss=node('button','Скрыть'); dismiss.type='button';
        dismiss.addEventListener('click',()=>{jobs.delete(record.operationId); save(); render();}); row.append(dismiss);
      }
      list.append(row);
    }
  }

  async function accept(record,job) {
    const previous=record.snapshot;
    if(previous?.task_id === job.task_id && (
      previous.revision > job.revision ||
      (terminal.has(previous.state) && !terminal.has(job.state)) ||
      (previous.revision === job.revision && previous.state === job.state &&
        (previous.result_summary?.completed || 0) > (job.result_summary?.completed || 0))
    )) return;
    record.jobId=job.task_id; record.snapshot=job; record.error=''; save(); render();
    root.dispatchEvent(new CustomEvent('daily-job-updated',{detail:job}));
    if(terminal.has(job.state)) {
      announce(`${record.kind}: ${job.state}${job.reason_code ? ' · '+job.reason_code : ''}`);
      const callback=callbacks.get(record.operationId); callbacks.delete(record.operationId);
      if(callback) await callback(job);
    }
  }

  async function pollRecord(record) {
    if(inFlight.has(record.operationId)) return;
    inFlight.add(record.operationId);
    try {
      const path=record.jobId ? `/api/admin/daily/jobs/${encodeURIComponent(record.jobId)}` : `/api/admin/daily/jobs/by-operation/${encodeURIComponent(record.operationId)}`;
      const job=await api.adminRequest(path);
      await accept(record,job);
    } catch(error) {
      if(error.code !== 'aborted') {
        record.error=error.status === 404 ? 'Задание пока не найдено или срок хранения истёк. Приём не подтверждён; автоматический повтор не выполняется.' : error.message;
        render();
      }
    } finally {inFlight.delete(record.operationId);}
  }

  async function tick() {
    if(polling) return;
    polling=true;
    clearTimeout(timer);
    try {
      if(!document.hidden && navigator.onLine !== false) {
        const active=[...jobs.values()].filter(record=>!terminal.has(record.snapshot?.state));
        if(active.length) {
          const result=await api.adminRequest('/api/admin/daily/jobs?operations='+encodeURIComponent(active.map(record=>record.operationId).join(',')));
          for(const item of result.items || []) {
            const record=jobs.get(item.operation_id);
            if(!record) continue;
            if(item.job) await accept(record,item.job);
            else record.error=item.available ? 'Приём пока не подтверждён или срок хранения истёк.' : 'Хранилище заданий недоступно.';
          }
          render();
        }
        delay=Math.min(10000,Math.round(delay*1.4));
      }
    } catch(error) {if(error.code !== 'aborted') announce(error.message);}
    finally {
      polling=false;
      timer=setTimeout(tick,delay);
    }
  }

  async function run(command,{onTerminal,confirmationNote=''}={}) {
    const key=scope(command);
    if(startingScopes.has(key)) return null;
    startingScopes.add(key);
    try {return await startRun(command,{onTerminal,confirmationNote});}
    finally {startingScopes.delete(key);}
  }

  async function startRun(command,{onTerminal,confirmationNote=''}={}) {
    const existing=[...jobs.values()].find(record=>scope(record)===scope(command) && !terminal.has(record.snapshot?.state));
    if(existing) {await pollRecord(existing); announce('Эта операция уже отслеживается.'); return existing;}
    const preview=await api.adminRequest('/api/admin/daily/jobs/preview',{method:'POST',body:command});
    if(preview.blocked_reason) {
      const error=new api.AdminError('conflict',{status:409,data:preview});
      if(preview.blocked_reason === 'incomplete_sibling') error.message='Для изменения одной части сначала нужна полная соседняя часть. Подготовьте обе части вместе; черновики сохранены.';
      throw error;
    }
    const plan=preview.effective_policy;
    const selection=command.difficulty ? `сложность: ${command.difficulty}; ` : command.kind.startsWith('trivia.') ? `часть: ${command.part || 'all'}; ` : '';
    const message=`${command.date} · ${command.kind}; ${selection}вариантов: ${preview.readiness?.total ?? preview.work_items}; работа: ${preview.work_items}; модели: ${(plan?.models || []).join(' → ')}; режим: ${command.mode || 'missing'}. `+
      (command.mode === 'replace' ? 'Существующее содержимое будет заменено. ' : 'Готовое содержимое сохраняется. ')+
      (plan?.explicit ? 'Выбранная модель закреплена для операции.' : 'Auto будет закреплён при старте и может измениться до него.')+
      (confirmationNote ? ` ${confirmationNote}` : '');
    if(root.showConfirm && !await root.showConfirm('Подтвердить подготовку',message)) return null;
    const operationId=api.newOperationId();
    const record={operationId,jobId:null,kind:command.kind,date:command.date,difficulty:command.difficulty,part:command.part};
    jobs.set(operationId,record); save(); render();
    try {
      const accepted=await api.adminRequest('/api/admin/daily/jobs',{method:'POST',body:command,operationId});
      // A conflict alias may point to another command (including a different
      // Trivia section). Only confirmed fresh admission owns submitted drafts.
      // Unknown admissions are reconciled as external updates until compared.
      if(accepted.state === 'accepted' && onTerminal) callbacks.set(operationId,onTerminal);
      record.jobId=accepted.job_id;
      if(record.snapshot?.task_id !== accepted.job_id) record.snapshot={state:'queued',storage_mode:accepted.storage_mode};
      announce(accepted.state === 'existing' ? `Уже выполняется задание ${accepted.job_id}. Новая правка не принята; черновик сохранён.` : `Задание принято: ${accepted.job_id}`); save(); render();
      if(terminal.has(record.snapshot?.state)) {
        try {await accept(record,record.snapshot);} catch(error) {announce(`Состояние задания подтверждено; редактор пока не обновлён: ${error.message}`);}
      } else await pollRecord(record);
    } catch(error) {
      if(!error.outcomeUnknown) {jobs.delete(operationId); callbacks.delete(operationId); save(); render(); throw error;}
      record.jobId=null; record.error=error.message; save(); render();
      await pollRecord(record);
    }
    delay=2000; clearTimeout(timer); timer=setTimeout(tick,delay);
    return record;
  }

  async function calendar() {
    const target=document.getElementById('daily-readiness-calendar');
    if(!target || calendarBusy) return;
    calendarBusy=true;
    try {
      const data=await api.adminRequest('/api/admin/daily/readiness?days=8');
      root.DailyChrome?.updateCalendar(data);
      target.replaceChildren();
      for(const day of data.days || []) {
        const row=node('div'); row.className='info-card'; row.append(node('strong',day.date));
        const croc=day.croc;
        const details=(croc?.puzzles || []).map(p=>`${p.difficulty}: ${p.fully_ready ? 'готово' : p.hints_ready ? 'текст готов, арт ожидается' : 'не готово'}`);
        if(croc?.available === false) details.push('Croc: недоступно');
        details.push(`2048: ${day.daily2048?.available === false ? 'недоступно' : day.daily2048?.ready ? 'готово' : 'не готово'}`);
        details.push(`Trivia: ${day.trivia?.available === false ? 'недоступно' : day.trivia?.ready ? 'готово' : 'не готово'}`);
        details.push(`Tarot: ${day.tarot?.available === false ? 'недоступно' : `${day.tarot?.ready || 0}/${day.tarot?.total || 156}`}`);
        row.append(node('p',details.join(' · ')));
        for(const [kind,label] of [['croc.prepare_day','Подготовить Croc'],['tarot.prepare','Дополнить Tarot']]) {
          const button=node('button',label); button.type='button';
          button.addEventListener('click',async()=>{button.disabled=true; try {await run({kind,date:day.date,mode:'missing'});} catch(error){announce(error.message);} finally{button.disabled=false;}});
          row.append(button);
        }
        if(croc?.job?.id || croc?.job?.task_id) row.append(node('p',`Croc job: ${croc.job.task_id || croc.job.id} · ${croc.job.state}`));
        target.append(row);
      }
      if(data.today) root.dailyBusinessToday=data.today;
    } catch(error) {if(error.code !== 'aborted') target.replaceChildren(node('p',error.message));}
    finally {calendarBusy=false;}
  }

  function init() {
    if(!api) return;
    try {
      for(const saved of JSON.parse(sessionStorage.getItem(storageKey)||'[]').slice(-32)) {
        if(typeof saved.operationId === 'string' && typeof saved.kind === 'string' && typeof saved.date === 'string') jobs.set(saved.operationId,saved);
      }
    } catch(_) { /* Ignore malformed browser-local metadata. */ }
    render(); calendar(); tick();
    document.getElementById('daily-readiness-refresh')?.addEventListener('click',calendar);
    document.addEventListener('visibilitychange',()=>{if(!document.hidden) {delay=2000;tick();}});
    root.addEventListener('online',()=>{delay=2000;tick();});
    root.addEventListener('pagehide',()=>clearTimeout(timer));
  }
  root.DailyJobs={run,calendar};
  if(document.readyState === 'loading') document.addEventListener('DOMContentLoaded',init,{once:true}); else init();
})(globalThis);
