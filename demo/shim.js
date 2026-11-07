/* TAT SMART demo: a fake house bridge inside the page, so the whole app runs from one link. */
(function(){
const DKEY="tat-demo-v1";
const store={get(k){try{return JSON.parse(localStorage.getItem(DKEY+k));}catch(e){return null;}},set(k,v){try{localStorage.setItem(DKEY+k,JSON.stringify(v));}catch(e){}}};
const SEED=__SEED__;
let DB=store.get("db")||SEED.db;
let SCH=store.get("sch")||SEED.sch;
const TUYA=store.get("tuya")||SEED.tuya;
const saveDb=()=>store.set("db",DB),saveSch=()=>store.set("sch",SCH),saveTuya=()=>store.set("tuya",TUYA);
// the app also keeps its own copy under this key; start it from the demo house
try{if(!localStorage.getItem("knx-plan-v1"))localStorage.setItem("knx-plan-v1",JSON.stringify(DB));}catch(e){}
const cur=()=>DB.projects.find(p=>p.id===DB.current)||DB.projects[0];
const json=(o,s)=>Promise.resolve(new Response(JSON.stringify(o),{status:s||200,headers:{"Content-Type":"application/json"}}));
const realFetch=window.fetch.bind(window);
let sock=null;
const emit=m=>{if(sock&&sock.onmessage)setTimeout(()=>sock.onmessage({data:JSON.stringify(m)}),0);};
// KNX 2-byte float
function f16enc(v){let m=Math.round(v*100),e=0;while(m<-2048||m>2047){m=Math.round(m/2);e++;}const neg=m<0;let mm=m&0x7ff;const raw=(neg?0x8000:0)|(e<<11)|mm;return [raw>>8,raw&255];}
function tg(ga,dpt,v){if(!ga)return;const main=String(dpt).split(".")[0];
 if(main==="1")emit({t:"tg",ga,bit:v?1:0});else if(main==="5")emit({t:"tg",ga,bytes:[Math.round(v*255/100)]});else if(main==="9")emit({t:"tg",ga,bytes:f16enc(+v)});}
function devByGa(ga){for(const d of cur().devices){const g=d.ga||{};for(const k in g)if(g[k]===ga)return [d,k];}return [null,null];}
function knxWrite(m){
 const [d,k]=devByGa(m.ga);if(!d)return;const g=d.ga;
 setTimeout(()=>{
  if(d.type==="switch"&&k==="cmd")tg(g.status,"1.001",m.value);
  if(d.type==="dimmer"){if(k==="cmd"){tg(g.status,"1.001",m.value);tg(g.valueStatus,"5.001",m.value?(d.st&&d.st.level)||100:0);}if(k==="value"){tg(g.valueStatus,"5.001",m.value);tg(g.status,"1.001",m.value>0);}}
  if(d.type==="blind"){if(k==="updown")tg(g.positionStatus,"5.001",m.value?100:0);if(k==="position")tg(g.positionStatus,"5.001",m.value);}
 },250);
}
function tuyaCmd(m){const x=TUYA.find(t=>t.id===m.id);if(!x)return;const cmds=m.commands||[{code:m.code,value:m.value}];
 cmds.forEach(c=>{x.status[c.code]=c.value;});
 if(x.status.switch!=null&&"cur_power" in x.status)x.status.cur_power=x.status.switch?11850:0;
 saveTuya();setTimeout(()=>emit({t:"tuya",devices:[x]}),300);}
function setDev(d,on,lv){const g=d.ga||{};d.st=d.st||{};
 if(d.type==="tuya"){tuyaCmd({id:d.tuyaId,commands:[{code:d.code||"switch",value:!!on}].concat(lv!=null&&d.dimCode?[{code:d.dimCode,value:Math.round((d.dimMin||10)+(lv/100)*((d.dimMax||1000)-(d.dimMin||10)))}]:[])});return;}
 if(d.type==="switch")tg(g.cmd,"1.001",on);
 if(d.type==="dimmer"){if(lv!=null&&on)tg(g.valueStatus,"5.001",lv);tg(g.status,"1.001",on);if(!on)tg(g.valueStatus,"5.001",0);}
 if(d.type==="hvac")tg(g.cmd,"1.001",on);
}
function runActions(acts){acts.forEach(a=>{const d=cur().devices.find(x=>x.id===a.id);if(!d)return;
 if(d.type==="blind"&&a.position!=null){tg(d.ga.positionStatus,"5.001",a.position);return;}
 if(d.type==="hvac"&&a.setpoint!=null)tg(d.ga.setpoint,"9.001",a.setpoint);
 setDev(d,a.on,a.level);});return acts.length;}
const sun={sunrise:"06:19",sunset:"16:58"};
// ---- sound: two DSPPA amplifiers, songs, radio directory (all pretend)
const AMPS=store.get("amps")||[
 {id:"a1",name:"الصالة",room:"الصالة",devname:"DW0250 Salon",online:true,playing:true,state:"play",vol:32,mute:false,mode:"20",source:"واي فاي",title:"Fairuz Radio",artist:"فيروز"},
 {id:"a2",name:"المطبخ",room:"المطبخ",devname:"DW0250 Kitchen",online:true,playing:false,state:"pause",vol:20,mute:false,mode:"20",source:"واي فاي",title:"",artist:""}];
const MUSIC=store.get("music")||{songs:[{file:"fairuz-sabah.mp3",name:"فيروز - صباح ومسا",size:5400000,t:1},{file:"kadim.mp3",name:"كاظم الساهر - زيديني عشقاً",size:7900000,t:2}],stations:[{name:"Fairuz Radio",url:"http://demo/fairuz"}]};
const RADIO=[{name:"Fairuz Radio",url:"http://demo/fairuz",country:"Lebanon",tags:"fairuz",kbps:128},{name:"إذاعة القرآن الكريم",url:"http://demo/quran",country:"Saudi Arabia",tags:"quran",kbps:64},
 {name:"راديو دجلة",url:"http://demo/dijla",country:"Iraq",tags:"iraq",kbps:128},{name:"Umm Kulthum Radio",url:"http://demo/omk",country:"Egypt",tags:"tarab",kbps:128},{name:"MBC FM",url:"http://demo/mbc",country:"Saudi Arabia",tags:"arabic",kbps:96},{name:"Spacetoon Kids",url:"http://demo/kids",country:"UAE",tags:"kids",kbps:64}];
const saveAu=()=>{store.set("amps",AMPS);store.set("music",MUSIC);};
const pushAu=()=>{saveAu();emit({t:"audio",zones:AMPS});};
function audioApi(path,method,body,url){
 if(path==="api/audio")return json({zones:AMPS});
 if(path==="api/audio/cmd"){const z=AMPS.find(a=>a.id===body.id);if(z){const c=body.cmd,v=body.value;
   if(c==="toggle"){z.playing=!z.playing;}if(c==="play")z.playing=true;if(c==="pause")z.playing=false;if(c==="vol")z.vol=+v;if(c==="mute")z.mute=!!v;
   if(c==="src"){z.mode=v==="bt"?"41":v==="aux"?"40":"10";z.source=v==="bt"?"بلوتوث":v==="aux"?"AUX":"واي فاي";z.title=v==="bt"?"من التلفون":v==="aux"?"AUX":z.title;}
   if(c==="song"){const m=MUSIC.songs.find(x=>x.file===v);z.title=m?m.name:v;z.artist="";z.playing=true;z.mode="10";z.source="واي فاي";}
   if(c==="url"){const st=RADIO.concat(MUSIC.stations).find(x=>x.url===v);z.title=st?st.name:"راديو";z.artist="";z.playing=true;z.mode="10";z.source="واي فاي";}
   if(c==="preset"){z.title="مفضلة "+v;z.playing=true;}
   if(c==="next"||c==="prev")z.title=z.title+" ›";
   z.state=z.playing?"play":"pause";setTimeout(pushAu,200);}
  return json({ok:true});}
 if(path==="api/music"&&method==="GET")return json(MUSIC);
 if(path==="api/music/part"){const q=new URL(url,location.href).searchParams;if(+q.get("i")<+q.get("n")-1)return json({ok:true});
  const n=q.get("name")||"song.mp3";MUSIC.songs.unshift({file:n,name:n.replace(/\.[^.]+$/,""),size:4000000,t:Date.now()});saveAu();return json({ok:true,added:1,songs:MUSIC.songs});}
 if(path.startsWith("api/music/")&&method==="DELETE"){const f=decodeURIComponent(path.slice(10));MUSIC.songs=MUSIC.songs.filter(x=>x.file!==f);saveAu();return json({ok:true,songs:MUSIC.songs});}
 if(path==="api/music/stations"){MUSIC.stations=body.stations||[];saveAu();return json({ok:true,stations:MUSIC.stations});}
 if(path==="api/radio/search"){const q=new URL(url,location.href).searchParams;const k=(q.get("q")||q.get("tag")||q.get("cc")||"").toLowerCase();
  const map={fairuz:"fairuz",kalthoum:"tarab",quran:"quran",iq:"iraq",tarab:"tarab",arabic:"arabic",kids:"kids"};const t=map[k]||k;
  let r=RADIO.filter(x=>x.tags===t||x.name.toLowerCase().includes(k)||x.country.toLowerCase().includes(k));if(!r.length)r=RADIO;
  return new Promise(res=>setTimeout(()=>res(json({ok:true,stations:r})),500));}
 return null;}
window.fetch=function(u,o){
 const url=typeof u==="string"?u:u.url;const path=url.replace(/^\.?\//,"").split("?")[0];
 if(!path.startsWith("api/"))return realFetch(u,o);
 const method=((o&&o.method)||"GET").toUpperCase();let body={};try{body=o&&typeof o.body==="string"?JSON.parse(o.body):{};}catch(e){}
 const AU=audioApi(path,method,body,url);if(AU)return AU;
 switch(path){
  case "api/role":return json({installer:true,locked:false});
  case "api/project":if(method==="PUT"){DB=body;saveDb();return json({ok:true});}return json(DB);
  case "api/schedules":if(method==="PUT"){SCH={schedules:body.schedules||[],holiday:body.holiday||SCH.holiday};saveSch();return json({ok:true});}return json(Object.assign({sun},SCH));
  case "api/tuya/devices":return json({ok:true,devices:TUYA.slice().sort((a,b)=>a.name.localeCompare(b.name))});
  case "api/tuya/spec":return json({ok:true,spec:{}});
  case "api/scene":{const sc=(cur().scenes||[]).find(s=>s.id===body.id);if(!sc)return json({ok:false},404);return json({ok:true,count:runActions(sc.actions||[])});}
  case "api/scenes":cur().scenes=body.scenes||[];saveDb();return json({ok:true});
  case "api/owner":{const p=DB.projects.find(x=>x.id===body.project);if(p)p.devices.forEach(d=>{const c=(body.devices||{})[d.id];if(c){if(c.name)d.name=c.name;d.fav=!!c.fav;if(Number.isInteger(c.ord))d.ord=c.ord;}});if(p&&body.home)p.home=body.home;saveDb();return json({ok:true});}
  case "api/alloff":cur().devices.forEach(d=>{if(d.type!=="scene"&&d.type!=="blind")setDev(d,false);});return json({ok:true});
  case "api/installer":case "api/installer/logout":return json({ok:true});
  case "api/bridge":return json({house:"tat-demo",tuya:{ok:true,count:TUYA.length},assistant:"local"});
  case "api/assistant":return json({ok:true,reply:"المساعد مخفي بهاي النسخة."});
 }
 return json({ok:false},404);
};
class FakeWS{constructor(){this.readyState=0;sock=this;setTimeout(()=>{this.readyState=1;this.onopen&&this.onopen();
  emit({t:"status",state:"connected"});emit({t:"tuya",devices:TUYA});emit({t:"audio",zones:AMPS});
  // room temperature drifts so the AC card looks alive
  setInterval(()=>{const h=cur().devices.filter(d=>d.type==="hvac");h.forEach(d=>{const st=d.st||{};const tgt=st.on?st.setpoint||23:29;const t=(st.temp||27)+Math.sign(tgt-(st.temp||27))*0.1;tg(d.ga.temp,"9.001",Math.round(t*10)/10);});},8000);
 },120);}
 send(s){const m=JSON.parse(s);if(m.t==="write")knxWrite(m);if(m.t==="tuya")tuyaCmd(m);}
 close(){}
}
FakeWS.OPEN=1;
window.WebSocket=FakeWS;
window.TAT_DEMO_RESET=()=>{try{Object.keys(localStorage).filter(k=>k.startsWith(DKEY)||k==="knx-plan-v1").forEach(k=>localStorage.removeItem(k));}catch(e){}location.reload();};
})();
