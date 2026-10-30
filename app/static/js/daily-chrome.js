/* Shared Daily page behavior. Drafts remain in their owning editors. */
(function (root) {
  'use strict';
  const bootstrap = JSON.parse(document.getElementById('daily-bootstrap').textContent);
  const today = () => bootstrap.today || root.AdminApi.kyivDate();
  function updateCalendar(data) {
    if(data?.timezone==='Europe/Kyiv' && /^\d{4}-\d{2}-\d{2}$/.test(data.today)) bootstrap.today=data.today;
  }
  async function fetch(url,options={}) {
    const response=await root.AdminApi.adminFetch(url,options);
    const codes={401:'session_expired',403:'denied',429:'temporary_unavailable',503:'temporary_unavailable'};
    if(codes[response.status] || response.status >= 500) {
      const data=await response.json().catch(()=>null);
      const error=new root.AdminApi.AdminError(codes[response.status] || 'temporary_unavailable',{status:response.status,data,retryAfter:response.headers.get('Retry-After'),
        outcomeUnknown:root.AdminApi.mutationOutcomeUnknown(options.method,response.status,data)});
      if(error.retryAfter && [429,503].includes(response.status)) error.message+=` Повторите через ${error.retryAfter.slice(0,100)} с.`;
      throw error;
    }
    return response;
  }
  function shiftDate(day, amount) {
    const value = new Date(`${day}T12:00:00Z`);
    value.setUTCDate(value.getUTCDate() + amount);
    return value.toISOString().slice(0,10);
  }
  let closeDialog = null;
  function confirm(title, message) {
    closeDialog?.(false);
    return new Promise(resolve => {
      const modal = document.getElementById('confirm-modal');
      const accept = document.getElementById('modal-confirm'), cancel = document.getElementById('modal-cancel');
      const previous = document.activeElement;
      const outside = [...document.body.children].filter(node => node !== modal && !['SCRIPT','LINK'].includes(node.tagName));
      const inert = outside.map(node => node.inert);
      outside.forEach(node => {node.inert=true;});
      document.getElementById('modal-title').textContent=title;
      document.getElementById('modal-message').textContent=message;
      modal.style.display='flex';
      function keydown(event) {
        if(event.key==='Escape') {event.preventDefault();finish(false);}
        if(event.key==='Tab') {
          event.preventDefault();
          (document.activeElement === cancel ? accept : cancel).focus();
        }
      }
      function finish(value) {
        modal.style.display='none';
        modal.removeEventListener('keydown',keydown);
        accept.onclick=cancel.onclick=null;
        outside.forEach((node,index) => {node.inert=inert[index];});
        closeDialog=null;
        if(previous?.isConnected) previous.focus({preventScroll:true});
        resolve(value);
      }
      closeDialog=finish;
      modal.addEventListener('keydown',keydown);
      accept.onclick=()=>finish(true); cancel.onclick=()=>finish(false);
      cancel.focus();
    });
  }
  function enhanceLabels() {
    document.querySelectorAll('label:not([for])').forEach(label => {
      const control=label.querySelector('input,select,textarea') || label.nextElementSibling;
      if(control?.matches('input,select,textarea') && control.id) label.htmlFor=control.id;
    });
    document.querySelectorAll('input:not([type=hidden]),select,textarea').forEach((control,index) => {
      if(control.labels?.length || control.getAttribute('aria-label') || control.getAttribute('aria-labelledby')) return;
      const board=control.closest('#s2048-editor-board');
      const field=control.closest('.field');
      const text=board ? `Клетка ${Number(control.dataset.y)+1}, ${Number(control.dataset.x)+1}`
        : control.placeholder || field?.querySelector('label')?.textContent || control.title;
      // Older inline filters have no visible label; retain a concrete name.
      const names={'sub-search':'Поиск подписчика','sub-status-filter':'Статус подписки','sub-game-filter':'Игра подписки',
        'offer-status-filter':'Статус предложения','offer-game-filter':'Игра предложения','offer-limit':'Количество предложений'};
      control.setAttribute('aria-label',text || names[control.id] || `Поле ${control.id || index+1}`);
    });
    document.querySelectorAll('button').forEach(button => {
      button.setAttribute('aria-busy',String(button.disabled && /⏳|Сохраня|Отправ|Подгот|Провер|Загруз/.test(button.textContent)));
    });
  }
  function layout() {
    const header=document.querySelector('.header'), tabs=document.querySelector('.tab-nav');
    document.documentElement.style.setProperty('--daily-header-height',`${header.offsetHeight}px`);
    document.documentElement.style.setProperty('--daily-sticky-height',`${header.offsetHeight+tabs.offsetHeight+12}px`);
  }
  function init(activate) {
    const tabs=[...document.querySelectorAll('.tab-btn')];
    document.querySelector('.tab-nav').setAttribute('role','tablist');
    document.querySelector('.tab-nav').setAttribute('aria-label','Разделы Daily');
    tabs.forEach(button => {
      button.id=`daily-tab-${button.dataset.tab}`;
      button.setAttribute('role','tab');
      button.setAttribute('aria-controls',`tab-${button.dataset.tab}`);
      const panel=document.getElementById(`tab-${button.dataset.tab}`);
      panel.setAttribute('role','tabpanel'); panel.setAttribute('aria-labelledby',button.id);
      button.addEventListener('keydown',async event => {
        const index=tabs.indexOf(button);
        const next=event.key==='Home'?0:event.key==='End'?tabs.length-1:event.key==='ArrowRight'?(index+1)%tabs.length:event.key==='ArrowLeft'?(index+tabs.length-1)%tabs.length:null;
        if(next===null) return;
        event.preventDefault();
        if(await activate(tabs[next].dataset.tab)!==false) tabs[next].focus();
      });
    });
    const observer=new MutationObserver(()=>{enhanceLabels();});
    // Labels are enhanced after editor rendering, without replacing controls.
    observer.observe(document.body,{childList:true,subtree:true});
    new ResizeObserver(layout).observe(document.querySelector('.header'));
    new ResizeObserver(layout).observe(document.querySelector('.tab-nav'));
    enhanceLabels(); layout();
  }
  root.DailyChrome={today,shiftDate,confirm,init,enhanceLabels,fetch,updateCalendar};
})(globalThis);
