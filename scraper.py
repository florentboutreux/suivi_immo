#!/usr/bin/env python3
"""
=============================================================================
VEILLE IMMOBILIÈRE SPÉCIALE MARCHAND DE BIENS (MDB) – MILLAU (12100) v3.0
=============================================================================
"""

import csv
import json
import logging
import os
import random
import re
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
EXCLURE_TERRAINS_SEULS = True
MAX_PAGES_PAR_AGENCE = 5
MAX_FICHES_PAR_RUN = 200
VERSION_PARSEUR = 11        # Incrémentée pour v3.0 (secteur + type_bien + scoring calibré)
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
    secteur: str = "Périphérie Millau"
    type_bien: str = "maison"
    v: int = VERSION_PARSEUR
    photos: list = field(default_factory=list)

CHAMPS_ANNONCE = {f.name for f in fields(Annonce)}

def inferer_secteur(texte: str) -> str:
    t = texte.lower()
    if any(k in t for k in ("droite", "halles", "centre ancien", "capelle", "foch")):
        return "Centre Ancien"
    if any(k in t for k in ("mandarous", "jaurès", "jaures", "république", "bonald")):
        return "Mandarous / Jaurès"
    if any(k in t for k in ("tarn", "faubourg", "viaduc", "cureplat", "pouncho", "paulhe")):
        return "Faubourgs / Tarn"
    if any(k in t for k in ("gare", "malhourtet", "beauregard")):
        return "Gare / Malhourtet"
    return "Périphérie Millau"

def inferer_type_bien(texte: str) -> str:
    t = texte.lower()
    if any(k in t for k in ("immeuble", "monopropri", "bâtisse")):
        return "immeuble"
    if any(k in t for k in ("grange", "plateau", "remise", "atelier")):
        return "grange"
    if any(k in t for k in ("appartement", "t2", "t3", "t4", "studio")):
        return "appartement"
    return "maison"

# --------------------------------------------------------------------------- #
# NOTIFICATIONS TELEGRAM & DISCORD
# --------------------------------------------------------------------------- #
def envoyer_alerte_push(annonce: Annonce, motif: str = "PÉPITE DÉTECTÉE"):
    texte = (
        f"🚨 <b>MILLAU MDB : {motif} !</b>\n"
        f"🏢 <b>{annonce.titre}</b>\n"
        f"📍 {annonce.agence} ({annonce.secteur}) | DPE : <b>{annonce.dpe}</b>\n"
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
                    log.info("📉 BAISSE VALIDÉE (-%.1f%%) sur %s : %d € -> %d €", baisse_prix_pct, url, ancien_prix, prix)

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
        ca_estime = int(surface * 1900)
        cout_travaux_m2 = 780 if dpe == "G" else (680 if dpe == "F" else 550)
        total_travaux = int(surface * cout_travaux_m2)
        frais_mdb = int((prix * 0.022) + 3000 + (prix * 0.03))
        cout_revient = prix + total_travaux + frais_mdb
        marge_estimee_montant = ca_estime - cout_revient
        marge_estimee_pct = round((marge_estimee_montant / ca_estime) * 100, 1) if ca_estime > 0 else 0.0

        pts_marge = 35 if marge_estimee_pct >= 22 else (30 if marge_estimee_pct >= 18 else (20 if marge_estimee_pct >= 14 else (10 if marge_estimee_pct >= 10 else 0)))
        est_monopro = "immeuble" in texte_bas or "monopropri" in texte_bas
        est_divisible = "divisible" in texte_bas or "plateau" in texte_bas or surface >= 140
        pts_decoupe = 25 if est_monopro else (18 if est_divisible else 10)
        pts_negoc = min(20, (8 if dpe == "G" else (6 if dpe == "F" else 0)) + (8 if baisse_prix_pct >= 10 else (5 if baisse_prix_pct >= 5 else (3 if baisse_prix_pct > 0 else 0))) + (4 if prix_m2 and prix_m2 < 850 else (2 if prix_m2 and prix_m2 < 1050 else 0)))
        pts_securite = min(20, (8 if est_monopro else 5) + (8 if prix <= 150000 else (6 if prix <= 210000 else 3)) + 4)
        score_v3 = round(min(10.0, (pts_marge + pts_decoupe + pts_negoc + pts_securite) / 10.0), 1)

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

    texte_complet = f"{titre} {' '.join(detectes)} {base[:1200]}"
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
        secteur=inferer_secteur(texte_complet),
        type_bien=inferer_type_bien(texte_complet),
        photos=photos
    )

# --------------------------------------------------------------------------- #
# EXPORT HTML ARCHITECTURAL MULTI-VUES (GITHUB PAGES)
# --------------------------------------------------------------------------- #
TEMPLATE_HTML = r"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Millau Foncier — Veille &amp; Arbitrage MDB (12100)</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700&family=Plus+Jakarta+Sans:wght@500;600;700;800&family=Public+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<script src="https://cdn.tailwindcss.com"></script>
<script>
  tailwind.config = {
    darkMode: 'class',
    theme: {
      extend: {
        fontFamily: {
          display: ['Plus Jakarta Sans', 'system-ui', 'sans-serif'],
          sans: ['Public Sans', 'system-ui', 'sans-serif'],
          mono: ['JetBrains Mono', 'monospace']
        }
      }
    }
  };
</script>
<style>
  .tabular-nums { font-variant-numeric: tabular-nums; }
  @media print { aside, header, .no-print { display: none !important; } }
