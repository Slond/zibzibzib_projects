const {test} = require('node:test');
const assert = require('node:assert/strict');
const {describe,render} = require('../app/static/sport/muscle-map.js');
const catalog = {bench:{muscle:'Грудь'},row:{muscle:'Верх спины'},squat:{muscle:'Квадрицепсы'},
  rdl:{muscle:'Задняя поверхность бедра'},calf:{muscle:'Икры'},custom:{muscle:'Грудь, Трицепс'},unknown:{muscle:'Не указано'}};
const resolve = id => catalog[id];
const session = (...ids) => ({status:'planned',plan:{title:'Любое название',exercises:ids.map(id=>({id}))},sets:{}});

test('chest and legs follow exercise metadata, not the workout title', () => {
  const chest = session('bench');chest.plan.title = 'Ноги';
  assert.deepEqual(describe(chest,resolve).groups,['chest']);
  assert.deepEqual(describe(session('squat','rdl','calf'),resolve).groups,['quads','hamstrings','calves']);
  const html = render(chest,resolve);
  assert.match(html,/data-muscle="chest" class="muscle-region active"/);
  assert.doesNotMatch(html,/data-muscle="(?:quads|hamstrings|calves)" class="muscle-region active"/);
});

test('mixed and custom workouts highlight all listed primary groups once', () => {
  assert.deepEqual(describe(session('bench','row','custom'),resolve).groups,['chest','upperBack','triceps']);
  assert.match(render(session('row'),resolve),/data-muscle="upperBack" class="muscle-region active"/);
});

test('rest, skipped workouts and unknown metadata never get invented highlights', () => {
  for (const value of [null,{...session('squat'),status:'moved'},{...session('squat'),status:'skipped'},session('unknown')]) {
    assert.deepEqual(describe(value,resolve).groups,[]);
    assert.doesNotMatch(render(value,resolve),/class="muscle-region active"/);
  }
  assert.match(render(session('unknown'),resolve),/Мышцы не указаны/);
  assert.equal(describe(session('bench','unknown'),resolve).missing,1);
});

test('completed and partial sessions use exercises with confirmed sets', () => {
  const partial = session('bench','squat');partial.status = 'partial';
  partial.sets = {bench:[{done:true}],squat:[{done:false}]};
  assert.deepEqual(describe(partial,resolve).groups,['chest']);
  partial.status = 'started';
  assert.deepEqual(describe(partial,resolve).groups,['chest','quads']);
});

test('every named group in the bundled catalog has a mapped diagram area', () => {
  const data = require('../app/catalogs/sport_exercises.json').exercises;
  for (const [id,exercise] of Object.entries(data)) {
    const actual = describe(session(id),key=>data[key]);
    if (exercise.muscle === 'Не указано') assert.equal(actual.missing,1);
    else {
      assert.ok(actual.groups.length,id+': '+exercise.muscle);
      const html = render(session(id),key=>data[key]);
      for (const group of actual.groups) assert.ok(html.includes(`data-muscle="${group}" class="muscle-region active"`));
    }
  }
});
