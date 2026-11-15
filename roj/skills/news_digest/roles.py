"""Roles and output contracts for the morning-brief slots.

Bootstrap (expensive, once):  planner, critic, coder
Daily run (cheap):            classifier (only for events rules could not classify),
                              analyst, checker (another model family than the analyst)
Role prose exists in Polish and English; the project's prompt_language picks one (ADR-0003).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from ...prompting.compiler import RoleSpec
from ..slots import Slot
from .spec import Tier, TopicRule

# --- output contracts ------------------------------------------------------------------

class PlannedSource(BaseModel):
    source_id: str
    weight: float = Field(1.0, ge=0.0, le=3.0)
    tier: Tier = "unknown"
    region: str = "global"
    topics: list[str] = Field(default_factory=list)
    independence_key: str | None = None
    reason: str


class RejectedSource(BaseModel):
    source_id: str
    reason: str


class CandidateSource(BaseModel):
    name: str
    url: str
    why: str
    tier: Tier = "unknown"
    region: str = "global"


class PlannedInstrument(BaseModel):
    instrument_id: str
    reason: str = ""


class SourcePlan(BaseModel):
    selected: list[PlannedSource] = Field(min_length=1)
    rejected: list[RejectedSource] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    extra_candidates: list[CandidateSource] = Field(default_factory=list, max_length=8)
    topics: list[TopicRule] = Field(min_length=4, max_length=14)
    instruments: list[PlannedInstrument] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class Issue(BaseModel):
    issue: str
    severity: Literal["low", "medium", "high"]
    fix: str


class Critique(BaseModel):
    verdict: Literal["accept", "revise"]
    issues: list[Issue] = Field(default_factory=list)


class AdapterCode(BaseModel):
    code: str = Field(min_length=40)
    notes: str = ""


class ClassifiedEvent(BaseModel):
    event_id: str
    topics: list[str] = Field(default_factory=list)
    relevance: int = Field(ge=0, le=3)


class Classified(BaseModel):
    events: list[ClassifiedEvent]


class Claim(BaseModel):
    text: str = Field(min_length=5)
    item_ids: list[str] = Field(min_length=1)


class AnalysedEvent(BaseModel):
    event_id: str
    headline: str = Field(min_length=5, max_length=160)
    summary: str = Field(min_length=10, max_length=600)
    why_it_matters: str = Field(min_length=10, max_length=600)
    channels: list[str] = Field(default_factory=list, max_length=6)
    confidence: Literal["low", "medium", "high"] = "medium"
    claims: list[Claim] = Field(min_length=1, max_length=6)


class Analysis(BaseModel):
    events: list[AnalysedEvent] = Field(min_length=1)
    watch_today: list[str] = Field(default_factory=list, max_length=5)
    one_line: str = Field("", max_length=240)


class Verdict(BaseModel):
    event_id: str
    claim_index: int = Field(ge=0)
    supported: bool
    note: str = ""


class Verdicts(BaseModel):
    verdicts: list[Verdict]


class CandidateList(BaseModel):
    candidates: list[CandidateSource] = Field(default_factory=list, max_length=8)
    targets: list[str] = Field(default_factory=list)  # which gaps these candidates should close


class ItemLabel(BaseModel):
    item_id: str
    relevance: int = Field(ge=0, le=3)
    topics: list[str] = Field(default_factory=list)


class SourceAssessment(BaseModel):
    source_id: str
    quality: int = Field(ge=1, le=5)
    note: str = ""


class Labels(BaseModel):
    items: list[ItemLabel]
    sources: list[SourceAssessment] = Field(default_factory=list)


class RuleSet(BaseModel):
    topics: list[TopicRule] = Field(min_length=4, max_length=14)
    notes: str = ""


# --- roles -------------------------------------------------------------------------------

ROLE_TEXT: dict[str, dict[str, str]] = {
    "news-planner": {
        "en": ("You design the source portfolio for a daily morning brief on world events and their economic impact. "
               "You see only measured probe reports. Select sources that are reachable, fresh and complementary; "
               "prefer primary sources (central banks, statistics offices) plus independent newsrooms across regions. "
               "Reject sources with failing probes. Propose extra candidates only as real feed URLs you are confident "
               "exist; they will be probed before use. Define topic rules with keywords that a deterministic classifier "
               "can apply to headlines."),
        "pl": ("Projektujesz portfel źródeł do codziennego porannego przeglądu wydarzeń na świecie i ich wpływu na gospodarkę. "
               "Widzisz tylko zmierzone raporty z sond. Wybieraj źródła dostępne, świeże i uzupełniające się; preferuj "
               "źródła pierwotne (banki centralne, urzędy statystyczne) oraz niezależne redakcje z różnych regionów. "
               "Odrzucaj źródła z nieudanymi sondami. Dodatkowych kandydatów proponuj tylko jako prawdziwe adresy kanałów, "
               "co do których masz pewność; zostaną sprawdzone sondą przed użyciem. Zdefiniuj reguły tematów ze słowami "
               "kluczowymi, które deterministyczny klasyfikator zastosuje do nagłówków."),
    },
    "news-critic": {
        "en": ("You review a source plan for a morning economic brief. Find its weakest points: missing regions or topics, "
               "dependence on one newsroom, low-trust sources weighted too high, topic rules that will misfire. "
               "Return 'revise' only when an issue would visibly hurt the brief."),
        "pl": ("Recenzujesz plan źródeł porannego przeglądu gospodarczego. Znajdź najsłabsze punkty: brakujące regiony "
               "lub tematy, zależność od jednej redakcji, zbyt wysoka waga źródeł o niskim zaufaniu, reguły tematów, które "
               "będą się mylić. Zwróć 'revise' tylko wtedy, gdy problem wyraźnie pogorszy przegląd."),
    },
    "adapter-coder": {
        "en": ("You write a small, pure Python function parse(raw, base_url) that extracts news items from one web page. "
               "Return a list of dicts with keys title, link, published (ISO 8601 or null) and summary. Use only the modules "
               "re, json, html, html.parser, datetime, urllib.parse, xml.etree.ElementTree, email.utils. No I/O, no network, "
               "no eval/exec/open, no classes, no dunder attributes. Resolve relative links with urllib.parse.urljoin."),
        "pl": ("Piszesz małą, czystą funkcję Pythona parse(raw, base_url), która wyciąga wiadomości z jednej strony WWW. "
               "Zwróć listę słowników z kluczami title, link, published (ISO 8601 lub null) i summary. Używaj tylko modułów "
               "re, json, html, html.parser, datetime, urllib.parse, xml.etree.ElementTree, email.utils. Bez I/O, bez sieci, "
               "bez eval/exec/open, bez klas, bez atrybutów dunder. Względne linki rozwiązuj przez urllib.parse.urljoin."),
    },
    "news-classifier": {
        "en": ("You tag news events with topics from a fixed list and rate their relevance for the world economy: "
               "0 none, 1 minor, 2 notable, 3 major. Judge only from the given headlines and summaries."),
        "pl": ("Przypisujesz wydarzeniom tematy z ustalonej listy i oceniasz ich znaczenie dla gospodarki światowej: "
               "0 brak, 1 niewielkie, 2 zauważalne, 3 duże. Oceniaj wyłącznie na podstawie podanych nagłówków i streszczeń."),
    },
    "news-analyst": {
        "en": ("You are a macro analyst writing a morning brief for a busy reader. For each event write in your own words "
               "what happened and why it matters for the economy and markets: transmission channels, who is affected, what to "
               "watch. Every factual claim must cite the item ids it comes from. Use numbers only if they appear in the "
               "cited items or in the market table. Do not copy sentences from sources."),
        "pl": ("Jesteś analitykiem makro i piszesz poranny przegląd dla zapracowanego czytelnika. Dla każdego wydarzenia "
               "napisz własnymi słowami, co się stało i dlaczego to ważne dla gospodarki i rynków: kanały transmisji, kogo "
               "dotyczy, na co patrzeć. Każde twierdzenie faktyczne musi wskazywać identyfikatory elementów, z których "
               "pochodzi. Liczb używaj tylko wtedy, gdy występują w cytowanych elementach albo w tabeli rynkowej. "
               "Nie kopiuj zdań ze źródeł."),
    },
    "news-scout": {
        "en": ("You find additional news sources that close coverage gaps of a morning economic brief. Propose only feed "
               "URLs (RSS/Atom) or news-list pages you are confident exist, preferably official or established newsrooms. "
               "Never repeat a URL that was already probed. Every candidate will be fetched and measured before use."),
        "pl": ("Szukasz dodatkowych źródeł wiadomości, które zamkną luki w pokryciu porannego przeglądu gospodarczego. "
               "Proponuj tylko adresy kanałów (RSS/Atom) lub stron z listą wiadomości, co do których masz pewność, najlepiej "
               "oficjalne lub uznane redakcje. Nie powtarzaj adresów już sprawdzonych. Każdy kandydat zostanie pobrany i "
               "zmierzony przed użyciem."),
    },
    "news-assessor": {
        "en": ("You are the teacher for a deterministic news classifier. For every item give its relevance for the world "
               "economy and markets (0 none, 1 minor, 2 notable, 3 major) and its topics from the allowed list only. "
               "Rate every source's overall quality from 1 to 5 based on its items. Your labels become the test set "
               "the classifier is measured against, so be consistent."),
        "pl": ("Jesteś nauczycielem deterministycznego klasyfikatora wiadomości. Każdemu elementowi nadaj znaczenie dla "
               "gospodarki światowej i rynków (0 brak, 1 niewielkie, 2 zauważalne, 3 duże) oraz tematy wyłącznie z "
               "dozwolonej listy. Oceń ogólną jakość każdego źródła w skali 1–5 na podstawie jego elementów. Twoje etykiety "
               "staną się zbiorem testowym, na którym mierzony jest klasyfikator, więc bądź konsekwentny."),
    },
    "rule-smith": {
        "en": ("You improve keyword rules of a deterministic topic classifier. You see the current rules, their measured "
               "precision and recall, and misclassified examples. Add keywords that catch missed items and remove keywords "
               "that cause false matches. Keep topic names unchanged. Prefer short, specific phrases over single generic words."),
        "pl": ("Ulepszasz reguły słów kluczowych deterministycznego klasyfikatora tematów. Widzisz obecne reguły, ich "
               "zmierzoną precyzję i czułość oraz błędnie sklasyfikowane przykłady. Dodaj słowa, które złapią pominięte "
               "elementy, i usuń te, które powodują fałszywe dopasowania. Nie zmieniaj nazw tematów. Wybieraj krótkie, "
               "konkretne frazy zamiast pojedynczych ogólnych słów."),
    },
    "news-checker": {
        "en": ("You verify claims against the source snippets they cite. A claim is supported only if the cited snippets "
               "state it or directly imply it. Numbers must match exactly. Be strict: when in doubt, mark it unsupported."),
        "pl": ("Weryfikujesz twierdzenia względem fragmentów źródeł, które cytują. Twierdzenie jest poparte tylko wtedy, gdy "
               "cytowane fragmenty je stwierdzają lub bezpośrednio z nich wynika. Liczby muszą się zgadzać dokładnie. "
               "Bądź surowy: w razie wątpliwości oznacz jako niepoparte."),
    },
}


def role(name: str, language: str) -> RoleSpec:
    text = ROLE_TEXT[name].get(language) or ROLE_TEXT[name]["en"]
    return RoleSpec(name=name, instructions=text, version="1")


def slot(name: str, language: str) -> Slot:
    spec = {
        "news-planner": (SourcePlan, 4000),
        "news-critic": (Critique, 1500),
        "adapter-coder": (AdapterCode, 2500),
        "news-classifier": (Classified, 1500),
        "news-analyst": (Analysis, 4000),
        "news-checker": (Verdicts, 2000),
        "news-scout": (CandidateList, 2000),
        "news-assessor": (Labels, 6000),
        "rule-smith": (RuleSet, 3000),
    }[name]
    return Slot(name=name, role=role(name, language), output=spec[0], max_tokens=spec[1])


# role in the skill -> slot name (bindings are configured per role)
ROLE_OF_SLOT = {
    "news-planner": "planner", "news-critic": "critic", "adapter-coder": "coder",
    "news-classifier": "classifier", "news-analyst": "analyst", "news-checker": "checker",
    "news-scout": "scout", "news-assessor": "assessor", "rule-smith": "rulesmith",
}
