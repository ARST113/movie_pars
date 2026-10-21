import {FIELDS,text,parseTitles,prepare,findRows,counts,frequencies,seasonSort} from './engine.mjs';

const $ = id => document.getElementById(id);
const formatter=new Intl.NumberFormat('ru-RU');
const collator=new Intl.Collator(['ru','en'],{numeric:true,sensitivity:'base'});
const COLS = FIELDS.map(([name])=>name);
const HEADERS=FIELDS.map(([,label])=>label);
const state={ rows:[], visible:[], ready:false,selected:new Set(),page:0,pageSize:50,sortField:'anime_title',sortAsc:true,
  meta:{},loadedName:'',db:null };
const REFRESH_DEBOUNCE=220; let refreshTimeout;

function notify(message,level='info',hide=false){const el=$('notice');el.textContent=message;el.className='notice'+(level==='err'?' err':level==='warn'?' warn':'');el.hidden=hide;}
function showStatus(value){$('databaseStatus').textContent=value;}
function options(id,values,defaultOption){const el=$(id);el.textContent='';const all=document.createElement('option');all.value='';all.textContent=defaultOption;el.append(all);
  for(const value of values){const op=document.createElement('option');op.value=value;op.textContent=value;el.append(op)} }
function distinct(field){return [...new Set(state.rows.map(r=>r[field]).filter(Boolean))];}
function fillFacetControls(){options('seasonFilter',distinct('season').sort((a,b)=>{const s1=seasonSort(a),s2=seasonSort(b);return s1!==null&&s2!==null?s2-s1:collator.compare(a,b)}),'Все сезоны');
  options('animeTypeFilter',distinct('anime_type').sort(collator.compare),'Все типы аниме');
  options('songTypeFilter',distinct('song_type').sort(collator.compare),'Все типы песен');}
function readFilters(){return {
  all:$('globalSearch').value, bulkAnime:$('bulkAnime').value, emptyMode:$('animeBlank').value,
  season:$('seasonFilter').value,animeType:$('animeTypeFilter').value,songType:$('songTypeFilter').value,
  artist:$('artistSearch').value,song:$('songSearch').value,
  exact:document.querySelector('input[name="titleMatch"]:checked')?.value==='exact'
};}
function scheduleRefresh(){window.clearTimeout(refreshTimeout);refreshTimeout=window.setTimeout(()=>refresh(true),REFRESH_DEBOUNCE)}
function compareRows(a,b,field){let left=a[field],right=b[field];if(field==='season'){const al=seasonSort(left),br=seasonSort(right);if(al!==null && br!==null)return al-br}return collator.compare(left,right)}
function sortVisible(){const {sortField,sortAsc}=state;state.visible.sort((a,b)=>{const cmp=compareRows(a,b,sortField);return sortAsc?cmp:-cmp});}
function refresh(resetPage=false){if(!state.ready)return;
  state.visible=findRows(state.rows,readFilters());sortVisible();if(resetPage)state.page=0;
  const num=counts(state.visible);
  $('songsCount').textContent=formatter.format(num.songs);$('animeCount').textContent=formatter.format(num.anime);$('artistsCount').textContent=formatter.format(num.artists);$('emptyCount').textContent=formatter.format(num.empty);
  $('resultSummary').textContent=`${formatter.format(num.songs)} записей из ${formatter.format(state.rows.length)} · ${formatter.format(parseTitles($('bulkAnime').value).length)} названий в списке`;
  $('titleInputCount').textContent=`${formatter.format(parseTitles($('bulkAnime').value).length)} названий`;
  $('exportCSV').disabled = num.songs===0;
  render();}
function cell(content,className){const td=document.createElement('td');if(className)td.className=className;
  if(content instanceof Node)td.appendChild(content);else td.textContent=content===undefined?'':content;return td;}
