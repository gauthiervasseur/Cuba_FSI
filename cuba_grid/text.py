"""HTML -> clean note text, plus number and time normalisation shared by regex and validators."""
import re

from bs4 import BeautifulSoup

KEYWORDS = re.compile(r"\bMW\b|disponibilidad|demanda|déficit|afectaci", re.I)
# Anything that is reader comments, related links or share widgets.
NOISE_CLASS = re.compile(r"comment|related|share|tags|sidebar|footer|vea|series|moreposts", re.I)


def _paragraphs(node) -> list[str]:
    out = []
    for p in node.find_all(["p", "li"]):
        if any(NOISE_CLASS.search(" ".join(parent.get("class") or []) + " " + (parent.get("id") or ""))
               for parent in p.parents):
            continue
        t = " ".join(p.get_text(" ").split())
        if t:
            out.append(t)
    return out


def html_to_text(html: str, selector: str | None = None) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "form"]):
        tag.decompose()
    node = soup.select_one(selector) if selector else None
    if node is None:
        # Generic fallback: the container whose paragraphs mention grid keywords most often.
        best, best_score = None, 0
        for cand in soup.find_all(["article", "div", "section"]):
            if NOISE_CLASS.search(" ".join(cand.get("class") or []) + " " + (cand.get("id") or "")):
                continue
            direct = [p for p in cand.find_all("p", recursive=False)]
            score = sum(len(KEYWORDS.findall(p.get_text())) for p in direct)
            if score > best_score:
                best, best_score = cand, score
        node = best
    if node is None:
        return ""
    return "\n".join(_paragraphs(node))


def is_daily_note(text: str) -> bool:
    t = text.lower()
    return (
        "disponibilidad" in t
        and "demanda" in t
        and re.search(r"\bmw\b", t) is not None
        and re.search(r"pico|horario|sen\b", t) is not None
    )


# "1 850", "1.850", "1,850", "1 850", "1850", "1 mil 850" -> 1850; "1028.8" -> 1029.
NUM = r"(?:(?:\d+|un|dos|tres|cuatro)\s+)?mil\s+\d{1,3}(?!\d)|\d{1,3}(?:[ .,  ]\d{3})+(?!\d)|\d+(?:[.,]\d{1,2})?(?!\d)"


def to_int(s: str | None) -> int | None:
    if s is None:
        return None
    s = s.strip()
    if m := re.fullmatch(r"(?:(\d+|un|dos|tres|cuatro)\s+)?mil\s+(\d{1,3})", s, re.I):  # "mil 113", "dos mil 42"
        word = (m[1] or "un").lower()
        thousands = {"un": 1, "dos": 2, "tres": 3, "cuatro": 4}.get(word) or int(word)
        return thousands * 1000 + int(m[2])
    if re.fullmatch(r"\d+[.,]\d{1,2}", s):  # decimal MW, as in some 2022 notes
        return round(float(s.replace(",", ".")))
    digits = re.sub(r"[ .,  ]", "", s)
    return int(digits) if digits.isdigit() else None


TIME = r"\d{1,2}(?::\d{2}\s*(?:horas|hrs?|h\b)?\s*(?:[ap]\.?\s?m\b\.?)?|\s*[ap]\.?\s?m\b\.?)"


_HOUR_WORDS = {"una": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6, "siete": 7, "ocho": 8,
               "nueve": 9, "diez": 10, "once": 11, "doce": 12}


def _hour_in_words(s: str) -> str | None:
    """'a las seis de la mañana' -> '06:00', 'las ocho y media de la noche' -> '20:30'."""
    m = re.search(r"\b(" + "|".join(_HOUR_WORDS) + r")(\s+y\s+media)?\s+de\s+la\s+(mañana|madrugada|tarde|noche)",
                  s, re.I)
    if not m:
        return None
    h = _HOUR_WORDS[m[1].lower()] % 12 + (12 if m[3].lower() in ("tarde", "noche") else 0)
    return f"{h:02d}:{30 if m[2] else 0:02d}"


def to_hhmm(s: str | None) -> str | None:
    if not s:
        return None
    if words := _hour_in_words(s):
        return words
    # First number that is really a time (has minutes or a.m./p.m.), so "879 MW a las 7:20 p.m." works.
    pat = r"(?<!\d)(\d{1,2})(?::\s?(\d{2}))?\s*(?:horas|hrs?|h\b)?\s*(?:([ap])\.?\s?m\b|(de la (?:tarde|noche)))?"
    m = next((m for m in re.finditer(pat, s, re.I) if m.group(2) or m.group(3) or m.group(4)), None)
    if not m:
        return None
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower()
    if (ap == "p" or m.group(4)) and h < 12:
        h += 12
    if ap == "a" and h == 12:
        h = 0
    return f"{h:02d}:{mi:02d}" if h < 24 and mi < 60 else None


def normalize_ws(s: str) -> str:
    return " ".join(s.replace(" ", " ").replace(" ", " ").split())


def all_hhmm(s: str | None) -> list[str]:
    """Every time in a quote, for quotes like 'desde las 6:26 p.m. hasta las 6:56 p.m.'."""
    if not s:
        return []
    parts = re.split(r"\b(?=(?:desde|hasta|a)\s+las\b)", s)
    return [t for t in (to_hhmm(p) for p in parts) if t]
