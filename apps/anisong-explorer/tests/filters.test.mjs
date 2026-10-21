import test from 'node:test';
import assert from 'node:assert/strict';
import {prepare,findRows,parseTitles,counts,frequencies,seasonSort} from '../site/engine.mjs';
const rows=[
 prepare({id:1,season:'Spring 2025',anime_type:'TV',anime_title:'Naruto',song_type:'Opening',song_title:'Song A',artist:'AAA',anime_english:'Naruto English',anime_japanese:'Naruto',anime_alt:'Naruto Alias',ann_id:'1'}),
 prepare({id:2,season:'Spring 2025',anime_type:'TV',anime_title:'Bleach',song_type:'Ending',song_title:'Song B',artist:'BBB',anime_english:'Bleach EN',anime_japanese:'Bleach',anime_alt:'',ann_id:'2'}),
 prepare({id:3,season:'Winter 2026',anime_type:'Movie',anime_title:'',song_type:'Insert',song_title:'Song C',artist:'',anime_english:'',anime_japanese:'',anime_alt:'',ann_id:'3'}),
 prepare({id:4,season:'Summer 2024',anime_type:'TV',anime_title:'Naruto',song_type:'Ending',song_title:'Song D',artist:'AAA',anime_english:'',anime_japanese:'',anime_alt:'',ann_id:'1'}),
];
function f(overrides={}){return {all:'',bulkAnime:'',song:'',artist:'',season:'',animeType:'',songType:'',exact:false,emptyMode:'all',...overrides}}
test('100 bulk input lines accepted; OR selection, not AND',()=>{
 let names=Array.from({length:100},(_,i)=>`Series ${i}`);names[93]='Bleach';assert.equal(parseTitles(names.join('\n')).length,100);
 assert.deepEqual(findRows(rows,f({bulkAnime:names.join('\n')})).map(r=>r.id),[2]);
});
test('empty marker includes nameless anime',()=>{
 assert.deepEqual(findRows(rows,f({bulkAnime:'[пусто]'})).map(r=>r.id),[3]);
 assert.deepEqual(findRows(rows,f({bulkAnime:'Naruto\n[пусто]'})).map(r=>r.id),[1,3,4]);
});
test('filters can select only empty anime titles',()=>{
 assert.deepEqual(findRows(rows,f({emptyMode:'empty'})).map(r=>r.id),[3]);
 assert.equal(findRows(rows,f({emptyMode:'filled'})).length,3);
});
test('exact alias search and partial artist filtering',()=>{
 assert.deepEqual(findRows(rows,f({bulkAnime:'Naruto Alias',exact:true,artist:'aaa'})).map(r=>r.id),[1]);
 assert.deepEqual(findRows(rows,f({animeType:'TV',songType:'Ending',artist:'aaa'})).map(r=>r.id),[4]);
});
test('empty lines and case-insensitive duplicates ignored',()=>{
 assert.deepEqual(parseTitles('\n Naruto \r\nnaruto\n  \nBLEACH\n'),['naruto','bleach']);
});
test('counts and frequencies are accurate and preserve blank names',()=>{
 assert.deepEqual(counts(rows),{songs:4,anime:2,artists:2,empty:1});
 assert.deepEqual(frequencies(rows,'anime_title'),[{value:'Naruto',count:2},{value:'',count:1},{value:'Bleach',count:1}]);
});
test('season ordering helper',()=>assert.ok(seasonSort('Fall 2025')<seasonSort('Winter 2026')));

test('Shikimori Russian and licensed names match bulk search (exact/partial)', () => {
  const rows=[prepare({id:101,anime_title:'Naruto',anime_english:'Naruto',anime_japanese:'Naruto',anime_alt:'',anime_russian:'Наруто',anime_ru_license:'Наруто: Ураганные хроники',song_title:'Silhouette',song_type:'Opening',artist:'KANA-BOON',season:'Fall 2002',anime_type:'TV',ann_id:'1'})];
  assert.equal(findRows(rows,{bulkAnime:'Наруто',exact:true}).length,1);
  assert.equal(findRows(rows,{bulkAnime:'Ураганные',exact:false}).length,1);
  assert.equal(findRows(rows,{all:'НАРУТО'}).length,1);
  assert.equal(findRows(rows,{bulkAnime:'Ван-Пис',exact:false}).length,0);
});
