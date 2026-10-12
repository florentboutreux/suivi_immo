#!/usr/bin/env python3
"""
=============================================================================
VEILLE IMMOBILIÈRE SPÉCIALE MARCHAND DE BIENS (MDB) – MILLAU (12100) v2.2
=============================================================================
"""

import csv
import json
import logging
import os
import random
import re
import sys
import unicodedata
import urllib.request
from collections import Counter, deque
from dataclasses import dataclass, asdict, field, fields
from datetime import datetime
from pathlib import Path
from urllib.parse import urldefrag, urlparse

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# --------------------------------------------------------------------------- #
# CONFIGURATION
# --------------------------------------------------------------------------- #
DATA_DIR = Path("data")
FICHIER_ETAT = DATA_DIR / "annonces_vues.json"
FICHIER_CSV = DATA_DIR / "annonces.csv"
DOSSIER_DOCS = Path("docs")
FICHIER_HTML = DOSSIER_DOCS / "index.html"
DEBUG_DIR = Path("debug")

VILLES_AUTORISEES = ("millau",)
LOCALISATION_STRICTE = True

COMMUNES_VOISINES = [
    "verrieres", "le rozier", "rozier", "la cavalerie", "cavalerie", "nant", "saint georges de luzencon",
    "creissels", "compeyre", "aguessac", "paulhe", "compregnac", "peyreleau", "veyreau", "mostuejouls",
    "montjaux", "saint rome de cernon", "saint beauzely", "la couvertoirade", "severac", "saint affrique",
    "lapanouse", "viala du pas de jaux", "viala du tarn", "tournemire", "roquefort", "la roque sainte marguerite",
    "candas", "massegros", "meyrueis", "riviere sur tarn", "boyne", "la cresse", "saint leons", "salles curan",
    "lanuejols", "campestre", "le caylar", "saint jean du bruel", "saint laurent de levezou", "la bastide pradines",
    "saint paul des fonts", "saint martin de lenne", "sainte eulalie de cernon", "castelnau pegayrols",
    "saint germain", "saint jean et saint paul", "balsac", "lavernhe",
    "saint georges", "saint sernin", "belmont sur rance", "sauclieres", "le clapier",
]

PRIX_MAX = 290_000          # Budget acquisition cible MDB
SCORE_MINIMUM = 2           # Seuil pour retenir le bien
SCORE_MINIMUM_ALERTE = 4    # Seuil pour déclencher une notification Telegram/Discord immédiate
EXCLURE_TERRAINS_SEULS = True  # Écarte automatiquement les annonces qui concernent uniquement des terrains nus
MAX_PAGES_PAR_AGENCE = 5
MAX_FICHES_PAR_RUN = 200
VERSION_PARSEUR = 10        # Incrémentée : force la réanalyse des fiches écartées à tort (v9)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# ALERTES SMARTPHONE (laisser vide si non utilisé ou définir dans GitHub Secrets)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")

PATTERN_FICHE_DEFAUT = (
    r"(/annonce|/bien/|/vente/.+|/detail|/property/|/fiche|/produit|ref-|/\d{4,}|-r\d+)"
)

AGENCES = [
    {
        "nom": "Roques",
        "url": "https://www.roques-immobilier.com/vente/435-millau/1",
        "pattern_fiche": r"/vente/435-millau/[^/]+/\d+-",
        "mode": "pattern",
    },
    {"nom": "SGA", "url": "https://www.sga-immobilier.com/immobilier/immobilier-vente-millau.htm"},
    {"nom": "JMB", "url": "https://www.jmb-immobilier.com/resultats?transac=vente", "mode": "prix"},
    {
        "nom": "Notaires",
        "url": "https://www.immobilier.notaires.fr/fr/annonces-immobilieres-liste?typeTransaction=VENTE,VNI,VAE",
        "mode": "prix",
        "preparation": "filtre_ville",
        "ville": "Millau",
    },
    {"nom": "Mesnard maisons", "url": "https://mesnard-immobilier.com/property-type/maison/", "pattern_fiche": r"/property/"},
    {"nom": "Mesnard apparts", "url": "https://mesnard-immobilier.com/property-type/appartement/", "pattern_fiche": r"/property/"},
    {
        "nom": "iad",
        "url": "https://www.iadfrance.fr/annonces/millau-12100/vente",
        "pattern_fiche": r"/annonce/[^/]*millau[^/]*/r\d+",
        "mode": "pattern",
        "page_param": "page",
    },
    {
        "nom": "Expertimo",
        "url": "https://www.reseau-expertimo.fr/vente/435-millau/1",
        "pattern_fiche": r"/vente/\d+-millau/(?:[^/]+/)+\d{4,}-",
        "mode": "auto",
    },
    {
        "nom": "AP Immobilier",
        "url": "https://www.apimmobilier.fr/recherche/",
        "mode": "prix",
    },
]

EXCLUS = re.compile(
    r"(contact|mentions|legal|cgu|cgv|confidential|cookies|estimation|connexion|login|"
    r"inscri|actualit|blog|recrut|honoraires|facebook|instagram|linkedin|twitter|"
    r"youtube|mailto:|tel:|javascript:|\.pdf$|\.jpg$|\.png$)",
    re.I,
)

MOTS_CLES_MDB = {
    r"\bimmeuble( de rapport)?\b|\bmono-?propri[ée]t[ée]\b": 4,
    r"\bdivisible\b|\bdivision\b|\bd[ée]tachable\b|\bplusieurs lots\b": 4,
    r"\bplateau( brut)?\b|\bcombles am[ée]nageables?\b|\bgrenier\b": 3,
    r"\bgrange\b|\bhangar\b|\batelier\b|\bremise\b|\bgarage double\b": 2,
    r"\bsuccession\b|\bvendeur press[ée]\b|\bvente urgente\b|\bopportunit[ée]\b": 3,
    r"\bn[ée]gociable\b|\bbaisse de prix\b|\bprix r[ée]vis[ée]\b": 2,
    r"\b(à|a) r[ée]nover\b|\bgros travaux\b|\br[ée]habilitation compl[èe]te\b": 3,
    r"\btravaux\b|\bà rafra[iî]chir\b|\bà restaurer\b": 2,
}

MOTS_REDHIBITOIRES = re.compile(
    r"\b(viager|vendu|vendue|sous compromis|compromis sign[ée]|sous offre|offre accept[ée]e)\b", re.I
)

RE_AGRICOLE = re.compile(
    r"\b(?:terres?\s+agricoles?|parcelles?\s+agricoles?|terrains?\s+agricoles?|"
    r"terrains?\s+viticoles?|parcelles?\s+viticoles?|vignes?\s+(?:a|à)\s+vendre|"
    r"bois\s+(?:et|à)\s+forêt|for[êe]ts?\s+(?:a|à)\s+vendre|parcelles?\s+de\s+bois|"
    r"prairies?|inconstructibles?|zone\s+agricole|zone\s+naturelle)\b",
    re.I
)

RE_TERRAIN_PUR = re.compile(
    r"^\s*(?:vente\s+)?(?:terrain|parcelle)\b|\bterrain\s+(?:[àa]\s+b[âa]tir|constructible|agricole|de\s+loisir|nu|viabilis[ée])\b|\bparcelle\s+(?:nue|agricole|constructible)\b|/(?:terrain|terrains|terrain-[^/]+|parcelle)[/-]",
    re.I
)

