/* Pure search and statistics; can be unit tested without a browser. */
export const FIELDS = [
  ['season','Сезон'],['anime_type','Тип аниме'],['anime_title','Название аниме'],
  ['song_type','Тип песни'],['song_title','Название песни'],['artist','Артист']
];
export const text = value => String(value ?? '').normalize('NFKC').toLocaleLowerCase().replace(/\s+/g,' ').trim();

export function parseTitles(raw) {
  const out=[]; const used=new Set();
  for(const line of String(raw || '').split(/\r?\n/)){
    const s=text(line);
    if(!s || used.has(s)) continue;
    used.add(s);out.push(s);
  }
  return out;
}

export function prepare(row) {
  const searchable = [row.season,row.anime_type,row.anime_title,row.song_type,row.song_title,row.artist,row.anime_english,row.anime_japanese,row.anime_alt].map(text);
  row._norm=searchable;
  row._names = [searchable[2], searchable[6], searchable[7], ...String(row.anime_alt || '').split(/\s\|\s|\\\$/).map(text)].filter(Boolean);
  return row;
}

export function createCriteria(filters) {
  const titles=parseTitles(filters.bulkAnime);
  const hasEmptyInTitles=titles.includes('[пусто]') || titles.includes('""');
  const nonempty=titles.filter(t=>t!=='[пусто]' && t!=='""');
  return {
    all:text(filters.all), song:text(filters.song), artist:text(filters.artist),
    season:filters.season||'', animeType:filters.animeType||'',songType:filters.songType||'',
    emptyMode:filters.emptyMode||'all', exact:filters.exact===true,
    names:nonempty, namesSet:new Set(nonempty), wantsEmpty:hasEmptyInTitles,
    hasBulk:titles.length>0,
  };
}

export function matches(row, c) {
  const n=row._norm;
  if(c.all && !n.some(v=>v.includes(c.all))) return false;
  if(c.song && !n[4].includes(c.song))return false;
  if(c.artist && !n[5].includes(c.artist))return false;
  if(c.season && row.season !== c.season)return false;
  if(c.animeType && row.anime_type !== c.animeType)return false;
  if(c.songType && row.song_type !== c.songType)return false;
  if(c.emptyMode==='empty' && n[2])return false;
  if(c.emptyMode==='filled' && !n[2])return false;
  if(c.hasBulk){
    let good=c.wantsEmpty && !n[2];
    if(!good && c.names.length){
      if(c.exact) good=row._names.some(v=>c.namesSet.has(v));
      else good=row._names.some(v=>c.names.some(query=>v.includes(query)));
    }
    if(!good)return false;
  }
  return true;
}

export function findRows(rows, filters) {
  const c=createCriteria(filters);
  return rows.filter(row=>matches(row,c));
}

export function counts(rows) {
  const anime = new Set(), artists = new Set();
  let empty=0;
  for(const row of rows){
    if(!row.anime_title)empty++;
    else anime.add(row.ann_id ? `id:${row.ann_id}` : text(row.anime_title));
    if(row.artist)artists.add(text(row.artist));
  }
  return {songs:rows.length,anime:anime.size,artists:artists.size,empty};
}

export function frequencies(rows, field) {
  if(!FIELDS.some(([key])=>key===field))throw new Error('Unknown column');
  const map=new Map();
  for(const row of rows) {const key=row[field]||'';map.set(key,(map.get(key)||0)+1)}
  return [...map].sort((a,b)=>b[1]-a[1] || a[0].localeCompare(b[0],'ru')).map(([value,count])=>({value,count}));
}

export function seasonSort(season) {
  const m=/^(Winter|Spring|Summer|Fall)\s+(\d{4})$/i.exec(season);
  if(!m) return null;
  return Number(m[2])*4+({winter:0,spring:1,summer:2,fall:3}[m[1].toLowerCase()]||0);
}
