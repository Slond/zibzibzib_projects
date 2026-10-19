'use strict';
((root, factory) => {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.SportMuscles = api;
})(globalThis, () => {
  // Areas follow the catalog's primary target; this is not an intensity estimate.
  const labels = {chest:'Грудь',shoulders:'Плечи',biceps:'Бицепс',triceps:'Трицепс',forearms:'Предплечья',
    abs:'Пресс',obliques:'Косые мышцы живота',upperBack:'Верх спины',lats:'Широчайшие',traps:'Трапеции',
    lowerBack:'Поясница',glutes:'Ягодицы',quads:'Квадрицепсы',hamstrings:'Задняя поверхность бедра',
    calves:'Икры',hipFlexors:'Сгибатели бедра',adductors:'Приводящие мышцы',abductors:'Отводящие мышцы',
    outerThigh:'Наружная поверхность бедра',neck:'Шея',hands:'Ладони',feet:'Стопы'};
  const aliases = {
    'грудь':['chest'],'chest':['chest'],'плечи':['shoulders'],'shoulders':['shoulders'],
    'бицепс':['biceps'],'biceps':['biceps'],'трицепс':['triceps'],'triceps':['triceps'],
    'предплечья':['forearms'],'forearms':['forearms'],'пресс':['abs'],'abs':['abs'],
    'косые мышцы живота':['obliques'],'obliques':['obliques'],'верх спины':['upperBack'],
    'upper back':['upperBack'],'middle back':['upperBack'],'широчайшие':['lats'],'lats':['lats'],
    'трапеции':['traps'],'traps':['traps'],'поясница':['lowerBack'],'lower back':['lowerBack'],
    'ягодицы':['glutes'],'glutes':['glutes'],'квадрицепсы':['quads'],'quads':['quads'],
    'задняя поверхность бедра':['hamstrings'],'hamstrings':['hamstrings'],'икры':['calves'],'calves':['calves'],
    'сгибатели бедра':['hipFlexors'],'hip flexors':['hipFlexors'],
    'приводящие мышцы бедра':['adductors'],'adductors':['adductors'],
    'отводящие мышцы бедра':['abductors'],'abductors':['abductors'],
    'подвздошно-большеберцовый тракт':['outerThigh'],'it band':['outerThigh'],
    'шея':['neck'],'neck':['neck'],'ладонная фасция':['hands'],'palmar fascia':['hands'],
    'подошвенная фасция':['feet'],'plantar fascia':['feet'],
    'ноги':['quads','hamstrings','calves'],'legs':['quads','hamstrings','calves'],
    'спина':['upperBack','lats','lowerBack'],'back':['upperBack','lats','lowerBack'],
    'руки':['biceps','triceps','forearms'],'arms':['biceps','triceps','forearms'],
  };

  function describe(session, resolve) {
    if (!session || session.status === 'moved') return {groups:[],missing:0,empty:'День отдыха'};
    if (session.status === 'skipped') return {groups:[],missing:0,empty:'Тренировки не было'};
    const groups = new Set();let missing = 0;
    const finished = ['done','partial'].includes(session.status);
    for (const item of session.plan?.exercises || []) {
      if (finished && !session.sets?.[item.id]?.some(set => set.done)) continue;
      const muscle = resolve(item.id)?.muscle || '';
      let matched = false;
      for (const token of muscle.toLocaleLowerCase('ru').replace(/ё/g,'е').split(/[,;/+&]+|\s+и\s+/)) {
        for (const group of aliases[token.trim()] || []) {groups.add(group);matched = true;}
      }
      if (!matched) missing++;
    }
    return {groups:[...groups],missing,empty:finished?'Нет выполненных упражнений':'Мышцы не указаны'};
  }

  const outline = 'M54 35V42Q43 42 36 49L25 82 21 111Q20 117 26 118Q30 118 31 113L42 77 44 106 40 126 44 172 45 214H57L59 174 65 145 71 174 73 214H85L86 172 90 126 86 106 88 77 99 113Q100 118 104 118Q110 117 109 111L105 82 94 49Q87 42 76 42V35';
  const front = {
    neck:'M57 33H73V43H57Z', shoulders:'M44 47Q47 44 54 45L51 58 40 65Z M76 45Q83 44 86 47L90 65 79 58Z',
    chest:'M53 59L63 57V79L50 84 47 66Z M67 57L77 59 83 66 80 84 67 79Z',
    biceps:'M38 66L47 69 42 88 34 86Z M83 69L92 66 96 86 88 88Z',
    forearms:'M32 90L40 93 31 110 25 109Z M90 93L98 90 105 109 99 110Z',
    abs:'M56 85H74L72 111H58Z', obliques:'M48 85L54 86 56 111 47 105Z M76 86L82 85 83 105 74 111Z',
    hipFlexors:'M48 112L61 118 57 130 45 121Z M69 118L82 112 85 121 73 130Z',
    quads:'M44 130L55 134 58 147 54 170 46 170Z M75 134L86 130 84 170 76 170 72 147Z',
    adductors:'M58 126L64 133 58 159 55 147Z M66 133L72 126 75 147 72 159Z',
    abductors:'M42 113L48 111 44 130 40 126Z M82 111L88 113 90 126 86 130Z',
    outerThigh:'M41 132L44 133 47 169 44 169Z M86 133L89 132 86 169 83 169Z',
    hands:'M22 112H30L28 120H22Z M100 112H108V120H102Z',
    feet:'M45 215H57V221H41Z M73 215H85L89 221H73Z',
  };
  const back = {
    neck:front.neck, shoulders:front.shoulders, forearms:front.forearms,hands:front.hands,feet:front.feet,
    traps:'M58 44H72L80 56 66 62 50 56Z',
    upperBack:'M51 61L63 66V83L49 80Z M67 66L79 61 81 80 67 83Z',
    lats:'M47 83L62 88V108L51 102Z M68 88L83 83 79 102 68 108Z',
    lowerBack:'M57 109H73L77 121H53Z',
    triceps:'M38 66L47 69 42 88 34 86Z M83 69L92 66 96 86 88 88Z',
    glutes:'M46 123L63 125V143L49 145 43 136Z M67 125L84 123 87 136 81 145 67 143Z',
    hamstrings:'M46 148L57 148 54 170H47Z M73 148L84 148 83 170H76Z',
    calves:'M47 176H55L54 198 49 208 46 198Z M75 176H83L84 198 79 208 76 198Z',
    abductors:front.abductors,outerThigh:front.outerThigh,
  };

  function render(session, resolve) {
    const state = describe(session, resolve), active = new Set(state.groups);
    const names = state.groups.map(group => labels[group]);
    const summary = names.length ? names.slice(0,3).join(' · ') + (names.length>3?' · +'+(names.length-3):'') : state.missing?'Мышцы не указаны':state.empty;
    const full = names.length ? 'Основные зоны: '+names.join(', ')+(state.missing?'. Есть упражнения без группы мышц.':'') : summary;
    const body = (regions, offset, label) => `<g transform="translate(${offset} 0)"><circle cx="65" cy="20" r="15" class="body-outline"/><path d="${outline}" class="body-outline"/>${Object.entries(regions).map(([group,path])=>`<path d="${path}" data-muscle="${group}" class="muscle-region${active.has(group)?' active':''}"/>`).join('')}<text x="65" y="238" text-anchor="middle">${label}</text></g>`;
    return `<figure class="muscle-map" title="${full}"><svg viewBox="0 0 260 247" role="img" aria-label="${full}">${body(front,0,'Спереди')}${body(back,130,'Сзади')}</svg><figcaption>${summary}${state.missing&&names.length?'<small>Часть мышц не указана</small>':''}</figcaption></figure>`;
  }

  return {describe,render};
});
