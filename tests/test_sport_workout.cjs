const {test} = require('node:test');
const assert = require('node:assert/strict');
const {toKg, fromKg, resizeSets, KG_PER_LB} = require('../app/static/sport/workout-model.js');

function workout() {
  const plan = {title: 'Workout', exercises: [{id: 'press', sets: 3, reps: '10', rest: 90}]};
  return {status: 'started', plan, originalPlan: structuredClone(plan), weightUnits: {press: 'lbs'},
    sets: {press: [{weight: '45.359237', reps: '10', done: true}, {weight: '0', reps: '12', done: true}, {weight: '', reps: '', done: false}]}};
}

test('pounds convert to bounded, canonical kilograms, including fractional and zero weights', () => {
  assert.equal(toKg('100', 'lbs'), '45.359237');
  assert.equal(toKg('2.5', 'lbs'), '1.133981');
  assert.equal(toKg('.5', 'lbs'), '0.226796');
  assert.equal(toKg('0', 'lbs'), '0');
  assert.equal(toKg('', 'lbs'), '');
  assert.equal(toKg('2000', 'kg'), '2000');
  for (const value of ['-1', 'NaN', 'Infinity', '1e2', '4409.246']) assert.throws(() => toKg(value, 'lbs'));
  assert.throws(() => toKg('2000.1', 'kg'));
  assert.throws(() => toKg('10', 'stone'));
  assert.equal(KG_PER_LB, 0.45359237);
});

test('display switching never modifies confirmed canonical results', () => {
  const session = workout(), before = structuredClone(session);
  for (let n = 0; n < 100; n++) {
    assert.equal(fromKg(session.sets.press[0].weight, 'lbs'), '100');
    assert.equal(fromKg(session.sets.press[0].weight, 'kg'), '45.359237');
  }
  assert.deepEqual(session, before);
  assert.equal(fromKg('', 'lbs'), '');
  assert.equal(fromKg('0', 'lbs'), '0');
});

test('adding/removing sets changes this session and retains completed results and original plan', () => {
  const session = workout(), before = structuredClone(session);
  assert.equal(resizeSets(session, 'press', 1), true);
  assert.equal(session.plan.exercises[0].sets, 4);
  assert.equal(session.sets.press.length, 4);
  assert.deepEqual(session.sets.press.slice(0, 3), before.sets.press);
  assert.deepEqual(session.sets.press[3], {weight: '', reps: '', done: false});
  assert.equal(session.manual, true);
  assert.equal(resizeSets(session, 'press', -1), true);
  assert.deepEqual(session.sets, before.sets);
  assert.deepEqual(session.originalPlan, before.originalPlan);
  assert.deepEqual(session.weightUnits, before.weightUnits);
});

test('set boundaries and closed workouts cannot be changed', () => {
  const session = workout();
  while (session.sets.press.length < 12) assert.equal(resizeSets(session, 'press', 1), true);
  assert.equal(resizeSets(session, 'press', 1), false);
  while (session.sets.press.length > 1) assert.equal(resizeSets(session, 'press', -1), true);
  assert.equal(resizeSets(session, 'press', -1), false);
  assert.deepEqual(session.sets.press[0], {weight: '45.359237', reps: '10', done: true});
  for (const status of ['done', 'partial', 'skipped', 'moved']) {
    session.status = status;
    const before = structuredClone(session);
    assert.equal(resizeSets(session, 'press', 1), false);
    assert.deepEqual(session, before);
  }
});