</style>
</head>
<body class="bg-stone-50 text-slate-900 dark:bg-slate-950 dark:text-slate-100 min-h-screen font-sans antialiased">
<div class="min-h-screen flex">
  <!-- SIDEBAR DESKTOP -->
  <aside class="hidden lg:flex lg:flex-col lg:w-64 lg:shrink-0 border-r border-stone-200 dark:border-slate-800 bg-white dark:bg-slate-900 sticky top-0 h-screen">
    <div class="px-6 py-5 border-b border-stone-200 dark:border-slate-800">
      <div class="font-display text-lg font-bold tracking-tight text-slate-900 dark:text-white">Millau Foncier</div>
      <div class="text-xs text-slate-500 dark:text-slate-400 mt-0.5">Analyse &amp; Arbitrage MDB · 12100</div>
    </div>
    <nav class="flex-1 px-3 py-5 space-y-1">
      <button onclick="setTab('deals')" id="nav-deals" class="w-full text-left px-3.5 py-2.5 rounded-lg cursor-pointer">
        <span class="block text-sm font-semibold">Marché &amp; Opportunités</span>
        <span class="block text-xs opacity-75">Veille agences Millau</span>
      </button>
      <button onclick="setTab('audit')" id="nav-audit" class="w-full text-left px-3.5 py-2.5 rounded-lg cursor-pointer">
        <span class="block text-sm font-semibold">Audit Technique</span>
        <span class="block text-xs opacity-75">Travaux &amp; stratégies</span>
      </button>
      <button onclick="setTab('simu')" id="nav-simu" class="w-full text-left px-3.5 py-2.5 rounded-lg cursor-pointer">
        <span class="block text-sm font-semibold">Simulateur MDB</span>
        <span class="block text-xs opacity-75">Découpe, TVA &amp; IS</span>
      </button>
      <button onclick="setTab('strategie')" id="nav-strategie" class="w-full text-left px-3.5 py-2.5 rounded-lg cursor-pointer">
        <span class="block text-sm font-semibold">Stratégie &amp; Secteur</span>
        <span class="block text-xs opacity-75">Micro-marchés 12100</span>
      </button>
    </nav>
    <div class="px-5 py-4 border-t border-stone-200 dark:border-slate-800 space-y-2 text-xs">
      <div class="font-semibold text-slate-500 dark:text-slate-400">Pipeline d'acquisition (<span id="pipe-total">0</span>)</div>
      <div class="flex justify-between"><span class="text-slate-600 dark:text-slate-400">À étudier</span><span id="pipe-etude" class="font-mono font-semibold">0</span></div>
      <div class="flex justify-between"><span class="text-slate-600 dark:text-slate-400">Visites planifiées</span><span id="pipe-visite" class="font-mono font-semibold text-amber-700 dark:text-amber-400">0</span></div>
      <div class="flex justify-between"><span class="text-slate-600 dark:text-slate-400">Offres transmises</span><span id="pipe-offre" class="font-mono font-semibold text-emerald-700 dark:text-emerald-400">0</span></div>
      <div class="pt-2 text-[11px] text-slate-400">Mise à jour : __DATE__</div>
    </div>
  </aside>

  <div class="flex-1 min-w-0 flex flex-col">
    <!-- TOP BAR -->
    <header class="sticky top-0 z-30 bg-white/95 dark:bg-slate-900/95 backdrop-blur border-b border-stone-200 dark:border-slate-800">
      <div class="max-w-6xl mx-auto px-4 sm:px-6 h-14 flex items-center justify-between gap-4">
        <div class="flex items-center gap-2 text-xs min-w-0">
          <span class="font-display font-bold text-slate-900 dark:text-white lg:hidden whitespace-nowrap">Millau Foncier</span>
          <span class="hidden lg:inline text-slate-500 dark:text-slate-400">Millau 12100</span>
          <span class="hidden lg:inline text-slate-400">/</span>
          <h1 id="top-title" class="hidden lg:block font-display text-sm font-bold text-slate-900 dark:text-white truncate">Marché &amp; Opportunités</h1>
        </div>
        <div class="flex items-center gap-2 shrink-0">
          <button onclick="toggleTheme()" id="btn-theme" class="px-3 py-1.5 rounded-lg border border-stone-200 dark:border-slate-700 text-xs font-medium hover:bg-stone-100 dark:hover:bg-slate-800 cursor-pointer whitespace-nowrap">Thème sombre</button>
          <button onclick="exporterCsv()" class="px-3.5 py-1.5 rounded-lg bg-slate-900 dark:bg-amber-500 text-white dark:text-slate-950 text-xs font-semibold hover:opacity-90 cursor-pointer whitespace-nowrap">Exporter CSV</button>
        </div>
      </div>
      <nav class="lg:hidden border-t border-stone-200 dark:border-slate-800 px-3 py-1.5 flex items-center gap-1 overflow-x-auto">
        <button onclick="setTab('deals')" id="mnav-deals" class="px-3 py-1.5 rounded-md text-xs font-semibold whitespace-nowrap cursor-pointer">Marché</button>
        <button onclick="setTab('audit')" id="mnav-audit" class="px-3 py-1.5 rounded-md text-xs font-semibold whitespace-nowrap cursor-pointer">Audit</button>
        <button onclick="setTab('simu')" id="mnav-simu" class="px-3 py-1.5 rounded-md text-xs font-semibold whitespace-nowrap cursor-pointer">Simulateur</button>
        <button onclick="setTab('strategie')" id="mnav-strategie" class="px-3 py-1.5 rounded-md text-xs font-semibold whitespace-nowrap cursor-pointer">Secteur</button>
      </nav>
    </header>

    <main class="flex-1 max-w-6xl w-full mx-auto px-4 sm:px-6 py-6">
      <!-- VUE 1 : MARCHÉ & OPPORTUNITÉS -->
      <div id="tab-deals" class="space-y-6">
        <section id="kpi-cards" class="grid grid-cols-2 lg:grid-cols-4 gap-3 sm:gap-4"></section>

        <section class="bg-white dark:bg-slate-900 border border-stone-200 dark:border-slate-800 rounded-xl p-5">
          <div class="flex flex-wrap items-baseline justify-between gap-3 mb-3">
            <div>
              <h2 class="font-display text-sm font-bold text-slate-900 dark:text-white">Positionnement Prix / m² par surface (Millau 12100)</h2>
              <p class="text-xs text-slate-500 dark:text-slate-400 mt-0.5">Zone d'achat décotée (&lt; 1 100 €/m²) vs valeur cible rénovée (1 900 €/m²)</p>
            </div>
            <div class="text-xs text-slate-500 dark:text-slate-400">Cliquez un point pour auditer</div>
          </div>
          <div id="scatter-wrap"></div>
          <div id="scatter-hover" class="mt-2 pt-2 border-t border-stone-100 dark:border-slate-800 text-xs text-slate-500 dark:text-slate-400">Survolez un point pour afficher le bien.</div>
        </section>

        <section class="bg-white dark:bg-slate-900 border border-stone-200 dark:border-slate-800 rounded-xl p-4 space-y-3">
          <div class="flex flex-wrap items-center gap-3">
            <input type="text" id="q" oninput="renderDeals()" placeholder="Rechercher par rue, agence, mot-clé (ex: Rue Droite, immeuble)..." class="flex-1 min-w-[220px] px-3 py-2 bg-stone-50 dark:bg-slate-950 border border-stone-200 dark:border-slate-700 rounded-lg text-xs focus:outline-none focus:border-slate-900 dark:focus:border-amber-500" />
            <select id="f-type" onchange="renderDeals()" class="px-3 py-2 bg-stone-50 dark:bg-slate-950 border border-stone-200 dark:border-slate-700 rounded-lg text-xs">
              <option value="all">Tous types de biens</option>
              <option value="immeuble">Immeubles</option>
              <option value="maison">Maisons</option>
              <option value="appartement">Appartements</option>
              <option value="grange">Granges &amp; Plateaux</option>
            </select>
            <select id="f-secteur" onchange="renderDeals()" class="px-3 py-2 bg-stone-50 dark:bg-slate-950 border border-stone-200 dark:border-slate-700 rounded-lg text-xs">
              <option value="all">Tous secteurs Millau</option>
              <option value="Centre Ancien">Centre Ancien</option>
              <option value="Mandarous / Jaurès">Mandarous / Jaurès</option>
              <option value="Faubourgs / Tarn">Faubourgs / Tarn</option>
              <option value="Gare / Malhourtet">Gare / Malhourtet</option>
              <option value="Périphérie Millau">Périphérie Millau</option>
            </select>
            <select id="f-dpe" onchange="renderDeals()" class="px-3 py-2 bg-stone-50 dark:bg-slate-950 border border-stone-200 dark:border-slate-700 rounded-lg text-xs">
              <option value="all">Tous DPE</option>
              <option value="passoires">Passoires F &amp; G</option>
              <option value="G">DPE G uniquement</option>
              <option value="F">DPE F uniquement</option>
            </select>
            <select id="f-sort" onchange="renderDeals()" class="px-3 py-2 bg-stone-50 dark:bg-slate-950 border border-stone-200 dark:border-slate-700 rounded-lg text-xs font-semibold">
              <option value="score_desc">Tri : Score V3 décroissant</option>
              <option value="marge_desc">Tri : Marge estimée décroissante</option>
              <option value="prix_asc">Tri : Prix croissant</option>
              <option value="prix_desc">Tri : Prix décroissant</option>
              <option value="m2_asc">Tri : Prix / m² croissant</option>
              <option value="surface_desc">Tri : Surface décroissante</option>
            </select>
          </div>
          <div class="flex flex-wrap items-center justify-between gap-3 pt-2 border-t border-stone-100 dark:border-slate-800 text-xs">
            <div class="flex flex-wrap items-center gap-4">
              <label class="flex items-center gap-2 cursor-pointer">
                <input type="checkbox" id="f-drops" onchange="renderDeals()" class="rounded border-stone-300 dark:border-slate-700" />
                <span>Baisses de prix uniquement</span>
              </label>
              <select id="f-statut" onchange="renderDeals()" class="bg-transparent text-xs font-medium cursor-pointer">
                <option value="all">Tous statuts</option>
                <option value="nouveau">Nouveau</option>
                <option value="a_etudier">À étudier</option>
                <option value="visite">Visite planifiée</option>
                <option value="offre">Offre envoyée</option>
                <option value="ecarte">Écarté</option>
              </select>
              <span id="deals-count" class="font-mono text-slate-500 dark:text-slate-400"></span>
            </div>
            <div class="flex items-center gap-1 p-0.5 bg-stone-100 dark:bg-slate-800 rounded-lg border border-stone-200 dark:border-slate-700">
              <button onclick="setViewMode('grid')" id="btn-view-grid" class="px-2.5 py-1 rounded-md text-xs font-semibold cursor-pointer">Cartes</button>
              <button onclick="setViewMode('table')" id="btn-view-table" class="px-2.5 py-1 rounded-md text-xs font-semibold cursor-pointer">Tableau</button>
            </div>
          </div>
        </section>

        <div id="deals-container"></div>
      </div>

      <!-- VUE 2 : AUDIT TECHNIQUE -->
      <div id="tab-audit" class="hidden space-y-6">
        <section class="bg-white dark:bg-slate-900 border border-stone-200 dark:border-slate-800 rounded-xl p-5 flex flex-wrap items-center justify-between gap-4">
          <div class="min-w-0">
            <div id="ap-meta" class="text-xs text-slate-500 dark:text-slate-400">Audit Technique &amp; Arbitrage</div>
            <h2 id="ap-titre" class="font-display text-lg font-bold mt-0.5 truncate">Sélectionnez un bien</h2>
            <div id="ap-metrics" class="text-xs font-mono text-slate-600 dark:text-slate-300 mt-1"></div>
          </div>
          <div class="flex items-center gap-2 no-print">
            <select id="ap-select-deal" onchange="changerDealAudit(this.value)" class="max-w-xs px-3 py-2 bg-stone-50 dark:bg-slate-950 border border-stone-200 dark:border-slate-700 rounded-lg text-xs font-semibold"></select>
            <button onclick="window.print()" class="px-3 py-2 rounded-lg border border-stone-200 dark:border-slate-700 text-xs font-semibold cursor-pointer">Imprimer PDF</button>
          </div>
        </section>

        <section class="grid grid-cols-1 lg:grid-cols-12 gap-6">
          <div class="lg:col-span-7 bg-white dark:bg-slate-900 border border-stone-200 dark:border-slate-800 rounded-xl p-5 space-y-3">
            <h3 class="font-display text-sm font-bold">Relevé visuel de l'annonce</h3>
            <div id="ap-photos-grid" class="grid grid-cols-2 gap-3"></div>
            <div id="ap-photos-constat" class="text-xs text-slate-500 dark:text-slate-400 pt-2 border-t border-stone-100 dark:border-slate-800"></div>
          </div>
          <div class="lg:col-span-5 bg-white dark:bg-slate-900 border border-stone-200 dark:border-slate-800 rounded-xl p-5 space-y-3">
            <h3 class="font-display text-sm font-bold">Points de contrôle techniques (Millau)</h3>
            <div id="ap-diagnostics-list" class="divide-y divide-stone-100 dark:divide-slate-800 text-xs"></div>
          </div>
        </section>

        <section class="bg-white dark:bg-slate-900 border border-stone-200 dark:border-slate-800 rounded-xl p-5 space-y-4">
          <div class="flex flex-wrap items-baseline justify-between gap-2">
            <div>
              <h3 class="font-display text-sm font-bold">Chiffrage estimatif des travaux (modifiable)</h3>
              <p class="text-xs text-slate-500 dark:text-slate-400">Ajustez chaque poste TTC pour recalibrer les 3 stratégies de sortie.</p>
            </div>
            <div id="ap-total-travaux-badge" class="font-mono text-sm font-bold text-slate-900 dark:text-amber-300"></div>
          </div>
          <div class="overflow-x-auto border border-stone-200 dark:border-slate-800 rounded-lg">
            <table class="w-full text-xs text-left">
              <thead class="bg-stone-50 dark:bg-slate-950 text-slate-500 dark:text-slate-400 border-b border-stone-200 dark:border-slate-800">
                <tr>
                  <th class="py-2.5 px-3 w-10 text-center">Incl.</th>
                  <th class="py-2.5 px-3 font-semibold">Poste</th>
                  <th class="py-2.5 px-3 font-semibold">Diagnostic</th>
                  <th class="py-2.5 px-3 font-semibold">Préconisation</th>
                  <th class="py-2.5 px-3 font-semibold text-right w-32">Budget TTC</th>
                </tr>
              </thead>
              <tbody id="ap-travaux-tbody" class="divide-y divide-stone-100 dark:divide-slate-800"></tbody>
            </table>
          </div>
        </section>

        <section class="bg-white dark:bg-slate-900 border border-stone-200 dark:border-slate-800 rounded-xl p-5 space-y-4">
          <h3 class="font-display text-sm font-bold">Comparatif des 3 stratégies de sortie MDB</h3>
          <div id="ap-scenarios-cards" class="grid grid-cols-1 md:grid-cols-3 gap-4"></div>
          <div class="pt-4 border-t border-stone-200 dark:border-slate-800 flex flex-wrap items-center justify-between gap-4">
            <p id="ap-verdict-texte" class="text-xs text-slate-600 dark:text-slate-300 max-w-2xl"></p>
            <button onclick="injecterScenarioDansSimulateur()" class="px-4 py-2.5 rounded-lg bg-slate-900 dark:bg-amber-500 text-white dark:text-slate-950 text-xs font-semibold cursor-pointer">Transférer ce scénario au Simulateur</button>
          </div>
        </section>
      </div>

      <!-- VUE 3 : SIMULATEUR MDB -->
      <div id="tab-simu" class="hidden space-y-6">
        <section class="bg-white dark:bg-slate-900 border border-stone-200 dark:border-slate-800 rounded-xl p-5 flex flex-wrap items-center justify-between gap-4">
          <div>
            <div id="simu-context-meta" class="text-xs text-slate-500 dark:text-slate-400">Simulation financière MDB · Millau 12100</div>
            <h2 id="simu-context-titre" class="font-display text-lg font-bold mt-0.5">Ensemble immobilier Millau</h2>
          </div>
          <button onclick="window.print()" class="px-3.5 py-2 rounded-lg border border-stone-200 dark:border-slate-700 text-xs font-semibold cursor-pointer no-print">Imprimer le dossier</button>
        </section>

        <div class="grid grid-cols-1 lg:grid-cols-12 gap-6 items-start">
          <div class="lg:col-span-7 space-y-6">
            <section class="bg-white dark:bg-slate-900 border border-stone-200 dark:border-slate-800 rounded-xl p-5 space-y-4">
              <h3 class="font-display text-sm font-bold">01. Acquisition &amp; régime fiscal</h3>
              <div class="grid grid-cols-1 sm:grid-cols-3 gap-4 text-xs">
                <div>
                  <label class="block text-slate-500 mb-1">Prix affiché FAI (€)</label>
                  <input type="number" id="sim-prix" value="149000" oninput="calculerSimu()" class="w-full px-3 py-2 bg-stone-50 dark:bg-slate-950 border border-stone-200 dark:border-slate-700 rounded-lg font-mono font-bold" />
                </div>
                <div>
                  <label class="block text-slate-500 mb-1">Négociation visée (€)</label>
                  <input type="number" id="sim-nego" value="14000" oninput="calculerSimu()" class="w-full px-3 py-2 bg-stone-50 dark:bg-slate-950 border border-stone-200 dark:border-slate-700 rounded-lg font-mono text-amber-700 dark:text-amber-300" />
                </div>
                <div>
                  <label class="block text-slate-500 mb-1">Droits de mutation</label>
                  <select id="sim-notaire" onchange="calculerSimu()" class="w-full px-3 py-2 bg-stone-50 dark:bg-slate-950 border border-stone-200 dark:border-slate-700 rounded-lg font-mono text-xs">
                    <option value="0.00715">MDB (0,715%)</option>
                    <option value="0.078">Classique (7,8%)</option>
                  </select>
                </div>
              </div>
              <label class="flex items-center gap-2 pt-2 border-t border-stone-100 dark:border-slate-800 text-xs cursor-pointer">
                <input type="checkbox" id="sim-tva-marge" onchange="calculerSimu()" class="rounded border-stone-300 dark:border-slate-700" />
                <span>Provisionner la TVA sur marge (20% sur revente − achat)</span>
              </label>
            </section>

            <section class="bg-white dark:bg-slate-900 border border-stone-200 dark:border-slate-800 rounded-xl p-5 space-y-4">
              <div class="flex items-center justify-between">
                <h3 class="font-display text-sm font-bold">02. Plan de découpe &amp; revente par lot</h3>
                <button onclick="ajouterLot()" class="px-3 py-1.5 rounded-lg border border-stone-200 dark:border-slate-700 text-xs font-semibold cursor-pointer">+ Ajouter un lot</button>
              </div>
              <div class="overflow-x-auto">
                <table class="w-full text-xs text-left">
                  <thead class="text-slate-500 border-b border-stone-200 dark:border-slate-800">
                    <tr>
                      <th class="py-2">Lot</th>
                      <th class="py-2 w-16 text-right">m²</th>
                      <th class="py-2 w-24 text-right">Travaux</th>
                      <th class="py-2 w-28 text-right">Revente</th>
                      <th class="py-2 w-16 text-right">€/m²</th>
                      <th class="py-2 w-8"></th>
                    </tr>
                  </thead>
                  <tbody id="lots-table-body" class="divide-y divide-stone-100 dark:divide-slate-800"></tbody>
                </table>
              </div>
              <div class="grid grid-cols-3 gap-2 pt-2 border-t border-stone-100 dark:border-slate-800 text-xs">
                <div>Surface : <strong id="total-surf" class="font-mono">--</strong></div>
                <div>Travaux : <strong id="total-travaux-lots" class="font-mono text-amber-700 dark:text-amber-300">--</strong></div>
                <div class="text-right">CA : <strong id="total-revente-lots" class="font-mono text-emerald-700 dark:text-emerald-400">--</strong></div>
              </div>
            </section>

            <section class="bg-white dark:bg-slate-900 border border-stone-200 dark:border-slate-800 rounded-xl p-5 space-y-4">
              <h3 class="font-display text-sm font-bold">03. Division juridique &amp; portage financier</h3>
              <div class="grid grid-cols-2 sm:grid-cols-5 gap-3 text-xs">
                <div><label class="block text-slate-500 mb-1">Géomètre (€)</label><input type="number" id="sim-geometre" value="3200" oninput="calculerSimu()" class="w-full px-2.5 py-1.5 bg-stone-50 dark:bg-slate-950 border border-stone-200 dark:border-slate-700 rounded-lg font-mono" /></div>
                <div><label class="block text-slate-500 mb-1">Divers (€)</label><input type="number" id="sim-divers" value="1500" oninput="calculerSimu()" class="w-full px-2.5 py-1.5 bg-stone-50 dark:bg-slate-950 border border-stone-200 dark:border-slate-700 rounded-lg font-mono" /></div>
                <div><label class="block text-slate-500 mb-1">Portage (mois)</label><input type="number" id="sim-mois" value="8" oninput="calculerSimu()" class="w-full px-2.5 py-1.5 bg-stone-50 dark:bg-slate-950 border border-stone-200 dark:border-slate-700 rounded-lg font-mono" /></div>
                <div><label class="block text-slate-500 mb-1">Taux (%)</label><input type="number" step="0.1" id="sim-taux" value="3.8" oninput="calculerSimu()" class="w-full px-2.5 py-1.5 bg-stone-50 dark:bg-slate-950 border border-stone-200 dark:border-slate-700 rounded-lg font-mono" /></div>
                <div><label class="block text-slate-500 mb-1">Apport (€)</label><input type="number" id="sim-apport" value="35000" oninput="calculerSimu()" class="w-full px-2.5 py-1.5 bg-stone-50 dark:bg-slate-950 border border-stone-200 dark:border-slate-700 rounded-lg font-mono text-emerald-700 dark:text-emerald-400" /></div>
              </div>
            </section>
          </div>

          <div class="lg:col-span-5 space-y-6">
            <section class="bg-white dark:bg-slate-900 border border-stone-200 dark:border-slate-800 rounded-xl p-5 space-y-4">
              <h3 class="font-display text-sm font-bold">Bilan financier prévisionnel</h3>
              <div class="py-4 border-y border-stone-200 dark:border-slate-800 flex items-baseline justify-between">
                <div>
                  <span class="text-xs text-slate-500 block">Marge brute opération</span>
                  <span id="res-marge-brute" class="font-mono text-3xl font-bold text-emerald-700 dark:text-emerald-400">--</span>
                </div>
                <div class="text-right">
                  <span class="text-xs text-slate-500 block">Taux de marge</span>
                  <span id="res-marge-pct" class="font-mono text-lg font-bold">--</span>
                </div>
              </div>
              <div class="grid grid-cols-3 gap-3 text-xs pb-3 border-b border-stone-100 dark:border-slate-800">
                <div><span class="text-slate-500 block">Marge nette IS</span><strong id="res-marge-nette" class="font-mono">--</strong></div>
                <div><span class="text-slate-500 block">ROI apport</span><strong id="res-roi" class="font-mono text-amber-700 dark:text-amber-400">--</strong></div>
                <div><span class="text-slate-500 block">TRI annualisé</span><strong id="res-tri" class="font-mono">--</strong></div>
              </div>
              <dl class="space-y-2 text-xs font-mono">
                <div class="flex justify-between"><dt class="font-sans text-slate-500">Prix achat net</dt><dd id="res-achat">--</dd></div>
                <div class="flex justify-between"><dt class="font-sans text-slate-500">Frais notaire</dt><dd id="res-notaire">--</dd></div>
                <div class="flex justify-between"><dt class="font-sans text-slate-500">Travaux</dt><dd id="res-travaux" class="text-amber-700 dark:text-amber-300">--</dd></div>
                <div class="flex justify-between"><dt class="font-sans text-slate-500">Géomètre &amp; divers</dt><dd id="res-geometre">--</dd></div>
                <div class="flex justify-between"><dt class="font-sans text-slate-500">Portage financier</dt><dd id="res-portage">--</dd></div>
                <div class="flex justify-between pt-2 border-t border-stone-200 dark:border-slate-800 font-bold"><dt class="font-sans">Coût de revient</dt><dd id="res-revient">--</dd></div>
                <div class="flex justify-between font-bold text-emerald-700 dark:text-emerald-400"><dt class="font-sans">Chiffre d'affaires</dt><dd id="res-revente">--</dd></div>
                <div id="row-tva" class="hidden flex justify-between text-rose-700 dark:text-rose-400"><dt class="font-sans">TVA sur marge</dt><dd id="res-tva">--</dd></div>
                <div class="flex justify-between text-rose-700 dark:text-rose-400"><dt class="font-sans">IS estimé (15%/25%)</dt><dd id="res-is">--</dd></div>
              </dl>
              <div class="pt-3 border-t border-stone-100 dark:border-slate-800 text-xs text-slate-500">Économie notaire MDB : <strong id="res-gain-notaire" class="font-mono text-emerald-700 dark:text-emerald-400">--</strong></div>
            </section>

            <section class="bg-white dark:bg-slate-900 border border-stone-200 dark:border-slate-800 rounded-xl p-5 space-y-3">
              <h3 class="font-display text-sm font-bold">Test de sensibilité (stress-test)</h3>
              <div id="simu-sensibilite" class="divide-y divide-stone-100 dark:divide-slate-800 text-xs"></div>
            </section>
          </div>
        </div>

        <section class="bg-white dark:bg-slate-900 border border-stone-200 dark:border-slate-800 rounded-xl p-5 space-y-3">
          <div class="flex items-center justify-between gap-3">
            <h3 class="font-display text-sm font-bold">Proposition d'offre &amp; argumentaire de négociation</h3>
            <button onclick="copierPitch()" id="btn-copy-pitch" class="px-3.5 py-1.5 rounded-lg border border-stone-200 dark:border-slate-700 text-xs font-semibold cursor-pointer no-print">Copier le courrier</button>
          </div>
          <pre id="strat-pitch" class="whitespace-pre-wrap font-sans text-xs text-slate-700 dark:text-slate-300 bg-stone-50 dark:bg-slate-950 border border-stone-200 dark:border-slate-800 rounded-lg p-4 leading-relaxed"></pre>
        </section>
      </div>

      <!-- VUE 4 : STRATÉGIE & SECTEUR MILLAU -->
      <div id="tab-strategie" class="hidden space-y-6">
        <section class="bg-white dark:bg-slate-900 border border-stone-200 dark:border-slate-800 rounded-xl p-5 space-y-4">
          <h2 class="font-display text-base font-bold">Atlas des micro-marchés de Millau (12100)</h2>
          <div class="overflow-x-auto border border-stone-200 dark:border-slate-800 rounded-lg">
            <table class="w-full text-xs text-left">
              <thead class="bg-stone-50 dark:bg-slate-950 text-slate-500 border-b border-stone-200 dark:border-slate-800">
                <tr>
                  <th class="py-3 px-4 font-semibold">Secteur &amp; Quartiers</th>
                  <th class="py-3 px-3 font-semibold">Achat brut</th>
                  <th class="py-3 px-3 font-semibold">Revente rénovée</th>
                  <th class="py-3 px-3 font-semibold">Liquidité</th>
                  <th class="py-3 px-4 font-semibold">Vigilance urbanisme</th>
                </tr>
              </thead>
              <tbody class="divide-y divide-stone-100 dark:divide-slate-800">
                <tr><td class="py-3 px-4"><strong>Centre Ancien</strong><div class="text-slate-500">Rue Droite, Halles, Capelle, Foch</div></td><td class="py-3 px-3 font-mono">600 – 950 €/m²</td><td class="py-3 px-3 font-mono text-emerald-700 dark:text-emerald-400">1 650 – 1 950 €/m²</td><td class="py-3 px-3 font-semibold text-emerald-700 dark:text-emerald-400">Élevée</td><td class="py-3 px-4 text-slate-500">Périmètre ABF (menuiseries bois, enduits chaux)</td></tr>
                <tr><td class="py-3 px-4"><strong>Mandarous / Jaurès</strong><div class="text-slate-500">Mandarous, Av. Jean Jaurès, Bd Bonald</div></td><td class="py-3 px-3 font-mono">850 – 1 150 €/m²</td><td class="py-3 px-3 font-mono text-emerald-700 dark:text-emerald-400">1 850 – 2 150 €/m²</td><td class="py-3 px-3 font-semibold text-emerald-700 dark:text-emerald-400">Élevée</td><td class="py-3 px-4 text-slate-500">Attention aux baux commerciaux RDC</td></tr>
                <tr><td class="py-3 px-4"><strong>Faubourgs / Tarn</strong><div class="text-slate-500">Bords du Tarn, Cureplat, La Pouncho</div></td><td class="py-3 px-3 font-mono">750 – 1 100 €/m²</td><td class="py-3 px-3 font-mono text-emerald-700 dark:text-emerald-400">1 800 – 2 200 €/m²</td><td class="py-3 px-3 font-semibold text-emerald-700 dark:text-emerald-400">Élevée</td><td class="py-3 px-4 text-slate-500">Vérifier PPRI Tarn sur parcelles basses</td></tr>
                <tr><td class="py-3 px-4"><strong>Gare / Malhourtet</strong><div class="text-slate-500">Gare SNCF, Malhourtet, Beauregard</div></td><td class="py-3 px-3 font-mono">650 – 950 €/m²</td><td class="py-3 px-3 font-mono text-emerald-700 dark:text-emerald-400">1 600 – 1 850 €/m²</td><td class="py-3 px-3 font-semibold text-amber-700 dark:text-amber-400">Moyenne</td><td class="py-3 px-4 text-slate-500">Changement de destination PLU zone U</td></tr>
                <tr><td class="py-3 px-4"><strong>Périphérie Millau</strong><div class="text-slate-500">Hauteurs, Massebiau, sorties ville</div></td><td class="py-3 px-3 font-mono">800 – 1 150 €/m²</td><td class="py-3 px-3 font-mono text-emerald-700 dark:text-emerald-400">1 750 – 2 100 €/m²</td><td class="py-3 px-3 font-semibold text-slate-500">Sélective</td><td class="py-3 px-4 text-slate-500">Assainissement et accès voirie</td></tr>
              </tbody>
            </table>
          </div>
        </section>

        <div class="grid grid-cols-1 md:grid-cols-2 gap-6 text-xs">
          <section class="bg-white dark:bg-slate-900 border border-stone-200 dark:border-slate-800 rounded-xl p-5 space-y-3">
            <h3 class="font-display text-sm font-bold">Les 4 piliers d'exécution Marchand de Biens</h3>
            <div class="divide-y divide-stone-100 dark:divide-slate-800">
              <div class="py-2.5"><strong>01. Monopropriétés Centre Ancien</strong><p class="text-slate-500 mt-0.5">Achat R+2/R+3 entre 600 et 850 €/m², découpe en T2/T3 sans syndic initial bloquant.</p></div>
              <div class="py-2.5"><strong>02. Passoires thermiques F &amp; G</strong><p class="text-slate-500 mt-0.5">Décote -15% à -25% appuyée par l'audit énergétique, sortie DPE C par ITI + PAC.</p></div>
              <div class="py-2.5"><strong>03. Détachement parcellaire &amp; granges</strong><p class="text-slate-500 mt-0.5">Division d'un lot bâti ou terrain constructible pour solder rapidement le portage.</p></div>
              <div class="py-2.5"><strong>04. Liquidité cible 85 k€ – 145 k€</strong><p class="text-slate-500 mt-0.5">Revente optimale entre 1 750 et 2 100 €/m² sur Millau pour primo-accédants et locatif.</p></div>
            </div>
          </section>
          <section class="bg-white dark:bg-slate-900 border border-stone-200 dark:border-slate-800 rounded-xl p-5 space-y-3">
            <h3 class="font-display text-sm font-bold">Grille de visite terrain (12100)</h3>
            <div class="divide-y divide-stone-100 dark:divide-slate-800">
              <div class="py-2.5"><strong>Colonnes Enedis &amp; Eau</strong><p class="text-slate-500 mt-0.5">Faisabilité colonne montante en façade ou cage d'escalier et compteurs individuels.</p></div>
              <div class="py-2.5"><strong>Périmètre ABF</strong><p class="text-slate-500 mt-0.5">Menuiseries bois peintes obligatoires en secteur sauvegardé (Rue Droite / Capelle).</p></div>
              <div class="py-2.5"><strong>Structure planchers &amp; toiture</strong><p class="text-slate-500 mt-0.5">Sonder solives autour des pièces humides, état zinguerie et couverture.</p></div>
              <div class="py-2.5"><strong>Zonage PPRI Tarn</strong><p class="text-slate-500 mt-0.5">Contrôle inondation sur Cureplat / Bords du Tarn avant aménagement RDC.</p></div>
            </div>
          </section>
        </div>
      </div>
    </main>
  </div>
