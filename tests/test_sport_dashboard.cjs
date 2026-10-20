const {test}=require('node:test');
const assert=require('node:assert/strict');
const {dashboardFocus}=require('../app/static/sport/workout-model.js');
const day='2026-10-20';
const session=status=>({status,plan:{title:'Тренировка',exercises:[]},sets:{}});

test('today keeps the whole focus until the dated workout is closed',()=>{
 for(const status of ['planned','started','skipped']){
  const today=session(status),next=session('planned');
  const actual=dashboardFocus({'2026-10-19':session('done'),[day]:today,'2026-10-21':next},day);
  assert.equal(actual.mode,'today');assert.deepEqual(actual.current,[day,today]);
 }
});

test('a completed or partial workout yields to the nearest actual planned date',()=>{
 for(const status of ['done','partial']){
  const data={'2026-10-25':session('planned'),'2026-10-22':session('planned'),
   '2026-10-21':session('moved'),[day]:session(status),'2026-10-19':session('planned')};
  const before=structuredClone(data),actual=dashboardFocus(data,day);
  assert.equal(actual.mode,'completed');assert.equal(actual.completed[0],day);
  assert.equal(actual.next[0],'2026-10-22');assert.deepEqual(data,before);
 }
});

test('rest days retain the last result, and the next workout takes over on its date',()=>{
 const data={'2026-10-19':session('done'),'2026-10-22':session('planned')};
 assert.equal(dashboardFocus(data,day).completed[0],'2026-10-19');
 const nextDay=dashboardFocus(data,'2026-10-22');
 assert.equal(nextDay.mode,'today');assert.equal(nextDay.current[0],'2026-10-22');
 data['2026-10-22'].status='done';
 assert.equal(dashboardFocus(data,'2026-10-22').next,null);
 data['2026-10-22'].status='started';
 assert.equal(dashboardFocus(data,'2026-10-22').mode,'today');
});

test('empty journals and exhausted schedules have explicit empty states',()=>{
 assert.deepEqual(dashboardFocus({},day),{mode:'rest'});
 assert.equal(dashboardFocus({'2026-10-22':session('planned')},day).mode,'upcoming');
 const exhausted=dashboardFocus({[day]:session('done'),'2026-10-21':session('moved')},day);
 assert.equal(exhausted.mode,'completed');assert.equal(exhausted.next,null);
});