function textNode(value,className=''){const span=document.createElement('span');span.className=className;span.textContent=value||'—';return span;}
function render(){const total=state.visible.length;const pages=Math.max(1,Math.ceil(total/state.pageSize));state.page=Math.max(0,Math.min(state.page,pages-1));const offset=state.page*state.pageSize;const current=state.visible.slice(offset,offset+state.pageSize);
  const body=$('rows');body.replaceChildren();if(!current.length){const tr=document.createElement('tr');const td=cell('По выбранным фильтрам ничего не найдено. Попробуйте изменить запрос.','placeholder');td.colSpan=7;tr.append(td);body.append(tr)}
  const fragment=document.createDocumentFragment();
  for(const r of current){const tr=document.createElement('tr');const select=cell('','checkbox-cell');const check=document.createElement('input');check.type='checkbox';check.value=r.id;check.checked=state.selected.has(r.id);check.setAttribute('aria-label','Выбрать строку');check.addEventListener('change',()=>{if(check.checked)state.selected.add(r.id);else state.selected.delete(r.id);updateSelection(current)});select.append(check);tr.append(select);
    tr.append(cell(r.season || textNode('нет данных','empty-value')));
    tr.append(cell(r.anime_type ? textNode(r.anime_type,'type-pill') : textNode('не указано','empty-value')));
    const anime=cell('');anime.append(textNode(r.anime_title||'пустое значение',r.anime_title?'anime-title':'empty-value'));
    if(r.anime_english && text(r.anime_english)!==text(r.anime_title)){const hint=document.createElement('div');hint.className='title-secondary';hint.textContent=r.anime_english;anime.append(hint)}tr.append(anime);
    tr.append(cell(r.song_type ? textNode(r.song_type,'song-pill '+text(r.song_type)) : textNode('не указано','empty-value')));
    tr.append(cell(r.song_title || textNode('не указано','empty-value')));
    tr.append(cell(r.artist || textNode('не указан','empty-value')));
    fragment.appendChild(tr);
  }
  body.appendChild(fragment);
  $('pageRange').textContent=total?`${formatter.format(offset+1)}–${formatter.format(offset+current.length)} из ${formatter.format(total)}`:'0 записей';
  $('pageNumber').textContent=`${state.page+1} / ${pages}`;
  $('pageFirst').disabled=$('pagePrev').disabled=state.page===0;
  $('pageNext').disabled=$('pageLast').disabled=state.page>=pages-1;
  updateSelection(current);
  document.querySelectorAll('th[data-sort]').forEach(th=>{const active=th.dataset.sort===state.sortField;th.querySelector('.sort-sign').textContent=active?(state.sortAsc?'↑':'↓'):'↕'});
}
function updateSelection(current){$('selectedBadge').textContent=formatter.format(state.selected.size);$('exportSelection').disabled=state.selected.size===0;const selectedCount=current.filter(r=>state.selected.has(r.id)).length;$('selectPage').checked=current.length>0 && selectedCount===current.length;$('selectPage').indeterminate=selectedCount>0 && selectedCount<current.length;}