</div>

<!-- MODAL SCORING V3 -->
<div id="modal-scoring" class="hidden fixed inset-0 z-50 flex items-center justify-center p-4 bg-slate-950/60 backdrop-blur-sm">
  <div class="bg-white dark:bg-slate-900 border border-stone-200 dark:border-slate-800 rounded-xl max-w-xl w-full p-6 shadow-xl relative">
    <button onclick="fermerModalScoring()" class="absolute right-4 top-4 text-slate-400 hover:text-slate-900 dark:hover:text-white cursor-pointer">✕</button>
    <div id="modal-qualite" class="text-xs font-semibold text-emerald-700 dark:text-emerald-400 mb-1"></div>
    <h3 id="modal-titre" class="font-display text-lg font-bold"></h3>
    <p id="modal-sous-titre" class="text-xs text-slate-500 mb-4"></p>
    <div id="modal-piliers" class="divide-y divide-stone-200 dark:divide-slate-800 border-y border-stone-200 dark:border-slate-800 text-xs"></div>
    <div class="mt-4 flex justify-end gap-2">
      <button onclick="fermerModalScoring()" class="px-3.5 py-2 rounded-lg border border-stone-200 dark:border-slate-700 text-xs font-semibold cursor-pointer">Fermer</button>
      <button id="modal-btn-simu" class="px-3.5 py-2 rounded-lg bg-slate-900 dark:bg-amber-500 text-white dark:text-slate-950 text-xs font-semibold cursor-pointer">Simuler</button>
    </div>
  </div>
