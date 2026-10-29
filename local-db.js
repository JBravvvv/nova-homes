/* Stand-in for claude.ai's db capability on the self-hosted site: listings come from homes.json,
   Josh's status/notes live in this phone's localStorage (key nh-user), keyed by home id. */
(function(){
  const KEY='nh-user';
  const load=()=>{try{return JSON.parse(localStorage.getItem(KEY)||'{}');}catch(e){return {};}};
  let user=load(), homes=[], subs=[];
  const merged=()=>homes.map(h=>{const u=user[h.id]||{};return Object.assign({},h,u);});
  const emit=()=>{const docs=merged().map(h=>({id:h.id,exists:true,data:()=>{const {id,...r}=h;return r;}}));subs.forEach(s=>s.next({docs}));};
  const ready=fetch('homes.json',{cache:'no-cache'}).then(r=>r.json()).then(list=>{
    homes=list;
    if(!localStorage.getItem(KEY)){homes.forEach(h=>{if(h.status)user[h.id]={status:h.status};});try{localStorage.setItem(KEY,JSON.stringify(user));}catch(e){}}
  });
  const db={
    collection:()=>({onSnapshot(next,err){const s={next};subs.push(s);ready.then(emit,e=>err&&err(e));return()=>{subs=subs.filter(x=>x!==s);};}}),
    doc:path=>({async update(fields){const id=path.split('/')[1];user[id]=Object.assign({},user[id],fields);
      localStorage.setItem(KEY,JSON.stringify(user));setTimeout(emit,0);}})
  };
  window.claude={use:async n=>n==='db'?db:null};
  if('serviceWorker' in navigator)addEventListener('load',()=>navigator.serviceWorker.register('sw.js').catch(()=>{}));
})();
