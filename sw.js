// Network-first for the page and data so updates show up; cache-first for photos and icons.
const C='nova-homes-v2';
self.addEventListener('install',e=>self.skipWaiting());
self.addEventListener('activate',e=>e.waitUntil(self.clients.claim()));
self.addEventListener('fetch',e=>{
  const u=new URL(e.request.url);
  if(e.request.method!=='GET'||u.origin!==location.origin)return;
  const net=()=>fetch(e.request).then(r=>{const c=r.clone();caches.open(C).then(x=>x.put(e.request,c));return r;});
  if(/\/photos\/|\.png$/.test(u.pathname))e.respondWith(caches.match(e.request).then(r=>r||net()));
  else e.respondWith(net().catch(()=>caches.match(e.request)));
});