function sanitizeCSV(v){let s=String(v??'');if(/^[\s]*[=+@\-\t\r]/.test(s))s="'"+s;return /[;"\n\r]/.test(s)?'"'+s.replaceAll('"','""')+'"':s;}
function csvString(rows){return '\ufeff'+rows.map(r=>r.map(sanitizeCSV).join(';')).join('\r\n')+'\r\n'}
function download(blob,filename){const url=URL.createObjectURL(blob);const a=document.createElement('a');a.href=url;a.download=filename;document.body.append(a);a.click();a.remove();window.setTimeout(()=>URL.revokeObjectURL(url),30000)}
function downloadCSV(name,rows){download(new Blob([csvString(rows)],{type:'text/csv;charset=utf-8'}),name)}
function exportData(items,format='csv'){
  if(format==='json'){download(new Blob([JSON.stringify(items.map(r=>Object.fromEntries(FIELDS.map(([k,label])=>[label,r[k]]))),null,2)],{type:'application/json;charset=utf-8'}),'anisongdb_results.json');return}
  downloadCSV('anisongdb_results.csv',[HEADERS,...items.map(r=>COLS.map(k=>r[k]))]);
}
function exportColumn(field){const label=FIELDS.find(([key])=>key===field)?.[1];if(!label)return;const stats=frequencies(state.visible,field);downloadCSV(`anisongdb_${field}_frequency.csv`,[[label,'Количество песен'],...stats.map(v=>[v.value,v.count])]);}

// ZIP without a third-party library; produces an uncompressed standards-compliant ZIP archive.
function crc32(bytes){let crc=0xffffffff;for(const b of bytes){crc^=b;for(let i=0;i<8;i++)crc=(crc&1)?(crc>>>1)^0xedb88320:(crc>>>1)}return (crc^0xffffffff)>>>0}
function zipFiles(files){const encoder=new TextEncoder();const parts=[];const directory=[];let offset=0;const now=new Date();const time=(now.getHours()<<11)|(now.getMinutes()<<5)|(now.getSeconds()>>1);const date=((Math.max(1980,now.getFullYear())-1980)<<9)|((now.getMonth()+1)<<5)|now.getDate();
  function u16(d,o,n){d.setUint16(o,n,true)}function u32(d,o,n){d.setUint32(o,n,true)}
  for(const file of files){const filename=encoder.encode(file.name),data=encoder.encode(file.content),crc=crc32(data);
    const local=new Uint8Array(30+filename.length);const h=new DataView(local.buffer);u32(h,0,0x04034b50);u16(h,4,20);u16(h,6,0x0800);u16(h,8,0);u16(h,10,time);u16(h,12,date);u32(h,14,crc);u32(h,18,data.length);u32(h,22,data.length);u16(h,26,filename.length);local.set(filename,30);parts.push(local,data);
    const central=new Uint8Array(46+filename.length);const c=new DataView(central.buffer);u32(c,0,0x02014b50);u16(c,4,20);u16(c,6,20);u16(c,8,0x0800);u16(c,10,0);u16(c,12,time);u16(c,14,date);u32(c,16,crc);u32(c,20,data.length);u32(c,24,data.length);u16(c,28,filename.length);u32(c,42,offset);central.set(filename,46);directory.push(central);offset+=local.length+data.length;}
  const directorySize=directory.reduce((sum,arr)=>sum+arr.length,0);const end=new Uint8Array(22);const e=new DataView(end.buffer);u32(e,0,0x06054b50);u16(e,8,directory.length);u16(e,10,directory.length);u32(e,12,directorySize);u32(e,16,offset);
  return new Blob([...parts,...directory,end],{type:'application/zip'});
}
function downloadAllColumns(){if(!state.visible.length)return;const files=FIELDS.map(([key,label])=>({name:`${key}.csv`,content:csvString([[label,'Количество песен'],...frequencies(state.visible,key).map(item=>[item.value,item.count])])}));download(zipFiles(files),'anisongdb_6_columns.zip');}

function sqlRows(db){const tables=new Set(db.exec("SELECT name FROM sqlite_master WHERE type IN ('table','view')")[0]?.values.map(r=>r[0])||[]);
  let stmt,decode;
  if(tables.has('songsFull')){
    stmt=db.prepare('SELECT * FROM songsFull');
    decode=s=>({season:s[10],anime_type:s[11],anime_title:s[6]||s[7],song_type:({1:'Opening',2:'Ending',3:'Insert'})[s[16]]||s[16],song_title:s[20],artist:s[22],anime_english:s[7],anime_japanese:s[6],anime_alt:String(s[9]||'').replaceAll('\\$',' | '),ann_id:s[0]});
  }else if(tables.has('songs')){
    const names=new Set(db.exec('PRAGMA table_info(songs)')[0]?.values.map(r=>r[1])||[]);
    if(names.has('anime_title')&&names.has('song_title')){
      stmt=db.prepare('SELECT season,anime_type,anime_title,song_type,song_title,artist,anime_english,anime_japanese,anime_alt,ann_id FROM songs');
      decode=s=>({season:s[0],anime_type:s[1],anime_title:s[2],song_type:s[3],song_title:s[4],artist:s[5],anime_english:s[6],anime_japanese:s[7],anime_alt:s[8],ann_id:s[9]});
    }else if(names.has('anime_name')&&names.has('song_name')){
      stmt=db.prepare('SELECT season,anime_type,anime_name,song_type,song_name,artist FROM songs');
      decode=s=>({season:s[0],anime_type:s[1],anime_title:s[2],song_type:s[3],song_title:s[4],artist:s[5],anime_english:'',anime_japanese:'',anime_alt:'',ann_id:''});
    }
  }
  if(!stmt)throw new Error('Неподдерживаемый формат SQLite. Нужна база AniSongDB с таблицей songsFull или подготовленный файл catalog.sqlite.');
  const out=[];let id=0;
  try{while(stmt.step()){
    const v=decode(stmt.get());const row={id:++id};for(const [key,val] of Object.entries(v)){row[key]=val==null?'':String(val).trim()};out.push(prepare(row));
  }}finally{stmt.free()}
  const meta={};if(tables.has('metadata')){for(const [key,value] of (db.exec('SELECT key,value FROM metadata')[0]?.values||[]))meta[key]=value;}
  return {rows:out,meta};
}
async function loadJs(url){return new Promise((resolve,reject)=>{const node=document.createElement('script');node.src=url;node.onload=resolve;node.onerror=()=>reject(new Error(`Не удалось загрузить ${url}`));document.head.append(node)})}
let SQLModule;
async function getSQL(){if(SQLModule)return SQLModule;
  let vendor=true;
  try{await loadJs('./vendor/sql-wasm.js')}
  catch{vendor=false;await loadJs('https://cdn.jsdelivr.net/npm/sql.js@1.13.0/dist/sql-wasm.js')}
  const base=vendor?'./vendor/':'https://cdn.jsdelivr.net/npm/sql.js@1.13.0/dist/';
  SQLModule=await window.initSqlJs({locateFile:file=>base+file});return SQLModule;
}
async function importFile(bytes,name){notify('Открываем SQLite-базу…');showStatus('Читаем записи…');
  await new Promise(resolve=>window.setTimeout(resolve,30));
  const SQL=await getSQL();const db=new SQL.Database(new Uint8Array(bytes));
  let result;
  try{result=sqlRows(db)}catch(e){db.close();throw e}
  if(!result.rows.length){db.close();throw new Error('В базе нет песен')}
  if(state.db)state.db.close();state.db=db;
  state.rows=result.rows;state.meta=result.meta;state.loadedName=name;state.ready=true;state.selected.clear();state.page=0;
  fillFacetControls();refresh(true);
  showStatus(`${formatter.format(state.rows.length)} композиций`);
  if(name.startsWith('catalog.sqlite')&&state.meta.source?.includes('(sample;'))notify('Публичный API был недоступен при сборке. Сейчас используется авторская примерная SQLite-база; для более свежих данных запустите обновление.', 'warn');
  else if(state.meta.source?.includes('demo'))notify('ДЕМОНСТРАЦИОННАЯ база. GitHub Actions автоматически заменит её полным каталогом после публикации. Также можно загрузить свою .db через кнопку вверху.', 'warn');
  else notify(`База загружена: ${formatter.format(state.rows.length)} песен. Фильтры применяются локально, без обращений к сайту источника.`, 'info');
  $('sourceMeta').textContent=`Источник: ${state.meta.source || name}${state.meta.generated_utc?' · обновлено '+state.meta.generated_utc.slice(0,10):''}`;
}

function wire(){
  for(const id of ['globalSearch','bulkAnime','songSearch','artistSearch'])$(id).addEventListener('input',scheduleRefresh);
  for(const id of ['animeBlank','seasonFilter','animeTypeFilter','songTypeFilter'])$(id).addEventListener('change',()=>refresh(true));
  document.querySelectorAll('input[name="titleMatch"]').forEach(i=>i.addEventListener('change',()=>refresh(true)));
  $('pageSize').addEventListener('change',()=>{state.pageSize=Number($('pageSize').value);state.page=0;render()});
  $('resetFilters').addEventListener('click',()=>{for(const id of ['globalSearch','bulkAnime','songSearch','artistSearch'])$(id).value='';for(const id of ['seasonFilter','animeTypeFilter','songTypeFilter'])$(id).value='';$('animeBlank').value='all';document.querySelector('input[name="titleMatch"][value="partial"]').checked=true;refresh(true)});
  $('pageFirst').addEventListener('click',()=>{state.page=0;render()});$('pagePrev').addEventListener('click',()=>{state.page--;render()});$('pageNext').addEventListener('click',()=>{state.page++;render()});$('pageLast').addEventListener('click',()=>{state.page=Math.ceil(state.visible.length/state.pageSize)-1;render()});
  $('selectPage').addEventListener('change',e=>{const cur=state.visible.slice(state.page*state.pageSize,(state.page+1)*state.pageSize);for(const r of cur){if(e.target.checked)state.selected.add(r.id);else state.selected.delete(r.id)}render()});
  document.querySelectorAll('th[data-sort] .sort-button').forEach(b=>b.addEventListener('click',()=>{const name=b.closest('th').dataset.sort;if(name===state.sortField)state.sortAsc=!state.sortAsc;else{state.sortField=name;state.sortAsc=true}sortVisible();state.page=0;render()}));
  document.querySelectorAll('button[data-col]').forEach(b=>b.addEventListener('click',()=>exportColumn(b.dataset.col)));
  $('exportCSV').addEventListener('click',()=>exportData(state.visible));$('exportJSON').addEventListener('click',()=>exportData(state.visible,'json'));
  $('exportAllColumns').addEventListener('click',downloadAllColumns);
  $('exportSelection').addEventListener('click',()=>exportData(state.rows.filter(r=>state.selected.has(r.id))));
  $('uploadButton').addEventListener('click',()=>$('filePicker').click());$('filePicker').addEventListener('change',async e=>{
    const file=e.target.files?.[0];if(!file)return;
    try{await importFile(await file.arrayBuffer(),file.name)}catch(error){notify(`Ошибка загрузки: ${error.message}`,'err');showStatus('Ошибка базы')}finally{e.target.value=''}
  });
}
async function startup(){wire();try{const resp=await fetch('./data/catalog.sqlite',{cache:'no-cache'});if(!resp.ok)throw new Error(`HTTP ${resp.status}`);await importFile(await resp.arrayBuffer(),'catalog.sqlite')}catch(e){notify(`Не удалось открыть SQLite. Запустите сайт через HTTP (python -m http.server), либо загрузите .db вручную. Подробности: ${e.message}`,'err');showStatus('База не загружена')}}
startup();