RE_BATI_PRESENT = re.compile(
    r"\b(?:maison|immeuble|bâtisse|batisse|grange|hangar|atelier|remise|villa|appartement|corps de ferme|propriété|ruine|mazet|garage|local|plateau|chalet)\b",
    re.I
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("scraper_mdb")
REJETS: Counter = Counter()

# --------------------------------------------------------------------------- #
# MODÈLE DE DONNÉES ENRICHI
# --------------------------------------------------------------------------- #
@dataclass
class Annonce:
    url: str
    agence: str
    titre: str = ""
    prix: int | None = None
    prix_initial: int | None = None
    baisse_prix_pct: float = 0.0
    historique_prix: list = field(default_factory=list)
    surface: float | None = None
    terrain: float | None = None
    prix_m2: int | None = None
    dpe: str = "INCONNU"
    score: int = 0
    score_v3: float = 0.0
    marge_estimee_montant: int = 0
    marge_estimee_pct: float = 0.0
    mots_detectes: str = ""
    nouvelle: bool = False
    a_baisse: bool = False
    date_collecte: str = ""
    localisation: str = "Millau"
    v: int = VERSION_PARSEUR
    photos: list = field(default_factory=list)

CHAMPS_ANNONCE = {f.name for f in fields(Annonce)}

# --------------------------------------------------------------------------- #
# NOTIFICATIONS TELEGRAM & DISCORD
# --------------------------------------------------------------------------- #
def envoyer_alerte_push(annonce: Annonce, motif: str = "PÉPITE DÉTECTÉE"):
    texte = (
        f"🚨 <b>MILLAU MDB : {motif} !</b>\n"
        f"🏢 <b>{annonce.titre}</b>\n"
        f"📍 {annonce.agence} | DPE : <b>{annonce.dpe}</b>\n"
        f"💰 Prix : <b>{annonce.prix:,} €</b> ({annonce.prix_m2 or '?'} €/m²)\n"
    )
    if annonce.marge_estimee_montant > 0:
        texte += f"📈 Marge Prévisionnelle : <b>+{annonce.marge_estimee_montant:,} € ({annonce.marge_estimee_pct:.1f}%)</b> | Score V3 : <b>{annonce.score_v3}/10</b>\n"
    if annonce.baisse_prix_pct > 0 and annonce.prix_initial:
        texte += f"📉 Baisse constatée : <b>-{annonce.baisse_prix_pct:.1f}%</b> (ancien : {annonce.prix_initial:,} €)\n"
    texte += f"🏷 Signaux : {annonce.mots_detectes}\n🔗 {annonce.url}"

    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            url_api = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            payload = json.dumps({
                "chat_id": TELEGRAM_CHAT_ID,
                "text": texte,
                "parse_mode": "HTML",
                "disable_web_page_preview": False
            }).encode("utf-8")
            req = urllib.request.Request(url_api, data=payload, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=5):
                pass
            log.info("📲 Alerte Telegram envoyée !")
        except Exception as e:
            log.warning("Échec notification Telegram : %s", e)

    if DISCORD_WEBHOOK_URL:
        try:
            payload = json.dumps({"content": texte.replace("<b>", "**").replace("</b>", "**")}).encode("utf-8")
            req = urllib.request.Request(DISCORD_WEBHOOK_URL, data=payload, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=5):
                pass
            log.info("📲 Alerte Discord envoyée !")
        except Exception as e:
            log.warning("Échec notification Discord : %s", e)

# --------------------------------------------------------------------------- #
# OUTILS EXTRACTION & NAVIGATION
# --------------------------------------------------------------------------- #
def normaliser_url(url: str) -> str:
    url, _ = urldefrag(url)
    return url.rstrip("/")

def pause(a: float = 0.8, b: float = 2.0) -> None:
    import time
    time.sleep(random.uniform(a, b))

def accepter_cookies(page) -> None:
    motif = re.compile(r"^\s*(tout accepter|accepter|j.accepte|ok|continuer sans accepter)\s*$", re.I)
    try:
        btn = page.locator("button, a, input[type=button]").filter(has_text=motif).first
        if btn.count() and btn.is_visible(timeout=1000):
            btn.click(timeout=2000)
            page.wait_for_timeout(500)
    except Exception:
        pass

JS_NB_PAR_PAGE = r"""() => {
  document.querySelectorAll('select').forEach(sel => {
    const opts = [...sel.options];
    const tous = opts.find(o => /^\s*(tous|tout|all)\s*$/i.test(o.text));
    const autres = opts.filter(o => o !== tous);
    if (tous && autres.length && autres.every(o => /^\s*\d+\s*$/.test(o.text)) && sel.value !== tous.value) {
      sel.value = tous.value;
      sel.dispatchEvent(new Event('change', {bubbles: true}));
    }
  });
}"""

def afficher_tout(page) -> None:
    try:
        page.evaluate(JS_NB_PAR_PAGE)
        page.wait_for_timeout(1500)
    except Exception:
        pass
    motif = re.compile(r"(voir|charger|afficher) (plus|davantage|la suite)|plus (de biens|de résultats|d.annonces)", re.I)
    for _ in range(8):
        try:
            btn = page.locator("button, a").filter(has_text=motif).first
            if btn.count() and btn.is_visible(timeout=500):
                btn.click(timeout=2000)
                page.wait_for_timeout(1200)
            else:
                break
        except Exception:
            break

def charger_page(page, url: str, tentatives: int = 3) -> bool:
    for i in range(1, tentatives + 1):
        try:
            page.goto(url, timeout=30_000, wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=6_000)
            except PWTimeout:
                pass
            return True
        except Exception as e:
            log.warning("Tentative %d/%d pour %s : %s", i, tentatives, url, e)
            pause(2, 4)
    return False

def scroller_jusqu_au_bout(page, max_tours: int = 6) -> None:
    derniere = 0
    for _ in range(max_tours):
        hauteur = page.evaluate("document.body.scrollHeight")
        if hauteur == derniere:
            break
        derniere = hauteur
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(800)

JS_CARTES_PRIX = r"""() => {
  const out = new Set();
  document.querySelectorAll('a[href]').forEach(a => {
    let el = a;
    for (let i = 0; i < 5 && el; i++, el = el.parentElement) {
      const t = el.innerText || '';
      if (t.length > 1500) break;
      if (/\d[\d\s.\u00a0\u202f]*(€|euros?)/i.test(t)) { out.add(a.href); break; }
    }
  });
  return [...out];
}"""

def _domaine(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")

def _filtrer_liens(hrefs: list[str], base_url: str) -> set[str]:
    domaine, base = _domaine(base_url), normaliser_url(base_url)
    liens = set()
    for href in hrefs:
        href = normaliser_url(href)
        if urlparse(href).scheme not in ("http", "https"):
            continue
        if _domaine(href) != domaine or href == base or EXCLUS.search(href):
            continue
        liens.add(href)
    return liens

def extraire_liens_fiches(page, base_url: str, pattern: re.Pattern, mode: str = "auto") -> set[str]:
    hrefs = page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
    par_pattern = {
        h for h in _filtrer_liens(hrefs, base_url)
        if pattern.search(urlparse(h).path + (f"?{urlparse(h).query}" if urlparse(h).query else ""))
    }
    if mode == "pattern":
        return par_pattern
    par_prix = _filtrer_liens(page.evaluate(JS_CARTES_PRIX), base_url)
    if mode == "prix":
        return par_prix
    return par_pattern or par_prix

def aller_page_suivante(page, num_page: int = 1) -> bool:
    selecteurs = [
        "a[rel='next']", "a.next", "li.next a", ".pagination a.next",
        "a:has-text('Suivant')", "a:has-text('suivant')", "a:has-text('›')", "a:has-text('»')",
    ]
    for sel in selecteurs:
        try:
            el = page.locator(sel).first
            if el.count() and el.is_visible(timeout=500):
                el.click(timeout=3000)
                page.wait_for_load_state("domcontentloaded", timeout=12_000)
                page.wait_for_timeout(1000)
                return True
        except Exception:
            continue
    try:
        suivant = re.compile(rf"^\s*{num_page + 1}\s*$")
        el = page.locator('[class*="pagin" i] a, nav[aria-label*="pag" i] a, ul.pagination a').filter(has_text=suivant).first
        if el.count() and el.is_visible(timeout=500):
            el.click(timeout=3000)
            page.wait_for_load_state("domcontentloaded", timeout=12_000)
            page.wait_for_timeout(1000)
            return True
    except Exception:
        pass
    return False

SELECTEURS_CHAMP_VILLE = [
    'input[placeholder*="ville" i]', 'input[placeholder*="localisation" i]', 'input[placeholder*="où" i]',
    'input[placeholder*="code postal" i]', 'input[aria-label*="ville" i]', 'input[aria-label*="localisation" i]',
    'input[name*="ville" i]', 'input[id*="ville" i]', 'input[type="search"]',
]

def filtrer_par_ville(page, ville: str = "Millau") -> bool:
    try:
        champ = None
        for sel in SELECTEURS_CHAMP_VILLE:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible(timeout=1000):
                champ = loc
                break
        if champ is None:
            return False
        champ.click(timeout=2000)
        champ.fill("")
        champ.press_sequentially(ville, delay=100)
        page.wait_for_timeout(1500)
        sugg = page.locator('[role="option"], li[class*="suggest" i], li').filter(has_text=re.compile(re.escape(ville), re.I)).first
        if sugg.count() and sugg.is_visible(timeout=800):
            sugg.click(timeout=2000)
        else:
            champ.press("Enter")
        page.wait_for_timeout(1500)
        return True
    except Exception:
        return False

PREPARATIONS = {"filtre_ville": filtrer_par_ville}

def get_liens_agences_locales(context) -> dict[str, str]:
    resultats: dict[str, str] = {}
    page = context.new_page()

    for agence in AGENCES:
        nom, url = agence["nom"], agence["url"]
        pattern = re.compile(agence.get("pattern_fiche", PATTERN_FICHE_DEFAUT), re.I)
        log.info("▶ %s : %s", nom, url)

        if not charger_page(page, url):
            continue
        mode = agence.get("mode", "auto")
        accepter_cookies(page)

        prep = agence.get("preparation")
        if prep and not PREPARATIONS[prep](page, agence.get("ville", "Millau")):
            log.warning("   ✗ préparation '%s' impossible pour %s (agence ignorée)", prep, nom)
            continue

        for num_page in range(1, MAX_PAGES_PAR_AGENCE + 1):
            afficher_tout(page)
            scroller_jusqu_au_bout(page)
            nouveaux = extraire_liens_fiches(page, url, pattern, mode) - set(resultats)
            for lien in nouveaux:
                resultats[lien] = nom
            log.info("   page %d : +%d liens", num_page, len(nouveaux))
            if not nouveaux and num_page > 1:
                break
            page_param = agence.get("page_param")
            if page_param:
                sep = "&" if "?" in url else "?"
                if not charger_page(page, f"{url}{sep}{page_param}={num_page + 1}"):
                    break
            elif not aller_page_suivante(page, num_page):
                break
            pause()
        log.info("   → Total : %d fiches pour %s", len([k for k, v in resultats.items() if v == nom]), nom)
        pause()

    page.close()
    return resultats

JS_PRIX_CANONIQUE = r"""() => {
  const bon = v => Number.isFinite(v) && v >= 15000 && v <= 5000000;
  const toNum = s => {
    const t = String(s).replace(/[\s\u00a0\u202f]/g, '').replace(',', '.');
    const f = parseFloat(t);
    return Number.isFinite(f) ? Math.round(f) : NaN;
  };

  for (const el of document.querySelectorAll('meta[property="og:price:amount"], meta[property="product:price:amount"], meta[itemprop="price"]')) {
    const val = toNum(el.content || '');
    if (bon(val)) return val;
  }

  for (const script of document.querySelectorAll('script[type="application/ld+json"]')) {
    try {
      const data = JSON.parse(script.textContent);
      const items = [data, data && data.offers, data && data['@graph']].flat().filter(Boolean);
      for (const item of items) {
        const offers = Array.isArray(item.offers) ? item.offers[0] : item.offers;
        const p = item.price || (offers && offers.price);
        if (p) {
          const val = toNum(p);
          if (bon(val)) return val;
        }
      }
    } catch (e) {}
  }

  for (const el of document.querySelectorAll('input[name*="prix" i], input[id*="prix" i]')) {
    const val = toNum(el.value || '');
    if (bon(val)) return val;
  }

  const sels = [
    '.fiche-prix', '.prix-bien', '.detail-prix', '.price-val', '.bien-prix',
    '.price', '[class*="price" i]', '[class*="prix" i]'
  ];
  for (const sel of sels) {
    for (const el of document.querySelectorAll(sel)) {
      const t = el.innerText || '';
      if (t.length > 80) continue;
      const m = t.match(/(\d[\d\s.\u00a0\u202f]*)\s*(?:€|euros?)/i);
      if (m) {
        const val = parseInt(m[1].replace(/\D/g, ''), 10);
        if (bon(val)) return val;
      }
    }
  }
  return null;
}"""

_NUM = r"\d{1,3}(?:[ \u00a0\u202f.,]\d{3})+|\d{4,8}"
RE_PRIX = re.compile(
    rf"(?:€\s*({_NUM})(?![\d])|({_NUM})(?:[.,]\d{{1,2}})?\s*(?:€|euros?\b|eur\b))", re.I
)
CTX_POSITIF = re.compile(r"\bprix\b|\bvente\b|\bf\.a\.i\b|\bnet vendeur\b", re.I)
CTX_NEGATIF = re.compile(r"\bhonoraires?\b|\bfrais\b|\bcharges?\b|\btaxe\b|\bfonci[èe]re\b|\bloyer\b|\bpar mois\b|\bmensualit", re.I)
RE_SURFACE = re.compile(r"(\d{2,4}(?:[.,]\d{1,2})?)\s*m(?:²|2)\b", re.I)
RE_TERRAIN = re.compile(r"(?:terrain|parcelle|jardin)[^\d]{0,40}(\d{2,6}(?:[.,]\d{1,2})?)\s*m(?:²|2)", re.I)

RE_DPE = re.compile(
    r"(?:classe|[ée]tiquette|dpe|consommation)\s*(?:[ée]nerg[ée]tique)?\s*[:=\-\s]\s*([A-G]|vierge|non soumis)"
    r"|\b([A-G])\s*(?:\([0-9]{2,3}\s*kwh|kwh/m)",
    re.I
)

def extraire_dpe(texte: str, html: str = "") -> str:
    m = RE_DPE.search(texte)
    if m:
        lettre = (m.group(1) or m.group(2) or "").strip().upper()
        if lettre in ("A", "B", "C", "D", "E", "F", "G"):
            return lettre
        if "VIERGE" in lettre:
            return "VIERGE"
    m_html = re.search(r'class="[^"]*(?:dpe|energy)[^"]*([a-g])\b', html, re.I)
    if m_html:
        return m_html.group(1).upper()
    return "INCONNU"

def extraire_prix_fallback(texte: str) -> int | None:
    candidats = []
    for m in RE_PRIX.finditer(texte):
        val = int(re.sub(r"\D", "", m.group(1) or m.group(2)))
        if not 15_000 <= val <= 3_000_000:
            continue
        ctx = texte[max(0, m.start() - 50): m.end() + 30]
        if CTX_NEGATIF.search(ctx):
            continue
        pts = 2 if CTX_POSITIF.search(ctx) else 0
        candidats.append((pts, -m.start(), val))
    if candidats:
        return max(candidats)[2]
    return None

class PageListe(Exception):
    def __init__(self, liens=()):
        super().__init__("page liste")
        self.liens = set(liens)

class FicheInjoignable(Exception):
    pass

JS_CONTENU = r"""() => {
  const cands = [...document.querySelectorAll('main, article, #content, .content')];
  const root = cands.sort((a, b) => b.textContent.length - a.textContent.length)[0] || document.body;
  const c = root.cloneNode(true);
  c.querySelectorAll('header, footer, nav, aside, script, style, [class*=footer], [class*=header], [class*=menu]').forEach(e => e.remove());
  return (c.textContent || '').replace(/\s+/g, ' ').trim();
}"""

def _norm(t: str) -> str:
    t = unicodedata.normalize("NFD", t.lower())
    t = "".join(ch for ch in t if unicodedata.category(ch) != "Mn")
    return re.sub(r"[^a-z0-9]+", " ", t).strip()

RE_PROXIMITE = re.compile(r"\b(?:a|au|proche|quelques minutes|environs|vers)\s+de\s+millau", re.I)
RE_COMMUNES_VOISINES = re.compile(r"\b(" + "|".join(sorted(map(re.escape, COMMUNES_VOISINES), key=len, reverse=True)) + r")\b")

def evaluer_localisation(url: str, h1: str, contenu: str) -> str:
    path = urlparse(url).path
    if re.search(r"/\d+-millau/|-millau-", path, re.I):
        return "oui"
    h1n = RE_PROXIMITE.sub(" ", _norm(h1))
    if "millau" in h1n and not RE_COMMUNES_VOISINES.search(h1n):
        return "oui"
    t = RE_PROXIMITE.sub(" ", _norm(f"{h1} {contenu[:2500]}"))
    if "millau" in t and not RE_COMMUNES_VOISINES.search(t):
        return "oui"
    if RE_COMMUNES_VOISINES.search(t):
        return "non"
    return "oui" if "millau" in t else "incertain"

def analyser_fiche(page, url: str, agence: str, etat_existant: dict) -> Annonce | None:
    if not charger_page(page, url, tentatives=2):
        raise FicheInjoignable(url)

    try:
        page.wait_for_function("document.body && /€|euro/i.test(document.body.innerText)", timeout=4000)
    except PWTimeout:
        pass

    try:
        titre = h1 = page.locator("h1").first.inner_text(timeout=2000).strip()
    except Exception:
        titre, h1 = page.title(), ""

    texte = page.inner_text("body")
    contenu = page.evaluate(JS_CONTENU)

    m_exclu = MOTS_REDHIBITOIRES.search(f"{h1} {contenu[:1000]}")
    if m_exclu:
        REJETS["rédhibitoire (vendu/compromis/viager)"] += 1
        log.info("   ✗ écartée (%s) : %s", m_exclu.group(0), url)
        return None

    statut = evaluer_localisation(url, h1, contenu)
    if statut == "non" or (statut == "incertain" and LOCALISATION_STRICTE):
        REJETS[f"localisation {statut}"] += 1
        log.info("   ✗ écartée (localisation %s) : %s", statut, url)
        return None

    texte_verif = f"{titre} {h1} {urlparse(url).path}"

    if RE_AGRICOLE.search(texte_verif) and not RE_BATI_PRESENT.search(texte_verif):
        REJETS["agricole"] += 1
        log.info("   ✗ écartée (bien ou terrain agricole exclu pour MDB) : %s", url)
        return None

    if RE_TERRAIN_PUR.search(texte_verif) and not RE_BATI_PRESENT.search(texte_verif):
        REJETS["terrain sans bâti"] += 1
        log.info("   ✗ écartée (terrain sans bâti) : %s", url)
        return None

    base = contenu if len(contenu) >= 200 else texte
    prix = page.evaluate(JS_PRIX_CANONIQUE)
    if prix is None:
        prix = extraire_prix_fallback(base)
    if prix is None:
        REJETS["prix introuvable"] += 1
        log.info("   ✗ écartée (prix introuvable) : %s", url)
        return None

    surfaces = [float(s.replace(",", ".")) for s in RE_SURFACE.findall(base)]
    surface = next((s for s in surfaces if 15 <= s <= 1500), None)
    m_terrain = RE_TERRAIN.search(base)
    terrain = float(m_terrain.group(1).replace(",", ".")) if m_terrain else None

    if surface is None or surface < 25:
        if re.search(r"\b(?:terrain|parcelle|foncier|agricole)\b", f"{titre} {h1}", re.I) and not RE_BATI_PRESENT.search(f"{titre} {h1}"):
            REJETS["sans surface habitable"] += 1
            log.info("   ✗ écartée (absence de surface habitable suffisante) : %s", url)
            return None
    dpe = extraire_dpe(base, page.content())

    now_str = datetime.now().strftime("%d/%m/%Y")
    historique = []
    prix_initial = prix
    baisse_prix_pct = 0.0
    a_baisse = False

    if url in etat_existant:
        anc = etat_existant[url]
        if anc.get("v") != VERSION_PARSEUR:
            prix_initial = prix
            historique = [{"date": now_str, "prix": prix}]
            a_baisse = False
            baisse_prix_pct = 0.0
        else:
            prix_initial = anc.get("prix_initial") or anc.get("prix") or prix
            historique = anc.get("historique_prix", [])
            ancien_prix = anc.get("prix")

            if ancien_prix and prix < ancien_prix:
                delta_euros = ancien_prix - prix
                delta_pct = ((ancien_prix - prix) / ancien_prix) * 100

                if delta_euros >= 3000 and 3.0 <= delta_pct <= 35.0:
                    a_baisse = True
                    baisse_prix_pct = round(((prix_initial - prix) / prix_initial) * 100, 1)
                    log.info("📉 VRAIE BAISSE DE PRIX VALIDÉE (-%.1f%%) sur %s : %d € -> %d € (-%d €)", baisse_prix_pct, url, ancien_prix, prix, delta_euros)
                else:
                    log.warning("⚠️ Écart de prix ignoré (faux positif évité) sur %s : %d € vs %d € (delta: %d € / %.1f%%)", url, ancien_prix, prix, delta_euros, delta_pct)

    if not historique or historique[-1].get("prix") != prix:
        historique.append({"date": now_str, "prix": prix})

    score, detectes = 0, []
    texte_bas = f"{titre} {base}".lower()

    for regex, points in MOTS_CLES_MDB.items():
        m = re.search(regex, texte_bas, re.I)
        if m:
            score += points
            detectes.append(m.group(0).strip())

    if dpe == "G":
        score += 4
        detectes.append("Passoire G (Audit & Travaux)")
    elif dpe == "F":
        score += 3
        detectes.append("Passoire F")

    if a_baisse or baisse_prix_pct >= 5:
        score += 3
        detectes.append(f"Baisse -{baisse_prix_pct:.0f}%")

    prix_m2 = int(prix / surface) if prix and surface else None
    if prix_m2 and prix_m2 < 1100:
        score += 3
        detectes.append(f"Prix canon {prix_m2} €/m²")

    marge_estimee_montant = 0
    marge_estimee_pct = 0.0
    score_v3 = float(score)

    if surface and surface > 20 and prix:
        ca_estime = int(surface * 1850)
        cout_travaux_m2 = 900 if dpe == "G" else (750 if dpe == "F" else 600)
        total_travaux = int(surface * cout_travaux_m2)
        frais_mdb = int((prix * 0.022) + 3000 + (prix * 0.035))
        cout_revient = prix + total_travaux + frais_mdb
        marge_estimee_montant = ca_estime - cout_revient
        marge_estimee_pct = round((marge_estimee_montant / ca_estime) * 100, 1) if ca_estime > 0 else 0.0

        pts_marge = 35 if marge_estimee_pct >= 22 else (30 if marge_estimee_pct >= 18 else (20 if marge_estimee_pct >= 14 else (10 if marge_estimee_pct >= 10 else 0)))
        pts_decoupe = 25 if "immeuble" in texte_bas or "monopropri" in texte_bas else (18 if "divisible" in texte_bas or "plateau" in texte_bas else 10)
        pts_negoc = (8 if dpe in ("F", "G") else 0) + (8 if baisse_prix_pct >= 10 else (4 if baisse_prix_pct >= 5 else 0)) + (4 if prix_m2 and prix_m2 < 800 else 0)
        pts_securite = (8 if "immeuble" in texte_bas else 4) + (6 if prix <= 150000 else 3) + 4
        score_v3 = round(min(10.0, (pts_marge + pts_decoupe + pts_negoc + pts_securite) / 10.0), 1)

    # EXTRACTION DES PHOTOS DE L'ANNONCE
    try:
        photos = page.evaluate("""() => {
            return [...document.querySelectorAll('img')]
                .map(img => img.src)
                .filter(src => src && (src.includes('.jpg') || src.includes('.webp') || src.includes('photo') || src.includes('pictures')))
                .filter(src => !src.includes('logo') && !src.includes('icon') && !src.includes('avatar'))
                .slice(0, 4);
        }""")
    except Exception:
        photos = []

    return Annonce(
        url=url, agence=agence, titre=titre[:140], prix=prix,
        prix_initial=prix_initial, baisse_prix_pct=baisse_prix_pct,
        historique_prix=historique, a_baisse=a_baisse,
        surface=surface, terrain=terrain, prix_m2=prix_m2, dpe=dpe,
        score=score, score_v3=score_v3, marge_estimee_montant=marge_estimee_montant,
        marge_estimee_pct=marge_estimee_pct,
        mots_detectes=", ".join(dict.fromkeys(detectes)),
        date_collecte=datetime.now().strftime("%Y-%m-%d %H:%M"),
        localisation="Millau",
        photos=photos
    )

# --------------------------------------------------------------------------- #
# EXPORT HTML MULTI-ONGLETS & SIMULATEUR EMBARQUÉ (GITHUB PAGES)
# --------------------------------------------------------------------------- #
TEMPLATE_HTML = r"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MDB Scanner Pro – Millau (12100)</title>
<script src="https://cdn.tailwindcss.com"></script>
<style>
  :root { color-scheme: dark; }
</style>
</head>
<body class="bg-slate-950 text-slate-100 min-h-screen font-sans pb-16">
  <!-- HEADER -->
  <header class="bg-slate-900 border-b border-slate-800 sticky top-0 z-30 shadow-lg">
    <div class="max-w-7xl mx-auto px-4 py-3 flex flex-wrap items-center justify-between gap-3">
      <div class="flex items-center gap-3">
        <div class="h-10 w-10 rounded-xl bg-amber-500 flex items-center justify-center text-slate-950 font-black text-lg shadow-md shadow-amber-500/20">
          MDB
        </div>
        <div>
          <div class="text-base font-bold text-white flex items-center gap-2">
            MDB Scanner Pro <span class="text-[10px] font-semibold px-2 py-0.5 rounded-full bg-amber-500/20 text-amber-300 border border-amber-500/30">Millau 12100</span>
          </div>
          <div class="text-xs text-slate-400">Actualisé le : __DATE__</div>
        </div>
      </div>

      <nav class="flex space-x-1 sm:space-x-2">
        <button onclick="setTab('deals')" id="btn-tab-deals" class="px-3.5 py-1.5 rounded-lg text-xs font-bold transition bg-amber-500 text-slate-950 cursor-pointer">
          🏢 Annonces & Deals
        </button>
        <button onclick="setTab('audit_profond')" id="btn-tab-audit_profond" class="px-3.5 py-1.5 rounded-lg text-xs font-semibold transition text-slate-300 hover:bg-slate-800 cursor-pointer">
          🔬 Audit Approfondi
        </button>
        <button onclick="setTab('simu')" id="btn-tab-simu" class="px-3.5 py-1.5 rounded-lg text-xs font-semibold transition text-slate-300 hover:bg-slate-800 cursor-pointer">
          🧮 Simulateur MDB
        </button>
        <button onclick="setTab('audit')" id="btn-tab-audit" class="px-3.5 py-1.5 rounded-lg text-xs font-semibold transition text-slate-300 hover:bg-slate-800 cursor-pointer">
          🎯 Stratégie MDB
        </button>
      </nav>
    </div>
  </header>

  <main class="max-w-7xl mx-auto px-4 py-6">
    <!-- ONGLET 1 : ANNONCES & DEALS -->
    <div id="tab-deals" class="space-y-6">
      <div class="grid grid-cols-2 sm:grid-cols-4 gap-3" id="kpi-cards"></div>
      <div class="bg-slate-900 p-4 rounded-2xl border border-slate-800 flex flex-wrap gap-3 items-center justify-between">
        <input type="text" id="q" oninput="renderDeals()" placeholder="Rechercher par rue, mot-clé (ex: 'rue droite', 'immeuble')..." class="px-3 py-2 bg-slate-950 border border-slate-700 rounded-xl text-xs text-white placeholder-slate-400 flex-1 min-w-[200px] focus:outline-none focus:border-amber-500" />
        <select id="f-type" onchange="renderDeals()" class="px-3 py-2 bg-slate-950 border border-slate-700 rounded-xl text-xs text-slate-200 focus:outline-none focus:border-amber-500">
          <option value="all">Tous types de biens</option>
          <option value="immeuble">🏢 Immeubles</option>
          <option value="maison">🏡 Maisons</option>
          <option value="appartement">🏢 Appartements</option>
          <option value="grange">🏚️ Granges / Plateaux</option>
        </select>
        <select id="f-sort" onchange="renderDeals()" class="px-3 py-2 bg-slate-950 border border-slate-700 rounded-xl text-xs text-amber-300 font-semibold focus:outline-none focus:border-amber-500">
          <option value="score_desc">⚡ Score MDB V3 décroissant</option>
          <option value="marge_desc">💰 Marge potentielle décroissante</option>
          <option value="prix_asc">💶 Prix croissant</option>
          <option value="prix_desc">💶 Prix décroissant</option>
          <option value="m2_asc">📐 Prix / m² croissant</option>
          <option value="m2_desc">📐 Prix / m² décroissant</option>
          <option value="surface_desc">📏 Surface décroissante</option>
        </select>
        <select id="f-dpe" onchange="renderDeals()" class="px-3 py-2 bg-slate-950 border border-slate-700 rounded-xl text-xs text-slate-200 focus:outline-none focus:border-amber-500">
          <option value="all">Tous DPE</option>
          <option value="passoires">🔥 Passoires F & G</option>
          <option value="G">DPE G uniquement</option>
          <option value="F">DPE F uniquement</option>
        </select>
        <label class="flex items-center gap-1.5 text-xs text-slate-300 cursor-pointer select-none">
          <input type="checkbox" id="f-drops" onchange="renderDeals()" class="rounded bg-slate-800 border-slate-700 text-amber-500" />
          <span>Baisses de prix uniquement</span>
        </label>
      </div>
      <div class="grid grid-cols-1 md:grid-cols-2 gap-4" id="deals-grid"></div>
    </div>

    <!-- ONGLET 2 : AUDIT APPROFONDI -->
    <div id="tab-audit_profond" class="hidden space-y-6">
      <div class="bg-slate-900 p-6 rounded-2xl border border-slate-800">
        <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-4 mb-6 pb-4 border-b border-slate-800">
          <div>
            <span class="px-3 py-1 rounded-full bg-amber-500/10 border border-amber-500/30 text-amber-400 text-xs font-semibold uppercase tracking-wider inline-flex items-center gap-1.5 mb-2">
              🔬 Audit Approfondi & Arbitrage MDB
            </span>
            <h2 class="text-xl font-bold text-white flex items-center gap-2">
              <span id="ap-titre">Sélectionnez un bien pour lancer l’audit</span>
            </h2>
            <p class="text-xs text-slate-400 mt-1" id="ap-soustitre">Analyse architecturale, chiffrage des travaux, 3 stratégies de marge et comparaison de décision.</p>
          </div>
          <div class="flex items-center gap-2">
            <select id="ap-select-deal" onchange="changerDealAudit(this.value)" class="bg-slate-950 border border-slate-700 text-amber-300 font-bold text-xs rounded-xl px-3 py-2 focus:outline-none focus:ring-1 focus:ring-amber-500 cursor-pointer">
              <option value="">-- Choisir un bien dans la veille --</option>
            </select>
            <button onclick="window.print()" class="px-3.5 py-2 bg-slate-800 hover:bg-slate-700 text-slate-200 text-xs font-semibold rounded-xl border border-slate-700 cursor-pointer">
              🖨️ PDF
            </button>
          </div>
        </div>

        <div class="grid grid-cols-1 lg:grid-cols-3 gap-6 mb-8">
          <!-- PHOTOS -->
          <div class="lg:col-span-2 bg-slate-950 p-4 rounded-xl border border-slate-800 space-y-3">
            <h3 class="text-sm font-bold text-white flex items-center gap-2">
              📸 Analyse Visuelle : Photos extraites de l'annonce
            </h3>
            <div class="grid grid-cols-2 sm:grid-cols-4 gap-2" id="ap-photos-grid">
              <!-- Photos injectées -->
            </div>
            <div class="text-xs text-slate-400 p-3 bg-slate-900 rounded-lg border border-slate-800/80" id="ap-photos-constat">
              Chargement des constats visuels...
            </div>
          </div>

          <!-- DIAGNOSTICS -->
          <div class="bg-slate-950 p-4 rounded-xl border border-slate-800 space-y-3 text-xs">
            <h3 class="text-sm font-bold text-amber-400 flex items-center gap-2">
              📋 État des Lieux & Diagnostics
            </h3>
            <div class="space-y-2" id="ap-diagnostics-list">
              <!-- Diagnostics injectés -->
            </div>
          </div>
        </div>

        <!-- ESTIMATIF TRAVAUX -->
        <div class="mb-8">
          <div class="flex items-center justify-between mb-3">
            <h3 class="text-sm font-bold text-white flex items-center gap-2">
              🔨 Descriptif Complet & Chiffrage Estimatif des Travaux
            </h3>
            <span class="text-xs font-bold text-amber-300" id="ap-total-travaux-badge">Enveloppe estimée : --</span>
          </div>
          <div class="overflow-x-auto bg-slate-950 rounded-xl border border-slate-800">
            <table class="w-full text-xs text-left">
              <thead class="bg-slate-900 text-slate-400 uppercase text-[10px] border-b border-slate-800">
                <tr>
                  <th class="py-2.5 px-3">Poste de travaux</th>
                  <th class="py-2.5 px-3">Diagnostic constaté</th>
                  <th class="py-2.5 px-3">Préconisation technique MDB</th>
                  <th class="py-2.5 px-3 text-right">Budget estimé</th>
                </tr>
              </thead>
              <tbody id="ap-travaux-tbody" class="divide-y divide-slate-850 text-slate-300">
                <!-- Postes injectés dynamiquement -->
              </tbody>
            </table>
          </div>
        </div>

        <!-- STRATÉGIES INTERACTIVES -->
        <div class="space-y-4">
          <h3 class="text-base font-bold text-white flex items-center gap-2">
            🏆 3 Stratégies de Création de Valeur & Arbitrage MDB
          </h3>
          <div class="grid grid-cols-1 md:grid-cols-3 gap-4 text-xs" id="ap-scenarios-cards">
            <!-- 3 Cartes de stratégies injectées -->
          </div>
          <div class="bg-slate-950 p-5 rounded-xl border border-slate-800 space-y-3 mt-4">
            <div class="flex items-center justify-between">
              <h4 class="text-sm font-bold text-emerald-400">
                🎯 Recommandation & Validation Stratégique
              </h4>
              <span class="text-[11px] text-slate-400 font-mono" id="ap-verdict-delai">Délai : --</span>
            </div>
            <p class="text-xs text-slate-300 leading-relaxed" id="ap-verdict-texte">
              Sélectionnez une annonce pour générer la synthèse décisionnelle.
            </p>
            <div class="pt-2 flex justify-end">
              <button onclick="injecterScenarioDansSimulateur()" id="ap-btn-inject-simu" class="px-4 py-2 bg-amber-500 hover:bg-amber-400 text-slate-950 font-bold text-xs rounded-xl transition shadow-md cursor-pointer">
                Transférer ce scénario dans le Simulateur Financier →
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- ONGLET 3 : SIMULATEUR MDB -->
    <div id="tab-simu" class="hidden space-y-6">
      <div class="bg-slate-900 p-6 rounded-2xl border border-slate-800">
        <div class="flex flex-wrap items-center justify-between gap-3 mb-4">
          <div>
            <h2 class="text-xl font-bold text-white flex items-center gap-2">
              <span>🧮</span> Simulateur Financier d’Opération & Découpe MDB
            </h2>
            <p class="text-xs text-slate-400">Business plan complet marchand de biens : acquisition 0,715%, découpe, travaux, portage, TVA sur marge et IS.</p>
          </div>
          <div class="flex gap-2">
            <button onclick="window.print()" class="px-3 py-1.5 bg-slate-800 hover:bg-slate-700 text-slate-200 text-xs font-semibold rounded-lg border border-slate-700 transition cursor-pointer">
              🖨️ Imprimer
            </button>
            <button onclick="genererAnalyseIA()" class="px-3.5 py-1.5 bg-amber-500 hover:bg-amber-400 text-slate-950 text-xs font-bold rounded-lg transition cursor-pointer shadow-md shadow-amber-500/20">
              ✨ Analyse Stratégique & Marge
            </button>
          </div>
        </div>

        <div class="grid grid-cols-1 lg:grid-cols-12 gap-6">
          <div class="lg:col-span-7 space-y-5">
            <!-- 1. ACQUISITION -->
            <div class="bg-slate-950 p-4 rounded-xl border border-slate-800 space-y-3">
              <h3 class="text-xs font-bold text-amber-400 uppercase tracking-wider flex items-center gap-1.5">
                <span>1. Acquisition & Frais d’acte</span>
              </h3>
              <div class="grid grid-cols-1 sm:grid-cols-3 gap-3 text-xs">
                <div>
                  <label class="block text-slate-400 mb-1">Prix affiché FAI (€)</label>
                  <input type="number" id="sim-prix" value="149000" oninput="calculerSimu()" class="w-full px-3 py-2 bg-slate-900 border border-slate-700 rounded-lg text-white font-mono font-bold" />
                </div>
                <div>
                  <label class="block text-slate-400 mb-1">Négociation visée (€)</label>
                  <input type="number" id="sim-nego" value="14000" oninput="calculerSimu()" class="w-full px-3 py-2 bg-slate-900 border border-slate-700 rounded-lg text-amber-300 font-mono" />
                </div>
                <div>
                  <label class="block text-slate-400 mb-1">Régime notaire</label>
                  <select id="sim-notaire" onchange="calculerSimu()" class="w-full px-3 py-2 bg-slate-900 border border-slate-700 rounded-lg text-white font-mono">
                    <option value="0.00715">MDB (0,715%)</option>
                    <option value="0.078">Particulier (7,8%)</option>
                  </select>
                </div>
              </div>
            </div>

            <!-- 2. LOTS ET DECOUPE -->
            <div class="bg-slate-950 p-4 rounded-xl border border-slate-800 space-y-3">
              <div class="flex items-center justify-between">
                <h3 class="text-xs font-bold text-indigo-400 uppercase tracking-wider">
                  2. Plan de Découpe & Revente Lot par Lot
                </h3>
                <button onclick="ajouterLot()" class="text-[11px] font-bold text-amber-400 hover:text-amber-300 bg-amber-500/10 px-2 py-1 rounded border border-amber-500/20 cursor-pointer">
                  + Ajouter un lot
                </button>
              </div>
              <div class="overflow-x-auto">
                <table class="w-full text-xs text-left">
                  <thead class="text-slate-400 text-[10px] uppercase border-b border-slate-800">
                    <tr>
                      <th class="py-2">Désignation du lot</th>
                      <th class="py-2 w-16">Surf. (m²)</th>
                      <th class="py-2 w-24">Travaux (€)</th>
                      <th class="py-2 w-28">Prix Revente (€)</th>
                      <th class="py-2 w-8"></th>
                    </tr>
                  </thead>
                  <tbody id="lots-table-body" class="divide-y divide-slate-800/60 font-mono"></tbody>
                </table>
              </div>
              <div class="grid grid-cols-3 gap-2 pt-2 border-t border-slate-800/60 text-xs">
                <div class="text-slate-400">Total Surface : <b id="total-surf" class="text-white">-- m²</b></div>
                <div class="text-slate-400">Total Travaux : <b id="total-travaux-lots" class="text-amber-400">-- €</b></div>
                <div class="text-slate-400">Total Revente : <b id="total-revente-lots" class="text-emerald-400">-- €</b></div>
              </div>
            </div>

            <!-- 3. FRAIS ANNEXES, DIVISION ET PORTAGE -->
            <div class="bg-slate-950 p-4 rounded-xl border border-slate-800 space-y-3">
              <h3 class="text-xs font-bold text-slate-300 uppercase tracking-wider">
                3. Frais de Division & Financement / Portage
              </h3>
              <div class="grid grid-cols-2 sm:grid-cols-4 gap-3 text-xs">
                <div>
                  <label class="block text-slate-400 mb-1">Géomètre & EDD (€)</label>
                  <input type="number" id="sim-geometre" value="3200" oninput="calculerSimu()" class="w-full px-2.5 py-1.5 bg-slate-900 border border-slate-700 rounded-lg text-white font-mono" />
                </div>
                <div>
                  <label class="block text-slate-400 mb-1">Durée portage (mois)</label>
                  <input type="number" id="sim-mois" value="8" oninput="calculerSimu()" class="w-full px-2.5 py-1.5 bg-slate-900 border border-slate-700 rounded-lg text-white font-mono" />
                </div>
                <div>
                  <label class="block text-slate-400 mb-1">Taux emprunt (%)</label>
                  <input type="number" step="0.1" id="sim-taux" value="3.8" oninput="calculerSimu()" class="w-full px-2.5 py-1.5 bg-slate-900 border border-slate-700 rounded-lg text-white font-mono" />
                </div>
                <div>
                  <label class="block text-slate-400 mb-1">Apport (€)</label>
                  <input type="number" id="sim-apport" value="35000" oninput="calculerSimu()" class="w-full px-2.5 py-1.5 bg-slate-900 border border-slate-700 rounded-lg text-emerald-400 font-mono" />
                </div>
              </div>
            </div>
          </div>

          <!-- RESULTATS FINANCIERS DETAILLES -->
          <div class="lg:col-span-5 bg-slate-950 p-5 rounded-2xl border border-slate-800 space-y-4 flex flex-col justify-between">
            <div>
              <div class="flex items-center justify-between mb-3">
                <h3 class="text-sm font-bold text-white uppercase tracking-wider">Bilan Financier & Marges</h3>
                <span class="text-[10px] px-2 py-0.5 rounded bg-emerald-500/20 text-emerald-300 font-bold border border-emerald-500/30">MDB V3</span>
              </div>
              <div class="bg-gradient-to-br from-emerald-950/60 to-slate-900 border border-emerald-500/50 p-4 rounded-xl text-center mb-3">
                <span class="text-[11px] text-emerald-300 block font-semibold uppercase tracking-wider">Marge Brute Opération</span>
                <span class="text-3xl font-black text-emerald-400 font-mono" id="res-marge-brute">+68 500 €</span>
                <span class="text-xs text-emerald-300 block mt-1" id="res-marge-pct">22.1% du CA de revente</span>
              </div>
              <div class="grid grid-cols-2 gap-2 mb-3">
                <div class="bg-slate-900 p-2.5 rounded-xl border border-slate-800 text-center">
                  <span class="text-[10px] text-slate-400 block uppercase">Marge Nette (Après IS)</span>
                  <b class="text-sm font-black text-white font-mono" id="res-marge-nette">+53 125 €</b>
                </div>
                <div class="bg-slate-900 p-2.5 rounded-xl border border-slate-800 text-center">
                  <span class="text-[10px] text-slate-400 block uppercase">ROI sur Apport</span>
                  <b class="text-sm font-black text-amber-400 font-mono" id="res-roi">151%</b>
                </div>
              </div>
              <div class="text-xs space-y-1.5 text-slate-300 font-mono border-t border-slate-800 pt-3">
                <div class="flex justify-between"><span>Prix d’achat net :</span><span id="res-achat" class="text-white font-semibold">135 000 €</span></div>
                <div class="flex justify-between"><span>Frais notaire (MDB) :</span><span id="res-notaire" class="text-white">965 €</span></div>
                <div class="flex justify-between"><span>Travaux de rénovation :</span><span id="res-travaux" class="text-amber-300">85 000 €</span></div>
                <div class="flex justify-between"><span>Géomètre & Division :</span><span id="res-geometre" class="text-white">3 200 €</span></div>
                <div class="flex justify-between"><span>Portage financier & intérêts :</span><span id="res-portage" class="text-slate-400">4 600 €</span></div>
                <div class="flex justify-between font-bold text-white pt-1.5 border-t border-slate-800">
                  <span>Coût de revient total :</span><span id="res-revient" class="text-white">228 765 €</span>
                </div>
                <div class="flex justify-between font-bold text-emerald-400">
                  <span>Chiffre d’Affaires total :</span><span id="res-revente">310 000 €</span>
                </div>
                <div class="flex justify-between text-slate-400 pt-1 text-[11px]">
                  <span>Impôt Sociétés (IS estimé 15%/25%) :</span><span id="res-is" class="text-rose-400">-15 375 €</span>
                </div>
              </div>
            </div>
            <div class="space-y-2 pt-3">
              <div class="bg-slate-900 p-3 rounded-xl border border-slate-800 text-[11px] text-slate-400">
                💡 <strong>Gain statut Marchand de Biens :</strong> économie de <span id="res-gain-notaire" class="text-emerald-400 font-bold">+9 565 €</span>.
              </div>
              <button onclick="genererAnalyseIA()" class="w-full py-2.5 bg-gradient-to-r from-amber-500 to-amber-600 hover:from-amber-400 hover:to-amber-500 text-slate-950 font-bold text-xs rounded-xl shadow-lg transition cursor-pointer">
                🎯 Lancer l’Analyse Stratégique & Décisionnelle
              </button>
            </div>
          </div>
        </div>

        <!-- BLOC ANALYSE STRATÉGIQUE -->
        <div id="bloc-analyse-strategique" class="mt-6 pt-6 border-t border-slate-800 space-y-4">
          <div class="flex items-center justify-between">
            <h3 class="text-base font-bold text-white flex items-center gap-2">
              <span class="text-amber-400">✨</span> Rapport d’Analyse Stratégique & Validation Bancaire
            </h3>
            <span class="text-xs text-slate-400 font-mono" id="analyse-timestamp">Prêt pour audit</span>
          </div>
          <div class="grid grid-cols-1 md:grid-cols-3 gap-4 text-xs">
            <div class="bg-slate-950 p-4 rounded-xl border border-slate-800 space-y-2">
              <strong class="text-amber-400 text-sm block flex items-center gap-1.5">
                🏢 Stratégie d’Arbitrage Conseillée
              </strong>
              <p class="text-slate-300 leading-relaxed" id="strat-arbitrage">...</p>
              <div class="pt-2 border-t border-slate-800/80 text-[11px] text-slate-400" id="strat-lots-detail">...</div>
            </div>
            <div class="bg-slate-950 p-4 rounded-xl border border-slate-800 space-y-2">
              <strong class="text-emerald-400 text-sm block flex items-center gap-1.5">
                📐 Explication Précise du Calcul
              </strong>
              <div class="text-slate-300 space-y-1.5 leading-relaxed text-[11px]" id="strat-calcul-explication">...</div>
            </div>
            <div class="bg-slate-950 p-4 rounded-xl border border-slate-800 space-y-2">
              <strong class="text-sky-400 text-sm block flex items-center gap-1.5">
                💬 Argumentaire de Négociation
              </strong>
              <p class="text-slate-300 leading-relaxed text-[11px]" id="strat-pitch">...</p>
              <button onclick="copierPitch()" id="btn-copy-pitch" class="mt-2 w-full py-1.5 bg-slate-900 hover:bg-slate-800 border border-slate-700 text-slate-200 text-[11px] font-semibold rounded-lg transition cursor-pointer">
                📋 Copier le pitch de négociation
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- ONGLET 4 : STRATEGIE MDB -->
    <div id="tab-audit" class="hidden space-y-6">
      <div class="bg-slate-900 p-6 rounded-2xl border border-slate-800 space-y-4">
        <h2 class="text-xl font-bold text-white">Les 4 Piliers Rentables du Marchand de Biens à Millau</h2>
        <div class="grid grid-cols-1 md:grid-cols-2 gap-4 text-xs text-slate-300">
          <div class="bg-slate-950 p-4 rounded-xl border border-slate-800">
            <strong class="text-amber-400 text-sm block mb-1">1. Monopropriétés Centre Ancien (Rue Droite, Halles)</strong>
            <p class="text-slate-400 leading-relaxed">Acheter des immeubles R+3 complets à 550 - 800 €/m² et découper en plusieurs lots (T2/T3). Aucun syndic de copro bloquant.</p>
          </div>
          <div class="bg-slate-950 p-4 rounded-xl border border-slate-800">
            <strong class="text-purple-400 text-sm block mb-1">2. Passoires thermiques G et F (Loi Climat)</strong>
            <p class="text-slate-400 leading-relaxed">Négociation agressive (-20% à -30%) en mettant en avant le devis de rénovation globale pour mise aux normes locatives.</p>
          </div>
          <div class="bg-slate-950 p-4 rounded-xl border border-slate-800">
            <strong class="text-emerald-400 text-sm block mb-1">3. Découpe de parcelles & Granges (Faubourgs)</strong>
            <p class="text-slate-400 leading-relaxed">Détachement d’un terrain constructible viabilisé pour amortir immédiatement l’acquisition, et rénovation du bâtiment restant.</p>
          </div>
          <div class="bg-slate-950 p-4 rounded-xl border border-slate-800">
            <strong class="text-sky-400 text-sm block mb-1">4. Prix de Revente Cible Millau</strong>
            <p class="text-slate-400 leading-relaxed">À Millau, les T2 et T3 rénovés avec DPE C/D se revendent entre 1 650 € et 2 100 €/m².</p>
          </div>
        </div>
      </div>
    </div>
  </main>

  <!-- MODAL DETAIL SCORING V3 -->
  <div id="modal-scoring" class="hidden fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/80 backdrop-blur-sm overflow-y-auto">
    <div class="bg-slate-900 border border-slate-700 rounded-3xl max-w-2xl w-full p-6 shadow-2xl relative max-h-[90vh] overflow-y-auto">
      <button onclick="fermerModalScoring()" class="absolute right-4 top-4 text-slate-400 hover:text-white p-1 rounded-lg hover:bg-slate-800 transition cursor-pointer">✕</button>
      <div class="flex items-center gap-2 mb-2">
        <span id="modal-badge-qualite" class="px-2.5 py-0.5 rounded text-xs font-black border bg-emerald-500/20 text-emerald-300 border-emerald-500/40">Excellente opportunité</span>
        <span class="text-xs text-slate-400 bg-slate-800 px-2 py-0.5 rounded">Scoring V3 Millau</span>
      </div>
      <h3 id="modal-titre" class="text-lg font-bold text-white mb-1">Détail du bien</h3>
      <p id="modal-sous-titre" class="text-xs text-slate-400 mb-5">Agence • Millau</p>

      <div class="space-y-3 text-xs mb-5">
        <div class="bg-slate-950 p-3.5 rounded-xl border border-slate-800">
          <div class="flex justify-between font-bold text-white mb-1">
            <span class="text-emerald-400">1. Marge Potentielle Prévisionnelle (35%)</span>
            <span id="modal-pts-marge" class="text-emerald-400">-- / 35 pts</span>
          </div>
          <p id="modal-desc-marge" class="text-slate-400 text-[11px] leading-relaxed">Calculé sur CA Revente - Travaux - Frais MDB.</p>
        </div>
        <div class="bg-slate-950 p-3.5 rounded-xl border border-slate-800">
          <div class="flex justify-between font-bold text-white mb-1">
            <span class="text-indigo-400">2. Découpe & Création de Valeur (25%)</span>
            <span id="modal-pts-decoupe" class="text-indigo-400">-- / 25 pts</span>
          </div>
          <p id="modal-desc-decoupe" class="text-slate-400 text-[11px] leading-relaxed">Monopropriété sans syndic, plateaux ou division en plusieurs lots.</p>
        </div>
        <div class="bg-slate-950 p-3.5 rounded-xl border border-slate-800">
          <div class="flex justify-between font-bold text-white mb-1">
            <span class="text-amber-400">3. Levier de Négociation & Décote (20%)</span>
            <span id="modal-pts-negoc" class="text-amber-400">-- / 20 pts</span>
          </div>
          <p id="modal-desc-negoc" class="text-slate-400 text-[11px] leading-relaxed">Passoire thermique F/G, historique de baisses de prix constatées et prix/m² bas.</p>
        </div>
        <div class="bg-slate-950 p-3.5 rounded-xl border border-slate-800">
          <div class="flex justify-between font-bold text-white mb-1">
            <span class="text-sky-400">4. Sécurité & Risque d’Exécution (20%)</span>
            <span id="modal-pts-securite" class="text-sky-400">-- / 20 pts</span>
          </div>
          <p id="modal-desc-securite" class="text-slate-400 text-[11px] leading-relaxed">Ticket d’achat liquide et absence de copropriété bloquante.</p>
        </div>
      </div>

      <div class="flex justify-end gap-2 pt-3 border-t border-slate-800">
        <button onclick="fermerModalScoring()" class="px-4 py-2 bg-slate-800 hover:bg-slate-700 text-slate-200 text-xs font-semibold rounded-xl">Fermer</button>
        <button id="modal-btn-simu" class="px-4 py-2 bg-amber-500 hover:bg-amber-400 text-slate-950 text-xs font-bold rounded-xl shadow-md">🧮 Simuler ce bien</button>
      </div>
    </div>
  </div>

  <script>
    const DATA = __DATA__;
    let currentLots = [
      { id: '1', nom: 'Lot 1 : Local RDC / Garage', surface: 45, travaux: 18000, revente: 42000 },
      { id: '2', nom: 'Lot 2 : T2 1er étage rénové', surface: 60, travaux: 45000, revente: 105000 },
      { id: '3', nom: 'Lot 3 : T3 2ème étage rénové', surface: 65, travaux: 48000, revente: 118000 }
    ];
    let selectedDealContext = null;
    let currentAuditDeal = null;
    let currentAuditScenario = null;
    let selectedScenarioKey = 'strat2';

    function fmt(n) {
      return (Number(n) || 0).toLocaleString('fr-FR');
    }

    function setTab(name) {
      ['deals', 'audit_profond', 'simu', 'audit'].forEach(t => {
        const el = document.getElementById('tab-' + t);
        if (el) el.classList.toggle('hidden', t !== name);
        const btn = document.getElementById('btn-tab-' + t);
        if (btn) {
          if (t === name) {
            btn.className = 'px-3.5 py-1.5 rounded-lg text-xs font-bold transition bg-amber-500 text-slate-950 cursor-pointer';
          } else {
            btn.className = 'px-3.5 py-1.5 rounded-lg text-xs font-semibold transition text-slate-300 hover:bg-slate-800 cursor-pointer';
          }
        }
      });
      window.scrollTo({ top: 0, behavior: 'smooth' });
    }

    function initAuditDealSelector() {
      const sel = document.getElementById('ap-select-deal');
      if (!sel) return;
      sel.innerHTML = '<option value="">-- Choisir un bien dans la veille (' + DATA.length + ' dispo) --</option>';
      DATA.forEach((d, idx) => {
        const opt = document.createElement('option');
        opt.value = idx;
        opt.textContent = (d.titre || 'Bien sans titre') + ' — ' + (d.prix ? fmt(d.prix) + ' €' : '') + ' (' + (d.surface || '?') + ' m²)';
        sel.appendChild(opt);
      });
    }

    function changerDealAudit(val) {
      if (val === '') return;
      const d = DATA[parseInt(val, 10)];
      if (d) lancerAuditApprofondi(d);
    }

    function lancerAuditApprofondi(deal) {
      currentAuditDeal = deal;
      const idx = DATA.indexOf(deal);
      const sel = document.getElementById('ap-select-deal');
      if (sel && idx >= 0) sel.value = idx;

      document.getElementById('ap-titre').textContent = deal.titre || 'Bien immobilier';
      document.getElementById('ap-soustitre').textContent = (deal.agence || 'Agence') + ' • ' + (deal.prix ? fmt(deal.prix) + ' €' : '') + ' • DPE ' + (deal.dpe || '?') + ' • ' + (deal.surface || 0) + ' m²';

      // 1. Photos
      const photosGrid = document.getElementById('ap-photos-grid');
      if (deal.photos && deal.photos.length > 0) {
        photosGrid.innerHTML = deal.photos.map(src => 
          `<div class="h-32 rounded-xl overflow-hidden border border-slate-800 relative group bg-slate-900 flex items-center justify-center">
            <img src="${src}" class="w-full h-full object-cover transition-transform duration-500 group-hover:scale-110" alt="Photo extraite de l'annonce" onerror="this.style.display='none'">
          </div>`
        ).join('');
      } else {
        photosGrid.innerHTML = `<div class="col-span-4 text-center py-8 text-slate-500 text-xs border border-dashed border-slate-700 rounded-xl">Aucune photo HD extractible sur cette plateforme.</div>`;
      }

      document.getElementById('ap-photos-constat').innerHTML = 
        `🔍 <strong>Source des visuels et de l'analyse :</strong> <a href="${deal.url}" target="_blank" class="text-amber-400 font-bold hover:underline">${deal.agence} ↗</a><br/>
        <span class="text-[10px]">Images extraites automatiquement depuis l'annonce d'origine pour les besoins de l'audit interne MDB.</span>`;

      // 2. Diagnostics
      const diagList = document.getElementById('ap-diagnostics-list');
      diagList.innerHTML = 
        '<div class="p-2.5 rounded-lg bg-slate-900 border border-slate-800"><b class="text-slate-200 block">⚡ Électricité & Réseaux</b><span class="text-slate-400 text-[11px]">Installation à vérifier. Nécessité de créer des colonnes individuelles Enedis pour chaque lot.</span></div>' +
        '<div class="p-2.5 rounded-lg bg-slate-900 border border-slate-800"><b class="text-slate-200 block">🔥 Performance Énergétique (DPE ' + (deal.dpe || '?') + ')</b><span class="text-slate-400 text-[11px]">Isolation thermique par l’intérieur (ITI) et VMC hydrob à prévoir pour optimisation.</span></div>';

      // 3. Postes de travaux
      const surf = deal.surface || 150;
      const budgetM2 = deal.dpe === 'G' ? 750 : 600;
      const totalTrav = Math.round(surf * budgetM2);
      document.getElementById('ap-total-travaux-badge').textContent = 'Enveloppe globale estimée : ' + fmt(totalTrav) + ' € TTC';

      const tbody = document.getElementById('ap-travaux-tbody');
      tbody.innerHTML = 
        '<tr class="hover:bg-slate-900/50"><td class="py-2.5 px-3 font-semibold text-white">⚡ Colonnes & Enedis</td><td class="py-2.5 px-3 text-slate-400">Compteur unique</td><td class="py-2.5 px-3 text-amber-300">Création colonnes / compteurs Linky individuels</td><td class="py-2.5 px-3 text-right font-mono font-bold text-amber-400">9 500 €</td></tr>' +
        '<tr class="hover:bg-slate-900/50"><td class="py-2.5 px-3 font-semibold text-white">🪟 Menuiseries & Isolation</td><td class="py-2.5 px-3 text-slate-400">Vétuste</td><td class="py-2.5 px-3 text-amber-300">Double vitrage + doublage laine minérale 120mm</td><td class="py-2.5 px-3 text-right font-mono font-bold text-amber-400">' + fmt(Math.round(surf * 180)) + ' €</td></tr>' +
        '<tr class="hover:bg-slate-900/50"><td class="py-2.5 px-3 font-semibold text-white">🚰 Chauffage & PAC</td><td class="py-2.5 px-3 text-slate-400">Électrique obsolète</td><td class="py-2.5 px-3 text-amber-300">Ballons thermo. + splits PAC air/air individuels</td><td class="py-2.5 px-3 text-right font-mono font-bold text-amber-400">' + fmt(Math.round(surf * 140)) + ' €</td></tr>' +
        '<tr class="hover:bg-slate-900/50"><td class="py-2.5 px-3 font-semibold text-white">🔨 Second-œuvre</td><td class="py-2.5 px-3 text-slate-400">À refaire</td><td class="py-2.5 px-3 text-amber-300">Cuisines équipées, douches, sols stratifiés, peintures</td><td class="py-2.5 px-3 text-right font-mono font-bold text-amber-400">' + fmt(Math.round(surf * 230)) + ' €</td></tr>';

      // 4. Stratégies interactives
      const prixAchat = deal.prix || 140000;
      const notaireMdb = Math.round(prixAchat * 0.00715);

      currentAuditScenario = {
        strat1: { 
          id: 'strat1', titre: 'Vente Plateaux Bruts', delai: '4 mois',
          desc: 'Travaux limités aux parties communes et compteurs. Les acquéreurs font leur propre aménagement.',
          travaux: 32000, revente: Math.round(surf * 1250) 
        },
        strat2: { 
          id: 'strat2', titre: 'Rénovation Totale Clé en main', delai: '11 mois',
          desc: 'Rénovation prête à habiter (DPE C). Maximise le chiffre d’affaires et le prix au m² sur Millau.',
          travaux: totalTrav, revente: Math.round(surf * 1950), recommande: true
        },
        strat3: { 
          id: 'strat3', titre: 'Formule Hybride', delai: '7 mois',
          desc: 'Rénover 1 lot principal et vendre le reste en plateaux bruts. Excellent compromis risque/délai.',
          travaux: Math.round(totalTrav * 0.65), revente: Math.round(surf * 1650) 
        }
      };

      Object.keys(currentAuditScenario).forEach(key => {
        let sc = currentAuditScenario[key];
        sc.marge = sc.revente - (prixAchat + notaireMdb + sc.travaux + 6000 + (key==='strat2'?12000:0));
      });

      selectedScenarioKey = 'strat2';
      afficherScenarios();
      setTab('audit_profond');
    }

    function afficherScenarios() {
      const scnCards = document.getElementById('ap-scenarios-cards');
      const signe = n => (n >= 0 ? '+' : '') + fmt(n) + ' €';

      scnCards.innerHTML = Object.values(currentAuditScenario).map(sc => {
        const isSelected = selectedScenarioKey === sc.id;
        const borderClass = isSelected ? 'border-amber-500 bg-amber-500/10 scale-[1.02] shadow-lg shadow-amber-500/10' : 'border-slate-800 bg-slate-950 hover:border-slate-600 opacity-80 hover:opacity-100';
        const titleClass = isSelected ? 'text-amber-400' : 'text-white';
        const badge = sc.recommande ? `<span class="absolute -top-2.5 right-3 bg-amber-500 text-slate-950 text-[10px] font-black px-2 py-0.5 rounded-full shadow-md">⭐ RECOMMANDÉ</span>` : '';

        return `
          <div onclick="selectionnerScenario('${sc.id}')" class="p-4 rounded-xl border ${borderClass} space-y-2 relative transition-all duration-300 cursor-pointer">
            ${badge}
            <div class="flex justify-between items-center">
              <span class="font-bold ${titleClass}">${sc.titre}</span>
              <span class="text-[10px] bg-slate-800 text-slate-300 px-2 py-0.5 rounded">${sc.delai}</span>
            </div>
            <div class="text-slate-400 text-[11px] h-8">${sc.desc}</div>
            <div class="pt-2 border-t ${isSelected ? 'border-amber-500/30' : 'border-slate-800'} space-y-1">
              <div>Travaux : <b class="text-white">${fmt(sc.travaux)} €</b></div>
              <div>Revente : <b class="text-white">${fmt(sc.revente)} €</b></div>
              <div>Marge brute : <b class="text-emerald-400 font-bold">${signe(sc.marge)}</b></div>
            </div>
          </div>
        `;
      }).join('');

      const scActuel = currentAuditScenario[selectedScenarioKey];
      document.getElementById('ap-verdict-delai').textContent = 'Horizon de débouclage : ' + scActuel.delai;
      document.getElementById('ap-verdict-texte').innerHTML = 
        `Vous avez sélectionné la stratégie <strong>${scActuel.titre}</strong>. Cette option nécessite une enveloppe travaux estimée à <strong>${fmt(scActuel.travaux)} €</strong> et permet de viser un Chiffre d'Affaires de revente de <strong>${fmt(scActuel.revente)} €</strong>.`;
    }

    function selectionnerScenario(key) {
      selectedScenarioKey = key;
      afficherScenarios();
    }

    function injecterScenarioDansSimulateur() {
      if (!currentAuditDeal || !currentAuditScenario) return;
      const a = currentAuditDeal;
      const stratChoisie = currentAuditScenario[selectedScenarioKey];
      
      selectedDealContext = { prix: a.prix, surface: a.surface, titre: a.titre, dpe: a.dpe, agence: a.agence };
      document.getElementById('sim-prix').value = a.prix || 140000;
      document.getElementById('sim-nego').value = Math.round((a.prix || 140000) * 0.08);

      if (selectedScenarioKey === 'strat1') {
        currentLots = [
          { id: '1', nom: 'Vente en Plateau Brut 1', surface: Math.round((a.surface||100)*0.5), travaux: Math.round(stratChoisie.travaux*0.5), revente: Math.round(stratChoisie.revente*0.5) },
          { id: '2', nom: 'Vente en Plateau Brut 2', surface: Math.round((a.surface||100)*0.5), travaux: Math.round(stratChoisie.travaux*0.5), revente: Math.round(stratChoisie.revente*0.5) }
        ];
      } else if (selectedScenarioKey === 'strat2') {
        currentLots = [
          { id: '1', nom: 'Appartement Rénové Clé en main 1', surface: Math.round((a.surface||100)*0.5), travaux: Math.round(stratChoisie.travaux*0.5), revente: Math.round(stratChoisie.revente*0.5) },
          { id: '2', nom: 'Appartement Rénové Clé en main 2', surface: Math.round((a.surface||100)*0.5), travaux: Math.round(stratChoisie.travaux*0.5), revente: Math.round(stratChoisie.revente*0.5) }
        ];
      } else {
        currentLots = [
          { id: '1', nom: 'Lot RDC Brut', surface: Math.round((a.surface||100)*0.4), travaux: Math.round(stratChoisie.travaux*0.2), revente: Math.round(stratChoisie.revente*0.35) },
          { id: '2', nom: 'Lot Étage Rénové', surface: Math.round((a.surface||100)*0.6), travaux: Math.round(stratChoisie.travaux*0.8), revente: Math.round(stratChoisie.revente*0.65) }
        ];
      }

      renderLotsTable();
      genererAnalyseIA(false);
      setTab('simu');
    }

    function majTotauxLots() {
      let totalSurf = 0, totalTrav = 0, totalRev = 0;
      currentLots.forEach(lot => {
        totalSurf += lot.surface || 0;
        totalTrav += lot.travaux || 0;
        totalRev += lot.revente || 0;
      });
      document.getElementById('total-surf').textContent = totalSurf + ' m²';
      document.getElementById('total-travaux-lots').textContent = fmt(totalTrav) + ' €';
      document.getElementById('total-revente-lots').textContent = fmt(totalRev) + ' €';
      calculerSimu();
    }

    function renderLotsTable() {
      const tbody = document.getElementById('lots-table-body');
      tbody.innerHTML = '';
      currentLots.forEach((lot, idx) => {
        const tr = document.createElement('tr');
        tr.className = 'hover:bg-slate-900/60 transition';
        tr.innerHTML =
          '<td class="py-2 pr-2"><input type="text" class="lot-nom-input w-full px-2 py-1 bg-slate-900 border border-slate-800 rounded text-slate-200 text-xs focus:border-amber-500" /></td>' +
          '<td class="py-2 pr-2"><input type="number" class="lot-surf-input w-full px-2 py-1 bg-slate-900 border border-slate-800 rounded text-white text-xs" /></td>' +
          '<td class="py-2 pr-2"><input type="number" class="lot-trav-input w-full px-2 py-1 bg-slate-900 border border-slate-800 rounded text-amber-300 text-xs" /></td>' +
          '<td class="py-2 pr-2"><input type="number" class="lot-rev-input w-full px-2 py-1 bg-slate-900 border border-slate-800 rounded text-emerald-400 font-bold text-xs" /></td>' +
          '<td class="py-2 text-center"><button class="btn-suppr-lot text-slate-600 hover:text-rose-400 text-xs cursor-pointer">✕</button></td>';

        const inpNom = tr.querySelector('.lot-nom-input');
        inpNom.value = lot.nom || '';
        inpNom.oninput = function() { currentLots[idx].nom = this.value; };

        const inpSurf = tr.querySelector('.lot-surf-input');
        inpSurf.value = lot.surface || 0;
        inpSurf.oninput = function() { updateLot(idx, 'surface', parseFloat(this.value) || 0); };

        const inpTrav = tr.querySelector('.lot-trav-input');
        inpTrav.value = lot.travaux || 0;
        inpTrav.oninput = function() { updateLot(idx, 'travaux', parseFloat(this.value) || 0); };

        const inpRev = tr.querySelector('.lot-rev-input');
        inpRev.value = lot.revente || 0;
        inpRev.oninput = function() { updateLot(idx, 'revente', parseFloat(this.value) || 0); };

        tr.querySelector('.btn-suppr-lot').onclick = function() { supprimerLot(idx); };
        tbody.appendChild(tr);
      });
      majTotauxLots();
    }

    function ajouterLot() {
      const num = currentLots.length + 1;
      currentLots.push({
        id: Date.now().toString(),
        nom: 'Lot ' + num + ' : T2 rénové',
        surface: 50, travaux: 38000, revente: 92000
      });
      renderLotsTable();
    }

    function supprimerLot(idx) {
      if (currentLots.length <= 1) return;
      currentLots.splice(idx, 1);
      renderLotsTable();
    }

    function updateLot(idx, field, val) {
      if (currentLots[idx]) {
        currentLots[idx][field] = val;
        majTotauxLots();
      }
    }

    function simulerDeal(prix, surface, titre, dpe, agence) {
      selectedDealContext = { prix, surface, titre, dpe, agence };
      document.getElementById('sim-prix').value = prix || 140000;
      document.getElementById('sim-nego').value = Math.round((prix || 140000) * 0.08);

      const m2 = surface || 160;
      if (m2 > 180) {
        currentLots = [
          { id: '1', nom: 'RDC', surface: Math.round(m2 * 0.22), travaux: Math.round(m2 * 0.22 * 450), revente: Math.round(m2 * 0.22 * 1100) },
          { id: '2', nom: '1er étage', surface: Math.round(m2 * 0.26), travaux: Math.round(m2 * 0.26 * 850), revente: Math.round(m2 * 0.26 * 1900) },
          { id: '3', nom: '2ème étage', surface: Math.round(m2 * 0.26), travaux: Math.round(m2 * 0.26 * 800), revente: Math.round(m2 * 0.26 * 1900) },
          { id: '4', nom: '3ème étage', surface: Math.round(m2 * 0.26), travaux: Math.round(m2 * 0.26 * 850), revente: Math.round(m2 * 0.26 * 1950) }
        ];
      } else if (m2 > 100) {
        currentLots = [
          { id: '1', nom: 'Lot 1', surface: Math.round(m2 * 0.55), travaux: Math.round(m2 * 0.55 * 800), revente: Math.round(m2 * 0.55 * 1950) },
          { id: '2', nom: 'Lot 2', surface: Math.round(m2 * 0.45), travaux: Math.round(m2 * 0.45 * 850), revente: Math.round(m2 * 0.45 * 1900) }
        ];
      } else {
        currentLots = [
          { id: '1', nom: 'Lot unique', surface: m2, travaux: Math.round(m2 * (dpe === 'G' ? 950 : 750)), revente: Math.round(m2 * 1900) }
        ];
      }

      renderLotsTable();
      genererAnalyseIA(false);
      setTab('simu');
    }

    function calculerSimu() {
      const prixAffiche = parseFloat(document.getElementById('sim-prix').value) || 0;
      const negociation = parseFloat(document.getElementById('sim-nego').value) || 0;
      const prixNet = Math.max(0, prixAffiche - negociation);
      const tauxNotaire = parseFloat(document.getElementById('sim-notaire').value) || 0.00715;
      const notaire = Math.round(prixNet * tauxNotaire);
      const geometre = parseFloat(document.getElementById('sim-geometre').value) || 0;
      const dureeMois = parseFloat(document.getElementById('sim-mois').value) || 8;
      const tauxCredit = parseFloat(document.getElementById('sim-taux').value) || 3.8;
      const apport = parseFloat(document.getElementById('sim-apport').value) || 0;

      const totalTravaux = currentLots.reduce((sum, l) => sum + (l.travaux || 0), 0);
      const reventeTotale = currentLots.reduce((sum, l) => sum + (l.revente || 0), 0);

      const emprunt = Math.max(0, prixNet + notaire + totalTravaux + geometre - apport);
      const portage = Math.round(emprunt * (tauxCredit / 100) * (dureeMois / 12) + (emprunt * 0.01));

      const revient = prixNet + notaire + totalTravaux + geometre + portage;
      const margeBrute = reventeTotale - revient;
      const pctBrute = reventeTotale > 0 ? ((margeBrute / reventeTotale) * 100).toFixed(1) : 0;

      let isEstime = 0;
      if (margeBrute > 0) {
        if (margeBrute <= 42500) isEstime = Math.round(margeBrute * 0.15);
        else isEstime = Math.round((42500 * 0.15) + ((margeBrute - 42500) * 0.25));
      }
      const margeNette = margeBrute - isEstime;
      const roi = apport > 0 ? Math.round((margeNette / apport) * 100) : 0;
      const gainNotaire = Math.round(prixNet * (0.078 - 0.00715));

      document.getElementById('res-achat').textContent = fmt(prixNet) + ' €';
      document.getElementById('res-notaire').textContent = fmt(notaire) + ' €';
      document.getElementById('res-travaux').textContent = fmt(totalTravaux) + ' €';
      document.getElementById('res-geometre').textContent = fmt(geometre) + ' €';
      document.getElementById('res-portage').textContent = fmt(portage) + ' €';
      document.getElementById('res-revient').textContent = fmt(revient) + ' €';
      document.getElementById('res-revente').textContent = fmt(reventeTotale) + ' €';
      document.getElementById('res-is').textContent = (isEstime > 0 ? '-' : '') + fmt(isEstime) + ' €';
      document.getElementById('res-marge-brute').textContent = (margeBrute >= 0 ? '+' : '') + fmt(margeBrute) + ' €';
      document.getElementById('res-marge-pct').textContent = pctBrute + '% du CA de revente';
      document.getElementById('res-marge-nette').textContent = (margeNette >= 0 ? '+' : '') + fmt(margeNette) + ' €';
      document.getElementById('res-roi').textContent = roi + '%';
      document.getElementById('res-gain-notaire').textContent = '+' + fmt(gainNotaire) + ' €';

      window.__revientSimu = revient;

      if (typeof genererAnalyseIA === 'function') {
        genererAnalyseIA(false);
      }
    }

    function genererAnalyseIA(userInteraction) {
      const withScroll = userInteraction !== false; 

      const prix = parseFloat(document.getElementById('sim-prix').value) || 140000;
      const nego = parseFloat(document.getElementById('sim-nego').value) || 0;
      const prixNet = Math.max(0, prix - nego);
      const nbLots = currentLots.length;
      const revente = currentLots.reduce((s, l) => s + (l.revente || 0), 0);
      const travaux = currentLots.reduce((s, l) => s + (l.travaux || 0), 0);
      const revient = window.__revientSimu || parseFloat(document.getElementById('res-revient').textContent.replace(/[^\d]/g, '')) || 0;
      const marge = revente - revient;
      const margePct = revente > 0 ? ((marge / revente) * 100).toFixed(1) : 0;

      const titre = selectedDealContext ? selectedDealContext.titre : 'Ensemble immobilier Millau';
      const dpe = selectedDealContext ? selectedDealContext.dpe : 'F';

      document.getElementById('analyse-timestamp').textContent = 'Audit généré à ' + new Date().toLocaleTimeString('fr-FR', { hour: '2-digit', minute: '2-digit', second: '2-digit' });

      if (nbLots >= 3) {
        document.getElementById('strat-arbitrage').innerHTML =
          '<strong>Découpe cadastrale en ' + nbLots + ' lots en copropriété neuve.</strong><br/>Acheter la monopropriété en bloc (statut MDB art. 1115 CGI, notaire ~0.715%). Créer l’EDD et le règlement de copropriété avec géomètre. Rénovation par tranche : le cash flow des 2 premières ventes solde le crédit bancaire.';
        document.getElementById('strat-lots-detail').textContent =
          'Cible de revente Millau : T2 et T3 prêts à habiter (1 800 à 1 950 €/m²). Marché très demandeur.';
      } else {
        document.getElementById('strat-arbitrage').innerHTML =
          '<strong>Réhabilitation thermique & arbitrage rapide.</strong><br/>Sortir le bien du statut de passoire (DPE ' + dpe + ') par isolation globale et PAC. Revente à la découpe ou en bloc rénové.';
        document.getElementById('strat-lots-detail').textContent =
          'Revente estimée à 1 850 €/m² pour des appartements DPE C.';
      }

      document.getElementById('strat-calcul-explication').innerHTML =
        '<div>• <strong>Prix achat net négocié :</strong> ' + fmt(prixNet) + ' € (-' + fmt(nego) + ' € d’offre).</div>' +
        '<div>• <strong>Budget travaux global :</strong> ' + fmt(travaux) + ' € (compteurs Enedis individuels, réfection totale).</div>' +
        '<div>• <strong>Coût de revient total :</strong> ' + fmt(revient) + ' € (frais MDB + géomètre + portage inclus).</div>' +
        '<div>• <strong>Chiffre d’Affaires :</strong> ' + fmt(revente) + ' € sur ' + nbLots + ' lots.</div>' +
        '<div>• <strong>Marge Brute dégagée :</strong> <span class="' + (marge >= 0 ? 'text-emerald-400' : 'text-rose-400') + ' font-bold">' + (marge >= 0 ? '+' : '') + fmt(marge) + ' € (' + margePct + '%)</span>.</div>';

      document.getElementById('strat-pitch').textContent =
        '« Bonjour, suite à notre visite du bien "' + titre.substring(0, 50) + '...", nous vous transmettons notre offre ferme à ' + fmt(prixNet) + ' € net vendeur. Notre dossier est sans condition suspensive de prêt (financement pro MDB disponible). Nous prenons le bien en l’état avec l’audit énergétique et nous nous engageons sur une signature sous 30 jours. »';

      if (withScroll) {
        const bloc = document.getElementById('bloc-analyse-strategique');
        if (bloc) {
          bloc.scrollIntoView({ behavior: 'smooth', block: 'center' });
          bloc.classList.remove('border-slate-800');
          bloc.classList.add('border-amber-500', 'bg-amber-500/10', 'transition-all', 'duration-500');
          setTimeout(() => {
            bloc.classList.remove('border-amber-500', 'bg-amber-500/10');
            bloc.classList.add('border-slate-800');
          }, 800);
        }
      }
    }

    function copierPitch() {
      const text = document.getElementById('strat-pitch').textContent;
      try { navigator.clipboard.writeText(text); } catch (e) {}
      const btn = document.getElementById('btn-copy-pitch');
      btn.textContent = '✅ Argumentaire copié !';
      setTimeout(() => { btn.textContent = '📋 Copier le pitch de négociation'; }, 2000);
    }

    function afficherModalScoring(idx) {
      const a = DATA[idx];
      if (!a) return;
      document.getElementById('modal-titre').textContent = a.titre || 'Bien immobilier';
      document.getElementById('modal-sous-titre').textContent = (a.agence || '') + ' • Millau • ' + (a.prix ? fmt(a.prix) + ' €' : '') + ' (' + (a.prix_m2 || '?') + ' €/m²)';

      const marge = a.marge_estimee_montant || 0;
      const margePct = a.marge_estimee_pct || 0;
      document.getElementById('modal-pts-marge').textContent = (margePct >= 18 ? '30' : (margePct >= 12 ? '20' : '10')) + ' / 35 pts';
      document.getElementById('modal-desc-marge').textContent = 'Marge brute estimée : ' + (marge >= 0 ? '+' : '') + fmt(marge) + ' € (' + margePct + '% du CA). Standard MDB de sécurité financière.';

      document.getElementById('modal-pts-decoupe').textContent = (a.surface > 150 ? '25' : (a.surface > 80 ? '18' : '10')) + ' / 25 pts';
      document.getElementById('modal-desc-decoupe').textContent = 'Surface ' + (a.surface || 0) + ' m². Potentiel de division en ' + (a.surface > 150 ? '3 à 4 lots' : '2 lots') + ' indépendants.';

      const ptsNegoc = (a.dpe === 'G' ? 8 : (a.dpe === 'F' ? 6 : 0)) + (a.baisse_prix_pct > 0 ? 8 : 0);
      document.getElementById('modal-pts-negoc').textContent = ptsNegoc + ' / 20 pts';
      document.getElementById('modal-desc-negoc').textContent = 'DPE ' + (a.dpe || '?') + ' + Baisses de prix constatées (-' + (a.baisse_prix_pct || 0) + '%). Fort pouvoir de négociation.';

      document.getElementById('modal-pts-securite').textContent = '16 / 20 pts';
      document.getElementById('modal-desc-securite').textContent = 'Marché millavois liquide sous 180 000 €. Absence de copropriété complexe bloquante.';

      const btnSimu = document.getElementById('modal-btn-simu');
      btnSimu.onclick = function() {
        fermerModalScoring();
        simulerDeal(a.prix, a.surface, a.titre, a.dpe, a.agence);
      };

      document.getElementById('modal-scoring').classList.remove('hidden');
    }

    function fermerModalScoring() {
      document.getElementById('modal-scoring').classList.add('hidden');
    }

    function renderDeals() {
      const q = (document.getElementById('q').value || '').toLowerCase();
      const ftype = document.getElementById('f-type').value;
      const fsort = document.getElementById('f-sort').value;
      const fdpe = document.getElementById('f-dpe').value;
      const fdrops = document.getElementById('f-drops').checked;

      let filtered = DATA.filter(a => {
        const fullText = ((a.titre || '') + ' ' + (a.mots_detectes || '') + ' ' + (a.agence || '')).toLowerCase();
        const matchesQ = !q || fullText.includes(q);
        const matchesDpe = fdpe === 'all' || (fdpe === 'passoires' && (a.dpe === 'F' || a.dpe === 'G')) || a.dpe === fdpe;
        const matchesDrops = !fdrops || (a.baisse_prix_pct > 0);

        let matchesType = true;
        if (ftype === 'immeuble') matchesType = fullText.includes('immeuble') || fullText.includes('monopropri') || fullText.includes('bâtisse');
        else if (ftype === 'maison') matchesType = fullText.includes('maison') || fullText.includes('villa');
        else if (ftype === 'appartement') matchesType = fullText.includes('appartement') || fullText.includes('t2') || fullText.includes('t3') || fullText.includes('studio');
        else if (ftype === 'grange') matchesType = fullText.includes('grange') || fullText.includes('plateau') || fullText.includes('remise') || fullText.includes('atelier');

        return matchesQ && matchesDpe && matchesDrops && matchesType;
      });

      filtered.sort((a, b) => {
        if (fsort === 'score_desc') return (b.score_v3 || b.score || 0) - (a.score_v3 || a.score || 0);
        if (fsort === 'marge_desc') return (b.marge_estimee_montant || 0) - (a.marge_estimee_montant || 0);
        if (fsort === 'prix_asc') return (a.prix || 9999999) - (b.prix || 9999999);
        if (fsort === 'prix_desc') return (b.prix || 0) - (a.prix || 0);
        if (fsort === 'm2_asc') return (a.prix_m2 || 9999999) - (b.prix_m2 || 9999999);
        if (fsort === 'm2_desc') return (b.prix_m2 || 0) - (a.prix_m2 || 0);
        if (fsort === 'surface_desc') return (b.surface || 0) - (a.surface || 0);
        return 0;
      });

      const total = DATA.length;
      const drops = DATA.filter(a => a.baisse_prix_pct > 0).length;
      const passoires = DATA.filter(a => a.dpe === 'F' || a.dpe === 'G').length;
      const news = DATA.filter(a => a.nouvelle).length;

      document.getElementById('kpi-cards').innerHTML =
        '<div class="bg-slate-900 border border-slate-800 p-3 rounded-xl"><b class="text-xl font-bold text-white block">' + total + '</b><span class="text-[10px] uppercase text-slate-400">Biens suivis</span></div>' +
        '<div class="bg-slate-900 border border-slate-800 p-3 rounded-xl"><b class="text-xl font-bold text-amber-400 block">' + news + '</b><span class="text-[10px] uppercase text-slate-400">Nouveautés</span></div>' +
        '<div class="bg-slate-900 border border-slate-800 p-3 rounded-xl"><b class="text-xl font-bold text-rose-400 block">' + drops + '</b><span class="text-[10px] uppercase text-slate-400">Baisses de prix</span></div>' +
        '<div class="bg-slate-900 border border-slate-800 p-3 rounded-xl"><b class="text-xl font-bold text-purple-400 block">' + passoires + '</b><span class="text-[10px] uppercase text-slate-400">Passoires F/G</span></div>';

      const grid = document.getElementById('deals-grid');
      grid.innerHTML = '';
      if (!filtered.length) {
        grid.innerHTML = '<div class="col-span-2 text-center py-12 text-slate-500">' +
          (total === 0
            ? 'Aucune annonce collectée pour le moment. Consultez les logs du scraper.'
            : 'Aucun bien ne correspond aux filtres sélectionnés.') +
          '</div>';
        return;
      }

      filtered.forEach(a => {
        try {
          const dpeClass = a.dpe === 'G' ? 'bg-purple-900 text-purple-100 ring-1 ring-purple-400' :
                           a.dpe === 'F' ? 'bg-rose-900 text-rose-100' :
                           a.dpe === 'E' ? 'bg-amber-900 text-amber-100' : 'bg-slate-800 text-slate-300';

          const scoreVal = a.score_v3 ? Number(a.score_v3).toFixed(1) : (a.score ? a.score + '/10' : '–');
          const margeMontant = a.marge_estimee_montant ? '+' + fmt(a.marge_estimee_montant) + ' €' : '–';
          const margePct = a.marge_estimee_pct ? '(' + a.marge_estimee_pct + '%)' : '';

          const card = document.createElement('div');
          card.className = 'bg-slate-900 border border-slate-800 p-5 rounded-2xl space-y-3 flex flex-col justify-between hover:border-slate-700 transition shadow-sm';
          const dpeVal = a.dpe || '?';

          card.innerHTML =
            '<div>' +
              '<div class="flex items-center justify-between gap-2 mb-2">' +
                '<div class="flex items-center gap-1.5 flex-wrap">' +
                  '<span class="px-2 py-0.5 rounded text-[11px] font-bold ' + dpeClass + '">DPE ' + dpeVal + '</span>' +
                  '<button class="btn-score-card px-2 py-0.5 rounded text-[11px] font-bold bg-amber-500/20 hover:bg-amber-500/30 text-amber-300 border border-amber-500/30 transition cursor-pointer" title="Cliquer pour voir les 4 piliers de scoring">⚡ Score V3: ' + scoreVal + '</button>' +
                  (a.baisse_prix_pct > 0 ? '<span class="px-2 py-0.5 rounded text-[11px] font-bold bg-rose-500/20 text-rose-300 border border-rose-500/30">📉 -' + a.baisse_prix_pct + '%</span>' : '') +
                  (a.marge_estimee_montant > 0 ? '<span class="px-2 py-0.5 rounded text-[11px] font-bold bg-emerald-500/20 text-emerald-300 border border-emerald-500/30">💰 ' + margeMontant + ' ' + margePct + '</span>' : '') +
                '</div>' +
                '<span class="text-[11px] text-slate-500 font-medium">' + (a.agence || '') + '</span>' +
              '</div>' +
              '<h3 class="text-base font-bold text-white leading-snug"><a href="' + a.url + '" target="_blank" rel="noopener" class="hover:text-amber-400 transition">' + (a.titre || a.url) + '</a></h3>' +
              '<div class="grid grid-cols-3 gap-2 bg-slate-950 p-2.5 rounded-xl border border-slate-800/80 my-3 text-center">' +
                '<div><span class="text-[9px] uppercase text-slate-500 block">Prix FAI</span><b class="text-sm font-bold text-white">' + (a.prix ? fmt(a.prix) + ' €' : '–') + '</b></div>' +
                '<div><span class="text-[9px] uppercase text-slate-500 block">Surface</span><b class="text-sm font-bold text-slate-300">' + (a.surface ? a.surface + ' m²' : '–') + '</b></div>' +
                '<div><span class="text-[9px] uppercase text-slate-500 block">Prix au m²</span><b class="text-sm font-bold text-amber-300">' + (a.prix_m2 ? a.prix_m2 + ' €' : '–') + '</b></div>' +
              '</div>' +
              '<div class="text-xs text-slate-400 line-clamp-2">' + (a.mots_detectes || '') + '</div>' +
            '</div>' +
            '<div class="pt-3 border-t border-slate-800 flex items-center justify-between gap-2 flex-wrap">' +
              '<div class="flex items-center gap-2">' +
                '<a href="' + a.url + '" target="_blank" rel="noopener" class="text-xs text-slate-400 hover:text-white underline">Fiche ↗</a>' +
                '<button class="btn-detail-card text-xs text-amber-400 hover:text-amber-300 underline font-medium cursor-pointer">Piliers</button>' +
              '</div>' +
              '<div class="flex items-center gap-2">' +
                '<button class="btn-audit-card px-3 py-1.5 bg-gradient-to-r from-amber-500/25 to-indigo-500/25 hover:from-amber-500/35 hover:to-indigo-500/35 text-amber-200 border border-amber-500/40 text-xs font-bold rounded-lg cursor-pointer transition">🔬 Audit Approfondi</button>' +
                '<button class="btn-simu-card px-3 py-1.5 bg-amber-500 hover:bg-amber-400 text-slate-950 text-xs font-bold rounded-lg cursor-pointer transition">🧮 Simuler</button>' +
              '</div>' +
            '</div>';

          const itemIndex = DATA.indexOf(a);
          card.querySelector('.btn-score-card').onclick = function() { afficherModalScoring(itemIndex); };
          card.querySelector('.btn-detail-card').onclick = function() { afficherModalScoring(itemIndex); };
          card.querySelector('.btn-audit-card').onclick = function() { lancerAuditApprofondi(a); };
          card.querySelector('.btn-simu-card').onclick = function() { simulerDeal(a.prix || 0, a.surface || 0, a.titre || '', a.dpe || '?', a.agence || ''); };

          grid.appendChild(card);
        } catch (e) {
          console.warn('Erreur affichage carte:', e);
        }
      });
    }

    try { renderDeals(); } catch (e) { console.error('renderDeals', e); }
    try { initAuditDealSelector(); } catch (e) { console.error('initAuditDealSelector', e); }
    try { renderLotsTable(); } catch (e) { console.error('renderLotsTable', e); }
  </script>
</body>
</html>"""

def exporter_html(annonces: list[Annonce]):
    DOSSIER_DOCS.mkdir(exist_ok=True)
    donnees = json.dumps([asdict(a) for a in annonces], ensure_ascii=False)
    donnees = donnees.replace("</", "<\\/").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    html = TEMPLATE_HTML.replace("__DATA__", donnees).replace("__DATE__", datetime.now().strftime("%d/%m/%Y à %H:%M"))
    FICHIER_HTML.write_text(html, encoding="utf-8")
    (DOSSIER_DOCS / ".nojekyll").touch()
    log.info("📄 Rapport HTML généré : %s (%d annonces)", FICHIER_HTML, len(annonces))

def exporter_csv(annonces: list[Annonce]):
    DATA_DIR.mkdir(exist_ok=True)
    if not annonces:
        return
    with FICHIER_CSV.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(asdict(annonces[0])), delimiter=";")
        w.writeheader()
        w.writerows(asdict(a) for a in annonces)

# --------------------------------------------------------------------------- #
# MAIN SCRAPER RUNNER
# --------------------------------------------------------------------------- #
def main():
    DATA_DIR.mkdir(exist_ok=True)
    etat = {}
    if FICHIER_ETAT.exists():
        try:
            etat = json.loads(FICHIER_ETAT.read_text(encoding="utf-8"))
        except Exception:
            etat = {}

    log.info("🚀 Démarrage du scraper MDB Millau (état actuel : %d annonces en mémoire)", len(etat))

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(user_agent=USER_AGENT, locale="fr-FR")
            context.route(
                "**/*",
                lambda route: route.abort()
                if route.request.resource_type in ("image", "font", "media")
                else route.continue_(),
            )

            liens = get_liens_agences_locales(context)
            log.info("✨ %d liens de fiches collectés au total", len(liens))

            inconnues = [(u, a) for u, a in liens.items() if u not in etat]
            obsoletes = [(u, a) for u, a in liens.items() if u in etat and etat[u].get("v") != VERSION_PARSEUR]
            a_traiter = (inconnues + obsoletes)[:MAX_FICHES_PAR_RUN]
            log.info("🆕 %d fiches à analyser (%d nouvelles, %d à réanalyser)...", len(a_traiter), len(inconnues), len(obsoletes))

            page = context.new_page()
            file = deque(a_traiter)

            while file:
                url, agence = file.popleft()
                deja_connue = url in etat

                try:
                    annonce = analyser_fiche(page, url, agence, etat)
                except Exception as e:
                    log.warning("Erreur analyse %s : %s", url, e)
                    REJETS["erreur technique"] += 1
                    continue

                if annonce:
                    annonce.nouvelle = not deja_connue
                    etat[url] = asdict(annonce)
                    if (annonce.nouvelle and annonce.score >= SCORE_MINIMUM_ALERTE) or annonce.a_baisse:
                        motif = "BAISSE DE PRIX" if annonce.a_baisse else "NOUVELLE PÉPITE"
                        envoyer_alerte_push(annonce, motif)
                else:
                    etat[url] = {"url": url, "ignoree": True, "v": VERSION_PARSEUR}
                pause()

        finally:
            browser.close()

    FICHIER_ETAT.write_text(json.dumps(etat, ensure_ascii=False, indent=1), encoding="utf-8")

    if REJETS:
        log.info("🔎 Raisons de rejet ce run : %s", dict(REJETS))

    annonces = []
    for d in etat.values():
        if d.get("ignoree"):
            continue
        titre_l = (d.get("titre") or "").lower()
        surface_val = d.get("surface")

        if EXCLURE_TERRAINS_SEULS:
            est_terrain_nu = (
                surface_val is None and any(w in titre_l for w in ["terrain", "parcelle"]) and
                not any(w in titre_l for w in ["maison", "immeuble", "grange", "bâtisse", "remise", "atelier", "villa", "appartement", "corps de ferme", "ruine", "mazet", "plateau"])
            )
            if est_terrain_nu:
                continue

        try:
            annonces.append(Annonce(**{k: v for k, v in d.items() if k in CHAMPS_ANNONCE}))
        except Exception as e:
            log.warning("Entrée d'état illisible ignorée (%s) : %s", d.get("url"), e)

    if PRIX_MAX:
        annonces = [a for a in annonces if a.prix is None or a.prix <= PRIX_MAX]
    annonces.sort(key=lambda a: (a.score_v3 or 0, a.score or 0), reverse=True)

    exporter_csv(annonces)
    exporter_html(annonces)

    interessantes = [a for a in annonces if a.score >= SCORE_MINIMUM]
    log.info("🎯 Scan terminé ! %d annonces publiées (%d à fort potentiel MDB).", len(annonces), len(interessantes))
    print(f"\n✅ Terminé : {len(annonces)} annonces publiées dans docs/index.html")

if __name__ == "__main__":
    main()
