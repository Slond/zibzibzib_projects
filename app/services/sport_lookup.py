"""Import a user-selected Muscle & Strength exercise; never follow arbitrary URLs."""
from __future__ import annotations

import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
import httpx
from pydantic import Field

from app.services.sport import ExerciseDefinition, StrictModel
from app.services.sport_media import extract_video

HOSTS = {"www.muscleandstrength.com", "muscleandstrength.com"}
LABELS = {"Quads": "Квадрицепсы", "Hamstrings": "Задняя поверхность бедра", "Glutes": "Ягодицы",
          "Chest": "Грудь", "Lats": "Широчайшие", "Middle Back": "Спина", "Lower Back": "Поясница",
          "Shoulders": "Плечи", "Biceps": "Бицепс", "Triceps": "Трицепс", "Abs": "Пресс", "Calves": "Икры",
          "Machine": "Тренажёр", "Barbell": "Штанга", "Dumbbell": "Гантели", "Dumbbells": "Гантели",
          "Bodyweight": "Без инвентаря", "Cable": "Блочный тренажёр", "Kettlebells": "Гири",
          "Bands": "Резинка", "Exercise Ball": "Фитбол", "Other": "Другое"}


class ExerciseImport(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    url: str = Field(min_length=1, max_length=2000)


def exercise_url(value):
    parts = urlsplit(value.strip())
    if (parts.scheme != "https" or parts.hostname not in HOSTS or parts.port not in {None, 443}
            or parts.username or parts.password or parts.query or parts.fragment
            or not re.fullmatch(r"/exercises/[a-zA-Z0-9-]+(?:\.html)?/?", parts.path)):
        raise ValueError("Нужна HTTPS-ссылка на конкретное упражнение Muscle & Strength.")
    return "https://www.muscleandstrength.com" + parts.path


def parse_exercise(html, name, url):
    soup = BeautifulSoup(html, "html.parser")
    video = extract_video(soup, url)
    for tag in soup(["script", "style", "noscript", "nav", "footer"]):
        tag.decompose()
    heading = soup.find("h1")
    if not heading:
        raise ValueError("Не удалось распознать страницу упражнения. Заполните карточку вручную.")
    english = re.sub(r"\s+(Video\s+)?Exercise Guide.*$", "", heading.get_text(" ", strip=True), flags=re.I)
    if not english or "just a moment" in english.lower():
        raise ValueError("Источник не отдал описание упражнения. Заполните карточку вручную.")
    text = soup.get_text(" ", strip=True)

    def profile(label, following):
        match = re.search(re.escape(label) + r"\s+(.+?)\s+(?:" + following + r")", text, re.I)
        return match.group(1).strip() if match else ""

    muscle = profile("Target Muscle Group", "Exercise Type|Equipment Required")
    equipment = profile("Equipment Required", "Mechanics|Force Type|Experience Level|Secondary Muscles")
    lines = []
    for section in soup.find_all(re.compile(r"^h[2-4]$")):
        if not re.search(r"\bInstructions\s*$", section.get_text(" ", strip=True), re.I):
            continue
        # Article content can be wrapped; stop before any subsequent section.
        for node in section.next_elements:
            if getattr(node, "name", None) in {"h2", "h3", "h4", "h5"}:
                break
            if getattr(node, "name", None) == "li":
                lines.append(node.get_text(" ", strip=True))
        if lines:
            break
    if not muscle or not equipment:
        raise ValueError("Не удалось прочитать мышцы или оборудование. Заполните карточку вручную.")
    image_tag = soup.find("meta", attrs={"property": "og:image"})
    image = urljoin(url, image_tag.get("content", "")) if image_tag else ""
    return ExerciseDefinition(name=name.strip(), en=english[:120],
                              muscle=LABELS.get(muscle, muscle)[:80],
                              equipment=LABELS.get(equipment, equipment)[:80],
                              instructions="\n".join(lines)[:4000], sourceUrl=url,
                              videoUrl=video, imageUrl=image, bodyweight=equipment == "Bodyweight",
                              attribution="Muscle & Strength").model_dump()


async def import_exercise(name, url, *, transport=None):
    url = exercise_url(url)
    async with httpx.AsyncClient(timeout=12, follow_redirects=False, transport=transport,
                                 headers={"User-Agent": "PersonalWorkoutJournal/1.0"}) as client:
        for _ in range(3):
            async with client.stream("GET", url) as response:
                if response.status_code in {301, 302, 307, 308}:
                    url = exercise_url(urljoin(url, response.headers.get("location", "")))
                    continue
                if response.status_code in {403, 429}:
                    raise ValueError("Muscle & Strength заблокировал загрузку страницы. Ссылка сохранится; заполните остальные поля вручную.")
                response.raise_for_status()
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > 2 * 1024 * 1024:
                        raise ValueError("Страница слишком большая. Заполните карточку вручную.")
                return parse_exercise(content.decode("utf-8", errors="replace"), name, url)
    raise ValueError("Источник перенаправляет слишком много раз. Заполните карточку вручную.")