</div>

<script>
const DATA = __DATA__;
const STATUT_KEY = 'millau_mdb_statuts_v1';
const THEME_KEY = 'millau_mdb_theme_v1';
let statuts = {};
try { statuts = JSON.parse(localStorage.getItem(STATUT_KEY) || '{}'); } catch (e) {}
let viewMode = 'grid';
let currentAuditDeal = DATA[0] || null;
let auditPostes = [];
let selectedStratId = 'cle_en_main';
let currentLots = [
  { id: '1', nom: 'Lot 1 : RDC / Garage', surface: 45, travaux: 18000, revente: 58000 },
  { id: '2', nom: 'Lot 2 : T2 1er étage', surface: 58, travaux: 42000, revente: 112000 },
  { id: '3', nom: 'Lot 3 : T3 2e étage', surface: 65, travaux: 46000, revente: 126000 }
];
let simuContext = { titre: 'Ensemble immobilier Millau', dpe: 'F', agence: 'Millau' };

function fmt(n) { return Math.round(Number(n) || 0).toLocaleString('fr-FR'); }
function sfmt(n) { const r = Math.round(Number(n) || 0); return (r > 0 ? '+' : '') + r.toLocaleString('fr-FR') + ' €'; }
function esc(s) { return String(s == null ? '' : s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;'); }
function safeUrl(u) { const s = String(u || '').trim(); return /^https?:\/\//i.test(s) ? esc(s) : '#'; }

function applyTheme(t) {
  document.documentElement.classList.toggle('dark', t === 'dark');
  const btn = document.getElementById('btn-theme');
  if (btn) btn.textContent = t === 'dark' ? 'Thème clair' : 'Thème sombre';
  try { localStorage.setItem(THEME_KEY, t); } catch (e) {}
}
function toggleTheme() { applyTheme(document.documentElement.classList.contains('dark') ? 'light' : 'dark'); }
applyTheme(localStorage.getItem(THEME_KEY) === 'dark' ? 'dark' : 'light');

function setTab(name) {
  const titles = { deals: 'Marché & Opportunités', audit: 'Audit Technique & Travaux', simu: 'Simulateur & Découpe MDB', strategie: 'Stratégie & Secteur Millau' };
  document.getElementById('top-title').textContent = titles[name] || '';
  ['deals', 'audit', 'simu', 'strategie'].forEach(t => {
    document.getElementById('tab-' + t).classList.toggle('hidden', t !== name);
    const nav = document.getElementById('nav-' + t);
    const mnav = document.getElementById('mnav-' + t);
    if (nav) nav.className = 'w-full text-left px-3.5 py-2.5 rounded-lg cursor-pointer ' + (t === name ? 'bg-slate-900 text-white dark:bg-amber-500 dark:text-slate-950' : 'text-slate-700 hover:bg-stone-100 dark:text-slate-300 dark:hover:bg-slate-800/70');
    if (mnav) mnav.className = 'px-3 py-1.5 rounded-md text-xs font-semibold whitespace-nowrap cursor-pointer ' + (t === name ? 'bg-slate-900 text-white dark:bg-amber-500 dark:text-slate-950' : 'text-slate-600 dark:text-slate-300');
  });
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

function setViewMode(m) {
  viewMode = m;
  document.getElementById('btn-view-grid').className = 'px-2.5 py-1 rounded-md text-xs font-semibold cursor-pointer ' + (m === 'grid' ? 'bg-white dark:bg-slate-900 shadow-xs' : 'text-slate-500');
  document.getElementById('btn-view-table').className = 'px-2.5 py-1 rounded-md text-xs font-semibold cursor-pointer ' + (m === 'table' ? 'bg-white dark:bg-slate-900 shadow-xs' : 'text-slate-500');
  renderDeals();
}

function getSecteur(a) {
  if (a.secteur) return a.secteur;
  const t = ((a.titre || '') + ' ' + (a.mots_detectes || '')).toLowerCase();
  if (/droite|halles|capelle|foch/.test(t)) return 'Centre Ancien';
  if (/mandarous|jaur[èe]s|bonald/.test(t)) return 'Mandarous / Jaurès';
  if (/tarn|faubourg|viaduc|cureplat|pouncho/.test(t)) return 'Faubourgs / Tarn';
  if (/gare|malhourtet|beauregard/.test(t)) return 'Gare / Malhourtet';
  return 'Périphérie Millau';
}

function getType(a) {
  if (a.type_bien) return a.type_bien;
  const t = ((a.titre || '') + ' ' + (a.mots_detectes || '')).toLowerCase();
  if (/immeuble|monopropri|bâtisse/.test(t)) return 'immeuble';
  if (/grange|plateau|remise|atelier/.test(t)) return 'grange';
  if (/appartement|t2|t3|t4|studio/.test(t)) return 'appartement';
  return 'maison';
}

function updatePipelineCounts() {
  const c = { etude: 0, visite: 0, offre: 0 };
  DATA.forEach(a => {
    const st = statuts[a.url] || (a.nouvelle ? 'nouveau' : 'a_etudier');
    if (st === 'visite') c.visite++;
    else if (st === 'offre') c.offre++;
    else if (st !== 'ecarte') c.etude++;
  });
  document.getElementById('pipe-total').textContent = DATA.length;
  document.getElementById('pipe-etude').textContent = c.etude;
  document.getElementById('pipe-visite').textContent = c.visite;
  document.getElementById('pipe-offre').textContent = c.offre;
}

function changeStatutByIdx(idx, val) {
  const a = DATA[idx];
  if (!a || !a.url) return;
  statuts[a.url] = val;
  try { localStorage.setItem(STATUT_KEY, JSON.stringify(statuts)); } catch (e) {}
  updatePipelineCounts();
}

function getFiltered() {
  const q = (document.getElementById('q').value || '').toLowerCase();
  const ft = document.getElementById('f-type').value;
  const fs = document.getElementById('f-secteur').value;
  const fd = document.getElementById('f-dpe').value;
  const fsort = document.getElementById('f-sort').value;
  const fdrops = document.getElementById('f-drops').checked;
  const fst = document.getElementById('f-statut').value;

  const list = DATA.filter(a => {
    const sec = getSecteur(a), typ = getType(a);
    const txt = ((a.titre || '') + ' ' + (a.mots_detectes || '') + ' ' + (a.agence || '') + ' ' + sec).toLowerCase();
    if (q && !txt.includes(q)) return false;
    if (ft !== 'all' && typ !== ft) return false;
    if (fs !== 'all' && sec !== fs) return false;
    if (fd === 'passoires' && a.dpe !== 'F' && a.dpe !== 'G') return false;
    if ((fd === 'F' || fd === 'G') && a.dpe !== fd) return false;
    if (fdrops && !(a.baisse_prix_pct > 0 || a.a_baisse)) return false;
    const cur = statuts[a.url] || (a.nouvelle ? 'nouveau' : 'a_etudier');
    if (fst !== 'all' && cur !== fst) return false;
    return true;
  });

  list.sort((a, b) => {
    if (fsort === 'score_desc') return (b.score_v3 || b.score || 0) - (a.score_v3 || a.score || 0);
    if (fsort === 'marge_desc') return (b.marge_estimee_montant || 0) - (a.marge_estimee_montant || 0);
    if (fsort === 'prix_asc') return (a.prix || 9e9) - (b.prix || 9e9);
    if (fsort === 'prix_desc') return (b.prix || 0) - (a.prix || 0);
    if (fsort === 'm2_asc') return (a.prix_m2 || 9e9) - (b.prix_m2 || 9e9);
    if (fsort === 'surface_desc') return (b.surface || 0) - (a.surface || 0);
    return 0;
  });
  return list;
}

function renderScatter(list) {
  const valid = list.filter(a => a.surface > 0 && a.prix_m2 > 0);
  const wrap = document.getElementById('scatter-wrap');
  if (!valid.length) { wrap.innerHTML = '<div class="py-10 text-center text-xs text-slate-500">Aucun point exploitable.</div>'; return; }
  const W = 760, H = 220, pL = 52, pR = 20, pT = 20, pB = 30;
  const maxS = Math.max(260, ...valid.map(a => a.surface + 20));
  const maxM = Math.max(2100, ...valid.map(a => a.prix_m2 + 150));
  const xp = s => pL + ((s - 40) / Math.max(100, maxS - 40)) * (W - pL - pR);
  const yp = m => H - pB - ((m - 400) / Math.max(800, maxM - 400)) * (H - pT - pB);

  let lines = [600, 1100, 1500, 1900].map(v => {
    const y = yp(v);
    const col = v === 1900 ? '#059669' : (v === 1100 ? '#D97706' : 'currentColor');
    const dash = (v === 1900 || v === 1100) ? 'stroke-dasharray="5 4"' : '';
    const op = (v === 1900 || v === 1100) ? '0.6' : '0.12';
    return `<line x1="${pL}" x2="${W-pR}" y1="${y}" y2="${y}" stroke="${col}" stroke-opacity="${op}" ${dash} /><text x="${pL-6}" y="${y+3}" text-anchor="end" class="fill-slate-400 text-[10px] font-mono">${v}€</text>`;
  }).join('');

  const pts = valid.map(a => {
    const cx = Math.max(pL+6, Math.min(W-pR-6, xp(a.surface)));
    const cy = Math.max(pT+6, Math.min(H-pB-6, yp(a.prix_m2)));
    const fill = a.score_v3 >= 7.5 ? '#059669' : (a.score_v3 >= 5.5 ? '#D97706' : '#94A3B8');
    const idx = DATA.indexOf(a);
    return `<circle cx="${cx}" cy="${cy}" r="6" fill="${fill}" stroke="#fff" stroke-width="1.5" class="cursor-pointer" onmouseover="hoverScatter(${idx})" onclick="lancerAudit(${idx})" />`;
  }).join('');

  wrap.innerHTML = `<svg viewBox="0 0 ${W} ${H}" class="w-full h-auto max-h-56 select-none">${lines}<text x="${W-pR}" y="${yp(1900)-5}" text-anchor="end" class="fill-emerald-600 text-[10px]">Revente cible 1 900 €/m²</text><text x="${W-pR}" y="${yp(1100)-5}" text-anchor="end" class="fill-amber-600 text-[10px]">Seuil achat 1 100 €/m²</text>${pts}</svg>`;
}

function hoverScatter(idx) {
  const a = DATA[idx];
  if (a) document.getElementById('scatter-hover').innerHTML = `<strong>${esc(a.titre)}</strong> — <span class="font-mono">${esc(a.agence)} · ${fmt(a.prix)} € · ${a.surface} m² · ${a.prix_m2} €/m² · Score ${Number(a.score_v3).toFixed(1)}/10</span>`;
}

function renderDeals() {
  const list = getFiltered();
  updatePipelineCounts();
  renderScatter(list);

  const validM2 = DATA.map(a => a.prix_m2).filter(Boolean).sort((a, b) => a - b);
  const med = validM2.length ? validM2[Math.floor(validM2.length / 2)] : null;
  const drops = DATA.filter(a => a.baisse_prix_pct > 0 || a.a_baisse).length;
  const pass = DATA.filter(a => a.dpe === 'F' || a.dpe === 'G').length;
  const top = list.filter(a => a.marge_estimee_montant >= 35000).length;

  document.getElementById('kpi-cards').innerHTML =
    `<div class="bg-white dark:bg-slate-900 border border-stone-200 dark:border-slate-800 rounded-xl p-4"><div class="text-xs text-slate-500">Biens suivis</div><div class="mt-1 font-mono text-2xl font-bold">${DATA.length}</div><div class="mt-0.5 text-xs font-mono text-emerald-700 dark:text-emerald-400">${top} à marge ≥ 35 k€</div></div>` +
    `<div class="bg-white dark:bg-slate-900 border border-stone-200 dark:border-slate-800 rounded-xl p-4"><div class="text-xs text-slate-500">Prix médian affiché</div><div class="mt-1 font-mono text-2xl font-bold whitespace-nowrap">${med ? med + ' €/m²' : '–'}</div><div class="mt-0.5 text-xs font-mono text-slate-500">Cible 1 900 €/m²</div></div>` +
    `<div class="bg-white dark:bg-slate-900 border border-stone-200 dark:border-slate-800 rounded-xl p-4"><div class="text-xs text-slate-500">Baisses détectées</div><div class="mt-1 font-mono text-2xl font-bold text-amber-700 dark:text-amber-400">${drops}</div><div class="mt-0.5 text-xs text-slate-500">Levier négociation</div></div>` +
    `<div class="bg-white dark:bg-slate-900 border border-stone-200 dark:border-slate-800 rounded-xl p-4"><div class="text-xs text-slate-500">Passoires F &amp; G</div><div class="mt-1 font-mono text-2xl font-bold text-rose-700 dark:text-rose-400">${pass}</div><div class="mt-0.5 text-xs text-slate-500">Décote travaux</div></div>`;

  document.getElementById('deals-count').textContent = list.length + ' résultat(s)';
  const box = document.getElementById('deals-container');
  if (!list.length) { box.innerHTML = '<div class="bg-white dark:bg-slate-900 border border-stone-200 dark:border-slate-800 rounded-xl p-10 text-center text-xs text-slate-500">Aucun bien ne correspond aux critères.</div>'; return; }

  if (viewMode === 'table') {
    box.innerHTML = `<div class="bg-white dark:bg-slate-900 border border-stone-200 dark:border-slate-800 rounded-xl overflow-x-auto"><table class="w-full text-xs text-left"><thead class="bg-stone-50 dark:bg-slate-950 text-slate-500 border-b border-stone-200 dark:border-slate-800"><tr><th class="py-3 px-4">Bien &amp; Secteur</th><th class="py-3 px-3">Agence</th><th class="py-3 px-3 text-right">Prix FAI</th><th class="py-3 px-3 text-right">Surf.</th><th class="py-3 px-3 text-right">€/m²</th><th class="py-3 px-3 text-center">DPE</th><th class="py-3 px-3 text-right">Marge est.</th><th class="py-3 px-3 text-right">Score</th><th class="py-3 px-4 text-right">Actions</th></tr></thead><tbody class="divide-y divide-stone-100 dark:divide-slate-800">${list.map(a => {
      const idx = DATA.indexOf(a);
      return `<tr class="hover:bg-stone-50 dark:hover:bg-slate-800/40"><td class="py-3 px-4 max-w-xs"><div class="font-semibold truncate">${esc(a.titre)}</div><div class="text-slate-500">${esc(getSecteur(a))}${a.baisse_prix_pct>0?' · -'+a.baisse_prix_pct+'%':''}</div></td><td class="py-3 px-3">${esc(a.agence)}</td><td class="py-3 px-3 text-right font-mono font-semibold">${a.prix?fmt(a.prix)+' €':'–'}</td><td class="py-3 px-3 text-right font-mono">${a.surface?a.surface+' m²':'–'}</td><td class="py-3 px-3 text-right font-mono">${a.prix_m2?a.prix_m2+' €':'–'}</td><td class="py-3 px-3 text-center font-mono font-bold">${esc(a.dpe)}</td><td class="py-3 px-3 text-right font-mono font-semibold ${a.marge_estimee_montant>=0?'text-emerald-700 dark:text-emerald-400':'text-rose-700'}">${sfmt(a.marge_estimee_montant)}</td><td class="py-3 px-3 text-right font-mono font-bold"><button onclick="afficherModalScoring(${idx})" class="hover:underline cursor-pointer">${Number(a.score_v3||0).toFixed(1)}/10</button></td><td class="py-3 px-4 text-right whitespace-nowrap space-x-2"><button onclick="lancerAudit(${idx})" class="hover:underline cursor-pointer">Audit</button><button onclick="simulerDeal(${idx})" class="font-semibold text-amber-700 dark:text-amber-400 hover:underline cursor-pointer">Simuler</button></td></tr>`;
    }).join('')}</tbody></table></div>`;
    return;
  }

  box.innerHTML = `<div class="grid grid-cols-1 md:grid-cols-2 gap-4">${list.map(a => {
    const idx = DATA.indexOf(a);
    const cur = statuts[a.url] || (a.nouvelle ? 'nouveau' : 'a_etudier');
    const thumb = (a.photos && a.photos[0]) ? `<img src="${safeUrl(a.photos[0])}" referrerpolicy="no-referrer" class="w-20 h-16 rounded-lg object-cover shrink-0 border border-stone-200 dark:border-slate-800" onerror="this.style.display='none'" />` : '';
    const href = safeUrl(a.url);
    return `<article class="bg-white dark:bg-slate-900 border border-stone-200 dark:border-slate-800 rounded-xl p-5 flex flex-col justify-between gap-4">
      <div class="space-y-3">
        <div class="flex items-center justify-between gap-2 text-xs">
          <div class="text-slate-500 dark:text-slate-400 truncate"><strong class="text-slate-800 dark:text-slate-200">${esc(a.agence)}</strong> · ${esc(getSecteur(a))} · DPE ${esc(a.dpe)}${a.baisse_prix_pct>0?' · <span class="font-mono font-semibold text-rose-700 dark:text-rose-400">-'+a.baisse_prix_pct+'%</span>':''}</div>
          <button onclick="afficherModalScoring(${idx})" class="font-mono font-bold hover:underline shrink-0 cursor-pointer">Score ${Number(a.score_v3||0).toFixed(1)}/10</button>
        </div>
        <div class="flex items-start gap-3">${thumb}<div class="min-w-0 flex-1"><h3 class="font-display text-base font-bold leading-snug line-clamp-2"><a href="${href}" target="_blank" rel="noopener noreferrer" class="hover:underline">${esc(a.titre || a.url)}</a></h3><p class="text-xs text-slate-500 mt-1 line-clamp-1">${esc(a.mots_detectes || '')}</p></div></div>
        <div class="grid grid-cols-4 gap-2 pt-3 border-t border-stone-100 dark:border-slate-800 text-xs">
          <div><span class="text-slate-500 block">Prix FAI</span><span class="font-mono font-bold">${a.prix?fmt(a.prix)+' €':'–'}</span></div>
          <div><span class="text-slate-500 block">Surface</span><span class="font-mono font-semibold">${a.surface?a.surface+' m²':'–'}</span></div>
          <div><span class="text-slate-500 block">Prix / m²</span><span class="font-mono font-semibold">${a.prix_m2?a.prix_m2+' €':'–'}</span></div>
          <div><span class="text-slate-500 block">Marge est.</span><span class="font-mono font-bold ${a.marge_estimee_montant>=0?'text-emerald-700 dark:text-emerald-400':'text-rose-700'}">${sfmt(a.marge_estimee_montant)}</span></div>
        </div>
      </div>
      <div class="pt-3 border-t border-stone-100 dark:border-slate-800 flex items-center justify-between gap-2">
        <div class="flex items-center gap-2">
          <select onchange="changeStatutByIdx(${idx}, this.value)" class="text-xs bg-stone-50 dark:bg-slate-950 border border-stone-200 dark:border-slate-700 rounded-md px-2 py-1">
            <option value="nouveau" ${cur==='nouveau'?'selected':''}>Nouveau</option>
            <option value="a_etudier" ${cur==='a_etudier'?'selected':''}>À étudier</option>
            <option value="visite" ${cur==='visite'?'selected':''}>Visite planifiée</option>
            <option value="offre" ${cur==='offre'?'selected':''}>Offre envoyée</option>
            <option value="ecarte" ${cur==='ecarte'?'selected':''}>Écarté</option>
          </select>
          <a href="${href}" target="_blank" rel="noopener noreferrer" class="text-xs text-slate-500 hover:underline">Annonce ↗</a>
        </div>
        <div class="flex items-center gap-2">
          <button onclick="lancerAudit(${idx})" class="px-3 py-1.5 rounded-lg border border-stone-300 dark:border-slate-700 text-xs font-semibold cursor-pointer">Audit</button>
          <button onclick="simulerDeal(${idx})" class="px-3 py-1.5 rounded-lg bg-slate-900 dark:bg-amber-500 text-white dark:text-slate-950 text-xs font-semibold cursor-pointer">Simuler</button>
        </div>
      </div>
    </article>`;
  }).join('')}</div>`;
}

function initAuditSelector() {
  const sel = document.getElementById('ap-select-deal');
  sel.innerHTML = DATA.map((d, i) => `<option value="${i}">${esc((d.titre||'Bien').slice(0,52))} — ${d.prix?fmt(d.prix)+' €':''}</option>`).join('');
  if (DATA.length) chargerAudit(0, false);
}

function changerDealAudit(val) { if (val !== '') chargerAudit(parseInt(val, 10), false); }
function lancerAudit(idx) { chargerAudit(idx, true); }

function chargerAudit(idx, switchTab) {
  const deal = DATA[idx];
  if (!deal) return;
  currentAuditDeal = deal;
  document.getElementById('ap-select-deal').value = idx;
  const sec = getSecteur(deal);
  document.getElementById('ap-meta').textContent = `Audit Technique · ${sec} · ${deal.agence}`;
  document.getElementById('ap-titre').textContent = deal.titre || deal.url;
  document.getElementById('ap-metrics').textContent = `Prix FAI : ${deal.prix?fmt(deal.prix)+' €':'–'} · Surface : ${deal.surface?deal.surface+' m²':'N/C'} · DPE ${deal.dpe}`;

  const pg = document.getElementById('ap-photos-grid');
  pg.innerHTML = (deal.photos && deal.photos.length) ? deal.photos.map(s => `<img src="${safeUrl(s)}" referrerpolicy="no-referrer" class="aspect-[4/3] w-full rounded-lg object-cover border border-stone-200 dark:border-slate-800" onerror="this.style.display='none'" />`).join('') : '<div class="col-span-2 py-8 text-center text-xs text-slate-500 border border-dashed border-stone-300 dark:border-slate-700 rounded-lg">Aucune photo extraite</div>';
  document.getElementById('ap-photos-constat').innerHTML = `Annonce source : <a href="${safeUrl(deal.url)}" target="_blank" rel="noopener noreferrer" class="font-semibold underline">${esc(deal.agence)} ↗</a>`;

  document.getElementById('ap-diagnostics-list').innerHTML =
    `<div class="py-2.5"><strong>Électricité &amp; division Enedis</strong><p class="text-slate-500 mt-0.5">Colonne montante et compteurs Linky individuels à prévoir par lot.</p></div>` +
    `<div class="py-2.5"><strong>Performance énergétique (DPE ${esc(deal.dpe)})</strong><p class="text-slate-500 mt-0.5">Sortie DPE C par ITI + PAC air/air + VMC hygroréglable.</p></div>` +
    `<div class="py-2.5"><strong>Urbanisme (${esc(sec)})</strong><p class="text-slate-500 mt-0.5">${sec==='Centre Ancien'?'Périmètre ABF : menuiseries bois et enduit chaux.':'Contrôler PLU / PPRI avant division RDC.'}</p></div>`;

  const surf = deal.surface && deal.surface > 20 ? deal.surface : 140;
  const coeff = deal.dpe === 'G' ? 1.15 : (deal.dpe === 'F' ? 1.0 : 0.85);
  auditPostes = [
    { poste: 'Colonnes électriques & Enedis', diag: 'Compteur unique', preco: 'Colonne montante & Linky individuels', budget: surf > 130 ? 9500 : 5800, actif: true },
    { poste: 'Menuiseries & Isolation (ITI)', diag: 'DPE ' + deal.dpe, preco: sec === 'Centre Ancien' ? 'Bois double vitrage ABF + ITI' : 'Double vitrage + ITI 140 mm', budget: Math.round(surf * 185 * coeff), actif: true },
    { poste: 'Chauffage, ECS & VMC', diag: 'Équipement vétuste', preco: 'Ballon thermo + PAC air/air + VMC', budget: Math.round(surf * 135 * coeff), actif: true },
    { poste: 'Second-œuvre & finitions', diag: 'Distribution datée', preco: 'Cuisines, douches, sols, peintures', budget: Math.round(surf * 230), actif: true },
    { poste: 'Parties communes & toiture', diag: 'Couverture / cage', preco: 'Révision toiture & façade', budget: surf > 150 ? 11500 : 6500, actif: true }
  ];
  selectedStratId = 'cle_en_main';
  renderAuditTravaux();
  if (switchTab) setTab('audit');
}

function renderAuditTravaux() {
  const tbody = document.getElementById('ap-travaux-tbody');
  tbody.innerHTML = auditPostes.map((p, i) => `<tr class="${p.actif?'':'opacity-50'}"><td class="py-2 px-3 text-center"><input type="checkbox" ${p.actif?'checked':''} onchange="auditPostes[${i}].actif=this.checked;renderAuditTravaux()" /></td><td class="py-2 px-3 font-semibold">${p.poste}</td><td class="py-2 px-3 text-slate-500">${p.diag}</td><td class="py-2 px-3">${p.preco}</td><td class="py-2 px-3 text-right"><input type="number" step="500" value="${p.budget}" oninput="auditPostes[${i}].budget=Math.max(0,parseFloat(this.value)||0);renderAuditStrategies()" class="w-28 px-2 py-1 text-right font-mono bg-stone-50 dark:bg-slate-950 border border-stone-200 dark:border-slate-700 rounded" /></td></tr>`).join('');
  renderAuditStrategies();
}

function renderAuditStrategies() {
  if (!currentAuditDeal) return;
  const totalTrav = auditPostes.reduce((s, p) => s + (p.actif ? p.budget : 0), 0);
  document.getElementById('ap-total-travaux-badge').textContent = 'Enveloppe active : ' + fmt(totalTrav) + ' €';
  const prix = currentAuditDeal.prix || 140000;
  const surf = currentAuditDeal.surface && currentAuditDeal.surface > 20 ? currentAuditDeal.surface : 140;
  const notaire = Math.round(prix * 0.00715), geom = surf > 120 ? 3200 : 1800;

  const strats = [
    { id: 'plateaux', titre: 'Vente Plateaux Bruts', delai: '4-5 mois', mois: 5, trav: Math.round(Math.min(34000, totalTrav * 0.28)), rev: Math.round(surf * 1260), desc: 'Division, parties communes et réseaux Enedis uniquement.' },
    { id: 'cle_en_main', titre: 'Rénovation Clé en main', delai: '10-12 mois', mois: 11, trav: totalTrav, rev: Math.round(surf * 1950), desc: 'Réhabilitation DPE C prête à habiter (CA maximal).' },
    { id: 'hybride', titre: 'Formule Hybride', delai: '7-8 mois', mois: 7, trav: Math.round(totalTrav * 0.65), rev: Math.round(surf * 1680), desc: 'Rénovation des lots principaux + plateau brut.' }
  ].map(s => {
    const port = Math.round((prix + s.trav) * (s.id === 'cle_en_main' ? 0.05 : s.id === 'hybride' ? 0.035 : 0.025));
    s.marge = s.rev - (prix + notaire + s.trav + geom + port);
    s.pct = s.rev ? ((s.marge / s.rev) * 100).toFixed(1) : '0';
    return s;
  });
  window.__auditStrats = strats;

  document.getElementById('ap-scenarios-cards').innerHTML = strats.map(s => {
    const sel = s.id === selectedStratId;
    return `<button onclick="selectedStratId='${s.id}';renderAuditStrategies()" class="text-left p-4 rounded-xl border cursor-pointer ${sel?'border-slate-900 bg-stone-50 dark:border-amber-500 dark:bg-amber-500/10':'border-stone-200 dark:border-slate-800'}"><div class="flex justify-between text-xs text-slate-500 mb-1"><span>Délai : ${s.delai}</span></div><div class="font-display text-sm font-bold mb-1">${s.titre}</div><p class="text-xs text-slate-500 mb-3">${s.desc}</p><div class="pt-2 border-t border-stone-200 dark:border-slate-800 text-xs space-y-1"><div class="flex justify-between"><span>Travaux</span><strong class="font-mono">${fmt(s.trav)} €</strong></div><div class="flex justify-between"><span>Revente</span><strong class="font-mono">${fmt(s.rev)} €</strong></div><div class="flex justify-between"><span>Marge brute</span><strong class="font-mono ${s.marge>=0?'text-emerald-700 dark:text-emerald-400':'text-rose-700'}">${sfmt(s.marge)} (${s.pct}%)</strong></div></div></button>`;
  }).join('');

  const cur = strats.find(s => s.id === selectedStratId) || strats[1];
  document.getElementById('ap-verdict-texte').innerHTML = `Scénario retenu : <strong>${cur.titre}</strong> · Travaux <strong class="font-mono">${fmt(cur.trav)} €</strong> pour revente <strong class="font-mono">${fmt(cur.rev)} €</strong>.`;
}

function injecterScenarioDansSimulateur() {
  if (!currentAuditDeal || !window.__auditStrats) return;
  const sc = window.__auditStrats.find(s => s.id === selectedStratId) || window.__auditStrats[1];
  const idx = DATA.indexOf(currentAuditDeal);
  simulerDeal(idx, sc.trav, sc.mois, sc.rev);
}

function simulerDeal(idx, customTrav, customMois, customRev) {
  const a = DATA[idx];
  if (!a) return;
  const prix = a.prix || 140000;
  const surf = a.surface && a.surface > 20 ? a.surface : 140;
  const trav = customTrav || Math.round(surf * (a.dpe === 'G' ? 780 : a.dpe === 'F' ? 680 : 550));
  const rev = customRev || Math.round(surf * 1900);
  document.getElementById('sim-prix').value = prix;
  document.getElementById('sim-nego').value = Math.round(prix * 0.08);
  document.getElementById('sim-mois').value = customMois || 9;
  simuContext = { titre: a.titre || 'Bien Millau', dpe: a.dpe || '?', agence: a.agence || 'Agence' };
  document.getElementById('simu-context-titre').textContent = simuContext.titre;
  document.getElementById('simu-context-meta').textContent = `Simulation financière MDB · ${simuContext.agence} · DPE ${simuContext.dpe}`;

  if (surf > 150) {
    currentLots = [
      { id: '1', nom: 'Lot 1 : RDC / Local', surface: Math.round(surf*0.3), travaux: Math.round(trav*0.22), revente: Math.round(rev*0.24) },
      { id: '2', nom: 'Lot 2 : 1er étage T2', surface: Math.round(surf*0.35), travaux: Math.round(trav*0.39), revente: Math.round(rev*0.38) },
      { id: '3', nom: 'Lot 3 : 2e étage T2/T3', surface: Math.round(surf*0.35), travaux: Math.round(trav*0.39), revente: Math.round(rev*0.38) }
    ];
  } else {
    currentLots = [
      { id: '1', nom: 'Lot 1', surface: Math.round(surf*0.5), travaux: Math.round(trav*0.5), revente: Math.round(rev*0.5) },
      { id: '2', nom: 'Lot 2', surface: Math.round(surf*0.5), travaux: Math.round(trav*0.5), revente: Math.round(rev*0.5) }
    ];
  }
  renderLotsTable();
  setTab('simu');
}

function renderLotsTable() {
  document.getElementById('lots-table-body').innerHTML = currentLots.map((l, i) => `<tr>
    <td class="py-1.5 pr-2"><input type="text" value="${esc(l.nom)}" oninput="currentLots[${i}].nom=this.value" class="w-full px-2 py-1 bg-stone-50 dark:bg-slate-950 border border-stone-200 dark:border-slate-700 rounded text-xs" /></td>
    <td class="py-1.5 pr-2"><input type="number" value="${l.surface}" oninput="currentLots[${i}].surface=parseFloat(this.value)||0;calculerSimu()" class="w-full px-2 py-1 text-right font-mono bg-stone-50 dark:bg-slate-950 border border-stone-200 dark:border-slate-700 rounded text-xs" /></td>
    <td class="py-1.5 pr-2"><input type="number" step="500" value="${l.travaux}" oninput="currentLots[${i}].travaux=parseFloat(this.value)||0;calculerSimu()" class="w-full px-2 py-1 text-right font-mono bg-stone-50 dark:bg-slate-950 border border-stone-200 dark:border-slate-700 rounded text-xs" /></td>
    <td class="py-1.5 pr-2"><input type="number" step="1000" value="${l.revente}" oninput="currentLots[${i}].revente=parseFloat(this.value)||0;calculerSimu()" class="w-full px-2 py-1 text-right font-mono font-bold bg-stone-50 dark:bg-slate-950 border border-stone-200 dark:border-slate-700 rounded text-xs" /></td>
    <td class="py-1.5 pr-2 text-right font-mono text-slate-500">${l.surface>0?Math.round(l.revente/l.surface):0} €</td>
    <td class="py-1.5 text-right"><button onclick="if(currentLots.length>1){currentLots.splice(${i},1);renderLotsTable()}" class="text-slate-400 hover:text-rose-600 cursor-pointer">✕</button></td>
  </tr>`).join('');
  calculerSimu();
}

function ajouterLot() {
  currentLots.push({ id: Date.now()+'', nom: 'Lot ' + (currentLots.length + 1), surface: 48, travaux: 36000, revente: 91000 });
  renderLotsTable();
}

function calculerSimu() {
  const prix = parseFloat(document.getElementById('sim-prix').value) || 0;
  const nego = parseFloat(document.getElementById('sim-nego').value) || 0;
  const prixNet = Math.max(0, prix - nego);
  const tauxNot = parseFloat(document.getElementById('sim-notaire').value) || 0.00715;
  const notaire = Math.round(prixNet * tauxNot);
  const geom = parseFloat(document.getElementById('sim-geometre').value) || 0;
  const divers = parseFloat(document.getElementById('sim-divers').value) || 0;
  const mois = parseFloat(document.getElementById('sim-mois').value) || 8;
  const taux = parseFloat(document.getElementById('sim-taux').value) || 3.8;
  const apport = parseFloat(document.getElementById('sim-apport').value) || 0;
  const withTva = document.getElementById('sim-tva-marge').checked;

  const surf = currentLots.reduce((s, l) => s + (l.surface || 0), 0);
  const trav = currentLots.reduce((s, l) => s + (l.travaux || 0), 0);
  const ca = currentLots.reduce((s, l) => s + (l.revente || 0), 0);
  document.getElementById('total-surf').textContent = surf + ' m²';
  document.getElementById('total-travaux-lots').textContent = fmt(trav) + ' €';
  document.getElementById('total-revente-lots').textContent = fmt(ca) + ' €';

  const besoin = prixNet + notaire + trav + geom + divers;
  const emp = Math.max(0, besoin - apport);
  const port = Math.round(emp * (taux / 100) * (mois / 12) + emp * 0.01);
  const rev = besoin + port;
  const tva = (withTva && ca > prixNet) ? Math.round(((ca - prixNet) * 0.2) / 1.2) : 0;
  const mb = ca - rev - tva;
  const pct = ca > 0 ? ((mb / ca) * 100).toFixed(1) : '0';
  const is = mb <= 0 ? 0 : (mb <= 42500 ? Math.round(mb * 0.15) : Math.round(42500 * 0.15 + (mb - 42500) * 0.25));
  const mn = mb - is;
  const roi = apport > 0 ? Math.round((mn / apport) * 100) : 0;
  const tri = apport > 0 && mois > 0 ? Math.round(roi * (12 / mois)) : 0;

  document.getElementById('res-achat').textContent = fmt(prixNet) + ' €';
  document.getElementById('res-notaire').textContent = fmt(notaire) + ' €';
  document.getElementById('res-travaux').textContent = fmt(trav) + ' €';
  document.getElementById('res-geometre').textContent = fmt(geom + divers) + ' €';
  document.getElementById('res-portage').textContent = fmt(port) + ' €';
  document.getElementById('res-revient').textContent = fmt(rev) + ' €';
  document.getElementById('res-revente').textContent = fmt(ca) + ' €';
  document.getElementById('row-tva').classList.toggle('hidden', !withTva);
  document.getElementById('res-tva').textContent = '-' + fmt(tva) + ' €';
  document.getElementById('res-is').textContent = (is > 0 ? '-' : '') + fmt(is) + ' €';
  document.getElementById('res-marge-brute').textContent = sfmt(mb);
  document.getElementById('res-marge-pct').textContent = pct + '% CA';
  document.getElementById('res-marge-nette').textContent = sfmt(mn);
  document.getElementById('res-roi').textContent = roi + '%';
  document.getElementById('res-tri').textContent = tri + '% / an';
  document.getElementById('res-gain-notaire').textContent = '+' + fmt(Math.max(0, Math.round(prixNet * (0.078 - tauxNot)))) + ' €';

  const sens = [
    ['Prudent (-10% revente / +15% travaux)', -0.10, 0.15],
    ['Médian (-5% revente / +10% travaux)', -0.05, 0.10],
    ['Base simulée (nominal)', 0, 0],
    ['Optimiste (+5% revente)', 0.05, 0]
  ].map(([lbl, vr, vt]) => {
    const r2 = Math.round(ca * (1 + vr)), t2 = Math.round(trav * (1 + vt));
    const c2 = prixNet + notaire + t2 + geom + divers + port;
    const tv2 = (withTva && r2 > prixNet) ? Math.round(((r2 - prixNet) * 0.2) / 1.2) : 0;
    const m2 = r2 - c2 - tv2;
    const p2 = r2 > 0 ? ((m2 / r2) * 100).toFixed(1) : '0';
    return `<div class="py-2 flex justify-between"><span class="text-slate-600 dark:text-slate-300">${lbl}</span><strong class="font-mono ${m2>=25000?'text-emerald-700 dark:text-emerald-400':(m2>=0?'text-amber-700':'text-rose-700')}">${sfmt(m2)} (${p2}%)</strong></div>`;
  }).join('');
  document.getElementById('simu-sensibilite').innerHTML = sens;

  document.getElementById('strat-pitch').textContent =
    `Objet : Offre d'achat ferme — ${simuContext.titre.slice(0, 60)} (${simuContext.agence})\n\nBonjour,\n\nSuite à notre étude technique et compte tenu du DPE ${simuContext.dpe} ainsi que de l'enveloppe travaux estimée à ${fmt(trav)} €, nous vous transmettons une offre ferme à ${fmt(prixNet)} € FAI.\n\nOpérateur Marchand de Biens sur Millau, notre financement professionnel est validé sans condition suspensive de prêt, pour une signature sous 30 à 45 jours.`;
}

function copierPitch() {
  try { navigator.clipboard.writeText(document.getElementById('strat-pitch').textContent); } catch (e) {}
  const b = document.getElementById('btn-copy-pitch');
  b.textContent = 'Offre copiée';
  setTimeout(() => { b.textContent = 'Copier le courrier'; }, 2000);
}

function afficherModalScoring(idx) {
  const a = DATA[idx];
  if (!a) return;
  document.getElementById('modal-titre').textContent = a.titre || a.url;
  document.getElementById('modal-sous-titre').textContent = `${a.agence} · ${getSecteur(a)} · ${a.prix?fmt(a.prix)+' €':'–'} · ${a.surface?a.surface+' m²':'–'} · DPE ${a.dpe}`;
  const mp = a.marge_estimee_pct || 0;
  const p1 = mp >= 22 ? 35 : mp >= 18 ? 30 : mp >= 14 ? 20 : mp >= 10 ? 10 : 0;
  const p2 = ((a.titre||'').toLowerCase().includes('immeuble') || (a.titre||'').toLowerCase().includes('monopropri')) ? 25 : ((a.surface||0)>=140 ? 18 : 10);
  const p3 = (a.dpe==='G'?8:a.dpe==='F'?6:0) + (a.baisse_prix_pct>=10?8:a.baisse_prix_pct>=5?5:0) + ((a.prix_m2&&a.prix_m2<850)?4:0);
  const p4 = ((a.prix&&a.prix<=150000)?8:6) + 8;
  document.getElementById('modal-qualite').textContent = `Score V3 : ${Number(a.score_v3||0).toFixed(1)} / 10`;
  document.getElementById('modal-piliers').innerHTML = [
    ['01. Marge prévisionnelle (35%)', p1, 35, `Marge estimée ${sfmt(a.marge_estimee_montant)} (${mp}% CA à 1 900 €/m²).`],
    ['02. Découpe & création de valeur (25%)', p2, 25, `Surface ${a.surface||'–'} m², potentiel multi-lots.`],
    ['03. Levier de négociation (20%)', Math.min(20, p3), 20, `DPE ${a.dpe}, baisse constatée -${a.baisse_prix_pct||0}%.`],
    ['04. Sécurité & liquidité (20%)', Math.min(20, p4), 20, `Ticket d'acquisition sur le marché millavois.`]
  ].map(x => `<div class="py-3"><div class="flex justify-between font-semibold"><span>${x[0]}</span><span class="font-mono">${x[1]} / ${x[2]} pts</span></div><p class="text-slate-500 mt-1">${x[3]}</p></div>`).join('');
  document.getElementById('modal-btn-simu').onclick = () => { fermerModalScoring(); simulerDeal(idx); };
  document.getElementById('modal-scoring').classList.remove('hidden');
}

function fermerModalScoring() { document.getElementById('modal-scoring').classList.add('hidden'); }

function exporterCsv() {
  const list = getFiltered();
  const h = ['agence','titre','secteur','prix','surface','prix_m2','dpe','score_v3','marge_estimee_montant','url'];
  const rows = list.map(a => [a.agence, a.titre, getSecteur(a), a.prix||'', a.surface||'', a.prix_m2||'', a.dpe, a.score_v3, a.marge_estimee_montant, a.url].map(v => `"${String(v).replace(/"/g,'""')}"`).join(';'));
  const blob = new Blob(['\uFEFF' + [h.join(';'), ...rows].join('\n')], { type: 'text/csv;charset=utf-8;' });
  const link = document.createElement('a');
  link.href = URL.createObjectURL(blob);
  link.download = 'millau-mdb-selection.csv';
  link.click();
}

setTab('deals');
setViewMode('grid');
initAuditSelector();
renderLotsTable();
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

    log.info("🚀 Démarrage du scraper MDB Millau v3.0 (état actuel : %d annonces en mémoire)", len(etat))

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
