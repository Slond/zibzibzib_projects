'use strict';
// Canonical results always use kilograms; units only affect entry and display.
((root, factory) => {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.SportWorkout = api;
})(globalThis, () => {
  const KG_PER_LB = 0.45359237;
  const MAX_KG = 2000;
  const validUnit = unit => unit === 'kg' || unit === 'lbs';
  const tenth = value => Math.round((value + Number.EPSILON) * 10) / 10;

  function toKg(value, unit) {
    if (!validUnit(unit)) throw new Error('Unknown weight unit');
    value = value.trim().replace(',', '.');
    if (value === '') return '';
    if (!/^(?:\d+(?:\.\d*)?|\.\d+)$/.test(value)) throw new Error('Invalid weight');
    const kg = Number(value) * (unit === 'lbs' ? KG_PER_LB : 1);
    if (!Number.isFinite(kg) || kg < 0 || kg > MAX_KG) throw new Error('Invalid weight');
    // Round the entered unit, keeping conversion precision internally to avoid drift.
    return String(Number((tenth(Number(value)) * (unit === 'lbs' ? KG_PER_LB : 1)).toFixed(6)));
  }

  function fromKg(value, unit) {
    if (!validUnit(unit)) throw new Error('Unknown weight unit');
    if (value === '') return '';
    return tenth(Number(value) / (unit === 'lbs' ? KG_PER_LB : 1)).toFixed(1);
  }

  function resizeSets(session, id, delta) {
    if (!session || !['planned', 'started'].includes(session.status) || ![-1, 1].includes(delta)) return false;
    const config = session.plan?.exercises.find(e => e.id === id);
    if (!config || config.sets + delta < 1 || config.sets + delta > 12) return false;
    const rows = session.sets[id] || Array.from({length: config.sets}, () => ({weight: '', reps: '', done: false}));
    if (rows.length !== config.sets) return false;
    if (delta === 1) rows.push({weight: '', reps: '', done: false});
    else rows.pop();
    session.sets[id] = rows;
    config.sets += delta;
    session.manual = true;
    return true;
  }

  return {KG_PER_LB, MAX_KG, toKg, fromKg, resizeSets};
});
