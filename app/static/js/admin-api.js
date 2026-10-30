/* Shared bounded admin transport. Mutations are never retried automatically. */
(function (root) {
  'use strict';
  const codes = {401:'session_expired',403:'denied',409:'conflict',429:'temporary_unavailable',503:'temporary_unavailable'};
  const messages = {session_expired:'Сессия истекла. Войдите снова.', denied:'Доступ запрещён. Обновите страницу.',
    conflict:'Данные изменились. Сравните с актуальной версией.', temporary_unavailable:'Временно недоступно. Повторите позже.',
    timeout:'Время ожидания истекло.', aborted:'Загрузка отменена.', network:'Соединение потеряно.', invalid_response:'Ответ сервера не распознан.'};

  class AdminError extends Error {
    constructor(code, {status=0, outcomeUnknown=false, data=null, retryAfter=null}={}) {
      super(outcomeUnknown ? 'Результат пока неизвестен. Проверьте состояние операции перед повтором.' : messages[code] || 'Ошибка запроса.');
      this.name = code === 'aborted' ? 'AbortError' : 'AdminError';
      Object.assign(this,{code,status,outcomeUnknown,data,retryAfter});
    }
  }

  async function boundedFetch(url, options, consume) {
    const {timeoutMs=10000,signal,...init} = options;
    const mutation = !['GET','HEAD'].includes((init.method || 'GET').toUpperCase());
    const controller = new AbortController();
    const headers = new Headers(init.headers || {});
    const csrf = typeof document !== 'undefined' ? document.querySelector('meta[name="csrf-token"]')?.content : '';
    if (mutation && csrf && !headers.has('X-CSRF-Token')) headers.set('X-CSRF-Token',csrf);
    let timer, abortListener;
    const interrupted = new Promise((_,reject) => {
      const stop = code => {controller.abort(); reject(new AdminError(code,{outcomeUnknown:mutation}));};
      timer = setTimeout(() => stop('timeout'),timeoutMs);
      abortListener = () => stop('aborted');
      if (signal?.aborted) abortListener();
      else signal?.addEventListener('abort',abortListener,{once:true});
    });
    try {
      if (signal?.aborted) return await interrupted;
      const work = Promise.resolve().then(() => root.fetch(url,{credentials:'same-origin',...init,headers,signal:controller.signal})).then(consume);
      return await Promise.race([work,interrupted]);
    } catch (error) {
      if (error instanceof AdminError) throw error;
      throw new AdminError('network',{outcomeUnknown:mutation});
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener('abort',abortListener);
    }
  }

  function adminFetch(url, options={}) {
    // Buffer the bounded admin response while the deadline still owns its body.
    return boundedFetch(url, options, async response => {
      const bytes = await response.arrayBuffer();
      return new Response([204,205,304].includes(response.status) ? null : bytes,
        {status:response.status,statusText:response.statusText,headers:response.headers});
    });
  }

  function mutationOutcomeUnknown(method, status, data, invalidResponse=false) {
    if (['GET','HEAD'].includes((method || 'GET').toUpperCase())) return false;
    const admission = data?.admission || data;
    if (data?.outcome_unknown === true || admission?.state === 'unknown' || admission?.reason_code === 'admission_unconfirmed') return true;
    if (admission?.state === 'rejected') return false;
    // A server/proxy failure can arrive after the write committed. Reconcile
    // by operation ID or revision before allowing the caller to try again.
    return status >= 500 || (invalidResponse && status >= 200 && status < 300);
  }

  async function adminRequest(url, {method='GET',body,signal,timeoutMs=10000,operationId,headers={}}={}) {
    const init = {method,signal,timeoutMs,headers:new Headers(headers)};
    init.headers.set('Accept','application/json');
    if (body !== undefined) {
      init.headers.set('Content-Type','application/json');
      init.body = JSON.stringify(operationId ? {...body,operation_id:operationId} : body);
    }
    return boundedFetch(url,init,async response => {
      let data;
      try { data = await response.json(); }
      catch (_) { throw new AdminError(codes[response.status] || 'invalid_response',{status:response.status,outcomeUnknown:mutationOutcomeUnknown(method,response.status,null,true)}); }
      if (!response.ok) throw new AdminError(codes[response.status] || 'request_failed',{
        status:response.status,data,retryAfter:response.headers.get('Retry-After'),
        outcomeUnknown:mutationOutcomeUnknown(method,response.status,data)
      });
      return data;
    });
  }

  function createLatestRequest() {
    let controller=null, sequence=0;
    return {
      async request(url,options={}) {
        controller?.abort(); controller=new AbortController();
        const version=++sequence;
        const value=await adminRequest(url,{...options,signal:controller.signal});
        if (version !== sequence) throw new AdminError('aborted');
        return value;
      },
      abort() {sequence+=1; controller?.abort();}
    };
  }

  function kyivDate(value=new Date()) {
    const parts = new Intl.DateTimeFormat('en-CA',{timeZone:'Europe/Kyiv',year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(value);
    const field = name => parts.find(part => part.type === name).value;
    return `${field('year')}-${field('month')}-${field('day')}`;
  }

  function newOperationId() {
    if(root.crypto.randomUUID) return root.crypto.randomUUID();
    const bytes=root.crypto.getRandomValues(new Uint8Array(16));
    bytes[6]=(bytes[6]&15)|64; bytes[8]=(bytes[8]&63)|128;
    const hex=[...bytes].map(value=>value.toString(16).padStart(2,'0')).join('');
    return `${hex.slice(0,8)}-${hex.slice(8,12)}-${hex.slice(12,16)}-${hex.slice(16,20)}-${hex.slice(20)}`;
  }

  const api={adminRequest,adminFetch,mutationOutcomeUnknown,createLatestRequest,kyivDate,newOperationId,AdminError};
  root.AdminApi=api;
  if (typeof module !== 'undefined' && module.exports) module.exports=api;
})(globalThis);
