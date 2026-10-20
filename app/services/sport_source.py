"""Labels and source instruction extraction shared by single and bulk import."""
import re

MUSCLES = {'Middle Back':'Спина', 'Quads':'Квадрицепсы', 'Hamstrings':'Задняя поверхность бедра', 'Glutes':'Ягодицы',
           'Chest':'Грудь', 'Lats':'Широчайшие', 'Upper Back':'Верх спины', 'Lower Back':'Поясница',
           'Shoulders':'Плечи', 'Biceps':'Бицепс', 'Triceps':'Трицепс', 'Abs':'Пресс', 'Calves':'Икры',
           'Forearms':'Предплечья', 'Traps':'Трапеции', 'Obliques':'Косые мышцы живота',
           'Hip Flexors':'Сгибатели бедра', 'Adductors':'Приводящие мышцы бедра',
           'Abductors':'Отводящие мышцы бедра', 'IT Band':'Подвздошно-большеберцовый тракт',
           'Neck':'Шея', 'Palmar Fascia':'Ладонная фасция', 'Plantar Fascia':'Подошвенная фасция'}
EQUIPMENT = {'Dumbbells':'Гантели', 'Kettlebells':'Гири', 'Dumbbell':'Гантели', 'Bodyweight':'Без инвентаря', 'Barbell':'Штанга',
             'Cable':'Блочный тренажёр', 'Machine':'Тренажёр', 'Kettle Bells':'Гири',
             'Bands':'Резинка', 'Exercise Ball':'Фитбол', 'Other':'Другой инвентарь',
             'EZ Bar':'EZ-гриф', 'Foam Roll':'Массажный ролик', 'Medicine Ball':'Медицинбол',
             'Sled':'Сани', 'Box':'Тумба', 'Jump Rope':'Скакалка', 'Lacrosse Ball':'Мяч для лакросса',
             'Trap Bar':'Трэп-гриф', 'Tiger Tail':'Ролик Tiger Tail', 'Chains':'Цепи',
             'Landmine':'Лэндмайн', 'Bench':'Скамья', 'Rings':'Кольца', 'Valslide':'Скользящие диски Valslide',
             'Hip Thruster':'Тренажёр Hip Thruster', 'Fat Bar':'Толстый гриф', 'Rope':'Канат',
             'Safety Bar':'Гриф Safety Bar', 'Tire':'Покрышка'}


def extract_instructions(soup):
    """Read the numbered article section, excluding empty tracking/list items."""
    for heading in soup.find_all(re.compile(r"^h[2-4]$")):
        if not re.search(r"\bInstructions\s*$", heading.get_text(" ", strip=True), re.I):
            continue
        steps, paragraphs = [], []
        for node in heading.next_elements:
            if getattr(node, "name", None) in {"h2", "h3", "h4", "h5", "footer", "nav"}:
                break
            tag = getattr(node, "name", None)
            if tag not in {"li", "p"} or node.find_parent("li"):
                continue
            text = " ".join(node.get_text(" ", strip=True).split())
            text = re.sub(r"\s+([,.;:!?])", r"\1", text)
            if text:
                (steps if tag == "li" else paragraphs).append(text)
        if steps or paragraphs:
            return "\n".join(steps or paragraphs)
    return ""
